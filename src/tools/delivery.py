"""MCP tools: kapruka_list_delivery_cities, kapruka_check_delivery."""

import json
from datetime import date as Date, datetime, timedelta, timezone
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.api.client import KaprukaClient, handle_api_error
from src.delivery_scope import fmt_city_list
from src.server import mcp

# ── Constants ────────────────────────────────────────────────────────────────

# Sri Lanka standard time — all "today" logic anchors here, not the MCP host clock.
_LK_TZ = timezone(timedelta(hours=5, minutes=30))

# Product-code prefixes that are reliably perishable. Used to add a
# soft warning when the user picks a far-future delivery date for these items.
# Backend has no per-product perishable flag — this heuristic covers the bulk.
_PERISHABLE_PREFIXES = ("CAKE", "FLOWER", "COMBO")


def _is_perishable(product_id: str | None) -> bool:
    if not product_id:
        return False
    return product_id.upper().startswith(_PERISHABLE_PREFIXES)


def _clean_aliases(aliases: list | None) -> list[str]:
    """Backend includes a literal 'none' placeholder for cities with no alias."""
    if not aliases:
        return []
    return [a for a in aliases if a and a.lower() != "none"]


def _today_lk() -> str:
    return datetime.now(_LK_TZ).date().isoformat()


# ── Tool 1: kapruka_list_delivery_cities ──────────────────────────────────────


class ListDeliveryCitiesInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    query: Optional[str] = Field(
        default=None,
        description=(
            "Filter cities by partial match against name or aliases (case-insensitive). "
            "Omit to see the first `limit` cities alphabetically."
        ),
        max_length=50,
    )
    limit: int = Field(
        default=25,
        description="Max cities to return (1–50).",
        ge=1,
        le=50,
    )
    response_format: str = Field(
        default="markdown",
        description="'markdown' (default) or 'json'",
    )

    @field_validator("response_format")
    @classmethod
    def validate_format(cls, v: str) -> str:
        if v not in ("markdown", "json"):
            raise ValueError("response_format must be 'markdown' or 'json'")
        return v


