# CLI Fools Client

A standalone Python terminal client for Fools Arena. The Django application remains the graphical web client and the authoritative source of game rules and state. This repository communicates with it through its existing JSON API and WebSocket endpoints; it does not import Django models or game services.

## Status

The client includes HTTP/JSON transport, CSRF handling, persistent session cookies, authentication, WebSocket synchronization, lobby management, a Textual interface, game commands, and lobby chat. Game actions are sent to the server, and the displayed state is rebuilt from server snapshots.

The terminal and graphical clients can be opened against the same server. Game state stays on the server when switching clients. Browser authentication is separate from the CLI cookie store, so the user may need to sign in again in the browser.

## Architecture

- `auth.py`, `lobbies.py`, and `games.py` contain API-facing application services.
- `api.py`, `realtime.py`, and `session.py` handle transport, WebSocket connections, and session persistence.
- `live.py` converts server snapshots into view panels and computes panel-level changes.
- `ui/` contains Textual screens, navigation, event handlers, and incremental widgets.
- The server owns validation, rules, and persistent game state. UI screens do not apply game actions optimistically.

This separation keeps application services independent of the Textual presentation layer and allows another presentation to use the same server-backed operations.

## Requirements

- Python 3.13 or newer
- `httpx`, `websockets`, and Textual (pinned in `pyproject.toml`)
- A running Fools Arena server for live use

Django, Channels, Redis, and the database are not client dependencies.

## Install and run (Windows PowerShell)

From the repository root:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\fools-client.exe --help
```

Run the terminal interface:

```powershell
.\.venv\Scripts\fools-client.exe --server http://127.0.0.1:8000
```

Open the existing graphical web app in the default browser:

```powershell
.\.venv\Scripts\fools-client.exe --server http://127.0.0.1:8000 web
```

The TUI also provides an **Open graphical UI** action from lobby and game screens. The browser may require a separate sign-in. State and game progress remain on the server.

Register, sign in, inspect the profile, or sign out from the CLI:

```powershell
.\.venv\Scripts\fools-client.exe --server http://127.0.0.1:8000 register --username player --email player@example.com
.\.venv\Scripts\fools-client.exe --server http://127.0.0.1:8000 login --username player
.\.venv\Scripts\fools-client.exe --server http://127.0.0.1:8000 profile
.\.venv\Scripts\fools-client.exe --server http://127.0.0.1:8000 logout
```

Passwords are entered through hidden interactive input and are never persisted. If the server requires email confirmation, registration does not start a session.

## Tests

Run the offline contract tests without a Django server:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m compileall -q src tests
```

These tests cover selected API headers and errors, game command routes and payloads, lobby chat, and game snapshot projection. They do not replace an end-to-end run against a live server.

## Session storage

By default, session cookies are stored in the current user's application data directory. Use `--state-dir PATH` to choose another location. Only session and CSRF cookies are saved; passwords, profiles, and game state are not. On Windows, cookie data is protected with DPAPI for the current OS user. On POSIX systems, the storage directory and file use restrictive permissions.

Use HTTPS/WSS outside trusted local development. The client does not automatically retry state-changing API commands after a network failure because the result may be unknown.

## Existing server integration

The client uses the existing routes documented in `docs/server-notes.md` and `docs/realtime.md`. The adjacent `Django-Durak` repository is treated as read-only by this project unless the owner explicitly requests a server change.
