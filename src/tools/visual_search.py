"""MCP tool: kapruka_visual_search (PRIVATE).

A second, additional product search backed by Eagle's visual search (captions
of product photos + embeddings, fused ranking, served from a cache). It
understands descriptions — colour, style, what is in the picture — where the
Doofinder keyword search matches any single word. It does NOT replace or touch
kapruka_search_products: that tool still goes MCP -> commerce_phase1.jsp ->
Doofinder, and nothing here is on that path. If Eagle is down, only this tool
fails, after a short timeout.

Private: hidden from tools/list, not in the well-known manifest, landing page,
llms.txt or the MCP registry, and callable only from VISUAL_SEARCH_TRUSTED_IPS
(default: the trusted tier, i.e. the eagle box running Kapruka's sales agent).

Upstream contract: eagle-dashboard/docs/visual-search-cache.md. Eagle itself
only accepts calls from this MCP host's IP plus an X-API-Key, so the key never
leaves the server. Prices come back in LKR only; we convert with Kapruka's own
rate (src/fx.py) so they match what the checkout charges.
"""

from __future__ import annotations

import json
import logging
import math
from typing import Optional

import httpx
from mcp.server.fastmcp import Context
from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.config.settings import settings
from src.fx import lkr_per_unit
from src.private_access import is_trusted_caller
from src.server import mcp
from src.tools.products import SUPPORTED_CURRENCIES, _validate_query

logger = logging.getLogger(__name__)

_ACCESS_DENIED = (
    "Error: kapruka_visual_search is available to Kapruka's first-party sales "
    "agent only (private MCP). This caller's IP is not on the trusted list."
)
_FALL_BACK = "Use kapruka_search_products for this request instead."

# Tests swap in an httpx.MockTransport here; production uses the default.
_TRANSPORT: Optional[httpx.AsyncBaseTransport] = None


class VisualSearchInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    q: str = Field(
        ...,
        description=(
            "What the customer wants, in their words — descriptions work here "
            "(e.g. 'red roses in a heart box', 'unicorn cake for a 5 year old', "
            "'mobility walker with wheels'). Min 3 characters."
        ),
        min_length=3,
        max_length=200,
    )
    category: Optional[str] = Field(
        default=None,
        description=(
            "Narrow to a category name listed in `categories` of a previous "
            "kapruka_visual_search answer for the same words (e.g. 'Fresh Flowers', "
            "'Kapruka Cakes'). A name that isn't one of them is dropped and the valid "
            "names are returned."
        ),
        max_length=100,
    )
    limit: int = Field(default=10, ge=1, le=50, description="Results per page (1–50).")
    page: int = Field(default=1, ge=1, le=10, description="Page number (1–10).")
    currency: str = Field(
        default="LKR",
        description=f"Currency for prices AND for min/max_price. Supported: {', '.join(SUPPORTED_CURRENCIES)}",
    )
    min_price: Optional[float] = Field(default=None, ge=0, description="Minimum price, in `currency`.")
    max_price: Optional[float] = Field(default=None, ge=0, description="Maximum price, in `currency`.")
    sort: str = Field(default="relevance", description="'relevance' (default), 'price_asc' or 'price_desc'.")
    include_adult: bool = Field(default=False, description="Include adult products (off by default).")
    response_format: str = Field(default="markdown", description="'markdown' (default) or 'json'.")

    @field_validator("q")
    @classmethod
    def validate_q(cls, v: str) -> str:
        return _validate_query(v)

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, v: str) -> str:
        v = v.upper()
        if v not in SUPPORTED_CURRENCIES:
            raise ValueError(f"Currency must be one of: {', '.join(SUPPORTED_CURRENCIES)}")
        return v

    @field_validator("sort")
    @classmethod
    def validate_sort(cls, v: str) -> str:
        if v not in ("relevance", "price_asc", "price_desc"):
            raise ValueError("sort must be 'relevance', 'price_asc' or 'price_desc'")
        return v

    @field_validator("response_format")
    @classmethod
    def validate_format(cls, v: str) -> str:
        if v not in ("markdown", "json"):
            raise ValueError("response_format must be 'markdown' or 'json'")
        return v


