"""International gifts (Kapruka API 2026-10-13 / 10-19): store tools, product view,
delivery check and create_order with delivery.type "international_gift".

Response shapes follow production (checked 2026-10-20).
"""

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from src import intl_gifts as ig
from src.cache import cache
from src.tools import delivery as delivery_tool
from src.tools import orders as orders_tool
from src.tools import products as products_tool
from src.tools.delivery import CheckDeliveryInput, kapruka_check_delivery
from src.tools.international import (
    GiftCountriesInput,
    GiftCountryProductsInput,
    kapruka_gift_countries,
    kapruka_gift_country_products,
)
from src.tools.orders import CreateOrderInput, kapruka_create_order
from src.tools.products import GetProductInput, kapruka_get_product

LK = timezone(timedelta(hours=5, minutes=30))


def _in(days: int) -> str:
    return (datetime.now(LK).date() + timedelta(days=days)).isoformat()


def _usd(a):
    return {"amount": a, "currency": "USD"}


COUNTRIES = [
    {"country": "USA", "store_url": "https://www.kapruka.com/online/USA", "delivery_fee": _usd(71),
     "zone_required": False, "zones": [], "earliest_date": _in(3)},
    {"country": "Australia", "store_url": "https://www.kapruka.com/online/Australia", "delivery_fee": _usd(0),
     "zone_required": True, "earliest_date": _in(3),
     "zones": [{"name": "Dandenong", "delivery_fee": _usd(10)}, {"name": "Narre Warren", "delivery_fee": _usd(15)}]},
    {"country": "UAE", "store_url": "https://www.kapruka.com/online/UAE", "delivery_fee": _usd(0),
     "zone_required": True, "earliest_date": _in(3),
     "zones": [{"name": "Abu-Dhabi", "delivery_fee": _usd(40.68)}, {"name": "Dubai", "delivery_fee": _usd(40.68)},
               {"name": "Ras al Khaimah", "delivery_fee": _usd(54.96)}]},
    {"country": "France", "store_url": "https://www.kapruka.com/online/France", "delivery_fee": _usd(28.57),
     "zone_required": False, "zones": [], "earliest_date": _in(3)},
]
STORES = {
    "USA": [{"id": "intGift00001", "name": "Vanilla Bean Cakes", "type": "cakes", "summary": "Soft vanilla cake.",
             "price": _usd(119.54), "in_stock": True, "image_url": "https://x/1.jpg", "url": "https://x/1"},
            {"id": "intGift00002", "name": "Red Wine Favorites", "type": "hampers", "summary": "Wine hamper.",
             "price": _usd(150), "in_stock": True, "image_url": "https://x/2.jpg", "url": "https://x/2"}],
    "Australia": [{"id": "EF_PC_AUST0V3811POD00039", "name": "Classic Ribbon Cake 500g", "type": "Cakes",
                   "summary": "A ribbon cake.", "price": _usd(38.5), "in_stock": True, "url": "https://x/3"},
                  {"id": "EF_PC_AUST0V3811POD00099", "name": "Gift Hamper", "type": "Hamper",
                   "summary": "", "price": _usd(60), "in_stock": True, "url": "https://x/4"}],
    "UAE": [{"id": "intGift00931", "name": "Birthday Rocher Bouquet", "type": "chocolate",
             "summary": "`Offer your loved ones a sweet surprise.`", "price": _usd(120), "in_stock": True,
             "url": "https://x/5"}],
    "France": [],
}


class Api:
    calls: list = []
    posts: list = []

    async def post(self, endpoint, body, **p):
        Api.posts.append((endpoint, body, p))
        if endpoint == "gift_countries":
            return {"currency": p.get("currency"), "countries": COUNTRIES}
        if endpoint == "gift_country_products":
            prods = STORES[body["country"]]
            return {"country": body["country"], "currency": p.get("currency"), "count": len(prods), "products": prods}
        if endpoint == "create_order":
            d = body["delivery"]
            return {"order_ref": "ORD-INTL-1", "checkout_url": "https://www.kapruka.com/pay/intl",
                    "summary": {"items_total": 120, "delivery_fee": 40.68, "addons_total": 0,
                                "grand_total": 160.68, "currency": "USD", "delivery_type": "international_gift",
                                "country": d["country"], "zone": d.get("zone"), "delivery_date": d["date"]}}
        raise AssertionError(endpoint)

    async def call(self, endpoint, **p):
        Api.calls.append((endpoint, p))
        if endpoint == "product":
            return {"id": "intGift00931", "name": "Birthday Rocher Bouquet", "price": _usd(120), "in_stock": True,
                    "delivery": {"island_wide": None, "international": True, "country": "UAE"},
                    "international_gift": {"country": "UAE", "orderable": True,
                                           "store_url": "https://www.kapruka.com/online/UAE"},
                    "economy_delivery": None, "shipping": {"ships_from": "AE"}}
        if endpoint == "delivery_check":
            gift = {"country": "UAE", "zone": p.get("zone"), "zone_required": True,
                    "zones": [{"name": z["name"]} for z in COUNTRIES[2]["zones"]], "earliest_date": _in(3)}
            listed = {z["name"].lower() for z in COUNTRIES[2]["zones"]}
            date = p.get("delivery_date")
            if not p.get("zone") or p["zone"].lower() not in listed:
                return {"available": False, "reason": f"'{p.get('zone')}' is not a delivery zone of UAE.",
                        "checked_date": date, "international_gift": gift}
            if date and date < _in(3):
                return {"available": False, "reason": "Too early.", "next_available_date": _in(3),
                        "checked_date": date, "international_gift": gift}
            return {"available": True, "delivery_fee": 40.68, "fee_currency": "USD", "fee_basis": "fixed",
                    "checked_date": date, "international_gift": gift}
        raise AssertionError(endpoint)


