"""Rupees per unit of a foreign currency, read from Kapruka's own price list.

Kapruka's commerce API prices in LKR/USD/GBP/AUD/EUR itself, so catalogue tools
never need this. It exists for sources that only speak LKR — Eagle's visual
search — so their prices and budgets line up with what the checkout will charge.

The rate is the ratio of one product's price in LKR to its price in the target
currency, which is exactly the conversion the storefront applies. Cached for 6
hours; a failure is remembered for 15 minutes so a missing price list doesn't
cost several upstream calls on every request.
"""

from __future__ import annotations

import logging
import time

from src.api.client import KaprukaClient

logger = logging.getLogger(__name__)

_RATE_TTL_S = 6 * 3600
_RATE_FAIL_TTL_S = 15 * 60
_RATE_CACHE: dict[str, tuple[float, float]] = {}
# Any in-catalogue product works — the ratio is the price list's, not the item's.
_RATE_ANCHORS = ("CAKE00KA002192", "CAKE00KA001423", "FLOWERS00T2075", "CAKE00KA001535")


async def lkr_per_unit(currency: str) -> float | None:
    """Rupees per 1 unit of `currency`; 1.0 for LKR; None when it can't be read."""
    cur = (currency or "LKR").upper()
    if cur == "LKR":
        return 1.0
    hit = _RATE_CACHE.get(cur)
    if hit:
        rate, at = hit
        if (time.time() - at) < (_RATE_TTL_S if rate else _RATE_FAIL_TTL_S):
            return rate or None
    client = KaprukaClient()
    for pid in _RATE_ANCHORS:
        try:
            in_lkr = await client.call("product", product_id=pid, currency="LKR")
            in_cur = await client.call("product", product_id=pid, currency=cur)
            a = float((in_lkr.get("price") or {}).get("amount") or 0)
            b = float((in_cur.get("price") or {}).get("amount") or 0)
        except Exception:
            continue
        if a > 0 and b > 0 and 1.0 < a / b < 100000.0:  # LKR is worth less than any peer
            _RATE_CACHE[cur] = (a / b, time.time())
            logger.info("fx: %s -> LKR %.2f (anchor %s)", cur, a / b, pid)
            return a / b
    _RATE_CACHE[cur] = (0.0, time.time())
    logger.warning("fx: could not derive an LKR rate for %s", cur)
    return None
