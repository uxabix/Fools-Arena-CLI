"""Server snapshots projected into independently refreshable view panels."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import aclosing, suppress
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from fools_client.api import ApiClient
from fools_client.errors import (
    ApiError, AuthenticationRequired, ClientError, ConfigurationError, NetworkError, ProtocolError,
)
from fools_client.realtime import ConnectionNotice, ServerEvent, SocketFeed, Subscription


@dataclass(frozen=True)
class PanelUpdate:
    subscription: Subscription
    panels: dict[str, Any]
    reason: str


class _Panels:
    def __init__(self, subscription: Subscription):
        self.subscription = subscription
        self.values: dict[str, Any] = {}

    def update(self, values: dict[str, Any], reason: str) -> PanelUpdate | None:
        changed = {
            key: deepcopy(value)
            for key, value in values.items()
            if key not in self.values or self.values[key] != value
        }
        self.values.update(deepcopy(changed))
        if not changed:
            return None
        return PanelUpdate(self.subscription, changed, reason)


class SnapshotLoader:
    def __init__(self, client: ApiClient, viewer_id: str):
        self.client = client
        self.viewer_id = viewer_id

    @staticmethod
    def targets(subscription: Subscription) -> set[str]:
        if subscription.kind == "lobbies":
            return {"lobbies"}
        if subscription.kind == "lobby":
            return {"lobby", "chat"}
        return {"game"}

    @staticmethod
    def event_targets(subscription: Subscription, name: str) -> set[str]:
        if name in {"hand_signal", "hand_signals"}:
            return set()
        if subscription.kind == "lobby":
            if name == "lobby_chat":
                return {"chat"}
            if name in {
                "lobby_created", "player_joined", "player_left", "ready_changed",
                "settings_changed", "game_started", "game_finished", "lobby_closed",
            }:
                return {"lobby"}
        return SnapshotLoader.targets(subscription)

    @staticmethod
    def _object(payload: Any, identity: str | None) -> dict[str, Any]:
        if not isinstance(payload, dict) or payload.get("id") != identity:
            raise ProtocolError("The API returned a snapshot for another resource or an invalid format.")
        return payload

    @staticmethod
    def _rows(payload: Any) -> list[dict[str, Any]]:
        if (
            not isinstance(payload, list)
            or not all(isinstance(row, dict) and isinstance(row.get("id"), str) for row in payload)
            or len({row["id"] for row in payload}) != len(payload)
        ):
            raise ProtocolError("The API returned an invalid object list.")
        return payload

    async def load(self, subscription: Subscription, targets: set[str]) -> dict[str, Any]:
        panels = {}
        if "lobbies" in targets:
            panels["lobbies"] = self._rows(await self.client.request("GET", "/api/fools/lobbies/"))
        if "lobby" in targets:
            lobby = self._object(
                await self.client.request("GET", f"/api/fools/lobbies/{subscription.resource_id}/"),
                subscription.resource_id,
            )
            if not isinstance(lobby.get("players"), list) or not isinstance(lobby.get("settings"), dict):
                raise ProtocolError("The lobby snapshot is missing players or settings.")
            panels.update({
                "lobby": {key: value for key, value in lobby.items() if key not in {"players", "settings"}},
                "players": lobby["players"], "settings": lobby["settings"],
            })
        if "chat" in targets:
            panels["chat"] = self._rows(await self.client.request(
                "GET", f"/api/fools/lobbies/{subscription.resource_id}/messages/",
            ))
        if "game" in targets:
            game = self._object(
                await self.client.request("GET", f"/api/fools/games/{subscription.resource_id}/"),
                subscription.resource_id,
            )
            panels.update(self._game_panels(game))
        return panels

    def _game_panels(self, game: dict[str, Any]) -> dict[str, Any]:
        if (
            not isinstance(game.get("players"), list)
            or not isinstance(game.get("runtime"), dict)
            or not isinstance(game.get("table"), list)
            or not isinstance(game.get("public_events"), list)
            or not all(isinstance(player, dict) for player in game["players"])
        ):
            raise ProtocolError("The API returned an incomplete game snapshot.")
        viewers = [player for player in game["players"] if player.get("user_id") == self.viewer_id]
        if len(viewers) != 1 or not isinstance(viewers[0].get("hand"), list):
            raise ProtocolError("The game snapshot does not contain the current user's hand.")
        deck_fields = {"trump_card", "trump_in_deck", "deck_remaining", "discard_count", "discard_top"}
        excluded = deck_fields | {"players", "runtime", "table", "public_events"}
        return {
            "game": {key: value for key, value in game.items() if key not in excluded},
            "players": [
                {key: value for key, value in player.items() if key != "hand"}
                for player in game["players"]
            ],
            "hand": viewers[0]["hand"], "table": game["table"], "turn": game["runtime"],
            "deck": {key: game.get(key) for key in sorted(deck_fields)},
            "events": game["public_events"],
        }


class LiveSession:
    def __init__(
        self, client: ApiClient, viewer_id: str, *, feed: SocketFeed | None = None,
        debounce: float = 0.05, reconcile_interval: float = 30.0,
    ):
        if debounce < 0 or not reconcile_interval > 0:
            raise ConfigurationError("Invalid synchronization intervals.")
        self.feed = feed or SocketFeed(client)
        self.loader = SnapshotLoader(client, viewer_id)
        self.debounce = debounce
        self.reconcile_interval = reconcile_interval

    async def watch(self, subscription: Subscription) -> AsyncIterator[PanelUpdate]:
        queue: asyncio.Queue[tuple[int, object]] = asyncio.Queue(maxsize=64)
        generation = 0
        connected = False
        connection_changed = asyncio.Event()
        panels = _Panels(subscription)
        loop = asyncio.get_running_loop()
        reconcile_at = loop.time() + self.reconcile_interval

        async def pump() -> None:
            nonlocal generation, connected
            try:
                async with aclosing(self.feed.events(subscription)) as stream:
                    async for message in stream:
                        if isinstance(message, ConnectionNotice):
                            connected = message.state == "connected"
                            if connected:
                                generation += 1
                            connection_changed.set()
                        await queue.put((generation, message))
            except Exception as error:
                connected = False
                connection_changed.set()
                await queue.put((generation, error))
            else:
                await queue.put((generation, ProtocolError("The WebSocket event stream ended.")))
            finally:
                connected = False
                connection_changed.set()

        def current(identity: int) -> bool:
            return connected and identity == generation

        async def load_current(identity: int, targets: set[str]) -> dict[str, Any] | None:
            connection_changed.clear()
            if not current(identity):
                return None
            loading = asyncio.create_task(self.loader.load(subscription, targets))
            changing = asyncio.create_task(connection_changed.wait())
            try:
                await asyncio.wait({loading, changing}, return_when=asyncio.FIRST_COMPLETED)
                if changing.done() or not current(identity):
                    return None
                return await loading
            finally:
                loading.cancel()
                changing.cancel()
                await asyncio.gather(loading, changing, return_exceptions=True)

        async def refresh(
            identity: int, targets: set[str], reason: str,
        ) -> AsyncIterator[PanelUpdate]:
            failures = 0
            while current(identity):
                try:
                    values = await load_current(identity, targets)
                except ApiError as error:
                    if error.authentication_required:
                        raise AuthenticationRequired("The session has expired. Please sign in again.") from None
                    if error.status_code not in {408, 425, 429} and not 500 <= error.status_code <= 599:
                        raise
                except NetworkError:
                    pass
                else:
                    if values is None:
                        return
                    if current(identity):
                        values["connection"] = {"state": "live", "stale": False}
                        update = panels.update(values, reason)
                        if update is not None:
                            yield update
                    return
                if not current(identity):
                    return
                delay = self.feed.policy.delay(failures)
                failures += 1
                update = panels.update({"connection": {
                    "state": "snapshot_retry", "stale": True, "retry_in": delay,
                }}, "snapshot_failed")
                if update is not None:
                    yield update
                connection_changed.clear()
                if not current(identity):
                    return
                try:
                    await asyncio.wait_for(connection_changed.wait(), delay)
                except TimeoutError:
                    pass

        task = asyncio.create_task(pump(), name=f"fools-{subscription.kind}-socket")
        pending = None
        try:
            while True:
                if connected and loop.time() >= reconcile_at:
                    async with aclosing(refresh(
                        generation, self.loader.targets(subscription), "reconcile",
                    )) as updates:
                        async for update in updates:
                            yield update
                    reconcile_at = loop.time() + self.reconcile_interval
                    continue
                if pending is not None:
                    identity, message = pending
                    pending = None
                else:
                    timeout = max(0.001, reconcile_at - loop.time()) if connected else None
                    try:
                        identity, message = await asyncio.wait_for(queue.get(), timeout)
                    except TimeoutError:
                        continue
                if isinstance(message, Exception):
                    raise message
                if identity != generation:
                    continue
                if isinstance(message, ConnectionNotice):
                    state = "synchronizing" if message.state == "connected" else message.state
                    update = panels.update({"connection": {
                        "state": state, "stale": True, "attempt": message.attempt,
                        "retry_in": message.retry_in,
                    }}, "connection")
                    if update is not None:
                        yield update
                    if message.state == "connected":
                        async with aclosing(refresh(
                            identity, self.loader.targets(subscription), "snapshot",
                        )) as updates:
                            async for update in updates:
                                yield update
                        reconcile_at = loop.time() + self.reconcile_interval
                    continue
                if not isinstance(message, ServerEvent) or not current(identity):
                    continue
                names = {message.name}
                targets = self.loader.event_targets(subscription, message.name)
                if not targets:
                    continue
                await asyncio.sleep(self.debounce)
                while not queue.empty():
                    queued = queue.get_nowait()
                    queued_identity, queued_message = queued
                    if queued_identity != identity or not isinstance(queued_message, ServerEvent):
                        pending = queued
                        break
                    names.add(queued_message.name)
                    targets.update(self.loader.event_targets(subscription, queued_message.name))
                async with aclosing(refresh(identity, targets, ",".join(sorted(names)))) as updates:
                    async for update in updates:
                        yield update
        except ClientError:
            update = panels.update({"connection": {"state": "failed", "stale": True}}, "connection")
            if update is not None:
                yield update
            raise
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
