"""Origin-bound cookie persistence; no credentials or game state are stored."""

import ctypes
import hashlib
import json
import os
import tempfile
from contextlib import suppress
from http.cookiejar import Cookie, CookieJar
from pathlib import Path
from typing import Any

from fools_client.config import normalize_server
from fools_client.errors import SessionStorageError

COOKIE_NAMES = frozenset({"sessionid", "csrftoken"})
MAX_SESSION_BYTES = 65536


def _windows_crypt(data: bytes, server: str, *, decrypt: bool) -> bytes:
    from ctypes import wintypes

    class DataBlob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    function = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    function.argtypes = [
        ctypes.POINTER(DataBlob),
        ctypes.c_void_p,
        ctypes.POINTER(DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(DataBlob),
    ]
    function.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    input_buffer = ctypes.create_string_buffer(data)
    entropy = server.encode("utf-8")
    entropy_buffer = ctypes.create_string_buffer(entropy)
    input_blob = DataBlob(len(data), ctypes.cast(input_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    entropy_blob = DataBlob(
        len(entropy), ctypes.cast(entropy_buffer, ctypes.POINTER(ctypes.c_ubyte))
    )
    output_blob = DataBlob()
    if not function(
        ctypes.byref(input_blob), None, ctypes.byref(entropy_blob), None, None, 1,
        ctypes.byref(output_blob),
    ):
        raise OSError("Windows DPAPI could not protect or restore the session.")
    try:
        return ctypes.string_at(output_blob.data, output_blob.size)
    finally:
        kernel32.LocalFree(output_blob.data)


class SessionStore:
    def __init__(self, directory: Path, server: str):
        self.server = normalize_server(server)
        self.directory = directory.expanduser().absolute() / "sessions"
        identity = hashlib.sha256(self.server.encode("utf-8")).hexdigest()
        self.path = self.directory / f"{identity}.session"

    @staticmethod
    def snapshot(cookies: CookieJar) -> list[dict[str, Any]]:
        return [
            {
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain,
                "domain_specified": cookie.domain_specified,
                "path": cookie.path,
                "path_specified": cookie.path_specified,
                "secure": cookie.secure,
                "expires": cookie.expires,
                "discard": cookie.discard,
            }
            for cookie in cookies
            if cookie.name in COOKIE_NAMES and not cookie.is_expired()
        ]

    def _check_paths(self) -> None:
        for path in (self.directory, self.path):
            if path.is_symlink() or (os.name == "nt" and path.is_junction()):
                raise OSError("Session paths must not be links.")

    def load(self) -> CookieJar:
        cookies = CookieJar()
        try:
            self._check_paths()
            try:
                with self.path.open("rb") as stream:
                    encoded = stream.read(MAX_SESSION_BYTES + 1)
            except FileNotFoundError:
                return cookies
            if len(encoded) > MAX_SESSION_BYTES:
                raise ValueError("Session file is too large.")
            if os.name == "nt":
                prefix = b"FASESSION1-DPAPI\n"
                if not encoded.startswith(prefix):
                    raise ValueError("Unsupported session format.")
                raw = _windows_crypt(encoded[len(prefix):], self.server, decrypt=True)
            else:
                os.chmod(self.directory, 0o700)
                os.chmod(self.path, 0o600)
                prefix = b"FASESSION1-JSON\n"
                if not encoded.startswith(prefix):
                    raise ValueError("Unsupported session format.")
                raw = encoded[len(prefix):]
            document = json.loads(raw)
            if (
                not isinstance(document, dict)
                or document.get("version") != 1
                or document.get("server") != self.server
                or not isinstance(document.get("cookies"), list)
                or len(document["cookies"]) > 32
            ):
                raise ValueError("Invalid session document.")
            for record in document["cookies"]:
                cookie = self._restore_cookie(record)
                if not cookie.is_expired():
                    cookies.set_cookie(cookie)
            return cookies
        except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
            raise SessionStorageError(
                "Could not read the local session. The file may be damaged, inaccessible, "
                "or created for another OS user. Use a different --state-dir "
                "or delete the session file and sign in again."
            ) from None

    @staticmethod
    def _restore_cookie(record: Any) -> Cookie:
        if not isinstance(record, dict) or record.get("name") not in COOKIE_NAMES:
            raise ValueError("Invalid cookie.")
        for field in ("name", "value", "domain", "path"):
            value = record[field]
            if (
                not isinstance(value, str)
                or not value
                or len(value) > 8192
                or any(ord(character) < 32 or ord(character) == 127 for character in value)
            ):
                raise ValueError("Invalid cookie field.")
        for field in ("domain_specified", "path_specified", "secure", "discard"):
            if not isinstance(record[field], bool):
                raise ValueError("Invalid cookie flag.")
        if record["expires"] is not None and type(record["expires"]) is not int:
            raise ValueError("Invalid cookie expiry.")
        return Cookie(
            version=0, name=record["name"], value=record["value"],
            port=None, port_specified=False,
            domain=record["domain"], domain_specified=record["domain_specified"],
            domain_initial_dot=record["domain"].startswith("."),
            path=record["path"], path_specified=record["path_specified"],
            secure=record["secure"], expires=record["expires"], discard=record["discard"],
            comment=None, comment_url=None, rest={}, rfc2109=False,
        )

    def save(self, cookies: CookieJar) -> None:
        records = self.snapshot(cookies)
        if not records:
            self.clear()
            return
        temporary_path = None
        try:
            for record in records:
                self._restore_cookie(record)
            raw = json.dumps({
                "version": 1, "server": self.server, "cookies": records,
            }).encode("utf-8")
            if os.name == "nt":
                encoded = b"FASESSION1-DPAPI\n" + _windows_crypt(raw, self.server, decrypt=False)
            else:
                encoded = b"FASESSION1-JSON\n" + raw
            if len(encoded) > MAX_SESSION_BYTES:
                raise ValueError("Session file is too large.")
            self._check_paths()
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            if os.name != "nt":
                os.chmod(self.directory, 0o700)
            descriptor, filename = tempfile.mkstemp(prefix=".session-", dir=self.directory)
            temporary_path = Path(filename)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, self.path)
        except (OSError, ValueError, TypeError):
            raise SessionStorageError(
                "Could not save the session. If the server already processed the request, "
                "its result has not been rolled back. Check access to --state-dir."
            ) from None
        finally:
            if temporary_path is not None:
                with suppress(OSError):
                    temporary_path.unlink(missing_ok=True)

    def clear(self) -> None:
        try:
            self._check_paths()
            self.path.unlink(missing_ok=True)
        except OSError:
            raise SessionStorageError("Could not delete the local session file.") from None
