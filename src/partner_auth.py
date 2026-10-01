"""PartnerAuthMiddleware — resolves partner identity once, outermost.

For every HTTP request:
  1. Read `X-Partner-Key`; if it matches a configured key the request is that
     partner. Anything else (missing, empty, wrong, oversized) is public —
     silently, with no error that would reveal whether a key exists.
  2. For a partner only, read `X-Partner-Customer-Id` (opaque, [A-Za-z0-9_-],
     max 128); an invalid value is ignored. Non-partners' header is ignored.
  3. Remove both headers from the ASGI scope. Nothing downstream — the MCP SDK,
     tools, error handlers, tracebacks — can ever see or log the key.
  4. Bind MCP sessions to the identity they were created with:
       - a session created WITH a key only accepts requests carrying that same
         key; anything else (no key, invalid key, another or rotated key) is
         403 session_identity_mismatch;
       - a session created WITHOUT a key stays public: a key presented later is
         ignored (no upgrade mid-session, and no error either).
  5. Attach the result as scope["kapruka.identity"] (src.partners.Identity, or
     absent for public) for the limiters, the private-tool gate, tools/list and
     the activity log.

IP-allowlisted callers (Eagle) are not touched here: with no key they are
"public" to this middleware and keep their IP-based treatment everywhere else.
"""

from __future__ import annotations

import json
import logging
from collections import OrderedDict
from threading import Lock
from typing import Optional

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from src import partners
from src.partners import CUSTOMER_HEADER, KEY_HEADER, Identity

logger = logging.getLogger(__name__)

_SESSION_HEADER = b"mcp-session-id"
_PUBLIC = ""  # fingerprint of "no partner key"


class _SessionBindings:
    """session id -> key fingerprint, for sessions opened WITH a key only.
    Public sessions aren't stored (an unknown session is public anyway), so
    public traffic can never push partner sessions out of the LRU cap. In
    memory — MCP sessions are in memory too, so both go away on restart."""

    def __init__(self, max_sessions: int = 100_000) -> None:
        self._map: OrderedDict[str, str] = OrderedDict()
        self._max = max_sessions
        self._lock = Lock()

    def bind(self, session_id: str, fp: str) -> None:
        with self._lock:
            self._map[session_id] = fp
            self._map.move_to_end(session_id)
            while len(self._map) > self._max:
                self._map.popitem(last=False)

    def get(self, session_id: str) -> Optional[str]:
        with self._lock:
            fp = self._map.get(session_id)
            if fp is not None:
                self._map.move_to_end(session_id)
            return fp

    def drop(self, session_id: str) -> None:
        with self._lock:
            self._map.pop(session_id, None)


class PartnerAuthMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        config: Optional[partners.PartnerConfig] = None,
        watched_prefixes: tuple[str, ...] = ("/mcp",),
        max_sessions: int = 100_000,
    ) -> None:
        self.app = app
        self._config = config
        self.watched_prefixes = watched_prefixes
        self.sessions = _SessionBindings(max_sessions)

    @property
    def config(self) -> partners.PartnerConfig:
        return self._config if self._config is not None else partners.current()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        presented: Optional[str] = None
        customer_raw: Optional[str] = None
        session_id: Optional[str] = None
        kept: list[tuple[bytes, bytes]] = []
        for name, value in scope.get("headers") or []:
            if name == KEY_HEADER:
                presented = value.decode("latin-1")
            elif name == CUSTOMER_HEADER:
                customer_raw = value.decode("latin-1").strip()
            else:
                if name == _SESSION_HEADER:
                    session_id = value.decode("latin-1").strip() or None
                kept.append((name, value))
        # Strip the partner headers from everything downstream, always.
        scope = dict(scope)
        scope["headers"] = kept

        match = self.config.authenticate(presented)
        presented = None  # drop the only reference to the raw key
        fp = match[1] if match else _PUBLIC

        watched = any(scope.get("path", "").startswith(p) for p in self.watched_prefixes)
        if watched and session_id is not None:
            bound = self.sessions.get(session_id)
            if bound is None:
                # A session opened without a key (or one we no longer know): it
                # stays public — a key presented now is ignored, not an upgrade.
                if match:
                    logger.info("partner_auth: %s presented its key on a public session; "
                                "treated as public", match[0])
                match, fp = None, _PUBLIC
            elif bound != fp:
                logger.info("partner_auth: refused a request on a partner session without its key")
                await _reject(send)
                return

        if match:
            customer = partners.valid_customer_id(customer_raw)
            if customer_raw and customer is None:
                logger.warning("partner_auth: %s sent an invalid %s; ignored",
                               match[0], CUSTOMER_HEADER.decode())
            scope["kapruka.identity"] = Identity(partner=match[0], customer_id=customer, key_fp=fp)

        if not watched:
            await self.app(scope, receive, send)
            return

        is_delete = scope.get("method") == "DELETE"

        async def bind_new_session(message: Message) -> None:
            if message["type"] == "http.response.start" and session_id is None and fp:
                for name, value in message.get("headers") or []:
                    if name == _SESSION_HEADER:
                        self.sessions.bind(value.decode("latin-1").strip(), fp)
                        break
            await send(message)

        await self.app(scope, receive, bind_new_session)
        if is_delete and session_id is not None:
            self.sessions.drop(session_id)


async def _reject(send: Send) -> None:
    body = json.dumps({
        "error": "session_identity_mismatch",
        "message": "This MCP session was created with different credentials. Start a new session.",
    }).encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": 403,
        "headers": [(b"content-type", b"application/json")],
    })
    await send({"type": "http.response.body", "body": body})
