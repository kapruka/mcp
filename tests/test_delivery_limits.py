"""Per-product delivery-city limits: rendering + error-envelope parsing.

Pure unit tests — the upstream client is stubbed, no network. The response
shapes below are copied from live phase1 responses on 2026-09-11 (see
docs/phase1_delivery_limits_mcp_update.md for the API brief and test data).
"""

import json

import httpx
import pytest

from src.api.client import handle_api_error
from src.delivery_scope import describe_delivery, fmt_city_list, is_city_limited
from src.tools import delivery as delivery_tool
from src.tools import products as products_tool
from src.tools.delivery import CheckDeliveryInput, kapruka_check_delivery
from src.tools.products import GetProductInput, kapruka_get_product

CITIES_47 = [f"City {i:02d}" for i in range(1, 48)]
COLOMBO = ["Colombo 01", "Colombo 02", "Colombo 03", "Dehiwala", "Nugegoda"]


class _StubClient:
    """Stands in for KaprukaClient; records the params it was called with."""

    payload: dict = {}
    calls: list[tuple[str, dict]] = []

    async def call(self, endpoint, **params):
        _StubClient.calls.append((endpoint, params))
        return dict(_StubClient.payload)


@pytest.fixture(autouse=True)
def _stub(monkeypatch):
    _StubClient.calls = []
    monkeypatch.setattr(delivery_tool, "KaprukaClient", _StubClient)
    monkeypatch.setattr(products_tool, "KaprukaClient", _StubClient)


def _http_422(payload: dict) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://example.com")
    response = httpx.Response(
        422, request=request, content=json.dumps(payload).encode(),
        headers={"content-type": "application/json"},
    )
    return httpx.HTTPStatusError("", request=request, response=response)


# ── helpers ──────────────────────────────────────────────────────────────────


def test_fmt_city_list_truncates_and_reports_true_total():
    out = fmt_city_list(COLOMBO, total=47, show=3)
    assert out == "Colombo 01, Colombo 02, Colombo 03 (+44 more)"


def test_fmt_city_list_short_list_no_suffix():
    assert fmt_city_list(COLOMBO, total=5) == ", ".join(COLOMBO)


def test_describe_delivery_island_wide():
    assert describe_delivery({"island_wide": True}) == "Island-wide"
    assert not is_city_limited({"island_wide": True})


def test_describe_delivery_limited_mentions_count_and_check_tool():
    d = {"island_wide": False, "deliverable_city_count": 47, "deliverable_cities": COLOMBO}
    out = describe_delivery(d)
    assert "Selected cities only" in out and "(47)" in out
    assert "+42 more" in out
    assert "kapruka_check_delivery" in out
    assert is_city_limited(d)


def test_describe_delivery_absent():
    assert describe_delivery(None) is None  # older API / search hits


# ── kapruka_get_product ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_product_renders_limited_delivery_line():
    _StubClient.payload = {
        "id": "amrith00100", "name": "Chicken Haryali Kebab",
        "price": {"amount": 1800, "currency": "LKR"}, "in_stock": True,
        "delivery": {"island_wide": False, "deliverable_city_count": 47,
                     "deliverable_cities": CITIES_47[:60]},
    }
    out = await kapruka_get_product(GetProductInput(product_id="amrith00100"))
    assert "**Delivery**: **Selected cities only** (47)" in out
    assert "+37 more" in out  # 10 shown, true total 47


@pytest.mark.asyncio
async def test_get_product_renders_island_wide():
    _StubClient.payload = {
        "id": "cake00ka001846", "name": "Cake", "price": {"amount": 4500, "currency": "LKR"},
        "in_stock": True, "delivery": {"island_wide": True},
    }
    out = await kapruka_get_product(GetProductInput(product_id="cake00ka001846"))
    assert "**Delivery**: Island-wide" in out


@pytest.mark.asyncio
async def test_get_product_json_passes_delivery_through():
    _StubClient.payload = {"id": "x", "name": "x", "delivery": {"island_wide": True}}
    out = json.loads(await kapruka_get_product(GetProductInput(product_id="xyz", response_format="json")))
    assert out["delivery"] == {"island_wide": True}


# ── kapruka_check_delivery ───────────────────────────────────────────────────

_BASE = {"city": "Jaffna", "now": "2026-09-11T13:40:00+05:30", "checked_date": "2026-09-20",
         "rate": 2500, "currency": "LKR"}


@pytest.mark.asyncio
async def test_check_delivery_forwards_product_id_upstream():
    _StubClient.payload = {**_BASE, "available": True, "item_deliverable": True}
    await kapruka_check_delivery(CheckDeliveryInput(city="Jaffna", delivery_date="2026-09-20",
                                                    product_id="cake00ka001846"))
    endpoint, params = _StubClient.calls[-1]
    assert endpoint == "delivery_check"
    assert params["product_id"] == "cake00ka001846"
    assert params["city"] == "Jaffna" and params["delivery_date"] == "2026-09-20"


