"""Trusted-partner access: X-Partner-Key alongside the IP allowlist.

Most tests drive the real MCP app (build_app + its lifespan) through
httpx.ASGITransport, so sessions, middleware order, tool gating, tools/list
and logging are exercised exactly as in production. Upstreams (Eagle visual
search, Kapruka commerce API) are stubbed.

All keys here are throwaway test values.
"""

from __future__ import annotations

import hashlib
import json
import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest

from src import partners, server
from src.activity_log import _BASE_COLUMNS, _INSERT_SQL, _PARTNER_COLUMNS, _insert_sql
from src.config.settings import settings
from src.partner_auth import PartnerAuthMiddleware
from src.server import build_app, mcp
from src.tools import custom_cakes as cc
from src.tools import orders as orders_tool
from src.tools import visual_search as vs

KEY_A = "test-only-aloka-key-A-0123456789abcdef"      # stored as a sha256 hash
KEY_B = "test-only-aloka-key-B-rotated-fedcba987654"   # stored raw (rotation partner)
KEY_Z = "test-only-zeta-key-0000000000000000000000"    # a second partner
ALL_KEYS = (KEY_A, KEY_B, KEY_Z)

EAGLE = "23.111.183.104"
PUBLIC_IP = "203.0.113.9"
WORKER_IP = "104.28.0.10"   # a Cloudflare Workers egress-style address, not allow-listed

HIDDEN_VS = "kapruka_visual_search"
HIDDEN_CC = {"kapruka_custom_cake_options", "kapruka_custom_cake_request", "kapruka_custom_cake_status"}


def _sha(k: str) -> str:
    return hashlib.sha256(k.encode()).hexdigest()


def _config(limits: str = "") -> partners.PartnerConfig:
    return partners.load({
        "PARTNER_KEYS": f"aloka:sha256:{_sha(KEY_A)}; aloka:{KEY_B}, zeta:sha256:{_sha(KEY_Z)}",
        "PARTNER_SCOPES": "aloka:visual_search; zeta:custom_cake",
        "PARTNER_LIMITS": limits,
    })


# ── harness ──────────────────────────────────────────────────────────────────


class FakeActivityLog:
    entries: list[dict] = []

    def __init__(self, dsn: str) -> None:
        pass

    async def ensure_started(self) -> None:
        pass

    def enqueue(self, entry: dict) -> None:
        type(self).entries.append(entry)


class Session:
    def __init__(self, client: httpx.AsyncClient):
        self.c = client
        self.sid: str | None = None

    @staticmethod
    def _headers(ip, key, customer, sid):
        h = {"content-type": "application/json", "accept": "application/json, text/event-stream",
             "cf-connecting-ip": ip}
        if key is not None:
            h["x-partner-key"] = key
        if customer is not None:
            h["x-partner-customer-id"] = customer
        if sid:
            h["mcp-session-id"] = sid
        return h

    async def post(self, payload, *, ip=PUBLIC_IP, key=None, customer=None, sid="__own__"):
        sid = self.sid if sid == "__own__" else sid
        return await self.c.post("/mcp", headers=self._headers(ip, key, customer, sid), json=payload)

    async def open(self, *, ip=PUBLIC_IP, key=None, customer=None):
        r = await self.post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}},
            ip=ip, key=key, customer=customer, sid=None)
        assert r.status_code == 200, r.text
        self.sid = r.headers["mcp-session-id"]
        await self.post({"jsonrpc": "2.0", "method": "notifications/initialized"},
                        ip=ip, key=key, customer=customer)
        return r

    @staticmethod
    def result(r: httpx.Response):
        lines = [l[5:].strip() for l in r.text.splitlines() if l.startswith("data:")]
        return json.loads(lines[0]) if lines else r.json()

    async def tools(self, **kw) -> tuple[httpx.Response, set[str]]:
        r = await self.post({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, **kw)
        if r.status_code != 200:
            return r, set()
        return r, {t["name"] for t in self.result(r)["result"]["tools"]}

    async def call(self, name, args, **kw) -> tuple[httpx.Response, str]:
        r = await self.post({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                             "params": {"name": name, "arguments": {"params": args}}}, **kw)
        if r.status_code != 200:
            return r, r.text
        return r, self.result(r)["result"]["content"][0]["text"]


@pytest.fixture
def stubs(monkeypatch):
    """Upstream stubs and IP allow-lists (Eagle is the only allow-listed IP)."""
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "rate_limit_exempt_ips", [EAGLE])
    monkeypatch.setattr(settings, "visual_search_trusted_ips", [EAGLE])
    monkeypatch.setattr(settings, "custom_cake_trusted_ips", [EAGLE])
    monkeypatch.setattr(settings, "activity_db_url", "postgresql://fake")
    monkeypatch.setattr(server, "ActivityLogger", FakeActivityLog)
    FakeActivityLog.entries = []

    monkeypatch.setattr(settings, "eagle_vs_url", "https://eagle.example/api/vs/search")
    monkeypatch.setattr(settings, "eagle_vs_api_key", "eagle-test")
    monkeypatch.setattr(vs, "_TRANSPORT", httpx.MockTransport(lambda req: httpx.Response(200, json={
        "ok": True, "total": 1, "page": 1, "limit": 10, "categories": [],
        "results": [{"id": "FLOWERS00T1828", "title": "Rose Bouquet", "best_price_lkr": 5940,
                     "price_lkr": 5940, "sale_price_lkr": None, "url": "u", "categories": []}],
        "cache": {}})))

    class FakeCommerce:
        async def post(self, endpoint, body=None, **q):
            if endpoint == "custom_cake_options":
                return {"available": True, "flavours": [], "sizes": [], "icing_colors": []}
            return {"order_ref": "ORD-TEST", "checkout_url": "https://pay.example/x",
                    "summary": {"grand_total": 1, "currency": "LKR"}}
    monkeypatch.setattr(cc, "KaprukaClient", FakeCommerce)
    monkeypatch.setattr(orders_tool, "KaprukaClient", FakeCommerce)


