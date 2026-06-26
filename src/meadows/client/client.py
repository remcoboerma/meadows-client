"""MeadowClient — the client-side Socket.IO transport for MEADOWS.

This wraps `socketio.AsyncClient` with:
  - JWT handshake on /chat namespace connect
  - Auto-reconnect (delegated to socketio's reconnection logic)
  - A typed `send_message()` that constructs the protocol envelope
  - Event-handler registration via `on(event, handler)`

It contains no domain logic. Bots and UIs built on top register their
own handlers; this client only ensures the wire-level contract holds.

Key invariant: this client never sends a frame that violates
`meadows.protocol`. The `send_message()` method constructs a valid
`Message` before emitting, so the chokepoint on the server side never
sees an invalid frame from us.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

import socketio

from meadows.protocol import EventName, JWTClaims, Message, MessageType
from meadows.protocol.jwt import ALGORITHM

import jwt as pyjwt

logger = logging.getLogger(__name__)

Handler = Callable[[dict[str, Any]], Awaitable[None] | None]
ConnectHandler = Callable[[], Awaitable[None] | None]


class MeadowClientError(Exception):
    """Raised when the client cannot start or a transport-level error occurs."""


class MeadowClient:
    """Async Socket.IO client for MEADOWS, with JWT handshake.

    Usage:
        client = MeadowClient(
            server_url="http://localhost:8080",
            claims=build_claims(name="alice", role=JWTRole.USER),
            jwt_secret=b"...",
        )
        client.on(EventName.MESSAGE, handle_message)
        await client.connect()
        await client.send_message(content="hello", group_id="general")
    """

    NAMESPACE = "/chat"

    def __init__(
        self,
        *,
        server_url: str,
        claims: JWTClaims,
        jwt_secret: bytes,
        socketio_client: socketio.AsyncClient | None = None,
    ) -> None:
        self.server_url = server_url
        self.claims = claims
        self.jwt_secret = jwt_secret

        self.sio: socketio.AsyncClient = socketio_client or socketio.AsyncClient(
            reconnection=True,
            reconnection_attempts=None,
            reconnection_delay=1,
            reconnection_delay_max=10,
            logger=False,
            engineio_logger=False,
        )

        self._connected = False
        self._authenticated = False
        self._handlers: dict[EventName, Handler] = {}
        self._connect_handlers: list[ConnectHandler] = []
        self._disconnect_handlers: list[ConnectHandler] = []

        self._register_internal_handlers()

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def authenticated(self) -> bool:
        return self._authenticated

    def on(self, event: EventName | str, handler: Handler) -> None:
        """Register a handler for an event on the /chat namespace.

        BUSINESS RULE: python-socketio's on() replaces existing handlers
        (dict assignment, not append). The client has internal handlers
        for AUTHENTICATED, BOT_AUTHENTICATED, AUTH_ERROR, and ERROR that
        must always run (they set connection state). If a user registers
        a handler for the same event, we chain: internal handler runs
        first, then the user's handler. This prevents a bot's
        on_bot_authenticated from silently overriding the client's
        _on_authenticated (which sets the authenticated flag).
        """
        name = event.value if isinstance(event, EventName) else str(event)
        self._handlers[EventName(name) if name in {e.value for e in EventName} else name] = handler  # type: ignore[arg-type]

        # Chain with any existing handler (internal or user-registered).
        existing = None
        sio_handlers = getattr(self.sio, "handlers", None)
        if sio_handlers is not None:
            ns_handlers = sio_handlers.get(self.NAMESPACE, {})
            if name in ns_handlers:
                existing = ns_handlers[name]
        else:
            # FakeAsyncClient (tests) — check _handlers dict
            for (evt, _ns), h in getattr(self.sio, "_handlers", {}).items():
                if evt == name:
                    existing = h
                    break

        if existing is None:
            self.sio.on(name, handler, namespace=self.NAMESPACE)
        else:

            async def chained(_data: dict[str, Any]) -> None:
                result = existing(_data)
                if hasattr(result, "__await__"):
                    await result
                result = handler(_data)
                if hasattr(result, "__await__"):
                    await result

            self.sio.on(name, chained, namespace=self.NAMESPACE)

    def on_connect(self, handler: ConnectHandler) -> None:
        """Register a handler fired when /chat namespace connects (post-auth)."""
        self._connect_handlers.append(handler)

    def on_disconnect(self, handler: ConnectHandler) -> None:
        """Register a handler fired when /chat namespace disconnects."""
        self._disconnect_handlers.append(handler)

    def _register_internal_handlers(self) -> None:
        self.sio.on("connect", self._on_namespace_connect, namespace=self.NAMESPACE)
        self.sio.on("disconnect", self._on_namespace_disconnect, namespace=self.NAMESPACE)
        self.sio.on(
            EventName.BOT_AUTHENTICATED.value,
            self._on_authenticated,
            namespace=self.NAMESPACE,
        )
        self.sio.on(
            EventName.AUTHENTICATED.value,
            self._on_authenticated,
            namespace=self.NAMESPACE,
        )
        self.sio.on(EventName.AUTH_ERROR.value, self._on_auth_error, namespace=self.NAMESPACE)
        self.sio.on(EventName.ERROR.value, self._on_error, namespace=self.NAMESPACE)

    async def _on_namespace_connect(self) -> None:
        """On /chat connect, send the authenticate event with our JWT."""
        logger.info("connected to %s namespace, authenticating", self.NAMESPACE)
        token = pyjwt.encode(
            self.claims.model_dump(exclude_none=True),
            self.jwt_secret,
            algorithm=ALGORITHM,
        )
        await self.sio.emit(
            EventName.AUTHENTICATE.value,
            {"token": token},
            namespace=self.NAMESPACE,
        )

    async def _on_namespace_disconnect(self) -> None:
        self._connected = False
        self._authenticated = False
        logger.info("disconnected from %s namespace", self.NAMESPACE)
        for handler in self._disconnect_handlers:
            result = handler()
            if hasattr(result, "__await__"):
                await result

    async def _on_authenticated(self, _data: dict[str, Any]) -> None:
        self._connected = True
        self._authenticated = True
        logger.info("authenticated as %s", self.claims.name())
        for handler in self._connect_handlers:
            result = handler()
            if hasattr(result, "__await__"):
                await result

    async def _on_auth_error(self, data: dict[str, Any]) -> None:
        self._authenticated = False
        logger.error("auth error: %s", data)
        raise MeadowClientError(f"authentication failed: {data}")

    async def _on_error(self, data: dict[str, Any]) -> None:
        logger.error("server error: %s", data)

    async def connect(self) -> None:
        """Connect to the server and start the background receiver.

        Returns once the transport is connected; auth completes
        asynchronously via the `_on_authenticated` handler.
        """
        try:
            await self.sio.connect(
                self.server_url,
                namespaces=[self.NAMESPACE],
                transports=["websocket"],
            )
        except Exception as exc:
            raise MeadowClientError(f"failed to connect to {self.server_url}: {exc}") from exc

    async def disconnect(self) -> None:
        """Disconnect from the server."""
        self._connected = False
        self._authenticated = False
        await self.sio.disconnect()

    async def wait(self) -> None:
        """Block until the client disconnects."""
        await self.sio.wait()

    async def send_message(
        self,
        *,
        content: str,
        group_id: str = "general",
        quoted_message_id: str | None = None,
    ) -> Message:
        """Send a chat message. Returns the constructed Message.

        The Message is constructed via the protocol envelope so the wire
        form is always valid — the server-side chokepoint never sees an
        invalid frame from us.
        """
        from meadows.protocol.envelope import QuotedMessage, generate_message_id, now_iso

        quoted = None
        if quoted_message_id:
            quoted = QuotedMessage(
                id=quoted_message_id,
                author=self.claims.name(),
                content="",
                timestamp=now_iso(),
            )

        msg = Message(
            id=generate_message_id(),
            type=MessageType.USER if self.claims.is_user() else MessageType.BOT,
            user_id=self.claims.sub,
            username=self.claims.username,
            bot_name=self.claims.bot_name,
            group_id=group_id,
            content=content,
            quoted_message=quoted,
        )

        await self.sio.emit(
            EventName.MESSAGE.value,
            msg.model_dump(exclude_none=True),
            namespace=self.NAMESPACE,
        )
        return msg

    async def emit(self, event: EventName | str, data: Any) -> None:
        """Emit a raw event. Use sparingly — prefer send_message() for chat.

        This is the escape hatch for events the client sends that aren't
        ordinary messages (e.g. typing, add_reaction). The caller is
        responsible for the shape of `data`.
        """
        name = event.value if isinstance(event, EventName) else str(event)
        await self.sio.emit(name, data, namespace=self.NAMESPACE)


__all__ = ["MeadowClient", "MeadowClientError"]
