"""Phase 2 customer tools: profile, order history, and saved addresses.

Backed by /tools/commerce_phase2.jsp on www.kapruka.com (bearer-authed).
All three are read-only lookups keyed by the customer's email address.

NOTE: during the Phase 2 rollout the upstream backend only serves data for
designated test accounts; other emails return not-found. The tools surface
that upstream behaviour as a normal error string.
"""

import hmac
import html as html_mod
import json
import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.api.client import KaprukaClient, handle_api_error
from src.config.settings import settings
from src.server import mcp

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

_ACCESS_DENIED = (
    "Error: This tool requires a valid access_token. Phase 2 customer tools are "
    "in a private preview; contact mcp_support@kapruka.com for access."
)


def _check_access(token: str | None) -> bool:
    """Constant-time check of the caller-supplied Phase 2 access token.

    Fails closed: if no KAPRUKA_PHASE2_ACCESS_TOKEN is configured server-side,
    every call is refused rather than left open.
    """
    expected = settings.phase2_access_token
    if not expected or not token:
        return False
    return hmac.compare_digest(token.strip(), expected)


def _clean(v) -> str:
    """Scrub backend artifacts: stray '<BR' fragments and HTML entities."""
    if v is None:
        return ""
    s = html_mod.unescape(str(v))
    s = re.sub(r"<\s*/?\s*br\s*/?\s*>?", " ", s, flags=re.I)
    return " ".join(s.split()).strip()


def _fmt_amount(a) -> str:
    """Backend returns amount as {'value': '1530', 'currency': 'LKR'}."""
    if isinstance(a, dict):
        val, cur = a.get("value"), a.get("currency", "LKR")
        try:
            return f"{cur} {float(val):,.2f}"
        except (TypeError, ValueError):
            return f"{cur} {val}" if val is not None else ""
    try:
        return f"LKR {float(a):,.2f}"
    except (TypeError, ValueError):
        return _clean(a)


def _validate_email(v: str) -> str:
    v = v.strip().lower()
    if not _EMAIL_RE.match(v):
        raise ValueError("email must be a valid email address")
    return v


class CustomerDetailsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    email: str = Field(
        ...,
        description="Customer's email address on their Kapruka account.",
        min_length=6,
        max_length=120,
    )
    access_token: str = Field(
        ...,
        description=(
            "Phase 2 access token issued by the Kapruka team. Required — "
            "these tools are in a private preview."
        ),
        min_length=8,
        max_length=200,
    )
    response_format: str = Field(
        default="markdown",
        description="'markdown' (default) or 'json'.",
    )

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        return _validate_email(v)

    @field_validator("response_format")
    @classmethod
    def _fmt(cls, v: str) -> str:
        if v not in ("markdown", "json"):
            raise ValueError("response_format must be 'markdown' or 'json'")
        return v


class OrderHistoryInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    email: str = Field(
        ...,
        description="Customer's email address on their Kapruka account.",
        min_length=6,
        max_length=120,
    )
    access_token: str = Field(
        ...,
        description=(
            "Phase 2 access token issued by the Kapruka team. Required — "
            "these tools are in a private preview."
        ),
        min_length=8,
        max_length=200,
    )
    limit: int = Field(
        default=5,
        description="Number of most-recent orders to return (1-20).",
        ge=1,
        le=20,
    )
    response_format: str = Field(
        default="markdown",
        description="'markdown' (default) or 'json'.",
    )

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        return _validate_email(v)

    @field_validator("response_format")
    @classmethod
    def _fmt(cls, v: str) -> str:
        if v not in ("markdown", "json"):
            raise ValueError("response_format must be 'markdown' or 'json'")
        return v


class CustomerAddressesInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    email: str = Field(
        ...,
        description="Customer's email address on their Kapruka account.",
        min_length=6,
        max_length=120,
    )
    access_token: str = Field(
        ...,
        description=(
            "Phase 2 access token issued by the Kapruka team. Required — "
            "these tools are in a private preview."
        ),
        min_length=8,
        max_length=200,
    )
    response_format: str = Field(
        default="markdown",
        description="'markdown' (default) or 'json'.",
    )

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        return _validate_email(v)

    @field_validator("response_format")
    @classmethod
    def _fmt(cls, v: str) -> str:
        if v not in ("markdown", "json"):
            raise ValueError("response_format must be 'markdown' or 'json'")
        return v


