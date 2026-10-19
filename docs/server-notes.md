# Server Contract Notes

The adjacent `Django-Durak` repository is used as a read-only integration reference. No server changes are applied from this client project.

## Existing routes

| Operation | Existing route |
| --- | --- |
| Register, sign in, profile, sign out | `/api/accounts/auth/{register,login,profile,logout}/` |
| List and create lobbies | `/api/fools/lobbies/` |
| Lobby snapshot | `/api/fools/lobbies/<lobby_id>/` |
| Join, leave, readiness, settings, start | `/api/fools/lobbies/<lobby_id>/{join,leave,ready,settings,start}/` |
| Lobby message history and send | `/api/fools/lobbies/<lobby_id>/messages/` |
| Game snapshot | `/api/fools/games/<game_id>/` |
| Game actions | `/api/fools/games/<game_id>/{attack,seal,defend,take,transfer,pass,bito,leave,again}/` |
| Live lobby list | `/ws/lobbies/` |
| Lobby events, including chat | `/ws/lobbies/<lobby_id>/` |
| Game events | `/ws/games/<game_id>/` |

These routes were checked against the existing Django URL configuration and API views. The web app remains the graphical client; the terminal client is another API consumer.

## Email confirmation behavior

When `ACCOUNT_EMAIL_CONFIRMATION_REQUIRED` is enabled, registration returns a message without creating a session. If disabled, the server may start a session immediately. Login intentionally uses the same error for unconfirmed email and invalid credentials, so the client must not claim to know the exact cause.

## Known event timing limitations

Some service actions broadcast lobby or game events before the surrounding database transaction commits. A WebSocket client can therefore receive an event before a follow-up HTTP snapshot sees the committed state, or receive an event for a transaction that rolls back. The client handles events by loading snapshots and performs periodic reconciliation, but cannot guarantee immediate consistency while the server emits events before commit.

A server-side improvement would schedule these broadcasts with `transaction.on_commit`, passing immutable identifiers and payload data to the callback. This should be implemented and tested in the server repository only after explicit authorization.

Readiness updates are sent to the lobby channel but are not currently published to the global public-lobby list. The client reconciles that list periodically. If immediate public-list updates are required, the server can publish the existing lobby-list event after readiness is committed; this requires an explicit server-side change request.
