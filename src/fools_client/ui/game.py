"""Live game table and lobby chat; all game decisions remain server-side."""

import asyncio
from typing import Any

from rich import box
from rich.columns import Columns
from rich.console import Group
from rich.cells import cell_len
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, HorizontalScroll, Vertical, VerticalScroll
from textual.events import Click
from textual.message import Message
from textual.widgets import Button, Footer, Header, Input, Label, Static

from fools_client.errors import ClientError, display_text
from fools_client.realtime import SocketFeed, Subscription
from fools_client.ui.common import LiveScreen, SnapshotTable


_SUIT_SYMBOLS = {
    "heart": "♥", "hearts": "♥", "h": "♥",
    "diamond": "♦", "diamonds": "♦", "d": "♦",
    "club": "♣", "clubs": "♣", "c": "♣",
    "spade": "♠", "spades": "♠", "s": "♠",
}
_RANK_SYMBOLS = {
    "ace": "A", "jack": "J", "queen": "Q", "king": "K",
    "joker": "JK", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10", "10": "10",
}


def _suit_symbol(value: Any) -> str:
    suit = str(value or "?").strip().lower()
    return _SUIT_SYMBOLS.get(suit, suit[:1].upper() or "?")


def _rank_symbol(value: Any) -> str:
    rank = str(value or "?").strip()
    return _RANK_SYMBOLS.get(rank.lower(), rank)


