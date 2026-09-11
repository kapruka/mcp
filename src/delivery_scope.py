"""Helpers for per-product delivery-city limits.

Most Kapruka gifts ship island-wide, but restaurant food, hotel cakes and
liquor only reach a limited city set (typically the Colombo area). The phase1
API exposes that scope as a `delivery` object on `product`, folds it into
`delivery_check` when a `product_id` is passed, and hard-rejects
`create_order` with `city_not_deliverable_for_item`. These helpers render
that data consistently across the three tools.
"""

from __future__ import annotations

from typing import Any

# How many city names to print inline before collapsing to "+N more". The API
# already caps the list (60 on product/delivery_check, 40 on the order error);
# `total` is the true count and is what we always report.
_SHOW_CITIES = 10


def fmt_city_list(cities: list[str] | None, total: int | None = None, show: int = _SHOW_CITIES) -> str:
    """'Colombo 01, Colombo 02, … (+37 more)' — honest about truncation."""
    cities = [c for c in (cities or []) if c]
    if total is None or total < len(cities):
        total = len(cities)
    if not cities:
        return f"{total} cities" if total else "no cities listed"
    head = ", ".join(cities[:show])
    rest = total - min(len(cities), show)
    return f"{head} (+{rest} more)" if rest > 0 else head


def describe_delivery(delivery: dict[str, Any] | None) -> str | None:
    """One-line markdown for a product's `delivery` object; None if absent."""
    if not isinstance(delivery, dict):
        return None
    if delivery.get("island_wide"):
        return "Island-wide"
    count = delivery.get("deliverable_city_count")
    cities = delivery.get("deliverable_cities") or []
    if count is None:
        count = len(cities)
    return (
        f"**Selected cities only** ({count}) — {fmt_city_list(cities, count)}. "
        "Confirm the customer's city with kapruka_check_delivery(product_id=...) "
        "before promising delivery."
    )


def is_city_limited(delivery: dict[str, Any] | None) -> bool:
    return isinstance(delivery, dict) and delivery.get("island_wide") is False
