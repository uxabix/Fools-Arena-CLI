"""Authentication commands; account policy stays on the Django server."""

from dataclasses import dataclass
from typing import Any

from fools_client.api import ApiClient
from fools_client.errors import ApiError, AuthenticationRequired, ClientError, ProtocolError


@dataclass(frozen=True)
class UserProfile:
    user_id: str
    username: str
    email: str

    @classmethod
    def from_payload(cls, payload: Any) -> "UserProfile":
        if (
            not isinstance(payload, dict)
            or not all(isinstance(payload.get(field), str) for field in ("id", "username", "email"))
            or not payload["id"]
            or not payload["username"]
        ):
            raise ProtocolError("The server returned an invalid user profile.")
        return cls(payload["id"], payload["username"], payload["email"])


class AuthService:
    def __init__(self, client: ApiClient):
        self.client = client

    async def register(self, username: str, email: str, password: str) -> str:
        if self.client.has_session:
            try:
                await self.profile()
            except AuthenticationRequired:
                pass
            else:
                raise ClientError("Log out before registering another account.")
        payload = await self.client.request(
            "POST", "/api/accounts/auth/register/",
            data={"username": username, "email": email, "password": password},
        )
        return self._notice(payload)

    async def login(self, username: str, password: str) -> UserProfile:
        payload = await self.client.request(
            "POST", "/api/accounts/auth/login/",
            data={"username": username, "password": password},
        )
        profile = UserProfile.from_payload(payload)
        if not self.client.has_session:
            raise ProtocolError("The server returned a profile but did not issue a sessionid cookie.")
        return profile

    async def profile(self) -> UserProfile:
        try:
            payload = await self.client.request("GET", "/api/accounts/auth/profile/")
        except ApiError as error:
            if error.authentication_required:
                raise AuthenticationRequired("The session is missing or expired. Please sign in.") from None
            raise
        return UserProfile.from_payload(payload)

    async def logout(self) -> str:
        try:
            payload = await self.client.request("POST", "/api/accounts/auth/logout/")
        except ApiError as error:
            if error.authentication_required:
                return "The server session has already ended. Local cookies were removed."
            raise
        notice = self._notice(payload)
        await self.client.forget_session()
        return notice

    @staticmethod
    def _notice(payload: Any) -> str:
        if not isinstance(payload, dict) or not isinstance(payload.get("detail"), str):
            raise ProtocolError("The server returned an invalid notification.")
        return payload["detail"]
