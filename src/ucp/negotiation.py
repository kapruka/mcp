"""UCP negotiation: resolve the agent's (platform's) profile, check the protocol
version, and intersect capabilities (UCP overview, "Negotiation Protocol").

The platform names its profile on every request — `UCP-Agent: profile="…"`
(REST) or `meta["ucp-agent"].profile` (MCP). We must fetch it (or use a cached
copy) before acting. Fetching follows the spec's safety rules: HTTPS only, no
redirects, every resolved address must be public (no loopback, private,
link-local/cloud-metadata or other special-use ranges), bounded time and size,
an LRU cache with a 60 s TTL floor, and a global budget on discovery fetches so
unknown agents cannot make us fetch without limit.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
import socket
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlsplit

import httpx

from src.config.settings import settings
from src.ucp import STOREFRONT, VERSION
from src.ucp.profile import CAPABILITIES

logger = logging.getLogger(__name__)

_FETCH_TIMEOUT = 5.0
_MAX_PROFILE_BYTES = 256 * 1024
_TTL_FLOOR, _TTL_CEILING, _TTL_DEFAULT = 60.0, 3600.0, 300.0
_FAILURE_TTL = 60.0            # back off from a profile that just failed
_CACHE_SIZE = 512
_FETCH_BUDGET_PER_MINUTE = 60  # unknown-profile fetches, all agents together

# Tests swap in an httpx.MockTransport; production uses the default.
_TRANSPORT: Optional[httpx.AsyncBaseTransport] = None


class UcpError(Exception):
    """A negotiation or protocol failure, carried to the transport layer.

    `kind` decides how each transport reports it:
      - "transport": REST status `http_status` with {code, content, continue_url};
        MCP JSON-RPC error `rpc_code` with the same in `data`.
      - "outcome":   a UCP error response (ucp.status "error" + messages), REST 200
        unless `http_status` says otherwise, MCP `result.structuredContent`.
    """

    def __init__(self, code: str, content: str, *, kind: str = "transport", http_status: Optional[int] = None,
                 rpc_code: int = -32001, severity: str = "unrecoverable",
                 continue_url: Optional[str] = STOREFRONT, retry_after: Optional[int] = None):
        super().__init__(f"{code}: {content}")
        self.code, self.content, self.kind = code, content, kind
        if http_status is None:
            http_status = 200 if kind == "outcome" else 400
        self.http_status, self.rpc_code, self.severity = http_status, rpc_code, severity
        self.continue_url, self.retry_after = continue_url, retry_after

    def body(self) -> dict:
        if self.kind == "outcome":
            return error_response(self.code, self.content, severity=self.severity,
                                  continue_url=self.continue_url)
        out = {"code": self.code, "content": self.content}
        if self.continue_url:
            out["continue_url"] = self.continue_url
        if self.retry_after:
            out["retry_after"] = self.retry_after
        return out


def error_response(code: str, content: str, *, severity: str = "unrecoverable",
                   continue_url: Optional[str] = STOREFRONT, path: Optional[str] = None) -> dict:
    """A UCP error response: no resource, just why (common/types/error_response.json)."""
    msg = {"type": "error", "code": code, "content": content, "severity": severity}
    if path:
        msg["path"] = path
    out = {"ucp": {"version": VERSION, "status": "error"}, "messages": [msg]}
    if continue_url:
        out["continue_url"] = continue_url
    return out


@dataclass
class PlatformProfile:
    url: str
    version: str
    capabilities: dict[str, list[dict]] = field(default_factory=dict)


@dataclass
class Negotiated:
    """The active capability set for one request."""

    platform: PlatformProfile
    active: set[str]

    def ucp_block(self, root: str, *, payment_handlers: bool = False) -> dict:
        """The response's `ucp` member: version + capabilities relevant to `root`."""
        relevant = {
            name for name in self.active
            if name == root or _parents(name) & {root}
        }
        block: dict = {
            "version": VERSION,
            "status": "success",
            "capabilities": {name: [{"version": VERSION}] for name in sorted(relevant)},
        }
        if payment_handlers:
            block["payment_handlers"] = {}
        return block


