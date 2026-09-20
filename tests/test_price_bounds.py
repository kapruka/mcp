"""Price bounds must mean what the caller said, in the caller's currency.

Upstream `products_search` converts the PRICES it returns into the requested
currency but compares `min_price`/`max_price` against the raw LKR figures.
Proven against production 2026-09-21:

    q="cake" currency=USD max_price=32   -> "No products found"
    q="cake" currency=USD max_price=9600 -> cakes priced USD 13.15-31.48
    q="cake" currency=USD max_price=100  -> greeting cards at USD 0.30 (LKR 90)

So a dollar budget was tested against rupee numbers, and ~62% of this agent's
customers are overseas. This module pins the fix: convert the bound to LKR for
the upstream call, and always enforce the caller's own bound on what we return.
"""

import json

import pytest

from src.tools import products as products_tool
from src.tools.products import SearchProductsInput, kapruka_search_products

RATE = 300.0  # rupees per dollar, what the anchor product implies in these stubs


def _row(pid, name, amount, currency="USD"):
    return {"id": pid, "name": name, "price": {"amount": amount, "currency": currency},
            "url": f"https://www.kapruka.com/p/{pid}", "in_stock": True}


class _Client:
    """Mirrors upstream: prices come back in the asked currency, but min/max are
    compared against the LKR figure."""

    calls = []
    anchor_calls = 0
    rate_available = True
    catalogue = [  # (id, name, LKR price)
        ("CAKE1", "Puppy Pals Bento Cake", 3945.0),      # USD 13.15
        ("CAKE2", "Royal Chocolate Drizzle Tower", 7611.0),  # USD 25.37
        ("CAKE3", "Grand Tier Wedding Cake", 15000.0),   # USD 50.00
        ("CARD1", "Pretty Pink Mini Bday Card", 90.0),   # USD 0.30
    ]

    async def call(self, endpoint, **params):
        type(self).calls.append({"endpoint": endpoint, **params})
        if endpoint == "product":
            type(self).anchor_calls += 1
            if not type(self).rate_available:
                raise RuntimeError("anchor unavailable")
            cur = (params.get("currency") or "LKR").upper()
            amount = 3000.0 if cur == "LKR" else 3000.0 / RATE
            return {"price": {"amount": amount, "currency": cur}}
        cur = (params.get("currency") or "LKR").upper()
        lo, hi = params.get("min_price"), params.get("max_price")
        out = []
        for pid, name, lkr in type(self).catalogue:
            if lo is not None and lkr < float(lo):
                continue
            if hi is not None and lkr > float(hi):
                continue
            out.append(_row(pid, name, lkr if cur == "LKR" else round(lkr / RATE, 2), cur))
        return {"results": out}


@pytest.fixture(autouse=True)
def _stub(monkeypatch):
    _Client.calls = []
    _Client.anchor_calls = 0
    _Client.rate_available = True
    products_tool._RATE_CACHE.clear()
    monkeypatch.setattr(products_tool, "KaprukaClient", _Client)


async def _run(**kw):
    return await kapruka_search_products(SearchProductsInput(q="cake", **kw))


def _names(out):
    return json.loads(out)["results"]


# ---------- the bug, fixed ----------

@pytest.mark.asyncio
async def test_usd_cap_is_converted_not_taken_as_rupees():
    out = await _run(currency="USD", max_price=32, response_format="json")
    rows = _names(out)
    assert rows, "a USD 32 cap must not empty the catalogue"
    assert [r["id"] for r in rows] == ["CAKE1", "CAKE2", "CARD1"]
    # the upstream call carried the LKR equivalent, not the raw 32
    search = [c for c in _Client.calls if c["endpoint"] == "products_search"][0]
    assert search["max_price"] == pytest.approx(32 * RATE)


@pytest.mark.asyncio
async def test_usd_cap_still_excludes_what_is_over_budget():
    rows = _names(await _run(currency="USD", max_price=32, response_format="json"))
    assert all(r["price"]["amount"] <= 32 for r in rows)
    assert "CAKE3" not in [r["id"] for r in rows]   # USD 50


