"""Category filter: retry without it instead of reporting an empty catalogue.

Upstream products_search honours only a few category values. Measured against
production on 2026-09-20: Birthday, Chocolates, Books, Electronics, Fruits and
Clothing return rows; Cakes, Flowers, Toys, Jewelry, Perfume, Grocery,
Vouchers, Fashion, Bakery, Hampers and every occasion name return nothing for
every query — even the value the product itself reports (a cake's own category
field reads "cakes", and searching that returns nothing).

Over 5 days of live agent traffic, 92 searches carried a category filter and 75
came back empty: Cakes 40 of 40, Flowers 23 of 23. Until the API is fixed the
tool drops the filter and says so.
"""

import json

import pytest

from src.tools import products as products_tool
from src.tools.products import SearchProductsInput, kapruka_search_products

# Categories upstream actually honours (probed 2026-09-20).
WORKING = {"birthday", "chocolates", "books", "electronics", "fruits", "clothing"}


def _row(pid: str, name: str) -> dict:
    return {
        "id": pid,
        "name": name,
        "price": {"amount": 4850.0, "currency": "LKR"},
        "url": f"https://www.kapruka.com/p/{pid}",
        "in_stock": True,
    }


class _StubClient:
    """Mirrors upstream: a category outside WORKING yields nothing at all."""

    calls: list[dict] = []

    async def call(self, endpoint, **params):
        type(self).calls.append(params)
        cat = (params.get("category") or "").lower()
        if cat and cat not in WORKING:
            return {"results": []}
        return {"results": [_row("CAKE00KA001535", "Birthday Bliss Ribbon Cake")]}


@pytest.fixture(autouse=True)
def _stub(monkeypatch):
    _StubClient.calls = []
    monkeypatch.setattr(products_tool, "KaprukaClient", _StubClient)


async def _run(**kw):
    return await kapruka_search_products(SearchProductsInput(q="cake", currency="LKR", **kw))


@pytest.mark.asyncio
async def test_broken_category_falls_back_instead_of_empty():
    out = await _run(category="Cakes")
    assert "No products found" not in out
    assert "Birthday Bliss Ribbon Cake" in out
    # It must tell the caller the filter was dropped, and not to send it again.
    assert "category filter was dropped" in out or "was\ndropped" in out or "dropped" in out
    assert "Do not re-send that category" in out
    # Header must not claim the results came from that category.
    assert 'in **Cakes**' not in out
    # Exactly two upstream calls: with the filter, then without.
    assert len(_StubClient.calls) == 2
    assert _StubClient.calls[0].get("category") == "Cakes"
    assert "category" not in _StubClient.calls[1] or _StubClient.calls[1].get("category") is None


@pytest.mark.asyncio
async def test_working_category_is_not_touched():
    out = await _run(category="Chocolates")
    assert "in **Chocolates**" in out
    assert "dropped" not in out
    assert len(_StubClient.calls) == 1


@pytest.mark.asyncio
async def test_no_category_means_no_retry():
    out = await _run()
    assert "Birthday Bliss Ribbon Cake" in out
    assert len(_StubClient.calls) == 1


@pytest.mark.asyncio
async def test_json_view_flags_the_dropped_filter():
    out = json.loads(await _run(category="Flowers", response_format="json"))
    assert out["category_filter_dropped"] == "Flowers"
    assert out["results"][0]["id"] == "CAKE00KA001535"


@pytest.mark.asyncio
async def test_json_view_silent_when_nothing_was_dropped():
    out = json.loads(await _run(response_format="json"))
    assert "category_filter_dropped" not in out


@pytest.mark.asyncio
async def test_genuinely_empty_still_reports_empty():
    class _Empty(_StubClient):
        async def call(self, endpoint, **params):
            type(self).calls.append(params)
            return {"results": []}

    products_tool.KaprukaClient = _Empty
    out = await _run(category="Cakes")
    assert "No products found for 'cake' in category 'Cakes'." in out


def test_description_no_longer_recommends_broken_values():
    desc = SearchProductsInput.model_fields["category"].description
    # The old text offered 'Cakes' and 'Flowers' as the examples to copy.
    assert "e.g. 'Birthday', 'Cakes', 'Flowers'" not in desc
    assert "Chocolates" in desc and "return" in desc