@pytest.fixture(autouse=True)
def _stub(monkeypatch):
    Api.calls, Api.posts = [], []
    cache.clear()
    for mod in (ig, delivery_tool, orders_tool, products_tool):
        monkeypatch.setattr(mod, "KaprukaClient", Api)
    yield
    cache.clear()


# ── helpers ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name,country", [
    ("UAE", "UAE"), ("dubai", "UAE"), ("United Arab Emirates", "UAE"), ("usa", "USA"),
    ("United States", "USA"), ("australia", "Australia"), ("Melbourne", "Australia"), ("Italy", None),
])
def test_country_aliases(name, country):
    row = ig.match_country(name, COUNTRIES)
    assert (row["country"] if row else None) == country


@pytest.mark.parametrize("given,zone", [
    ("abu dhabi", "Abu-Dhabi"), ("DUBAI", "Dubai"), ("ras al-khaimah", "Ras al Khaimah"),
    ("Dandenong South", None), ("", None),
])
def test_zone_matching_is_loose_but_exact(given, zone):
    zones = COUNTRIES[2]["zones"] + COUNTRIES[1]["zones"]
    hit = ig.match_zone(given, zones)
    assert (hit["name"] if hit else None) == zone


def test_type_labels_and_text_cleanup():
    assert [ig.type_label(t) for t in ("cakes", "Hamper", "fruitbaskets", "softoys", "NA", None)] == \
        ["Cakes", "Hampers", "Fruit Baskets", "Soft Toys", "Other", "Other"]
    assert ig.clean_text("`Offer your loved ones.`") == "Offer your loved ones."
    assert ig.clean_text("16` x 14` box") == "16` x 14` box"


# ── store tools ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gift_countries_counts_fees_and_zones():
    out = await kapruka_gift_countries(GiftCountriesInput(currency="USD"))
    assert "**USA** — 2 gifts — delivery USD 71.00 per order" in out
    assert "**UAE** — 1 gifts — delivers ONLY to these zones (fee per order): Abu-Dhabi USD 40.68" in out
    assert "**France** — no gifts available right now" in out
    assert _in(3) in out
    js = json.loads(await kapruka_gift_countries(GiftCountriesInput(currency="USD", response_format="json")))
    assert {c["country"]: c["products_in_stock"] for c in js["countries"]} == \
        {"USA": 2, "Australia": 2, "UAE": 1, "France": 0}


@pytest.mark.asyncio
async def test_gift_countries_are_cached():
    await kapruka_gift_countries(GiftCountriesInput())
    n = len(Api.posts)
    await kapruka_gift_countries(GiftCountriesInput())
    assert len(Api.posts) == n


@pytest.mark.asyncio
async def test_gbp_is_shown_in_usd():
    out = await kapruka_gift_countries(GiftCountriesInput(currency="GBP"))
    assert Api.posts[0][2]["currency"] == "USD" and "Prices are in USD" in out


@pytest.mark.asyncio
async def test_store_listing_grouped_by_type_with_clean_text():
    out = await kapruka_gift_country_products(GiftCountryProductsInput(country="Dubai", currency="USD"))
    assert "## Kapruka gifts delivered in UAE (1)" in out
    assert "### Chocolates (1)" in out
    assert "`intGift00931` **Birthday Rocher Bouquet** — USD 120.00 — Offer your loved ones a sweet surprise." in out
    assert "`" + "Offer" not in out
    assert "Ask which listed zone" in out


@pytest.mark.asyncio
async def test_store_listing_type_filter_and_labels():
    out = await kapruka_gift_country_products(GiftCountryProductsInput(country="Australia", type="hampers"))
    assert "### Hampers (1)" in out and "Ribbon Cake" not in out
    js = json.loads(await kapruka_gift_country_products(
        GiftCountryProductsInput(country="usa", response_format="json")))
    assert {p["type"] for p in js["products"]} == {"Cakes", "Hampers"}


@pytest.mark.asyncio
async def test_empty_and_unknown_stores():
    assert "No gifts are available in Kapruka's France store" in await kapruka_gift_country_products(
        GiftCountryProductsInput(country="France"))
    out = await kapruka_gift_country_products(GiftCountryProductsInput(country="Italy"))
    assert out.startswith("Error (country_not_available)") and "USA, Australia, UAE, France" in out


