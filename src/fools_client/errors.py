"""User-facing failures, independent of the terminal interface."""

import unicodedata
from typing import Any


def display_text(value: object) -> str:
    return "".join(
        character
        for character in str(value)
        if not unicodedata.category(character).startswith("C")
    )[:1000]


class ClientError(Exception):
    """A failure that can be shown without a traceback."""


class ConfigurationError(ClientError):
    pass


class SessionStorageError(ClientError):
    pass


class ProtocolError(ClientError):
    pass


class AuthenticationRequired(ClientError):
    pass


class SubscriptionDenied(ClientError):
    pass


class NetworkError(ClientError):
    def __init__(self, *, command_sent: bool, timed_out: bool = False):
        self.command_sent = command_sent
        message = "The server timed out." if timed_out else "Could not connect to the server."
        if command_sent:
            message += (
                " The command outcome is unknown; it may have succeeded."
                " Check the server state before retrying."
            )
        super().__init__(message)


class ApiError(ClientError):
    def __init__(self, status_code: int, data: Any):
        self.status_code = status_code
        self.data = data
        self.code = data.get("code") if isinstance(data, dict) else None
        messages = self._messages(data)
        detail = "; ".join(messages)[:2000] or "The server rejected the request."
        super().__init__(f"HTTP {status_code}: {detail}")

    @property
    def authentication_required(self) -> bool:
        return self.status_code == 401 or (
            self.status_code == 403
            and isinstance(self.data, dict)
            and self.data.get("detail") == "Authentication credentials were not provided."
        )

    @classmethod
    def _messages(cls, data: Any, prefix: str = "", depth: int = 0) -> list[str]:
        if depth > 5:
            return []
        if isinstance(data, dict):
            messages = []
            for field, value in list(data.items())[:20]:
                label = "" if field in {"detail", "non_field_errors"} else display_text(field)
                if field == "code":
                    continue
                field_prefix = ".".join(part for part in (prefix, label) if part)
                messages.extend(cls._messages(value, field_prefix, depth + 1))
            return messages
        if isinstance(data, list):
            return [
                message
                for value in data[:20]
                for message in cls._messages(value, prefix, depth + 1)
            ]
        if isinstance(data, str):
            return [f"{prefix}: {display_text(data)}" if prefix else display_text(data)]
        return []
