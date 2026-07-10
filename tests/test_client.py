"""Tests for meadows-client.

These tests use a FakeAsyncClient that records emits and lets us trigger
handlers manually. No real server, no real sockets. The protocol package
is the only sibling dependency — installed editable.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from meadows.client import MeadowClient, MeadowClientError
from meadows.protocol import EventName, JWTRole, build_claims


class FakeAsyncClient:
    """Stand-in for socketio.AsyncClient. Records emits, triggers handlers."""

    def __init__(self) -> None:
        self.emits: list[tuple[str, Any, str]] = []  # (event, data, namespace)
        self._handlers: dict[tuple[str, str], Any] = {}
        self.connected = False
        self.connect_url: str | None = None
        self.connect_namespaces: list[str] = []
        self.disconnect_count = 0

    def on(self, event: str, handler: Any, namespace: str = "/") -> None:
        self._handlers[(event, namespace)] = handler

    async def connect(self, url: str, namespaces: list[str] | None = None, **_kwargs: Any) -> None:
        self.connect_url = url
        self.connect_namespaces = namespaces or []
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False
        self.disconnect_count += 1

    async def emit(self, event: str, data: Any, namespace: str = "/") -> None:
        self.emits.append((event, data, namespace))

    async def wait(self) -> None:
        pass

    async def trigger(self, event: EventName | str, data: Any, namespace: str = "/chat") -> None:
        name = event.value if isinstance(event, EventName) else str(event)
        handler = self._handlers.get((name, namespace))
        if handler is None:
            return
        result = handler(data)
        if hasattr(result, "__await__"):
            await result

    async def trigger_connect(self, namespace: str = "/chat") -> None:
        handler = self._handlers.get(("connect", namespace))
        if handler is None:
            return
        result = handler()
        if hasattr(result, "__await__"):
            await result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client(role: JWTRole = JWTRole.USER, name: str = "alice") -> tuple[MeadowClient, FakeAsyncClient]:
    fake = FakeAsyncClient()
    client = MeadowClient(
        server_url="http://localhost:8080",
        claims=build_claims(name=name, role=role),
        jwt_secret=b"test-secret-key-that-is-long-enough-32b!",
        socketio_client=fake,
    )
    return client, fake


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestMeadowClientConstruction:
    def test_client_stores_config(self):
        client, _ = _make_client()
        assert client.server_url == "http://localhost:8080"
        assert client.claims.sub == "user-alice"
        assert client.jwt_secret == b"test-secret-key-that-is-long-enough-32b!"

    def test_client_starts_disconnected(self):
        client, _ = _make_client()
        assert client.connected is False
        assert client.authenticated is False

    def test_client_registers_internal_handlers(self):
        _client, fake = _make_client()
        # The internal handlers are registered on the /chat namespace
        assert ("connect", "/chat") in fake._handlers
        assert ("disconnect", "/chat") in fake._handlers
        assert (EventName.AUTHENTICATED.value, "/chat") in fake._handlers
        assert (EventName.BOT_AUTHENTICATED.value, "/chat") in fake._handlers
        assert (EventName.AUTH_ERROR.value, "/chat") in fake._handlers
        assert (EventName.ERROR.value, "/chat") in fake._handlers


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


class TestMeadowClientConnection:
    async def test_connect_calls_socketio_connect(self):
        client, fake = _make_client()
        await client.connect()
        assert fake.connect_url == "http://localhost:8080"
        assert "/chat" in fake.connect_namespaces

    async def test_connect_raises_on_socketio_failure(self):
        client, fake = _make_client()

        async def failing_connect(*_args: Any, **_kwargs: Any) -> None:
            raise ConnectionError("server unreachable")

        fake.connect = failing_connect  # type: ignore[method-assign]
        with pytest.raises(MeadowClientError, match="failed to connect"):
            await client.connect()

    async def test_disconnect_clears_state(self):
        client, fake = _make_client()
        await client.connect()
        client._connected = True
        client._authenticated = True
        await client.disconnect()
        assert client.connected is False
        assert client.authenticated is False
        assert fake.disconnect_count == 1


# ---------------------------------------------------------------------------
# JWT handshake
# ---------------------------------------------------------------------------


class TestMeadowClientHandshake:
    async def test_connect_emits_authenticate_with_jwt(self):
        client, fake = _make_client()
        await client.connect()
        # Simulate server-side /chat connect
        await fake.trigger_connect()

        # The client should have emitted an authenticate event
        auth_emits = [e for e in fake.emits if e[0] == EventName.AUTHENTICATE.value]
        assert len(auth_emits) == 1
        _, data, ns = auth_emits[0]
        assert ns == "/chat"
        assert "token" in data
        assert isinstance(data["token"], str)

    async def test_authenticated_event_sets_authenticated_flag(self):
        client, fake = _make_client()
        await client.connect()
        client._connected = False
        client._authenticated = False

        await fake.trigger(EventName.AUTHENTICATED, {"user_id": "user-alice", "groups": []})

        assert client.authenticated is True
        assert client.connected is True

    async def test_bot_authenticated_event_also_sets_authenticated_flag(self):
        client, fake = _make_client(role=JWTRole.BOT, name="echo")
        await client.connect()
        client._connected = False
        client._authenticated = False

        await fake.trigger(EventName.BOT_AUTHENTICATED, {"bot_name": "echo", "groups": []})

        assert client.authenticated is True

    async def test_auth_error_raises_meadow_client_error(self):
        client, fake = _make_client()
        await client.connect()

        with pytest.raises(MeadowClientError, match="authentication failed"):
            await fake.trigger(EventName.AUTH_ERROR, {"error": "bad token"})


# ---------------------------------------------------------------------------
# Message sending
# ---------------------------------------------------------------------------


class TestMeadowClientSend:
    async def test_send_message_emits_message_event(self):
        client, fake = _make_client()
        await client.connect()

        await client.send_message(content="hello", group_id="general")

        message_emits = [e for e in fake.emits if e[0] == EventName.MESSAGE.value]
        assert len(message_emits) == 1
        _, data, ns = message_emits[0]
        assert ns == "/chat"
        assert data["content"] == "hello"
        assert data["group_id"] == "general"
        assert data["type"] == "user"
        assert data["user_id"] == "user-alice"

    async def test_send_message_returns_constructed_message(self):
        client, _ = _make_client()
        await client.connect()

        msg = await client.send_message(content="hi", group_id="general")
        assert msg.content == "hi"
        assert msg.group_id == "general"
        assert msg.user_id == "user-alice"

    async def test_send_message_for_bot_has_bot_type(self):
        client, fake = _make_client(role=JWTRole.BOT, name="echo")
        await client.connect()

        await client.send_message(content="pong", group_id="general")

        message_emits = [e for e in fake.emits if e[0] == EventName.MESSAGE.value]
        assert message_emits[0][1]["type"] == "bot"
        assert message_emits[0][1]["bot_name"] == "echo"

    async def test_send_message_with_quoted_message(self):
        client, fake = _make_client()
        await client.connect()

        await client.send_message(content="reply", group_id="general", quoted_message_id="msg-123")

        _, data, _ = next(e for e in fake.emits if e[0] == EventName.MESSAGE.value)
        assert data["quoted_message"]["id"] == "msg-123"
        assert data["quoted_message"]["author"] == "alice"


# ---------------------------------------------------------------------------
# Handler registration
# ---------------------------------------------------------------------------


class TestMeadowClientHandlers:
    async def test_on_registers_handler(self):
        client, fake = _make_client()
        received: list[dict] = []

        def handler(data: dict) -> None:
            received.append(data)

        client.on(EventName.MESSAGE, handler)
        await fake.trigger(EventName.MESSAGE, {"content": "hi"})

        assert len(received) == 1
        assert received[0]["content"] == "hi"

    async def test_on_connect_handlers_fire_on_authenticated(self):
        client, fake = _make_client()
        fired: list[str] = []

        def on_ready() -> None:
            fired.append("ready")

        client.on_connect(on_ready)
        await fake.trigger(EventName.AUTHENTICATED, {"user_id": "user-alice"})

        assert fired == ["ready"]

    async def test_on_disconnect_handlers_fire_on_disconnect(self):
        client, fake = _make_client()
        fired: list[str] = []

        def on_gone() -> None:
            fired.append("gone")

        client.on_disconnect(on_gone)
        # Disconnect handler takes no args — trigger directly via _handlers
        handler = fake._handlers.get(("disconnect", "/chat"))
        if handler is not None:
            result = handler()
            if hasattr(result, "__await__"):
                await result

        assert fired == ["gone"]

    async def test_emit_sends_raw_event(self):
        client, fake = _make_client()
        await client.connect()

        await client.emit(EventName.TYPING, {"group_id": "general"})

        typing_emits = [e for e in fake.emits if e[0] == EventName.TYPING.value]
        assert len(typing_emits) == 1
        assert typing_emits[0][1] == {"group_id": "general"}
        assert typing_emits[0][2] == "/chat"


# ---------------------------------------------------------------------------
# Async handlers
# ---------------------------------------------------------------------------


class TestAsyncHandlers:
    async def test_async_handler_supported(self):
        client, fake = _make_client()
        received: list[str] = []

        async def handler(data: dict) -> None:
            await asyncio.sleep(0)
            received.append(data["content"])

        client.on(EventName.MESSAGE, handler)
        await fake.trigger(EventName.MESSAGE, {"content": "async hello"})

        assert received == ["async hello"]

    async def test_async_connect_handler_supported(self):
        client, fake = _make_client()
        fired: list[str] = []

        async def on_ready() -> None:
            await asyncio.sleep(0)
            fired.append("ready")

        client.on_connect(on_ready)
        await fake.trigger(EventName.AUTHENTICATED, {"user_id": "user-alice"})

        assert fired == ["ready"]


# ---------------------------------------------------------------------------
# Label subscriptions
# ---------------------------------------------------------------------------


class TestLabelSubscriptions:
    async def test_register_label_subscription_emits(self):
        """register_label_subscription stores the subscription."""
        client, _fake = _make_client()
        client._connected = True
        client._authenticated = True
        client.register_label_subscription("sentiment", {"regex_match": [{"var": "label"}, "^sentiment$"]})
        assert len(client._label_subscriptions) == 1
        assert client._label_subscriptions[0]["name"] == "sentiment"

    async def test_unregister_label_subscription_removes(self):
        """unregister removes from local list."""
        client, _fake = _make_client()
        client.register_label_subscription("s1", {})
        client.register_label_subscription("s2", {})
        assert len(client._label_subscriptions) == 2
        client.unregister_label_subscription("s1")
        assert len(client._label_subscriptions) == 1
        assert client._label_subscriptions[0]["name"] == "s2"

    async def test_label_assigned_dispatches_to_handler(self):
        """on_label_assigned decorator registers callback; event dispatches to it."""
        client, _fake = _make_client()
        received: list[dict] = []

        @client.on_label_assigned("sentiment")
        def handler(data: dict) -> None:
            received.append(data)

        await client._on_label_assigned_event({"subscription_name": "sentiment", "labels": []})
        assert len(received) == 1

    async def test_label_assigned_no_match_ignored(self):
        """Event with no matching handler is silently ignored."""
        client, _fake = _make_client()
        # Should not raise
        await client._on_label_assigned_event({"subscription_name": "unknown", "labels": []})

    async def test_subscriptions_stored_for_replay(self):
        """Subscriptions are kept in _label_subscriptions for replay."""
        client, _fake = _make_client()
        client.register_label_subscription("s1", {}, scope="global")
        assert client._label_subscriptions[0]["scope"] == "global"

    async def test_empty_predicate_sent_as_empty_dict(self):
        """predicate=None sends {}."""
        client, _fake = _make_client()
        client.register_label_subscription("s1", None)
        assert client._label_subscriptions[0]["predicate"] == {}

    async def test_subscriptions_replayed_on_auth(self):
        """After AUTHENTICATED, stored subscriptions are re-emitted."""
        client, fake = _make_client()
        client.register_label_subscription("s1", {"test": True})
        fake.emits.clear()
        await fake.trigger(EventName.AUTHENTICATED, {})
        emits = [(e, d) for e, d, _ in fake.emits if e == EventName.REGISTER_LABEL_SUBSCRIPTION.value]
        assert len(emits) >= 1
