"""UCP Level 1 (src/ucp): profile, negotiation, catalog, checkout over REST and MCP.

Kapruka's commerce API and the agent's profile are stubbed; every response is
validated against the official UCP 2026-08-25 JSON schemas (tests/ucp_schemas).
"""

import json
import pathlib
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from starlette.testclient import TestClient

from src.config.settings import settings
from src.ucp import VERSION, catalog as ucp_catalog, checkout as ucp_checkout, negotiation
from src.ucp.profile import business_profile
from src.ucp.store import Store, set_store

LK = timezone(timedelta(hours=5, minutes=30))
AGENT = "https://agent.example/.well-known/ucp"
BEAR = "EF_PC_SOFT0V2686POD00003"
SHIRT = "EF_PC_CLOT0V160POD02119FDP"
CAKE = "CAKE00KA001719"


def _day(n: int) -> str:
    return (datetime.now(LK).date() + timedelta(days=n)).isoformat()


# ── Official schemas ─────────────────────────────────────────────────────────

_SCHEMA_DIR = pathlib.Path(__file__).parent / "ucp_schemas"
_REGISTRY = Registry().with_resources(
    (doc["$id"], Resource.from_contents(doc))
    for doc in (json.loads(p.read_text(encoding="utf-8")) for p in _SCHEMA_DIR.rglob("*.json"))
    if "$id" in doc
)


def assert_valid(instance, ref: str):
    validator = Draft202012Validator({"$ref": f"https://ucp.dev/schemas/{ref}"}, registry=_REGISTRY)
    errors = sorted(validator.iter_errors(instance), key=lambda e: list(e.path))
    assert not errors, "\n".join(f"{list(e.path)}: {e.message}" for e in errors[:5])


CHECKOUT_SCHEMA = "shopping/fulfillment.json#/$defs/dev.ucp.shopping.checkout"
ERROR_SCHEMA = "common/types/error_response.json"


# ── Fakes ────────────────────────────────────────────────────────────────────


def _money(amount, cur="LKR"):
    return {"amount": amount, "currency": cur}


PRODUCTS = {
    BEAR: {"id": BEAR, "name": "Breathing Teddy Bear", "summary": "A teddy that breathes.",
           "description": "A teddy that breathes.", "price": _money(2250), "compare_at_price": _money(3250),
           "in_stock": True, "url": "https://www.kapruka.com/buyonline/bear/kid/bear",
           "images": ["https://static.kapruka.com/bear.jpg"], "category": {"name": "Soft Toys"},
           "delivery": {"island_wide": True},
           "variants": [{"id": BEAR + "_default", "name": "Default", "sku": BEAR, "price": _money(2250),
                         "compare_at_price": _money(3250), "in_stock": True, "attributes": {"weight": "0"}}]},
    SHIRT: {"id": SHIRT, "name": "Adam Cotton Shirt", "summary": "Cotton shirt.", "price": _money(4950),
            "in_stock": True, "url": "https://www.kapruka.com/buyonline/shirt/kid/shirt",
            "images": [], "category": {"name": "Clothing"}, "delivery": {"island_wide": True},
            "variants": [
                {"id": SHIRT + "_TC1", "name": "S", "sku": SHIRT + "_TC1", "price": _money(4950),
                 "in_stock": True, "attributes": {"size": "S"}},
                {"id": SHIRT + "_TC2", "name": "M", "sku": SHIRT + "_TC2", "price": _money(4950),
                 "in_stock": False, "attributes": {"size": "M"}},
                {"id": SHIRT + "_TC3", "name": "L", "sku": SHIRT + "_TC3", "price": _money(4950),
                 "in_stock": True, "attributes": {"size": "L"}},
            ]},
    CAKE: {"id": CAKE, "name": "Little Racer Cake", "summary": "Cake.", "price": _money(5900),
           "in_stock": True, "url": "https://www.kapruka.com/buyonline/cake/kid/cake", "images": [],
           "delivery": {"island_wide": False, "deliverable_cities": ["Colombo 03"]},
           "variants": [{"id": CAKE + "_default", "name": "Default", "price": _money(5900), "in_stock": True}]},
}
CITIES = [{"name": "Colombo 03", "aliases": ["kollupitiya", "none"]},
          {"name": "Galle", "aliases": ["none"]}, {"name": "Kandy", "aliases": ["none"]}]
FEES = {"Galle": 562, "Colombo 03": 300, "Kandy": 562}
ECONOMY = {"Galle": 480, "Colombo 03": 295, "Kandy": 480}