class _Upstream(Exception):
    """An Eagle failure, already phrased for the agent."""


async def _eagle_search(params: dict) -> dict:
    try:
        async with httpx.AsyncClient(
            timeout=settings.eagle_vs_timeout,
            follow_redirects=False,  # a redirect means Cloudflare Access, not an answer
            transport=_TRANSPORT,
        ) as client:
            resp = await client.get(
                settings.eagle_vs_url,
                params={k: v for k, v in params.items() if v is not None},
                headers={
                    "X-API-Key": settings.eagle_vs_api_key,
                    "Accept": "application/json",
                    "User-Agent": "kapruka-mcp/1.0 (+https://mcp.kapruka.com)",
                },
            )
    except httpx.TimeoutException:
        raise _Upstream(f"Error (visual_search_unavailable): visual search did not answer in "
                        f"{settings.eagle_vs_timeout:g}s. {_FALL_BACK}")
    except httpx.HTTPError as e:
        raise _Upstream(f"Error (visual_search_unavailable): could not reach visual search "
                        f"({type(e).__name__}). {_FALL_BACK}")

    code = resp.status_code
    if code == 200:
        try:
            data = resp.json()
        except Exception:
            data = None
        if isinstance(data, dict) and data.get("ok", True) is not False:
            return data
        raise _Upstream(f"Error (visual_search_failed): unreadable answer. {_FALL_BACK}")
    if code == 401:
        logger.warning("visual_search: Eagle rejected the API key (401)")
        raise _Upstream(f"Error (visual_search_unauthorized): the MCP's visual-search key was "
                        f"rejected. {_FALL_BACK}")
    if 300 <= code < 400 or code == 403:
        logger.warning("visual_search: blocked by Eagle's edge (HTTP %s)", code)
        raise _Upstream(f"Error (visual_search_blocked): this MCP host is not allowed through to "
                        f"visual search (HTTP {code}). {_FALL_BACK}")
    if code == 429:
        wait = resp.headers.get("retry-after", "a few")
        raise _Upstream(f"Error (visual_search_rate_limited): wait {wait} seconds before the next "
                        f"visual search. {_FALL_BACK}")
    raise _Upstream(f"Error (visual_search_failed): visual search failed (HTTP {code}). {_FALL_BACK}")


def _norm(s: str) -> str:
    return " ".join(s.split()).casefold()


def _money(amount_lkr, rate: float) -> Optional[float]:
    if amount_lkr is None:
        return None
    try:
        v = float(amount_lkr)
    except (TypeError, ValueError):
        return None
    return round(v, 0) if rate == 1.0 else round(v / rate, 2)


def _fmt(amount: Optional[float], currency: str) -> str:
    if amount is None:
        return "Price unavailable"
    return f"LKR {amount:,.0f}" if currency == "LKR" else f"{currency} {amount:,.2f}"


def _row(r: dict, rate: float, currency: str) -> dict:
    best = _money(r.get("best_price_lkr"), rate)
    list_price = _money(r.get("price_lkr"), rate)
    on_sale = r.get("sale_price_lkr") is not None and list_price is not None and best is not None \
        and list_price > best
    out = {
        "id": r.get("id"),
        "name": r.get("title"),
        "url": r.get("url"),
        "image_url": r.get("image"),
        "price": {"amount": best, "currency": currency},
        "compare_at_price": {"amount": list_price, "currency": currency} if on_sale else None,
        "brand": r.get("brand"),
        "categories": r.get("categories") or [],
        "summary": r.get("summary"),
        "has_variants": bool(r.get("has_variants")),
    }
    if r.get("stands_in_for"):
        out["stands_in_for"] = r["stands_in_for"]
    return out


