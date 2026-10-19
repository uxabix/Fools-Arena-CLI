"""Account forms and restoration of an existing server session."""

from textual import on
from textual.app import ComposeResult
from textual.containers import Grid, VerticalScroll
from textual.widgets import Button, Footer, Header, Input, Label, Static

from fools_client.errors import ApiError, AuthenticationRequired
from fools_client.ui.common import ActionScreen


class AuthScreen(ActionScreen):
    def __init__(self, notice: str = ""):
        super().__init__()
        self.initial_notice = notice

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(classes="form"):
            yield Label("Fools Arena — Sign in or register", classes="heading")
            yield Static(self.client_app.client.server, markup=False, classes="muted")
            yield Static(self.client_app.transport_notice, markup=False, classes="warning")
            yield Label("Username")
            yield Input(id="username", max_length=150)
            yield Label("Email (for registration)")
            yield Input(id="email", max_length=254)
            yield Label("Password")
            yield Input(id="password", password=True)
            with Grid(classes="actions"):
                yield Button("Sign in", id="login", variant="primary")
                yield Button("Register", id="register")
                yield Button("Check session", id="restore")
                yield Button("Open graphical UI", id="web")
            yield Static(self.initial_notice, id="notice", markup=False)
            yield Static(
                "Registration does not start a session when email confirmation is required. "
                "Open the link in your email, then sign in. Passwords are never stored on disk.",
                markup=False, classes="muted",
            )
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#username", Input).focus()
        if self.client_app.client.has_session:
            self.start_action(self.restore)

    async def restore(self) -> None:
        try:
            profile = await self.client_app.auth.profile()
        except AuthenticationRequired:
            self.notice("No saved session was found, or it has expired. Please sign in again.")
        else:
            self.client_app.signed_in(profile)

    @on(Input.Submitted, "#password")
    def submit_password(self) -> None:
        self.start_action(self.login)

    @on(Button.Pressed)
    def press(self, event: Button.Pressed) -> None:
        actions = {"login": self.login, "register": self.register, "restore": self.restore}
        if event.button.id in actions:
            self.start_action(actions[event.button.id])
        elif event.button.id == "web":
            opened = self.client_app.open_graphical()
            self.notice(
                "Opened the graphical app in your browser. Sign in there if needed."
                if opened else "Could not open a browser. Open the server URL manually.",
                error=not opened,
            )

    def credentials(self) -> tuple[str, str]:
        username = self.query_one("#username", Input).value.strip()
        password = self.query_one("#password", Input).value
        self.query_one("#password", Input).value = ""
        return username, password

    async def login(self) -> None:
        username, password = self.credentials()
        try:
            profile = await self.client_app.auth.login(username, password)
        except ApiError as error:
            if error.status_code == 400:
                self.notice(
                    f"{error} If you registered recently, check your email for a confirmation link. "
                    "The API does not distinguish an unconfirmed email from invalid credentials.",
                    error=True,
                )
                return
            raise
        self.client_app.signed_in(profile)

    async def register(self) -> None:
        username, password = self.credentials()
        email = self.query_one("#email", Input).value.strip()
        notice = await self.client_app.auth.register(username, email, password)
        if self.client_app.client.has_session:
            self.client_app.signed_in(
                await self.client_app.auth.profile(),
                notice + " The server started a session because email confirmation is disabled.",
            )
        else:
            self.notice(notice + " No session was started. Confirm your email, then select Sign in.")
