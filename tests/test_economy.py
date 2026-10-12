"""Economy delivery (commerce API 2026-10-12) and the MCP's policy on top of it.

Policy (Dulith, 2026-10-12): offer an optional economy choice only when it
saves at least ECONOMY_MIN_SAVING_LKR; never give the API's reasons; date
economy orders 4 days ahead (or the API's earliest_date when later).
Response shapes follow Kapruka's ECONOMY_DELIVERY_MCP.md and production.
"""

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from src.config.settings import settings
from src.economy import economy_date, economy_offer
from src.tools import delivery as delivery_tool
from src.tools import orders as orders_tool
from src.tools import products as products_tool
from src.tools.delivery import CheckDeliveryInput, kapruka_check_delivery
from src.tools.orders import CreateOrderInput, kapruka_create_order
from src.tools.products import GetProductInput, kapruka_get_product

LK = timezone(timedelta(hours=5, minutes=30))


def _in(days: int) -> str:
    return (datetime.now(LK).date() + timedelta(days=days)).isoformat()


def _check(fee=562, economy=None, available=True):
    out = {"city": "Galle", "checked_date": _in(2), "available": available,
           "delivery_fee": fee, "fee_currency": "LKR", "fee_basis": "cart",
           "fee_items_value": 2250, "rate": 575, "currency": "LKR"}
    if economy is not None:
        out["economy"] = economy
    return out


OPTIONAL = {"available": True, "forced": False, "fee": 480, "currency": "LKR",
            "promise": "Deliver within 3 to 5 days"}
FORCED = {"available": True, "forced": True, "fee": 480, "currency": "LKR",
          "promise": "Deliver within 1 to 3 days"}
REFUSED = {"available": False, "forced": False, "reason": "gift_order",
           "message": "Economy delivery is not available for gift orders (cakes, flowers, "
                      "birthday/anniversary/funeral messages)"}


class _Api:
    calls: list = []
    answer: dict = {}
    error: tuple | None = None   # (status, envelope)

    async def call(self, endpoint, **params):
        _Api.calls.append((endpoint, params))
        if endpoint == "product":
            return {"id": params["product_id"], "name": "Bear", "price": {"amount": 2250, "currency": "LKR"},
                    "in_stock": True, "economy_delivery": _Api.answer.get("economy_delivery")}
        return _Api.answer

    async def post(self, endpoint, body, **params):
        _Api.calls.append((endpoint, {"body": body, **params}))
        if _Api.error:
            status, env = _Api.error
            req = httpx.Request("POST", "https://x/commerce_phase1.jsp")
            raise httpx.HTTPStatusError("x", request=req, response=httpx.Response(status, json=env, request=req))
        return _Api.answer


@pytest.fixture(autouse=True)
def _stub(monkeypatch):
    _Api.calls, _Api.answer, _Api.error = [], {}, None
    for mod in (delivery_tool, orders_tool, products_tool):
        monkeypatch.setattr(mod, "KaprukaClient", _Api)
    monkeypatch.setattr(settings, "economy_min_saving_lkr", 50.0)


CART = [{"product_id": "EF_PC_SOFT0V2686POD00003", "quantity": 1}]


async def _check_md(**kw):
    kw.setdefault("city", "Galle")
    kw.setdefault("delivery_date", _in(2))
    kw.setdefault("cart", CART)
    return await kapruka_check_delivery(CheckDeliveryInput(**kw))


# ── policy helper ───────────────────────────────────────────────────────────


def test_economy_date_is_four_days_ahead_unless_api_says_later():
    assert economy_date() == _in(4)
    assert economy_date(_in(2)) == _in(4)
    assert economy_date(_in(7)) == _in(7)
    assert economy_date("garbage") == _in(4)


@pytest.mark.parametrize("standard,shown", [(562, True), (530, True), (529, False), (300, False)])
def test_optional_economy_needs_a_significant_saving(standard, shown):
    offer = economy_offer(_check(fee=standard, economy=OPTIONAL))
    assert (offer is not None) is shown
    if shown:
        assert offer["saving"] == standard - 480 and offer["delivery_date"] == _in(4)


def test_forced_economy_is_shown_whatever_the_saving():
    offer = economy_offer(_check(fee=300, economy={**FORCED, "fee": 295}))
    assert offer["forced"] is True and offer["fee"] == 295


def test_threshold_is_a_setting(monkeypatch):
    monkeypatch.setattr(settings, "economy_min_saving_lkr", 100.0)
    assert economy_offer(_check(fee=562, economy=OPTIONAL)) is None


def test_refused_or_absent_economy_offers_nothing():
    assert economy_offer(_check(economy=REFUSED)) is None
    assert economy_offer(_check()) is None


# ── kapruka_check_delivery ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_offers_significant_economy_with_promise_and_date():
    _Api.answer = _check(fee=562, economy=OPTIONAL)
    out = await _check_md(gift_message="With love")
    assert "Delivery fee for this cart: LKR 562" in out
    assert "Cheaper option — economy delivery: LKR 480** (saves LKR 82)" in out
    assert f"Deliver within 3 to 5 days; expected by" in out and _in(4) in out
    assert 'delivery.type: "economy"' in out
    endpoint, params = _Api.calls[-1]
    assert params["gift_message"] == "With love"