@asynccontextmanager
async def running(cfg: partners.PartnerConfig):
    partners.set_current(cfg)
    mcp._session_manager = None   # StreamableHTTPSessionManager.run() is once per instance
    app = build_app()
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://127.0.0.1:3200") as c:
                yield lambda: Session(c)
    finally:
        mcp._session_manager = None
        partners.set_current(None)


ORDER_ARGS = {
    "cart": [{"product_id": "CAKE00KA001537"}],
    "recipient": {"name": "Test", "phone": "+94771234567"},
    "delivery": {"address": "1 Test Rd", "city": "Colombo 03", "date": "2099-01-01"},
    "sender": {"name": "Test"},
}


# ── scopes: hidden tools for a valid key, only within its scopes ─────────────


@pytest.mark.asyncio
async def test_valid_key_gets_its_scoped_hidden_tools_and_nothing_else(stubs):
    async with running(_config()) as new:
        s = new()
        await s.open(ip=WORKER_IP, key=KEY_A)
        _, names = await s.tools(ip=WORKER_IP, key=KEY_A)
        assert HIDDEN_VS in names
        assert not (HIDDEN_CC & names)
        _, text = await s.call(HIDDEN_VS, {"q": "red roses"}, ip=WORKER_IP, key=KEY_A)
        assert "Rose Bouquet" in text
        _, text = await s.call("kapruka_custom_cake_options", {}, ip=WORKER_IP, key=KEY_A)
        assert "closed MCP" in text


@pytest.mark.asyncio
async def test_a_second_partner_gets_only_its_own_scope(stubs):
    async with running(_config()) as new:
        s = new()
        await s.open(ip=WORKER_IP, key=KEY_Z)
        _, names = await s.tools(ip=WORKER_IP, key=KEY_Z)
        assert HIDDEN_CC <= names and HIDDEN_VS not in names
        _, text = await s.call("kapruka_custom_cake_options", {}, ip=WORKER_IP, key=KEY_Z)
        assert "available choices" in text
        _, text = await s.call(HIDDEN_VS, {"q": "red roses"}, ip=WORKER_IP, key=KEY_Z)
        assert "private MCP" in text


@pytest.mark.asyncio
async def test_partner_without_scopes_gets_no_hidden_tools(stubs):
    cfg = partners.load({"PARTNER_KEYS": f"aloka:sha256:{_sha(KEY_A)}"})   # no PARTNER_SCOPES
    async with running(cfg) as new:
        s = new()
        await s.open(ip=WORKER_IP, key=KEY_A)
        _, names = await s.tools(ip=WORKER_IP, key=KEY_A)
        assert HIDDEN_VS not in names
        _, text = await s.call(HIDDEN_VS, {"q": "red roses"}, ip=WORKER_IP, key=KEY_A)
        assert "private MCP" in text


