"""MCP tool: kapruka_create_order — guest checkout session creation."""

import json
import re
import uuid
from datetime import date as Date, datetime, timedelta, timezone
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.api.client import KaprukaClient, handle_api_error
from src.server import mcp

SUPPORTED_CURRENCIES = ["LKR", "USD", "GBP", "AUD", "CAD", "EUR"]

# All "today" checks anchor on Sri Lanka time, not the MCP host clock.
_LK_TZ = timezone(timedelta(hours=5, minutes=30))

# Hard cart caps — pushed up from upstream into the MCP so we reject obviously
# bad input before burning a backend round-trip. Values from product call.
_MAX_CART_ITEMS = 30
_MAX_QTY_PER_ITEM = 99
_MAX_ICING_TEXT = 120
_MAX_GIFT_MESSAGE = 300

# Permissive: E.164 ("+9477..."), local SL ("077...", "0117..."), with spaces /
# dashes / parens allowed. Backend normalises — we just block empty/garbage.
_PHONE_RE = re.compile(r"^[+\d][\d\s\-()]{6,30}$")


# ── Sub-models ───────────────────────────────────────────────────────────────


class CartItem(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    product_id: str = Field(
        ...,
        description="Kapruka product ID (e.g. 'cake00ka002034').",
        min_length=3,
        max_length=80,
    )
    quantity: int = Field(
        default=1,
        description=f"Quantity (1–{_MAX_QTY_PER_ITEM}).",
        ge=1,
        le=_MAX_QTY_PER_ITEM,
    )
    icing_text: Optional[str] = Field(
        default=None,
        description="Cake icing text. Silently ignored for non-cake products.",
        max_length=_MAX_ICING_TEXT,
    )


class Recipient(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: str = Field(..., description="Recipient name shown on the order.", min_length=1, max_length=80)
    phone: str = Field(
        ...,
        description="Recipient phone — E.164 (+9477…) or local SL (077…) format.",
        min_length=7,
        max_length=30,
    )

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, v: str) -> str:
        if not _PHONE_RE.match(v):
            raise ValueError("phone must be a valid phone number (E.164 or local Sri Lanka format)")
        return v


class Delivery(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    address: str = Field(..., description="Street address.", min_length=3, max_length=250)
    city: str = Field(
        ...,
        description="Must be a Kapruka delivery city — use kapruka_list_delivery_cities to look up valid names.",
        min_length=2,
        max_length=100,
    )
    location_type: str = Field(
        default="house",
        description="One of: house, apartment, office, other.",
    )
    date: str = Field(
        ...,
        description="Delivery date in YYYY-MM-DD (Asia/Colombo). Must be today or future.",
    )
    instructions: Optional[str] = Field(
        default=None,
        description="Free-form delivery instructions.",
        max_length=250,
    )

    @field_validator("location_type")
    @classmethod
    def validate_loc(cls, v: str) -> str:
        allowed = {"house", "apartment", "office", "other"}
        if v not in allowed:
            raise ValueError(f"location_type must be one of: {', '.join(sorted(allowed))}")
        return v

    @field_validator("date")
    @classmethod
    def validate_date(cls, v: str) -> str:
        try:
            d = Date.fromisoformat(v)
        except Exception:
            raise ValueError("delivery.date must be YYYY-MM-DD")
        today_lk = datetime.now(_LK_TZ).date()
        if d < today_lk:
            raise ValueError(
                f"delivery.date {v} is in the past (Asia/Colombo today is {today_lk.isoformat()})"
            )
        return v


class Sender(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: str = Field(..., description="Sender name on the gift card.", min_length=1, max_length=80)
    anonymous: bool = Field(
        default=False,
        description="If true, gift card shows 'Anonymous' instead of the sender name.",
    )


# ── Tool input ───────────────────────────────────────────────────────────────


class CreateOrderInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    cart: list[CartItem] = Field(
        ...,
        description=f"1–{_MAX_CART_ITEMS} items.",
        min_length=1,
        max_length=_MAX_CART_ITEMS,
    )
    recipient: Recipient
    delivery: Delivery
    sender: Sender
    gift_message: Optional[str] = Field(
        default=None,
        description="Optional gift card message.",
        max_length=_MAX_GIFT_MESSAGE,
    )
    currency: str = Field(
        default="LKR",
        description=f"Pricing currency. Supported: {', '.join(SUPPORTED_CURRENCIES)}.",
    )
    response_format: str = Field(
        default="markdown",
        description="'markdown' (default) or 'json'.",
    )

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, v: str) -> str:
        v = v.upper()
        if v not in SUPPORTED_CURRENCIES:
            raise ValueError(f"currency must be one of: {', '.join(SUPPORTED_CURRENCIES)}")
        return v

    @field_validator("response_format")
    @classmethod
    def validate_format(cls, v: str) -> str:
        if v not in ("markdown", "json"):
            raise ValueError("response_format must be 'markdown' or 'json'")
        return v


# ── Helpers ──────────────────────────────────────────────────────────────────


def _fmt_total(amount, currency: str) -> str:
    if amount is None:
        return f"{currency} —"
    if currency == "LKR":
        return f"LKR {amount:,.0f}"
    return f"{currency} {amount:,.2f}"


# ── Tool ─────────────────────────────────────────────────────────────────────


@mcp.tool(
    name="kapruka_create_order",
    annotations={
        "title": "Create Kapruka Order (Guest Checkout)",
        "readOnlyHint": False,
        # Creating a checkout session doesn't destroy or overwrite anything;
        # payment is a separate step the customer performs in the browser.
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def kapruka_create_order(params: CreateOrderInput) -> str:
    """Create a guest-checkout order on Kapruka and return a click-to-pay link.

    Builds a Kapruka order from the supplied cart + recipient + delivery + sender,
    then returns a checkout URL the customer opens in a browser to complete payment.
    No Kapruka account is required. Prices are locked for the lifetime of the link
    (60 minutes) — the customer pays exactly the quoted grand total even if the
    catalog price changes meanwhile.

    Free public tier limits: 30 orders per hour per client IP. Cart up to 30 items,
    quantity up to 99 per item. A fresh idempotency key is generated per call so
    retries on transient errors return the same checkout URL rather than duplicates.

    Args:
        params (CreateOrderInput):
            - cart (list[CartItem]): 1–30 items. Each: product_id, quantity (default 1), optional icing_text (cakes only).
            - recipient (Recipient): name + phone (E.164 +9477… or local 077…)
            - delivery (Delivery): address, city (must be Kapruka-deliverable — use kapruka_list_delivery_cities), location_type (house/apartment/office/other, default house), date (YYYY-MM-DD, today-or-future Asia/Colombo), optional instructions
            - sender (Sender): name + anonymous flag
            - gift_message (Optional[str]): Up to 300 chars
            - currency (str): LKR (default), USD, GBP, AUD, CAD, EUR
            - response_format (str): 'markdown' (default) or 'json'

    Returns:
        str: Order confirmation with checkout URL.

        JSON schema:
        {
          "checkout_url": str,           # Open in browser to pay (no login required)
          "order_ref": str,              # e.g. "ORD-20260520-7823"
          "summary": {
            "items_total":   number,
            "delivery_fee":  number,
            "addons_total":  number,
            "grand_total":   number,     # items_total + delivery_fee + addons_total
            "currency":      str
          },
          "expires_at": str              # ISO 8601 — link stops working after this
        }

        Error: "Error (<code>): <message>" on failure. Common codes:
          empty_cart, missing_field, past_delivery_date, product_not_found,
          product_out_of_stock, city_not_deliverable, date_not_deliverable.
    """
    body: dict = {
        "auth_token": None,
        "idempotency_key": str(uuid.uuid4()),
        "cart": [item.model_dump(exclude_none=True) for item in params.cart],
        "recipient": params.recipient.model_dump(),
        "delivery": params.delivery.model_dump(exclude_none=True),
        "sender": params.sender.model_dump(),
    }
    if params.gift_message:
        body["gift_message"] = params.gift_message

    try:
        client = KaprukaClient()
        data = await client.post(
            "create_order",
            body=body,
            currency=params.currency,
        )
    except Exception as e:
        return handle_api_error(e)

    if params.response_format == "json":
        return json.dumps(data, indent=2, ensure_ascii=False)

    # ── Markdown
    summary = data.get("summary") or {}
    currency = summary.get("currency") or params.currency
    order_ref = data.get("order_ref", "—")
    checkout_url = data.get("checkout_url", "")
    expires_at = data.get("expires_at", "")

    lines: list[str] = [
        f"## Order created — `{order_ref}`",
        "",
        f"**Grand total:** {_fmt_total(summary.get('grand_total'), currency)}",
        "",
        "| | |",
        "|---|---|",
        f"| Items | {_fmt_total(summary.get('items_total'), currency)} |",
        f"| Delivery | {_fmt_total(summary.get('delivery_fee'), currency)} |",
    ]
    if summary.get("addons_total"):
        lines.append(f"| Addons | {_fmt_total(summary.get('addons_total'), currency)} |")
    lines.append("")

    if checkout_url:
        lines.append(f"**[Open checkout to pay]({checkout_url})**")
        lines.append("")
    if expires_at:
        lines.append(f"_Checkout link expires at {expires_at}. Prices are locked for that window._")

    return "\n".join(lines)
