"""UCP Checkout (+ Fulfillment extension) on top of Kapruka's commerce API.

A UCP checkout is a session the agent edits (create, update…) until it holds
everything Kapruka needs: priced lines, a Sri Lankan delivery address in a
Kapruka delivery city, a delivery option, the recipient's phone and the
sender's name. Kapruka's `create_order` is one-shot, so the session lives here
(store.py) and we mint a Kapruka checkout link only once the session is
complete. Level 1 takes no payment: a complete session is
`requires_escalation` and `continue_url` is Kapruka's pay page.

Delivery options (fulfillment groups[0].options):
  - `standard:YYYY-MM-DD` — Kapruka delivery on that date; the earliest open
    date and the next two that are open. Fee = what checkout charges for the
    cart (delivery_check with the cart).
  - `economy` — the cheaper courier, when Kapruka offers it and it saves at
    least ECONOMY_MIN_SAVING_LKR (src/economy.py). Economy-only carts get only
    this option.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
import time
import uuid
from datetime import date as Date, datetime, timedelta, timezone
from typing import Optional

import httpx

from src.api.client import KaprukaClient
from src.config.settings import settings
from src.economy import economy_date, economy_offer
from src.tools.delivery import _clean_aliases
from src.ucp import STOREFRONT
from src.ucp.catalog import _fetch_product, _variant_id
from src.ucp.money import checkout_currency, to_minor
from src.ucp.negotiation import Negotiated, UcpError, error_response
from src.ucp.store import store

ROOT = "dev.ucp.shopping.checkout"
FULFILLMENT = "dev.ucp.shopping.fulfillment"

SESSION_TTL = 6 * 3600            # UCP default checkout TTL
MAX_LINES = 30
MAX_QUANTITY = 99
STANDARD_DATES = 3                # earliest open date + the next open ones checked
_LINK_MARGIN = 300                # re-mint a Kapruka link with < 5 min left

_LK_TZ = timezone(timedelta(hours=5, minutes=30))
_PHONE_RE = re.compile(r"^[+\d][\d\s\-()]{6,30}$")
_METHOD_ID, _DEST_ID, _GROUP_ID = "shipping_1", "dest_1", "group_1"

LINKS = [
    {"type": "privacy_policy", "url": "https://www.kapruka.com/contactUs/privacyPolicy.jsp"},
    {"type": "terms_of_service", "url": "https://www.kapruka.com/shop/returns-refunds-and-other-policies-of-kapruka"},
    {"type": "refund_policy", "url": "https://www.kapruka.com/shop/returns-refunds-and-other-policies-of-kapruka"},
    {"type": "shipping_policy", "url": "https://www.kapruka.com/contactUs/shippingPolicy.jsp"},
    {"type": "faq", "url": "https://www.kapruka.com/shop/faq"},
]


def _now() -> float:
    return time.time()


def _today() -> Date:
    return datetime.now(_LK_TZ).date()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _err(code: str, content: str, path: Optional[str] = None, severity: str = "recoverable") -> dict:
    m = {"type": "error", "code": code, "content": content, "severity": severity}
    if path:
        m["path"] = path
    return m


def _bad_request(content: str) -> UcpError:
    return UcpError("invalid_request", content, kind="transport", http_status=400, rpc_code=-32602,
                    continue_url=None)


# ── Request parsing ──────────────────────────────────────────────────────────


def _parse_lines(raw, existing: list[dict]) -> list[dict]:
    if not isinstance(raw, list) or not raw:
        raise _bad_request("checkout.line_items must be a non-empty array.")
    if len(raw) > MAX_LINES:
        raise _bad_request(f"At most {MAX_LINES} line items per checkout.")
    used = {line["id"] for line in existing}
    next_n = 1 + max([int(m.group(1)) for i in used if (m := re.fullmatch(r"li_(\d+)", i))] or [0])
    lines, seen = [], set()
    for i, li in enumerate(raw):
        item = li.get("item") if isinstance(li, dict) else None
        item_id = item.get("id") if isinstance(item, dict) else None
        qty = li.get("quantity") if isinstance(li, dict) else None
        if not isinstance(item_id, str) or not item_id.strip():
            raise _bad_request(f"line_items[{i}].item.id is required.")
        if not isinstance(qty, int) or isinstance(qty, bool) or qty < 1:
            raise _bad_request(f"line_items[{i}].quantity must be a positive integer.")
        lid = li.get("id") if isinstance(li.get("id"), str) and li.get("id") in used else None
        if lid is None or lid in seen:
            lid = f"li_{next_n}"
            next_n += 1
        seen.add(lid)
        lines.append({"id": lid, "item_id": item_id.strip(), "quantity": qty})
    return lines


_ADDRESS_FIELDS = ("street_address", "extended_address", "address_locality", "address_region",
                   "postal_code", "address_country", "first_name", "last_name", "phone_number")


def _apply_fulfillment(state: dict, raw) -> None:
    if raw is None:
        return
    if not isinstance(raw, dict):
        raise _bad_request("checkout.fulfillment must be an object.")
    methods = raw.get("methods") or []
    if not isinstance(methods, list):
        raise _bad_request("fulfillment.methods must be an array.")
    if not methods:
        return
    m = methods[0] if isinstance(methods[0], dict) else {}
    if m.get("type") not in (None, "shipping"):
        state["unsupported_method"] = m.get("type")
        return
    state.pop("unsupported_method", None)
    dests = m.get("destinations")
    if isinstance(dests, list) and dests:
        d = dests[0] if isinstance(dests[0], dict) else {}
        state["dest"] = {k: str(d[k]).strip() for k in _ADDRESS_FIELDS if isinstance(d.get(k), (str, int))
                         and str(d[k]).strip()}
    groups = m.get("groups")
    if isinstance(groups, list) and groups and isinstance(groups[0], dict) and "selected_option_id" in groups[0]:
        sel = groups[0]["selected_option_id"]
        state["selected_option_id"] = sel if isinstance(sel, str) else None


def _apply_request(state: dict, req: dict, *, create: bool) -> None:
    if not isinstance(req, dict):
        raise _bad_request("`checkout` must be an object.")
    state["lines"] = _parse_lines(req.get("line_items"), [] if create else state.get("lines", []))
    if "buyer" in req:
        if req["buyer"] is not None and not isinstance(req["buyer"], dict):
            raise _bad_request("checkout.buyer must be an object.")
        state["buyer"] = {k: str(v).strip() for k, v in (req["buyer"] or {}).items()
                          if k in ("first_name", "last_name", "email", "phone_number") and isinstance(v, str)}
    if create and isinstance(req.get("context"), dict):
        state["context"] = {k: v for k, v in req["context"].items()
                            if k in ("address_country", "address_region", "postal_code", "currency", "language")
                            and isinstance(v, str)}
    _apply_fulfillment(state, req.get("fulfillment"))


# ── Evaluation ───────────────────────────────────────────────────────────────


async def _cities() -> list[dict]:
    data = await KaprukaClient().call("delivery_cities")
    return [{"name": c["name"], "aliases": _clean_aliases(c.get("aliases"))}
            for c in data.get("cities") or [] if isinstance(c, dict) and c.get("name")]


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


async def _resolve_city(dest: dict) -> tuple[Optional[str], list[str]]:
    """(canonical Kapruka city, suggestions) for the destination's locality."""
    cities = await _cities()
    tried = [dest.get("address_locality"), dest.get("address_region")]
    for value in [t for t in tried if t]:
        n = _norm(value)
        for c in cities:
            if _norm(c["name"]) == n or any(_norm(a) == n for a in c["aliases"]):
                return c["name"], []
    n = _norm(dest.get("address_locality") or "")
    suggestions = [c["name"] for c in cities if n and (n in _norm(c["name"]) or _norm(c["name"]) in n)][:8]
    return None, suggestions


