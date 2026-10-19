"""Server identity and platform-specific client configuration."""

import os
import sys
from pathlib import Path

import httpx

from fools_client.errors import ConfigurationError


def normalize_server(value: str) -> str:
    try:
        address = httpx.URL(value)
    except (httpx.InvalidURL, ValueError):
        raise ConfigurationError("Invalid server address.") from None
    if (
        address.scheme not in {"http", "https"}
        or not address.host
        or address.userinfo
        or address.path != "/"
        or address.query
        or address.fragment
        or any(character.isspace() for character in value)
        or "?" in value
        or "#" in value
    ):
        raise ConfigurationError(
            "Provide the server root HTTP(S) URL without credentials, path, or query parameters."
        )
    if address.port == {"http": 80, "https": 443}[address.scheme]:
        address = address.copy_with(port=None)
    return str(address).rstrip("/")


def default_state_directory() -> Path:
    if sys.platform == "win32":
        root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return root / "FoolsArena"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "FoolsArena"
    root = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    if not root.is_absolute():
        root = Path.home() / ".local" / "state"
    return root / "fools-arena"
