"""Per-IP rate-limit ASGI middleware.

Fixed-window counter keyed on client IP. We sit behind Caddy, which sets
X-Real-IP — we only trust that header when the request comes from a
configured trusted proxy IP (defaults to localhost).

Returns 429 with Retry-After + RateLimit-* headers when over the limit.
Health/readiness paths bypass the limiter.
"""

from __future__ import annotations

import json
import time
from collections import OrderedDict
from threading import Lock
from typing import Awaitable, Callable

from starlette.types import ASGIApp, Message, Receive, Scope, Send

_BYPASS_PATHS = {"/health", "/ready"}


class _Window:
    __slots__ = ("count", "reset_at")

    def __init__(self, reset_at: float) -> None:
        self.count = 0
        self.reset_at = reset_at


class IPRateLimiter:
    def __init__(self, limit_per_minute: int, max_tracked_ips: int = 50_000) -> None:
        self._limit = max(1, limit_per_minute)
        self._window_seconds = 60.0
        self._max_ips = max_tracked_ips
        self._windows: OrderedDict[str, _Window] = OrderedDict()
        self._lock = Lock()

    def check(self, ip: str) -> tuple[bool, int, int]:
        """Returns (allowed, remaining, reset_in_seconds)."""
        now = time.monotonic()
        with self._lock:
            w = self._windows.get(ip)
            if w is None or now >= w.reset_at:
                w = _Window(reset_at=now + self._window_seconds)
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


def _client_ip(scope: Scope, trusted_proxies: set[str]) -> str:
    client = scope.get("client") or ("unknown", 0)
    peer_ip = client[0] if client else "unknown"

    if peer_ip in trusted_proxies:
        for name, value in scope.get("headers") or []:
            if name == b"x-real-ip":
                return value.decode("latin-1").strip() or peer_ip
            if name == b"x-forwarded-for":
                first = value.decode("latin-1").split(",")[0].strip()
                if first:
                    return first
    return peer_ip


class RateLimitMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        limit_per_minute: int,
        trusted_proxies: list[str] | None = None,
    ) -> None:
        self.app = app
        self.limiter = IPRateLimiter(limit_per_minute)
        self.trusted_proxies = set(trusted_proxies or ["127.0.0.1", "::1"])
        self.limit = limit_per_minute

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path in _BYPASS_PATHS:
            await self.app(scope, receive, send)
            return

        ip = _client_ip(scope, self.trusted_proxies)
        allowed, remaining, reset_in = self.limiter.check(ip)

        if not allowed:
            body = json.dumps(
                {
                    "error": "rate_limit_exceeded",
                    "message": (
                        f"Free tier limit of {self.limit} requests/minute exceeded. "
                        f"Retry in {reset_in}s."
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
            await send({"type": "http.response.body", "body": body})
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                headers.extend(
                    [
                        (b"ratelimit-limit", str(self.limit).encode()),
                        (b"ratelimit-remaining", str(remaining).encode()),
                        (b"ratelimit-reset", str(reset_in).encode()),
                    ]
                )
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)
