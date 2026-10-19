"""Authenticated, receive-only WebSocket feeds for the existing server."""

import asyncio
import json
import logging
import random
import ssl
from collections.abc import AsyncIterator
from dataclasses import dataclass
from importlib.metadata import version
from typing import Any
from uuid import UUID

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus

from fools_client.api import ApiClient
from fools_client.auth import AuthService
from fools_client.errors import (
    ApiError,
    AuthenticationRequired,
    ConfigurationError,
    NetworkError,
    ProtocolError,
    SubscriptionDenied,
)


@dataclass(frozen=True)
class Subscription:
    kind: str
    resource_id: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in {"lobbies", "lobby", "game"}:
            raise ConfigurationError("Unknown WebSocket subscription type.")
        if self.kind == "lobbies":
            if self.resource_id is not None:
                raise ConfigurationError("The lobby list subscription does not accept an identifier.")
        else:
            try:
                identity = str(UUID(str(self.resource_id)))
            except ValueError:
                raise ConfigurationError("A lobby or game subscription requires a UUID.") from None
            object.__setattr__(self, "resource_id", identity)

    @property
    def path(self) -> str:
        if self.kind == "lobbies":
            return "/ws/lobbies/"
        collection = "lobbies" if self.kind == "lobby" else "games"
        return f"/ws/{collection}/{self.resource_id}/"


@dataclass(frozen=True)
class ServerEvent:
    name: str
    payload: dict[str, Any]

    @classmethod
    def parse(cls, message: str | bytes) -> "ServerEvent":
        if not isinstance(message, str):
            raise ProtocolError("The WebSocket returned binary data instead of JSON text.")
        try:
            document = json.loads(message)
        except (ValueError, RecursionError):
            raise ProtocolError("The WebSocket returned invalid JSON.") from None
        if (
            not isinstance(document, dict)
            or not isinstance(document.get("event"), str)
            or not 0 < len(document["event"]) <= 100
            or not isinstance(document.get("payload"), dict)
        ):
            raise ProtocolError("Unknown WebSocket event format.")
        return cls(document["event"], document["payload"])


@dataclass(frozen=True)
class ConnectionNotice:
    state: str
    attempt: int
    retry_in: float | None = None


@dataclass(frozen=True)
class ReconnectPolicy:
    initial_delay: float = 0.5
    maximum_delay: float = 30.0
    stable_after: float = 10.0
    jitter: float = 0.2

    def __post_init__(self) -> None:
        if (
            not 0 < self.initial_delay <= self.maximum_delay
            or not self.stable_after > 0
            or not 0 <= self.jitter <= 1
        ):
            raise ConfigurationError("Invalid reconnection parameters.")

    def delay(self, failures: int) -> float:
        base = min(self.maximum_delay, self.initial_delay * 2 ** min(failures, 16))
        return min(self.maximum_delay, base * random.uniform(1 - self.jitter, 1 + self.jitter))


class _DirectConnect(connect):
    def process_redirect(self, exception: Exception) -> Exception:
        return exception


class SocketFeed:
    def __init__(self, client: ApiClient, *, policy: ReconnectPolicy | None = None):
        self.client = client
        self.policy = policy or ReconnectPolicy()
        self._logger = logging.Logger("fools_client.websocket", level=logging.CRITICAL + 1)

    async def _check_close(self, code: int | None) -> None:
        if code == 4401:
            await self.client.forget_session()
            raise AuthenticationRequired("The WebSocket session has expired. Please sign in again.")
        if code in {4403, 1008}:
            raise SubscriptionDenied("The server denied access to this WebSocket subscription.")
        if code in {1002, 1003, 1007, 1009, 1010}:
            raise ProtocolError("The WebSocket closed because the message format or size is unsupported.")

    async def _check_handshake(self, error: InvalidStatus) -> None:
        status = error.response.status_code
        if status == 401:
            await self.client.forget_session()
            raise AuthenticationRequired("The server requires authentication for WebSocket. Please sign in.")
        if status == 403:
            try:
                await AuthService(self.client).profile()
            except NetworkError:
                return
            except ApiError as failure:
                if failure.status_code in {408, 425, 429} or 500 <= failure.status_code <= 599:
                    return
                raise
            raise SubscriptionDenied("You do not have access to this lobby or game WebSocket.")
        if status == 404:
            raise SubscriptionDenied("The WebSocket channel was not found on the server.")
        if status in {408, 425, 429} or 500 <= status <= 599:
            return
        if 300 <= status <= 399:
            raise ProtocolError(
                "The WebSocket returned a redirect. Set the final address with --server."
            )
        raise ProtocolError(f"The server rejected the WebSocket connection: HTTP {status}.")

    async def events(
        self, subscription: Subscription,
    ) -> AsyncIterator[ConnectionNotice | ServerEvent]:
        address = httpx.URL(self.client.server + subscription.path)
        uri = str(address.copy_with(scheme="wss" if address.scheme == "https" else "ws"))
        failures = 0
        attempt = 0
        loop = asyncio.get_running_loop()
        while True:
            attempt += 1
            started = None
            yield ConnectionNotice("connecting", attempt)
            headers = {}
            cookie = self.client.cookie_header(subscription.path)
            if cookie:
                headers["Cookie"] = cookie
            try:
                async with _DirectConnect(
                    uri, additional_headers=headers, origin=self.client.server,
                    user_agent_header=f"CLI-Fools-Client/{version('cli-fools-client')}",
                    open_timeout=10, close_timeout=3, ping_interval=20, ping_timeout=20,
                    max_size=1048576, max_queue=16, compression=None, logger=self._logger,
                ) as connection:
                    started = loop.time()
                    yield ConnectionNotice("connected", attempt)
                    async for message in connection:
                        event = ServerEvent.parse(message)
                        if event.name not in {"hand_signal", "hand_signals"}:
                            yield event
                    await self._check_close(connection.close_code)
            except InvalidStatus as error:
                await self._check_handshake(error)
            except ConnectionClosed as error:
                closing = error.rcvd or error.sent
                await self._check_close(closing.code if closing else None)
            except ssl.SSLCertVerificationError:
                raise ProtocolError("Could not verify the WebSocket server's TLS certificate.") from None
            except (OSError, TimeoutError):
                pass
            except InvalidHandshake:
                raise ProtocolError("The server did not complete a valid WebSocket handshake.") from None
            if started is not None and loop.time() - started >= self.policy.stable_after:
                failures = 0
            delay = self.policy.delay(failures)
            failures += 1
            yield ConnectionNotice("disconnected", attempt)
            yield ConnectionNotice("retrying", attempt, delay)
            await asyncio.sleep(delay)