# ── invalid / missing / empty keys are just public ───────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("key", [None, "", "   ", "not-a-real-key-but-long-enough-000", "x" * 5000])
async def test_invalid_missing_or_empty_key_behaves_as_public(stubs, key):
    async with running(_config()) as new:
        s = new()
        r = await s.open(ip=PUBLIC_IP, key=key)
        assert r.headers["ratelimit-limit"] == "60"
        r, names = await s.tools(ip=PUBLIC_IP, key=key)
        assert r.status_code == 200 and len(names) == 8 and HIDDEN_VS not in names
        _, text = await s.call(HIDDEN_VS, {"q": "red roses"}, ip=PUBLIC_IP, key=key)
        assert "private MCP" in text


# ── rotation ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rotation_both_keys_are_valid_at_once(stubs):
    async with running(_config()) as new:
        for key in (KEY_A, KEY_B):
            s = new()
            r = await s.open(ip=WORKER_IP, key=key)
            assert r.headers["ratelimit-limit"] == "600", "partner tier, not the public 60"
            _, names = await s.tools(ip=WORKER_IP, key=key)
            assert HIDDEN_VS in names


# ── rate limits ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_partner_wide_request_cap_is_shared_across_ips_and_sessions(stubs):
    async with running(_config("aloka:rpm=6,customer_rpm=100")) as new:
        a, b = new(), new()
        await a.open(ip="104.28.0.1", key=KEY_A)                # 2 requests
        await b.open(ip="104.28.0.2", key=KEY_A)                # 4
        assert (await a.tools(ip="104.28.0.3", key=KEY_A))[0].status_code == 200   # 5
        assert (await b.tools(ip="104.28.0.4", key=KEY_A))[0].status_code == 200   # 6
        r, _ = await a.tools(ip="104.28.0.5", key=KEY_A)                           # 7
        assert r.status_code == 429
        assert "Partner tier limit of 6 requests/minute" in r.json()["message"]
        # the public is untouched by the partner's bucket
        p = new()
        assert (await p.open(ip=PUBLIC_IP)).status_code == 200


@pytest.mark.asyncio
async def test_per_customer_request_cap_within_the_partner(stubs):
    async with running(_config("aloka:rpm=100,customer_rpm=3")) as new:
        s = new()
        await s.open(ip=WORKER_IP, key=KEY_A)                   # no customer id: partner bucket only
        for _ in range(3):
            assert (await s.tools(ip=WORKER_IP, key=KEY_A, customer="cust_1"))[0].status_code == 200
        r, _ = await s.tools(ip=WORKER_IP, key=KEY_A, customer="cust_1")
        assert r.status_code == 429 and "Per-customer" in r.json()["message"]
        assert (await s.tools(ip=WORKER_IP, key=KEY_A, customer="cust_2"))[0].status_code == 200
        assert (await s.tools(ip=WORKER_IP, key=KEY_A))[0].status_code == 200, \
            "without the header only the partner-wide cap applies"


@pytest.mark.asyncio
async def test_create_order_caps_per_customer_then_per_partner(stubs):
    async with running(_config("aloka:orders_per_hour=3,customer_orders_per_hour=2")) as new:
        s = new()
        await s.open(ip=WORKER_IP, key=KEY_A)
        order = lambda c: s.call("kapruka_create_order", ORDER_ARGS, ip=WORKER_IP, key=KEY_A, customer=c)
        for _ in range(2):
            r, text = await order("cust_1")
            assert r.status_code == 200 and "ORD-TEST" in text
        r, _ = await order("cust_1")
        assert r.status_code == 429 and "per customer" in r.json()["message"]
        r, _ = await order("cust_2")                     # partner total: 3 of 3
        assert r.status_code == 200
        r, _ = await order("cust_2")
        assert r.status_code == 429 and "Order limit of 3/hour per partner" in r.json()["message"]


@pytest.mark.asyncio
async def test_customer_header_is_ignored_without_a_valid_key(stubs):
    async with running(_config()) as new:
        s = new()
        r = await s.open(ip=PUBLIC_IP, customer="cust_1")
        assert r.headers["ratelimit-limit"] == "60"
        remaining = int(r.headers["ratelimit-remaining"])
        r, _ = await s.tools(ip=PUBLIC_IP, customer="cust_2")
        assert r.headers["ratelimit-limit"] == "60"
        assert int(r.headers["ratelimit-remaining"]) < remaining, "one shared per-IP bucket"
        assert all(e["partner_customer_id"] is None for e in FakeActivityLog.entries)


