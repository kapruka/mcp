"""International gifts: Kapruka's "Send gifts to <country>" stores (API 2026-10-13/19).

Products in these stores are delivered by Kapruka's partner IN that country,
not from Sri Lanka. They are not in products_search; the stores are small
(USA 11, Canada 16, Australia 55, UK 10, UAE 37 in Oct 2026), so agents list a
whole store and choose from it.

  gift_countries          (phase 3) countries, per-order delivery fee, zones, earliest_date
  gift_country_products   (phase 3) a store's in-stock products, with `type`
  delivery_check          (phase 1) accepts store products + `zone`
  create_order            (phase 1) delivery.type "international_gift" + country/zone/zip

Kapruka matches zone names exactly (case and outer spaces aside) and has no
postcodes, so we map loose input ("abu dhabi", "DANDENONG") to the listed name
here, and only when exactly one zone matches.
"""

from __future__ import annotations

import re
from datetime import date as Date, datetime, timedelta, timezone
from typing import Optional

from src.api.client import KaprukaClient
from src.cache import cache

CURRENCIES = ("LKR", "USD")       # what these stores price in; GBP/AUD/EUR -> USD
EARLIEST_DAYS = 3                 # Kapruka's rule (today + 3, Sri Lanka date)
_COUNTRIES_TTL = 6 * 3600         # Kapruka: cache gift_countries up to 6 hours
_PRODUCTS_TTL = 30 * 60

_LK_TZ = timezone(timedelta(hours=5, minutes=30))

_ALIASES = {
    "usa": "USA", "us": "USA", "united states": "USA", "united states of america": "USA", "america": "USA",
    "uk": "UK", "gb": "UK", "united kingdom": "UK", "england": "UK", "britain": "UK",
    "great britain": "UK", "scotland": "UK", "wales": "UK", "northern ireland": "UK",
    "uae": "UAE", "united arab emirates": "UAE", "emirates": "UAE", "dubai": "UAE", "abu dhabi": "UAE",
    "sharjah": "UAE", "ajman": "UAE",
    "au": "Australia", "aus": "Australia", "melbourne": "Australia",
    "ca": "Canada", "nz": "New Zealand", "in": "India", "fr": "France",
}

# Kapruka returns the stored type codes as-is (`cakes`, `fruitbaskets`, Partner
# Central's `Hamper`) until its data is cleaned; this is display only.
_TYPE_LABELS = {
    "cake": "Cakes", "cakes": "Cakes", "flower": "Flowers", "flowers": "Flowers",
    "hamper": "Hampers", "hampers": "Hampers", "chocolate": "Chocolates", "chocolates": "Chocolates",
    "fruitbasket": "Fruit Baskets", "fruitbaskets": "Fruit Baskets", "giftpack": "Gift Packs",
    "giftpacks": "Gift Packs", "softoys": "Soft Toys", "softtoys": "Soft Toys", "softtoy": "Soft Toys",
    "card": "Cards", "cards": "Cards",
}


def gift_currency(currency: Optional[str]) -> str:
    cur = (currency or "LKR").upper()
    return cur if cur in CURRENCIES else "USD"


def type_label(raw: Optional[str]) -> str:
    if not raw or str(raw).strip().lower() in ("na", "none", "null"):
        return "Other"
    key = re.sub(r"[^a-z]", "", str(raw).lower())
    return _TYPE_LABELS.get(key) or str(raw).strip().title()


def clean_text(text: Optional[str]) -> str:
    """Strip the stray outer backticks some UAE descriptions carry."""
    t = (text or "").strip()
    if len(t) >= 2 and t[0] == "`" and t[-1] == "`":
        t = t[1:-1].strip()
    elif t.startswith("`"):
        t = t[1:].strip()
    return t


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower().replace("_", " ")).strip()


def _squash(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def earliest_date(country_row: Optional[dict] = None) -> str:
    if country_row and country_row.get("earliest_date"):
        return country_row["earliest_date"]
    return (datetime.now(_LK_TZ).date() + timedelta(days=EARLIEST_DAYS)).isoformat()


def fmt_day(iso: str) -> str:
    try:
        d = Date.fromisoformat(iso)
    except (TypeError, ValueError):
        return str(iso)
    return f"{d.strftime('%a')} {d.day} {d.strftime('%b')} ({iso})"


async def countries(currency: str = "LKR") -> list[dict]:
    currency = gift_currency(currency)
    key = f"intl:countries:{currency}"
    hit = cache.get(key)
    if hit is not None:
        return hit
    data = await KaprukaClient().post("gift_countries", body={}, currency=currency)
    rows = [c for c in data.get("countries") or [] if isinstance(c, dict) and c.get("country")]
    cache.set(key, rows, _COUNTRIES_TTL)
    return rows


async def products(country: str, currency: str = "LKR") -> dict:
    currency = gift_currency(currency)
    key = f"intl:products:{country}:{currency}"
    hit = cache.get(key)
    if hit is not None:
        return hit
    data = await KaprukaClient().post("gift_country_products", body={"country": country}, currency=currency)
    out = {
        "country": data.get("country") or country,
        "currency": data.get("currency") or currency,
        "products": [p for p in data.get("products") or [] if isinstance(p, dict) and p.get("id")],
    }
    cache.set(key, out, _PRODUCTS_TTL)
    return out


def match_country(name: Optional[str], rows: list[dict]) -> Optional[dict]:
    """The gift_countries row for a loosely written country name, else None."""
    if not name:
        return None
    n = _norm(name)
    by_norm = {_norm(r["country"]): r for r in rows}
    if n in by_norm:
        return by_norm[n]
    alias = _ALIASES.get(n)
    return next((r for r in rows if r["country"] == alias), None) if alias else None


def match_zone(name: Optional[str], zones: list[dict]) -> Optional[dict]:
    """The listed zone for loose input, only when exactly one zone matches."""
    if not name:
        return None
    s = _squash(name)
    hits = [z for z in zones if isinstance(z, dict) and z.get("name") and _squash(z["name"]) == s]
    return hits[0] if len(hits) == 1 else None


def money(amount, currency: str) -> str:
    try:
        v = float(amount)
    except (TypeError, ValueError):
        return f"{currency} {amount}"
    return f"LKR {v:,.0f}" if currency == "LKR" else f"{currency} {v:,.2f}"


def fee_text(row: dict) -> str:
    """'USD 71.00 per order', or the zone fees for zone-only countries."""
    fee = row.get("delivery_fee") or {}
    cur = fee.get("currency") or "LKR"
    zones = row.get("zones") or []
    if row.get("zone_required") and zones:
        parts = [f"{z['name']} {money((z.get('delivery_fee') or {}).get('amount'), cur)}" for z in zones]
        return "delivers ONLY to these zones (fee per order): " + ", ".join(parts)
    return f"delivery {money(fee.get('amount'), cur)} per order"
