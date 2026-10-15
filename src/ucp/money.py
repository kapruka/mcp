"""UCP amounts are integers in the currency's ISO 4217 minor unit.

Every currency Kapruka prices (LKR, USD, GBP, AUD, EUR) has exponent 2, so
LKR 2,250 is 225000 and USD 8.33 is 833.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

_EXPONENT = {"LKR": 2, "USD": 2, "GBP": 2, "AUD": 2, "EUR": 2}

# What a checkout is charged in. Kapruka's checkout takes LKR or USD; GBP, AUD
# and EUR customers are charged in USD.
CHECKOUT_CURRENCIES = ("LKR", "USD")


def to_minor(amount, currency: str) -> int:
    exp = _EXPONENT.get(currency, 2)
    value = Decimal(str(amount)) * (10 ** exp)
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def to_major(minor: int, currency: str) -> float:
    exp = _EXPONENT.get(currency, 2)
    return float(Decimal(minor) / (10 ** exp))


def checkout_currency(context: dict | None) -> str:
    """The currency the buyer will be charged in.

    `context.currency` is a buyer signal; the business decides (UCP checkout
    `currency`). Kapruka charges LKR or USD: other supported currencies, and
    buyers signalling a country other than Sri Lanka, are charged in USD.
    """
    ctx = context if isinstance(context, dict) else {}
    cur = str(ctx.get("currency") or "").upper()
    if cur in CHECKOUT_CURRENCIES:
        return cur
    if cur:
        return "USD"
    country = str(ctx.get("address_country") or "").upper()
    if country and country != "LK":
        return "USD"
    return "LKR"