class Api:
    calls: list = []
    orders: list = []
    order_fail: dict | None = None

    async def call(self, endpoint, **p):
        Api.calls.append((endpoint, p))
        cur = p.get("currency") or "LKR"
        if endpoint == "product":
            pid = p["product_id"]
            if pid in PRODUCTS:
                return _in(PRODUCTS[pid], cur)
            for prod in PRODUCTS.values():
                for v in prod["variants"]:
                    if v["id"] == pid and not pid.endswith("_default"):
                        return _in({**prod, "id": pid, "name": f"{prod['name']} {v['name']}",
                                    "price": v["price"], "in_stock": v["in_stock"], "variants": [v]}, cur)
            raise _http_error(404, "product_not_found")
        if endpoint == "products_search":
            rows = [{"id": pid, "type": "product", "name": d["name"], "summary": d["summary"],
                     "price": d["price"], "compare_at_price": d.get("compare_at_price"),
                     "in_stock": d["in_stock"], "url": d["url"], "image_url": "https://static.kapruka.com/x.jpg",
                     "category": {"name": "Soft Toys"}} for pid, d in PRODUCTS.items()]
            hi = p.get("max_price")
            rows = [r for r in rows if hi is None or r["price"]["amount"] <= hi]
            return {"results": [_in(r, cur) for r in rows], "next_cursor": "Mg==", "total_estimate": len(rows)}
        if endpoint == "delivery_cities":
            return {"cities": CITIES}
        if endpoint == "delivery_check":
            city, day = p["city"], p["delivery_date"]
            if p.get("product_id"):
                prod = PRODUCTS.get(p["product_id"], {})
                ok = (prod.get("delivery") or {}).get("island_wide", True) or \
                    city in (prod.get("delivery") or {}).get("deliverable_cities", [])
                return {"available": ok, "item_deliverable": ok,
                        "deliverable_cities": [] if ok else prod["delivery"]["deliverable_cities"]}
            out = {"city": city, "checked_date": day, "available": day != _day(0),
                   "next_available_date": _day(1) if day == _day(0) else None,
                   "delivery_fee": FEES[city] if cur == "LKR" else 6.41,
                   "fee_currency": "LKR" if cur == "LKR" else "USD", "fee_basis": "cart", "currency": "LKR"}
            if p.get("cart"):
                ids = [t.split(":")[0] for t in p["cart"].split(",")]
                eligible = all(i.startswith("EF_PC") for i in ids)
                out["economy"] = ({"available": True, "forced": False, "fee": ECONOMY[city], "currency": "LKR",
                                   "promise": "Deliver within 3 to 5 days"} if eligible and cur == "LKR"
                                  else {"available": False, "forced": False, "reason": "items_not_eligible"})
            return out
        raise AssertionError(f"unexpected endpoint {endpoint}")

    async def post(self, endpoint, body, **p):
        assert endpoint == "create_order"
        Api.orders.append({"body": body, **p})
        if Api.order_fail:
            raise _http_error(422, Api.order_fail["code"])
        cur = p.get("currency") or "LKR"
        items = sum(_in(PRODUCTS.get(c["product_id"]) or _variant_product(c["product_id"]), cur)["price"]["amount"]
                    * c["quantity"] for c in body["cart"])
        d = body["delivery"]
        fee = ECONOMY[d["city"]] if d.get("type") == "economy" else (FEES[d["city"]] if cur == "LKR" else 6.41)
        n = len(Api.orders)
        return {"order_ref": f"ORD-TEST-{n}", "checkout_url": f"https://www.kapruka.com/pay/{n}",
                "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                "summary": {"items_total": items, "delivery_fee": fee, "addons_total": 0,
                            "grand_total": round(items + fee, 2), "currency": cur}}


def _variant_product(vid):
    for prod in PRODUCTS.values():
        for v in prod["variants"]:
            if v["id"] == vid:
                return {**prod, "price": v["price"]}
    raise _http_error(404, "product_not_found")


def _in(d: dict, cur: str) -> dict:
    if cur == "LKR":
        return d
    k = 270.0

    def conv(m):
        return {"amount": round(m["amount"] / k, 2), "currency": cur} if isinstance(m, dict) else m
    out = {**d, "price": conv(d.get("price")), "compare_at_price": conv(d.get("compare_at_price"))}
    if d.get("variants"):
        out["variants"] = [{**v, "price": conv(v.get("price")), "compare_at_price": conv(v.get("compare_at_price"))}
                           for v in d["variants"]]
    return out


