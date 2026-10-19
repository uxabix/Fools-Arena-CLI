"""Standalone Textual client, authentication commands, and live diagnostics."""

import argparse
import asyncio
import getpass
import ipaddress
import json
import sys
import warnings
import webbrowser
from contextlib import aclosing
from importlib.metadata import version
from pathlib import Path

import httpx

from fools_client.api import ApiClient
from fools_client.auth import AuthService, UserProfile
from fools_client.config import default_state_directory, normalize_server
from fools_client.errors import ApiError, ClientError, ConfigurationError, display_text
from fools_client.live import LiveSession
from fools_client.realtime import Subscription


def _server_argument(value: str) -> str:
    try:
        return normalize_server(value)
    except ConfigurationError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


def _password() -> str:
    if not sys.stdin.isatty():
        raise ConfigurationError(
            "Run this command in an interactive terminal to enter the password securely."
        )
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass("Password: ")
        except getpass.GetPassWarning:
            raise ConfigurationError(
                "This terminal does not support hidden password input. "
                "Run the command in an interactive terminal."
            ) from None


def _print_profile(profile: UserProfile) -> None:
    print(f"Signed in as {display_text(profile.username)} ({display_text(profile.email)}).")


async def _run(arguments: argparse.Namespace) -> int:
    address = httpx.URL(arguments.server)
    try:
        local_server = ipaddress.ip_address(address.host).is_loopback
    except ValueError:
        local_server = address.host.lower() == "localhost"
    transport_notice = (
        "Warning: HTTP does not encrypt passwords or cookies. Use HTTPS for remote servers."
        if address.scheme == "http" and not local_server else ""
    )
    if arguments.command == "tui" and (not sys.stdin.isatty() or not sys.stdout.isatty()):
        raise ConfigurationError(
            "Run the Textual interface in an interactive terminal. "
            "The profile and watch commands are available for scripts."
        )
    if transport_notice and arguments.command != "tui":
        print(transport_notice, file=sys.stderr)
    if arguments.command == "web":
        if not webbrowser.open(f"{arguments.server}/"):
            raise ConfigurationError(
                "Could not open a browser. Open this URL manually: " + arguments.server
            )
        return 0
    async with ApiClient(arguments.server, state_directory=arguments.state_dir) as client:
        auth = AuthService(client)
        if arguments.command == "tui":
            from fools_client.ui.app import FoolsApp

            await FoolsApp(client, transport_notice=transport_notice).run_async()
        elif arguments.command == "register":
            username = arguments.username if arguments.username is not None else input("Username: ")
            email = arguments.email if arguments.email is not None else input("Email: ")
            notice = await auth.register(username, email, _password())
            print(display_text(notice))
            if not client.has_session:
                print("Confirm your email using the link in the message, then run login. No session was started.")
            else:
                print("The server started a session because email confirmation is disabled in its configuration.")
        elif arguments.command == "login":
            username = arguments.username if arguments.username is not None else input("Username: ")
            try:
                profile = await auth.login(username, _password())
            except ApiError as error:
                if error.status_code == 400:
                    print(
                        "If you registered recently, check your email for a confirmation link. "
                        "The API does not distinguish an unconfirmed email from invalid credentials.",
                        file=sys.stderr,
                    )
                raise
            _print_profile(profile)
        elif arguments.command == "logout":
            print(display_text(await auth.logout()))
        elif arguments.command == "watch":
            if arguments.lobby is not None:
                subscription = Subscription("lobby", arguments.lobby)
            elif arguments.game is not None:
                subscription = Subscription("game", arguments.game)
            else:
                subscription = Subscription("lobbies")
            profile = await auth.profile()
            session = LiveSession(client, profile.user_id)
            async with aclosing(session.watch(subscription)) as updates:
                async for update in updates:
                    print(json.dumps({
                        "scope": subscription.kind, "resource_id": subscription.resource_id,
                        "reason": update.reason, "changed": update.panels,
                    }, ensure_ascii=True), flush=True)
        else:
            _print_profile(await auth.profile())
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="fools-client",
        description="Fools Arena terminal client with Textual UI, authentication, and live lobbies.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {version('cli-fools-client')}",
    )
    parser.add_argument(
        "--server",
        required=True,
        type=_server_argument,
        metavar="URL",
        help="Server address, for example http://127.0.0.1:8000.",
    )
    parser.add_argument(
        "--state-dir", type=Path, default=default_state_directory(), metavar="PATH",
        help="Client data directory (defaults to the current OS user's data directory).",
    )
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")
    parser.set_defaults(command="tui")
    commands.add_parser("tui", help="Open the Textual interface (default).")
    commands.add_parser("web", help="Open the graphical web application in your browser.")
    register = commands.add_parser("register", help="Register and send an email confirmation message.")
    register.add_argument("--username", help="Username; prompted interactively when omitted.")
    register.add_argument("--email", help="Email address; prompted interactively when omitted.")
    login = commands.add_parser("login", help="Sign in and save the session.")
    login.add_argument("--username", help="Username; prompted interactively when omitted.")
    commands.add_parser("profile", help="Check the saved session with the server.")
    commands.add_parser("logout", help="End the server session and remove local cookies.")
    watch = commands.add_parser("watch", help="Watch panel changes as diagnostic JSON.")
    scope = watch.add_mutually_exclusive_group()
    scope.add_argument("--lobby", metavar="UUID", help="Lobby and chat; lobby membership is required.")
    scope.add_argument("--game", metavar="UUID", help="Game; game membership is required.")
    arguments = parser.parse_args()
    try:
        return asyncio.run(_run(arguments))
    except ClientError as error:
        print(display_text(error), file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("Cancelled.", file=sys.stderr)
        return 130
