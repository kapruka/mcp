"""MCP tools: kapruka_gift_countries, kapruka_gift_country_products (public).

International gifts — Kapruka's "Send gifts to <country>" stores, delivered by
Kapruka's partner in that country. See src/intl_gifts.py.
"""

from __future__ import annotations

import asyncio
import json
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.api.client import handle_api_error
from src import intl_gifts as ig
from src.server import mcp

_HOW_TO_ORDER = (
    "Order with kapruka_create_order, delivery.type \"international_gift\" plus delivery.country, "
    "delivery.zone (where the country has zones), delivery.zip, the recipient abroad, and a date "
    "on or after the earliest date. One country per order; not with Sri Lanka items or custom cakes; "
    "quantity 1 per line."
)


def _validate_currency(v: Optional[str]) -> str:
    v = (v or "LKR").upper()
    if v not in ("LKR", "USD", "GBP", "AUD", "EUR"):
        raise ValueError("currency must be LKR or USD (GBP/AUD/EUR are shown in USD)")
    return v


def _validate_format(v: str) -> str:
    if v not in ("markdown", "json"):
        raise ValueError("response_format must be 'markdown' or 'json'")
    return v


class GiftCountriesInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    currency: str = Field(default="LKR", description="LKR (default) or USD. GBP/AUD/EUR are shown in USD.")
    response_format: str = Field(default="markdown", description="'markdown' (default) or 'json'")

    @field_validator("currency")
    @classmethod
    def _cur(cls, v: str) -> str:
        return _validate_currency(v)

    @field_validator("response_format")
    @classmethod
    def _fmt(cls, v: str) -> str:
        return _validate_format(v)


