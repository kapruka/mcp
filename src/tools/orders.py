"""MCP tools: kapruka_create_order, kapruka_track_order."""

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


# ── Tool 2: kapruka_track_order ──────────────────────────────────────────────

# Order numbers from Kapruka's payment system look like "VIMP34456CB2" —
# uppercase alphanum, occasionally with a trailing digit. We accept lowercase
# too (customers paste from email) and allow dash/underscore defensively.
_ORDER_NUMBER_RE = re.compile(r"^[A-Za-z0-9_-]{4,40}$")


class TrackOrderInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    order_number: str = Field(
        ...,
        description=(
            "Order number from the customer's Kapruka order confirmation email or the "
            "order complete page on kapruka.com (e.g. 'VIMP34456CB2'). This is NOT the "
            "same as the order_ref returned by kapruka_create_order — the customer must "
            "complete payment first; the Kapruka order number is then emailed to them."
        ),
        min_length=4,
        max_length=40,
    )
    response_format: str = Field(
        default="markdown",
        description="'markdown' (default) or 'json'.",
    )

    @field_validator("order_number")
    @classmethod
    def validate_order_number(cls, v: str) -> str:
        if not _ORDER_NUMBER_RE.match(v):
            raise ValueError(
                "order_number must be 4–40 chars of letters, digits, dash, or underscore"
            )
        return v.upper()

    @field_validator("response_format")
    @classmethod
    def validate_format(cls, v: str) -> str:
        if v not in ("markdown", "json"):
            raise ValueError("response_format must be 'markdown' or 'json'")
        return v


@mcp.tool(
    name="kapruka_track_order",
    annotations={
        "title": "Track Kapruka Order",
        "readOnlyHint": True,
        "destructiveHint": False,
        # Response evolves as the order progresses (status changes, new progress
        # steps appear), so successive calls are not strictly idempotent in shape.
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def kapruka_track_order(params: TrackOrderInput) -> str:
    """Look up status and delivery progress for a Kapruka order by order number.

    Returns current status (received / confirmed / out-for-delivery / delivered /
    cancelled), the recipient and delivery details on file, a timestamped progress
    timeline, the cart contents, and flags for whether a delivery photo or video is
    available. Use this after a customer has placed and paid for an order and reads
    back the order number from their confirmation email or the order complete page.

    The order number is NOT the `order_ref` returned by kapruka_create_order
    (which is the pre-payment checkout reference). Once the customer completes
    payment in the browser, Kapruka emails them a separate order number — that
    is what this tool expects.

    Args:
        params (TrackOrderInput):
            - order_number (str): Kapruka order number (e.g. 'VIMP34456CB2')
            - response_format (str): 'markdown' (default) or 'json'

    Returns:
        str: Order tracking details in the requested format.

        JSON schema:
        {
          "order_number": str,
          "pnref": str,                 # internal payment reference (numeric; not the same as order_number)
          "status": str,                # received | confirmed | shipped | delivered | cancelled | ...
          "status_display": str,        # human label
          "order_date": str,            # human-formatted, Asia/Colombo
          "delivery_date": str,         # human-formatted
          "shipped_date": str | null,
          "amount": str,                # LKR string (e.g. "15500.00")
          "payment_method": str,
          "comments": str | null,
          "recipient": {"name": str, "phone": str, "address": str, "city": str},
          "greeting_message": str | null,
          "special_instructions": str | null,
          "progress": [{"step": str, "timestamp": str}],
          "live_tracking_available": bool,
          "has_delivery_video": bool,
          "has_delivery_photo": bool,
          "items": [{"product_id": str, "name": str, "quantity": int, "selling_price": float}]
        }

        Error: "Error: <message>" on failure (e.g. order not found).
    """
    try:
        client = KaprukaClient()
        data = await client.call(
            "order_tracking",
            order_number=params.order_number,
        )
    except Exception as e:
        return handle_api_error(e)

    if params.response_format == "json":
        return json.dumps(data, indent=2, ensure_ascii=False)

    # ── Markdown
    order_no = data.get("order_number", params.order_number)
    status_display = data.get("status_display") or data.get("status", "Unknown")
    amount = data.get("amount")
    payment_method = data.get("payment_method")
    order_date = data.get("order_date")
    delivery_date = data.get("delivery_date")
    shipped_date = data.get("shipped_date")
    comments = data.get("comments")

    lines: list[str] = [
        f"## Order `{order_no}` — {status_display}",
        "",
    ]

    # Headline facts
    meta_rows: list[str] = []
    if amount:
        # Backend returns amount as a string (e.g. "15500.00"). Try to format
        # as LKR for readability; fall back to raw string if non-numeric.
        try:
            meta_rows.append(f"| Total | LKR {float(amount):,.2f} |")
        except (TypeError, ValueError):
            meta_rows.append(f"| Total | {amount} |")
    if payment_method:
        meta_rows.append(f"| Payment | {payment_method} |")
    if order_date:
        meta_rows.append(f"| Ordered | {order_date} |")
    if shipped_date:
        meta_rows.append(f"| Shipped | {shipped_date} |")
    if delivery_date:
        meta_rows.append(f"| Delivery date | {delivery_date} |")
    if meta_rows:
        lines.append("| | |")
        lines.append("|---|---|")
        lines.extend(meta_rows)
        lines.append("")

    # Recipient
    recipient = data.get("recipient") or {}
    if recipient:
        name = recipient.get("name", "")
        addr_parts = [recipient.get("address"), recipient.get("city")]
        addr = ", ".join([p for p in addr_parts if p])
        phone = recipient.get("phone", "")
        lines.append("**Delivering to**")
        if name:
            lines.append(f"- {name}")
        if addr:
            lines.append(f"- {addr}")
        if phone:
            lines.append(f"- {phone}")
        lines.append("")

    # Items
    items = data.get("items") or []
    if items:
        lines.append("**Items**")
        for it in items:
            qty = it.get("quantity", 1)
            name = it.get("name", it.get("product_id", "Item"))
            price = it.get("selling_price")
            if price is not None:
                try:
                    price_str = f" — LKR {float(price):,.2f}"
                except (TypeError, ValueError):
                    price_str = f" — {price}"
            else:
                price_str = ""
            lines.append(f"- {qty} × {name}{price_str}")
        lines.append("")

    # Special instructions / greeting (only show if present, so happy-path stays tidy)
    greeting = data.get("greeting_message")
    if greeting:
        lines.append(f"**Greeting:** {greeting}")
    special = data.get("special_instructions")
    if special:
        lines.append(f"**Delivery instructions:** {special}")
    if comments:
        lines.append(f"**Notes:** {comments}")
    if greeting or special or comments:
        lines.append("")

    # Progress timeline
    progress = data.get("progress") or []
    if progress:
        lines.append("**Progress**")
        for step in progress:
            label = step.get("step", "")
            ts = step.get("timestamp", "")
            if ts:
                lines.append(f"- {ts} — {label}")
            else:
                lines.append(f"- {label}")
        lines.append("")

    # Extras (only mention when relevant — keep happy path tidy)
    extras: list[str] = []
    if data.get("live_tracking_available"):
        extras.append("live tracking available on the Kapruka order page")
    if data.get("has_delivery_photo"):
        extras.append("delivery photo available")
    if data.get("has_delivery_video"):
        extras.append("delivery video available")
    if extras:
        lines.append("_" + "; ".join(extras) + "._")

    return "\n".join(lines).rstrip() + "\n"