@pytest.mark.asyncio
async def test_invalid_customer_id_from_a_partner_is_ignored(stubs):
    async with running(_config("aloka:rpm=100,customer_rpm=1")) as new:
        s = new()
        await s.open(ip=WORKER_IP, key=KEY_A)
        for _ in range(3):   # a bad id never forms a per-customer bucket of 1
            r, _ = await s.tools(ip=WORKER_IP, key=KEY_A, customer="bad id!*")
            assert r.status_code == 200
        r, _ = await s.tools(ip=WORKER_IP, key=KEY_A, customer="x" * 129)
        assert r.status_code == 200


# ── session binding ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("later_key", [None, "", "not-a-real-key-but-long-enough-000", KEY_B, KEY_Z])
async def test_a_partner_session_cannot_be_reused_without_its_key(stubs, later_key):
    async with running(_config()) as new:
        s = new()
        await s.open(ip=WORKER_IP, key=KEY_A)
        r, _ = await s.tools(ip=WORKER_IP, key=later_key)
        assert r.status_code == 403 and r.json()["error"] == "session_identity_mismatch"
        r, names = await s.tools(ip=WORKER_IP, key=KEY_A)   # the right key still works
        assert r.status_code == 200 and HIDDEN_VS in names


@pytest.mark.asyncio
async def test_a_public_session_is_not_upgraded_by_a_key_later(stubs):
    async with running(_config()) as new:
        s = new()
        await s.open(ip=WORKER_IP)
        r, names = await s.tools(ip=WORKER_IP, key=KEY_A)
        assert r.status_code == 200 and HIDDEN_VS not in names
        assert r.headers["ratelimit-limit"] == "60"
        _, text = await s.call(HIDDEN_VS, {"q": "red roses"}, ip=WORKER_IP, key=KEY_A)
        assert "private MCP" in text


# ── Eagle (IP allow-list) is unchanged ───────────────────────────────────────


@pytest.mark.asyncio
async def test_eagle_ip_path_is_unchanged(stubs):
    async with running(_config()) as new:
        s = new()
        r = await s.open(ip=EAGLE)
        assert r.headers["ratelimit-limit"] == "600"
        _, names = await s.tools(ip=EAGLE)
        assert len(names) == 8 and HIDDEN_VS not in names, "Eagle calls hidden tools by name, as before"
        _, text = await s.call(HIDDEN_VS, {"q": "red roses"}, ip=EAGLE)
        assert "Rose Bouquet" in text
        _, text = await s.call("kapruka_custom_cake_options", {}, ip=EAGLE)
        assert "available choices" in text
        # a customer header from Eagle (no key) is ignored too
        r, _ = await s.tools(ip=EAGLE, customer="cust_1")
        assert r.headers["ratelimit-limit"] == "600"
        assert all(e["partner"] is None for e in FakeActivityLog.entries)


@pytest.mark.asyncio
async def test_eagle_still_works_with_no_partners_configured(stubs):
    async with running(partners.load({})) as new:
        s = new()
        await s.open(ip=EAGLE)
        _, text = await s.call(HIDDEN_VS, {"q": "red roses"}, ip=EAGLE)
        assert "Rose Bouquet" in text


# ── audit + no key in logs ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_activity_log_records_partner_and_customer(stubs):
    async with running(_config()) as new:
        s = new()
        await s.open(ip=WORKER_IP, key=KEY_A, customer="cust_9")
        await s.tools(ip=WORKER_IP, key=KEY_A, customer="cust_9")
        p = new()
        await p.open(ip=PUBLIC_IP)
    aloka = [e for e in FakeActivityLog.entries if e["partner"] == "aloka"]
    public = [e for e in FakeActivityLog.entries if e["client_ip"] == PUBLIC_IP]
    assert aloka and all(e["partner_customer_id"] == "cust_9" for e in aloka)
    assert public and all(e["partner"] is None for e in public)