def _http_error(status, code):
    req = httpx.Request("GET", "https://www.kapruka.com/tools/commerce_phase1.jsp")
    return httpx.HTTPStatusError(code, request=req, response=httpx.Response(
        status, json={"error": {"code": code, "message": code.replace("_", " ")}}, request=req))


PLATFORM = {"ucp": {"version": VERSION, "services": {}, "payment_handlers": {}, "capabilities": {
    "dev.ucp.shopping.checkout": [{"version": VERSION}],
    "dev.ucp.shopping.fulfillment": [{"version": VERSION, "extends": "dev.ucp.shopping.checkout"}],
    "dev.ucp.shopping.catalog.search": [{"version": VERSION}],
    "dev.ucp.shopping.catalog.lookup": [{"version": VERSION}],
}}}


class Agent:
    profiles: dict = {}
    fetches: list = []

    @classmethod
    def handler(cls, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        cls.fetches.append(url)
        spec = cls.profiles.get(url, PLATFORM)
        if isinstance(spec, httpx.Response):
            return spec
        return httpx.Response(200, json=spec, headers={"Cache-Control": "public, max-age=300"})


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    Api.calls, Api.orders, Api.order_fail = [], [], None
    Agent.profiles, Agent.fetches = {}, []
    monkeypatch.setattr(ucp_catalog, "KaprukaClient", Api)
    monkeypatch.setattr(ucp_checkout, "KaprukaClient", Api)
    monkeypatch.setattr(negotiation, "_TRANSPORT", httpx.MockTransport(Agent.handler))

    async def public(host, port):
        return None
    monkeypatch.setattr(negotiation, "_check_addresses", public)
    negotiation._cache.clear()
    negotiation._budget.clear()
    ucp_checkout._mints.clear()
    monkeypatch.setattr(settings, "economy_min_saving_lkr", 50.0)
    monkeypatch.setattr(settings, "ucp_mint_per_hour", 30)
    set_store(Store(":memory:"))
    yield
    set_store(None)


@pytest.fixture
def client():
    from src.server import build_app, mcp
    mcp._session_manager = None    # FastMCP's manager can run once; each test app gets a fresh one
    with TestClient(build_app(), base_url="http://localhost:8000") as c:
        yield c


H = {"UCP-Agent": f'profile="{AGENT}"'}


def _rest(client, method, path, body=None, headers=None, key=None):
    h = {**H, **(headers or {})}
    if key:
        h["Idempotency-Key"] = key
    return client.request(method, f"/ucp/v1{path}", json=body, headers=h)


GALLE = {"first_name": "Nimal", "last_name": "Perera", "phone_number": "+94771234567",
         "street_address": "12 Lighthouse St", "address_locality": "Galle", "address_country": "LK"}
BUYER = {"first_name": "Amara", "last_name": "Silva", "email": "amara@example.com"}


def _full(dest=GALLE, item=BEAR, buyer=BUYER, **extra):
    return {"line_items": [{"item": {"id": item}, "quantity": 1}], "buyer": buyer,
            "fulfillment": {"methods": [{"type": "shipping", "destinations": [dest]}]}, **extra}


# ── Profile ──────────────────────────────────────────────────────────────────


def test_business_profile_is_valid_and_points_here():
    p = business_profile()
    assert_valid(p, "profile.json#/$defs/business_schema")
    svc = {s["transport"]: s["endpoint"] for s in p["ucp"]["services"]["dev.ucp.shopping"]}
    assert svc == {"rest": "https://mcp.kapruka.com/ucp/v1", "mcp": "https://mcp.kapruka.com/ucp/mcp"}
    assert p["ucp"]["payment_handlers"] == {}
    assert all(e[0]["version"] == VERSION for e in p["ucp"]["capabilities"].values())


def test_profile_served_with_cache_headers(client):
    r = client.get("/.well-known/ucp")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    assert "public" in r.headers["cache-control"] and "max-age=3600" in r.headers["cache-control"]
    assert r.json() == business_profile()
    assert client.get("/.well-known/ucp", headers={"If-None-Match": r.headers["etag"]}).status_code == 304


def test_web_copy_of_profile_matches(tmp_path):
    web = pathlib.Path("C:/KaprukaShoppingApp/web/.well-known/ucp")
    if web.exists():
        assert json.loads(web.read_text(encoding="utf-8")) == business_profile()


# ── Negotiation ──────────────────────────────────────────────────────────────


def test_missing_agent_profile_is_400(client):
    r = client.post("/ucp/v1/checkout-sessions", json=_full())
    assert r.status_code == 400 and r.json()["code"] == "invalid_profile_url"


def test_old_protocol_version_is_unsupported(client):
    Agent.profiles[AGENT] = {"ucp": {**PLATFORM["ucp"], "version": "2026-04-08"}}
    r = _rest(client, "POST", "/checkout-sessions", _full())
    assert r.status_code == 422 and r.json()["code"] == "version_unsupported"


def test_no_shared_capability_is_a_200_error_response(client):
    Agent.profiles[AGENT] = {"ucp": {**PLATFORM["ucp"], "capabilities": {
        "dev.ucp.shopping.checkout": [{"version": "2026-04-08"}]}}}
    r = _rest(client, "POST", "/checkout-sessions", _full())
    assert r.status_code == 200
    assert_valid(r.json(), ERROR_SCHEMA)
    assert r.json()["messages"][0]["code"] == "capabilities_incompatible"


def test_redirecting_profile_is_unreachable_and_cached_as_failure(client):
    Agent.profiles[AGENT] = httpx.Response(302, headers={"Location": "https://elsewhere.example/"})
    assert _rest(client, "POST", "/checkout-sessions", _full()).json()["code"] == "profile_unreachable"
    _rest(client, "POST", "/checkout-sessions", _full())
    assert len(Agent.fetches) == 1       # backed off, not refetched


def test_profile_is_cached():
    for _ in range(3):
        import asyncio
        asyncio.run(negotiation.resolve(AGENT))
    assert len(Agent.fetches) == 1


@pytest.mark.parametrize("url", ["http://agent.example/p", "https://user:pw@agent.example/p", "nonsense"])
def test_unsafe_profile_urls_rejected(url):
    with pytest.raises(negotiation.UcpError) as e:
        negotiation._check_url(url)
    assert e.value.code == "invalid_profile_url"


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.5", "169.254.169.254", "192.168.1.1", "::1", "100.64.0.1"])
async def test_profiles_resolving_to_special_addresses_rejected(monkeypatch, ip):
    import socket

    from src.ucp.negotiation import _check_addresses as real  # noqa: F401
    monkeypatch.undo()   # restore the real _check_addresses
    loop_infos = [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))]

    class Loop:
        async def getaddrinfo(self, *a, **k):
            return loop_infos
    monkeypatch.setattr(negotiation.asyncio, "get_running_loop", lambda: Loop())
    with pytest.raises(negotiation.UcpError) as e:
        await negotiation._check_addresses("agent.example", 443)
    assert e.value.code == "invalid_profile_url"