@mcp.tool(
    name="kapruka_list_delivery_cities",
    annotations={
        "title": "List Kapruka Delivery Cities",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def kapruka_list_delivery_cities(params: ListDeliveryCitiesInput) -> str:
    """List or search Sri Lankan cities Kapruka delivers to.

    Use the `query` param to filter (e.g. "colombo" → all Colombo zones,
    "anur" → Anuradhapura). Without a query you get the first 25 cities
    alphabetically, which is rarely what an agent needs — pass a query.

    Returns canonical city names (use these as the `city` argument to
    kapruka_check_delivery) plus any common aliases / vernacular spellings.

    Args:
        params (ListDeliveryCitiesInput):
            - query (Optional[str]): Partial match filter
            - limit (int): Max results, 1–50 (default 25)
            - response_format (str): 'markdown' (default) or 'json'

    Returns:
        str: Cities list in the requested format.

        JSON schema:
        {
          "cities": [{"name": str, "aliases": [str]}],
          "total_matched": int,
          "showing": int
        }
    """
    try:
        client = KaprukaClient()
        data = await client.call("delivery_cities")
    except Exception as e:
        return handle_api_error(e)

    raw: list[dict] = data.get("cities", [])
    cleaned = [
        {"name": c.get("name", ""), "aliases": _clean_aliases(c.get("aliases"))}
        for c in raw
        if c.get("name")
    ]

    if params.query:
        q = params.query.lower()
        cleaned = [
            c for c in cleaned
            if q in c["name"].lower()
            or any(q in a.lower() for a in c["aliases"])
        ]

    total = len(cleaned)
    cities = cleaned[: params.limit]

    if params.response_format == "json":
        return json.dumps(
            {"cities": cities, "total_matched": total, "showing": len(cities)},
            indent=2,
            ensure_ascii=False,
        )

    if not cities:
        scope = f"matching '{params.query}'" if params.query else ""
        return f"No delivery cities found {scope}.".strip() + " Try a broader query."

    header = (
        f"## Kapruka delivery cities — '{params.query}' ({len(cities)} of {total})"
        if params.query
        else f"## Kapruka delivery cities ({len(cities)} of {total} total)"
    )
    lines = [header, ""]
    for c in cities:
        if c["aliases"]:
            lines.append(f"- **{c['name']}**  _aliases: {', '.join(c['aliases'])}_")
        else:
            lines.append(f"- **{c['name']}**")

    if total > len(cities):
        lines.append("")
        lines.append(
            f"_{total - len(cities)} more match — refine `query` or raise `limit` to see them._"
        )
    return "\n".join(lines)


# ── Tool 2: kapruka_check_delivery ────────────────────────────────────────────


class CheckDeliveryInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    city: str = Field(
        ...,
        description=(
            "Canonical city name (use kapruka_list_delivery_cities to find one). "
            "Examples: 'Colombo 03', 'Anuradhapura', 'Galle'."
        ),
        min_length=2,
        max_length=100,
    )
    delivery_date: Optional[str] = Field(
        default=None,
        description=(
            "Target delivery date in ISO format (YYYY-MM-DD), Sri Lanka time. "
            "Omit to check today."
        ),
    )
    product_id: Optional[str] = Field(
        default=None,
        description=(
            "Product ID to check the city AGAINST THAT ITEM'S delivery scope. "
            "Food, hotel cakes and liquor only reach selected cities — always pass "
            "this when a customer names a product and a city, and only promise "
            "delivery if `available` is true. Also adds a freshness warning for "
            "perishable codes (cake/flower/combo) when the date is >1 day out."
        ),
        max_length=80,
    )
    response_format: str = Field(
        default="markdown",
        description="'markdown' (default) or 'json'",
    )

    @field_validator("delivery_date")
    @classmethod
    def validate_date(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        try:
            Date.fromisoformat(v)
        except Exception:
            raise ValueError("delivery_date must be in YYYY-MM-DD format")
        return v

    @field_validator("response_format")
    @classmethod
    def validate_format(cls, v: str) -> str:
        if v not in ("markdown", "json"):
            raise ValueError("response_format must be 'markdown' or 'json'")
        return v


def _perishable_warning(product_id: str, delivery_date_iso: str) -> Optional[str]:
    """Warn if a perishable item is being scheduled more than 1 day out."""
    try:
        d = Date.fromisoformat(delivery_date_iso)
    except Exception:
        return None
    today = datetime.now(_LK_TZ).date()
    if (d - today).days <= 1:
        return None
    return (
        f"Note: Product `{product_id}` looks like a perishable item "
        f"(cake/flower/combo). Same-day or next-day delivery is recommended; "
        f"freshness on {delivery_date_iso} is not guaranteed."
    )


@mcp.tool(
    name="kapruka_check_delivery",
    annotations={
        "title": "Check Kapruka Delivery Availability and Rate",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": False,  # depends on real-time clock
        "openWorldHint": True,
    },
)
async def kapruka_check_delivery(params: CheckDeliveryInput) -> str:
    """Check whether Kapruka can deliver to a given city on a given date, and at what rate.

    Returns the flat delivery rate (LKR), whether the requested date is available,
    and — if not — the next available date plus reason. Kapruka delivers as a
    single shipment per order at one flat rate regardless of item count.

    Pass `product_id` whenever the customer has named a product: the answer then
    also checks that ITEM's delivery scope (restaurant food, hotel cakes and
    liquor only reach selected cities, typically the Colombo area). With a
    product_id, `available` is true only if the date is open AND the item is
    deliverable to that city. When `item_deliverable` is false, offer the
    customer one of the returned `deliverable_cities` or an island-wide
    alternative — do not attempt kapruka_create_order with the same city, it
    will be rejected. An unknown product_id is silently ignored (no item fields
    in the result), so check `item_deliverable` is present before relying on it.

    Perishable codes (CAKE*, FLOWER*, COMBO*) additionally get a freshness
    warning when the chosen delivery date is more than 1 day out.

    Args:
        params (CheckDeliveryInput):
            - city (str): Canonical city name (e.g. 'Colombo 03', 'Galle')
            - delivery_date (Optional[str]): YYYY-MM-DD; defaults to today (LK time)
            - product_id (Optional[str]): Check the city against this item's delivery scope
            - response_format (str): 'markdown' (default) or 'json'

    Returns:
        str: Delivery feasibility + rate in the requested format.

        JSON schema:
        {
          "city": str,
          "now": str,                       # ISO timestamp, Sri Lanka time
          "checked_date": str,              # YYYY-MM-DD
          "available": bool,                # date open AND (if product_id) item deliverable
          "rate": number,                   # flat LKR rate per order
          "currency": "LKR",
          "reason": str | null,             # date-block message, else "This item is not delivered to <City>."
          "next_available_date": str|null,  # only for date blocks
          "item_deliverable": bool,         # only when product_id resolved to a real product
          "deliverable_cities": [str],      # only when item_deliverable=false (capped at 60)
          "perishable_warning": str | null  # populated when product_id is perishable
        }
    """
    target_date = params.delivery_date or _today_lk()

    try:
        client = KaprukaClient()
        data = await client.call(
            "delivery_check",
            city=params.city,
            delivery_date=target_date,
            product_id=params.product_id,
        )
    except Exception as e:
        return handle_api_error(e)

    warning = None
    if _is_perishable(params.product_id):
        warning = _perishable_warning(params.product_id, target_date)

    if params.response_format == "json":
        out = dict(data)
        out["perishable_warning"] = warning
        return json.dumps(out, indent=2, ensure_ascii=False)

    # ── Markdown
    city = data.get("city", params.city)
    checked = data.get("checked_date", target_date)
    available = bool(data.get("available"))
    rate = data.get("rate")
    currency = data.get("currency", "LKR")

    # Present only when product_id resolved to a real product.
    item_deliverable = data.get("item_deliverable")
    item_blocked = item_deliverable is False

    lines = [f"## Delivery to {city} on {checked}"]
    if available:
        if rate is not None:
            lines.append(f"**Available** — flat rate {currency} {rate:,}")
        else:
            lines.append("**Available**")
        if item_deliverable is True:
            lines.append(f"- `{params.product_id}` can be delivered to {city}.")
    else:
        if item_blocked:
            # Item scope beats the date: a new date won't help, a new city will.
            # `reason` may carry only the date message when both fail, so state
            # the item block explicitly.
            lines.append(f"**Not available — `{params.product_id}` is not delivered to {city}.**")
            cities = data.get("deliverable_cities") or []
            if cities:
                lines.append(
                    f"- This item is delivered only to selected cities: "
                    f"{fmt_city_list(cities, len(cities))}."
                )
            lines.append(
                "- Offer the customer one of those cities or an island-wide "
                "alternative product. Do not place the order to this city."
            )
        else:
            lines.append("**Not available on this date.**")
        reason = data.get("reason")
        # Skip the API's own item message when we've already rendered it above.
        if reason and not (item_blocked and "not delivered to" in reason):
            lines.append(f"- {reason}")
        next_date = data.get("next_available_date")
        if next_date:
            lines.append(f"- Next available date: **{next_date}**")
        if item_deliverable is True:
            lines.append(f"- `{params.product_id}` itself can be delivered to {city} — only the date is the issue.")
        if rate is not None:
            lines.append(f"- Rate when available: {currency} {rate:,}")

    if warning:
        lines.append("")
        lines.append(warning)

    return "\n".join(lines)
