"""Website discounts, as the commerce API reports them since 2026-10-05.

`price` is what the cart charges (discount applied, same code as the website)
and `compare_at_price` is the pre-discount price, on products, variants and
search rows alike. The MCP shows both and the percentage off, rounded down so
it never overstates the discount (3,250 -> 2,250 is 30.8%, shown as 30%,
matching the website's "30% off").
"""

import pytest

from src.tools import products as products_tool
from src.tools.products import (
    GetProductInput,
    SearchProductsInput,
    _discount_pct,
    kapruka_get_product,
    kapruka_search_products,
)

BEAR = "EF_PC_SOFT0V2686POD00003"


def _money(amount, currency="LKR"):
    return {"amount": amount, "currency": currency}


class _Client:
    async def call(self, endpoint, **params):
        if endpoint == "product":
            return {
                "id": BEAR, "name": "Breathing Teddy Bear", "in_stock": True,
                "price": _money(2250), "compare_at_price": _money(3250),
                "variants": [
                    {"id": "v1", "name": "Brown", "price": _money(2250),
                     "compare_at_price": _money(3250), "in_stock": True},
                    {"id": "v2", "name": "White", "price": _money(2600),
                     "compare_at_price": None, "in_stock": True},
                ],
            }
        return {
            "results": [
                {"id": BEAR, "type": "product", "name": "Breathing Teddy Bear",
                 "price": _money(2250), "compare_at_price": _money(3250), "in_stock": True},
                {"id": "PLAIN1", "type": "product", "name": "Plain Teddy",
                 "price": _money(2300), "compare_at_price": None, "in_stock": True},
            ],
            "facets": {"categories": []},
        }


@pytest.fixture(autouse=True)
def _stub(monkeypatch):
    monkeypatch.setattr(products_tool, "KaprukaClient", _Client)


@pytest.mark.parametrize("now,was,pct", [
    (2250, 3250, 30),       # 30.77 -> 30, as the website labels it
    (130000, 150000, 13),
    (21990, 39990, 45),
    (2300, None, None),     # not discounted
    (3250, 3250, None),     # equal is not a discount
    (3300, 3250, None),     # nonsense pair is ignored
])
def test_discount_pct_rounds_down(now, was, pct):
    assert _discount_pct(_money(now), _money(was) if was else None) == pct


@pytest.mark.asyncio
async def test_search_row_shows_was_price_and_percent():
    out = await kapruka_search_products(SearchProductsInput(q="teddy bear"))
    assert f"`{BEAR}` · LKR 2,250 (was LKR 3,250, 30% off)" in out
    assert "`PLAIN1` · LKR 2,300 · In stock" in out


@pytest.mark.asyncio
async def test_product_shows_was_line_with_percent_and_discounted_variant():
    out = await kapruka_get_product(GetProductInput(product_id=BEAR))
    assert "**Price**: LKR 2,250" in out
    assert "**Was**: ~~LKR 3,250~~ (30% off)" in out
    assert "- Brown — LKR 2,250 (was LKR 3,250, 30% off) — In stock" in out
    assert "- White — LKR 2,600 — In stock" in out
