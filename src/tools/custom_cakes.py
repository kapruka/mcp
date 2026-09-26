"""MCP tools: kapruka_custom_cake_options / _request / _status.

Custom cakes are a QUOTE flow, not an instant purchase: the customer sends a
picture + choices, Kapruka staff price it in the admin panel (minutes to
hours), the customer gets an SMS, and the agent polls status. Ordering the
quoted cake goes through the normal kapruka_create_order with a
`custom_cake_request_id` cart line (see orders.py).

"Closed MCP": these tools are hidden from tools/list and only answer callers
whose IP is in CUSTOM_CAKE_TRUSTED_IPS (defaults to the trusted-tier list,
i.e. the eagle sales-agent box). Backend: commerce_phase3.jsp, phase-3 token.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import logging
import re
import uuid
from datetime import date as Date, datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlparse

import httpx
from mcp.server.fastmcp import Context
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.api.client import KaprukaClient, handle_api_error
from src.config.settings import settings
from src.private_access import is_trusted_caller
from src.server import mcp

logger = logging.getLogger(__name__)

_LK_TZ = timezone(timedelta(hours=5, minutes=30))
_PHONE_RE = re.compile(r"^[+\d][\d\s\-()]{6,30}$")
# Same regex as the API's UUIDv4 check — it rejects anything else with missing_field.
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{6,80}$")

_ACCESS_DENIED = (
    "Error: kapruka_custom_cake_* tools are available to Kapruka's first-party "
    "sales agent only (closed MCP). This caller's IP is not on the trusted list."
)


# ── Closed-MCP gate ──────────────────────────────────────────────────────────


def _check_access(ctx: Context | None) -> str | None:
    """Return an error string if the caller is not a trusted first-party IP."""
    if is_trusted_caller(ctx, settings.custom_cake_trusted_ips, "custom_cake"):
        return None
    return _ACCESS_DENIED


# ── Image fetch (LLMs can't emit 5 MB of base64 — we download for them) ─────


def _is_public_https(url: str) -> bool:
    try:
        p = urlparse(url)
    except Exception:
        return False
    if p.scheme != "https" or not p.hostname:
        return False
    host = p.hostname.lower()
    if host in ("localhost",) or host.endswith(".local"):
        return False
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_global
    except ValueError:
        return True  # hostname; resolution happens in httpx


def _sniff_image(head: bytes) -> str | None:
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    return None


async def fetch_image_base64(url: str, max_bytes: int | None = None) -> str:
    """Download a JPG/PNG (<= max_bytes) and return raw base64. Raises ValueError
    with a customer-safe message on any problem."""
    limit = max_bytes or settings.custom_cake_image_max_bytes
    if not _is_public_https(url):
        raise ValueError("image_url must be a public https:// URL")
    buf = bytearray()
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            async with client.stream(
                "GET", url, headers={"User-Agent": "kapruka-mcp/1.0 (+https://mcp.kapruka.com)"}
            ) as resp:
                resp.raise_for_status()
                declared = resp.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > limit:
                    raise ValueError(f"image is larger than {limit // (1024 * 1024)} MB")
                async for chunk in resp.aiter_bytes():
                    buf.extend(chunk)
                    if len(buf) > limit:
                        raise ValueError(f"image is larger than {limit // (1024 * 1024)} MB")
    except httpx.HTTPStatusError as e:
        raise ValueError(f"could not download image (HTTP {e.response.status_code})") from e
    except httpx.HTTPError as e:
        raise ValueError(f"could not download image ({type(e).__name__})") from e
    if not _sniff_image(bytes(buf[:8])):
        raise ValueError("image must be a JPG or PNG")
    return base64.b64encode(bytes(buf)).decode("ascii")


def _validate_base64_image(data: str, max_bytes: int | None = None) -> str:
    """Accept raw base64 or a data: URL; return raw base64. Checks size + magic."""
    limit = max_bytes or settings.custom_cake_image_max_bytes
    raw = data.strip()
    if raw.startswith("data:"):
        if ";base64," not in raw:
            raise ValueError("image_base64 data URL must be base64-encoded")
        raw = raw.split(";base64,", 1)[1]
    try:
        decoded = base64.b64decode(raw, validate=True)
    except Exception:
        raise ValueError("image_base64 is not valid base64")
    if len(decoded) > limit:
        raise ValueError(f"image is larger than {limit // (1024 * 1024)} MB")
    if not _sniff_image(decoded[:8]):
        raise ValueError("image must be a JPG or PNG")
    return raw


# ── Tool 1: options ──────────────────────────────────────────────────────────


class CustomCakeOptionsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    response_format: str = Field(default="markdown", description="'markdown' (default) or 'json'.")

    @field_validator("response_format")
    @classmethod
    def validate_format(cls, v: str) -> str:
        if v not in ("markdown", "json"):
            raise ValueError("response_format must be 'markdown' or 'json'")
        return v


@mcp.tool(
    name="kapruka_custom_cake_options",
    annotations={
        "title": "Custom Cake — Available Choices",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def kapruka_custom_cake_options(params: CustomCakeOptionsInput, ctx: Context) -> str:
    """List the choices a customer can make for a custom (photo) cake.

    Call this BEFORE collecting the customer's choices — never hard-code
    flavours, sizes or colours. Show the customer the flavour `label`; send the
    `value` to kapruka_custom_cake_request. If `available` is false, custom
    cakes are switched off right now: tell the customer and do not submit.

    Returns (JSON schema):
        {
          "available": bool,
          "flavours": [{"value": str, "label": str}],
          "sizes": [str],                    # e.g. "1 KG", "2 KG"
          "icing_colors": [str],
          "delivery_types": ["delivered", "pickup"],
          "pickup_locations": [{"id": int, "name": str, "address": str, "city": str, "times": str}],
          "greeting_min_length": int, "greeting_max_length": int,
          "image_formats": [str], "image_max_bytes": int,
          "quote_valid_days": int
        }
    """
    denied = _check_access(ctx)
    if denied:
        return denied
    try:
        data = await KaprukaClient().post("custom_cake_options", body={})
    except Exception as e:
        return handle_api_error(e)

    if params.response_format == "json":
        return json.dumps(data, indent=2, ensure_ascii=False)

    if not data.get("available", False):
        return (
            "## Custom cakes are currently unavailable\n"
            "The custom cake service is switched off right now. Offer a cake from "
            "the catalogue instead (kapruka_search_products, category 'Kapruka Cakes')."
        )
    flavours = ", ".join(
        f"{f.get('label')} (`{f.get('value')}`)" for f in data.get("flavours", []) if isinstance(f, dict)
    )
    lines = [
        "## Custom cake — available choices",
        f"- **Flavours**: {flavours or '—'}",
        f"- **Sizes**: {', '.join(data.get('sizes', [])) or '—'}",
        f"- **Icing colours**: {', '.join(data.get('icing_colors', [])) or '—'}",
        f"- **Delivery types**: {', '.join(data.get('delivery_types', [])) or '—'}",
        f"- **Greeting on cake**: {data.get('greeting_min_length', 3)}–{data.get('greeting_max_length', 30)} characters (required)",
        f"- **Picture**: {', '.join(data.get('image_formats', ['jpg', 'png']))}, max "
        f"{int(data.get('image_max_bytes', 5242880)) // (1024 * 1024)} MB",
        f"- **Quote valid for**: {data.get('quote_valid_days', 2)} days",
    ]
    pickups = data.get("pickup_locations") or []
    if pickups:
        lines.append("")
        lines.append("**Pickup locations** (use `pickup_location_id`):")
        for p in pickups:
            lines.append(
                f"- id `{p.get('id')}` — {p.get('name')}, {p.get('address')} ({p.get('city')})"
                + (f" · {p.get('times')}" if p.get("times") else "")
            )
    lines.append("")
    lines.append(
        "_Price is set by Kapruka staff after the request is submitted — never "
        "quote or estimate a price yourself._"
    )
    return "\n".join(lines)


# ── Tool 2: request ──────────────────────────────────────────────────────────


class CustomCakeCustomer(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: str = Field(..., min_length=1, max_length=80, description="Customer's name.")
    phone: str = Field(
        ...,
        min_length=7,
        max_length=30,
        description=(
            "Customer's WhatsApp number (E.164 +9477… or local 077…). The quote SMS "
            "goes here AND it is the key for kapruka_custom_cake_status and the "
            "order cart line — use the same number every time."
        ),
    )
    email: Optional[str] = Field(default=None, max_length=120, description="Optional; quote e-mail also sent here.")

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, v: str) -> str:
        if not _PHONE_RE.match(v):
            raise ValueError("phone must be a valid phone number (E.164 or local Sri Lanka format)")
        return v

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v == "":
            return None
        if "@" not in v or "." not in v.split("@")[-1]:
            raise ValueError("email must be a valid e-mail address")
        return v


class CustomCakeRequestInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    customer: CustomCakeCustomer
    flavour: str = Field(..., min_length=1, max_length=40, description="Flavour `value` (or label) from kapruka_custom_cake_options.")
    size: str = Field(..., min_length=1, max_length=20, description="Size exactly as listed in options, e.g. '2 KG'.")
    greeting: str = Field(..., min_length=3, max_length=30, description="Text written on the cake (3–30 chars). Required.")
    icing_color: str = Field(..., min_length=1, max_length=30, description="Icing colour from options.")
    delivery_date: str = Field(..., description="YYYY-MM-DD (Asia/Colombo), today or later.")
    delivery_type: str = Field(default="delivered", description="'delivered' or 'pickup'.")
    city: Optional[str] = Field(
        default=None,
        max_length=100,
        description="Canonical Kapruka delivery city (kapruka_list_delivery_cities). Required for delivery_type='delivered'.",
    )
    pickup_location_id: Optional[int] = Field(
        default=None, ge=0, description="Pickup location id from options. Required for delivery_type='pickup'."
    )
    image_url: Optional[str] = Field(
        default=None,
        max_length=2000,
        description=(
            "Public https URL of the customer's cake picture (JPG/PNG, ≤5 MB) — "
            "e.g. the WhatsApp media URL. The MCP downloads and encodes it."
        ),
    )
    image_base64: Optional[str] = Field(
        default=None,
        description="Alternative to image_url: raw base64 or data:image/...;base64 of the JPG/PNG (≤5 MB).",
    )
    response_format: str = Field(default="markdown", description="'markdown' (default) or 'json'.")

    @field_validator("delivery_type")
    @classmethod
    def validate_delivery_type(cls, v: str) -> str:
        v = v.lower()
        if v not in ("delivered", "pickup"):
            raise ValueError("delivery_type must be 'delivered' or 'pickup'")
        return v

    @field_validator("delivery_date")
    @classmethod
    def validate_date(cls, v: str) -> str:
        try:
            d = Date.fromisoformat(v)
        except Exception:
            raise ValueError("delivery_date must be YYYY-MM-DD")
        today = datetime.now(_LK_TZ).date()
        if d < today:
            raise ValueError(f"delivery_date {v} is in the past (Asia/Colombo today is {today.isoformat()})")
        return v

    @field_validator("response_format")
    @classmethod
    def validate_format(cls, v: str) -> str:
        if v not in ("markdown", "json"):
            raise ValueError("response_format must be 'markdown' or 'json'")
        return v

    @model_validator(mode="after")
    def check_shape(self) -> "CustomCakeRequestInput":
        if self.delivery_type == "delivered" and not self.city:
            raise ValueError("city is required when delivery_type is 'delivered'")
        if self.delivery_type == "pickup" and self.pickup_location_id is None:
            raise ValueError("pickup_location_id is required when delivery_type is 'pickup'")
        if not self.image_url and not self.image_base64:
            raise ValueError("provide the customer's cake picture as image_url or image_base64")
        if self.image_url and self.image_base64:
            raise ValueError("provide either image_url or image_base64, not both")
        return self


@mcp.tool(
    name="kapruka_custom_cake_request",
    annotations={
        "title": "Custom Cake — Submit for a Staff Quote",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def kapruka_custom_cake_request(params: CustomCakeRequestInput, ctx: Context) -> str:
    """Submit a custom (photo) cake request so Kapruka staff can price it.

    This does NOT place an order and returns NO price. Staff price the request
    in their admin queue (minutes to hours, working hours); the customer then
    gets an SMS (and e-mail if given). Poll kapruka_custom_cake_status with the
    returned `request_id` + the customer's phone when they come back.

    Collect EVERYTHING before calling: the picture, flavour, size, greeting
    text (3–30 chars), icing colour, delivered-or-pickup, city or pickup
    location, date, and the customer's name. Read the valid choices from
    kapruka_custom_cake_options first. Never invent a price or price range —
    before the quote exists the only honest answer is "our team will send you
    the price".

    Args:
        params (CustomCakeRequestInput): customer{name, phone, email?}, flavour, size,
            greeting, icing_color, delivery_date, delivery_type ('delivered'|'pickup'),
            city (delivered) or pickup_location_id (pickup), image_url OR image_base64,
            response_format.

    Returns:
        str: Confirmation. JSON schema:
        {"request_id": str, "status": "pending_quote", "image_url": str, "message": str}
        KEEP request_id IN THE CONVERSATION STATE.

        Errors: "Error (<code>): <message>" — invalid_parameter (choice not in
        options / greeting length / bad picture), city_not_deliverable,
        service_unavailable (custom cakes switched off), missing_field.
    """
    denied = _check_access(ctx)
    if denied:
        return denied

    try:
        if params.image_url:
            image_b64 = await fetch_image_base64(params.image_url)
        else:
            image_b64 = _validate_base64_image(params.image_base64 or "")
    except ValueError as e:
        return f"Error (invalid_image): {e}. Ask the customer to resend the cake picture as a JPG or PNG under 5 MB."

    body: dict = {
        "idempotency_key": str(uuid.uuid4()),
        "customer": params.customer.model_dump(exclude_none=True),
        "flavour": params.flavour,
        "size": params.size,
        "greeting": params.greeting,
        "icing_color": params.icing_color,
        "delivery_date": params.delivery_date,
        "delivery_type": params.delivery_type,
        "image_base64": image_b64,
    }
    if params.delivery_type == "pickup":
        body["pickup_location_id"] = params.pickup_location_id
    else:
        body["city"] = params.city

    try:
        data = await KaprukaClient().post("custom_cake_request", body=body)
    except Exception as e:
        return handle_api_error(e)

    if params.response_format == "json":
        return json.dumps(data, indent=2, ensure_ascii=False)

    rid = data.get("request_id", "—")
    lines = [
        f"## Custom cake request submitted — `{rid}`",
        f"**Status:** {data.get('status', 'pending_quote')}",
        "",
        "Kapruka's team will price the cake and the customer will receive an SMS "
        f"at {params.customer.phone}" + (" (and e-mail)" if params.customer.email else "") + " when the quote is ready. "
        "They can also just ask here — check with kapruka_custom_cake_status using this "
        "request_id and the same phone number.",
        "",
        f"_Keep `request_id` = `{rid}` in the conversation. No price has been set yet — do not estimate one._",
    ]
    msg = data.get("message")
    if msg:
        lines.append("")
        lines.append(str(msg))
    return "\n".join(lines)


# ── Tool 3: status ───────────────────────────────────────────────────────────


class CustomCakeStatusInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    request_id: str = Field(..., min_length=6, max_length=80, description="request_id from kapruka_custom_cake_request.")
    phone: str = Field(..., min_length=7, max_length=30, description="The customer.phone used on the request.")
    currency: str = Field(default="LKR", description="'LKR' (default) or 'USD' for the quote amounts.")
    response_format: str = Field(default="markdown", description="'markdown' (default) or 'json'.")

    @field_validator("request_id")
    @classmethod
    def validate_request_id(cls, v: str) -> str:
        if not _REQUEST_ID_RE.match(v):
            raise ValueError("request_id looks malformed")
        return v

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, v: str) -> str:
        if not _PHONE_RE.match(v):
            raise ValueError("phone must be a valid phone number")
        return v

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, v: str) -> str:
        v = v.upper()
        if v not in ("LKR", "USD"):
            raise ValueError("currency must be LKR or USD")
        return v

    @field_validator("response_format")
    @classmethod
    def validate_format(cls, v: str) -> str:
        if v not in ("markdown", "json"):
            raise ValueError("response_format must be 'markdown' or 'json'")
        return v


def _fmt_money(amount, currency: str) -> str:
    if amount is None:
        return f"{currency} —"
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return f"{currency} {amount}"
    return f"LKR {amount:,.0f}" if currency == "LKR" else f"{currency} {amount:,.2f}"


@mcp.tool(
    name="kapruka_custom_cake_status",
    annotations={
        "title": "Custom Cake — Quote Status",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def kapruka_custom_cake_status(params: CustomCakeStatusInput, ctx: Context) -> str:
    """Check whether Kapruka staff have priced a custom cake request.

    Statuses:
      - pending_quote — not priced yet. Tell the customer it is being reviewed;
        do not poll more often than every few minutes.
      - quoted — present the items and total, relay staff `instructions`, say
        the DELIVERY FEE IS ADDED AT ORDER TIME, and ask whether to go ahead.
        Place the order only after a clear yes: kapruka_create_order with a
        cart line {"custom_cake_request_id": ..., "phone": ...} (plus any
        catalogue items — one delivery fee covers the whole order), reusing
        `request.city` / `request.delivery_date` unless the customer changed
        them. `quote_url` is the same quote on the website if they prefer to
        pay there.
      - expired — quote older than quote_valid_days; offer to submit a new
        request.
    404 request_not_found = wrong id/phone pair, or staff declined/removed it:
    ask the customer to contact Kapruka support or submit again.

    Args:
        params (CustomCakeStatusInput): request_id, phone, currency ('LKR'|'USD'), response_format.

    Returns:
        str: JSON schema:
        {
          "request_id": str, "status": "pending_quote"|"quoted"|"expired", "image_url": str,
          "request": {"flavour", "size", "greeting", "icing_color", "delivery_type",
                      "city", "delivery_address", "delivery_date"},
          "quote": {                                   # only for quoted / expired
            "items": [{"name": str, "quantity": int, "total": number}],
            "total": number, "currency": str, "instructions": str,
            "quoted_at": str, "expires_at": str, "quote_url": str,
            "delivery_fee_note": str
          }
        }
    """
    denied = _check_access(ctx)
    if denied:
        return denied
    try:
        data = await KaprukaClient().post(
            "custom_cake_status",
            body={"request_id": params.request_id, "phone": params.phone},
            currency=params.currency if params.currency != "LKR" else None,
        )
    except Exception as e:
        return handle_api_error(e)

    if params.response_format == "json":
        return json.dumps(data, indent=2, ensure_ascii=False)

    status = data.get("status", "unknown")
    rid = data.get("request_id", params.request_id)
    req = data.get("request") or {}
    lines = [f"## Custom cake `{rid}` — {status}"]

    if req:
        where = req.get("city") if req.get("delivery_type") != "pickup" else f"pickup: {req.get('delivery_address')}"
        lines.append(
            f"{req.get('size', '')} {req.get('flavour', '')} cake, icing {req.get('icing_color', '')}, "
            f"\"{req.get('greeting', '')}\" — {where}, {req.get('delivery_date', '')}"
        )
    lines.append("")

    if status == "pending_quote":
        lines.append(
            "Kapruka's team has not priced this yet. Tell the customer it is being "
            "reviewed and they'll get an SMS when the quote is ready. Do not estimate "
            "a price; check again in a few minutes at the earliest."
        )
        return "\n".join(lines)

    quote = data.get("quote") or {}
    currency = quote.get("currency") or params.currency
    if status == "expired":
        lines.append(
            f"This quote expired on {quote.get('expires_at', '—')} "
            f"(was {_fmt_money(quote.get('total'), currency)}). Offer to submit a new "
            "request with kapruka_custom_cake_request."
        )
        return "\n".join(lines)

    if status == "quoted":
        lines.append("**Quote from Kapruka staff**")
        lines.append("")
        lines.append("| Item | Qty | Total |")
        lines.append("|---|---|---|")
        for it in quote.get("items") or []:
            lines.append(f"| {it.get('name')} | {it.get('quantity', 1)} | {_fmt_money(it.get('total'), currency)} |")
        lines.append("")
        lines.append(f"**Cake total: {_fmt_money(quote.get('total'), currency)}** — delivery fee is added when the order is placed.")
        if quote.get("instructions"):
            lines.append(f"**Note from staff:** {quote['instructions']}")
        lines.append(f"Quote valid until **{quote.get('expires_at', '—')}**.")
        if quote.get("quote_url"):
            lines.append(f"Website version of this quote: {quote['quote_url']}")
        lines.append("")
        lines.append(
            "Ask the customer whether to go ahead. On a clear yes, call kapruka_create_order "
            f"with cart line {{\"custom_cake_request_id\": \"{rid}\", \"phone\": \"{params.phone}\"}}"
            + (f", delivery city \"{req.get('city')}\" and date {req.get('delivery_date')}" if req.get("city") else "")
            + " (plus any catalogue items they want in the same delivery)."
        )
        return "\n".join(lines)

    lines.append(f"Unrecognised status `{status}`. Raw: {json.dumps(data, ensure_ascii=False)[:400]}")
    return "\n".join(lines)


# ── Closed MCP: callable, but omitted from tools/list (same trick as Phase 2).
from src.tools.customers import _HIDDEN_TOOLS  # noqa: E402

_HIDDEN_TOOLS.update(
    {"kapruka_custom_cake_options", "kapruka_custom_cake_request", "kapruka_custom_cake_status"}
)