@mcp.tool(
    name="kapruka_gift_countries",
    annotations={
        "title": "Kapruka International Gifts — Countries",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def kapruka_gift_countries(params: GiftCountriesInput) -> str:
    """List the countries Kapruka delivers gifts IN (international gift stores).

    Use when the RECIPIENT is outside Sri Lanka — e.g. "send flowers to my sister in
    Dubai", "a cake for my son in Melbourne". These gifts are delivered by Kapruka's
    partner in that country, not shipped from Sri Lanka. They are NOT in
    kapruka_search_products; list a store with kapruka_gift_country_products.

    For each country: how many gifts are in stock now (0 = nothing can be ordered
    there right now — say so), the delivery fee (one fee per order, whatever the
    number of items), and for Australia and the UAE the zones they deliver to —
    delivery is ONLY to those zones, each with its own fee. The earliest delivery
    date is 3 days from today (Sri Lanka date).

    Args:
        params: currency ('LKR' default or 'USD'), response_format.

    Returns:
        str. JSON: {"currency", "earliest_date", "countries": [{"country", "products_in_stock",
        "delivery_fee": {"amount","currency"}, "zone_required": bool,
        "zones": [{"name","delivery_fee"}], "earliest_date", "store_url"}]}
    """
    currency = ig.gift_currency(params.currency)
    try:
        rows = await ig.countries(currency)
    except Exception as e:
        return handle_api_error(e)

    async def count(row):
        try:
            return len((await ig.products(row["country"], currency))["products"])
        except Exception:
            return None

    counts = await asyncio.gather(*(count(r) for r in rows))
    earliest = ig.earliest_date(rows[0] if rows else None)

    if params.response_format == "json":
        return json.dumps({
            "currency": currency,
            "earliest_date": earliest,
            "countries": [{**r, "products_in_stock": n} for r, n in zip(rows, counts)],
        }, indent=2, ensure_ascii=False)

    lines = [
        "## Kapruka international gifts — delivered IN these countries",
        "Delivered by Kapruka's partner in the country (not shipped from Sri Lanka). "
        f"Earliest delivery: **{ig.fmt_day(earliest)}**.",
        "",
    ]
    for r, n in zip(rows, counts):
        if n == 0:
            lines.append(f"- **{r['country']}** — no gifts available right now")
            continue
        stock = f"{n} gifts" if n is not None else "gifts"
        lines.append(f"- **{r['country']}** — {stock} — {ig.fee_text(r)}")
    lines += ["", "List a store with kapruka_gift_country_products(country). " + _HOW_TO_ORDER]
    if params.currency != currency:
        lines.append(f"_Prices are in {currency}: these stores price in LKR or USD only._")
    return "\n".join(lines)


class GiftCountryProductsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    country: str = Field(
        ...,
        description="The recipient's country, e.g. 'UAE', 'Australia', 'USA', 'UK', 'Canada' "
                    "(common names like 'Dubai', 'United States', 'England' also work).",
        min_length=2, max_length=60,
    )
    type: Optional[str] = Field(
        default=None,
        description="Optional: only this type, e.g. 'Cakes', 'Flowers', 'Hampers', 'Chocolates'.",
        max_length=40,
    )
    currency: str = Field(default="LKR", description="LKR (default) or USD. GBP/AUD/EUR are shown in USD.")
    response_format: str = Field(default="markdown", description="'markdown' (default) or 'json'")

    @field_validator("currency")
    @classmethod
    def _cur(cls, v: str) -> str:
        return _validate_currency(v)

    @field_validator("response_format")
    @classmethod
    def _fmt(cls, v: str) -> str:
        return _validate_format(v)


@mcp.tool(
    name="kapruka_gift_country_products",
    annotations={
        "title": "Kapruka International Gifts — Store",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def kapruka_gift_country_products(params: GiftCountryProductsInput) -> str:
    """List EVERY gift in stock in one country's store (international gifts), grouped by type.

    The stores are small (tens of products), so this returns the whole store: read it
    and choose for the customer — there is no keyword search for these products.
    Prices are exactly what checkout charges for the item (no website discounts apply
    to these stores); delivery is extra — one fee per order (zone fee in Australia /
    the UAE), shown at the top. Use the `id` with kapruka_get_product for full detail
    and with kapruka_create_order (delivery.type "international_gift").

    Args:
        params: country, type (optional filter), currency ('LKR' default or 'USD'),
            response_format.

    Returns:
        str. JSON: {"country", "currency", "count", "delivery": {country row from
        kapruka_gift_countries}, "products": [{"id", "name", "type", "summary",
        "price": {"amount","currency"}, "image_url", "url"}]}
    """
    currency = ig.gift_currency(params.currency)
    try:
        rows = await ig.countries(currency)
    except Exception as e:
        return handle_api_error(e)
    row = ig.match_country(params.country, rows)
    if row is None:
        names = ", ".join(r["country"] for r in rows)
        return (f"Error (country_not_available): Kapruka has no gift store for '{params.country}'. "
                f"Stores: {names}. For a recipient in Sri Lanka use kapruka_search_products.")
    try:
        store = await ig.products(row["country"], currency)
    except Exception as e:
        return handle_api_error(e)

    items = [{
        "id": p["id"], "name": p.get("name", ""), "type": ig.type_label(p.get("type")),
        "summary": ig.clean_text(p.get("summary")), "price": p.get("price"),
        "image_url": p.get("image_url"), "url": p.get("url"),
    } for p in store["products"]]
    if params.type:
        want = ig.type_label(params.type).lower()
        items = [i for i in items if i["type"].lower() == want or params.type.lower() in i["type"].lower()]

    if params.response_format == "json":
        return json.dumps({"country": row["country"], "currency": currency, "count": len(items),
                           "earliest_date": ig.earliest_date(row), "delivery": row, "products": items},
                          indent=2, ensure_ascii=False)

    if not store["products"]:
        return (f"No gifts are available in Kapruka's {row['country']} store right now. "
                "Tell the customer; offer another country only if it suits them.")
    if not items:
        types = sorted({ig.type_label(p.get("type")) for p in store["products"]})
        return f"No {params.type} in the {row['country']} store. Types there: {', '.join(types)}."

    lines = [
        f"## Kapruka gifts delivered in {row['country']} ({len(items)})",
        f"{ig.fee_text(row).capitalize()}. Earliest delivery {ig.fmt_day(ig.earliest_date(row))}. "
        "Delivered by Kapruka's partner there.",
    ]
    if row.get("zone_required"):
        lines.append("_Ask which listed zone the recipient is in. If theirs is not listed, Kapruka "
                     "cannot deliver there — say so._")
    by_type: dict[str, list[dict]] = {}
    for i in items:
        by_type.setdefault(i["type"], []).append(i)
    for t in sorted(by_type, key=lambda k: (k == "Other", k)):
        lines += ["", f"### {t} ({len(by_type[t])})"]
        for i in by_type[t]:
            price = ig.money((i["price"] or {}).get("amount"), (i["price"] or {}).get("currency") or currency)
            s = i["summary"]
            s = (s[:110].rsplit(" ", 1)[0] + "…") if len(s) > 110 else s
            lines.append(f"- `{i['id']}` **{i['name']}** — {price}" + (f" — {s}" if s else ""))
    lines += ["", _HOW_TO_ORDER]
    return "\n".join(lines)