# ── product view ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_product_shows_international_not_sri_lanka_cities():
    out = await kapruka_get_product(GetProductInput(product_id="intGift00931"))
    assert "International gift — delivered in UAE" in out
    assert "**International gift**: delivered in UAE only" in out
    assert "Selected cities only" not in out


# ── delivery check ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_needs_no_city_maps_loose_zone_and_defaults_to_earliest():
    out = await kapruka_check_delivery(CheckDeliveryInput(product_id="intGift00931", zone="abu dhabi",
                                                          currency="USD"))
    assert "## International gift delivery — Abu-Dhabi, UAE" in out
    assert "**Available** — delivery fee **USD 40.68** for the whole order" in out
    last = Api.calls[-1][1]
    assert last["zone"] == "Abu-Dhabi" and last["delivery_date"] == _in(3) and last["city"] is None


@pytest.mark.asyncio
async def test_check_unlisted_zone_lists_zones():
    out = await kapruka_check_delivery(CheckDeliveryInput(product_id="intGift00931", zone="Fujairah"))
    assert "**Not available.**" in out and "UAE delivers ONLY to: Abu-Dhabi, Dubai, Ras al Khaimah" in out


@pytest.mark.asyncio
async def test_check_without_city_or_item_is_refused():
    out = await kapruka_check_delivery(CheckDeliveryInput())
    assert out.startswith("Error: city is required")


# ── create_order ────────────────────────────────────────────────────────────


def _order(**delivery):
    d = {"address": "Villa 12, Al Wasl Rd", "city": "Dubai", "date": _in(4), "type": "international_gift",
         "country": "uae", "zone": "dubai", "zip": "00000", **delivery}
    return CreateOrderInput(cart=[{"product_id": "intGift00931", "quantity": 1}],
                            recipient={"name": "Nimali", "phone": "+971501234567"}, delivery=d,
                            sender={"name": "Amara", "email": "amara@example.com"}, currency="USD")


@pytest.mark.asyncio
async def test_international_order_body_and_answer():
    out = await kapruka_create_order(_order())
    body = next(b for e, b, _ in Api.posts if e == "create_order")
    assert body["delivery"] == {"address": "Villa 12, Al Wasl Rd", "city": "Dubai", "date": _in(4),
                                "type": "international_gift", "country": "UAE", "zone": "Dubai", "zip": "00000"}
    assert body["sender"] == {"name": "Amara", "email": "amara@example.com", "anonymous": False}
    assert "**Delivery:** international gift — delivered in Dubai, UAE on " + _in(4) in out


@pytest.mark.asyncio
async def test_zone_less_country_drops_zone():
    o = _order(country="United States", zone=None, city="Austin")
    o.cart[0].product_id = "intGift00001"
    await kapruka_create_order(o)
    body = next(b for e, b, _ in Api.posts if e == "create_order")
    assert body["delivery"]["country"] == "USA" and "zone" not in body["delivery"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change,code", [
    ({"zone": "Fujairah"}, "invalid_parameter"),
    ({"zone": None}, "invalid_parameter"),
    ({"date": _in(2)}, "date_not_deliverable"),
    ({"country": "Italy"}, "country_not_available"),
])
async def test_refused_before_kapruka(change, code):
    out = await kapruka_create_order(_order(**change))
    assert out.startswith(f"Error ({code})") and "Nothing was created" in out
    assert not [e for e, *_ in Api.posts if e == "create_order"]


@pytest.mark.asyncio
async def test_quantity_above_one_refused():
    o = _order()
    o.cart[0].quantity = 2
    out = await kapruka_create_order(o)
    assert out.startswith("Error (product_out_of_stock)") and "1 per line" in out


def test_international_fields_validated():
    with pytest.raises(Exception, match="zip required"):
        _order(zip=None)
    with pytest.raises(Exception, match="only for delivery.type 'international_gift'"):
        CreateOrderInput(cart=[{"product_id": "CAKE1", "quantity": 1}], recipient={"name": "A", "phone": "+94771234567"},
                         delivery={"address": "1 Main St", "city": "Galle", "date": _in(1), "country": "UAE"},
                         sender={"name": "B"})


@pytest.mark.asyncio
async def test_sri_lanka_order_does_not_send_email():
    class SL(Api):
        async def post(self, endpoint, body, **p):
            Api.posts.append((endpoint, body, p))
            return {"order_ref": "ORD-1", "checkout_url": "https://x", "summary": {}}
    import src.tools.orders as o
    o.KaprukaClient = SL
    await kapruka_create_order(CreateOrderInput(
        cart=[{"product_id": "CAKE1", "quantity": 1}], recipient={"name": "A", "phone": "+94771234567"},
        delivery={"address": "1 Main St", "city": "Galle", "date": _in(1)},
        sender={"name": "B", "email": "b@example.com"}))
    body = Api.posts[-1][1]
    assert "email" not in body["sender"] and "location_type" in body["delivery"]