@pytest.mark.asyncio
async def test_check_hides_insignificant_economy():
    _Api.answer = _check(fee=300, economy={**OPTIONAL, "fee": 295})
    out = await _check_md(city="Colombo 03")
    assert "economy" not in out.lower()
    assert "Delivery fee for this cart: LKR 300" in out


@pytest.mark.asyncio
async def test_check_never_gives_the_reason():
    _Api.answer = _check(economy=REFUSED)
    out = await _check_md()
    assert "economy" not in out.lower() and "gift" not in out.lower()
    js = json.loads(await _check_md(response_format="json"))
    assert js["economy"] == {"available": False}


@pytest.mark.asyncio
async def test_check_forced_economy_replaces_the_standard_fee():
    _Api.answer = _check(fee=562, economy={**FORCED, "earliest_date": _in(6)})
    out = await _check_md()
    assert "LKR 562" not in out
    assert "These items go by economy delivery: LKR 480" in out
    assert "Deliver within 1 to 3 days; expected by" in out and _in(6) in out
    assert "only option" in out


@pytest.mark.asyncio
async def test_check_json_carries_policy_block():
    _Api.answer = _check(fee=562, economy=OPTIONAL)
    js = json.loads(await _check_md(response_format="json"))
    assert js["economy"] == {"available": True, "forced": False, "fee": 480, "currency": "LKR",
                             "promise": "Deliver within 3 to 5 days", "delivery_date": _in(4),
                             "saving": 82}


@pytest.mark.asyncio
async def test_gift_message_not_sent_without_cart():
    _Api.answer = _check()
    await kapruka_check_delivery(CheckDeliveryInput(city="Galle", gift_message="Happy Birthday"))
    assert "gift_message" not in {k for k, v in _Api.calls[-1][1].items() if v is not None}


# ── kapruka_create_order ────────────────────────────────────────────────────


def _order(**delivery):
    d = {"address": "1 Main St", "city": "Galle", "date": _in(1), **delivery}
    return CreateOrderInput(cart=CART, recipient={"name": "A", "phone": "+94771234567"},
                            delivery=d, sender={"name": "B"})


def _ok(**summary):
    return {"order_ref": "ORD-1", "checkout_url": "https://pay/x",
            "summary": {"items_total": 2250, "delivery_fee": 480, "addons_total": 0,
                        "grand_total": 2730, "currency": "LKR", **summary}}


@pytest.mark.asyncio
async def test_economy_order_is_sent_four_days_ahead_and_reported():
    _Api.answer = _ok(delivery_type="economy", delivery_date=_in(4),
                      delivery_promise="Deliver within 3 to 5 days")
    out = await kapruka_create_order(_order(type="economy"))
    sent = _Api.calls[-1][1]["body"]["delivery"]
    assert sent["type"] == "economy" and sent["date"] == _in(4)
    assert f"economy — Deliver within 3 to 5 days; expected by {_in(4)}" in out


@pytest.mark.asyncio
async def test_standard_order_sends_no_type_and_keeps_its_date():
    _Api.answer = _ok(delivery_type="standard", delivery_date=_in(1), delivery_fee=562)
    out = await kapruka_create_order(_order())
    sent = _Api.calls[-1][1]["body"]["delivery"]
    assert "type" not in sent and sent["date"] == _in(1)
    assert "economy" not in out.lower() and "moved" not in out


@pytest.mark.asyncio
async def test_economy_only_items_sent_as_standard_are_flagged():
    _Api.answer = _ok(delivery_type="economy", delivery_date=_in(3),
                      delivery_promise="Deliver within 1 to 3 days")
    out = await kapruka_create_order(_order())
    assert f"expected by {_in(3)}" in out
    assert "economy delivery only" in out


@pytest.mark.asyncio
async def test_moved_standard_date_is_flagged():
    _Api.answer = _ok(delivery_type="standard", delivery_date=_in(2))
    out = await kapruka_create_order(_order())
    assert f"**Delivery date:** {_in(2)} (checkout moved it from {_in(1)}" in out


@pytest.mark.asyncio
async def test_economy_refusal_hides_the_reason():
    _Api.error = (422, {"error": {"code": "economy_not_available", "message": "Economy delivery is "
                                  "not available for gift orders", "details": {"reason": "gift_order"}}})
    out = await kapruka_create_order(_order(type="economy"))
    assert out.startswith("Error (economy_not_available)")
    assert "gift" not in out.lower() and "standard" in out


def test_unknown_delivery_type_is_rejected_before_the_api():
    with pytest.raises(Exception, match="standard' or 'economy"):
        _order(type="express")


# ── kapruka_get_product ─────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("value,text", [
    ("optional", "possible for this item"),
    ("only", "within 1 to 3 days"),
    ("only_5to7", "within 5 to 7 days"),
    (None, None),
])
async def test_product_economy_line(value, text):
    _Api.answer = {"economy_delivery": value}
    out = await kapruka_get_product(GetProductInput(product_id="EF_PC_SOFT0V2686POD00003"))
    if text:
        assert f"**Economy delivery**: " in out and text in out
    else:
        assert "Economy" not in out
