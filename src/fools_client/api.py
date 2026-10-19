"""Async JSON transport using the existing Django session and CSRF contract."""

import asyncio
from http.cookies import CookieError, SimpleCookie
from importlib.metadata import version
from pathlib import Path
from typing import Any, Self

import httpx

from fools_client.config import default_state_directory, normalize_server
from fools_client.errors import ApiError, ConfigurationError, NetworkError, ProtocolError
from fools_client.session import SessionStore

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class ApiClient:
    def __init__(
        self,
        server: str,
        *,
        state_directory: Path | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.server = normalize_server(server)
        self._store = SessionStore(
            state_directory if state_directory is not None else default_state_directory(),
            self.server,
        )
        self._http = httpx.AsyncClient(
            base_url=self.server,
            headers={
                "Accept": "application/json",
                "User-Agent": f"CLI-Fools-Client/{version('cli-fools-client')}",
            },
            timeout=httpx.Timeout(15, connect=5),
            follow_redirects=False,
            transport=transport,
        )
        self._lock = asyncio.Lock()
        self._saved_cookies: list[dict[str, Any]] = []

    async def __aenter__(self) -> Self:
        cookies = await asyncio.to_thread(self._store.load)
        self._http.cookies.update(cookies)
        self._saved_cookies = self._store.snapshot(self._http.cookies.jar)
        await self._http.__aenter__()
        return self

    async def __aexit__(self, *arguments: Any) -> None:
        await self._http.__aexit__(*arguments)

    def _url(self, path: str) -> httpx.URL:
        if not path.startswith("/") or path.startswith("//") or chr(92) in path:
            raise ConfigurationError("Only paths on the configured server are allowed.")
        try:
            target = httpx.URL(self.server + path)
        except (httpx.InvalidURL, ValueError):
            raise ConfigurationError("Invalid request path.") from None
        if target.fragment or target.userinfo:
            raise ConfigurationError("Invalid request path.")
        return target

    def cookie_header(self, path: str) -> str:
        request = self._http.build_request("GET", self._url(path))
        return request.headers.get("Cookie", "")

    def _cookie(self, name: str, path: str) -> str | None:
        cookies = SimpleCookie()
        try:
            cookies.load(self.cookie_header(path))
        except CookieError:
            return None
        value = cookies.get(name)
        return value.value if value is not None else None

    @property
    def has_session(self) -> bool:
        return bool(self._cookie("sessionid", "/api/accounts/auth/profile/"))

    async def _persist(self) -> None:
        snapshot = self._store.snapshot(self._http.cookies.jar)
        if snapshot != self._saved_cookies:
            await asyncio.to_thread(self._store.save, self._http.cookies.jar)
            self._saved_cookies = snapshot

    async def _send(self, method: str, path: str, **arguments: Any) -> httpx.Response:
        try:
            response = await self._http.request(method, self._url(path), **arguments)
        except httpx.TimeoutException:
            raise NetworkError(command_sent=method not in SAFE_METHODS, timed_out=True) from None
        except httpx.RequestError:
            raise NetworkError(command_sent=method not in SAFE_METHODS) from None
        await self._persist()
        return response

    async def _csrf_token(self, path: str) -> str:
        token = self._cookie("csrftoken", path)
        if token is None:
            page = "/accounts/login/"
            for attempt in range(6):
                response = await self._send("GET", page)
                if not response.is_redirect:
                    if not response.is_success:
                        raise ProtocolError("Could not obtain a CSRF cookie from the login page.")
                    break
                location = response.headers.get("Location")
                if not location:
                    raise ProtocolError("The server returned a redirect without a destination.")
                destination = response.url.join(location)
                if (
                    normalize_server(str(destination.copy_with(path="/", query=None, fragment=None)))
                    != self.server
                    or destination.userinfo
                    or destination.fragment
                ):
                    raise ProtocolError(
                        "The login page redirects to another server. "
                        "Set the final server address explicitly with --server."
                    )
                page = destination.raw_path.decode("ascii")
            else:
                raise ProtocolError("Too many redirects while obtaining a CSRF cookie.")
            token = self._cookie("csrftoken", path)
        if token is None or len(token) not in {32, 64} or not token.isascii() or not token.isalnum():
            raise ProtocolError("The server did not issue a valid csrftoken cookie.")
        return token

    async def request(self, method: str, path: str, *, data: Any = None) -> Any:
        method = method.upper()
        if not path.startswith("/api/"):
            raise ConfigurationError("JSON requests must use an existing /api/ route.")
        async with self._lock:
            headers = {}
            if method not in SAFE_METHODS:
                headers = {
                    "X-CSRFToken": await self._csrf_token(path),
                    "Origin": self.server,
                    "Referer": self.server + "/",
                }
            response = await self._send(method, path, json=data, headers=headers)
            if response.is_redirect:
                raise ProtocolError(
                    "The API returned a redirect. Check the final --server address; "
                    "commands are never retried automatically."
                )
            if not response.is_success:
                try:
                    payload = response.json()
                except (ValueError, RecursionError):
                    payload = {"detail": "The server returned an error that was not JSON."}
                error = ApiError(response.status_code, payload)
                if error.authentication_required:
                    await self._forget_session()
                raise error
            if response.status_code == 204 or method == "HEAD":
                return None
            try:
                return response.json()
            except (ValueError, RecursionError):
                raise ProtocolError("The API returned invalid JSON instead of the expected snapshot.") from None

    async def _forget_session(self) -> None:
        self._http.cookies.clear()
        await asyncio.to_thread(self._store.clear)
        self._saved_cookies = []

    async def forget_session(self) -> None:
        async with self._lock:
            await self._forget_session()
