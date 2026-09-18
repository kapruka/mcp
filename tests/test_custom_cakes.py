"""Custom cake tools: closed-MCP gate, image handling, cart lines, rendering.

Pure unit tests — upstream client and image downloads are stubbed.
Response shapes follow docs/custom_cake_mcp.md.
"""

import base64
import json
from types import SimpleNamespace

import httpx
import pytest

from src.api.client import handle_api_error
from src.config.settings import settings
from src.tools import custom_cakes as cc
from src.tools.custom_cakes import (
    CustomCakeOptionsInput,
    CustomCakeRequestInput,
    CustomCakeStatusInput,
    _validate_base64_image,
    kapruka_custom_cake_options,
    kapruka_custom_cake_request,
    kapruka_custom_cake_status,
)
from src.tools.orders import CartItem, CreateOrderInput

JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
EAGLE = "23.111.183.104"


def _ctx(ip: str | None = EAGLE, header: str = "cf-connecting-ip"):
    headers = {header: ip} if ip else {}
    request = SimpleNamespace(headers=headers, client=SimpleNamespace(host="127.0.0.1"))
    return SimpleNamespace(request_context=SimpleNamespace(request=request))


class _StubClient:
    payload: dict = {}
    calls: list = []

    async def post(self, endpoint, body, **query):
        _StubClient.calls.append((endpoint, body, query))
        return dict(_StubClient.payload)


@pytest.fixture(autouse=True)
def _stub(monkeypatch):
    _StubClient.calls = []
    monkeypatch.setattr(cc, "KaprukaClient", _StubClient)
    monkeypatch.setattr(settings, "custom_cake_trusted_ips", [EAGLE])


# ── gate ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gate_denies_untrusted_ip():
    _StubClient.payload = {"available": True}
    out = await kapruka_custom_cake_options(CustomCakeOptionsInput(), _ctx("203.0.113.9"))
    assert out.startswith("Error:") and "closed MCP" in out
    assert _StubClient.calls == []  # never reached upstream


@pytest.mark.asyncio
async def test_gate_allows_trusted_ip_via_cf_header():
    _StubClient.payload = {"available": True, "flavours": [], "sizes": [], "icing_colors": []}
    out = await kapruka_custom_cake_options(CustomCakeOptionsInput(), _ctx(EAGLE))
    assert "available choices" in out
    assert _StubClient.calls[0][0] == "custom_cake_options"


@pytest.mark.asyncio
async def test_gate_allows_trusted_ip_via_x_real_ip():
    _StubClient.payload = {"available": True}
    out = await kapruka_custom_cake_options(CustomCakeOptionsInput(), _ctx(EAGLE, "x-real-ip"))
    assert not out.startswith("Error:")


@pytest.mark.asyncio
async def test_gate_fails_closed_when_unconfigured(monkeypatch):
    monkeypatch.setattr(settings, "custom_cake_trusted_ips", [])
    out = await kapruka_custom_cake_options(CustomCakeOptionsInput(), _ctx(EAGLE))
    assert out.startswith("Error:")


@pytest.mark.asyncio
async def test_gate_denies_without_request_context():
    out = await kapruka_custom_cake_options(CustomCakeOptionsInput(), None)
    assert out.startswith("Error:")


# ── options rendering ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_options_unavailable_message():
    _StubClient.payload = {"available": False}
    out = await kapruka_custom_cake_options(CustomCakeOptionsInput(), _ctx())
    assert "currently unavailable" in out


@pytest.mark.asyncio
async def test_options_render_labels_values_and_pickups():
    _StubClient.payload = {
        "available": True,
        "flavours": [{"value": "vanilla", "label": "Ribbon"}],
        "sizes": ["1 KG", "2 KG"], "icing_colors": ["red"], "delivery_types": ["delivered", "pickup"],
        "pickup_locations": [{"id": 0, "name": "Kapruka - Mirihana", "address": "12 Main St", "city": "Mirihana", "times": "9-5"}],
        "greeting_min_length": 3, "greeting_max_length": 30, "image_formats": ["jpg", "png"],
        "image_max_bytes": 5242880, "quote_valid_days": 2,
    }
    out = await kapruka_custom_cake_options(CustomCakeOptionsInput(), _ctx())
    assert "Ribbon (`vanilla`)" in out
    assert "1 KG, 2 KG" in out
    assert "id `0` — Kapruka - Mirihana" in out
    assert "never" in out and "price" in out


# ── image handling ───────────────────────────────────────────────────────────


def test_base64_accepts_raw_and_data_url():
    raw = base64.b64encode(JPG).decode()
    assert _validate_base64_image(raw) == raw
    assert _validate_base64_image(f"data:image/jpeg;base64,{raw}") == raw
    assert _validate_base64_image(base64.b64encode(PNG).decode())


def test_base64_rejects_non_image_and_oversize():
    with pytest.raises(ValueError, match="JPG or PNG"):
        _validate_base64_image(base64.b64encode(b"GIF89a" + b"\x00" * 20).decode())
    with pytest.raises(ValueError, match="larger than"):
        _validate_base64_image(base64.b64encode(JPG).decode(), max_bytes=10)
    with pytest.raises(ValueError, match="not valid base64"):
        _validate_base64_image("@@@not-base64@@@")