# ── Checkout over REST ───────────────────────────────────────────────────────


def test_create_with_only_items_says_what_is_missing(client):
    r = _rest(client, "POST", "/checkout-sessions", {"line_items": [{"item": {"id": BEAR}, "quantity": 2}]})
    assert r.status_code == 201
    co = r.json()
    assert_valid(co, CHECKOUT_SCHEMA)
    assert co["status"] == "incomplete" and co["currency"] == "LKR"
    assert co["line_items"][0]["item"] == {"id": BEAR, "title": "Breathing Teddy Bear", "price": 225000,
                                           "image_url": "https://static.kapruka.com/bear.jpg"}
    assert co["totals"] == [{"type": "subtotal", "amount": 450000}, {"type": "total", "amount": 450000}]
    paths = {m.get("path") for m in co["messages"]}
    assert {"$.fulfillment.methods[0].destinations", "$.buyer.first_name"} <= paths
    assert co["continue_url"] == PRODUCTS[BEAR]["url"]
    assert {link["type"] for link in co["links"]} >= {"privacy_policy", "terms_of_service", "refund_policy"}
    assert not Api.orders


def test_full_galle_checkout_offers_economy_and_hands_off_to_kapruka(client):
    co = _rest(client, "POST", "/checkout-sessions", _full()).json()
    assert_valid(co, CHECKOUT_SCHEMA)
    group = co["fulfillment"]["methods"][0]["groups"][0]
    ids = [o["id"] for o in group["options"]]
    assert ids == ["economy", f"standard:{_day(1)}", f"standard:{_day(2)}", f"standard:{_day(3)}"]
    eco = group["options"][0]
    assert eco["totals"] == [{"type": "total", "amount": 48000}]
    assert eco["description"]["plain"].startswith("Deliver within 3 to 5 days; expected by ")
    # Economy is cheapest so it is listed first, but the default is the earliest date.
    assert group["selected_option_id"] == f"standard:{_day(1)}"
    assert co["status"] == "requires_escalation"
    assert co["continue_url"] == "https://www.kapruka.com/pay/1"
    assert co["totals"] == [{"type": "subtotal", "amount": 225000},
                            {"type": "fulfillment", "display_text": "Delivery", "amount": 56200},
                            {"type": "total", "amount": 281200}]
    pay = [m for m in co["messages"] if m["code"] == "payment_required"]
    assert pay and pay[0]["severity"] == "requires_buyer_input"
    assert Api.orders[0]["body"]["delivery"] == {"address": "12 Lighthouse St", "city": "Galle",
                                                 "date": _day(1), "location_type": "house"}
    # Choosing economy: dated 4 days ahead, sent as type economy.
    upd = {**_full(), "fulfillment": {"methods": [{"id": "shipping_1", "line_item_ids": ["li_1"],
                                                    "groups": [{"id": "group_1", "selected_option_id": "economy"}]}]}}
    co = _rest(client, "PUT", f"/checkout-sessions/{co['id']}", upd).json()
    assert co["totals"][-1] == {"type": "total", "amount": 273000}
    sent = Api.orders[-1]["body"]
    assert sent["delivery"] == {"address": "12 Lighthouse St", "city": "Galle", "date": _day(4),
                                "location_type": "house", "type": "economy"}
    assert sent["recipient"] == {"name": "Nimal Perera", "phone": "+94771234567"}
    assert sent["sender"]["name"] == "Amara Silva"
    assert sent["cart"] == [{"product_id": BEAR, "quantity": 1}]