def _card_panel(
    card: Any, *, selected: bool = False, trump: bool = False, face_down: bool = False,
    focused: bool = False, playable: bool = False,
) -> Panel:
    if face_down:
        face = Text("╱ ▒ ▒ ╲\n│ ▒ ▒ │\n╲ ▒ ▒ ╱", style="bright_blue", justify="center")
        return Panel(face, title="Card back", box=box.ROUNDED, width=11, height=7, border_style="blue")
    if not isinstance(card, dict):
        face = Text("\n   ·\n", style="dim", justify="center")
        return Panel(face, title="Uncovered", box=box.ROUNDED, width=11, height=7, border_style="dim")

    rank = _rank_symbol(card.get("rank"))
    symbol = _suit_symbol(card.get("suit"))
    red = symbol in {"♥", "♦"}
    face_width = 13 if selected else 9
    face_style = "bold red" if red else "bold white"
    face = Text()

    def line(left: str = "", right: str = "", *, center: str = "") -> None:
        if center:
            left_padding = max(0, (face_width - cell_len(center)) // 2)
            right_padding = max(0, face_width - left_padding - cell_len(center))
            face.append(" " * left_padding)
            face.append(center, style=face_style)
            face.append(" " * right_padding)
        else:
            right_padding = max(0, face_width - cell_len(left) - cell_len(right))
            face.append(left, style=face_style)
            face.append(" " * right_padding)
            face.append(right, style=face_style)

    line(left=rank)
    face.append("\n")
    line(left=symbol)
    face.append("\n")
    line(center=symbol)
    face.append("\n")
    line(right=symbol)
    face.append("\n")
    line(right=rank)
    title = Text("SELECTED", style="bold yellow") if selected else "TRUMP" if trump else None
    border = "cyan" if focused else "bright_green" if playable else "bright_yellow" if selected or trump else "red" if red else "white"
    return Panel(
        face, title=title, box=box.ROUNDED, padding=(0, 0),
        width=15 if selected else 11, height=9 if selected else 7,
        border_style=border,
    )


class GameCard(Static):
    """Focusable, clickable card widget; selection is interpreted by GameScreen."""

    can_focus = True
    BINDINGS = [
        Binding("enter", "activate", "Select card"),
        Binding("space", "activate", "Select card"),
    ]

    class Activated(Message):
        def __init__(self, area: str, identity: str):
            super().__init__()
            self.area = area
            self.identity = identity

    def __init__(
        self, area: str, identity: str, card: Any, *, selected: bool = False,
        trump: bool = False, playable: bool = False,
    ):
        super().__init__(classes="game-card", markup=False)
        self.area = area
        self.identity = identity
        self.card = card
        self.selected = selected
        self.trump = trump
        self.playable = playable
        self.set_class(selected, "selected")
        self.refresh_card()

    def set_card(self, card: Any, *, selected: bool, trump: bool, playable: bool = False) -> None:
        self.card = card
        self.selected = selected
        self.trump = trump
        self.playable = playable
        self.set_class(selected, "selected")
        self.refresh_card()

    def refresh_card(self) -> None:
        self.update(_card_panel(
            self.card, selected=self.selected, trump=self.trump,
            focused=self.has_focus, playable=self.playable,
        ))

    def on_focus(self) -> None:
        self.call_after_refresh(self.refresh_card)

    def on_blur(self) -> None:
        self.call_after_refresh(self.refresh_card)

    def on_click(self, event: Click) -> None:
        self.focus()
        self.action_activate()
        event.stop()

    def action_activate(self) -> None:
        self.post_message(self.Activated(self.area, self.identity))


class TrickRow(Horizontal):
    def __init__(self, identity: str, attack_card: GameCard, defense_card: Static):
        super().__init__(attack_card, defense_card, classes="trick-row")
        self.identity = identity
        self.attack_card = attack_card
        self.defense_card = defense_card


def _player_panel(player: dict[str, Any], *, role: str = "", duty: str = "Waiting") -> Panel:
    name = display_text(player.get("username", "Player"))
    try:
        count = max(0, int(player.get("cards_remaining") or 0))
    except (TypeError, ValueError):
        count = 0
    details = Text()
    details.append(name, style="bold")
    if role:
        details.append(f"  {role}", style="bold yellow")
    details.append(f"\nSeat {player.get('seat_position', '?')} · {count} cards\n")
    if duty == "Playing":
        details.append("▶ Playing", style="bold yellow")
        border = "bright_yellow"
    elif duty == "Finished throwing":
        details.append("✓ Finished throwing", style="bold green")
        border = "green"
    elif duty == "Taking cards":
        details.append("▣ Taking cards", style="bold blue")
        border = "blue"
    elif duty == "Out":
        details.append("○ Out", style="dim")
        border = "dim"
    else:
        details.append("… Waiting", style="dim")
        border = "cyan"
    return Panel(details, box=box.ROUNDED, padding=(0, 1), border_style=border)


def _opponent_backs(count: int, selected: set[int] | None = None) -> Text:
    """Compact overlapping card backs; lifted indexes are shown above the fan."""
    selected = selected or set()
    count = max(0, count)
    visible = count if selected else min(count, 7)
    backs = Text()
    if selected:
        for index in range(visible):
            backs.append("▴ " if index in selected else "  ", style="bold yellow" if index in selected else "default")
        backs.append("\n")
    for index in range(visible):
        style = "bold yellow" if index in selected else "bright_blue"
        backs.append("╭─", style=style)
    if visible:
        backs.append("╮", style="bold yellow" if visible - 1 in selected else "bright_blue")
        backs.append("\n")
        for index in range(visible):
            backs.append("│▧", style="bold yellow" if index in selected else "bright_blue")
        backs.append("│", style="bold yellow" if visible - 1 in selected else "bright_blue")
        backs.append("\n")
        for index in range(visible):
            backs.append("╰─", style="bold yellow" if index in selected else "bright_blue")
        backs.append("╯", style="bold yellow" if visible - 1 in selected else "bright_blue")
    if count > visible:
        backs.append(f" +{count - visible}", style="dim")
    if not count:
        backs.append("No cards", style="dim")
    return backs


class GameScreen(LiveScreen):
    BINDINGS = [*LiveScreen.BINDINGS]

    def __init__(self, game_id: str, lobby_id: str | None = None, notice: str = ""):
        super().__init__(Subscription("game", game_id))
        self.game_id = self.subscription.resource_id
        self.lobby_id = Subscription("lobby", lobby_id).resource_id if lobby_id else None
        self.initial_notice = notice
        self.game: dict[str, Any] = {}
        self.players: list[dict[str, Any]] = []
        self.hand: list[dict[str, Any]] = []
        self.table: list[dict[str, Any]] = []
        self.turn: dict[str, Any] = {}
        self.deck: dict[str, Any] = {}
        self.events: list[dict[str, Any]] = []
        self.messages: list[dict[str, Any]] = []
        self.hand_signals: dict[str, dict[str, Any]] = {}
        self.playable_hand_ids: set[str] = set()
        self.selected_cards: set[str] = set()
        self._hand_signal_key: tuple[int, ...] | None = None
        self._hand_signal_revision = 0
        self.selected_table_id: str | None = None
        self._card_render_lock = asyncio.Lock()
        self._card_render_revision = 0
        self.confirm_leave = False
        self._chat_started = False

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(classes="detail"):
            yield Label("Game table", classes="heading")
            yield Static(self.game_id, markup=False, classes="muted")
            yield Static("", id="connection", markup=False, classes="connection")
            yield Static("Loading game snapshot…", id="game-info", markup=False)
            yield Static("", id="turn-info", markup=False, classes="warning")
            yield Static("", id="player-art")
            with Horizontal(id="pile-art", classes="pile-art"):
                pass
            yield Label("Table · Select an attack card to choose a defense target", classes="heading")
            with HorizontalScroll(id="board-cards", classes="card-board"):
                pass
            yield Label("Your hand · Select cards to attack or defend", classes="heading")
            with HorizontalScroll(id="hand-cards", classes="hand-cards"):
                pass
            yield Static("", id="selection", markup=False, classes="muted")
            with Grid(classes="actions game-actions"):
                yield Button("Attack / throw in", id="attack", variant="primary")
                yield Button("Defend", id="defend")
                yield Button("Transfer", id="transfer")
                yield Button("Beat", id="bito", variant="success")
                yield Button("Take cards", id="take")
                yield Button("Done throwing", id="pass")
                yield Button("Leave game", id="leave", variant="error")
                yield Button("Rematch", id="rematch", variant="success")
                yield Button("Open graphical UI", id="web")
            with VerticalScroll(id="chat-panel"):
                yield Label("Lobby chat", classes="heading")
                yield Static("Waiting for chat snapshot…", id="chat-connection", markup=False, classes="connection")
                yield SnapshotTable(id="chat")
                with Horizontal(classes="chat-entry"):
                    yield Input(placeholder="Message…", max_length=4000, id="chat-input")
                    yield Button("Send", id="send-chat", variant="primary")
            yield Static(self.initial_notice, id="notice", markup=False)
            yield Static("The server enforces rules and owns game state. A fresh snapshot is loaded after each command.", markup=False, classes="muted")
            with Vertical(id="leave-confirmation"):
                yield Static("Leave the game? The server will end the game and record your loss.", markup=False, classes="warning")
                with Grid(classes="actions"):
                    yield Button("Confirm leave", id="confirm-leave", variant="error")
                    yield Button("Stay", id="cancel-leave")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#chat", SnapshotTable).setup("Time", "Player", "Message")
        self.query_one("#leave-confirmation").display = False
        super().on_mount()

    def _start_chat(self) -> None:
        if self.lobby_id and not self._chat_started:
            self._chat_started = True
            self.run_worker(self._watch_chat(), group="chat-live", exclusive=True)

    async def _watch_chat(self) -> None:
        profile = self.client_app.profile
        if profile is None or not self.lobby_id:
            return
        live = self.client_app.live_factory(self.client_app.client, profile.user_id)
        try:
            from contextlib import aclosing

            async with aclosing(live.watch(Subscription("lobby", self.lobby_id))) as updates:
                async for update in updates:
                    panels = update.panels
                    if "connection" in panels:
                        connection = panels["connection"]
                        state = connection.get("state", "")
                        self.query_one("#chat-connection", Static).update(
                            "Chat connected." if state == "live" else
                            "Synchronizing chat…" if state == "synchronizing" else
                            "Chat connection lost; reconnecting…"
                        )
                    if "chat" in panels:
                        self.messages = panels["chat"]
                        self._render_chat()
        except ClientError as error:
            if self.is_mounted:
                self.query_one("#chat-connection", Static).update(display_text(error))

    def apply_panels(self, panels: dict[str, Any]) -> None:
        for key, attribute in (
            ("game", "game"), ("players", "players"), ("hand", "hand"),
            ("table", "table"), ("turn", "turn"), ("deck", "deck"), ("events", "events"),
        ):
            if key in panels:
                setattr(self, attribute, panels[key])
        if "hand_signals" in panels:
            for signal in panels["hand_signals"]:
                if isinstance(signal, dict) and isinstance(signal.get("user_id"), str):
                    self.hand_signals[signal["user_id"]] = signal
        if "game" in panels:
            self.lobby_id = self.game.get("lobby_id") or self.lobby_id
            self._start_chat()
            next_game = self.game.get("next_game_id")
            if next_game and next_game != self.game_id:
                self.go(GameScreen(next_game, self.lobby_id, "The rematch has started; the new table is open."))
                return
        if "hand_signals" in panels:
            me = self.client_app.profile.user_id if self.client_app.profile else ""
            self._render_players(me)
        if any(key in panels for key in ("game", "players", "hand", "table", "turn", "deck", "events")):
            self._render_game()
            self._schedule_hand_signal()

    def _render_game(self) -> None:
        status = {"in_progress": "In progress", "finished": "Game finished"}.get(
            self.game.get("status"), display_text(self.game.get("status", "")),
        )
        self.query_one("#game-info", Static).update(
            f"{status}. State version {self.turn.get('version', '?')} · "
            f"Phase: {display_text(self.turn.get('phase', '—'))}."
        )
        me = self.client_app.profile.user_id if self.client_app.profile else ""
        role = []
        if self.turn.get("attacker_id") == me:
            role.append("You are attacking")
        if self.turn.get("defender_id") == me:
            role.append("You are defending")
        current_hand_ids = {str(card.get("hand_id")) for card in self.hand}
        self.selected_cards.intersection_update(current_hand_ids)
        current_table_ids = {str(row.get("id")) for row in self.table}
        if self.selected_table_id not in current_table_ids:
            self.selected_table_id = None
        self._render_visual_table(me)
        self._update_selection_label()
        can_play = self.game.get("status") == "in_progress"
        phase = self.turn.get("phase")
        me_id = str(me)
        attacker = str(self.turn.get("attacker_id", "")) == me_id
        defender = str(self.turn.get("defender_id", "")) == me_id
        passed = {str(identity) for identity in (self.turn.get("passed_ids") or [])}
        pending = {str(identity) for identity in (self.turn.get("pending_ids") or [])}
        me_player = next(
            (player for player in self.players if str(player.get("user_id", "")) == me_id),
            {},
        )
        try:
            hand_count = max(0, int(me_player.get("cards_remaining") or 0))
        except (TypeError, ValueError):
            hand_count = 0
        has_table = bool(self.table)
        has_open_cards = any(not row.get("defense") for row in self.table)
        all_defended = has_table and not has_open_cards
        wave = phase in {"build", "defend"}
        self.playable_hand_ids = self._playable_cards(
            me_id, phase, attacker, defender, passed, has_open_cards,
        )
        self.query_one("#turn-info", Static).update(self._turn_prompt(
            me_id, phase, attacker, defender, pending, passed, has_open_cards, all_defended,
        ))
        visibility = {
            "attack": can_play and hand_count > 0 and (
                (phase == "between" and attacker)
                or (wave and not defender and str(me_id) not in passed)
                or (phase == "taking" and not defender and str(me_id) not in passed)
            ),
            "defend": can_play and hand_count > 0 and wave and defender and has_open_cards,
            "transfer": can_play and wave and defender and has_open_cards
            and self.game.get("transferable") is not False,
            "bito": can_play and wave and attacker and all_defended,
            "take": can_play and wave and defender and has_open_cards,
            "pass": can_play and phase == "taking" and me_id in pending,
        }
        for identity, visible in visibility.items():
            button = self.query_one(f"#{identity}", Button)
            button.display = visible
            button.disabled = self.busy or self.stale or not can_play
        self.query_one("#attack", Button).label = "Attack" if phase == "between" else "Throw in"
        self._sync_card_action_buttons()
        self.query_one("#leave", Button).disabled = self.busy or self.stale or not can_play
        self.query_one("#rematch", Button).display = bool(self.game.get("can_rematch"))
        self.query_one("#rematch", Button).disabled = self.busy or self.stale
        self.query_one("#leave-confirmation").display = self.confirm_leave

    def _render_visual_table(self, me: str) -> None:
        """Paint the visible board from the latest server snapshot only."""
        self._render_players(me)
        self.run_worker(self._render_piles(), group="pile-render", exclusive=True)
        self._schedule_card_widgets()

    def _playable_cards(
        self, me: str, phase: Any, attacker: bool, defender: bool,
        passed: set[str], has_open_cards: bool,
    ) -> set[str]:
        """Display-only guidance that mirrors the web client's legal-card marks."""
        if self.game.get("status") != "in_progress":
            return set()
        open_rows = [row for row in self.table if not row.get("defense")]
        if defender and phase in {"build", "defend"} and open_rows:
            trump = (self.deck.get("trump_card") or self.game.get("trump_card") or {}).get("suit")
            def beats(card: dict[str, Any], attack: dict[str, Any]) -> bool:
                if not trump or not attack:
                    return False
                value = card.get("value")
                attack_value = attack.get("value")
                if value is None or attack_value is None:
                    return False
                if card.get("suit") == trump and attack.get("suit") != trump:
                    return True
                if attack.get("suit") == trump and card.get("suit") != trump:
                    return False
                return card.get("suit") == attack.get("suit") and value > attack_value
            legal = {
                str(card.get("hand_id")) for card in self.hand
                if any(beats(card, row.get("attack") or {}) for row in open_rows)
            }
            if self.game.get("transferable") and len(self.table) < (6 if self.deck.get("discard_count", 0) else 5):
                ranks = {row.get("attack", {}).get("value") for row in self.table if row.get("attack")}
                if len(ranks) == 1 and len(open_rows) == len(self.table):
                    legal.update(
                        str(card.get("hand_id")) for card in self.hand
                        if card.get("value") in ranks
                    )
            return legal

        can_attack = (
            (phase == "between" and attacker)
            or (phase in {"build", "defend", "taking"} and not defender and me not in passed)
        )
        if not can_attack:
            return set()
        if phase in {"build", "defend", "taking"} and self.game.get("neighbor_throw_only"):
            ordered = sorted(self.players, key=lambda player: player.get("seat_position", 0))
            defender_index = next((i for i, player in enumerate(ordered) if str(player.get("user_id")) == str(self.turn.get("defender_id"))), None)
            my_index = next((i for i, player in enumerate(ordered) if str(player.get("user_id")) == me), None)
            if defender_index is not None and my_index is not None and len(ordered) > 2:
                if (defender_index - my_index) % len(ordered) not in {1, len(ordered) - 1}:
                    return set()
        cap = 6 if self.deck.get("discard_count", 0) else 5
        slots = cap - len(self.table)
        if phase in {"build", "defend", "taking"}:
            slots = min(slots, max(0, int(next((p.get("cards_remaining", 0) for p in self.players if str(p.get("user_id")) == str(self.turn.get("defender_id"))), 0) or 0) - len(open_rows)))
        if slots < 1:
            return set()
        if phase == "between":
            return {str(card.get("hand_id")) for card in self.hand}
        ranks = {
            card.get("value") for row in self.table for card in (row.get("attack"), row.get("defense"))
            if isinstance(card, dict)
        }
        return {str(card.get("hand_id")) for card in self.hand if card.get("value") in ranks}

    def _turn_prompt(
        self, me: str, phase: Any, attacker: bool, defender: bool,
        pending: set[str], passed: set[str], has_open: bool, all_defended: bool,
    ) -> str:
        if self.game.get("status") != "in_progress":
            return "Game finished. You can request a rematch if the option is available."
        if phase == "between":
            if attacker:
                return "YOUR MOVE: Attack. Select a card in your hand, then press Attack."
            actor = next((p.get("username", "player") for p in self.players if str(p.get("user_id")) == str(self.turn.get("attacker_id"))), "the attacker")
            return f"WAITING: {display_text(actor)} is choosing an attack."
        if phase == "taking":
            if defender:
                return "YOUR MOVE: You are taking the cards. Other players may throw in; wait for them to finish."
            if me in pending:
                return "YOUR MOVE: Throw in matching ranks, or press Done throwing."
            return "WAITING: The defender is taking the cards."
        if defender and has_open:
            return "YOUR MOVE: Defend. Select a green highlighted card and its attack card, then press Defend. Or press Take cards."
        if defender and all_defended:
            return "WAITING: All attacks are defended. The attacker must finish the round."
        if attacker and all_defended:
            return "YOUR MOVE: Throw in matching ranks, or press Beaten to end the round."
        if not defender and self.playable_hand_ids:
            return "YOUR MOVE: Throw in a card matching a rank on the table."
        actor_id = self.turn.get("defender_id") if has_open else self.turn.get("attacker_id")
        actor = next((p.get("username", "player") for p in self.players if str(p.get("user_id")) == str(actor_id)), "another player")
        return f"WAITING: {display_text(actor)} is playing."

    async def _render_piles(self) -> None:
        trump_card = self.deck.get("trump_card") or self.game.get("trump_card")
        pile_art = self.query_one("#pile-art", Horizontal)
        await pile_art.remove_children()
        deck = Static(
            Panel(Text(f"{self.deck.get('deck_remaining', '?')} cards", justify="center"), title="Deck", box=box.ROUNDED, width=18, height=7),
            classes="pile-item", markup=False,
        )
        trump = GameCard("trump", "trump", trump_card) if trump_card else Static(
            Panel(Text("No trump", justify="center"), title="Trump", box=box.ROUNDED, width=11, height=7),
            classes="pile-item", markup=False,
        )
        if isinstance(trump, GameCard):
            trump.can_focus = False
        discard = Static(
            Panel(Text(f"{self.deck.get('discard_count', '?')} cards", justify="center"), title="Discard", box=box.ROUNDED, width=18, height=7),
            classes="pile-item", markup=False,
        )
        await pile_art.mount(deck, trump, discard)

    def _render_players(self, me: str) -> None:
        pending_ids = {str(identity) for identity in (self.turn.get("pending_ids") or [])}
        passed_ids = {str(identity) for identity in (self.turn.get("passed_ids") or [])}
        attacker_id = str(self.turn.get("attacker_id", ""))
        defender_id = str(self.turn.get("defender_id", ""))
        phase = self.turn.get("phase")
        open_cards = any(not row.get("defense") for row in self.table)
        actor_id = ""
        if phase == "between":
            actor_id = attacker_id
        elif phase in {"build", "defend"}:
            actor_id = defender_id if open_cards else attacker_id if self.table else ""
        players = []
        for player in self.players:
            identity = str(player.get("user_id", ""))
            if identity == me:
                continue
            role = ""
            if identity == attacker_id:
                role = "Attacker" + (" · You" if identity == me else "")
            elif identity == defender_id:
                role = "Defender" + (" · You" if identity == me else "")
            elif identity in pending_ids:
                role = "Waiting" + (" · You" if identity == me else "")
            elif identity in passed_ids:
                role = "Passed" + (" · You" if identity == me else "")
            try:
                count = max(0, int(player.get("cards_remaining") or 0))
            except (TypeError, ValueError):
                count = 0
            if identity in passed_ids:
                duty = "Finished throwing"
            elif phase == "taking" and identity == defender_id:
                duty = "Taking cards"
            elif identity in (pending_ids if phase == "taking" else {actor_id}):
                duty = "Playing"
            elif count == 0 and not self.deck.get("deck_remaining", 0):
                duty = "Out"
            else:
                duty = "Waiting"
            signal = self.hand_signals.get(identity, {}) if self.game.get("share_hand_signals", True) is not False else {}
            indexes = signal.get("selected", [])
            selected = {
                index for index in indexes
                if isinstance(index, int) and not isinstance(index, bool) and 0 <= index < count
            } if isinstance(indexes, list) else set()
            panel = _player_panel(player, role=role, duty=duty)
            # Keep each opponent's fan below their name/role, while Columns
            # places complete opponent blocks side by side when there is room.
            players.append(Group(panel, _opponent_backs(count, selected)))
        self.query_one("#player-art", Static).update(
            Columns(players, equal=True, expand=True, padding=(0, 1))
            if players else Text("Waiting for players…", style="dim")
        )

    def _schedule_card_widgets(self) -> None:
        if not self.is_mounted:
            return
        self._card_render_revision += 1
        self.run_worker(
            self._sync_card_widgets(self._card_render_revision),
            group="card-render", exclusive=False,
        )

    async def _sync_card_widgets(self, revision: int) -> None:
        async with self._card_render_lock:
            if revision != self._card_render_revision or not self.is_mounted:
                return
            focused_card = next(
                ((widget.area, widget.identity) for widget in self.query(GameCard) if widget.has_focus),
                None,
            )
            hand_area = self.query_one("#hand-cards", HorizontalScroll)
            board_area = self.query_one("#board-cards", HorizontalScroll)
            await hand_area.remove_children()
            await board_area.remove_children()

            trump_card = self.deck.get("trump_card") or self.game.get("trump_card")
            trump_suit = _suit_symbol(trump_card.get("suit")) if isinstance(trump_card, dict) else ""
            hand_widgets = [
                GameCard(
                    "hand", str(card.get("hand_id", "")), card,
                    selected=str(card.get("hand_id", "")) in self.selected_cards,
                    trump=bool(trump_suit) and _suit_symbol(card.get("suit")) == trump_suit,
                    playable=str(card.get("hand_id", "")) in self.playable_hand_ids,
                ) for card in self.hand
            ]
            if hand_widgets:
                await hand_area.mount(*hand_widgets)
            else:
                await hand_area.mount(Static("Your hand is empty", classes="empty-board"))

            trick_rows = []
            for row in self.table:
                identity = str(row.get("id", ""))
                attack = GameCard(
                    "table", identity, row.get("attack"),
                    selected=identity == self.selected_table_id,
                )
                defense_card = row.get("defense")
                defense = Static(
                    _card_panel(defense_card) if defense_card else "",
                    classes="table-card",
                )
                trick_rows.append(TrickRow(identity, attack, defense))
            if trick_rows:
                await board_area.mount(*trick_rows)
            else:
                await board_area.mount(Static("No cards on the table", classes="empty-board"))

            if focused_card:
                match = next(
                    (widget for widget in self.query(GameCard)
                     if (widget.area, widget.identity) == focused_card),
                    None,
                )
                if match:
                    match.focus()
            self._update_card_visuals()

    def _update_card_visuals(self) -> None:
        if not self.is_mounted:
            return
        trump_card = self.deck.get("trump_card") or self.game.get("trump_card")
        trump_suit = _suit_symbol(trump_card.get("suit")) if isinstance(trump_card, dict) else ""
        hand_by_id = {str(card.get("hand_id", "")): card for card in self.hand}
        for widget in self.query(GameCard):
            if widget.area == "hand":
                card = hand_by_id.get(widget.identity, widget.card)
                widget.set_card(
                    card, selected=widget.identity in self.selected_cards,
                    trump=bool(trump_suit) and _suit_symbol(card.get("suit")) == trump_suit,
                    playable=widget.identity in self.playable_hand_ids,
                )
            elif widget.area == "table":
                widget.set_card(widget.card, selected=widget.identity == self.selected_table_id, trump=False)
        self._sync_card_action_buttons()
        self._update_selection_label()

    def _sync_card_action_buttons(self) -> None:
        """Require an explicit card selection before posting list actions."""
        if not self.is_mounted:
            return
        has_selected_cards = bool(self._selected_card_ids())
        for identity in ("attack", "transfer"):
            button = self.query_one(f"#{identity}", Button)
            button.disabled = self.busy or self.stale or not has_selected_cards

    def _update_selection_label(self) -> None:
        if not self.is_mounted:
            return
        target = next(
            (f"Trick {index}" for index, row in enumerate(self.table, 1)
             if str(row.get("id", "")) == self.selected_table_id),
            "none",
        )
        self.query_one("#selection", Static).update(
            f"Selected hand cards: {len(self.selected_cards)} · Defense target: {target}. "
            "Green cards are playable. Use Enter or Space to select a card."
        )

    def _render_chat(self) -> None:
        self.query_one("#chat", SnapshotTable).set_rows({
            str(message["id"]): (
                display_text(str(message.get("sent_at", ""))[:19]),
                display_text(message.get("username", message.get("sender", ""))),
                display_text(message.get("content", "")),
            ) for message in self.messages if isinstance(message, dict) and isinstance(message.get("id"), str)
        })

    def sync_controls(self) -> None:
        super().sync_controls()
        for identity in ("attack", "defend", "transfer", "bito", "take", "pass", "leave"):
            self.query_one(f"#{identity}", Button).disabled = self.busy or self.stale
        self.query_one("#send-chat", Button).disabled = self.busy or self.stale
        self.query_one("#rematch", Button).disabled = self.busy or self.stale
        self.query_one("#confirm-leave", Button).disabled = self.busy or self.stale
        if self.is_mounted and self.game:
            self._render_game()

    @on(GameCard.Activated)
    def card_activated(self, event: GameCard.Activated) -> None:
        if self.busy or self.stale:
            return
        if event.area == "hand":
            if event.identity in self.selected_cards:
                self.selected_cards.remove(event.identity)
            else:
                self.selected_cards.add(event.identity)
        elif event.area == "table":
            self.selected_table_id = None if self.selected_table_id == event.identity else event.identity
        self._update_card_visuals()
        if event.area == "hand":
            self._schedule_hand_signal()

    def _schedule_hand_signal(self) -> None:
        if not self.is_mounted:
            return
        if self.game.get("share_hand_signals", True) is False:
            self._hand_signal_key = None
            return
        selected = tuple(
            index for index, card in enumerate(self.hand)
            if str(card.get("hand_id", "")) in self.selected_cards
        )
        if selected == self._hand_signal_key:
            return
        self._hand_signal_key = selected
        self._hand_signal_revision += 1
        self.run_worker(
            self._publish_hand_signal(self._hand_signal_revision, selected),
            group="hand-signal", exclusive=True,
        )

    async def _publish_hand_signal(self, revision: int, selected: tuple[int, ...]) -> None:
        # Coalesce quick successive clicks and stay below the server's signal rate limit.
        await asyncio.sleep(0.06)
        if revision != self._hand_signal_revision or not self.is_mounted:
            return
        if self.game.get("share_hand_signals", True) is False:
            return
        try:
            await SocketFeed(self.client_app.client).send(
                Subscription("game", self.game_id),
                {"action": "hand_signal", "sort": "dealt", "selected": list(selected)},
            )
        except ClientError as error:
            if self.is_mounted:
                self.notice(f"Could not share hand selection: {error}", error=True)

    def _selected_card_ids(self) -> list[str]:
        return [
            str(card["id"]) for card in self.hand
            if str(card.get("hand_id")) in self.selected_cards and isinstance(card.get("id"), str)
        ]

    async def _game_command(self, action: str) -> None:
        card_ids = self._selected_card_ids()
        if action in {"attack", "transfer"} and not card_ids:
            self.notice("Select one or more cards first.", error=True)
            return
        selected_table = self.selected_table_id
        selected_card = next((card for card in self.hand if str(card.get("hand_id")) in self.selected_cards), None)
        game = self.client_app.games
        match action:
            case "attack": await game.attack(self.game_id, card_ids)
            case "transfer": await game.transfer(self.game_id, card_ids)
            case "defend":
                if selected_table is None or selected_card is None or len(card_ids) != 1:
                    self.notice("Select one card from your hand and an attack row on the table.", error=True)
                    return
                await game.defend(self.game_id, selected_table, str(selected_card["id"]))
            case "take": await game.take(self.game_id)
            case "pass": await game.pass_turn(self.game_id)
            case "bito": await game.bito(self.game_id)
        self.selected_cards.clear()
        self.selected_table_id = None
        self._update_card_visuals()
        self._schedule_hand_signal()
        self.notice("Command accepted by the server. Loading the latest snapshot.")
        self.start_stream()

    @on(Button.Pressed)
    def press(self, event: Button.Pressed) -> None:
        identity = event.button.id
        if identity in {"attack", "defend", "transfer", "bito", "take", "pass"}:
            if not self.busy and not self.stale:
                self.start_action(lambda: self._game_command(identity))
        elif identity == "leave":
            if self.busy or self.stale or self.game.get("status") != "in_progress":
                return
            self.confirm_leave = True
            self._render_game()
        elif identity == "confirm-leave":
            if not self.busy and not self.stale:
                self.confirm_leave = False
                self.start_action(self.leave_game)
        elif identity == "cancel-leave":
            self.confirm_leave = False
            self._render_game()
        elif identity == "rematch":
            if not self.busy and not self.stale and self.game.get("can_rematch"):
                self.start_action(self.rematch)
        elif identity == "send-chat":
            if not self.busy and not self.stale:
                self.start_action(self.send_chat)
        elif identity == "web":
            opened = self.client_app.open_graphical()
            self.notice(
                "Opened the graphical app in your browser. Sign in there if needed; game state stays on the server."
                if opened else "Could not open a browser. Open the server URL manually.",
                error=not opened,
            )

    @on(Input.Submitted, "#chat-input")
    def submit_chat(self) -> None:
        self.start_action(self.send_chat)

    async def send_chat(self) -> None:
        content = self.query_one("#chat-input", Input).value.strip()
        if not content or not self.lobby_id or self.stale:
            return
        await self.client_app.chat.send(self.lobby_id, content)
        self.query_one("#chat-input", Input).value = ""
        self.notice("Message sent.")

    async def leave_game(self) -> None:
        await self.client_app.games.leave(self.game_id)
        self.notice("You left the game. Loading the final snapshot.")
        self.start_stream()

    async def rematch(self) -> None:
        next_game = await self.client_app.games.rematch(self.game_id)
        self.go(GameScreen(next_game, self.lobby_id, "The rematch has started."))