def _fmt_day(d: Date) -> str:
    return f"{d.strftime('%a')} {d.day} {d.strftime('%b')}"


def _lk_time(d: Date, end: bool = False) -> str:
    return f"{d.isoformat()}T{'23:59:59' if end else '00:00:00'}+05:30"


async def _delivery_options(city: str, cart: list[tuple[str, int]], currency: str) -> list[dict]:
    client = KaprukaClient()
    cart_param = ",".join(f"{pid}:{qty}" for pid, qty in cart)

    async def check(d: Date) -> dict:
        return await client.call("delivery_check", city=city, delivery_date=d.isoformat(),
                                 currency=currency, cart=cart_param)

    first = await check(_today())
    options: list[dict] = []
    offer = economy_offer(first) if currency == "LKR" else None
    if offer:
        eco_date = Date.fromisoformat(offer["delivery_date"])
        promise = (offer.get("promise") or "").rstrip(".")
        options.append({
            "id": "economy",
            "title": "Economy delivery",
            "description": (f"{promise}; expected by {_fmt_day(eco_date)}." if promise
                            else f"Expected by {_fmt_day(eco_date)}."),
            "amount": to_minor(offer["fee"], offer.get("currency") or "LKR"),
            "kind": "economy", "date": eco_date.isoformat(),
            "earliest": _lk_time(_today() + timedelta(days=1)), "latest": _lk_time(eco_date, end=True),
        })
        if offer["forced"]:
            return options

    base = _today() if first.get("available") else None
    if base is None and first.get("next_available_date"):
        try:
            base = Date.fromisoformat(first["next_available_date"])
        except ValueError:
            base = None
    if base is None:
        return options
    answers = {base: first if base == _today() else None}
    later = [base + timedelta(days=i) for i in range(0 if answers[base] is None else 1, STANDARD_DATES)]
    for d, ans in zip(later, await asyncio.gather(*(check(d) for d in later))):
        answers[d] = ans
    standard = []
    for d in sorted(answers):
        ans = answers[d]
        if not ans or not ans.get("available") or ans.get("delivery_fee") is None:
            continue
        fee_cur = ans.get("fee_currency") or currency
        standard.append({
            "id": f"standard:{d.isoformat()}",
            "title": f"Standard delivery on {_fmt_day(d)}",
            "description": f"Delivered by Kapruka on {d.strftime('%A')} {d.day} {d.strftime('%B')}.",
            "amount": to_minor(ans["delivery_fee"], fee_cur),
            "kind": "standard", "date": d.isoformat(),
            "earliest": _lk_time(d), "latest": _lk_time(d, end=True),
        })
    # Cheapest first is economy when shown; then dates in order.
    return options + standard