@mcp.tool(
    name="kapruka_customer_details",
    annotations={
        "title": "Get Kapruka Customer Details",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def kapruka_customer_details(params: CustomerDetailsInput) -> str:
    """Look up a Kapruka customer's account profile by email address.

    Returns the profile on file: name, email, phone, default city, loyalty
    tier and account metadata as provided by the Kapruka backend. Use this to
    personalise a shopping session ("welcome back, Sandaru") or to prefill
    checkout details the customer has already saved.

    Only call this with an email the customer has themselves provided in the
    conversation. During the Phase 2 rollout the backend serves designated
    test accounts only; other emails return a not-found error.

    Args:
        params (CustomerDetailsInput):
            - email (str): customer's account email
            - response_format (str): 'markdown' (default) or 'json'

    Returns:
        str: Customer profile in the requested format, or
        "Error: <message>" on failure (e.g. no account for that email).
    """
    if not _check_access(params.access_token):
        return _ACCESS_DENIED

    try:
        client = KaprukaClient()
        data = await client.call("customer_details", email=params.email)
    except Exception as e:
        return handle_api_error(e)

    if params.response_format == "json":
        return json.dumps(data, indent=2, ensure_ascii=False)

    cust = data.get("customer", data) or {}
    name = _clean(
        cust.get("full name") or cust.get("name") or cust.get("full_name")
    ) or "Unknown"
    lines = [f"## Kapruka customer — {name}", ""]
    rows = []
    for label, keys in (
        ("Email", ("email",)),
        ("Phone", ("phone", "mobile", "telephone")),
        ("Language", ("language",)),
    ):
        for k in keys:
            v = _clean(cust.get(k))
            if v:
                rows.append(f"| {label} | {v} |")
                break
    billing = cust.get("billing") or {}
    if isinstance(billing, dict):
        b_phone = _clean(billing.get("phone"))
        if b_phone and not any("Phone" in r for r in rows):
            rows.append(f"| Phone (billing) | {b_phone} |")
        b_parts = [
            _clean(billing.get(k))
            for k in ("address", "city", "country")
            if _clean(billing.get(k)) not in ("", "NA")
        ]
        if b_parts:
            rows.append(f"| Billing address | {', '.join(b_parts)} |")
    if rows:
        lines += ["| | |", "|---|---|", *rows, ""]
    else:
        lines.append("_No profile fields returned._")
    return "\n".join(lines)


@mcp.tool(
    name="kapruka_order_history",
    annotations={
        "title": "Get Kapruka Customer Order History",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def kapruka_order_history(params: OrderHistoryInput) -> str:
    """List a Kapruka customer's most recent orders by email address.

    Returns up to `limit` recent orders with order number, date, status, total
    and a summary of items. Use it to answer "where is my last order?",
    "what did I buy for mum last time?", or to suggest a repeat purchase.
    For full tracking detail on one order, follow up with kapruka_track_order
    using the order number from this list.

    Only call this with an email the customer has themselves provided in the
    conversation. During the Phase 2 rollout the backend serves designated
    test accounts only; other emails return a not-found error.

    Args:
        params (OrderHistoryInput):
            - email (str): customer's account email
            - limit (int): max orders to return, 1-20 (default 5)
            - response_format (str): 'markdown' (default) or 'json'

    Returns:
        str: Recent orders in the requested format, or
        "Error: <message>" on failure.
    """
    if not _check_access(params.access_token):
        return _ACCESS_DENIED

    try:
        client = KaprukaClient()
        data = await client.call(
            "order_history", email=params.email, limit=params.limit
        )
    except Exception as e:
        return handle_api_error(e)

    if params.response_format == "json":
        return json.dumps(data, indent=2, ensure_ascii=False)

    orders = data.get("orders") or data.get("order_history") or []
    if not orders:
        return "No orders found for this account."

    lines = [f"## Recent Kapruka orders ({len(orders)})", ""]
    for o in orders:
        num = _clean(
            o.get("reference") or o.get("order_number") or o.get("orderNo")
        ) or "?"
        status = _clean(o.get("status_display") or o.get("status"))
        head = f"**`{num}`**"
        if status:
            head += f" — {status}"
        lines.append(head)
        meta = []
        odate = _clean(o.get("order date") or o.get("order_date") or o.get("date"))
        if odate:
            meta.append(f"ordered {odate}")
        ddate = _clean(o.get("delivery date") or o.get("delivery_date"))
        if ddate:
            meta.append(f"delivery {ddate}")
        amount = _fmt_amount(o.get("amount") or o.get("total"))
        if amount:
            meta.append(amount)
        if meta:
            lines.append("  " + " · ".join(meta))
        recipient = o.get("recipient") or {}
        if isinstance(recipient, dict):
            r_name = _clean(recipient.get("name"))
            r_city = _clean(recipient.get("city"))
            if r_name:
                to = f"  to {r_name}"
                if r_city:
                    to += f", {r_city}"
                lines.append(to)
        items = o.get("items") or []
        for it in items[:4]:
            iname = _clean(it.get("name") or it.get("product_name")) or "item"
            qty = it.get("quantity") or 1
            lines.append(f"  - {iname} ×{qty}")
        if len(items) > 4:
            lines.append(f"  - … and {len(items) - 4} more items")
        lines.append("")
    return "\n".join(lines)


@mcp.tool(
    name="kapruka_customer_addresses",
    annotations={
        "title": "Get Kapruka Customer Saved Addresses",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def kapruka_customer_addresses(params: CustomerAddressesInput) -> str:
    """List the delivery addresses saved on a Kapruka customer's account.

    Returns each saved address with recipient name, street address, city and
    phone. Use it at checkout so the customer can say "send it to my home
    address" instead of dictating the address again; pass the chosen address
    into kapruka_create_order.

    Only call this with an email the customer has themselves provided in the
    conversation. During the Phase 2 rollout the backend serves designated
    test accounts only; other emails return a not-found error.

    Args:
        params (CustomerAddressesInput):
            - email (str): customer's account email
            - response_format (str): 'markdown' (default) or 'json'

    Returns:
        str: Saved addresses in the requested format, or
        "Error: <message>" on failure.
    """
    if not _check_access(params.access_token):
        return _ACCESS_DENIED

    try:
        client = KaprukaClient()
        data = await client.call("customer_addresses", email=params.email)
    except Exception as e:
        return handle_api_error(e)

    if params.response_format == "json":
        return json.dumps(data, indent=2, ensure_ascii=False)

    book = data.get("address book") or data.get("addresses") or []
    recent = data.get("recent delivery addresses") or []
    if not book and not recent:
        return "No saved addresses for this account."

    lines: list[str] = []

    def _render(a: dict, i: int) -> None:
        label = _clean(a.get("label")) or _clean(a.get("name")) or f"Address {i}"
        lines.append(f"**{label}**")
        addr_parts = [
            _clean(a.get(k))
            for k in ("address", "street", "city", "country")
            if _clean(a.get(k)) not in ("", "NA")
        ]
        if addr_parts:
            lines.append(f"- {', '.join(addr_parts)}")
        phone = _clean(a.get("mobile") or a.get("phone"))
        if phone:
            lines.append(f"- {phone}")
        lines.append("")

    if book:
        lines.append(f"## Saved addresses ({len(book)})")
        lines.append("")
        for i, a in enumerate(book, 1):
            _render(a, i)
    if recent:
        lines.append(f"## Recent delivery addresses ({len(recent)})")
        lines.append("")
        for i, a in enumerate(recent, 1):
            _render(a, i)
    return "\n".join(lines)


# ── Private preview: keep these tools callable but omit them from tools/list.
# call_tool resolves by name via ToolManager.get_tool, which is unaffected.
_HIDDEN_TOOLS = {
    "kapruka_customer_details",
    "kapruka_order_history",
    "kapruka_customer_addresses",
}

_orig_list_tools = mcp._tool_manager.list_tools


def _visible_tools():
    return [t for t in _orig_list_tools() if t.name not in _HIDDEN_TOOLS]


mcp._tool_manager.list_tools = _visible_tools
