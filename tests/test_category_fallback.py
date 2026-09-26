"""Category facets: honour real ones, recover from wrong ones, publish the list.

`category` is the website's search facet, not a department. Since 2026-09-26
the API answers an unknown facet with `400 invalid_category` and the valid
names for the query in `details.valid_categories`, and every search response
carries `facets.categories`. (Before that, a wrong name was 200 with zero rows:
75 of 92 filtered searches were lost that way in a five-day sample.)

Contract pinned here:
- a real facet is sent once and honoured;
- a wrong one is retried ONCE without the filter, the drop is announced, and
  the valid names are handed back so the caller can narrow properly;
- an empty result inside a REAL facet is a genuine "none" — it must not be
  retried without the filter (that would turn "no cakes under $5" into
  greeting cards);
- other upstream errors are reported, not papered over;
- rows that are not `type: "product"` never reach the caller.
"""

import json

import httpx
import pytest

from src.tools import products as products_tool
from src.tools.products import SearchProductsInput, kapruka_search_products

FACETS = [
    {"name": "Kapruka Cakes", "count": 292},
    {"name": "Greeting Cards", "count": 245},
    {"name": "Birthday", "count": 92},
]
VALID = {f["name"].lower() for f in FACETS}


def _row(pid: str, name: str, rtype: str = "product") -> dict:
    return {"id": pid, "type": rtype, "name": name,
            "price": {"amount": 4850.0, "currency": "LKR"},
            "url": f"https://www.kapruka.com/p/{pid}", "in_stock": True}


def _http_error(status: int, payload: dict) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "https://www.kapruka.com/tools/commerce_phase1.jsp")
    resp = httpx.Response(status, json=payload, request=req)
    return httpx.HTTPStatusError(f"HTTP {status}", request=req, response=resp)


class _StubClient:
    """Mirrors the 2026-09-26 upstream."""

    calls: list[dict] = []
    empty_in_facet = False
    server_error = False

    async def call(self, endpoint, **params):
        type(self).calls.append(params)
        if type(self).server_error:
            raise _http_error(500, {"error": {"code": "internal_error", "message": "boom"}})
        cat = params.get("category")
        if cat and cat.strip().lower() not in VALID:
            raise _http_error(400, {"error": {
                "code": "invalid_category",
                "message": f"Unknown category '{cat}' for this query.",
                "details": {"category": cat, "valid_categories": [f["name"] for f in FACETS]},
            }})
        rows = [] if (cat and type(self).empty_in_facet) else [
            _row("CAKE00KA001535", "Birthday Bliss Ribbon Cake"),
            _row("CATSYM00230", "See Top Selling Cakes", rtype="category"),
        ]
        return {"results": rows, "total_estimate": len(rows), "facets": {"categories": FACETS},
                "applied_filters": {"q": params.get("q"), **({"category": cat} if cat else {})}}


@pytest.fixture(autouse=True)
def _stub(monkeypatch):
    _StubClient.calls = []
    _StubClient.empty_in_facet = False
    _StubClient.server_error = False
    monkeypatch.setattr(products_tool, "KaprukaClient", _StubClient)


async def _run(**kw):
    return await kapruka_search_products(SearchProductsInput(q="birthday cake", **kw))


@pytest.mark.asyncio
async def test_wrong_category_is_retried_without_it_and_the_valid_names_are_given():
    out = await _run(category="Cakes")
    assert "Birthday Bliss Ribbon Cake" in out
    assert "'Cakes' is not a category for this search" in out
    assert "Kapruka Cakes, Greeting Cards, Birthday" in out
    assert "Do not re-send 'Cakes'" in out
    assert "in **Cakes**" not in out
    assert len(_StubClient.calls) == 2
    assert _StubClient.calls[0].get("category") == "Cakes"
    assert _StubClient.calls[1].get("category") is None


@pytest.mark.asyncio
async def test_wrong_category_json_reports_drop_and_valid_names():
    payload = json.loads(await _run(category="Cakes", response_format="json"))
    assert payload["category_filter_dropped"] == "Cakes"
    assert payload["valid_categories"] == ["Kapruka Cakes", "Greeting Cards", "Birthday"]


@pytest.mark.asyncio
async def test_real_facet_is_sent_once_and_honoured():
    out = await _run(category="Kapruka Cakes")
    assert "in **Kapruka Cakes**" in out
    assert "not a category" not in out
    assert len(_StubClient.calls) == 1


@pytest.mark.asyncio
async def test_empty_result_inside_a_real_facet_is_not_retried():
    _StubClient.empty_in_facet = True
    out = await _run(category="Kapruka Cakes")
    assert out.startswith("No products found for 'birthday cake' in category 'Kapruka Cakes'")
    assert len(_StubClient.calls) == 1, "a genuine empty must not fall back to other categories"


@pytest.mark.asyncio
async def test_other_upstream_errors_are_reported():
    _StubClient.server_error = True
    out = await _run(category="Kapruka Cakes")
    assert out.startswith("Error")
    assert len(_StubClient.calls) == 1


@pytest.mark.asyncio
async def test_facets_are_offered_when_no_category_was_applied():
    out = await _run()
    assert "Narrow with `category` (facets for this search): Kapruka Cakes (292), " \
           "Greeting Cards (245), Birthday (92)" in out


@pytest.mark.asyncio
async def test_facets_pass_through_in_json():
    payload = json.loads(await _run(response_format="json"))
    assert payload["facets"]["categories"][0] == {"name": "Kapruka Cakes", "count": 292}
    assert payload["total_estimate"] == 2


@pytest.mark.asyncio
async def test_non_product_rows_never_reach_the_caller():
    payload = json.loads(await _run(response_format="json"))
    assert [r["id"] for r in payload["results"]] == ["CAKE00KA001535"]


@pytest.mark.asyncio
async def test_include_stubs_is_accepted_but_has_no_effect():
    payload = json.loads(await _run(include_stubs=True, response_format="json"))
    assert [r["id"] for r in payload["results"]] == ["CAKE00KA001535"]