def _parents(name: str) -> set[str]:
    ext = CAPABILITIES.get(name, {}).get("extends")
    if ext is None:
        return set()
    return {ext} if isinstance(ext, str) else set(ext)


def intersect(platform: PlatformProfile) -> set[str]:
    """Overview "Intersection Algorithm": same name and an exact shared version
    (only VERSION is ours), then prune extensions whose parents are gone."""
    active = {
        name for name in CAPABILITIES
        if any(isinstance(e, dict) and e.get("version") == VERSION
               for e in platform.capabilities.get(name) or [])
    }
    changed = True
    while changed:
        changed = False
        for name in list(active):
            parents = _parents(name)
            if parents and not parents & active:
                active.discard(name)
                changed = True
    return active


# ── Profile URL from the request ─────────────────────────────────────────────

_SF_PROFILE = re.compile(r'(?:^|[,;\s])profile\s*=\s*"([^"]+)"')


def profile_url_from_header(value: Optional[str]) -> Optional[str]:
    """`UCP-Agent: profile="https://…"` (RFC 8941 dictionary)."""
    if not value:
        return None
    m = _SF_PROFILE.search(value)
    return m.group(1).strip() if m else None


def profile_url_from_meta(meta) -> Optional[str]:
    if not isinstance(meta, dict):
        return None
    agent = meta.get("ucp-agent")
    if isinstance(agent, dict) and isinstance(agent.get("profile"), str):
        return agent["profile"].strip()
    return None


# ── Fetching ─────────────────────────────────────────────────────────────────


class _Cache:
    def __init__(self, size: int):
        self.size = size
        self.items: OrderedDict[str, tuple[float, object]] = OrderedDict()

    def get(self, key: str):
        hit = self.items.get(key)
        if hit is None:
            return None
        expires, value = hit
        if expires < time.monotonic():
            self.items.pop(key, None)
            return None
        self.items.move_to_end(key)
        return value

    def put(self, key: str, value, ttl: float) -> None:
        self.items[key] = (time.monotonic() + ttl, value)
        self.items.move_to_end(key)
        while len(self.items) > self.size:
            self.items.popitem(last=False)

    def clear(self) -> None:
        self.items.clear()


_cache = _Cache(_CACHE_SIZE)
_budget: list[float] = []
_locks: dict[str, asyncio.Lock] = {}


def _discovery_error(code: str, content: str, status: int) -> UcpError:
    return UcpError(code, content, kind="transport", http_status=status, rpc_code=-32001)


def _check_url(url: str) -> tuple[str, int]:
    try:
        parts = urlsplit(url)
    except ValueError:
        raise _discovery_error("invalid_profile_url", "The agent profile URL is malformed.", 400)
    secure = parts.scheme == "https" or (settings.ucp_allow_insecure_profiles and parts.scheme == "http")
    if not secure or not parts.hostname or parts.username or parts.password:
        raise _discovery_error("invalid_profile_url",
                               "The agent profile URL must be an https:// URL without credentials.", 400)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return parts.hostname, port


async def _check_addresses(host: str, port: int) -> None:
    """Every address the host resolves to must be public (RFC 6890)."""
    if settings.ucp_allow_insecure_profiles:
        return
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        raise _discovery_error("profile_unreachable", f"Could not resolve {host}.", 424)
    if not infos:
        raise _discovery_error("profile_unreachable", f"Could not resolve {host}.", 424)
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global:
            raise _discovery_error("invalid_profile_url",
                                   "The agent profile URL resolves to a non-public address.", 400)


def _take_budget() -> bool:
    now = time.monotonic()
    while _budget and _budget[0] < now - 60:
        _budget.pop(0)
    if len(_budget) >= _FETCH_BUDGET_PER_MINUTE:
        return False
    _budget.append(now)
    return True


def _ttl_from(cache_control: Optional[str]) -> float:
    m = re.search(r"max-age\s*=\s*(\d+)", cache_control or "")
    ttl = float(m.group(1)) if m else _TTL_DEFAULT
    return max(_TTL_FLOOR, min(_TTL_CEILING, ttl))