def test_choosing_a_standard_date_reprices_and_remints(client):
    co = _rest(client, "POST", "/checkout-sessions", _full()).json()
    upd = {**_full(), "fulfillment": {"methods": [{"id": "shipping_1", "line_item_ids": ["li_1"],
                                                    "groups": [{"id": "group_1",
                                                                "selected_option_id": f"standard:{_day(2)}"}]}]}}
    co2 = _rest(client, "PUT", f"/checkout-sessions/{co['id']}", upd).json()
    assert_valid(co2, CHECKOUT_SCHEMA)
    assert co2["fulfillment"]["methods"][0]["groups"][0]["selected_option_id"] == f"standard:{_day(2)}"
    assert co2["fulfillment"]["methods"][0]["destinations"][0]["address_locality"] == "Galle"  # kept
    assert co2["totals"][1]["amount"] == 56200 and co2["totals"][-1]["amount"] == 281200
    assert Api.orders[-1]["body"]["delivery"]["date"] == _day(2)
    assert "type" not in Api.orders[-1]["body"]["delivery"]
    assert co2["continue_url"] == "https://www.kapruka.com/pay/2"


def test_get_does_not_remint_and_unchanged_update_reuses_link(client):
    co = _rest(client, "POST", "/checkout-sessions", _full()).json()
    got = _rest(client, "GET", f"/checkout-sessions/{co['id']}").json()
    assert got["continue_url"] == co["continue_url"]
    again = _rest(client, "PUT", f"/checkout-sessions/{co['id']}", _full()).json()
    assert again["continue_url"] == co["continue_url"]
    assert len(Api.orders) == 1


def test_colombo_small_saving_hides_economy(client):
    dest = {**GALLE, "address_locality": "Kollupitiya"}       # alias of Colombo 03
    co = _rest(client, "POST", "/checkout-sessions", _full(dest=dest)).json()
    ids = [o["id"] for o in co["fulfillment"]["methods"][0]["groups"][0]["options"]]
    assert "economy" not in ids and ids[0] == f"standard:{_day(1)}"
    assert Api.orders[0]["body"]["delivery"]["city"] == "Colombo 03"


def test_usd_checkout_has_no_economy_and_charges_usd(client):
    co = _rest(client, "POST", "/checkout-sessions", _full(context={"address_country": "US"})).json()
    assert_valid(co, CHECKOUT_SCHEMA)
    assert co["currency"] == "USD"
    assert all(o["id"] != "economy" for o in co["fulfillment"]["methods"][0]["groups"][0]["options"])
    assert co["line_items"][0]["item"]["price"] == 833
    assert Api.orders[0]["currency"] == "USD"