@mcp.tool(
    name="kapruka_visual_search",
    annotations={
        "title": "Kapruka Visual Search (private)",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def kapruka_visual_search(params: VisualSearchInput, ctx: Context) -> str:
    """Search Kapruka products by what they LOOK like and what they ARE — a second search,
    in addition to kapruka_search_products.

    Ranks the catalogue on descriptions of each product's photo plus its text, so it
    understands phrases rather than single words: 'red roses in a heart-shaped box',
    'chocolate cake with gold decorations', 'mobility walker with wheels'. The keyword
    search (kapruka_search_products) matches ANY one word of the query, so it often
    returns look-alike names (Johnnie Walker for 'walker'); this one usually doesn't.

    When to use it:
      - the customer describes how something looks, who it is for, or the occasion;
      - kapruka_search_products returned products whose names don't contain what
        the customer asked for;
      - as a second opinion before telling a customer "we don't have that".
    Keep using kapruka_search_products for exact product names and codes.

    Results are real catalogue products: pass `id` to kapruka_get_product,
    kapruka_check_delivery and kapruka_create_order as usual. They carry NO stock or
    delivery-scope information — confirm with kapruka_get_product before promising.
    `has_variants: true` means sizes/options exist: check kapruka_get_product before
    ordering. `stands_in_for` marks a near-identical product shown in place of a
    sold-out one.

    Prices are converted from LKR at Kapruka's own rate, so they match the site;
    min_price/max_price are in the requested `currency` too.

    Args:
        params (VisualSearchInput): q, category, limit (1–50), page (1–10), currency,
            min_price, max_price, sort ('relevance'|'price_asc'|'price_desc'),
            include_adult (default false), response_format.

    Returns:
        str. JSON schema:
        {
          "results": [{"id", "name", "url", "image_url",
                       "price": {"amount", "currency"},
                       "compare_at_price": {"amount", "currency"} | null,   # set when on sale
                       "brand", "categories": [str], "summary": str,
                       "has_variants": bool, "stands_in_for"?: str}],
          "total": int, "page": int, "limit": int, "next_page": int | null,
          "categories": [{"name": str, "count": int}],      # narrow with `category`
          "applied": {"q", "normalized_query", "category", "currency",
                      "min_price", "max_price", "sort"},
          "category_filter_dropped"?: str, "valid_categories"?: [str],
          "fx_lkr_per_unit"?: float,                         # when currency != LKR
          "cache": {"outcome": "l1|l2|stale|miss", "ms": float, "degraded": bool}
        }

        Errors ("Error (<code>): ..."): visual_search_unavailable / _failed /
        _rate_limited / _unauthorized / _blocked — in every case fall back to
        kapruka_search_products.
    """
    if not is_trusted_caller(ctx, settings.visual_search_trusted_ips, "visual_search"):
        return _ACCESS_DENIED
    if not (settings.eagle_vs_url and settings.eagle_vs_api_key):
        return f"Error (visual_search_unconfigured): visual search is not set up on this server. {_FALL_BACK}"

    currency = params.currency
    rate = 1.0
    fx_note = None
    if currency != "LKR":
        r = await lkr_per_unit(currency)
        if r:
            rate = r
        else:
            # Can't convert: answer in rupees rather than guess, and say so.
            currency = "LKR"
            fx_note = (f"Could not read Kapruka's {params.currency} rate, so prices are in LKR"
                       + (" and the price range was not applied." if
                          params.min_price is not None or params.max_price is not None else "."))

    bounds_apply = currency == params.currency
    min_lkr = math.floor(params.min_price * rate) if bounds_apply and params.min_price is not None else None
    max_lkr = math.ceil(params.max_price * rate) if bounds_apply and params.max_price is not None else None

    def _query(category: Optional[str]) -> dict:
        return {
            "q": params.q,
            "limit": params.limit,
            "page": params.page,
            "category": category,
            "min_price": min_lkr,
            "max_price": max_lkr,
            "sort": params.sort if params.sort != "relevance" else None,
            "include_adult": 1 if params.include_adult else None,
        }

    dropped: Optional[str] = None
    try:
        data = await _eagle_search(_query(params.category))
        # Eagle answers an unknown category with 200 and zero results, and lists
        # the query's real categories alongside. Recover like the keyword search:
        # fix a case/spacing slip, otherwise drop the filter and say so.
        if params.category and not data.get("results"):
            names = [c.get("name") for c in data.get("categories") or [] if isinstance(c, dict) and c.get("name")]
            canonical = next((n for n in names if _norm(n) == _norm(params.category)), None)
            if canonical is None:
                dropped = params.category
                data = await _eagle_search(_query(None))
            elif canonical != params.category:
                data = await _eagle_search(_query(canonical))
    except _Upstream as e:
        return str(e)

    rows = [_row(r, rate, currency) for r in data.get("results") or [] if isinstance(r, dict)]
    categories = [
        {"name": c.get("name"), "count": c.get("count")}
        for c in data.get("categories") or [] if isinstance(c, dict) and c.get("name")
    ]
    valid = [c["name"] for c in categories]
    total = int(data.get("total") or 0)
    page, limit = int(data.get("page") or params.page), int(data.get("limit") or params.limit)
    next_page = page + 1 if page * limit < total and page < 10 else None
    applied_category = None if dropped else (data.get("category") or params.category)

    if not rows:
        where = f" in category '{applied_category}'" if applied_category else ""
        msg = f"No products found for '{params.q}'{where} in visual search."
        if dropped and valid:
            msg += f" ('{dropped}' is not a category here; valid: {', '.join(valid[:12])}.)"
        if fx_note:
            msg += f" {fx_note}"
        return msg

    if params.response_format == "json":
        cache = data.get("cache") or {}
        return json.dumps(
            {
                "results": rows,
                "total": total,
                "page": page,
                "limit": limit,
                "next_page": next_page,
                "categories": categories,
                "applied": {
                    "q": params.q,
                    "normalized_query": data.get("normalized_query"),
                    "category": applied_category,
                    "currency": currency,
                    "min_price": params.min_price if bounds_apply else None,
                    "max_price": params.max_price if bounds_apply else None,
                    "sort": params.sort,
                },
                **({"category_filter_dropped": dropped, "valid_categories": valid} if dropped else {}),
                **({"fx_lkr_per_unit": round(rate, 4)} if rate != 1.0 else {}),
                **({"note": fx_note} if fx_note else {}),
                "cache": {k: cache.get(k) for k in ("outcome", "ms", "degraded")},
            },
            indent=2,
            ensure_ascii=False,
        )

    lines = [
        f"## Kapruka visual search: \"{params.q}\"" + (f" in **{applied_category}**" if applied_category else ""),
        f"Showing {len(rows)} of {total} ({currency})",
    ]
    if dropped:
        lines.append(
            f"_'{dropped}' is not a category for this search, so it was dropped. "
            f"Categories here: {', '.join(valid[:12])}. Do not re-send '{dropped}'._"
        )
    if fx_note:
        lines.append(f"_{fx_note}_")
    lines.append("")
    for i, r in enumerate(rows, 1 + (page - 1) * limit):
        price = _fmt(r["price"]["amount"], currency)
        if r["compare_at_price"]:
            price += f" (was {_fmt(r['compare_at_price']['amount'], currency)})"
        extras = []
        if r["has_variants"]:
            extras.append("has size/option variants — check kapruka_get_product before ordering")
        if r.get("stands_in_for"):
            extras.append(f"similar item shown in place of sold-out `{r['stands_in_for']}`")
        lines.append(f"**{i}. {r['name']}**")
        lines.append(f"   ID: `{r['id']}` · {price}" + (f" · {'; '.join(extras)}" if extras else ""))
        if r.get("summary"):
            s = r["summary"].strip()
            lines.append(f"   _{s[:160]}{'…' if len(s) > 160 else ''}_")
        if r.get("url"):
            lines.append(f"   [View product]({r['url']})")
        lines.append("")
    if categories and not applied_category:
        lines.append("_Narrow with `category`: " + ", ".join(
            f"{c['name']} ({c['count']})" for c in categories[:8]) + "_")
    if next_page:
        lines.append(f"*More results: pass `page={next_page}`.*")
    lines.append("_No stock or delivery info here — confirm with kapruka_get_product before promising._")
    return "\n".join(lines)


# ── Private: callable, but omitted from tools/list (same mechanism as Phase 2).
from src.tools.customers import _HIDDEN_TOOLS  # noqa: E402

_HIDDEN_TOOLS.add("kapruka_visual_search")
