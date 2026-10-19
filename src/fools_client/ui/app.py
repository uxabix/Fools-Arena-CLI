"""Standalone Textual app sharing the CLI's HTTP client and session store."""

from collections.abc import Callable
import webbrowser

from textual.app import App
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import DataTable, Input, Select

from fools_client.api import ApiClient
from fools_client.auth import AuthService, UserProfile
from fools_client.errors import display_text
from fools_client.games import GameService, LobbyChatService
from fools_client.live import LiveSession
from fools_client.lobbies import LobbyService
from fools_client.ui.auth import AuthScreen
from fools_client.ui.lobbies import LobbiesScreen


class FoolsApp(App[None]):
    TITLE = "Fools Arena"
    CSS_PATH = "client.tcss"
    BINDINGS = [
        ("ctrl+q", "quit", "Quit client"),
        Binding("up", "focus_previous", "Previous control", show=True, priority=True),
        Binding("down", "focus_next", "Next control", show=True, priority=True),
        Binding("left", "focus_previous_horizontal", "Previous control", show=False),
        Binding("right", "focus_next_horizontal", "Next control", show=False),
    ]

    def __init__(
        self, client: ApiClient, *, transport_notice: str = "",
        live_factory: Callable[[ApiClient, str], LiveSession] = LiveSession,
    ):
        super().__init__()
        self.client = client
        self.auth = AuthService(client)
        self.lobbies = LobbyService(client)
        self.games = GameService(client)
        self.chat = LobbyChatService(client)
        self.live_factory = live_factory
        self.transport_notice = transport_notice
        self.profile: UserProfile | None = None
        self._navigation = 0
        self.sub_title = client.server

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action in {
            "focus_previous", "focus_next", "focus_previous_horizontal", "focus_next_horizontal",
        }:
            if isinstance(self.focused, (DataTable, Select)):
                return None
            if isinstance(self.focused, Input) and action in {
                "focus_previous_horizontal", "focus_next_horizontal",
            }:
                return None
        return True

    def action_focus_previous_horizontal(self) -> None:
        self.screen.focus_previous()

    def action_focus_next_horizontal(self) -> None:
        self.screen.focus_next()

    def on_mount(self) -> None:
        self.push_screen(AuthScreen())

    def navigate(self, screen: Screen) -> None:
        self._navigation += 1
        self.call_later(self._install_screen, screen, self._navigation)

    async def _install_screen(self, screen: Screen, generation: int) -> None:
        if generation != self._navigation:
            return
        if self.profile is None and not isinstance(screen, AuthScreen):
            screen = AuthScreen("Your session has expired. Please sign in again.")
        await self.switch_screen(screen)

    def signed_in(self, profile: UserProfile, notice: str = "") -> None:
        self.profile = profile
        self.sub_title = display_text(f"{profile.username} · {self.client.server}")
        self.show_lobbies(notice)

    def show_auth(self, notice: str = "") -> None:
        self.profile = None
        self.sub_title = self.client.server
        self.navigate(AuthScreen(notice))

    def show_lobbies(self, notice: str = "") -> None:
        self.navigate(LobbiesScreen(notice))

    def open_graphical(self) -> bool:
        """Open the server-hosted graphical app; authoritative state stays server-side."""
        return webbrowser.open(f"{self.client.server}/")