def test_unknown_city_is_recoverable_with_suggestion(client):
    co = _rest(client, "POST", "/checkout-sessions", _full(dest={**GALLE, "address_locality": "Colombo"})).json()
    err = next(m for m in co["messages"] if m["code"] == "address_undeliverable")
    assert err["severity"] == "recoverable"
    assert err["path"] == "$.fulfillment.methods[0].destinations[0].address_locality"
    assert "Colombo 03" in err["content"]
    assert co["status"] == "incomplete" and not Api.orders


def test_outside_sri_lanka_is_undeliverable(client):
    co = _rest(client, "POST", "/checkout-sessions", _full(dest={**GALLE, "address_country": "US"})).json()
    assert any(m["code"] == "address_undeliverable" and m["path"].endswith("address_country")
               for m in co["messages"])


def test_city_limited_item_blocks_with_its_cities(client):
    co = _rest(client, "POST", "/checkout-sessions", _full(item=CAKE)).json()
    assert_valid(co, CHECKOUT_SCHEMA)
    err = next(m for m in co["messages"] if m["code"] == "address_undeliverable")
    assert err["path"] == "$.line_items[0]" and "Colombo 03" in err["content"]
    assert co["status"] == "incomplete" and not Api.orders


def test_product_with_sizes_needs_a_variant(client):
    co = _rest(client, "POST", "/checkout-sessions", _full(item=SHIRT)).json()
    err = next(m for m in co["messages"] if m["code"] == "variant_required")
    assert err["path"] == "$.line_items[0].item.id" and "Size" in err["content"]
    co2 = _rest(client, "POST", "/checkout-sessions", _full(item=SHIRT + "_TC3")).json()
    assert co2["status"] == "requires_escalation"
    assert co2["line_items"][0]["item"]["title"] == "Adam Cotton Shirt (L)"
    assert Api.orders[-1]["body"]["cart"] == [{"product_id": SHIRT + "_TC3", "quantity": 1}]


def test_out_of_stock_variant(client):
    co = _rest(client, "POST", "/checkout-sessions", _full(item=SHIRT + "_TC2")).json()
    assert any(m["code"] == "out_of_stock" for m in co["messages"]) and not Api.orders


def test_unknown_item_dropped_or_error_response(client):
    r = _rest(client, "POST", "/checkout-sessions", {"line_items": [{"item": {"id": "NOPE123"}, "quantity": 1}]})
    assert r.status_code == 200
    assert_valid(r.json(), ERROR_SCHEMA)
    co = _rest(client, "POST", "/checkout-sessions", {"line_items": [
        {"item": {"id": "NOPE123"}, "quantity": 1}, {"item": {"id": BEAR}, "quantity": 1}]}).json()
    assert [li["item"]["id"] for li in co["line_items"]] == [BEAR]
    assert any(m["code"] == "item_unavailable" for m in co["messages"])


def test_missing_phone_and_bad_phone(client):
    no_phone = {k: v for k, v in GALLE.items() if k != "phone_number"}
    co = _rest(client, "POST", "/checkout-sessions", _full(dest=no_phone)).json()
    assert any(m["path"].endswith("phone_number") for m in co["messages"] if m["type"] == "error")
    co = _rest(client, "POST", "/checkout-sessions", _full(dest={**GALLE, "phone_number": "abc"})).json()
    assert any(m["code"] == "invalid_phone" for m in co["messages"])
    assert not Api.orders


def test_kapruka_refusal_becomes_recoverable_message(client):
    Api.order_fail = {"code": "city_not_deliverable_for_item"}
    co = _rest(client, "POST", "/checkout-sessions", _full()).json()
    assert co["status"] == "incomplete"
    assert any(m["code"] == "address_undeliverable" for m in co["messages"])


def test_complete_requires_key_and_is_a_handoff(client):
    co = _rest(client, "POST", "/checkout-sessions", _full()).json()
    r = _rest(client, "POST", f"/checkout-sessions/{co['id']}/complete", {"payment": {}})
    assert r.status_code == 400
    key = str(uuid.uuid4())
    done = _rest(client, "POST", f"/checkout-sessions/{co['id']}/complete", {"payment": {}}, key=key).json()
    assert_valid(done, CHECKOUT_SCHEMA)
    assert done["status"] == "requires_escalation" and "order" not in done
    assert done["continue_url"] == co["continue_url"]
    replay = _rest(client, "POST", f"/checkout-sessions/{co['id']}/complete", {"payment": {}}, key=key)
    assert replay.json() == done
    clash = _rest(client, "POST", f"/checkout-sessions/{co['id']}/complete", {"payment": {"x": 1}}, key=key)
    assert clash.status_code == 409


