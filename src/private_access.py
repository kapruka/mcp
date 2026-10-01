"""Closed-MCP gate for private tools.

Private tools (custom cakes, visual search) are hidden from the public
tools/list and only answer trusted callers:

    trusted = (caller IP on the tool's allow-list)          — e.g. the eagle box
           OR (valid partner key whose scopes include the tool's group)

The partner side is decided once by PartnerAuthMiddleware and read from the
request scope here; this module never looks at the key header itself. The MCP
app listens on 127.0.0.1 behind local Caddy (which sets X-Real-IP from
Cloudflare's CF-Connecting-IP), so the forwarded IP headers are trustworthy.
"""

from __future__ import annotations

import logging
from typing import Iterable

from src import partners

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


def _request_of(ctx):
    try:
        return ctx.request_context.request if ctx is not None else None
    except Exception:  # no request context (stdio transport, unit tests without one)
        return None


def partner_identity(ctx) -> "partners.Identity | None":
    """The partner identity of the HTTP request behind `ctx`, if any."""
    request = _request_of(ctx)
    return partners.identity_from_scope(getattr(request, "scope", None))


def is_trusted_caller(ctx, allowed: Iterable[str], family: str) -> bool:
    """True when the MCP request behind `ctx` comes from an allowed IP, or from a
    partner whose scopes include `family` (a key of partners.TOOL_GROUPS).

    Fails closed: an empty allow-list and no partner scope, or no HTTP request in
    the context (stdio transport, unit tests without one), means nobody gets in.
    """
    request = _request_of(ctx)
    allowed = set(allowed)
    if allowed and client_ip_from_request(request) in allowed:
        return True
    ident = partners.identity_from_scope(getattr(request, "scope", None))
    if ident is not None and family in partners.current().scopes_for(ident.partner):
        return True
    if ident is not None:
        logger.info("%s: denied partner=%s (not in its scopes)", family, ident.partner)
    else:
        logger.info("%s: denied ip=%s", family, client_ip_from_request(request))
    return False
