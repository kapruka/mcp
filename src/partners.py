"""Trusted-partner configuration and key authentication.

Partners are first-party callers that can't be IP-allowlisted — e.g. "aloka",
Kapruka's web shopping assistant on Cloudflare Workers, whose outbound IPs are
Cloudflare's shared pool (allowlisting those would trust every Worker on the
internet). They authenticate with `X-Partner-Key` instead.

Configuration (all optional; with none of it set nothing changes):

    PARTNER_KEYS    name:sha256:<64 hex>  or  name:<raw key>, separated by ';' or ','.
                    A partner may have several entries (key rotation).
    PARTNER_SCOPES  name:group,group;name2:group   (default: no groups — fails closed)
    PARTNER_LIMITS  name:rpm=600,orders_per_hour=300,customer_rpm=60,customer_orders_per_hour=10;...
    PARTNER_RATE_LIMIT_PER_MINUTE, PARTNER_ORDER_RATE_LIMIT_PER_HOUR,
    PARTNER_CUSTOMER_RATE_LIMIT_PER_MINUTE, PARTNER_CUSTOMER_ORDER_RATE_LIMIT_PER_HOUR
                    defaults for partners without an override (600, 300, 60, 10).

The raw key is never stored, logged or put in an exception. A presented key is
hashed and compared with hmac.compare_digest against every configured hash, so
neither the result nor the timing says which entry (if any) matched; an
invalid key is simply "no partner", indistinguishable from a missing one.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Mapping, Optional

logger = logging.getLogger(__name__)

# Hidden-tool groups a partner can be granted. The tools themselves check
# membership via src.private_access; tools/list uses it to show them.
TOOL_GROUPS: dict[str, frozenset[str]] = {
    "visual_search": frozenset({"kapruka_visual_search"}),
    "custom_cake": frozenset({
        "kapruka_custom_cake_options",
        "kapruka_custom_cake_request",
        "kapruka_custom_cake_status",
    }),
}

KEY_HEADER = b"x-partner-key"
CUSTOMER_HEADER = b"x-partner-customer-id"

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_CUSTOMER_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_MIN_RAW_KEY = 24
_MAX_PRESENTED_KEY = 256

_LIMIT_FIELDS = ("rpm", "orders_per_hour", "customer_rpm", "customer_orders_per_hour")


@dataclass(frozen=True)
class PartnerLimits:
    rpm: int = 600
    orders_per_hour: int = 300
    customer_rpm: int = 60
    customer_orders_per_hour: int = 10


@dataclass(frozen=True)
class Identity:
    """Who a request is, as far as partner access goes. Holds no secret:
    `key_fp` is the SHA-256 of the key — the same value stored in config —
    used only to bind an MCP session to the key it was created with."""

    partner: str
    customer_id: Optional[str]
    key_fp: str = field(repr=False)


@dataclass
class PartnerConfig:
    key_hashes: list[tuple[str, str]] = field(default_factory=list)  # (partner, sha256 hex)
    scopes: dict[str, frozenset[str]] = field(default_factory=dict)
    limits: dict[str, PartnerLimits] = field(default_factory=dict)
    default_limits: PartnerLimits = field(default_factory=PartnerLimits)

    @property
    def enabled(self) -> bool:
        return bool(self.key_hashes)

    def limits_for(self, partner: str) -> PartnerLimits:
        return self.limits.get(partner, self.default_limits)

    def scopes_for(self, partner: str) -> frozenset[str]:
        return self.scopes.get(partner, frozenset())

    def authenticate(self, presented: Optional[str]) -> Optional[tuple[str, str]]:
        """(partner, key_fp) for a valid key, else None. Never raises, never logs."""
        if not presented or not self.key_hashes:
            return None
        presented = presented.strip()
        if not presented or len(presented) > _MAX_PRESENTED_KEY:
            return None
        digest = hashlib.sha256(presented.encode("utf-8")).hexdigest()
        match: Optional[tuple[str, str]] = None
        for partner, stored in self.key_hashes:  # every entry, no early exit
            if hmac.compare_digest(digest, stored) and match is None:
                match = (partner, stored)
        return match


def valid_customer_id(value: Optional[str]) -> Optional[str]:
    if value and _CUSTOMER_RE.match(value):
        return value
    return None


# ── parsing ──────────────────────────────────────────────────────────────────


def _entries(raw: str, seps: str) -> list[str]:
    return [e.strip() for e in re.split(f"[{re.escape(seps)}]", raw or "") if e.strip()]


def _parse_keys(raw: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for entry in _entries(raw, ";,"):
        name, sep, rest = entry.partition(":")
        name = name.strip().lower()
        if not sep or not _NAME_RE.match(name):
            logger.warning("partners: skipped a PARTNER_KEYS entry with no valid partner name")
            continue
        rest = rest.strip()
        if rest.lower().startswith("sha256:"):
            h = rest[7:].strip().lower()
            if not _HEX64_RE.match(h):
                logger.warning("partners: %s: skipped a sha256 entry that is not 64 hex chars", name)
                continue
            out.append((name, h))
        elif len(rest) >= _MIN_RAW_KEY:
            out.append((name, hashlib.sha256(rest.encode("utf-8")).hexdigest()))
        else:
            logger.warning("partners: %s: skipped a raw key shorter than %d chars", name, _MIN_RAW_KEY)
    return out


def _parse_scopes(raw: str) -> dict[str, frozenset[str]]:
    out: dict[str, frozenset[str]] = {}
    for entry in _entries(raw, ";"):
        name, sep, rest = entry.partition(":")
        name = name.strip().lower()
        if not sep or not _NAME_RE.match(name):
            logger.warning("partners: skipped a PARTNER_SCOPES entry with no valid partner name")
            continue
        groups = set()
        for g in _entries(rest, ","):
            g = g.lower()
            if g in TOOL_GROUPS:
                groups.add(g)
            else:
                logger.warning("partners: %s: unknown tool group %r ignored (known: %s)",
                               name, g, ", ".join(sorted(TOOL_GROUPS)))
        out[name] = frozenset(out.get(name, frozenset()) | groups)
    return out


def _parse_limits(raw: str, default: PartnerLimits) -> dict[str, PartnerLimits]:
    out: dict[str, PartnerLimits] = {}
    for entry in _entries(raw, ";"):
        name, sep, rest = entry.partition(":")
        name = name.strip().lower()
        if not sep or not _NAME_RE.match(name):
            logger.warning("partners: skipped a PARTNER_LIMITS entry with no valid partner name")
            continue
        values = {f: getattr(default, f) for f in _LIMIT_FIELDS}
        for kv in _entries(rest, ","):
            k, _, v = kv.partition("=")
            k = k.strip().lower()
            try:
                n = int(v.strip())
            except ValueError:
                n = 0
            if k in values and n > 0:
                values[k] = n
            else:
                logger.warning("partners: %s: ignored limit %r", name, kv)
        out[name] = PartnerLimits(**values)
    return out


def _int_env(env: Mapping[str, str], key: str, default: int) -> int:
    try:
        v = int(env.get(key, "") or default)
        return v if v > 0 else default
    except ValueError:
        return default


def load(env: Mapping[str, str] | None = None) -> PartnerConfig:
    env = os.environ if env is None else env
    default = PartnerLimits(
        rpm=_int_env(env, "PARTNER_RATE_LIMIT_PER_MINUTE", 600),
        orders_per_hour=_int_env(env, "PARTNER_ORDER_RATE_LIMIT_PER_HOUR", 300),
        customer_rpm=_int_env(env, "PARTNER_CUSTOMER_RATE_LIMIT_PER_MINUTE", 60),
        customer_orders_per_hour=_int_env(env, "PARTNER_CUSTOMER_ORDER_RATE_LIMIT_PER_HOUR", 10),
    )
    cfg = PartnerConfig(
        key_hashes=_parse_keys(env.get("PARTNER_KEYS", "")),
        scopes=_parse_scopes(env.get("PARTNER_SCOPES", "")),
        limits=_parse_limits(env.get("PARTNER_LIMITS", ""), default),
        default_limits=default,
    )
    if cfg.enabled:
        names = sorted({p for p, _ in cfg.key_hashes})
        logger.info("partners: %s", "; ".join(
            f"{p} ({sum(1 for q, _ in cfg.key_hashes if q == p)} key(s), "
            f"scopes: {', '.join(sorted(cfg.scopes_for(p))) or 'none'})" for p in names))
    return cfg


# ── the active config (one per process; tests swap it) ──────────────────────

_current: Optional[PartnerConfig] = None


def current() -> PartnerConfig:
    global _current
    if _current is None:
        _current = load()
    return _current


def set_current(cfg: Optional[PartnerConfig]) -> None:
    global _current
    _current = cfg


def identity_from_scope(scope: Mapping | None) -> Optional[Identity]:
    """The partner identity PartnerAuthMiddleware attached, if any."""
    if not scope:
        return None
    ident = scope.get("kapruka.identity")
    return ident if isinstance(ident, Identity) else None