@pytest.mark.asyncio
async def test_the_key_never_appears_in_logs_responses_or_activity(stubs, caplog):
    caplog.set_level(logging.DEBUG)
    bodies: list[str] = []
    async with running(_config("aloka:rpm=12,customer_rpm=100")) as new:
        s = new()
        await s.open(ip=WORKER_IP, key=KEY_A, customer="cust_1")
        bodies.append((await s.call(HIDDEN_VS, {"q": "red roses"}, ip=WORKER_IP, key=KEY_A))[1])
        bodies.append((await s.call("kapruka_custom_cake_options", {}, ip=WORKER_IP, key=KEY_A))[1])
        bodies.append((await s.tools(ip=WORKER_IP, key=KEY_B))[0].text)          # 403 mismatch
        bodies.append((await s.tools(ip=WORKER_IP, key=None))[0].text)           # 403 mismatch
        z = new()
        await z.open(ip=WORKER_IP, key=KEY_Z)
        bodies.append((await z.call(HIDDEN_VS, {"q": "red roses"}, ip=WORKER_IP, key=KEY_Z))[1])
        pub = new()
        await pub.open(ip=PUBLIC_IP, key="not-a-real-key-but-long-enough-000")
        for _ in range(12):                                                       # trip the partner cap
            r, _ = await s.tools(ip=WORKER_IP, key=KEY_A)
            bodies.append(r.text)
        assert any('"rate_limit_exceeded"' in b for b in bodies)
    everything = caplog.text + "\n".join(
        (r.exc_text or "") + str(r.args) for r in caplog.records) + "\n".join(bodies) \
        + json.dumps(FakeActivityLog.entries, default=str)
    for key in ALL_KEYS + ("not-a-real-key-but-long-enough-000",):
        assert key not in everything
    # and the capture was live: partner events were logged — by name
    assert "denied partner=zeta" in caplog.text
    assert "refused a request on a partner session" in caplog.text


# ── middleware unit level: header stripping and identity ─────────────────────


@pytest.mark.asyncio
async def test_key_and_customer_headers_never_reach_the_app():
    seen = []

    async def inner(scope, receive, send):
        seen.append(scope)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    mw = PartnerAuthMiddleware(inner, config=_config())

    async def run(headers):
        scope = {"type": "http", "method": "POST", "path": "/mcp", "headers": headers}
        await mw(scope, None, lambda m: _noop(m))
        return seen[-1]

    s = await run([(b"x-partner-key", KEY_A.encode()), (b"x-partner-customer-id", b"cust_1"),
                   (b"user-agent", b"ua")])
    names = [n for n, _ in s["headers"]]
    assert b"x-partner-key" not in names and b"x-partner-customer-id" not in names and b"user-agent" in names
    ident = partners.identity_from_scope(s)
    assert ident.partner == "aloka" and ident.customer_id == "cust_1"
    assert KEY_A not in repr(ident)

    s = await run([(b"x-partner-key", b"wrong-key-that-is-long-enough-0000"),
                   (b"x-partner-customer-id", b"cust_1")])
    assert partners.identity_from_scope(s) is None
    assert [n for n, _ in s["headers"]] == []


async def _noop(_message):
    return None


# ── config parsing and authentication ────────────────────────────────────────


def test_parsing_hashes_raw_keys_scopes_and_limits():
    cfg = partners.load({
        "PARTNER_KEYS": f"aloka:sha256:{_sha(KEY_A).upper()};aloka:{KEY_B};bad;Bad Name:{KEY_Z};"
                        f"zeta:sha256:nothex;zeta:short",
        "PARTNER_SCOPES": "aloka:visual_search,custom_cake,nonsense;zeta:visual_search",
        "PARTNER_LIMITS": "aloka:rpm=900,customer_rpm=30,bogus=1,orders_per_hour=-5",
        "PARTNER_CUSTOMER_ORDER_RATE_LIMIT_PER_HOUR": "7",
    })
    assert [p for p, _ in cfg.key_hashes] == ["aloka", "aloka"]
    assert cfg.scopes_for("aloka") == {"visual_search", "custom_cake"}
    assert cfg.scopes_for("nobody") == frozenset()
    lim = cfg.limits_for("aloka")
    assert (lim.rpm, lim.customer_rpm, lim.orders_per_hour, lim.customer_orders_per_hour) == (900, 30, 300, 7)
    assert cfg.limits_for("zeta").rpm == 600
    assert KEY_B not in repr(cfg)


