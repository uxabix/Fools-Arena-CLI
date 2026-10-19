"""Standalone Textual app sharing the CLI's HTTP client and session store."""

from collections.abc import Callable
import webbrowser

from textual.app import App
from textual.screen import Screen

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
    BINDINGS = [("ctrl+q", "quit", "Quit client")]

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