def _parse(url: str, raw: bytes) -> PlatformProfile:
    try:
        doc = json.loads(raw)
    except ValueError:
        raise _discovery_error("profile_malformed", "The agent profile is not valid JSON.", 422)
    ucp = doc.get("ucp") if isinstance(doc, dict) else None
    version = ucp.get("version") if isinstance(ucp, dict) else None
    if not isinstance(version, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", version):
        raise _discovery_error("profile_malformed", "The agent profile has no valid ucp.version.", 422)
    caps = ucp.get("capabilities") or {}
    if not isinstance(caps, dict):
        raise _discovery_error("profile_malformed", "The agent profile's ucp.capabilities is not an object.", 422)
    return PlatformProfile(url=url, version=version,
                           capabilities={k: v for k, v in caps.items() if isinstance(v, list)})


async def _fetch(url: str) -> tuple[PlatformProfile, float]:
    host, port = _check_url(url)
    await _check_addresses(host, port)
    try:
        async with httpx.AsyncClient(timeout=_FETCH_TIMEOUT, follow_redirects=False,
                                     transport=_TRANSPORT) as client:
            async with client.stream("GET", url, headers={
                "Accept": "application/json",
                "User-Agent": "kapruka-ucp/1.0 (+https://www.kapruka.com/.well-known/ucp)",
            }) as resp:
                if resp.status_code != 200:
                    raise _discovery_error(
                        "profile_unreachable",
                        f"Fetching the agent profile returned HTTP {resp.status_code}"
                        + (" (redirects are not followed)" if 300 <= resp.status_code < 400 else "") + ".",
                        424)
                body = bytearray()
                async for chunk in resp.aiter_bytes():
                    body += chunk
                    if len(body) > _MAX_PROFILE_BYTES:
                        raise _discovery_error("profile_malformed", "The agent profile is too large.", 422)
                ttl = _ttl_from(resp.headers.get("cache-control"))
    except httpx.HTTPError as e:
        raise _discovery_error("profile_unreachable",
                               f"Could not fetch the agent profile ({type(e).__name__}).", 424)
    return _parse(url, bytes(body)), ttl


async def resolve(url: Optional[str]) -> PlatformProfile:
    if not url:
        raise _discovery_error("invalid_profile_url",
                               "Missing agent profile: send UCP-Agent: profile=\"https://…\" "
                               "(REST) or meta[\"ucp-agent\"].profile (MCP).", 400)
    cached = _cache.get(url)
    if isinstance(cached, PlatformProfile):
        return cached
    if isinstance(cached, UcpError):
        raise cached
    lock = _locks.setdefault(url, asyncio.Lock())
    async with lock:
        cached = _cache.get(url)
        if isinstance(cached, PlatformProfile):
            return cached
        if not _take_budget():
            raise UcpError("profile_unreachable",
                           "Agent profile discovery is busy; retry shortly.",
                           http_status=503, rpc_code=-32000, retry_after=10)
        try:
            profile, ttl = await _fetch(url)
        except UcpError as e:
            _cache.put(url, e, _FAILURE_TTL)
            logger.info("ucp: profile %s rejected (%s)", url, e.code)
            raise
        finally:
            _locks.pop(url, None)
        _cache.put(url, profile, ttl)
        return profile


async def negotiate(url: Optional[str], root: str) -> Negotiated:
    """Resolve the agent, check its version, and require `root` to be active."""
    platform = await resolve(url)
    if platform.version != VERSION:
        raise UcpError(
            "version_unsupported",
            f"Protocol version {platform.version} is not supported. Kapruka implements {VERSION}.",
            http_status=422, rpc_code=-32001)
    active = intersect(platform)
    if root not in active:
        raise UcpError("capabilities_incompatible",
                       f"No compatible capabilities: {root} at version {VERSION} is required.",
                       kind="outcome", http_status=200)
    return Negotiated(platform=platform, active=active)
