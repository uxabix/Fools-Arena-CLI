"""Live game table and lobby chat; all game decisions remain server-side."""

import asyncio
import math
from typing import Any

from rich import box
from rich.cells import cell_len
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, HorizontalScroll, Vertical, VerticalGroup, VerticalScroll
from textual.events import Click
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Button, Footer, Header, Input, Label, Static

from fools_client.errors import ApiError, ClientError, display_text
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
_HAND_SORTS = (
    "suit-left", "suit-right", "rank-ltr", "rank-rtl", "dealt", "dealt-rev",
)
_HAND_SORT_LABELS = {
    "suit-left": "Suit · trump left",
    "suit-right": "Suit · trump right",
    "rank-ltr": "Rank · low to high",
    "rank-rtl": "Rank · high to low",
    "dealt": "Dealt",
    "dealt-rev": "Dealt · reversed",
}
_SUIT_ORDER = {"♠": 0, "♥": 1, "♦": 2, "♣": 3}
_RANK_VALUES = {
    "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "jack": 11, "queen": 12, "king": 13, "ace": 14,
}


def _suit_symbol(value: Any) -> str:
    suit = str(value or "?").strip().lower()
    return _SUIT_SYMBOLS.get(suit, suit[:1].upper() or "?")


def _rank_symbol(value: Any) -> str:
    rank = str(value or "?").strip()
    return _RANK_SYMBOLS.get(rank.lower(), rank)


def _reparent(widget: Widget, parent: Widget, index: int) -> None:
    """Move a live widget under a new parent without destroying it."""
    if widget.parent is not None:
        widget.parent._nodes._remove(widget)
    widget._attach(parent)
    parent._nodes._insert(index, widget)