def test_authenticate():
    cfg = _config()
    assert cfg.authenticate(KEY_A)[0] == "aloka"
    assert cfg.authenticate(f"  {KEY_B}  ")[0] == "aloka"
    assert cfg.authenticate(KEY_Z)[0] == "zeta"
    for bad in (None, "", " ", KEY_A[:-1], KEY_A + "x", "x" * 300):
        assert cfg.authenticate(bad) is None
    assert partners.load({}).authenticate(KEY_A) is None
    assert not partners.load({}).enabled


def test_customer_id_format():
    assert partners.valid_customer_id("abc_DEF-123") == "abc_DEF-123"
    for bad in (None, "", "has space", "semi;colon", "x" * 129):
        assert partners.valid_customer_id(bad) is None


def test_private_access_still_works_for_contexts_without_a_scope():
    """The existing unit-test pattern (and stdio) — a request object with no ASGI scope."""
    from src.private_access import is_trusted_caller
    req = SimpleNamespace(headers={"cf-connecting-ip": EAGLE}, client=SimpleNamespace(host="127.0.0.1"))
    ctx = SimpleNamespace(request_context=SimpleNamespace(request=req))
    assert is_trusted_caller(ctx, [EAGLE], "visual_search")
    assert not is_trusted_caller(ctx, [], "visual_search")
    assert not is_trusted_caller(None, [EAGLE], "visual_search")


def test_activity_insert_sql():
    assert _insert_sql(_BASE_COLUMNS) == _INSERT_SQL
    assert "$6::jsonb" in _INSERT_SQL and "$14)" in _INSERT_SQL
    ext = _insert_sql(_BASE_COLUMNS + _PARTNER_COLUMNS)
    assert ext.endswith("$15, $16)") and "partner, partner_customer_id" in ext


@pytest.mark.asyncio
@pytest.mark.parametrize("present,expected", [
    ({"partner", "partner_customer_id"}, _BASE_COLUMNS + _PARTNER_COLUMNS),
    ({"partner"}, _BASE_COLUMNS),
    (set(), _BASE_COLUMNS),
    (None, _BASE_COLUMNS),            # the check itself fails
])
async def test_activity_log_uses_partner_columns_only_once_they_exist(present, expected):
    from src.activity_log import ActivityLogger

    class Conn:
        async def fetch(self, sql, names):
            if present is None:
                raise RuntimeError("permission denied")
            return [{"column_name": c} for c in present]

    class Pool:
        def acquire(self):
            pool = self

            class Ctx:
                async def __aenter__(self_inner):
                    return Conn()

                async def __aexit__(self_inner, *a):
                    return False
            return Ctx()

    log = ActivityLogger("postgresql://fake")
    log._pool = Pool()
    assert await log._detect_columns() == expected


@pytest.mark.asyncio
async def test_public_sessions_cannot_evict_a_partner_session_binding():
    """Only keyed sessions are stored, so a flood of public sessions can't push a
    partner session out of the (capped) binding table and silently demote it."""
    counter = iter(range(1000))

    async def inner(scope, receive, send):
        headers = [] if any(n == b"mcp-session-id" for n, _ in scope["headers"]) \
            else [(b"mcp-session-id", f"s{next(counter)}".encode())]
        await send({"type": "http.response.start", "status": 200, "headers": headers})
        await send({"type": "http.response.body", "body": b""})

    mw = PartnerAuthMiddleware(inner, config=_config(), max_sessions=2)
    sent: list[dict] = []

    async def capture(m):
        sent.append(m)

    async def req(key=None, sid=None, method="POST"):
        h = [(b"x-partner-key", key.encode())] if key else []
        if sid:
            h.append((b"mcp-session-id", sid.encode()))
        sent.clear()
        await mw({"type": "http", "method": method, "path": "/mcp", "headers": h}, None, capture)
        start = sent[0]
        return start["status"], dict(start["headers"]).get(b"mcp-session-id", b"").decode()

    _, partner_sid = await req(key=KEY_A)
    for _ in range(5):
        await req()                                   # five public sessions
    assert (await req(sid=partner_sid))[0] == 403     # still bound: no key -> refused
    assert (await req(key=KEY_A, sid=partner_sid))[0] == 200
    assert (await req(key=KEY_A, sid=partner_sid, method="DELETE"))[0] == 200
    assert (await req(sid=partner_sid))[0] == 200, "binding dropped once the session is deleted"