def test_cancel_is_terminal(client):
    co = _rest(client, "POST", "/checkout-sessions", _full()).json()
    c = _rest(client, "POST", f"/checkout-sessions/{co['id']}/cancel", key=str(uuid.uuid4())).json()
    assert_valid(c, CHECKOUT_SCHEMA)
    assert c["status"] == "canceled" and "continue_url" not in c
    upd = _rest(client, "PUT", f"/checkout-sessions/{co['id']}", _full()).json()
    assert upd["status"] == "canceled" and upd["messages"][0]["code"] == "checkout_canceled"


def test_unknown_checkout_is_404(client):
    r = _rest(client, "GET", "/checkout-sessions/chk_nope")
    assert r.status_code == 404
    assert_valid(r.json(), ERROR_SCHEMA)


def test_without_fulfillment_extension_hands_off_to_site(client):
    Agent.profiles[AGENT] = {"ucp": {**PLATFORM["ucp"], "capabilities": {
        "dev.ucp.shopping.checkout": [{"version": VERSION}]}}}
    co = _rest(client, "POST", "/checkout-sessions", _full()).json()
    assert_valid(co, "shopping/checkout.json")
    assert co["status"] == "requires_escalation" and "fulfillment" not in co
    assert co["continue_url"] == PRODUCTS[BEAR]["url"]
    assert set(co["ucp"]["capabilities"]) == {"dev.ucp.shopping.checkout"}


def test_mint_budget_is_per_ip(client, monkeypatch):
    monkeypatch.setattr(settings, "ucp_mint_per_hour", 1)
    assert _rest(client, "POST", "/checkout-sessions", _full()).status_code == 201
    r = _rest(client, "POST", "/checkout-sessions", _full(dest={**GALLE, "street_address": "13 Other St"}))
    assert r.status_code == 429 and r.headers["retry-after"] == "600"


def test_malformed_requests_are_400(client):
    assert _rest(client, "POST", "/checkout-sessions", {"line_items": []}).status_code == 400
    assert _rest(client, "POST", "/checkout-sessions",
                 {"line_items": [{"item": {"id": BEAR}, "quantity": 0}]}).status_code == 400


# ── Catalog over REST ────────────────────────────────────────────────────────


def test_search_catalog(client):
    r = _rest(client, "POST", "/catalog/search", {"query": "teddy bear", "pagination": {"limit": 5}})
    body = r.json()
    assert_valid(body, "shopping/catalog_search.json#/$defs/search_response")
    bear = next(p for p in body["products"] if p["id"] == BEAR)
    assert bear["price_range"]["min"] == {"amount": 225000, "currency": "LKR"}
    assert bear["variants"][0]["list_price"] == {"amount": 325000, "currency": "LKR"}
    assert body["pagination"]["has_next_page"] is True and body["pagination"]["cursor"]
    assert set(body["ucp"]["capabilities"]) == {"dev.ucp.shopping.catalog.search"}


def test_search_price_filter_in_minor_units_needs_currency(client):
    body = _rest(client, "POST", "/catalog/search", {"query": "teddy bear", "context": {"currency": "LKR"},
                                                     "filters": {"price": {"max": 300000}}}).json()
    assert {p["id"] for p in body["products"]} == {BEAR}
    assert Api.calls[-1][1]["max_price"] == 3000.0
    body = _rest(client, "POST", "/catalog/search", {"query": "teddy bear",
                                                     "filters": {"price": {"max": 300000}}}).json()
    assert body["messages"][0]["code"] == "filter_ignored"


def test_search_needs_a_query(client):
    r = _rest(client, "POST", "/catalog/search", {"filters": {"categories": ["Cakes"]}})
    assert_valid(r.json(), ERROR_SCHEMA)


def test_lookup_dedupes_and_correlates(client):
    body = _rest(client, "POST", "/catalog/lookup",
                 {"ids": [BEAR, BEAR, SHIRT + "_TC3", SHIRT, "NOPE123"]}).json()
    assert_valid(body, "shopping/catalog_lookup.json#/$defs/lookup_response")
    by_id = {p["id"]: p for p in body["products"]}
    assert by_id[BEAR]["variants"][0]["inputs"] == [{"id": BEAR, "match": "exact"}]
    shirt_inputs = {v["id"]: v["inputs"] for v in by_id[SHIRT]["variants"]}
    assert shirt_inputs[SHIRT + "_TC3"] == [{"id": SHIRT + "_TC3", "match": "exact"}]
    assert shirt_inputs[SHIRT + "_TC1"] == [{"id": SHIRT, "match": "featured"}]
    assert body["messages"] == [{"type": "info", "code": "not_found", "content": "NOPE123"}]


