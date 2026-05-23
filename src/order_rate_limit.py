"""Per-IP hourly rate-limit middleware for the create_order MCP tool.

Sniffs the JSON-RPC body on POSTs to /mcp; if the call is `tools/call` for
`kapruka_create_order`, applies a separate hourly per-IP cap on top of the
global per-minute limiter. Everything else passes through untouched.

Lives in its own middleware (rather than extending RateLimitMiddleware) so the
two limiters can evolve independently and so we don't have to plumb body-
sniffing into the existing per-minute path. The body is read once here and
replayed downstream so ActivityLogMiddleware can still see it.
"""

from __future__ import annotations

import json
import logging
import time
from collections import OrderedDict
from threading import Lock

from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

_ORDER_TOOL_NAME = "kapruka_create_order"
_WINDOW_SECONDS = 3600.0


class _Window:
    __slots__ = ("count", "reset_at")

    def __init__(self, reset_at: float) -> None:
        self.count = 0
        self.reset_at = reset_at


class _HourlyIPLimiter:
    def __init__(self, limit: int, max_ips: int = 50_000) -> None:
        self._limit = max(1, limit)
        self._max_ips = max_ips
        self._windows: OrderedDict[str, _Window] = OrderedDict()
        self._lock = Lock()

    def check(self, ip: str) -> tuple[bool, int, int]:
        """Returns (allowed, remaining, reset_in_seconds)."""
        now = time.monotonic()
        with self._lock:
            w = self._windows.get(ip)
            if w is None or now >= w.reset_at:
                w = _Window(reset_at=now + _WINDOW_SECONDS)
                self._windows[ip] = w
            else:
                self._windows.move_to_end(ip)

            allowed = w.count < self._limit
            if allowed:
                w.count += 1

            while len(self._windows) > self._max_ips:
                self._windows.popitem(last=False)

            remaining = max(0, self._limit - w.count)
            reset_in = max(0, int(w.reset_at - now))
            return allowed, remaining, reset_in


def _decode(value: bytes) -> str:
    return value.decode("latin-1").strip()


def _client_ip(scope: Scope) -> str:
    """Mirrors activity_log: CF-Connecting-IP > X-Real-IP > XFF > peer.

    The MCP app only listens on 127.0.0.1, so any of these headers were set by
    local Caddy and can be trusted without a per-proxy check.
    """
    client = scope.get("client") or ("unknown", 0)
    peer_ip = client[0] if client else "unknown"

    cf_ip = real_ip = xff = None
    for name, value in scope.get("headers") or []:
        if name == b"cf-connecting-ip":
            cf_ip = _decode(value)
        elif name == b"x-real-ip":
            real_ip = _decode(value)
        elif name == b"x-forwarded-for":
            xff = _decode(value)
    if cf_ip:
        return cf_ip
    if real_ip:
        return real_ip
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first
    return peer_ip


def _is_order_call(body: bytes) -> bool:
    if not body:
        return False
    try:
        msg = json.loads(body.decode("utf-8"))
    except Exception:
        return False
    if not isinstance(msg, dict):
        return False
    if msg.get("method") != "tools/call":
        return False
    params = msg.get("params") or {}
    if not isinstance(params, dict):
        return False
    return params.get("name") == _ORDER_TOOL_NAME


class OrderRateLimitMiddleware:
    """Hourly per-IP cap on the create_order MCP tool."""

    def __init__(
        self,
        app: ASGIApp,
        limit_per_hour: int,
        watched_prefixes: tuple[str, ...] = ("/mcp",),
    ) -> None:
        self.app = app
        self.limiter = _HourlyIPLimiter(limit_per_hour)
        self.limit = limit_per_hour
        self.watched_prefixes = watched_prefixes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if not any(path.startswith(p) for p in self.watched_prefixes):
            await self.app(scope, receive, send)
            return

        # Slurp the body so we can peek at the JSON-RPC envelope.
        chunks: list[bytes] = []
        more = True
        while more:
            msg = await receive()
            mtype = msg.get("type")
            if mtype == "http.request":
                chunk = msg.get("body") or b""
                if chunk:
                    chunks.append(chunk)
                more = msg.get("more_body", False)
            else:
                more = False
                if mtype == "http.disconnect":
                    return
        body = b"".join(chunks)

        if _is_order_call(body):
            ip = _client_ip(scope)
            allowed, remaining, reset_in = self.limiter.check(ip)
            if not allowed:
                logger.info("order_rate_limit: blocked ip=%s reset_in=%ds", ip, reset_in)
                payload = json.dumps(
                    {
                        "error": "order_rate_limit_exceeded",
                        "message": (
                            f"Free tier order limit of {self.limit}/hour per IP exceeded. "
                            f"Try again in {reset_in}s."
                        ),
                    }
                ).encode("utf-8")
                await send(
                    {
                        "type": "http.response.start",
                        "status": 429,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"retry-after", str(reset_in).encode()),
                            (b"ratelimit-limit", str(self.limit).encode()),
                            (b"ratelimit-remaining", b"0"),
                            (b"ratelimit-reset", str(reset_in).encode()),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": payload})
                return

        # Replay the captured body to the downstream app.
        replayed = False

        async def replay_receive() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay_receive, send)
