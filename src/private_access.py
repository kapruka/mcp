"""Closed-MCP gate for private tools.

Private tools (custom cakes, visual search) are hidden from tools/list and only
answer callers whose real IP is on a trusted list — in practice the eagle box
that runs Kapruka's own sales agent. The MCP app listens on 127.0.0.1 behind
local Caddy (which sets X-Real-IP from Cloudflare's CF-Connecting-IP), so the
forwarded headers are trustworthy here.
"""

from __future__ import annotations

import logging
from typing import Iterable

logger = logging.getLogger(__name__)


def client_ip_from_request(request) -> str:
    """CF-Connecting-IP > X-Real-IP > XFF[0] > peer — same precedence as the limiters."""
    if request is None:
        return "unknown"
    h = request.headers
    for name in ("cf-connecting-ip", "x-real-ip"):
        v = (h.get(name) or "").strip()
        if v:
            return v
    xff = (h.get("x-forwarded-for") or "").split(",")[0].strip()
    if xff:
        return xff
    client = getattr(request, "client", None)
    return client.host if client and client.host else "unknown"


def is_trusted_caller(ctx, allowed: Iterable[str], family: str) -> bool:
    """True when the MCP request behind `ctx` comes from an allowed IP.

    Fails closed: an empty allow-list, or no HTTP request in the context (stdio
    transport, unit tests without one), means nobody gets in.
    """
    allowed = set(allowed)
    if not allowed:
        return False
    try:
        request = ctx.request_context.request if ctx is not None else None
    except Exception:
        request = None
    ip = client_ip_from_request(request)
    if ip in allowed:
        return True
    logger.info("%s: denied ip=%s", family, ip)
    return False
