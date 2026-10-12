"""Economy delivery: Kapruka's cheaper courier option (commerce API, 2026-10-12).

The API decides whether an order can go economy (`delivery_check`'s `economy`
block) and charges it (`create_order` with `delivery.type: "economy"`). On top
of that, three MCP policies (Dulith, 2026-10-12):

- An optional economy choice is offered only when it saves at least
  `settings.economy_min_saving_lkr` on the order (Rs 5 off Colombo 03 is not
  worth a question). Economy-only carts are always shown: there is no choice.
- When economy is not offered, say nothing — never the API's reason.
- Economy orders are dated ECONOMY_DAYS_AHEAD days from today (Sri Lanka), or
  the API's `earliest_date` when that is later (5-to-7-day items).
"""

from __future__ import annotations

from datetime import date as Date, datetime, timedelta, timezone
from typing import Optional

from src.config.settings import settings

ECONOMY_DAYS_AHEAD = 4
_LK_TZ = timezone(timedelta(hours=5, minutes=30))


def economy_date(earliest: Optional[str] = None) -> str:
    """Today + ECONOMY_DAYS_AHEAD (Sri Lanka), or `earliest` when that is later."""
    d = datetime.now(_LK_TZ).date() + timedelta(days=ECONOMY_DAYS_AHEAD)
    try:
        if earliest and Date.fromisoformat(earliest) > d:
            d = Date.fromisoformat(earliest)
    except ValueError:
        pass
    return d.isoformat()


def economy_offer(data: dict) -> Optional[dict]:
    """What to offer from a delivery_check answer, or None to say nothing.

    {"forced": bool, "fee": number, "currency": "LKR", "promise": str,
     "delivery_date": "YYYY-MM-DD", "saving": number | None}
    """
    eco = data.get("economy")
    if not isinstance(eco, dict) or eco.get("available") is not True:
        return None
    fee = eco.get("fee")
    if not isinstance(fee, (int, float)):
        return None
    forced = eco.get("forced") is True
    saving = None
    standard = data.get("delivery_fee")
    if isinstance(standard, (int, float)) and (data.get("fee_currency") or "LKR") == "LKR":
        saving = standard - fee
    if not forced and (saving is None or saving < settings.economy_min_saving_lkr):
        return None
    return {
        "forced": forced,
        "fee": fee,
        "currency": eco.get("currency") or "LKR",
        "promise": eco.get("promise") or "",
        "delivery_date": economy_date(eco.get("earliest_date")),
        "saving": saving,
    }
