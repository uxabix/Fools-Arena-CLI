# Textual Interface

## Launch

From this repository after installing the package:

```powershell
.\.venv\Scripts\fools-client.exe --server http://127.0.0.1:8000
```

The default command opens the Textual interface. `--server` and `--state-dir` must appear before the command. Use `web` to open the server-hosted graphical application in the default browser.

## Authentication

The client validates a saved session with the server before showing lobbies. Expired sessions return to the sign-in form. Password inputs are cleared before requests and are never written to disk. Only session cookies are persisted.

If email confirmation is required, registration reports the server's message and does not create a session. The API intentionally does not reveal whether a failed login was caused by an unconfirmed email or invalid credentials.

## Lobby navigation

- Use arrow keys to select a public lobby; Enter opens its join form.
- Join a private lobby by UUID and enter its password in the masked field.
- Create a lobby with a name, privacy setting, password, player capacity, deck size, transfer mode, neighbor-only throw-in option, and optional bot fill.
- Lobby details show players, readiness, owner, settings, and active game. The owner can edit settings and start a game when the server reports `can_start`.
- Readiness and settings change only after the server accepts the command and a new snapshot is loaded.
- Leaving a lobby requires confirmation. During a game, the server may count this as leaving the game.

Escape returns to the lobby list without leaving the lobby. Ctrl+R reloads the live subscription and snapshot. Ctrl+Q closes the client while preserving the saved session. Tab and Shift+Tab move focus. The **Open graphical UI** button launches the Django web app; sign-in in the browser may be separate from the CLI session.

## Game table and chat

The game screen displays players, table cards, the current player's hand, turn information, deck and discard data, and lobby chat. Select hand cards with Space. To defend, select one hand card and a table attack row. Commands are sent to the server; the client does not apply moves locally. A successful command triggers a fresh snapshot, while a rule error is shown without replacing the displayed state.

Lobby chat is available before and during a game. Chat and game state synchronize through WebSocket events and server snapshots. A rematch is opened when the server creates its next game.

## Live behavior

Each live screen subscribes before loading its initial snapshot. After reconnecting it reloads the snapshot. Only changed panels and changed table cells are updated, preserving the selected row and active form. Stale data disables actions. Commands are not automatically retried after an uncertain network result.
