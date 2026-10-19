"""Shared screen lifecycle, safe terminal text, and incremental tables."""

from collections.abc import Awaitable, Callable
from contextlib import aclosing
from typing import TYPE_CHECKING, Any, cast

from rich.text import Text
from textual.screen import Screen
from textual.widgets import Button, DataTable, Static

from fools_client.errors import (
    ApiError, AuthenticationRequired, ClientError, NetworkError, display_text,
)
from fools_client.realtime import Subscription

if TYPE_CHECKING:
    from fools_client.ui.app import FoolsApp


class SnapshotTable(DataTable):
    def __init__(self, **arguments: Any):
        super().__init__(cursor_type="row", zebra_stripes=True, **arguments)
        self._snapshot_rows: dict[str, tuple[str, ...]] = {}
        self._column_keys = []

    def setup(self, *columns: str) -> None:
        self._column_keys = self.add_columns(*columns)

    def action_cursor_down(self) -> None:
        # Keep arrow navigation within rows, but let the user leave the table
        # from its last row and continue to the controls below it.
        if not self.row_count or self.cursor_row >= self.row_count - 1:
            self.screen.focus_next()
            return
        super().action_cursor_down()

    def action_cursor_up(self) -> None:
        # Likewise, move to the previous control from the first row.
        if not self.row_count or self.cursor_row <= 0:
            self.screen.focus_previous()
            return
        super().action_cursor_up()

    @property
    def selected_id(self) -> str | None:
        if not self.row_count or not 0 <= self.cursor_row < self.row_count:
            return None
        return self.ordered_rows[self.cursor_row].key.value

    def set_rows(self, rows: dict[str, tuple[str, ...]]) -> None:
        selected = self.selected_id
        for identity in self._snapshot_rows.keys() - rows.keys():
            self.remove_row(identity)
        for identity, cells in rows.items():
            previous = self._snapshot_rows.get(identity)
            if previous is None:
                self.add_row(*(Text(cell) for cell in cells), key=identity)
            else:
                for column, before, after in zip(self._column_keys, previous, cells, strict=True):
                    if before != after:
                        self.update_cell(identity, column, Text(after), update_width=True)
        self._snapshot_rows = rows.copy()
        if selected in rows:
            self.move_cursor(row=self.get_row_index(selected))


class ActionScreen(Screen):
    def __init__(self):
        super().__init__()
        self.busy = False

    @property
    def client_app(self) -> "FoolsApp":
        return cast("FoolsApp", self.app)

    def notice(self, text: object, *, error: bool = False) -> None:
        label = self.query_one("#notice", Static)
        label.update(display_text(text))
        label.set_class(error, "error")

    def sync_controls(self) -> None:
        for button in self.query(Button):
            button.disabled = self.busy

    def start_action(self, action: Callable[[], Awaitable[None]]) -> None:
        if self.busy:
            return
        self.busy = True
        self.sync_controls()
        self.run_worker(self._perform(action), group="command")

    async def _perform(self, action: Callable[[], Awaitable[None]]) -> None:
        try:
            await action()
        except AuthenticationRequired as error:
            self.client_app.show_auth(str(error))
        except ApiError as error:
            if error.authentication_required:
                self.client_app.show_auth("Your session has expired. Please sign in again.")
            else:
                self.command_error(error)
        except ClientError as error:
            self.command_error(error)
        finally:
            self.busy = False
            if self.is_mounted:
                self.sync_controls()

    def command_error(self, error: ClientError) -> None:
        self.notice(error, error=True)

    def go(self, screen: Screen) -> None:
        self.client_app.navigate(screen)

    def action_back(self) -> None:
        if not self.busy:
            self.client_app.show_lobbies()

    async def logout(self) -> None:
        notice = await self.client_app.auth.logout()
        self.client_app.show_auth(notice)


class LiveScreen(ActionScreen):
    BINDINGS = [("ctrl+r", "refresh", "Refresh")]

    def __init__(self, subscription: Subscription):
        super().__init__()
        self.subscription = subscription
        self.stale = True

    def on_mount(self) -> None:
        self.start_stream()

    def start_stream(self) -> None:
        self.stale = True
        self.query_one("#connection", Static).update("Connecting; data has not been verified yet.")
        self.sync_controls()
        self.run_worker(self._watch(), group="live", exclusive=True)

    def action_refresh(self) -> None:
        if not self.busy:
            self.start_stream()

    async def _watch(self) -> None:
        profile = self.client_app.profile
        if profile is None:
            self.client_app.show_auth("Please sign in.")
            return
        live = self.client_app.live_factory(self.client_app.client, profile.user_id)
        try:
            async with aclosing(live.watch(self.subscription)) as updates:
                async for update in updates:
                    if "connection" in update.panels:
                        connection = update.panels["connection"]
                        self.stale = connection.get("stale", True)
                        self._connection(connection)
                    self.apply_panels(update.panels)
                    self.sync_controls()
        except AuthenticationRequired as error:
            self.client_app.show_auth(str(error))
        except ClientError as error:
            self.stale = True
            self._connection({"state": "failed", "stale": True})
            if isinstance(error, ApiError) and error.authentication_required:
                self.client_app.show_auth("Your session has expired. Please sign in again.")
            else:
                self.notice(error, error=True)
                self.sync_controls()

    def _connection(self, connection: dict[str, Any]) -> None:
        labels = {
            "live": "Connected. Snapshot is current.",
            "connecting": "Connecting…",
            "synchronizing": "Connected; loading snapshot…",
            "reconnecting": "Connection lost; reconnecting…",
            "snapshot_retry": "Snapshot unavailable; retrying…",
            "failed": "Synchronization stopped. Press Refresh.",
        }
        text = labels.get(connection.get("state"), "Restoring connection…")
        if self.stale:
            text += " Data may be stale; actions are disabled."
        self.query_one("#connection", Static).update(text)

    def apply_panels(self, panels: dict[str, Any]) -> None:
        raise NotImplementedError

    def command_error(self, error: ClientError) -> None:
        super().command_error(error)
        if isinstance(error, NetworkError) and error.command_sent:
            self.start_stream()
