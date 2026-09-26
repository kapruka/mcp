"""Price bounds go to the API exactly as the caller gave them.

Until 2026-09-26 the API compared `min_price`/`max_price` against rupee
figures whatever `currency` said, so this tool derived an LKR rate from anchor
products and converted the bound on the way out. Kapruka fixed it upstream
(bounds are now applied in the requested currency and `applied_filters`
echoes `currency`), which made the conversion actively harmful: a USD 30 cap
went out as `max_price=8100` with `currency=USD` — i.e. "under $8,100".

These tests pin the new contract: no conversion, no rate lookups, and the
returned-price guard still trims any row outside the caller's own bound.
"""

import json

import pytest

from src.tools import products as products_tool
from src.tools.products import SearchProductsInput, kapruka_search_products


def _row(pid, name, amount, currency="USD"):
    return {"id": pid, "type": "product", "name": name,
            "price": {"amount": amount, "currency": currency},
            "url": f"https://www.kapruka.com/p/{pid}", "in_stock": True}


class _Client:
    """Mirrors the fixed upstream: bounds are applied in the requested currency."""

    calls: list[tuple[str, dict]] = []
    catalogue = [  # (id, name, USD price)
        ("CAKE1", "Puppy Pals Bento Cake", 13.15),
        ("CAKE2", "Fudge Fusion Strawberry Cake", 19.96),
        ("CAKE3", "Midnight Decadence Cake", 26.96),
        ("CAKE4", "Grand Tier Wedding Cake", 84.67),
    ]
    leak: list[dict] = []  # rows the API returns regardless of the bound

    async def call(self, endpoint, **params):
        type(self).calls.append((endpoint, params))
        lo, hi = params.get("min_price"), params.get("max_price")
        rows = [
            _row(i, n, p) for i, n, p in self.catalogue
            if (lo is None or p >= lo) and (hi is None or p <= hi)
        ]
        return {
            "results": rows + list(type(self).leak),
            "total_estimate": len(rows),
            "applied_filters": {"q": params.get("q"), "currency": params.get("currency"),
                                **({"min_price": lo} if lo is not None else {}),
                                **({"max_price": hi} if hi is not None else {})},
            "facets": {"categories": [{"name": "Kapruka Cakes", "count": len(rows)}]},
        }


@pytest.fixture(autouse=True)
def _stub(monkeypatch):
    _Client.calls = []
    _Client.leak = []
    monkeypatch.setattr(products_tool, "KaprukaClient", _Client)


async def _run(**kw):
    kw.setdefault("q", "cake")
    return await kapruka_search_products(SearchProductsInput(**kw))


@pytest.mark.asyncio
async def test_usd_bounds_are_sent_as_given():
    await _run(currency="USD", min_price=10, max_price=30)
    endpoint, sent = _Client.calls[-1]
    assert endpoint == "products_search"
    assert sent["currency"] == "USD"
    assert sent["min_price"] == 10 and sent["max_price"] == 30, "no conversion to rupees"


@pytest.mark.asyncio
async def test_one_upstream_call_and_no_rate_lookups():
    await _run(currency="GBP", max_price=25)
    assert [e for e, _ in _Client.calls] == ["products_search"], \
        "the old anchor-product rate lookups must be gone"


@pytest.mark.asyncio
async def test_usd_band_returns_what_is_in_it():
    payload = json.loads(await _run(currency="USD", min_price=10, max_price=30,
                                    response_format="json"))
    ids = [r["id"] for r in payload["results"]]
    assert ids == ["CAKE1", "CAKE2", "CAKE3"]


@pytest.mark.asyncio
async def test_lkr_is_passed_through_too():
    await _run(currency="LKR", max_price=9600)
    assert _Client.calls[-1][1]["max_price"] == 9600


@pytest.mark.asyncio
async def test_guard_still_trims_a_row_outside_the_callers_bound():
    # If the API ever regresses and returns an over-budget row, the caller's own
    # bound (row price and bound share a currency) still wins.
    _Client.leak = [_row("OVER", "Over Budget Cake", 45.00)]
    payload = json.loads(await _run(currency="USD", max_price=30, response_format="json"))
    assert "OVER" not in [r["id"] for r in payload["results"]]


@pytest.mark.asyncio
async def test_json_view_has_no_conversion_artifacts_and_echoes_currency():
    payload = json.loads(await _run(currency="USD", max_price=30, response_format="json"))
    assert "price_bounds_converted_to_lkr" not in payload
    assert "price_filtered_locally" not in payload
    assert payload["applied_filters"]["currency"] == "USD"


@pytest.mark.asyncio
async def test_markdown_path_respects_the_budget():
    out = await _run(currency="USD", max_price=30)
    assert "Puppy Pals Bento Cake" in out
    assert "Grand Tier Wedding Cake" not in out


@pytest.mark.asyncio
async def test_nothing_in_budget_says_so():
    out = await _run(currency="USD", max_price=5)
    assert out.startswith("No products found for 'cake'")
