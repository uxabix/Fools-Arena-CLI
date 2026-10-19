"""Live game table and lobby chat; all game decisions remain server-side."""

from typing import Any

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Footer, Header, Input, Label, Static

from fools_client.errors import ClientError, display_text
from fools_client.realtime import Subscription
from fools_client.ui.common import LiveScreen, SnapshotTable


def _card(card: Any) -> str:
    if not isinstance(card, dict):
        return "—"
    return f"{display_text(card.get('rank', '?'))} {display_text(card.get('suit', '?'))}"


class GameScreen(LiveScreen):
    BINDINGS = [*LiveScreen.BINDINGS, Binding("space", "toggle_card", "Select card")]

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
        self.selected_cards: set[str] = set()
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
            yield SnapshotTable(id="players")
            yield Static("", id="deck-info", markup=False)
            yield Label("Cards on table")
            yield SnapshotTable(id="table")
            yield Label("Your hand · Press Space to select cards; select multiple cards to attack")
            yield SnapshotTable(id="hand")
            yield Static("", id="selection", markup=False, classes="muted")
            with Grid(classes="actions game-actions"):
                yield Button("Attack / throw in", id="attack", variant="primary")
                yield Button("Defend", id="defend")
                yield Button("Transfer", id="transfer")
                yield Button("Beat", id="bito", variant="success")
                yield Button("Take cards", id="take")
                yield Button("Pass", id="pass")
                yield Button("Seal attack", id="seal")
                yield Button("Leave game", id="leave", variant="error")
                yield Button("Rematch", id="rematch", variant="success")
                yield Button("Lobby", id="lobby")
                yield Button("Open graphical UI", id="web")
                yield Button("Sign out", id="logout")
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
        self.query_one("#players", SnapshotTable).setup("Player", "Seat", "Cards")
        self.query_one("#table", SnapshotTable).setup("Attack", "Defense")
        self.query_one("#hand", SnapshotTable).setup("Selected", "Card", "Suit", "ID")
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
        if "game" in panels:
            self.lobby_id = self.game.get("lobby_id") or self.lobby_id
            self._start_chat()
            next_game = self.game.get("next_game_id")
            if next_game and next_game != self.game_id:
                self.go(GameScreen(next_game, self.lobby_id, "The rematch has started; the new table is open."))
                return
        if any(key in panels for key in ("game", "players", "hand", "table", "turn", "deck", "events")):
            self._render_game()

    def _render_game(self) -> None:
        status = {"in_progress": "In progress", "finished": "Game finished"}.get(
            self.game.get("status"), display_text(self.game.get("status", "")),
        )
        self.query_one("#game-info", Static).update(
            f"{status}. State version {self.turn.get('version', '?')} · "
            f"Phase: {display_text(self.turn.get('phase', '—'))}."
        )
        me = self.client_app.profile.user_id if self.client_app.profile else ""
        names = {player.get("user_id"): display_text(player.get("username", "")) for player in self.players}
        role = []
        if self.turn.get("attacker_id") == me:
            role.append("You are attacking")
        if self.turn.get("defender_id") == me:
            role.append("You are defending")
        self.query_one("#turn-info", Static).update(
            " · ".join(role) + (". Waiting for other players to confirm." if self.turn.get("pending_ids") else "")
        )
        self.query_one("#players", SnapshotTable).set_rows({
            str(player.get("user_id")): (
                names.get(player.get("user_id"), "?"),
                str(player.get("seat_position", "?")),
                str(player.get("cards_remaining", "?")),
            ) for player in self.players
        })
        self.query_one("#table", SnapshotTable).set_rows({
            str(row.get("id")): (_card(row.get("attack")), _card(row.get("defense")))
            for row in self.table
        })
        current_hand_ids = {str(card.get("hand_id")) for card in self.hand}
        self.selected_cards.intersection_update(current_hand_ids)
        self.query_one("#hand", SnapshotTable).set_rows({
            str(card.get("hand_id")): (
                "[x]" if str(card.get("hand_id")) in self.selected_cards else "[ ]",
                display_text(card.get("rank", "?")), display_text(card.get("suit", "?")),
                str(card.get("hand_id", ""))[:8],
            ) for card in self.hand
        })
        self.query_one("#selection", Static).update(
            f"Selected cards: {len(self.selected_cards)}. To defend, select one hand card and a table row."
        )
        trump = _card(self.game.get("trump_card"))
        self.query_one("#deck-info", Static).update(
            f"Trump: {trump}. Deck: {self.deck.get('deck_remaining', '?')}; "
            f"discarded: {self.deck.get('discard_count', '?')}; top discard: {_card(self.deck.get('discard_top'))}."
        )
        can_play = self.game.get("status") == "in_progress"
        for identity in ("attack", "defend", "transfer", "bito", "take", "pass", "seal"):
            self.query_one(f"#{identity}", Button).disabled = self.busy or self.stale or not can_play
        self.query_one("#leave", Button).disabled = self.busy or self.stale or not can_play
        self.query_one("#rematch", Button).display = bool(self.game.get("can_rematch"))
        self.query_one("#rematch", Button).disabled = self.busy or self.stale
        self.query_one("#leave-confirmation").display = self.confirm_leave

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
        for identity in ("attack", "defend", "transfer", "bito", "take", "pass", "seal", "leave"):
            self.query_one(f"#{identity}", Button).disabled = self.busy or self.stale
        self.query_one("#send-chat", Button).disabled = self.busy or self.stale
        self.query_one("#rematch", Button).disabled = self.busy or self.stale
        self.query_one("#confirm-leave", Button).disabled = self.busy or self.stale
        if self.is_mounted and self.game:
            self._render_game()

    def action_toggle_card(self) -> None:
        identity = self.query_one("#hand", SnapshotTable).selected_id
        if identity is None or self.busy or self.stale:
            return
        if identity in self.selected_cards:
            self.selected_cards.remove(identity)
        else:
            self.selected_cards.add(identity)
        self._render_game()

    def _selected_card_ids(self) -> list[str]:
        return [
            str(card["id"]) for card in self.hand
            if str(card.get("hand_id")) in self.selected_cards and isinstance(card.get("id"), str)
        ]

    async def _game_command(self, action: str) -> None:
        card_ids = self._selected_card_ids()
        selected_table = self.query_one("#table", SnapshotTable).selected_id
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
            case "seal": await game.seal(self.game_id)
            case "take": await game.take(self.game_id)
            case "pass": await game.pass_turn(self.game_id)
            case "bito": await game.bito(self.game_id)
        self.selected_cards.clear()
        self.notice("Command accepted by the server. Loading the latest snapshot.")
        self.start_stream()

    @on(Button.Pressed)
    def press(self, event: Button.Pressed) -> None:
        identity = event.button.id
        if identity in {"attack", "defend", "transfer", "bito", "take", "pass", "seal"}:
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
        elif identity == "lobby":
            if self.lobby_id:
                from fools_client.ui.lobbies import LobbyScreen
                self.go(LobbyScreen(self.lobby_id))
            else:
                self.client_app.show_lobbies()
        elif identity == "logout":
            self.start_action(self.logout)
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

