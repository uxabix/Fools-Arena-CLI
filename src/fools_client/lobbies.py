"""Lobby commands delegated to the existing server API, without local rules."""

from typing import Any

from fools_client.api import ApiClient
from fools_client.errors import ProtocolError
from fools_client.realtime import Subscription


class LobbyService:
    def __init__(self, client: ApiClient):
        self.client = client

    @staticmethod
    def _path(lobby_id: str, action: str = "") -> str:
        identity = Subscription("lobby", lobby_id).resource_id
        suffix = f"{action}/" if action else ""
        return f"/api/fools/lobbies/{identity}/{suffix}"

    @staticmethod
    def _snapshot(payload: Any) -> dict[str, Any]:
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("id"), str)
            or not isinstance(payload.get("players"), list)
            or not isinstance(payload.get("settings"), dict)
        ):
            raise ProtocolError("The server returned an invalid lobby snapshot.")
        Subscription("lobby", payload["id"])
        return payload

    async def create(
        self, name: str, *, is_private: bool, password: str, settings: dict[str, Any],
    ) -> dict[str, Any]:
        return self._snapshot(await self.client.request("POST", "/api/fools/lobbies/", data={
            **settings, "name": name, "is_private": is_private, "password": password,
        }))

    async def join(self, lobby_id: str, password: str = "") -> dict[str, Any]:
        return self._snapshot(await self.client.request(
            "POST", self._path(lobby_id, "join"), data={"password": password},
        ))

    async def leave(self, lobby_id: str) -> None:
        payload = await self.client.request("POST", self._path(lobby_id, "leave"), data={})
        if not isinstance(payload, dict) or not isinstance(payload.get("detail"), str):
            raise ProtocolError("The server returned an invalid lobby-leave confirmation.")

    async def ready(self, lobby_id: str, ready: bool) -> dict[str, Any]:
        return self._snapshot(await self.client.request(
            "POST", self._path(lobby_id, "ready"), data={"ready": ready},
        ))

    async def update_settings(self, lobby_id: str, settings: dict[str, Any]) -> dict[str, Any]:
        return self._snapshot(await self.client.request(
            "PATCH", self._path(lobby_id, "settings"), data=settings,
        ))

    async def start(self, lobby_id: str) -> str:
        payload = await self.client.request("POST", self._path(lobby_id, "start"), data={})
        if not isinstance(payload, dict) or not isinstance(payload.get("id"), str):
            raise ProtocolError("The server returned an invalid game-start confirmation.")
        Subscription("game", payload["id"])
        return payload["id"]
