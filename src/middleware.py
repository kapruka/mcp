"""Per-IP rate-limit ASGI middleware.

Fixed-window counter keyed on client IP. We sit behind Caddy, which sets
X-Real-IP — we only trust that header when the request comes from a
configured trusted proxy IP (defaults to localhost).

Requests carrying a valid partner key (scope["kapruka.identity"], set by
PartnerAuthMiddleware) are counted per partner instead of per IP — partner
traffic arrives from shared Cloudflare IPs — plus, when the partner sends
X-Partner-Customer-Id, per customer within that partner. Both caps must
allow a request; one that is refused consumes neither.

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

from src import partners

_BYPASS_PATHS = {"/health", "/ready"}


class _Window:
    __slots__ = ("count", "reset_at")

    def __init__(self, reset_at: float) -> None:
        self.count = 0
        self.reset_at = reset_at


class IPRateLimiter:
    def __init__(
        self, limit_per_minute: int, max_tracked_ips: int = 50_000, window_seconds: float = 60.0
    ) -> None:
        self._limit = max(1, limit_per_minute)
        self._window_seconds = window_seconds
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

    def peek(self, key: str) -> tuple[bool, int, int]:
        """Like check(), without counting the request."""
        now = time.monotonic()
        with self._lock:
            w = self._windows.get(key)
            if w is None or now >= w.reset_at:
                return True, self._limit, int(self._window_seconds)
            return w.count < self._limit, max(0, self._limit - w.count), max(0, int(w.reset_at - now))

    @property
    def limit(self) -> int:
        return self._limit


def acquire_all(checks: list[tuple["IPRateLimiter", str, str]]) -> tuple[bool, str, int, int, int]:
    """Count a request against several (limiter, key, label) buckets at once.

    Returns (allowed, label, limit, remaining, reset_in). If any bucket is full
    nothing is counted and the full one is reported; otherwise all are counted
    and the tightest one is reported.
    """
    for limiter, key, label in checks:
        ok, _, reset_in = limiter.peek(key)
        if not ok:
            return False, label, limiter.limit, 0, reset_in
    best = None
    for limiter, key, label in checks:
        _, remaining, reset_in = limiter.check(key)
        if best is None or remaining < best[3]:
            best = (True, label, limiter.limit, remaining, reset_in)
    return best


class PartnerBuckets:
    """Per-partner and per-(partner, customer) limiters, built on demand so a
    partner's own configured limits are used. Shared shape for both limiters."""

    def __init__(self, window_seconds: float, partner_attr: str, customer_attr: str, unit: str) -> None:
        self._window = window_seconds
        self._partner_attr, self._customer_attr, self.unit = partner_attr, customer_attr, unit
        self._limiters: dict[tuple[str, str, int], IPRateLimiter] = {}
        self._lock = Lock()

    def _limiter(self, kind: str, partner: str, limit: int) -> IPRateLimiter:
        key = (kind, partner, limit)
        with self._lock:
            lim = self._limiters.get(key)
            if lim is None:
                lim = self._limiters[key] = IPRateLimiter(limit, window_seconds=self._window)
            return lim

    def checks(self, ident: "partners.Identity", cfg: "partners.PartnerConfig"):
        lim = cfg.limits_for(ident.partner)
        out = [(self._limiter("partner", ident.partner, getattr(lim, self._partner_attr)),
                ident.partner, "Partner")]
        if ident.customer_id:
            out.append((self._limiter("customer", ident.partner, getattr(lim, self._customer_attr)),
                        ident.customer_id, "Per-customer"))
        return out


def _client_ip(scope: Scope, trusted_proxies: set[str]) -> str:
    client = scope.get("client") or ("unknown", 0)
    peer_ip = client[0] if client else "unknown"

    if peer_ip in trusted_proxies:
        cf_ip = real_ip = xff_ip = None
        for name, value in scope.get("headers") or []:
            if name == b"cf-connecting-ip":
                cf_ip = value.decode("latin-1").strip()
            elif name == b"x-real-ip":
                real_ip = value.decode("latin-1").strip()
            elif name == b"x-forwarded-for":
                xff_ip = value.decode("latin-1").split(",")[0].strip()
        # CF-Connecting-IP is the real client behind Cloudflare; X-Real-IP may
        # only be the CF edge that Caddy saw. Same precedence as activity_log
        # and the order limiter.
        return cf_ip or real_ip or xff_ip or peer_ip
    return peer_ip


class RateLimitMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        limit_per_minute: int,
        trusted_proxies: list[str] | None = None,
        exempt_ips: list[str] | None = None,
        trusted_limit_per_minute: int = 600,
    ) -> None:
        self.app = app
        self.limiter = IPRateLimiter(limit_per_minute)
        self.trusted_proxies = set(trusted_proxies or ["127.0.0.1", "::1"])
        # "Exempt" IPs get the trusted tier: a separate, much higher window
        # rather than no limit at all, so a runaway loop on a first-party box
        # still gets stopped instead of hammering the service unbounded.
        self.exempt_ips = set(exempt_ips or [])
        self.trusted_limiter = IPRateLimiter(trusted_limit_per_minute)
        self.trusted_limit = trusted_limit_per_minute
        self.limit = limit_per_minute
        self.partner_buckets = PartnerBuckets(60.0, "rpm", "customer_rpm", "requests/minute")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path in _BYPASS_PATHS:
            await self.app(scope, receive, send)
            return

        ident = partners.identity_from_scope(scope)
        if ident is not None:
            allowed, tier, limit, remaining, reset_in = acquire_all(
                self.partner_buckets.checks(ident, partners.current()))
            tier = f"{tier} tier" if tier == "Partner" else f"{tier} (within partner)"
        else:
            ip = _client_ip(scope, self.trusted_proxies)
            trusted = ip in self.exempt_ips
            limit = self.trusted_limit if trusted else self.limit
            limiter = self.trusted_limiter if trusted else self.limiter
            allowed, remaining, reset_in = limiter.check(ip)
            tier = "Trusted tier" if trusted else "Free tier"

        if not allowed:
            body = json.dumps(
                {
                    "error": "rate_limit_exceeded",
                    "message": (
                        f"{tier} limit of {limit} requests/minute exceeded. "
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
                        (b"ratelimit-limit", str(limit).encode()),
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
                        (b"ratelimit-limit", str(limit).encode()),
                        (b"ratelimit-remaining", str(remaining).encode()),
                        (b"ratelimit-reset", str(reset_in).encode()),
                    ]
                )
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)