@pytest.mark.asyncio
async def test_usd_min_price_is_converted_too():
    rows = _names(await _run(currency="USD", min_price=20, response_format="json"))
    assert [r["id"] for r in rows] == ["CAKE2", "CAKE3"]
    search = [c for c in _Client.calls if c["endpoint"] == "products_search"][0]
    assert search["min_price"] == pytest.approx(20 * RATE)


@pytest.mark.asyncio
async def test_usd_band_uses_both_bounds():
    rows = _names(await _run(currency="USD", min_price=10, max_price=30, response_format="json"))
    assert [r["id"] for r in rows] == ["CAKE1", "CAKE2"]


@pytest.mark.asyncio
async def test_lkr_bounds_are_passed_through_untouched():
    rows = _names(await _run(currency="LKR", max_price=5000, response_format="json"))
    assert [r["id"] for r in rows] == ["CAKE1", "CARD1"]
    search = [c for c in _Client.calls if c["endpoint"] == "products_search"][0]
    assert search["max_price"] == 5000
    assert _Client.anchor_calls == 0, "LKR needs no rate lookup"


@pytest.mark.asyncio
async def test_no_bounds_means_no_rate_lookup():
    await _run(currency="USD", response_format="json")
    assert _Client.anchor_calls == 0


# ---------- the rate itself ----------

@pytest.mark.asyncio
async def test_rate_is_cached_across_searches():
    await _run(currency="USD", max_price=32, response_format="json")
    first = _Client.anchor_calls
    await _run(currency="USD", max_price=25, response_format="json")
    assert _Client.anchor_calls == first, "the rate must be reused, not re-derived"


@pytest.mark.asyncio
async def test_unreadable_rate_falls_back_to_local_filtering():
    _Client.rate_available = False
    out = await _run(currency="USD", max_price=32, response_format="json")
    payload = json.loads(out)
    assert payload.get("price_filtered_locally") is True
    # no bound was sent upstream (it would have been misread) ...
    search = [c for c in _Client.calls if c["endpoint"] == "products_search"][0]
    assert search.get("max_price") is None and search.get("min_price") is None
    # ... but the caller's budget is still honoured
    assert all(r["price"]["amount"] <= 32 for r in payload["results"])
    assert "CAKE3" not in [r["id"] for r in payload["results"]]


@pytest.mark.asyncio
async def test_local_filtering_overfetches_for_headroom():
    _Client.rate_available = False
    await _run(currency="USD", max_price=32, limit=5, response_format="json")
    search = [c for c in _Client.calls if c["endpoint"] == "products_search"][0]
    assert search["limit"] > 5


# ---------- the returned price is the authority ----------

@pytest.mark.asyncio
async def test_row_over_budget_is_dropped_even_if_upstream_returned_it():
    class _Sloppy(_Client):
        async def call(self, endpoint, **params):
            type(self).calls.append({"endpoint": endpoint, **params})
            if endpoint == "product":
                return {"price": {"amount": 3000.0 if (params.get("currency") or "LKR").upper() == "LKR" else 10.0,
                                  "currency": params.get("currency")}}
            return {"results": [_row("CAKE3", "Grand Tier Wedding Cake", 50.0, "USD")]}

    products_tool.KaprukaClient = _Sloppy
    out = await kapruka_search_products(SearchProductsInput(q="cake", currency="USD", max_price=32))
    assert "No products found" in out


@pytest.mark.asyncio
async def test_json_view_reports_the_conversion():
    payload = json.loads(await _run(currency="USD", max_price=32, response_format="json"))
    assert payload["price_bounds_converted_to_lkr"] == pytest.approx(RATE, rel=0.01)


@pytest.mark.asyncio
async def test_markdown_path_also_respects_the_budget():
    out = await _run(currency="USD", max_price=32)
    assert "Puppy Pals Bento Cake" in out
    assert "Grand Tier Wedding Cake" not in out