@pytest.mark.asyncio
async def test_fetch_image_rejects_non_https_and_private():
    for bad in ("http://example.com/a.jpg", "https://127.0.0.1/a.jpg", "https://localhost/a.jpg", "ftp://x/a.jpg"):
        with pytest.raises(ValueError, match="public https"):
            await cc.fetch_image_base64(bad)


@pytest.mark.asyncio
async def test_fetch_image_downloads_and_encodes(monkeypatch):
    def handler(request):
        return httpx.Response(200, content=JPG, headers={"content-type": "image/jpeg"})

    real = httpx.AsyncClient

    def patched(**kw):
        return real(transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(cc.httpx, "AsyncClient", patched)
    out = await cc.fetch_image_base64("https://media.example.com/cake.jpg")
    assert base64.b64decode(out) == JPG


@pytest.mark.asyncio
async def test_fetch_image_enforces_size_cap(monkeypatch):
    def handler(request):
        return httpx.Response(200, content=JPG + b"\x00" * 1000)

    real = httpx.AsyncClient
    monkeypatch.setattr(cc.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    with pytest.raises(ValueError, match="larger than"):
        await cc.fetch_image_base64("https://media.example.com/cake.jpg", max_bytes=100)


# ── request ──────────────────────────────────────────────────────────────────


def _req(**over):
    base = dict(
        customer={"name": "Nimal Perera", "phone": "+94771234567"},
        flavour="chocolate", size="2 KG", greeting="Happy Birthday Amma", icing_color="red",
        delivery_date="2099-12-24", delivery_type="delivered", city="Kandy",
        image_base64=base64.b64encode(JPG).decode(),
    )
    base.update(over)
    return CustomCakeRequestInput(**base)


def test_request_input_shape_rules():
    with pytest.raises(ValueError, match="city is required"):
        _req(city=None)
    with pytest.raises(ValueError, match="pickup_location_id is required"):
        _req(delivery_type="pickup", city=None)
    with pytest.raises(ValueError, match="picture"):
        _req(image_base64=None)
    with pytest.raises(ValueError, match="not both"):
        _req(image_url="https://x.example/a.jpg")
    with pytest.raises(ValueError, match="in the past"):
        _req(delivery_date="2020-01-01")
    with pytest.raises(ValueError):
        _req(greeting="Hi")  # < 3 chars
    assert _req(delivery_type="pickup", city=None, pickup_location_id=0).pickup_location_id == 0


@pytest.mark.asyncio
async def test_request_sends_expected_body_and_keeps_request_id():
    _StubClient.payload = {"request_id": "1789714437864476831_customcake", "status": "pending_quote",
                           "image_url": "https://www.kapruka.com/cms_temp/x.jpg", "message": "ok"}
    out = await kapruka_custom_cake_request(_req(), _ctx())
    endpoint, body, _ = _StubClient.calls[-1]
    assert endpoint == "custom_cake_request"
    assert body["city"] == "Kandy" and "pickup_location_id" not in body
    assert body["customer"] == {"name": "Nimal Perera", "phone": "+94771234567"}
    assert body["greeting"] == "Happy Birthday Amma"
    assert len(body["idempotency_key"]) == 36
    assert base64.b64decode(body["image_base64"]) == JPG
    assert "`1789714437864476831_customcake`" in out
    assert "SMS at +94771234567" in out
    assert "No price has been set" in out


@pytest.mark.asyncio
async def test_request_pickup_sends_location_not_city():
    _StubClient.payload = {"request_id": "r1", "status": "pending_quote"}
    await kapruka_custom_cake_request(_req(delivery_type="pickup", city=None, pickup_location_id=0), _ctx())
    body = _StubClient.calls[-1][1]
    assert body["pickup_location_id"] == 0 and "city" not in body


@pytest.mark.asyncio
async def test_request_bad_image_is_customer_safe_error():
    out = await kapruka_custom_cake_request(_req(image_base64=base64.b64encode(b"GIF89a" + b"\x00" * 9).decode()), _ctx())
    assert out.startswith("Error (invalid_image)") and "resend" in out
    assert _StubClient.calls == []


# ── status ───────────────────────────────────────────────────────────────────

_QUOTED = {
    "request_id": "1789714437864476831_customcake", "status": "quoted",
    "image_url": "https://www.kapruka.com/cms_temp/x.jpg",
    "request": {"flavour": "chocolate", "size": "2 KG", "greeting": "Happy Birthday Amma", "icing_color": "red",
                "delivery_type": "delivered", "city": "Kandy", "delivery_address": "NA", "delivery_date": "2026-12-24"},
    "quote": {"items": [{"name": "Custom 2KG chocolate cake", "quantity": 1, "total": 12500},
                        {"name": "Sugar topper", "quantity": 2, "total": 1501}],
              "total": 14001, "currency": "LKR", "instructions": "Photo will be printed on edible sheet",
              "quoted_at": "2026-09-18T12:23:57+05:30", "expires_at": "2026-09-20T12:23:57+05:30",
              "quote_url": "https://www.kapruka.com/general/preview_order.jsp?id=1789714437864476831_customcake",
              "delivery_fee_note": "Delivery fee is added when the order is placed (create_order)."},
}


@pytest.mark.asyncio
async def test_status_pending():
    _StubClient.payload = {"request_id": "r1", "status": "pending_quote", "request": _QUOTED["request"]}
    out = await kapruka_custom_cake_status(CustomCakeStatusInput(request_id="r1abcd", phone="+94771234567"), _ctx())
    assert "pending_quote" in out and "not priced this yet" in out
    endpoint, body, query = _StubClient.calls[-1]
    assert endpoint == "custom_cake_status" and body == {"request_id": "r1abcd", "phone": "+94771234567"}
    assert query == {"currency": None}  # LKR default not sent


@pytest.mark.asyncio
async def test_status_quoted_renders_items_total_fee_note_and_next_step():
    _StubClient.payload = _QUOTED
    out = await kapruka_custom_cake_status(
        CustomCakeStatusInput(request_id="1789714437864476831_customcake", phone="+94771234567"), _ctx())
    assert "| Custom 2KG chocolate cake | 1 | LKR 12,500 |" in out
    assert "| Sugar topper | 2 | LKR 1,501 |" in out
    assert "**Cake total: LKR 14,001**" in out and "delivery fee is added" in out
    assert "**Note from staff:** Photo will be printed" in out
    assert "preview_order.jsp" in out
    assert '{"custom_cake_request_id": "1789714437864476831_customcake", "phone": "+94771234567"}' in out
    assert 'city "Kandy" and date 2026-12-24' in out


@pytest.mark.asyncio
async def test_status_expired_offers_resubmit():
    _StubClient.payload = {**_QUOTED, "status": "expired"}
    out = await kapruka_custom_cake_status(CustomCakeStatusInput(request_id="1789714437864476831_customcake", phone="0771234567"), _ctx())
    assert "expired on 2026-09-20" in out and "new request" in out


@pytest.mark.asyncio
async def test_status_usd_goes_on_query():
    _StubClient.payload = {**_QUOTED, "quote": {**_QUOTED["quote"], "currency": "USD", "total": 46.7, "items": []}}
    out = await kapruka_custom_cake_status(
        CustomCakeStatusInput(request_id="1789714437864476831_customcake", phone="+94771234567", currency="usd"), _ctx())
    assert _StubClient.calls[-1][2] == {"currency": "USD"}
    assert "USD 46.70" in out


def test_status_error_envelopes():
    request = httpx.Request("POST", "http://example.com")
    for code, status in (("request_not_found", 404), ("quote_not_ready", 409), ("quote_expired", 410)):
        resp = httpx.Response(status, request=request, headers={"content-type": "application/json"},
                              content=json.dumps({"error": {"code": code, "message": "m", "details": {}, "retryable": False}}).encode())
        assert handle_api_error(httpx.HTTPStatusError("", request=request, response=resp)) == f"Error ({code}): m"


# ── create_order cart lines ──────────────────────────────────────────────────


def test_cart_line_is_product_or_custom_cake():
    assert CartItem(product_id="cake00ka001537").to_api() == {"product_id": "cake00ka001537", "quantity": 1}
    assert CartItem(custom_cake_request_id="1789714437864476831_customcake", phone="+94771234567").to_api() == {
        "custom_cake_request_id": "1789714437864476831_customcake", "phone": "+94771234567"}
    with pytest.raises(ValueError, match="exactly one"):
        CartItem()
    with pytest.raises(ValueError, match="exactly one"):
        CartItem(product_id="cake00ka001537", custom_cake_request_id="abcdef1", phone="+94771234567")
    with pytest.raises(ValueError, match="phone"):
        CartItem(custom_cake_request_id="abcdef1")
    with pytest.raises(ValueError, match="quantity 1"):
        CartItem(custom_cake_request_id="abcdef1", phone="+94771234567", quantity=2)
    with pytest.raises(ValueError, match="icing_text"):
        CartItem(custom_cake_request_id="abcdef1", phone="+94771234567", icing_text="hi")
    with pytest.raises(ValueError, match="phone is only"):
        CartItem(product_id="cake00ka001537", phone="+94771234567")


def test_mixed_cart_serialises_both_kinds():
    order = CreateOrderInput(
        cart=[{"custom_cake_request_id": "1789714437864476831_customcake", "phone": "+94771234567"},
              {"product_id": "flowers00T1234", "quantity": 2}],
        recipient={"name": "A", "phone": "+94771234567"},
        delivery={"address": "1 Rd", "city": "Kandy", "date": "2099-12-24"},
        sender={"name": "B"},
    )
    assert [i.to_api() for i in order.cart] == [
        {"custom_cake_request_id": "1789714437864476831_customcake", "phone": "+94771234567"},
        {"product_id": "flowers00T1234", "quantity": 2},
    ]
