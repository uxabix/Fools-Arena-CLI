"""Offline contract tests for the standalone client."""

import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx

from fools_client.api import ApiClient
from fools_client.cli import _run
from fools_client.errors import ApiError, ProtocolError
from fools_client.games import GameService, LobbyChatService
from fools_client.live import SnapshotLoader
from fools_client.realtime import Subscription
from fools_client.ui.app import FoolsApp
from textual.app import App


GAME_ID = "11111111-1111-4111-8111-111111111111"
LOBBY_ID = "22222222-2222-4222-8222-222222222222"
USER_ID = "33333333-3333-4333-8333-333333333333"
CARD_ID = "44444444-4444-4444-8444-444444444444"
HAND_ID = "55555555-5555-4555-8555-555555555555"
TABLE_ID = "66666666-6666-4666-8666-666666666666"


class RecordingClient:
    def __init__(self):
        self.calls: list[tuple[str, str, Any]] = []
        self.response: dict[str, Any] = {"id": GAME_ID}

    async def request(self, method: str, path: str, *, data: Any = None) -> Any:
        self.calls.append((method, path, data))
        if path.endswith("/again/"):
            return {"id": "77777777-7777-4777-8777-777777777777"}
        if path.endswith("/messages/"):
            return {"id": "message-id", "content": data["content"]}
        return self.response


def run_immediate(coroutine):
    """Run a fake-backed coroutine that must not perform asynchronous I/O."""
    try:
        coroutine.send(None)
    except StopIteration as complete:
        return complete.value
    raise AssertionError("The fake-backed service unexpectedly suspended.")


class GameServiceTests(unittest.TestCase):
    def test_commands_use_existing_routes_and_payloads(self):
        client = RecordingClient()
        service = GameService(client)  # type: ignore[arg-type]

        for action in (
            service.attack(GAME_ID, [CARD_ID]),
            service.defend(GAME_ID, TABLE_ID, CARD_ID),
            service.transfer(GAME_ID, [CARD_ID]),
            service.seal(GAME_ID),
            service.take(GAME_ID),
            service.pass_turn(GAME_ID),
            service.bito(GAME_ID),
            service.leave(GAME_ID),
        ):
            run_immediate(action)
        next_game = run_immediate(service.rematch(GAME_ID))

        self.assertEqual(next_game, "77777777-7777-4777-8777-777777777777")
        self.assertEqual(
            client.calls,
            [
                ("POST", f"/api/fools/games/{GAME_ID}/attack/", {"card_ids": [CARD_ID]}),
                ("POST", f"/api/fools/games/{GAME_ID}/defend/", {
                    "table_card_id": TABLE_ID, "card_id": CARD_ID,
                }),
                ("POST", f"/api/fools/games/{GAME_ID}/transfer/", {"card_ids": [CARD_ID]}),
                ("POST", f"/api/fools/games/{GAME_ID}/seal/", {}),
                ("POST", f"/api/fools/games/{GAME_ID}/take/", {}),
                ("POST", f"/api/fools/games/{GAME_ID}/pass/", {}),
                ("POST", f"/api/fools/games/{GAME_ID}/bito/", {}),
                ("POST", f"/api/fools/games/{GAME_ID}/leave/", {}),
                ("POST", f"/api/fools/games/{GAME_ID}/again/", {}),
            ],
        )

    def test_malformed_action_snapshot_is_rejected(self):
        client = RecordingClient()
        with self.assertRaises(ProtocolError):
            client.response = {"id": "wrong"}
            run_immediate(GameService(client).take(GAME_ID))  # type: ignore[arg-type]

    def test_lobby_chat_sends_to_lobby_message_endpoint(self):
        client = RecordingClient()
        sent = run_immediate(LobbyChatService(client).send(LOBBY_ID, "Hello"))  # type: ignore[arg-type]

        self.assertEqual(sent["content"], "Hello")
        self.assertEqual(client.calls, [(
            "POST", f"/api/fools/lobbies/{LOBBY_ID}/messages/", {"content": "Hello"},
        )])