def _tricks_per_line(available: int) -> int:
    """How many full attack/defense pairs fit in one table row.

    Each pair keeps its 24-column frame plus a one-column gap. A narrower
    terminal starts another row instead of clipping or scrolling sideways.
    """
    return max(1, available // 25)


def _card_value(card: dict[str, Any]) -> float:
    value = card.get("value")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return float(_RANK_VALUES.get(str(card.get("rank", "")).lower(), 0))


def _can_beat(defense: dict[str, Any], attack: dict[str, Any], trump_suit: str) -> bool:
    """Return a display hint for a normal card defense; the server validates it."""
    defense_suit = _suit_symbol(defense.get("suit"))
    attack_suit = _suit_symbol(attack.get("suit"))
    if defense_suit == attack_suit:
        return _card_value(defense) > _card_value(attack)
    return bool(trump_suit) and defense_suit == trump_suit and attack_suit != trump_suit


def _sorted_hand(
    hand: list[dict[str, Any]], mode: str, trump_suit: Any,
) -> list[dict[str, Any]]:
    """Sort a display copy; game commands continue to use server hand order."""
    cards = list(hand)
    if mode == "dealt":
        return cards
    if mode == "dealt-rev":
        return list(reversed(cards))
    trump = _suit_symbol(trump_suit) if trump_suit else ""

    def suit_key(card: dict[str, Any], *, trump_right: bool = False) -> int:
        suit = _suit_symbol(card.get("suit"))
        if trump and suit == trump:
            return 8 if trump_right else -1
        return _SUIT_ORDER.get(suit, 4)

    if mode in {"suit-left", "suit-right"}:
        cards.sort(key=lambda card: (
            suit_key(card, trump_right=mode == "suit-right"),
            _card_value(card), suit_key(card), str(card.get("id", "")),
        ))
    else:
        cards.sort(key=lambda card: (
            _card_value(card), suit_key(card), str(card.get("id", "")),
        ))
        if mode == "rank-rtl":
            cards.reverse()
    return cards


def _card_panel(
    card: Any, *, selected: bool = False, trump: bool = False, face_down: bool = False,
    focused: bool = False, playable: bool = False, error_highlight: bool = False,
    actor_name: str | None = None, actor_progress: float = 0.0,
) -> Panel:
    if face_down:
        face = Text("╱ ▒ ▒ ╲\n│ ▒ ▒ │\n╲ ▒ ▒ ╱", style="bright_blue", justify="center")
        return Panel(face, title="Card back", box=box.ROUNDED, width=13, height=8, border_style="blue")
    if not isinstance(card, dict):
        face = Text("\n   ·\n", style="dim", justify="center")
        return Panel(face, title="Uncovered", box=box.ROUNDED, width=13, height=8, border_style="dim")

    rank = _rank_symbol(card.get("rank"))
    symbol = _suit_symbol(card.get("suit"))
    red = symbol in {"♥", "♦"}
    # A card must keep the same footprint when it is selected.  Letting a
    # selected card grow makes Textual lay out the whole hand again, which is
    # especially visible while a card is arriving at the table.
    face_width = 11
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

    if selected:
        face.append("SELECT", style="bold yellow")
        face.append("\n")
    line(left=rank)
    face.append("\n")
    line(left=symbol)
    face.append("\n")
    line(center=symbol)
    face.append("\n")
    line(right=symbol)
    face.append("\n")
    line(right=rank)
    if error_highlight:
        title = Text("INVALID", style="bold bright_red")
    elif actor_name:
        # Rich has no terminal alpha channel; blend the name's foreground
        # between a subdued and bright color to produce a smooth fade.
        progress = max(0.0, min(1.0, actor_progress))
        start, end = (18, 18, 18), (255, 244, 150)
        color = "#" + "".join(
            f"{round(a + (b - a) * progress):02x}" for a, b in zip(start, end)
        )
        title = Text(actor_name, style=f"bold {color}")
    else:
        title = "TRUMP" if trump else None
    border = (
        "bright_red" if error_highlight else "cyan" if focused else
        "bright_green" if playable else "bright_yellow" if selected or trump else
        "red" if red else "white"
    )
    return Panel(
        face, title=title, box=box.ROUNDED, padding=(0, 0),
        width=13, height=8,
        border_style=border,
    )


def _trump_pile_panel(card: Any, *, in_deck: bool) -> Panel:
    """Show the trump card while it is in stock, then retain only its suit."""
    if not isinstance(card, dict):
        return Panel(
            Text("No trump", justify="center"), title="Trump",
            box=box.ROUNDED, width=11, height=7,
        )
    symbol = _suit_symbol(card.get("suit"))
    red = symbol in {"♥", "♦"}
    color = "bold red" if red else "bold white"
    if not in_deck:
        return Panel(
            Text(symbol, style=f"{color} on default", justify="center"),
            box=box.ROUNDED, width=7, height=5, border_style="bright_yellow",
        )
    rank = _rank_symbol(card.get("rank"))
    face = Text(f"{rank}\n\n{symbol}\n\n{rank}", style=color, justify="center")
    return Panel(
        face, title="TRUMP", box=box.ROUNDED, padding=(0, 0),
        width=11, height=7, border_style="bright_yellow",
    )


class GameCard(Static):
    """Focusable, clickable card widget; selection is interpreted by GameScreen."""

    can_focus = True
    selection_lift = reactive(0.0)
    shake_progress = reactive(0.0)
    sort_progress = reactive(0.0)
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
        self.error_highlight = False
        self._error_animation_generation = 0
        self._error_base_offset = None
        self._sort_direction = 1
        # A restored screen may already contain a selected card.  Put it in
        # its final position without playing an animation before it is shown.
        self.selection_lift = -1.0 if selected and area == "hand" else 0.0
        self.set_class(selected, "selected")
        self.refresh_card()

    def set_card(self, card: Any, *, selected: bool, trump: bool, playable: bool = False) -> None:
        changed = (
            card != self.card
            or selected != self.selected
            or trump != self.trump
            or playable != self.playable
        )
        if not changed:
            return
        selection_changed = selected != self.selected
        self.card = card
        self.selected = selected
        self.trump = trump
        self.playable = playable
        self.set_class(selected, "selected")
        self.refresh_card()
        if selection_changed and self.area == "hand":
            self._animate_selection(selected)

    def _animate_selection(self, selected: bool) -> None:
        """Lift or return a hand card without changing the hand's layout."""
        target = -1.0 if selected else 0.0
        if not self.is_mounted:
            self.selection_lift = target
            return
        self.animate(
            "selection_lift", target,
            duration=0.18 if selected else 0.14,
            easing="out_back" if selected else "in_out_cubic",
        )

    def watch_selection_lift(self, value: float) -> None:
        # Hand cards normally have no positional offset.  Moving only this
        # widget gives selection a visible motion while preserving every
        # card's footprint and preventing sibling reflow.
        self._refresh_hand_offset(value)

    def animate_sort(self, index: int) -> None:
        """Give each card a brief alternating glide when the hand is reordered."""
        self._sort_direction = 1 if index % 2 == 0 else -1
        if self.is_mounted:
            self.sort_progress = 0.0
            self.animate("sort_progress", 1.0, duration=0.34, easing="in_out_cubic")

    def watch_sort_progress(self, _value: float) -> None:
        self._refresh_hand_offset(self.selection_lift)

    def _refresh_hand_offset(self, lift: float) -> None:
        if self.area == "hand" and not self.error_highlight:
            wave = math.sin(self.sort_progress * math.pi)
            horizontal = round(wave * 2 * self._sort_direction)
            vertical = round(lift - wave)
            self.styles.offset = (horizontal, vertical)

    def refresh_card(self) -> None:
        self.update(_card_panel(
            self.card, selected=self.selected, trump=self.trump,
            focused=self.has_focus, playable=self.playable,
            error_highlight=self.error_highlight,
        ))

    def flash_error(self) -> None:
        self._error_animation_generation += 1
        generation = self._error_animation_generation
        if not self.error_highlight:
            self._error_base_offset = self.styles.offset
        self.error_highlight = True
        self.refresh_card()
        self.shake_progress = 0.0
        self.animate(
            "shake_progress", 1.0, duration=0.62, easing="linear",
            on_complete=lambda: self._finish_error_animation(generation),
        )

    def watch_shake_progress(self, progress: float) -> None:
        # Four small side-to-side shakes, with decreasing travel, then settle
        # exactly at the card's original layout offset.
        travel = math.sin(progress * math.tau * 4) * 2 * (1 - progress)
        base = self._error_base_offset or self.styles.offset
        self.styles.offset = (base.x.value + round(travel), base.y.value)

    def _finish_error_animation(self, generation: int) -> None:
        if generation != self._error_animation_generation:
            return
        if self._error_base_offset is not None:
            self.styles.offset = self._error_base_offset
        self.set_timer(0.25, lambda: self._clear_error_highlight(generation))

    def _clear_error_highlight(self, generation: int) -> None:
        if generation != self._error_animation_generation or not self.is_mounted:
            return
        self.error_highlight = False
        self._error_base_offset = None
        self.refresh_card()

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
    def __init__(self, identity: str, attack_card: GameCard, defense_card: "BoardDefenseCard"):
        super().__init__(attack_card, defense_card, classes="trick-row")
        self.identity = identity
        self.attack_card = attack_card
        self.defense_card = defense_card
        self._defense: Any = None
        # The overlay belongs to the row only after a defense is actually
        # played. A blank overlay still covers part of the attack card.
        self.defense_card.display = False

    def set_defense(self, card: Any) -> None:
        """Replace the covered card only when that table row changed."""
        if card == self._defense:
            return
        self._defense = card
        self.defense_card.display = bool(card)
        self.defense_card.set_card(card)


class BoardDefenseCard(Static):
    """A defense card with the same temporary actor label as an attack card."""

    actor_progress = reactive(0.0)

    def __init__(self):
        super().__init__("", classes="table-card", markup=False)
        self.card: Any = None
        self.actor_name: str | None = None
        self._actor_animation_generation = 0

    def set_card(self, card: Any) -> None:
        self.card = card
        self.refresh_card()

    def show_actor(self, username: str) -> None:
        if not username:
            return
        self._actor_animation_generation += 1
        generation = self._actor_animation_generation
        self.actor_name = display_text(username)
        self.actor_progress = 0.0
        self.refresh_card()
        self.animate(
            "actor_progress", 1.0, duration=0.45, easing="out_cubic",
            on_complete=lambda: self._hold_actor(generation),
        )

    def watch_actor_progress(self, _progress: float) -> None:
        self.refresh_card()

    def _hold_actor(self, generation: int) -> None:
        if generation == self._actor_animation_generation and self.is_mounted:
            self.set_timer(2.4, lambda: self._fade_actor(generation))

    def _fade_actor(self, generation: int) -> None:
        if generation != self._actor_animation_generation or not self.is_mounted:
            return
        self.animate(
            "actor_progress", 0.0, duration=0.7, easing="in_out_cubic",
            on_complete=lambda: self._clear_actor(generation),
        )

    def _clear_actor(self, generation: int) -> None:
        if generation != self._actor_animation_generation:
            return
        self.actor_name = None
        self.refresh_card()

    def refresh_card(self) -> None:
        self.update(_card_panel(
            self.card, actor_name=self.actor_name, actor_progress=self.actor_progress,
        ) if self.card else "")


class OpponentFan(Static):
    """A fixed-size canvas for one opponent's overlapping card fan."""

    shuffle_progress = reactive(0.0)

    def __init__(self, user_id: str):
        super().__init__("", classes="opponent-fan", markup=False)
        self.user_id = user_id
        self._count = 0
        self._selected: set[int] = set()
        self._fan_width = 0

    async def on_mount(self) -> None:
        await self.sync(self._count, self._selected)
        # The parent may populate the fan before this widget is mounted.  In
        # that case ``sync`` correctly sees no state transition, but the first
        # frame still has to be painted.
        self._paint_fan()

    async def sync(self, count: int, selected: set[int], *, shuffle: bool = False) -> None:
        count = max(0, count)
        selected = {index for index in selected if 0 <= index < count}
        count_changed = count != self._count
        changed = count_changed or selected != self._selected
        self._count = count
        self._selected = selected
        target_width = max(5, 2 * count + 5)
        if self._fan_width != target_width:
            self.styles.width = target_width
            self._fan_width = target_width
        if changed:
            self._paint_fan()
        if shuffle and not count_changed and count > 1:
            self.shuffle_progress = 0.001
            self.animate(
                "shuffle_progress", 1.0, duration=0.38,
                easing="in_out_cubic", on_complete=self._finish_shuffle,
            )

    def watch_shuffle_progress(self, _value: float) -> None:
        self._paint_fan()

    def _finish_shuffle(self) -> None:
        self.animate("shuffle_progress", 0.0, duration=0.38, easing="in_out_cubic")

    def _paint_fan(self) -> None:
        count = self._count
        if count <= 0:
            self.update("No cards")
            return
        width = max(5, 2 * count + 5)
        # Leave a top line so lifted cards can move up without changing the
        # fan widget's size or making the opponent row reflow.
        rows = [[" "] * width for _ in range(5)]
        selected_spans: list[tuple[int, int]] = []
        progress = max(0.0, min(1.0, self.shuffle_progress))
        # Paint regular cards first. A lifted card is painted last, which also
        # keeps it visibly above neighbouring, overlapping card backs.
        ordered_indexes = [index for index in range(count) if index not in self._selected]
        ordered_indexes.extend(index for index in range(count) if index in self._selected)
        for index in ordered_indexes:
            phase = min(1.0, progress * 1.5 - index * 0.09)
            wave = max(0.0, phase) * max(0.0, 1.0 - max(0.0, phase - 0.58) / 0.42)
            shift = round(2 * wave * (1 if index % 2 == 0 else -1))
            start = max(0, min(width - 3, index * 2 + shift))
            lifted = index in self._selected
            glyphs = ("╭─╮", f"│{'▲' if lifted else '▧'}│", "╰─╯")
            top = 0 if lifted else 1
            for y, line in enumerate(glyphs, top):
                for x, char in enumerate(line):
                    rows[y][start + x] = char
            if lifted:
                selected_spans.extend((y, start) for y in range(top, top + len(glyphs)))
        fan = Text("\n".join("".join(row).rstrip() for row in rows), style="bright_blue")
        line_offsets = []
        offset = 0
        for row in rows:
            line_offsets.append(offset)
            offset += len("".join(row).rstrip()) + 1
        for row, start in selected_spans:
            fan.stylize("bold yellow", line_offsets[row] + start, line_offsets[row] + start + 3)
        self.update(fan)


class OpponentBlock(Vertical):
    """One opponent's status panel and persistent hand fan."""

    def __init__(self, user_id: str):
        self.user_id = user_id
        self.user_panel = Static("", markup=False, classes="opponent-status")
        self.fan = OpponentFan(user_id)
        self._status: tuple[Any, ...] | None = None
        super().__init__(self.user_panel, self.fan, classes="opponent-block")

    def set_status(self, player: dict[str, Any], *, role: str, duty: str) -> None:
        signature = (
            player.get("username"), player.get("seat_position"),
            player.get("cards_remaining"), role, duty,
        )
        if signature == self._status:
            return
        self._status = signature
        self.user_panel.update(_player_panel(player, role=role, duty=duty))


class BoardCard(GameCard):
    """A table card that can animate into place after a server snapshot."""

    motion_x = reactive(0.0)
    motion_y = reactive(0.0)
    actor_progress = reactive(0.0)

    def __init__(self, identity: str, card: Any, *, selected: bool = False, source: tuple[int, int] = (0, 4)):
        self.actor_name: str | None = None
        self._actor_animation_generation = 0
        super().__init__("table", identity, card, selected=selected)
        self._offset = source
        self.styles.offset = source

    def show_actor(self, username: str) -> None:
        """Fade an opponent's name onto this card, hold it, then fade it away."""
        if not username:
            return
        self._actor_animation_generation += 1
        generation = self._actor_animation_generation
        self.actor_name = display_text(username)
        self.actor_progress = 0.0
        self.refresh_card()
        self.animate(
            "actor_progress", 1.0, duration=0.45, easing="out_cubic",
            on_complete=lambda: self._hold_actor(generation),
        )

    def watch_actor_progress(self, _progress: float) -> None:
        self.refresh_card()

    def _hold_actor(self, generation: int) -> None:
        if generation == self._actor_animation_generation and self.is_mounted:
            self.set_timer(2.4, lambda: self._fade_actor(generation))

    def _fade_actor(self, generation: int) -> None:
        if generation != self._actor_animation_generation or not self.is_mounted:
            return
        self.animate(
            "actor_progress", 0.0, duration=0.7, easing="in_out_cubic",
            on_complete=lambda: self._clear_actor(generation),
        )

    def _clear_actor(self, generation: int) -> None:
        if generation != self._actor_animation_generation:
            return
        self.actor_name = None
        self.refresh_card()

    def move_to(self, x: int, y: int, *, animate: bool, from_y: int = 4) -> None:
        target = (max(0, x), max(0, y))
        if target == self._offset:
            return
        if animate:
            # Attacks keep their horizontal layout slot.  Only the new card
            # moves vertically, so neighbouring tricks never need a layout
            # pass for this animation.
            self.motion_x = float(target[0])
            self.motion_y = float(from_y)
            self.animate("motion_y", float(target[1]), duration=0.4, easing="out_cubic")
        else:
            self.styles.offset = target
        self._offset = target

    def watch_motion_x(self, value: float) -> None:
        self.styles.offset = (round(value), round(self.motion_y))

    def watch_motion_y(self, value: float) -> None:
        self.styles.offset = (round(self.motion_x), round(value))

    def refresh_card(self) -> None:
        self.update(_card_panel(
            self.card, selected=self.selected, trump=self.trump,
            focused=self.has_focus, playable=self.playable,
            error_highlight=self.error_highlight,
            actor_name=self.actor_name, actor_progress=self.actor_progress,
        ))


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
        self.hand_sort = "suit-left"
        self.hand_signals: dict[str, dict[str, Any]] = {}
        self.playable_hand_ids: set[str] = set()
        self._hand_signal_key: tuple[str, tuple[int, ...]] | None = None
        self._hand_signal_revision = 0
        self.selected_cards: set[str] = set()
        self.selected_table_id: str | None = None
        self._card_render_lock = asyncio.Lock()
        self._board_reflow_lock = asyncio.Lock()
        self._card_render_revision = 0
        self._rendered_hand_sort: str | None = None
        self._known_table_cards: set[str] = set()
        self._event_history_ready = False
        self._pending_actor_labels: dict[tuple[str, str], str] = {}
        self._table_layout_initialized = False
        self._piles_initialized = False
        self._pile_signatures: dict[str, Any] = {}
        self.confirm_leave = False
        self._chat_started = False

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(classes="detail"):
            yield Label("Game table", classes="heading")
            yield Static(self.game_id, markup=False, classes="muted")
            yield Static("", id="connection", markup=False, classes="connection")
            yield Static("Loading game snapshot…", id="game-info", markup=False)
            yield Static("", id="result", markup=False, classes="result")
            yield Static("", id="turn-info", markup=False, classes="warning")
            with HorizontalScroll(id="player-art", classes="opponent-board"):
                pass
            with Horizontal(id="pile-art", classes="pile-art"):
                pass
            yield Label("Table · Select an attack card to choose a defense target", classes="heading")
            with Vertical(id="board-cards", classes="card-board"):
                pass
            yield Label("Your hand · Select cards to attack or defend", classes="heading")
            yield Button(_HAND_SORT_LABELS[self.hand_sort], id="sort-hand", variant="default", disabled=True)
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
                yield Button("Back to lobbies", id="lobbies")
                yield Button("Open graphical UI", id="web")
            with Vertical(id="leave-confirmation"):
                yield Static("Leave the game? The server will end the game and record your loss.", markup=False, classes="warning")
                with Grid(classes="actions"):
                    yield Button("Confirm leave", id="confirm-leave", variant="error")
                    yield Button("Stay", id="cancel-leave")
            # The outer screen already scrolls. VerticalGroup deliberately
            # sizes itself to its contents, so chat cannot claim a spare
            # fractional screen height below the action buttons.
            with VerticalGroup(id="chat-panel"):
                yield Label("Lobby chat", classes="heading")
                yield Static("Waiting for chat snapshot…", id="chat-connection", markup=False, classes="connection")
                yield SnapshotTable(id="chat")
                with Horizontal(classes="chat-entry"):
                    yield Input(placeholder="Message…", max_length=4000, id="chat-input")
                    yield Button("Send", id="send-chat", variant="primary")
            yield Static(self.initial_notice, id="notice", markup=False)
            yield Static("The server enforces rules and owns game state. A fresh snapshot is loaded after each command.", markup=False, classes="muted")
        yield Footer()

    def _flash_rule_error(self) -> None:
        for card in self.query(GameCard):
            if card.selected:
                card.flash_error()

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
                        self._set_chat_connection(
                            "Chat connected." if state == "live" else
                            "Synchronizing chat…" if state == "synchronizing" else
                            "Chat connection lost; reconnecting…"
                        )
                    if "chat" in panels:
                        self.messages = panels["chat"]
                        self._render_chat()
        except ClientError as error:
            self._set_chat_connection(display_text(error))

    def _set_chat_connection(self, message: str) -> None:
        # A lobby snapshot can start the chat worker while the screen is still
        # mounting, and a late WebSocket update can arrive as it is unmounting.
        # In either case, the widget may not be in the DOM yet.
        widget = next(iter(self.query("#chat-connection")), None)
        if widget is not None:
            widget.update(message)

    def apply_panels(self, panels: dict[str, Any]) -> None:
        previous_events = self.events
        for key, attribute in (
            ("game", "game"), ("players", "players"), ("hand", "hand"),
            ("table", "table"), ("turn", "turn"), ("deck", "deck"), ("events", "events"),
        ):
            if key in panels:
                setattr(self, attribute, panels[key])
        if "events" in panels:
            if self._event_history_ready:
                self._queue_new_actor_labels(previous_events, self.events)
            else:
                # A newly opened client should not animate old actions from
                # the server's bounded public history.
                self._event_history_ready = True
        if "hand_signals" in panels:
            signals = panels["hand_signals"]
            if isinstance(signals, list):
                changed_users = set()
                for signal in signals:
                    if not isinstance(signal, dict) or not isinstance(signal.get("user_id"), str):
                        continue
                    identity = signal["user_id"]
                    previous = self.hand_signals.get(identity, {})
                    if previous.get("sort") != signal.get("sort"):
                        changed_users.add(identity)
                    self.hand_signals[identity] = signal
                self._render_hand_signals(changed_users)
        if "game" in panels:
            self.lobby_id = self.game.get("lobby_id") or self.lobby_id
            self._start_chat()
            next_game = self.game.get("next_game_id")
            if next_game and next_game != self.game_id:
                self.go(GameScreen(next_game, self.lobby_id, "The rematch has started; the new table is open."))
                return
        if any(key in panels for key in ("game", "players", "hand", "table", "turn", "deck")):
            self._render_game()
        if any(key in panels for key in ("game", "hand")):
            self._schedule_hand_signal()

    def _render_hand_signals(self, shuffled_users: set[str] | None = None) -> None:
        if not self.is_mounted:
            return
        me = str(self.client_app.profile.user_id) if self.client_app.profile else ""
        for block in self.query(OpponentBlock):
            if block.user_id == me:
                continue
            signal = self.hand_signals.get(block.user_id, {})
            selected = signal.get("selected", [])
            selected = {
                index for index in selected
                if isinstance(index, int) and not isinstance(index, bool) and index >= 0
            } if isinstance(selected, list) else set()
            raw_count = next((
                player.get("cards_remaining") or 0 for player in self.players
                if str(player.get("user_id", "")) == block.user_id
            ), 0)
            try:
                count = max(0, int(raw_count))
            except (TypeError, ValueError):
                count = 0
            self.run_worker(
                block.fan.sync(
                    count, selected,
                    shuffle=bool(shuffled_users and block.user_id in shuffled_users),
                ),
                group=f"fan-{block.user_id}", exclusive=True,
            )

    def _schedule_hand_signal(self) -> None:
        if not self.is_mounted or self.game.get("share_hand_signals", True) is False:
            return
        display_hand = self._display_hand()
        selected = tuple(
            index for index, card in enumerate(display_hand)
            if str(card.get("hand_id", "")) in self.selected_cards
        )
        key = (self.hand_sort, selected)
        if key == self._hand_signal_key:
            return
        self._hand_signal_key = key
        self._hand_signal_revision += 1
        self.run_worker(
            self._publish_hand_signal(self._hand_signal_revision, key),
            group="hand-signal", exclusive=True,
        )

    async def _publish_hand_signal(
        self, revision: int, key: tuple[str, tuple[int, ...]],
    ) -> None:
        # Coalesce quick selection and sort changes below the server's 20/s limit.
        await asyncio.sleep(0.12)
        if revision != self._hand_signal_revision or not self.is_mounted:
            return
        try:
            await SocketFeed(self.client_app.client).send(
                Subscription("game", self.game_id),
                {"action": "hand_signal", "sort": key[0], "selected": list(key[1])},
            )
        except ClientError:
            # Hand tells are optional display hints and must not interrupt play.
            return

    def _queue_new_actor_labels(
        self, previous: list[dict[str, Any]], current: list[dict[str, Any]],
    ) -> None:
        """Associate newly announced played cards with their table rows."""
        common = 0
        while common < min(len(previous), len(current)) and previous[common] == current[common]:
            common += 1
        if common == 0 and previous and current:
            # The server keeps a bounded event list. Once it is full, each
            # append drops the oldest entry, so find the retained overlap.
            for overlap in range(min(len(previous), len(current)), 0, -1):
                if previous[-overlap:] == current[:overlap]:
                    common = overlap
                    break
        new_events = current[common:]
        me = str(self.client_app.profile.user_id) if self.client_app.profile else ""
        for event in new_events:
            if not isinstance(event, dict) or str(event.get("user_id", "")) == me:
                continue
            action = event.get("action")
            if action not in {"attack", "transfer", "defend"}:
                continue
            username = str(event.get("username") or "")
            cards = event.get("cards")
            if not username or not isinstance(cards, list):
                continue
            card_ids = {
                str(card.get("id")) for card in cards
                if isinstance(card, dict) and card.get("id") is not None
            }
            card_field = "defense" if action == "defend" else "attack"
            for row in self.table:
                card = row.get(card_field)
                if isinstance(card, dict) and str(card.get("id")) in card_ids:
                    self._pending_actor_labels[(str(row.get("id", "")), card_field)] = username

    def _render_game(self) -> None:
        status = {"in_progress": "In progress", "finished": "Game finished"}.get(
            self.game.get("status"), display_text(self.game.get("status", "")),
        )
        self.query_one("#game-info", Static).update(
            f"{status}. State version {self.turn.get('version', '?')} · "
            f"Phase: {display_text(self.turn.get('phase', '—'))}."
        )
        me = self.client_app.profile.user_id if self.client_app.profile else ""
        self._render_result(str(me))
        phase = self.turn.get("phase")
        me_id = str(me)
        attacker = str(self.turn.get("attacker_id", "")) == me_id
        defender = str(self.turn.get("defender_id", "")) == me_id
        passed = {str(identity) for identity in (self.turn.get("passed_ids") or [])}
        pending = {str(identity) for identity in (self.turn.get("pending_ids") or [])}
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
        self.playable_hand_ids = self._playable_hand_ids(
            me_id, phase, attacker, defender, passed,
        )
        self._render_visual_table(me)
        self._update_selection_label()
        self.query_one("#turn-info", Static).update(self._turn_prompt(
            me_id, phase, attacker, defender, pending, passed,
        ))
        # These are presentation hints derived from the server's snapshot;
        # they neither validate a move nor change game state. The server still
        # remains authoritative when a button posts its command.
        can_play = self.game.get("status") == "in_progress"
        has_table = bool(self.table)
        has_open_cards = any(not row.get("defense") for row in self.table)
        all_defended = has_table and not has_open_cards
        wave = phase in {"build", "defend"}
        neighbor_only = bool(self.game.get("neighbor_throw_only"))
        neighbor_ids: set[str] = set()
        if neighbor_only and self.players:
            circle = sorted(self.players, key=lambda player: player.get("seat_position", 0))
            defender_index = next((
                index for index, player in enumerate(circle)
                if str(player.get("user_id", "")) == str(self.turn.get("defender_id", ""))
            ), None)
            if defender_index is not None and len(circle) > 1:
                neighbor_ids = {
                    str(circle[(defender_index - 1) % len(circle)].get("user_id", "")),
                    str(circle[(defender_index + 1) % len(circle)].get("user_id", "")),
                }
        can_throw_in = (
            can_play and bool(self.hand) and has_table and not defender
            and phase in {"build", "defend", "taking"} and me_id not in passed
            and (not neighbor_only or me_id in neighbor_ids)
        )
        visibility = {
            "attack": can_play and bool(self.hand) and (
                (phase == "between" and attacker) or can_throw_in
            ),
            "defend": can_play and bool(self.hand) and wave and defender and has_open_cards,
            "transfer": (
                can_play and bool(self.hand) and wave and defender and has_open_cards
                and self.game.get("transferable") is True
            ),
            "bito": can_play and wave and attacker and all_defended,
            "take": can_play and wave and defender and has_open_cards,
            "pass": can_play and phase == "taking" and me_id in pending,
        }
        for identity, visible in visibility.items():
            button = self.query_one(f"#{identity}", Button)
            button.display = visible
            button.disabled = self.busy or self.stale
        self.query_one("#attack", Button).label = "Attack" if phase == "between" else "Throw in"
        self.query_one("#sort-hand", Button).disabled = self.busy or self.stale
        leave = self.query_one("#leave", Button)
        leave.display = can_play
        leave.disabled = self.busy or self.stale
        self.query_one("#rematch", Button).display = bool(self.game.get("can_rematch"))
        self.query_one("#rematch", Button).disabled = self.busy or self.stale
        self.query_one("#leave-confirmation").display = self.confirm_leave

    def _playable_hand_ids(
        self, me_id: str, phase: Any, attacker: bool, defender: bool, passed: set[str],
    ) -> set[str]:
        """Project likely playable cards for the current server snapshot.

        This only colors cards; selecting or posting any card remains possible
        and the server remains the sole validator of every command.
        """
        if self.game.get("status") != "in_progress" or not self.hand:
            return set()
        hand_ids = {
            str(card.get("hand_id", "")) for card in self.hand
            if isinstance(card.get("hand_id"), str)
        }
        if phase == "between" and attacker:
            return hand_ids

        open_attacks = [
            row.get("attack") for row in self.table
            if not row.get("defense") and isinstance(row.get("attack"), dict)
        ]
        if not open_attacks:
            return set()
        trump_card = self.deck.get("trump_card") or self.game.get("trump_card")
        trump_suit = _suit_symbol(trump_card.get("suit")) if isinstance(trump_card, dict) else ""
        playable: set[str] = set()

        if phase in {"build", "defend"} and defender:
            playable.update(
                str(card["hand_id"]) for card in self.hand
                if isinstance(card.get("hand_id"), str)
                and any(_can_beat(card, attack, trump_suit) for attack in open_attacks)
            )

        if phase in {"build", "defend", "taking"} and not defender and me_id not in passed:
            table_values = {
                _card_value(card) for row in self.table for card in (row.get("attack"), row.get("defense"))
                if isinstance(card, dict)
            }
            playable.update(
                str(card["hand_id"]) for card in self.hand
                if isinstance(card.get("hand_id"), str) and _card_value(card) in table_values
            )
        return playable

    def _render_result(self, me_id: str) -> None:
        result = self.query_one("#result", Static)
        finished = self.game.get("status") == "finished"
        result.display = finished
        if not finished:
            return

        loser_id = str(self.game.get("loser_id") or "")
        loser = next(
            (player for player in self.players if str(player.get("user_id", "")) == loser_id),
            None,
        )
        if loser_id and loser_id == me_id:
            title = Text("You lost!", style="bold bright_red")
            detail = Text("You are the fool this game.")
        elif loser is not None:
            title = Text("You won!", style="bold bright_green")
            detail = Text("The fool this game is ")
            detail.append(display_text(loser.get("username", "Player")), style="bold")
            detail.append(".")
        else:
            title = Text("Game over", style="bold bright_yellow")
            detail = Text("This game has finished.")

        message = Text()
        message.append(title)
        message.append("\n")
        message.append(detail)
        if self.game.get("can_rematch"):
            message.append("\nYou can request a rematch.", style="dim")
        else:
            message.append("\nThe host can deal another game for everyone still in the lobby.", style="dim")
        result.update(message)

    def _render_visual_table(self, me: str) -> None:
        """Paint the visible board from the latest server snapshot only."""
        self._render_players(me)
        self.run_worker(self._render_piles(), group="pile-render", exclusive=True)
        self._schedule_card_widgets()

    def _turn_prompt(
        self, me: str, phase: Any, attacker: bool, defender: bool,
        pending: set[str], passed: set[str],
    ) -> str:
        phase_text = display_text(phase or "unknown")
        if attacker and defender:
            role_text = "attacker and defender"
        elif attacker:
            role_text = "attacker"
        elif defender:
            role_text = "defender"
        elif me in pending:
            role_text = "waiting participant"
        elif me in passed:
            role_text = "finished throwing"
        else:
            role_text = "participant"
        return (
            f"Server phase: {phase_text} · Your server-reported role: {role_text}. "
            "Select any needed cards and submit an action; the server validates it."
        )

    async def _render_piles(self) -> None:
        trump_card = self.deck.get("trump_card") or self.game.get("trump_card")
        trump_in_deck = self.deck.get("trump_in_deck", self.game.get("trump_in_deck", True)) is True
        pile_art = self.query_one("#pile-art", Horizontal)
        if not self._piles_initialized:
            self._deck_pile = Static(classes="pile-item", markup=False)
            self._trump_pile = Static(classes="pile-item", markup=False)
            self._discard_pile = Static(classes="pile-item", markup=False)
            await pile_art.mount(self._deck_pile, self._trump_pile, self._discard_pile)
            self._piles_initialized = True

        deck_signature = self.deck.get("deck_remaining")
        if "deck" not in self._pile_signatures or self._pile_signatures["deck"] != deck_signature:
            self._deck_pile.update(Panel(
                Text(f"{deck_signature if deck_signature is not None else '?'} cards", justify="center"),
                title="Deck", box=box.ROUNDED, width=18, height=7,
            ))
            self._pile_signatures["deck"] = deck_signature

        trump_signature = (trump_card, trump_in_deck)
        if "trump" not in self._pile_signatures or self._pile_signatures["trump"] != trump_signature:
            self._trump_pile.update(_trump_pile_panel(trump_card, in_deck=trump_in_deck))
            self._pile_signatures["trump"] = (
                trump_card.copy() if isinstance(trump_card, dict) else trump_card,
                trump_in_deck,
            )

        discard_signature = self.deck.get("discard_count")
        if "discard" not in self._pile_signatures or self._pile_signatures["discard"] != discard_signature:
            self._discard_pile.update(Panel(
                Text(f"{discard_signature if discard_signature is not None else '?'} cards", justify="center"),
                title="Discard", box=box.ROUNDED, width=18, height=7,
            ))
            self._pile_signatures["discard"] = discard_signature

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
        player_art = self.query_one("#player-art", HorizontalScroll)
        existing_blocks = {
            block.user_id: block for block in player_art.query(OpponentBlock)
        }
        desired_blocks = []
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
            block = existing_blocks.get(identity)
            if block is None:
                block = OpponentBlock(identity)
                player_art.mount(block)
            block.set_status(player, role=role, duty=duty)
            fan = block.fan
            signal = self.hand_signals.get(identity, {})
            selected = signal.get("selected", [])
            selected = {
                index for index in selected
                if isinstance(index, int) and not isinstance(index, bool) and 0 <= index < count
            } if isinstance(selected, list) else set()
            if not fan.is_mounted:
                fan._count = count
                fan._selected = selected
            else:
                self.run_worker(fan.sync(count, selected), group=f"fan-{identity}", exclusive=True)
            desired_blocks.append(block)
        for identity, block in existing_blocks.items():
            if block not in desired_blocks:
                self.run_worker(block.remove(), group=f"fan-remove-{identity}")
        for index, block in enumerate(desired_blocks):
            children = list(player_art.children)
            if index < len(children) and children[index] is not block:
                player_art.move_child(block, before=children[index])
        if not desired_blocks and not list(player_art.children):
            player_art.mount(Static("Waiting for players…", classes="empty-board"))

    def on_resize(self, event) -> None:
        if self.is_mounted:
            self.run_worker(self._reflow_board(), group="board-reflow", exclusive=True)

    async def _reflow_board(self) -> None:
        """Place table pairs on as many centered rows as the terminal width requires."""
        if not self.is_mounted:
            return
        async with self._board_reflow_lock:
            await self._reflow_board_locked()

    async def _reflow_board_locked(self) -> None:
        board = self.query_one("#board-cards", Vertical)
        order = {str(row.get("id", "")): index for index, row in enumerate(self.table)}
        tricks = sorted(board.query(TrickRow), key=lambda trick: order.get(trick.identity, 0))
        available = board.size.width
        if available <= 0:
            return
        groups = []
        per_line = _tricks_per_line(available)
        for start in range(0, len(tricks), per_line):
            groups.append(tricks[start:start + per_line])
        lines = [child for child in board.children if child.has_class("trick-line")]
        while len(lines) < len(groups):
            line = Horizontal(classes="trick-line")
            await board.mount(line)
            lines.append(line)
        for line, group in zip(lines, groups):
            for index, trick in enumerate(group):
                _reparent(trick, line, index)
        for line in lines[len(groups):]:
            await line.remove()
        board.refresh(layout=True)

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
            board_area = self.query_one("#board-cards", Vertical)
            old_table_ids = set(self._known_table_cards)
            next_table_ids = {str(row.get("id", "")) for row in self.table if row.get("id")}

            # Keep table card widgets alive across snapshots; this gives Textual
            # a stable position to animate from as the layout advances.
            for trick in list(board_area.query(TrickRow)):
                if trick.identity not in next_table_ids:
                    await trick.remove()

            for child in list(hand_area.children):
                if not isinstance(child, GameCard) or child.identity not in {str(card.get("hand_id", "")) for card in self.hand}:
                    await child.remove()

            trump_card = self.deck.get("trump_card") or self.game.get("trump_card")
            trump_suit = _suit_symbol(trump_card.get("suit")) if isinstance(trump_card, dict) else ""
            existing_hand = {widget.identity: widget for widget in hand_area.query(GameCard)}
            sort_changed = (
                self._rendered_hand_sort is not None
                and self._rendered_hand_sort != self.hand_sort
            )
            empty_hand = next((c for c in hand_area.children if not isinstance(c, GameCard)), None)
            if self.hand and empty_hand:
                await empty_hand.remove()
            hand_widgets = []
            display_hand = _sorted_hand(
                self.hand, self.hand_sort,
                trump_card.get("suit") if isinstance(trump_card, dict) else None,
            )
            for card in display_hand:
                identity = str(card.get("hand_id", ""))
                widget = existing_hand.get(identity)
                if widget is None:
                    widget = GameCard("hand", identity, card)
                    await hand_area.mount(widget)
                widget.set_card(
                    card, selected=identity in self.selected_cards,
                    trump=bool(trump_suit) and _suit_symbol(card.get("suit")) == trump_suit,
                    playable=identity in self.playable_hand_ids,
                )
                hand_widgets.append(widget)
            if not hand_widgets and not list(hand_area.children):
                await hand_area.mount(Static("Your hand is empty", classes="empty-board"))
            for index, widget in enumerate(hand_widgets):
                if index < len(hand_area.children) and hand_area.children[index] is not widget:
                    hand_area.move_child(widget, before=hand_area.children[index])
                if sort_changed:
                    widget.animate_sort(index)
            self._rendered_hand_sort = self.hand_sort

            existing_board = {
                trick.identity: trick for trick in board_area.query(TrickRow)
            }
            trick_rows = []
            for row in self.table:
                identity = str(row.get("id", ""))
                trick = existing_board.get(identity)
                is_new = identity not in old_table_ids
                if trick is None:
                    attack = BoardCard(identity, row.get("attack"), selected=identity == self.selected_table_id)
                    defense = BoardDefenseCard()
                    trick = TrickRow(identity, attack, defense)
                    await board_area.mount(trick)
                attack = trick.attack_card
                attack.set_card(row.get("attack"), selected=identity == self.selected_table_id, trump=False)
                trick.set_defense(row.get("defense"))
                trick_rows.append(trick)
                if isinstance(attack, BoardCard):
                    # Keep attacks in the left slot.  A raised defense sits
                    # to their right, leaving both card frames intact.
                    attack.move_to(
                        0, 0,
                        animate=self._table_layout_initialized and is_new,
                        from_y=4 if is_new else 0,
                    )
                    actor_name = self._pending_actor_labels.pop((identity, "attack"), None)
                    if actor_name:
                        attack.show_actor(actor_name)
                defense_actor = self._pending_actor_labels.pop((identity, "defense"), None)
                if defense_actor:
                    trick.defense_card.show_actor(defense_actor)
            self._known_table_cards = next_table_ids
            self._table_layout_initialized = True
            if trick_rows:
                for child in list(board_area.children):
                    if child.has_class("empty-board"):
                        await child.remove()
                await self._reflow_board()
            else:
                for line in list(board_area.query(".trick-line")):
                    await line.remove()
                if not board_area.query(".empty-board"):
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
        """Keep card actions available and let the server validate submissions."""
        if not self.is_mounted:
            return
        for identity in ("attack", "transfer"):
            button = self.query_one(f"#{identity}", Button)
            button.disabled = self.busy or self.stale

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
            "Use Enter or Space to select a card."
        )

    def _render_chat(self) -> None:
        table = next(iter(self.query("#chat")), None)
        if table is None:
            return
        table.set_rows({
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
        self.query_one("#sort-hand", Button).disabled = self.busy or self.stale
        self._sync_card_action_buttons()

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
    def _display_hand(self) -> list[dict[str, Any]]:
        trump_card = self.deck.get("trump_card") or self.game.get("trump_card")
        return _sorted_hand(
            self.hand, self.hand_sort,
            trump_card.get("suit") if isinstance(trump_card, dict) else None,
        )

    def _selected_card_ids(self) -> list[str]:
        return [
            str(card["id"]) for card in self.hand
            if str(card.get("hand_id")) in self.selected_cards and isinstance(card.get("id"), str)
        ]

    async def _game_command(self, action: str) -> None:
        card_ids = self._selected_card_ids()
        selected_table = self.selected_table_id
        selected_card = next((card for card in self.hand if str(card.get("hand_id")) in self.selected_cards), None)
        game = self.client_app.games
        try:
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
        except ApiError:
            # Catch the server rejection here so only rejected game actions
            # trigger the visual error effect. Chat and rematch errors do not.
            self._flash_rule_error()
            raise
        self.selected_cards.clear()
        self.selected_table_id = None
        self._update_card_visuals()
        self._schedule_hand_signal()
        self.notice("Command accepted by the server. Loading the latest snapshot.")
        self.start_stream()

    @on(Button.Pressed)
    def press(self, event: Button.Pressed) -> None:
        identity = event.button.id
        if identity == "sort-hand":
            if self.busy or self.stale:
                return
            self.hand_sort = _HAND_SORTS[(_HAND_SORTS.index(self.hand_sort) + 1) % len(_HAND_SORTS)]
            self.query_one("#sort-hand", Button).label = _HAND_SORT_LABELS[self.hand_sort]
            self._schedule_card_widgets()
            self._schedule_hand_signal()
        elif identity in {"attack", "defend", "transfer", "bito", "take", "pass"}:
            if not self.busy and not self.stale:
                self.start_action(lambda: self._game_command(identity))
        elif identity == "leave":
            if self.busy or self.stale:
                return
            self.confirm_leave = True
            self._render_game()
            self.query_one(".detail", VerticalScroll).scroll_to_widget(
                self.query_one("#leave-confirmation"), animate=False,
            )
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
        elif identity == "lobbies":
            if not self.busy:
                self.client_app.show_lobbies()
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
        self.client_app.show_lobbies("You left the game.")

    async def rematch(self) -> None:
        next_game = await self.client_app.games.rematch(self.game_id)
        self.go(GameScreen(next_game, self.lobby_id, "The rematch has started."))

