"""kapruka_visual_search: private, additional to Doofinder search, LKR-only upstream.

Eagle is stubbed with an httpx.MockTransport; response shapes follow
eagle-dashboard/docs/visual-search-cache.md (verified live 2026-09-26).
"""

import json
from types import SimpleNamespace

import httpx
import pytest

from src.config.settings import settings
from src.server import mcp
from src.tools import products as products_tool
from src.tools import visual_search as vs
from src.tools.visual_search import VisualSearchInput, kapruka_visual_search

EAGLE = "23.111.183.104"
URL = "https://eagle.example/api/vs/search"
CATS = [{"name": "Fresh Flowers", "count": 88}, {"name": "Greeting Cards", "count": 18}]


def _ctx(ip=EAGLE):
    req = SimpleNamespace(headers={"cf-connecting-ip": ip} if ip else {},
                          client=SimpleNamespace(host="127.0.0.1"))
    return SimpleNamespace(request_context=SimpleNamespace(request=req))


def _vs_row(pid, title, lkr, sale=None, **kw):
    return {"id": pid, "title": title, "url": f"https://www.kapruka.com/p/{pid}",
            "image": "https://static2.kapruka.com/x.jpg", "price_lkr": lkr,
            "sale_price_lkr": sale, "best_price_lkr": sale if sale is not None else lkr,
            "brand": "Kapruka", "categories": ["Fresh Flowers"], "summary": "A bouquet of red roses.",
            "has_variants": False, "rank": 1, **kw}


class Eagle:
    """Records requests; answers like Eagle."""

    requests: list[httpx.Request] = []
    status = 200
    headers: dict = {}
    rows = [_vs_row("FLOWERS00T1828", "The Rose Edit Bouquet 12 Red Roses", 5940),
            _vs_row("FLOWERS00T1182", "Single Red Rose For Her", 1500, sale=1350)]
    raise_exc: Exception | None = None

    @classmethod
    def handler(cls, request: httpx.Request) -> httpx.Response:
        cls.requests.append(request)
        if cls.raise_exc:
            raise cls.raise_exc
        if cls.status != 200:
            return httpx.Response(cls.status, headers=cls.headers, json={"error": "x"})
        cat = request.url.params.get("category")
        rows = cls.rows if cat in (None, "Fresh Flowers") else []
        return httpx.Response(200, json={
            "ok": True, "query": request.url.params.get("q"), "normalized_query": "red rose",
            "total": len(rows), "page": int(request.url.params.get("page", 1)),
            "limit": int(request.url.params.get("limit", 24)), "category": cat,
            "categories": CATS, "results": rows,
            "cache": {"outcome": "l1", "ms": 0.6, "generation": 3, "degraded": False},
        })


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    Eagle.requests, Eagle.status, Eagle.headers, Eagle.raise_exc = [], 200, {}, None
    monkeypatch.setattr(vs, "_TRANSPORT", httpx.MockTransport(Eagle.handler))
    monkeypatch.setattr(settings, "eagle_vs_url", URL)
    monkeypatch.setattr(settings, "eagle_vs_api_key", "test-key")
    monkeypatch.setattr(settings, "visual_search_trusted_ips", [EAGLE])

    async def rate(cur):
        return {"LKR": 1.0, "USD": 270.0}.get(cur)
    monkeypatch.setattr(vs, "lkr_per_unit", rate)


async def _run(ctx=None, **kw):
    kw.setdefault("q", "red roses")
    return await kapruka_visual_search(VisualSearchInput(**kw), ctx if ctx is not None else _ctx())


# ── private ──────────────────────────────────────────────────────────────────


def test_hidden_from_tools_list_but_callable():
    names = [t.name for t in mcp._tool_manager.list_tools()]
    assert "kapruka_visual_search" not in names
    assert mcp._tool_manager.get_tool("kapruka_visual_search") is not None


@pytest.mark.asyncio
async def test_untrusted_ip_is_refused_before_eagle_is_called():
    out = await _run(ctx=_ctx("203.0.113.9"))
    assert "private MCP" in out
    assert Eagle.requests == []


@pytest.mark.asyncio
async def test_fails_closed_without_trusted_ips(monkeypatch):
    monkeypatch.setattr(settings, "visual_search_trusted_ips", [])
    assert "private MCP" in await _run()


def test_not_in_public_manifests():
    from src.well_known import WELL_KNOWN_MCP
    import pathlib
    assert "visual_search" not in json.dumps(WELL_KNOWN_MCP)
    assert "visual_search" not in (pathlib.Path(products_tool.__file__).parents[1] / "static" / "index.html").read_text(encoding="utf-8")