def _fingerprint(state: dict, option: dict) -> str:
    payload = {
        "lines": [(li["kapruka_id"], li["quantity"]) for li in state["_priced"]],
        "dest": state.get("dest"), "option": option["id"], "date": option["date"],
        "buyer": state.get("buyer"), "currency": state["currency"],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


# Per-IP budget for minting Kapruka checkout links (each is a create_order).
_mints: dict[str, list[float]] = {}


def _take_mint(ip: str) -> bool:
    now = time.monotonic()
    window = [t for t in _mints.get(ip, []) if t > now - 3600]
    if len(window) >= settings.ucp_mint_per_hour:
        _mints[ip] = window
        return False
    window.append(now)
    _mints[ip] = window
    return True


_ORDER_ERRORS = {
    "city_not_deliverable_for_item": ("address_undeliverable", "$.fulfillment.methods[0].destinations[0]"),
    "city_not_deliverable": ("address_undeliverable", "$.fulfillment.methods[0].destinations[0]"),
    "date_not_deliverable": ("fulfillment_option_unavailable", "$.fulfillment.methods[0].groups[0].selected_option_id"),
    "past_delivery_date": ("fulfillment_option_unavailable", "$.fulfillment.methods[0].groups[0].selected_option_id"),
    "economy_not_available": ("fulfillment_option_unavailable", "$.fulfillment.methods[0].groups[0].selected_option_id"),
    "product_out_of_stock": ("out_of_stock", "$.line_items"),
    "product_not_found": ("item_unavailable", "$.line_items"),
}


async def _mint(state: dict, option: dict, names: dict, ip: str) -> tuple[Optional[dict], Optional[dict]]:
    """(mint record, error message). Reuses a live link for an unchanged session."""
    fp = _fingerprint(state, option)
    prev = state.get("mint")
    if prev and prev.get("fingerprint") == fp and prev.get("expires_ts", 0) > _now() + _LINK_MARGIN:
        return prev, None
    if not _take_mint(ip):
        raise UcpError("rate_limited", "Too many checkouts from this client; retry later.",
                       kind="transport", http_status=429, rpc_code=-32000, retry_after=600, continue_url=None)
    dest = state["dest"]
    address = ", ".join(p for p in (dest.get("street_address"), dest.get("extended_address")) if p)
    delivery = {"address": address, "city": names["city"], "date": option["date"], "location_type": "house"}
    if option["kind"] == "economy":
        delivery["date"] = economy_date()
        delivery["type"] = "economy"
    body = {
        "auth_token": None,
        "idempotency_key": str(uuid.uuid4()),
        "cart": [{"product_id": li["kapruka_id"], "quantity": li["quantity"]} for li in state["_priced"]],
        "recipient": {"name": names["recipient"], "phone": names["phone"]},
        "delivery": delivery,
        "sender": {"name": names["sender"], "anonymous": False},
    }
    try:
        data = await KaprukaClient().post("create_order", body=body, currency=state["currency"])
    except httpx.HTTPStatusError as e:
        code, content = "checkout_unavailable", "Kapruka could not create the checkout."
        try:
            err = e.response.json().get("error") or {}
            code, content = err.get("code") or code, err.get("message") or content
        except Exception:
            pass
        ucp_code, path = _ORDER_ERRORS.get(code, ("checkout_unavailable", None))
        return None, _err(ucp_code, content, path)
    except Exception:
        return None, _err("checkout_unavailable", "Kapruka's checkout is temporarily unavailable; retry.")
    url = data.get("checkout_url")
    if not url:
        return None, _err("checkout_unavailable", "Kapruka returned no checkout link; retry.")
    expires_ts = _now() + 3600
    try:
        expires_ts = datetime.fromisoformat(str(data.get("expires_at")).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        pass
    return {"fingerprint": fp, "checkout_url": url, "order_ref": data.get("order_ref"),
            "expires_ts": expires_ts, "summary": data.get("summary") or {}}, None


async def evaluate(state: dict, neg: Negotiated, ip: str) -> dict:
    """Recompute the session from Kapruka's data and build the UCP checkout."""
    currency = state["currency"]
    client = KaprukaClient()
    messages: list[dict] = []
    fulfillment_on = FULFILLMENT in neg.active

    # 1. Lines
    hits = await asyncio.gather(*(_fetch_product(client, li["item_id"], currency) for li in state["lines"]))
    priced, line_out, blocked = [], [], False
    for li, hit in zip(state["lines"], hits):
        if hit is None:
            messages.append(_err("item_unavailable", f"{li['item_id']} is not a Kapruka product or variant; "
                                 "the line was dropped."))
            continue
        detail, vid = hit["product"], hit["variant_id"]
        raw = detail.get("variants") or []
        variant = next((v for v in raw if v.get("id") == vid), None) if vid else None
        if variant is None and len(raw) == 1:
            variant = raw[0]
        idx = len(line_out)
        title = detail.get("name", li["item_id"])
        if variant and variant.get("name") and variant["name"] != "Default":
            title = f"{title} ({variant['name']})"
        price_src = (variant or {}).get("price") or detail.get("price") or {}
        if price_src.get("amount") is None:
            messages.append(_err("item_unavailable", f"{li['item_id']} has no price; the line was dropped."))
            continue
        qty = min(li["quantity"], MAX_QUANTITY)
        if qty != li["quantity"]:
            messages.append({"type": "warning", "code": "quantity_adjusted", "path": f"$.line_items[{idx}].quantity",
                             "content": f"At most {MAX_QUANTITY} of one item per order."})
        unit = to_minor(price_src["amount"], price_src.get("currency") or currency)
        kapruka_id = _variant_id(detail["id"], variant) if variant else detail["id"]
        item = {"id": li["item_id"], "title": title, "price": unit}
        images = detail.get("images") or []
        if images and isinstance(images[0], str):
            item["image_url"] = images[0]
        line_out.append({"id": li["id"], "item": item, "quantity": qty,
                         "totals": [{"type": "subtotal", "amount": unit * qty},
                                    {"type": "total", "amount": unit * qty}]})
        if len(raw) > 1 and variant is None:
            blocked = True
            names = sorted({k.capitalize() for v in raw for k in (v.get("attributes") or {}) if k != "weight"})
            messages.append(_err("variant_required",
                                 f"Choose {' and '.join(names) or 'an option'} for this item: send one of its "
                                 "variant IDs (get_product lists them).", f"$.line_items[{idx}].item.id"))
        elif not (variant or detail).get("in_stock", True):
            blocked = True
            messages.append(_err("out_of_stock", f"{title} is out of stock.", f"$.line_items[{idx}]"))
        priced.append({"line_id": li["id"], "kapruka_id": kapruka_id, "quantity": qty, "detail": detail})
    if not line_out:
        raise UcpError("item_unavailable", "None of the requested items can be bought from Kapruka.",
                       kind="outcome", continue_url=STOREFRONT)
    state["lines"] = [{"id": lo["id"], "item_id": lo["item"]["id"], "quantity": lo["quantity"]} for lo in line_out]
    state["_priced"] = priced
    subtotal = sum(lo["totals"][0]["amount"] for lo in line_out)

    # 2. Fulfillment
    dest = state.get("dest")
    city = None
    options: list[dict] = []
    selected = None
    method: Optional[dict] = None
    if not fulfillment_on:
        messages.append(_err("fulfillment_required",
                             "Kapruka needs a delivery address and date: complete the order on Kapruka.",
                             severity="requires_buyer_input"))
    elif state.get("unsupported_method"):
        messages.append(_err("fulfillment_method_unsupported",
                             "Kapruka delivers to addresses in Sri Lanka only (method type 'shipping').",
                             "$.fulfillment.methods[0].type"))
    elif not dest:
        messages.append(_err("field_required", "Add the delivery address in Sri Lanka: street_address and "
                             "address_locality (a Kapruka delivery city, e.g. 'Colombo 03' or 'Kandy').",
                             "$.fulfillment.methods[0].destinations"))
    else:
        dpath = "$.fulfillment.methods[0].destinations[0]"
        if dest.get("address_country") and dest["address_country"].upper() not in ("LK", "LKA", "SRI LANKA"):
            messages.append(_err("address_undeliverable", "Kapruka delivers within Sri Lanka only.",
                                 f"{dpath}.address_country"))
        else:
            city, suggestions = await _resolve_city(dest)
            if city is None:
                hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
                messages.append(_err("address_undeliverable",
                                     f"'{dest.get('address_locality', '')}' is not a Kapruka delivery city. Put the "
                                     f"recipient's city in address_locality.{hint}", f"{dpath}.address_locality"))
        if city:
            limited = [p for p in priced if (p["detail"].get("delivery") or {}).get("island_wide") is False]
            scope = await asyncio.gather(*(client.call(
                "delivery_check", city=city, delivery_date=_today().isoformat(), product_id=p["kapruka_id"])
                for p in limited))
            for p, ans in zip(limited, scope):
                if ans.get("item_deliverable") is False:
                    idx = next(i for i, lo in enumerate(line_out) if lo["id"] == p["line_id"])
                    cities = ", ".join((ans.get("deliverable_cities") or [])[:12])
                    messages.append(_err("address_undeliverable",
                                         f"{line_out[idx]['item']['title']} is delivered only to selected cities"
                                         + (f" ({cities})" if cities else "") + f", not {city}.",
                                         f"$.line_items[{idx}]"))
                    blocked = True
            if not blocked:
                options = await _delivery_options(
                    city, [(p["kapruka_id"], p["quantity"]) for p in priced], currency)
                if not options:
                    messages.append(_err("address_undeliverable",
                                         f"Kapruka has no delivery date open for {city} right now.", dpath))
        if options:
            wanted = state.get("selected_option_id")
            selected = next((o for o in options if o["id"] == wanted), None)
            if wanted and selected is None:
                messages.append({"type": "warning", "code": "fulfillment_option_changed",
                                 "path": "$.fulfillment.methods[0].groups[0].selected_option_id",
                                 "content": "The chosen delivery option is no longer available; "
                                            "the earliest one was selected."})
            # Default: the earliest standard date (gifts are usually for a day);
            # economy is pre-selected only when it is the only option.
            selected = selected or next((o for o in options if o["kind"] == "standard"), options[0])
            state["selected_option_id"] = selected["id"]

    if fulfillment_on:
        method = {"id": _METHOD_ID, "type": "shipping", "line_item_ids": [lo["id"] for lo in line_out]}
        if dest:
            method["destinations"] = [{"type": "shipping_address", "id": _DEST_ID, **dest}]
            method["selected_destination_id"] = _DEST_ID
        if options:
            method["groups"] = [{
                "id": _GROUP_ID, "line_item_ids": [lo["id"] for lo in line_out],
                "selected_option_id": selected["id"],
                "options": [{
                    "id": o["id"], "title": o["title"], "description": {"plain": o["description"]},
                    "carrier": "Kapruka" if o["kind"] == "standard" else "Kapruka economy courier",
                    "earliest_fulfillment_time": o["earliest"], "latest_fulfillment_time": o["latest"],
                    "totals": [{"type": "total", "amount": o["amount"]}],
                } for o in options],
            }]

    # 3. Who: sender (buyer) and recipient
    buyer = state.get("buyer") or {}
    sender = " ".join(p for p in (buyer.get("first_name"), buyer.get("last_name")) if p).strip()
    names = {"city": city, "sender": sender}
    if not sender:
        messages.append(_err("field_required", "The buyer's name is required (it is the sender on the gift card).",
                             "$.buyer.first_name"))
    if dest:
        recipient = " ".join(p for p in (dest.get("first_name"), dest.get("last_name")) if p).strip() or sender
        phone = dest.get("phone_number") or buyer.get("phone_number") or ""
        names.update(recipient=recipient, phone=phone)
        if not dest.get("street_address"):
            messages.append(_err("field_required", "The street address is required.",
                                 "$.fulfillment.methods[0].destinations[0].street_address"))
        if not phone:
            messages.append(_err("field_required", "The recipient's phone number is required for delivery.",
                                 "$.fulfillment.methods[0].destinations[0].phone_number"))
        elif not _PHONE_RE.match(phone):
            messages.append(_err("invalid_phone", "The phone number is not valid (e.g. +94771234567).",
                                 "$.fulfillment.methods[0].destinations[0].phone_number"))

    # 4. Ready? Mint Kapruka's checkout link.
    fee = selected["amount"] if selected else None
    totals = [{"type": "subtotal", "amount": subtotal}]
    if fee is not None:
        totals.append({"type": "fulfillment", "display_text": "Delivery", "amount": fee})
    complete = (not [m for m in messages if m["type"] == "error"]) and selected is not None
    mint = None
    if complete:
        mint, mint_error = await _mint(state, selected, names, ip)
        if mint_error:
            messages.append(mint_error)
    state["mint"] = mint
    if mint:
        s = mint["summary"]
        cur = s.get("currency") or currency
        totals = [{"type": "subtotal", "amount": to_minor(s.get("items_total", 0), cur)},
                  {"type": "fulfillment", "display_text": "Delivery", "amount": to_minor(s.get("delivery_fee", 0), cur)}]
        if s.get("addons_total"):
            totals.append({"type": "fee", "display_text": "Add-ons", "amount": to_minor(s["addons_total"], cur)})
        messages.append(_err("payment_required",
                             "Payment is taken on Kapruka's secure checkout page: hand the buyer to continue_url "
                             f"to review and pay (link valid until {_iso(mint['expires_ts'])}).",
                             severity="requires_buyer_input"))
    totals.append({"type": "total", "amount": sum(t["amount"] for t in totals)})

    status = "requires_escalation" if (mint or any(
        m.get("severity") in ("requires_buyer_input", "requires_buyer_review") for m in messages)) else "incomplete"
    if state.get("status") == "canceled":
        status = "canceled"
    state["status"] = status

    out = {
        "ucp": neg.ucp_block(ROOT, payment_handlers=True),
        "id": state["id"],
        "status": status,
        "currency": currency,
        "line_items": line_out,
        "totals": totals,
        "links": LINKS,
        "expires_at": _iso(state["expires"]),
    }
    if state.get("buyer"):
        out["buyer"] = state["buyer"]
    if method is not None:
        out["fulfillment"] = {"methods": [method]}
    if messages:
        out["messages"] = messages
    if status not in ("completed", "canceled"):
        first_url = (priced[0]["detail"].get("url") if priced else None) or STOREFRONT
        out["continue_url"] = mint["checkout_url"] if mint else first_url
    return out


# ── Operations ───────────────────────────────────────────────────────────────


def _load(checkout_id) -> dict:
    state = store().get(checkout_id) if isinstance(checkout_id, str) else None
    if state is None:
        raise UcpError("not_found", "No checkout session with that id (it may have expired).",
                       kind="outcome", http_status=404, continue_url=STOREFRONT)
    return state


def _save(state: dict, view: dict) -> dict:
    state.pop("_priced", None)
    state["view"] = view
    store().put(state, state["expires"])
    return view


async def create(req: dict, neg: Negotiated, ip: str) -> dict:
    now = _now()
    state = {"id": "chk_" + secrets.token_hex(12), "created": now, "expires": now + SESSION_TTL,
             "status": "incomplete", "platform": neg.platform.url}
    _apply_request(state, req, create=True)
    state["currency"] = checkout_currency(state.get("context"))
    return _save(state, await evaluate(state, neg, ip))


def _terminal(state: dict, op: str) -> Optional[dict]:
    if state.get("status") == "canceled":
        view = dict(state["view"])
        view["messages"] = [_err("checkout_canceled", f"This checkout was canceled; it cannot be {op}. "
                                 "Start a new checkout.", severity="unrecoverable")]
        return view
    return None


async def get(checkout_id, neg: Negotiated, ip: str) -> dict:
    state = _load(checkout_id)
    if state.get("status") == "canceled":
        return state["view"]
    mint = state.get("mint")
    if mint and mint.get("expires_ts", 0) < _now() + _LINK_MARGIN:
        return _save(state, await evaluate(state, neg, ip))   # refresh the expired pay link
    view = dict(state["view"])
    view["ucp"] = neg.ucp_block(ROOT, payment_handlers=True)
    return view


async def update(checkout_id, req: dict, neg: Negotiated, ip: str) -> dict:
    state = _load(checkout_id)
    if (t := _terminal(state, "updated")) is not None:
        return t
    _apply_request(state, req, create=False)
    return _save(state, await evaluate(state, neg, ip))


async def complete(checkout_id, req: dict, neg: Negotiated, ip: str) -> dict:
    """Level 1: no payment handlers, so completion is always a buyer handoff.

    A complete session answers `requires_escalation` with Kapruka's pay page in
    `continue_url`; an incomplete one says what is missing. No order is placed
    here — Kapruka creates it when the buyer pays.
    """
    state = _load(checkout_id)
    if (t := _terminal(state, "completed")) is not None:
        return t
    return _save(state, await evaluate(state, neg, ip))


async def cancel(checkout_id, neg: Negotiated) -> dict:
    state = _load(checkout_id)
    if state.get("status") == "canceled":
        view = dict(state["view"])
        view["messages"] = [_err("checkout_canceled", "This checkout is already canceled.",
                                 severity="unrecoverable")]
        return view
    state["status"] = "canceled"
    view = dict(state["view"])
    view.update(status="canceled", ucp=neg.ucp_block(ROOT, payment_handlers=True))
    view.pop("continue_url", None)
    view["messages"] = [m for m in view.get("messages", []) if m.get("code") != "payment_required"]
    if not view["messages"]:
        view.pop("messages")
    state["mint"] = None   # Kapruka's unpaid link simply expires
    return _save(state, view)


def not_found_response() -> dict:
    return error_response("not_found", "No checkout session with that id.", continue_url=STOREFRONT)
