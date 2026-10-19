"""Live lobby browser, forms, membership, readiness, and server-side start."""

from typing import Any

from textual import on
from textual.app import ComposeResult
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Checkbox, Footer, Header, Input, Label, Select, Static

from fools_client.errors import display_text
from fools_client.realtime import Subscription
from fools_client.ui.common import ActionScreen, LiveScreen, SnapshotTable

STATUS_LABELS = {
    "waiting": "Waiting", "ready": "Ready", "playing": "Playing", "left": "Left",
    "in_progress": "In progress", "closed": "Closed", "finished": "Finished",
}


class LobbyRules(Vertical):
    def __init__(self, settings: dict[str, Any] | None = None):
        super().__init__(classes="rules")
        self.settings = settings or {}

    def compose(self) -> ComposeResult:
        yield Label("Player capacity")
        yield Select.from_values(
            range(2, 9), value=self.settings.get("max_players", 4),
            allow_blank=False, id="max-players",
        )
        yield Label("Deck")
        yield Select.from_values(
            [24, 36, 52], value=self.settings.get("card_count", 36),
            allow_blank=False, id="card-count",
        )
        yield Checkbox("Transferable", value=self.settings.get("is_transferable", False), id="transferable")
        yield Checkbox(
            "Only neighboring players may throw in", value=self.settings.get("neighbor_throw_only", False),
            id="neighbors",
        )
        yield Checkbox("Fill empty seats with bots", value=self.settings.get("fill_with_bots", False), id="bots")

    def payload(self) -> dict[str, Any]:
        return {
            "max_players": self.query_one("#max-players", Select).value,
            "card_count": self.query_one("#card-count", Select).value,
            "is_transferable": self.query_one("#transferable", Checkbox).value,
            "neighbor_throw_only": self.query_one("#neighbors", Checkbox).value,
            "fill_with_bots": self.query_one("#bots", Checkbox).value,
            "share_hand_signals": self.settings.get("share_hand_signals", True),
        }