# ── additional to Doofinder, never on its path ───────────────────────────────


@pytest.mark.asyncio
async def test_does_not_touch_the_doofinder_search(monkeypatch):
    class Boom:
        async def call(self, *a, **k):
            raise AssertionError("kapruka_visual_search must not call commerce_phase1 search")
    monkeypatch.setattr(products_tool, "KaprukaClient", Boom)
    out = await _run()
    assert "The Rose Edit Bouquet" in out


# ── request shape ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sends_key_header_and_query():
    await _run(limit=5, page=2, sort="price_asc")
    req = Eagle.requests[-1]
    assert req.headers["x-api-key"] == "test-key"
    assert str(req.url).startswith(URL)
    p = req.url.params
    assert p["q"] == "red roses" and p["limit"] == "5" and p["page"] == "2" and p["sort"] == "price_asc"
    assert p["include_adult"] == "1", "adult included by default, sent explicitly"
    assert "min_price" not in p


@pytest.mark.asyncio
async def test_include_adult_false_is_sent_as_zero():
    await _run(include_adult=False)
    assert Eagle.requests[-1].url.params["include_adult"] == "0"


@pytest.mark.asyncio
async def test_usd_bounds_go_to_eagle_in_lkr_and_prices_come_back_in_usd():
    payload = json.loads(await _run(currency="USD", min_price=5, max_price=30, response_format="json"))
    p = Eagle.requests[-1].url.params
    assert p["min_price"] == "1350" and p["max_price"] == "8100"
    assert payload["results"][0]["price"] == {"amount": 22.0, "currency": "USD"}
    assert payload["fx_lkr_per_unit"] == 270.0
    assert payload["applied"]["currency"] == "USD" and payload["applied"]["max_price"] == 30


@pytest.mark.asyncio
async def test_sale_price_shown_with_was_price():
    out = await _run()
    assert "LKR 1,350 (was LKR 1,500)" in out


@pytest.mark.asyncio
async def test_unreadable_rate_answers_in_lkr_and_says_the_range_was_not_applied(monkeypatch):
    async def none(cur):
        return None
    monkeypatch.setattr(vs, "lkr_per_unit", none)
    out = await _run(currency="USD", max_price=30)
    assert "LKR 5,940" in out
    assert "price range was not applied" in out
    assert "max_price" not in Eagle.requests[-1].url.params


# ── category recovery (Eagle answers an unknown category with 200 + nothing) ─


@pytest.mark.asyncio
async def test_unknown_category_is_dropped_with_the_valid_names():
    out = await _run(category="Flowers")
    assert "'Flowers' is not a category for this search" in out
    assert "Fresh Flowers, Greeting Cards" in out
    assert "The Rose Edit Bouquet" in out
    assert [r.url.params.get("category") for r in Eagle.requests] == ["Flowers", None]


@pytest.mark.asyncio
async def test_case_slip_is_corrected_not_dropped():
    out = await _run(category="fresh  flowers")
    assert "in **Fresh Flowers**" in out and "not a category" not in out
    assert Eagle.requests[-1].url.params["category"] == "Fresh Flowers"


@pytest.mark.asyncio
async def test_json_reports_drop():
    payload = json.loads(await _run(category="Flowers", response_format="json"))
    assert payload["category_filter_dropped"] == "Flowers"
    assert payload["valid_categories"] == ["Fresh Flowers", "Greeting Cards"]


# ── failures: every one says "fall back to kapruka_search_products" ──────────


@pytest.mark.asyncio
@pytest.mark.parametrize("status,code", [
    (401, "visual_search_unauthorized"), (302, "visual_search_blocked"),
    (403, "visual_search_blocked"), (502, "visual_search_failed"), (500, "visual_search_failed"),
])
async def test_http_failures_map_to_fall_back(status, code):
    Eagle.status = status
    out = await _run()
    assert out.startswith(f"Error ({code})") and "kapruka_search_products" in out


@pytest.mark.asyncio
async def test_rate_limit_passes_retry_after():
    Eagle.status, Eagle.headers = 429, {"Retry-After": "3"}
    out = await _run()
    assert out.startswith("Error (visual_search_rate_limited): wait 3 seconds")


@pytest.mark.asyncio
async def test_timeout_is_unavailable():
    Eagle.raise_exc = httpx.ReadTimeout("slow")
    out = await _run()
    assert out.startswith("Error (visual_search_unavailable)") and "kapruka_search_products" in out


@pytest.mark.asyncio
async def test_unconfigured(monkeypatch):
    monkeypatch.setattr(settings, "eagle_vs_api_key", "")
    assert (await _run()).startswith("Error (visual_search_unconfigured)")