def test_get_product_option_selection(client):
    body = _rest(client, "POST", "/catalog/product", {"id": SHIRT, "selected": [{"name": "Size", "label": "M"}]}).json()
    assert_valid(body, "shopping/catalog_lookup.json#/$defs/get_product_response")
    p = body["product"]
    assert p["selected"] == [{"name": "Size", "label": "M"}]
    assert [v["id"] for v in p["variants"]] == [SHIRT + "_TC2"]
    values = {v["label"]: v for v in p["options"][0]["values"]}
    assert values["M"] == {"label": "M", "exists": True, "available": False}
    assert values["L"]["available"] is True
    by_variant = _rest(client, "POST", "/catalog/product", {"id": SHIRT + "_TC3"}).json()["product"]
    assert by_variant["variants"][0]["id"] == SHIRT + "_TC3"
    assert by_variant["selected"] == [{"name": "Size", "label": "L"}]


# ── MCP transport ────────────────────────────────────────────────────────────

MCP_H = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
META = {"ucp-agent": {"profile": AGENT}}


def _rpc(client, method, params=None):
    r = client.post("/ucp/mcp", headers=MCP_H, json={"jsonrpc": "2.0", "id": 1, "method": method,
                                                      **({"params": params} if params is not None else {})})
    assert r.status_code == 200, r.text
    return r.json()


def test_mcp_lists_the_ucp_tools(client):
    _rpc(client, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                "clientInfo": {"name": "t", "version": "1"}})
    tools = {t["name"] for t in _rpc(client, "tools/list")["result"]["tools"]}
    assert tools == {"create_checkout", "get_checkout", "update_checkout", "complete_checkout",
                     "cancel_checkout", "search_catalog", "lookup_catalog", "get_product"}


def test_mcp_checkout_flow(client):
    res = _rpc(client, "tools/call", {"name": "create_checkout", "arguments": {"meta": META, "checkout": _full()}})
    co = res["result"]["structuredContent"]
    assert_valid(co, CHECKOUT_SCHEMA)
    assert json.loads(res["result"]["content"][0]["text"]) == co
    assert co["status"] == "requires_escalation"
    got = _rpc(client, "tools/call", {"name": "get_checkout", "arguments": {"meta": META, "id": co["id"]}})
    assert got["result"]["structuredContent"]["id"] == co["id"]
    done = _rpc(client, "tools/call", {"name": "complete_checkout", "arguments": {
        "meta": {**META, "idempotency-key": str(uuid.uuid4())}, "id": co["id"], "checkout": {"payment": {}}}})
    assert done["result"]["structuredContent"]["continue_url"] == co["continue_url"]


def test_mcp_negotiation_failure_is_jsonrpc_error(client):
    Agent.profiles[AGENT] = {"ucp": {**PLATFORM["ucp"], "version": "2026-01-23"}}
    res = _rpc(client, "tools/call", {"name": "search_catalog", "arguments": {
        "meta": META, "catalog": {"query": "teddy bear"}}})
    assert res["error"]["code"] == -32001 and res["error"]["data"]["code"] == "version_unsupported"


def test_mcp_complete_without_key_and_bad_args(client):
    co = _rpc(client, "tools/call", {"name": "create_checkout",
                                     "arguments": {"meta": META, "checkout": _full()}})["result"]["structuredContent"]
    res = _rpc(client, "tools/call", {"name": "complete_checkout", "arguments": {
        "meta": META, "id": co["id"], "checkout": {}}})
    assert res["error"]["code"] == -32602
    res = _rpc(client, "tools/call", {"name": "update_checkout", "arguments": {"meta": META, "checkout": _full()}})
    assert res["error"]["code"] == -32602


def test_mcp_business_outcome_error_is_a_result(client):
    res = _rpc(client, "tools/call", {"name": "get_checkout", "arguments": {"meta": META, "id": "chk_nope"}})
    body = res["result"]["structuredContent"]
    assert_valid(body, ERROR_SCHEMA)
    assert body["ucp"]["status"] == "error"


def test_existing_mcp_untouched(client):
    r = client.post("/mcp", headers=MCP_H, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}})
    assert r.status_code == 200
