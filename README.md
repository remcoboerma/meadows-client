# meadows-client

> Client-side Socket.IO transport for MEADOWS: connect, reconnect, JWT handshake.
> No domain logic — shared by `meadows-bot` and future non-browser clients.

## What this package contains

- `client.py` — `MeadowClient` wrapping `socketio.AsyncClient`
  - JWT handshake on `/chat` namespace connect
  - Auto-reconnect (delegated to socketio)
  - `send_message()` constructs a valid protocol `Message` before emitting
  - `on(event, handler)` for user-registered handlers
  - `emit(event, data)` escape hatch for non-message events

## Install

```bash
uv pip install -e .
```

## Test

```bash
uv run pytest -q
```

## Usage

### Preferred: pre-signed JWT (only the server knows the signing key)

```python
from meadows.client import MeadowClient
from meadows.protocol import EventName, JWTRole, build_claims

client = MeadowClient(
    server_url="http://localhost:8080",
    claims=build_claims(name="alice", role=JWTRole.USER),
    token="<pre-signed-jwt>",
)

client.on(EventName.MESSAGE, lambda data: print("got:", data))

await client.connect()
await client.send_message(content="hello world", group_id="general")
```

Generate a token with `inv user-jwt` or `inv bot-jwt` on the server.

### Legacy: raw signing key (only for local dev / TUI)

```python
client = MeadowClient(
    server_url="http://localhost:8080",
    claims=build_claims(name="alice", role=JWTRole.USER),
    jwt_secret=b"<shared key bytes>",
)
```

## The protocol contract

This client never sends a frame that violates `meadows.protocol`. The
`send_message()` method constructs a valid `Message` envelope before
emitting, so the server-side chokepoint never sees an invalid frame from us.