class SnapshotLoaderTests(unittest.TestCase):
    def test_game_projection_keeps_private_hand_separate(self):
        loader = SnapshotLoader(None, USER_ID)  # type: ignore[arg-type]
        game = {
            "id": GAME_ID,
            "players": [
                {"user_id": USER_ID, "username": "me", "hand": [{"id": CARD_ID, "hand_id": HAND_ID}]},
                {"user_id": "other", "username": "opponent", "hand": None},
            ],
            "runtime": {"phase": "between", "version": 3},
            "table": [{"id": TABLE_ID, "attack": {"rank": "ace"}, "defense": None}],
            "public_events": [],
            "trump_card": {"rank": "six"},
            "trump_in_deck": True,
            "deck_remaining": 12,
            "discard_count": 1,
            "discard_top": None,
        }

        panels = loader._game_panels(game)

        self.assertEqual(panels["hand"], [{"id": CARD_ID, "hand_id": HAND_ID}])
        self.assertNotIn("hand", panels["players"][0])
        self.assertNotIn("trump_card", panels["game"])
        self.assertEqual(panels["turn"], game["runtime"])
        self.assertEqual(panels["table"], game["table"])

    def test_game_projection_rejects_missing_viewer_hand(self):
        loader = SnapshotLoader(None, USER_ID)  # type: ignore[arg-type]
        with self.assertRaises(ProtocolError):
            loader._game_panels({
                "players": [{"user_id": USER_ID, "hand": None}], "runtime": {},
                "table": [], "public_events": [],
            })


class ApiClientTests(unittest.TestCase):
    def test_screen_navigation_does_not_override_textual_internal_method(self):
        self.assertIs(FoolsApp._replace_screen, App._replace_screen)

    def test_web_command_opens_server_root_in_browser(self):
        from argparse import Namespace

        arguments = Namespace(
            server="https://arena.example", command="web", state_dir=Path(".local-test-state"),
        )
        with patch("fools_client.cli.webbrowser.open", return_value=True) as open_browser:
            result = run_immediate(_run(arguments))

        self.assertEqual(result, 0)
        open_browser.assert_called_once_with("https://arena.example/")

    def test_mutation_sends_session_cookie_csrf_and_same_origin_headers(self):
        seen: list[httpx.Request] = []

        async def respond(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"ok": True}, request=request)

        client = ApiClient(
            "http://127.0.0.1:8000",
            state_directory=Path(".local-test-state"),
            transport=httpx.MockTransport(respond),
        )
        client._http.cookies.set("csrftoken", "a" * 32, domain="127.0.0.1", path="/")
        client._http.cookies.set("sessionid", "session-value", domain="127.0.0.1", path="/")

        client._saved_cookies = client._store.snapshot(client._http.cookies.jar)
        result = run_immediate(client.request("POST", "/api/example/", data={"value": 1}))

        self.assertEqual(result, {"ok": True})
        request = seen[0]
        self.assertEqual(request.headers["X-CSRFToken"], "a" * 32)
        self.assertEqual(request.headers["Origin"], "http://127.0.0.1:8000")
        self.assertEqual(request.headers["Referer"], "http://127.0.0.1:8000/")
        self.assertIn("sessionid=session-value", request.headers["Cookie"])
        self.assertIn(f"csrftoken={'a' * 32}", request.headers["Cookie"])
        run_immediate(client._http.aclose())

    def test_api_error_preserves_status_and_server_code(self):
        async def reject(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                400, json={"detail": "Illegal move", "code": "phase"}, request=request,
            )

        client = ApiClient(
            "http://127.0.0.1:8000",
            state_directory=Path(".local-test-state"),
            transport=httpx.MockTransport(reject),
        )
        client._http.cookies.set("csrftoken", "b" * 32, domain="127.0.0.1", path="/")

        client._saved_cookies = client._store.snapshot(client._http.cookies.jar)
        with self.assertRaises(ApiError) as caught:
            run_immediate(client.request("POST", "/api/example/", data={}))

        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(caught.exception.code, "phase")
        self.assertIn("Illegal move", str(caught.exception))
        run_immediate(client._http.aclose())

    def test_api_redirect_is_not_followed_or_resent(self):
        seen: list[httpx.Request] = []

        async def redirect(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(302, headers={"Location": "/login/"}, request=request)

        client = ApiClient(
            "http://127.0.0.1:8000",
            state_directory=Path(".local-test-state"),
            transport=httpx.MockTransport(redirect),
        )
        client._http.cookies.set("csrftoken", "c" * 32, domain="127.0.0.1", path="/")

        client._saved_cookies = client._store.snapshot(client._http.cookies.jar)
        with self.assertRaises(ProtocolError):
            run_immediate(client.request("POST", "/api/example/", data={}))

        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].method, "POST")
        run_immediate(client._http.aclose())


if __name__ == "__main__":
    unittest.main()