class LobbiesScreen(LiveScreen):
    def __init__(self, notice: str = ""):
        super().__init__(Subscription("lobbies"))
        self.initial_notice = notice

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(classes="browser"):
            yield Label("Public lobbies", classes="heading")
            yield Static("", id="connection", markup=False, classes="connection")
            yield SnapshotTable(id="lobbies")
            yield Static("Loading lobby list…", id="summary", markup=False)
            with Grid(classes="actions"):
                yield Button("Join selected", id="join", variant="primary")
                yield Button("Join by UUID", id="join-id")
                yield Button("Create lobby", id="create")
                yield Button("Refresh", id="refresh")
                yield Button("Open graphical UI", id="web")
                yield Button("Sign out", id="logout")
            yield Static(self.initial_notice, id="notice", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#lobbies", SnapshotTable).setup("Name", "Players", "Ready", "Deck", "Mode")
        super().on_mount()
        self.query_one("#lobbies", SnapshotTable).focus()

    def apply_panels(self, panels: dict[str, Any]) -> None:
        if "lobbies" not in panels:
            return
        rows = {}
        for lobby in panels["lobbies"]:
            settings = lobby.get("settings", {})
            rows[lobby["id"]] = (
                display_text(lobby.get("name", "")),
                f"{lobby.get('active_player_count', '?')}/{settings.get('max_players', '?')}",
                str(lobby.get("ready_count", 0)), str(settings.get("card_count", "?")),
                "Transferable" if settings.get("is_transferable") else "Throw-in",
            )
        self.query_one("#lobbies", SnapshotTable).set_rows(rows)
        self.query_one("#summary", Static).update(
            f"Lobbies: {len(rows)}. Private lobbies can be joined by UUID."
            if rows else "No public lobbies. Create one or join by UUID."
        )

    def sync_controls(self) -> None:
        super().sync_controls()
        self.query_one("#join", Button).disabled = (
            self.busy or self.stale or self.query_one("#lobbies", SnapshotTable).selected_id is None
        )

    @on(SnapshotTable.RowHighlighted, "#lobbies")
    def highlighted(self) -> None:
        self.sync_controls()

    @on(SnapshotTable.RowSelected, "#lobbies")
    def selected(self) -> None:
        if not self.busy and not self.stale:
            identity = self.query_one("#lobbies", SnapshotTable).selected_id
            if identity:
                self.go(JoinLobbyScreen(identity))

    @on(Button.Pressed)
    def press(self, event: Button.Pressed) -> None:
        if self.busy:
            return
        match event.button.id:
            case "join":
                self.selected()
            case "join-id":
                self.go(JoinLobbyScreen())
            case "create":
                self.go(CreateLobbyScreen())
            case "refresh":
                self.action_refresh()
            case "web":
                opened = self.client_app.open_graphical()
                self.notice(
                    "Opened the graphical app in your browser. Sign in there if needed; game state stays on the server."
                    if opened else "Could not open a browser. Open the server URL manually.",
                    error=not opened,
                )
            case "logout":
                self.start_action(self.logout)


class JoinLobbyScreen(ActionScreen):
    BINDINGS = [("escape", "back", "Back to lobbies")]

    def __init__(self, lobby_id: str = ""):
        super().__init__()
        self.lobby_id = lobby_id

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(classes="form"):
            yield Label("Join lobby", classes="heading")
            yield Label("Lobby UUID")
            yield Input(self.lobby_id, id="lobby-id")
            yield Label("Private lobby password (leave blank for public lobbies)")
            yield Input(id="lobby-password", password=True, max_length=128)
            yield Static(
                "Joining another lobby removes you from your current lobby. "
                "If a game is in progress, the server records this as leaving the game.",
                markup=False, classes="warning",
            )
            with Grid(classes="actions"):
                yield Button("Join", id="submit", variant="primary")
                yield Button("Back", id="back")
            yield Static("", id="notice", markup=False)
        yield Footer()

    @on(Input.Submitted, "#lobby-password")
    def submitted(self) -> None:
        self.start_action(self.join)

    @on(Button.Pressed)
    def press(self, event: Button.Pressed) -> None:
        if event.button.id == "submit":
            self.start_action(self.join)
        elif event.button.id == "back":
            self.action_back()

    async def join(self) -> None:
        identity = self.query_one("#lobby-id", Input).value.strip()
        password = self.query_one("#lobby-password", Input).value
        self.query_one("#lobby-password", Input).value = ""
        lobby = await self.client_app.lobbies.join(identity, password)
        game_id = lobby.get("active_game_id")
        if game_id:
            from fools_client.ui.game import GameScreen
            self.go(GameScreen(game_id, lobby["id"]))
        else:
            self.go(LobbyScreen(lobby["id"]))


class CreateLobbyScreen(ActionScreen):
    BINDINGS = [("escape", "back", "Back to lobbies")]

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(classes="form"):
            yield Label("Create lobby", classes="heading")
            yield Label("Name")
            yield Input(id="lobby-name", max_length=100)
            yield Checkbox("Private lobby", id="private")
            yield Label("Private lobby password")
            yield Input(id="lobby-password", password=True, max_length=128, disabled=True)
            yield LobbyRules()
            yield Static(
                "Creating a lobby removes you from your current lobby and ends your current game, "
                "if any. The server validates the settings.",
                markup=False, classes="warning",
            )
            with Grid(classes="actions"):
                yield Button("Create", id="submit", variant="primary")
                yield Button("Back", id="back")
            yield Static("", id="notice", markup=False)
        yield Footer()

    @on(Checkbox.Changed, "#private")
    def privacy(self, event: Checkbox.Changed) -> None:
        self.query_one("#lobby-password", Input).disabled = not event.value

    @on(Button.Pressed)
    def press(self, event: Button.Pressed) -> None:
        if event.button.id == "submit":
            self.start_action(self.create)
        elif event.button.id == "back":
            self.action_back()

    async def create(self) -> None:
        name = self.query_one("#lobby-name", Input).value.strip()
        private = self.query_one("#private", Checkbox).value
        password = self.query_one("#lobby-password", Input).value if private else ""
        self.query_one("#lobby-password", Input).value = ""
        lobby = await self.client_app.lobbies.create(
            name, is_private=private, password=password,
            settings=self.query_one(LobbyRules).payload(),
        )
        self.go(LobbyScreen(lobby["id"]))


class SettingsScreen(ActionScreen):
    BINDINGS = [("escape", "back", "Cancel")]

    def __init__(self, lobby_id: str, settings: dict[str, Any]):
        super().__init__()
        self.lobby_id = lobby_id
        self.settings = settings.copy()

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(classes="form"):
            yield Label("Lobby settings", classes="heading")
            yield LobbyRules(self.settings)
            yield Static(
                "Settings are applied by the server and may reset player readiness. "
                "Ownership and lobby status are checked when saving.",
                markup=False, classes="muted",
            )
            with Grid(classes="actions"):
                yield Button("Save", id="submit", variant="primary")
                yield Button("Cancel", id="back")
            yield Static("", id="notice", markup=False)
        yield Footer()

    def action_back(self) -> None:
        if not self.busy:
            self.go(LobbyScreen(self.lobby_id))

    @on(Button.Pressed)
    def press(self, event: Button.Pressed) -> None:
        if event.button.id == "submit":
            self.start_action(self.save)
        elif event.button.id == "back":
            self.action_back()

    async def save(self) -> None:
        await self.client_app.lobbies.update_settings(
            self.lobby_id, self.query_one(LobbyRules).payload(),
        )
        self.go(LobbyScreen(self.lobby_id, "Settings saved on the server."))


class LobbyScreen(LiveScreen):
    BINDINGS = [("escape", "back", "Back to lobbies"), ("ctrl+r", "refresh", "Refresh")]

    def __init__(self, lobby_id: str, notice: str = ""):
        super().__init__(Subscription("lobby", lobby_id))
        self.lobby_id = self.subscription.resource_id
        self.initial_notice = notice
        self.lobby: dict[str, Any] = {}
        self.players: list[dict[str, Any]] = []
        self.settings: dict[str, Any] = {}
        self.messages: list[dict[str, Any]] = []
        self.pending: str | None = None
        self._opening_game_id: str | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(classes="detail"):
            yield Label("Lobby", classes="heading")
            yield Static(self.lobby_id, markup=False, classes="muted")
            yield Static("", id="connection", markup=False, classes="connection")
            yield Static("Loading lobby snapshot…", id="lobby-info", markup=False)
            yield SnapshotTable(id="players")
            yield Static("", id="rules-info", markup=False)
            yield Static("", id="game-info", markup=False, classes="warning")
            yield Label("Lobby chat", classes="heading")
            yield Static("", id="chat-connection", markup=False, classes="connection")
            yield SnapshotTable(id="chat")
            with Horizontal(classes="chat-entry"):
                yield Input(placeholder="Message…", max_length=4000, id="chat-input")
                yield Button("Send", id="send-chat", variant="primary")
            with Grid(classes="actions"):
                yield Button("Ready", id="ready", variant="primary")
                yield Button("Settings", id="settings")
                yield Button("Start game", id="start", variant="success")
                yield Button("Leave lobby", id="leave", variant="error")
                yield Button("Refresh", id="refresh")
                yield Button("Back to lobbies", id="back")
                yield Button("Open graphical UI", id="web")
                yield Button("Sign out", id="logout")
            with Vertical(id="confirmation"):
                yield Static("", id="confirm-text", markup=False, classes="warning")
                with Grid(classes="actions"):
                    yield Button("Confirm", id="confirm", variant="warning")
                    yield Button("Cancel", id="cancel")
            yield Static(self.initial_notice, id="notice", markup=False)
            yield Static(
                "Back to lobbies and closing the client do not leave this lobby. "
                "The game table and chat remain available after the game starts.",
                markup=False, classes="muted",
            )
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#players", SnapshotTable).setup("Player", "Status", "Role")
        self.query_one("#chat", SnapshotTable).setup("Time", "Player", "Message")
        super().on_mount()

    @property
    def member(self) -> dict[str, Any]:
        profile = self.client_app.profile
        return next((
            player for player in self.players
            if profile and player.get("user_id") == profile.user_id and player.get("status") != "left"
        ), {})

    def apply_panels(self, panels: dict[str, Any]) -> None:
        if "lobby" in panels:
            self.lobby = panels["lobby"]
        if "players" in panels:
            self.players = panels["players"]
        if "settings" in panels:
            self.settings = panels["settings"]
        if "chat" in panels:
            self.messages = panels["chat"]
            self._render_chat()
        if "connection" in panels:
            connection = panels["connection"]
            state = connection.get("state")
            self.query_one("#chat-connection", Static).update(
                "Chat connected." if state == "live" else
                "Synchronizing chat…" if state == "synchronizing" else
                "Chat connection lost; reconnecting…"
            )
        if {"lobby", "players", "settings"}.intersection(panels):
            self.render_snapshot()
        game_id = self.lobby.get("active_game_id")
        if game_id and game_id != self._opening_game_id:
            self._opening_game_id = game_id
            from fools_client.ui.game import GameScreen
            self.go(GameScreen(game_id, self.lobby_id))

    def _render_chat(self) -> None:
        self.query_one("#chat", SnapshotTable).set_rows({
            str(message["id"]): (
                display_text(str(message.get("sent_at", ""))[:19]),
                display_text(message.get("username", "")),
                display_text(message.get("content", "")),
            ) for message in self.messages if isinstance(message, dict) and isinstance(message.get("id"), str)
        })

    def render_snapshot(self) -> None:
        owner_id = self.lobby.get("owner_id")
        state = STATUS_LABELS.get(self.lobby.get("status"), self.lobby.get("status", ""))
        self.query_one("#lobby-info", Static).update(
            f"{display_text(self.lobby.get('name', ''))} — {display_text(state)}\n"
            f"Ready: {self.lobby.get('ready_count', '?')}. "
            f"Players: {self.lobby.get('active_player_count', '?')}/{self.settings.get('max_players', '?')}. "
            + ("Private lobby." if self.lobby.get("is_private") else "Public lobby.")
        )
        self.query_one("#players", SnapshotTable).set_rows({
            player["user_id"]: (
                display_text(player.get("username", "")) + (" (bot)" if player.get("is_bot") else ""),
                display_text(STATUS_LABELS.get(player.get("status"), player.get("status", ""))),
                "Owner" if player["user_id"] == owner_id else "Player",
            ) for player in self.players
        })
        self.query_one("#rules-info", Static).update(
            f"Deck: {self.settings.get('card_count', '?')}. "
            + ("Transferable. " if self.settings.get("is_transferable") else "Throw-in. ")
            + ("Only neighbors may throw in. " if self.settings.get("neighbor_throw_only") else "Anyone may throw in. ")
            + ("Empty seats are filled with bots." if self.settings.get("fill_with_bots") else "No bot fill.")
        )
        game_id = self.lobby.get("active_game_id")
        self.query_one("#game-info", Static).update(
            f"Game {display_text(game_id)} is already in progress. Opening the game table…"
            if game_id else ""
        )
        if not self.member:
            self.notice("You are no longer a member of this lobby. Return to the lobby list.", error=True)

    def permissions(self) -> dict[str, bool]:
        profile = self.client_app.profile
        owner = bool(profile and self.lobby.get("owner_id") == profile.user_id)
        waiting = self.lobby.get("status") == "waiting"
        available = not self.busy and not self.stale and bool(self.member)
        return {
            "ready": available and waiting,
            "settings": available and waiting and owner,
            "start": available and waiting and owner and bool(self.lobby.get("can_start")),
            "leave": available,
        }

    def sync_controls(self) -> None:
        super().sync_controls()
        self.query_one("#send-chat", Button).disabled = self.busy or self.stale
        permissions = self.permissions()
        for identity, allowed in permissions.items():
            self.query_one(f"#{identity}", Button).disabled = not allowed or self.pending is not None
        self.query_one("#ready", Button).label = "Set not ready" if self.member.get("status") == "ready" else "Set ready"
        self.query_one("#confirmation").display = self.pending is not None
        self.query_one("#confirm", Button).disabled = not permissions.get(self.pending, False)

    @on(Button.Pressed)
    def press(self, event: Button.Pressed) -> None:
        if self.busy:
            return
        identity = event.button.id
        if identity in {"ready", "settings", "start", "leave"} and not self.permissions()[identity]:
            return
        match identity:
            case "ready":
                self.start_action(self.ready)
            case "settings":
                self.go(SettingsScreen(self.lobby_id, self.settings))
            case "start" | "leave":
                self.pending = identity
                self.query_one("#confirm-text", Static).update(
                    "Start a game on the server? The client will open the game table."
                    if identity == "start" else
                    "Leave this lobby? During a game, the server records this as leaving the game. "
                    "An empty lobby may close, and ownership may be transferred to another player."
                )
                self.sync_controls()
                self.query_one("#confirmation").scroll_visible()
            case "confirm":
                if self.permissions().get(self.pending, False):
                    action = self.start if self.pending == "start" else self.leave
                    self.pending = None
                    self.start_action(action)
            case "cancel":
                self.pending = None
                self.sync_controls()
            case "refresh":
                self.action_refresh()
            case "web":
                opened = self.client_app.open_graphical()
                self.notice(
                    "Opened the graphical app in your browser. Sign in there if needed; game state stays on the server."
                    if opened else "Could not open a browser. Open the server URL manually.",
                    error=not opened,
                )
            case "back":
                self.action_back()
            case "logout":
                self.start_action(self.logout)
            case "send-chat":
                self.start_action(self.send_chat)

    @on(Input.Submitted, "#chat-input")
    def submit_chat(self) -> None:
        self.start_action(self.send_chat)

    async def send_chat(self) -> None:
        content = self.query_one("#chat-input", Input).value.strip()
        if not content or self.stale:
            return
        await self.client_app.chat.send(self.lobby_id, content)
        self.query_one("#chat-input", Input).value = ""
        self.notice("Message sent.")

    async def ready(self) -> None:
        await self.client_app.lobbies.ready(self.lobby_id, self.member.get("status") != "ready")
        self.notice("Readiness change accepted. Loading a fresh snapshot.")
        self.start_stream()

    async def start(self) -> None:
        game_id = await self.client_app.lobbies.start(self.lobby_id)
        from fools_client.ui.game import GameScreen
        self.go(GameScreen(game_id, self.lobby_id, "The server started the game."))

    async def leave(self) -> None:
        await self.client_app.lobbies.leave(self.lobby_id)
        self.client_app.show_lobbies("You left the lobby.")