@pytest.mark.asyncio
async def test_check_delivery_item_not_deliverable_lists_cities():
    _StubClient.payload = {**_BASE, "available": False,
                           "reason": "This item is not delivered to Jaffna.",
                           "item_deliverable": False, "deliverable_cities": CITIES_47}
    out = await kapruka_check_delivery(CheckDeliveryInput(city="Jaffna", delivery_date="2026-09-20",
                                                          product_id="amrith00100"))
    assert "`amrith00100` is not delivered to Jaffna" in out
    assert "City 01" in out and "+37 more" in out
    assert "Do not place the order to this city" in out
    assert out.count("not delivered to Jaffna") == 1  # API reason not duplicated


@pytest.mark.asyncio
async def test_check_delivery_item_block_plus_date_block_states_both():
    # Live behaviour: when both fail, `reason` carries only the date message.
    _StubClient.payload = {**_BASE, "available": False,
                           "reason": "Today's slots for Jaffna are full",
                           "next_available_date": "2026-09-21",
                           "item_deliverable": False, "deliverable_cities": COLOMBO}
    out = await kapruka_check_delivery(CheckDeliveryInput(city="Jaffna", product_id="amrith00100"))
    assert "is not delivered to Jaffna" in out
    assert "slots for Jaffna are full" in out
    assert "Next available date: **2026-09-21**" in out


@pytest.mark.asyncio
async def test_check_delivery_date_block_but_item_ok():
    _StubClient.payload = {**_BASE, "available": False, "reason": "Slots full",
                           "next_available_date": "2026-09-21", "item_deliverable": True}
    out = await kapruka_check_delivery(CheckDeliveryInput(city="Jaffna", product_id="cake00ka001846"))
    assert "Not available on this date" in out
    assert "itself can be delivered to Jaffna" in out
    assert "not delivered to" not in out


@pytest.mark.asyncio
async def test_check_delivery_available_with_item_ok():
    _StubClient.payload = {**_BASE, "city": "Colombo 03", "rate": 300, "available": True,
                           "item_deliverable": True}
    out = await kapruka_check_delivery(CheckDeliveryInput(city="Colombo 03", product_id="amrith00100"))
    assert "**Available** — flat rate LKR 300" in out
    assert "`amrith00100` can be delivered to Colombo 03" in out


@pytest.mark.asyncio
async def test_check_delivery_unknown_product_id_has_no_item_fields():
    # API silently ignores unknown ids -> behaves exactly as before.
    _StubClient.payload = {**_BASE, "available": True}
    out = await kapruka_check_delivery(CheckDeliveryInput(city="Jaffna", product_id="nope00000"))
    assert "**Available**" in out
    assert "can be delivered" not in out and "not delivered" not in out


@pytest.mark.asyncio
async def test_check_delivery_json_exposes_item_fields():
    _StubClient.payload = {**_BASE, "available": False, "item_deliverable": False,
                           "deliverable_cities": COLOMBO}
    out = json.loads(await kapruka_check_delivery(
        CheckDeliveryInput(city="Jaffna", product_id="amrith00100", response_format="json")))
    assert out["item_deliverable"] is False
    assert out["deliverable_cities"] == COLOMBO
    assert "perishable_warning" in out


# ── create_order: city_not_deliverable_for_item ──────────────────────────────


def test_city_not_deliverable_for_item_envelope_is_actionable():
    err = _http_422({"error": {
        "code": "city_not_deliverable_for_item",
        "message": "'Chicken Haryali Kebab' cannot be delivered to Jaffna.",
        "details": {"city": "Jaffna",
                    "items": [{"product_id": "amrith00100", "name": "Chicken Haryali Kebab"}],
                    "deliverable_cities": CITIES_47[:40], "deliverable_city_count": 47},
    }})
    out = handle_api_error(err)
    assert out.startswith("Error (city_not_deliverable_for_item): 'Chicken Haryali Kebab' cannot be delivered to Jaffna.")
    assert "Blocking item(s): Chicken Haryali Kebab (`amrith00100`)" in out
    assert "The whole cart can be delivered to: City 01" in out and "+37 more" in out
    assert "No order was created" in out
    assert "do not retry with the same city" in out


def test_mixed_cart_names_only_the_blocking_item():
    err = _http_422({"error": {
        "code": "city_not_deliverable_for_item",
        "message": "'Chicken Haryali Kebab' cannot be delivered to Jaffna.",
        "details": {"city": "Jaffna",
                    "items": [{"product_id": "amrith00100", "name": "Chicken Haryali Kebab"}],
                    "deliverable_cities": COLOMBO, "deliverable_city_count": 5},
    }})
    out = handle_api_error(err)
    assert "amrith00100" in out
    assert "cake00ka001846" not in out  # the island-wide cake is not a blocker


def test_other_422_codes_keep_one_line_format():
    err = _http_422({"error": {"code": "city_not_deliverable",
                               "message": "Jaffnaa is not a delivery city.",
                               "details": {"suggestions": ["Jaffna"]}}})
    assert handle_api_error(err) == "Error (city_not_deliverable): Jaffnaa is not a delivery city."
