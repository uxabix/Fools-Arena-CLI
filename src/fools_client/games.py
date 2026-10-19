"""Game and lobby-chat commands backed by the existing JSON API."""

from typing import Any

from fools_client.api import ApiClient
from fools_client.errors import ProtocolError
from fools_client.realtime import Subscription


class GameService:
    def __init__(self, client: ApiClient):
        self.client = client

    @staticmethod
    def _path(game_id: str, action: str = "") -> str:
        identity = Subscription("game", game_id).resource_id
        suffix = f"{action}/" if action else ""
        return f"/api/fools/games/{identity}/{suffix}"

    async def _command(self, game_id: str, action: str, data: dict[str, Any] | None = None) -> None:
        identity = Subscription("game", game_id).resource_id
        payload = await self.client.request("POST", self._path(game_id, action), data=data or {})
        if not isinstance(payload, dict) or payload.get("id") != identity:
            raise ProtocolError("The server returned an invalid game snapshot.")

    async def attack(self, game_id: str, card_ids: list[str]) -> None:
        await self._command(game_id, "attack", {"card_ids": card_ids})

    async def defend(self, game_id: str, table_card_id: str, card_id: str) -> None:
        await self._command(game_id, "defend", {"table_card_id": table_card_id, "card_id": card_id})

    async def transfer(self, game_id: str, card_ids: list[str]) -> None:
        await self._command(game_id, "transfer", {"card_ids": card_ids})

    async def seal(self, game_id: str) -> None:
        await self._command(game_id, "seal")

    async def take(self, game_id: str) -> None:
        await self._command(game_id, "take")

    async def pass_turn(self, game_id: str) -> None:
        await self._command(game_id, "pass")

    async def bito(self, game_id: str) -> None:
        await self._command(game_id, "bito")

    async def leave(self, game_id: str) -> None:
        await self._command(game_id, "leave")

    async def rematch(self, game_id: str) -> str:
        payload = await self.client.request("POST", self._path(game_id, "again"), data={})
        if not isinstance(payload, dict) or not isinstance(payload.get("id"), str):
            raise ProtocolError("The server returned an invalid rematch confirmation.")
        return Subscription("game", payload["id"]).resource_id


class LobbyChatService:
    def __init__(self, client: ApiClient):
        self.client = client

    @staticmethod
    def _path(lobby_id: str) -> str:
        identity = Subscription("lobby", lobby_id).resource_id
        return f"/api/fools/lobbies/{identity}/messages/"

    async def send(self, lobby_id: str, content: str) -> dict[str, Any]:
        payload = await self.client.request("POST", self._path(lobby_id), data={"content": content})
        if not isinstance(payload, dict) or not isinstance(payload.get("id"), str):
            raise ProtocolError("The server returned an invalid chat message.")
        return payload
