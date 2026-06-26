"""MEADOWS client transport.

The client side of the Socket.IO transport: connect, reconnect, JWT
handshake. Shared by `meadows-bot` and future non-browser clients (e.g.
a TUI, the ntfy-frontend proof from section 3.3 of the migration intent).

This package contains no domain logic — no command handling, no bot
behavior, no message rendering. It only manages the connection and the
auth handshake. What you do with the connection is the caller's job.

Section 2 of MEADOWS-migration-intent.md: "client maakt de tweede
implementatie goedkoop. Bot en (toekomstige niet-browser-)clients delen
connect/reconnect/JWT-handshake."
"""

from meadows.client.__about__ import __version__
from meadows.client.client import MeadowClient, MeadowClientError

__all__ = ["MeadowClient", "MeadowClientError", "__version__"]
