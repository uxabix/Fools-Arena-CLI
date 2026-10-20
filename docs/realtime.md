# Live Snapshots and WebSockets

## Diagnostic command

Sign in first, then run `watch` from the client repository:

```powershell
.\.venv\Scripts\fools-client.exe --server http://127.0.0.1:8000 watch
.\.venv\Scripts\fools-client.exe --server http://127.0.0.1:8000 watch --lobby LOBBY_UUID
.\.venv\Scripts\fools-client.exe --server http://127.0.0.1:8000 watch --game GAME_UUID
```

Replace the placeholders with real UUIDs. Lobby and game subscriptions require server-side membership. Without a scope, `watch` subscribes to the public lobby list. `--lobby` and `--game` are mutually exclusive. Ctrl+C closes the subscription.

Each output line is JSON containing `scope`, `resource_id`, `reason`, and changed panels. This diagnostic command may display game data in the terminal but does not persist it.

## Screen data flow

`LiveSession(client, viewer_id).watch(Subscription(...))` yields `PanelUpdate` objects. `viewer_id` comes from the server-validated profile. Close the async stream when leaving a screen; keep its `ApiClient` open while the stream is active.

Panels include lobby list; lobby, players, settings and chat; or game, players, hand, table, turn, deck and events. The private hand is projected only into the `hand` panel. Snapshots are in-memory view data, not a local game model.

The game screen separately subscribes to the lobby channel for chat. Screen navigation, not the transport, decides whether to open an active game or rematch.

## Synchronization

1. Open the authenticated WebSocket.
2. Load the HTTP snapshot after subscription is active.
3. Treat events as requests to reload affected panels; never apply moves locally.
4. Compare new panel data and emit only changes.

Chat events reload chat history. Lobby events reload lobby data. Game events reload the viewer's game snapshot. Unknown valid events cause a full subscription refresh. Hand-signal events update opponent card-back hints directly and do not reload or change the game snapshot.

Nearby events are coalesced for 50 ms. The queue is bounded without silently dropping events. A full snapshot comparison also runs every 30 seconds to recover from missed notifications. Reconnecting always triggers a fresh snapshot.

## Connection errors

Stale snapshots remain visible while actions are disabled. Temporary network and server failures reconnect with exponential backoff. Authentication failures request sign-in; authorization failures do not delete valid session cookies. WebSocket redirects are disabled and TLS verification remains enabled.
