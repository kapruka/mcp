"""MCP tools: kapruka_get_product, kapruka_search_products."""

import base64
import json
import logging
import re
import time
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.api.client import KaprukaClient, handle_api_error
from src.delivery_scope import describe_delivery
from src.server import mcp

logger = logging.getLogger(__name__)

# ── Shared helpers ────────────────────────────────────────────────────────────

SUPPORTED_CURRENCIES = ["LKR", "USD", "GBP", "AUD", "CAD", "EUR"]

# ── Anti-scrape constraints ──────────────────────────────────────────────────
# Hard cap on how many cursor-paginated pages a single query can traverse.
# Anyone wanting to walk further is enumerating the catalog, not searching.
_MAX_SEARCH_PAGES = 3

# Common English stopwords + low-signal single-letter variants. A query made up
# entirely of these is treated as a wildcard scrape attempt.
_STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "of", "to", "in", "on", "at", "by", "for", "with", "from", "into",
    "and", "or", "but", "if", "as", "it", "its", "this", "that", "these", "those",
    "all", "any", "some", "no", "not", "yes",
}
_TOKEN_RE = re.compile(r"[a-zA-Z\u0D80-\u0DFF]+")


def _validate_query(q: str) -> str:
    """Reject low-entropy queries that look like enumeration probes."""
    q = q.strip()
    if len(q) < 3:
        raise ValueError("Query must be at least 3 characters")
    if not re.search(r"[a-zA-Z0-9\u0D80-\u0DFF]", q):
        raise ValueError("Query must contain alphanumeric characters")
    tokens = [t.lower() for t in _TOKEN_RE.findall(q)]
    if tokens and all(t in _STOPWORDS for t in tokens):
        raise ValueError("Query too generic — provide more specific search terms")
    return q


def _wrap_cursor(upstream: str | None, page: int) -> str | None:
    """Wrap upstream cursor with a page counter so we can enforce depth caps."""
    if not upstream or page >= _MAX_SEARCH_PAGES:
        return None
    payload = json.dumps({"u": upstream, "p": page + 1}, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def _unwrap_cursor(wrapped: str | None) -> tuple[str | None, int]:
    """Return (upstream_cursor, page_number). Page 1 = first request."""
    if not wrapped:
        return None, 1
    try:
        padded = wrapped + "=" * (-len(wrapped) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
        page = max(1, min(int(payload.get("p", 1)), _MAX_SEARCH_PAGES))
        return payload.get("u"), page
    except Exception:
        # Treat malformed cursors as a fresh first-page request rather than erroring.
        return None, 1


def _fmt_price(price: dict | None) -> str:
    if not price or price.get("amount") is None:
        return "Price unavailable"
    amount = price["amount"]
    currency = price.get("currency", "LKR")
    if currency == "LKR":
        return f"LKR {amount:,.0f}"
    return f"{currency} {amount:,.2f}"


def _stock_label(item: dict) -> str:
    if not item.get("in_stock"):
        return "Out of stock"
    level = item.get("stock_level", "")
    return {"low": "In stock (low)", "medium": "In stock", "high": "In stock (high)"}.get(
        level, "In stock"
    )


def _is_category_stub(product_id: str) -> bool:
    """CATSYM products are category landing pages, not real products."""
    return product_id.upper().startswith("CATSYM")


# Matches both single-slash ("Http:/Www.") and normal ("https://www.") URL forms.
_CATSYM_URL_RE = re.compile(r'https?://?\S+', re.IGNORECASE)


def _extract_catsym_url(text: str) -> str | None:
    """Extract and normalise a kapruka.com URL from a CATSYM summary or description.

    Handles two formats produced by the API:
    - Single-slash:  'Https:/Www.kapruka.com/Online/Cakes'  (search summary)
    - Normal:        'https://www.kapruka.com/online/cakes'  (product description)
    Both are lowercased and returned with proper https://.
    """
    m = _CATSYM_URL_RE.search(text)
    if not m:
        return None
    url = m.group(0).lower()
    # Fix single-slash protocol: "https:/www." → "https://www."
    url = re.sub(r'^(https?):/([^/])', r'\1://\2', url)
    return url


# ── Tool 1: kapruka_get_product ───────────────────────────────────────────────


class GetProductInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    product_id: str = Field(
        ...,
        description="Kapruka product ID (e.g. 'cakeXX000000', 'EF_PC_CHOC0V2774P00065')",
        min_length=3,
        max_length=80,
    )
    currency: str = Field(
        default="LKR",
        description=f"Price currency. Supported: {', '.join(SUPPORTED_CURRENCIES)}",
    )
    type: Optional[str] = Field(
        default=None,
        description="Optional product type hint passed to the API (e.g. 'specialgifts'). Rarely needed.",
    )
    response_format: str = Field(
        default="markdown",
        description="'markdown' for human-readable output, 'json' for raw structured data.",
    )

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, v: str) -> str:
        v = v.upper()
        if v not in SUPPORTED_CURRENCIES:
            raise ValueError(f"Currency must be one of: {', '.join(SUPPORTED_CURRENCIES)}")
        return v

    @field_validator("response_format")
    @classmethod
    def validate_format(cls, v: str) -> str:
        if v not in ("markdown", "json"):
            raise ValueError("response_format must be 'markdown' or 'json'")
        return v


@mcp.tool(
    name="kapruka_get_product",
    annotations={
        "title": "Get Kapruka Product Details",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def kapruka_get_product(params: GetProductInput) -> str:
    """Fetch full details for a single Kapruka product by its product ID.

    Returns name, description, price (with optional currency conversion), stock status,
    images, variants, shipping info, delivery scope, and a direct product URL.

    Delivery scope: most gifts ship island-wide, but restaurant food, hotel cakes
    and liquor only reach a limited city set (typically the Colombo area). The
    `delivery` object is the authority — search results do NOT carry it. When
    `delivery.island_wide` is false, tell the customer up front that the item is
    delivered only to selected cities, and confirm their city with
    kapruka_check_delivery(city, product_id) before promising anything. If
    `deliverable_city_count` exceeds the returned list, the list is truncated —
    say "and more", don't treat it as complete.

    Note: Some IDs starting with 'CATSYM' are category landing pages, not purchasable
    products — this tool will flag those clearly.

    Args:
        params (GetProductInput):
            - product_id (str): Kapruka product ID (e.g. 'cakeXX000000')
            - currency (str): Price currency — LKR (default), USD, GBP, AUD, CAD, EUR
            - type (Optional[str]): Optional type hint (e.g. 'specialgifts')
            - response_format (str): 'markdown' (default) or 'json'

    Returns:
        str: Product details in the requested format.

        JSON schema:
        {
          "id": str,
          "name": str,
          "description": str,
          "summary": str,
          "price": {"amount": float, "currency": str},
          "compare_at_price": {"amount": float, "currency": str} | null,
          "in_stock": bool,
          "stock_level": str,           # "low" | "medium" | "high"
          "category": {"id": str, "name": str, "slug": str, "path": str},
          "variants": [{"id": str, "name": str, "sku": str, "price": {...},
                        "in_stock": bool, "stock_level": str, "attributes": {...}}],
          "images": [str],              # list of full-resolution image URLs
          "attributes": {"type": str, "subtype": str, "weight": str, "vendor": str},
          "shipping": {"ships_from": str, "ships_internationally": bool, "restricted_countries": [str]},
          "delivery": {
            "island_wide": bool,
            "deliverable_city_count": int,   # only when island_wide=false; TRUE total
            "deliverable_cities": [str]      # only when island_wide=false; capped at 60
          },
          "rating": null,
          "url": str
        }

        Error: "Error: <message>" on failure.
    """
    if _is_category_stub(params.product_id):
        # Fetch so we can extract the landing URL from the summary
        try:
            client = KaprukaClient()
            data = await client.call("product", product_id=params.product_id, currency="LKR")
            landing = _extract_catsym_url(data.get("description", "") or data.get("summary", ""))
            url = landing or data.get("url", "")
        except Exception:
            url = ""
        msg = f"'{params.product_id}' is a category browse page, not a purchasable product."
        if url:
            msg += f" Browse the category here: {url}"
        msg += " Use kapruka_search_products to find real products in this category."
        return msg

    try:
        client = KaprukaClient()
        data = await client.call(
            "product",
            product_id=params.product_id,
            currency=params.currency,
            type=params.type,
        )
    except Exception as e:
        return handle_api_error(e)

    if params.response_format == "json":
        return json.dumps(data, indent=2, ensure_ascii=False)

    # ── Markdown format
    lines: list[str] = []
    lines.append(f"## {data.get('name', 'Unknown Product')}")
    lines.append(f"**ID**: `{data.get('id')}`")
    lines.append(f"**Price**: {_fmt_price(data.get('price'))}")

    compare = data.get("compare_at_price")
    if compare and compare.get("amount"):
        lines.append(f"**Was**: ~~{_fmt_price(compare)}~~")

    lines.append(f"**Stock**: {_stock_label(data)}")

    cat = data.get("category", {})
    if cat.get("name"):
        lines.append(f"**Category**: {cat['name']}")

    attrs = data.get("attributes", {})
    if attrs.get("vendor"):
        lines.append(f"**Vendor**: {attrs['vendor']}")
    if attrs.get("weight"):
        lines.append(f"**Weight**: {attrs['weight']} lbs")

    shipping = data.get("shipping", {})
    intl = "Yes" if shipping.get("ships_internationally") else "No"
    lines.append(f"**International shipping**: {intl}")

    delivery_line = describe_delivery(data.get("delivery"))
    if delivery_line:
        lines.append(f"**Delivery**: {delivery_line}")

    lines.append("")
    summary = data.get("summary") or data.get("description", "")
    if summary:
        lines.append(summary[:400].strip())

    variants = data.get("variants", [])
    if len(variants) > 1:
        lines.append("")
        lines.append("**Variants:**")
        for v in variants:
            lines.append(
                f"- {v.get('name')} — {_fmt_price(v.get('price'))} — {_stock_label(v)}"
            )

    images = data.get("images", [])
    if images:
        lines.append("")
        lines.append(f"**Image**: {images[0]}")

    url = data.get("url")
    if url:
        lines.append("")
        lines.append(f"[View on Kapruka]({url})")

    return "\n".join(lines)


# ── Tool 2: kapruka_search_products ──────────────────────────────────────────


class SearchProductsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    q: str = Field(
        ...,
        description="Search query (e.g. 'birthday cake', 'roses', 'chocolates for mom'). Min 3 characters, must contain specific terms (not stopwords only).",
        min_length=3,
        max_length=200,
    )
    category: Optional[str] = Field(
        default=None,
        description=(
            "Filter by SUBCATEGORY FACET NAME — the same value the website's search uses in "
            "its `subcat=` parameter and shows in its left sidebar, e.g. 'Kapruka Cakes', "
            "'Fresh Flowers', 'Birthday', 'Greeting Cards', 'Cake And Flower', "
            "'Home And Lifestyle', 'Grocery Items'. These are narrower than a department "
            "and the valid set DEPENDS ON THE QUERY. Department words ('Cakes', 'Flowers', "
            "'Toys') and most values from kapruka_list_categories are NOT valid here and "
            "return nothing. When the filter matches nothing this tool retries without it "
            "and says so, so a wrong value costs relevance, not results. If unsure, leave "
            "it unset and put the words in `q`."
        ),
    )
    limit: int = Field(
        default=10,
        description="Number of results to return (1–50).",
        ge=1,
        le=50,
    )
    cursor: Optional[str] = Field(
        default=None,
        description="Pagination cursor from a previous search response's 'next_cursor' field.",
    )
    currency: str = Field(
        default="LKR",
        description=f"Price currency. Supported: {', '.join(SUPPORTED_CURRENCIES)}",
    )
    min_price: Optional[float] = Field(
        default=None,
        description="Minimum price (inclusive), in the requested currency.",
        ge=0,
    )
    max_price: Optional[float] = Field(
        default=None,
        description="Maximum price (inclusive), in the requested currency.",
        ge=0,
    )
    in_stock_only: bool = Field(
        default=False,
        description="If true, only return products currently in stock.",
    )
    sort: str = Field(
        default="relevance",
        description=(
            "Sort order: 'relevance' (default), 'price_asc', 'price_desc', "
            "'newest', 'bestseller'."
        ),
    )
    include_stubs: bool = Field(
        default=False,
        description="If false (default), category landing pages (CATSYM entries, price=0) are filtered out.",
    )
    response_format: str = Field(
        default="markdown",
        description="'markdown' for human-readable output, 'json' for raw structured data.",
    )

    @field_validator("sort")
    @classmethod
    def validate_sort(cls, v: str) -> str:
        allowed = {"relevance", "price_asc", "price_desc", "newest", "bestseller"}
        if v not in allowed:
            raise ValueError(f"sort must be one of: {', '.join(sorted(allowed))}")
        return v

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

    @field_validator("response_format")
    @classmethod
    def validate_format(cls, v: str) -> str:
        if v not in ("markdown", "json"):
            raise ValueError("response_format must be 'markdown' or 'json'")
        return v


# ── price bounds are evaluated in LKR upstream ───────────────────────────────
# `products_search` converts the PRICES it returns into the requested currency
# but compares `min_price`/`max_price` against the raw LKR figures. Proven
# 2026-09-21: q="cake" currency=USD max_price=32 -> "No products found", while
# max_price=9600 (= USD 32 at ~300/USD) returns cakes priced USD 13.15-31.48,
# and max_price=100 returns 30-cent greeting cards (LKR 90). Overseas customers
# are ~62% of this agent's traffic, so every dollar budget they gave was tested
# against rupee numbers: "a cake under $30" answered "we have none".
# Until the API compares in the caller's currency, convert here.
_RATE_TTL_S = 6 * 3600
# A currency with no Kapruka price list (CAD, measured 2026-09-21) fails every
# anchor, which is 8 upstream calls. Without remembering that, each bounded
# search in that currency pays them again — and this API throttles on novel
# queries PER SESSION, so the retries are what trips the throttle. Remember
# the failure too, briefly, so a price list added later is still picked up.
_RATE_FAIL_TTL_S = 15 * 60
_RATE_CACHE: dict[str, tuple[float, float]] = {}
# Any in-catalogue product works — the ratio is the price list's, not the item's.
_RATE_ANCHORS = ("CAKE00KA002192", "CAKE00KA001423", "FLOWERS00T2075", "CAKE00KA001535")


async def _lkr_per_unit(client, currency: str) -> float | None:
    """Rupees per 1 unit of `currency`, from Kapruka's own price list. None when
    it cannot be read — the caller then filters locally instead of guessing."""
    cur = (currency or "LKR").upper()
    if cur == "LKR":
        return 1.0
    hit = _RATE_CACHE.get(cur)
    if hit:
        rate, at = hit
        ttl = _RATE_TTL_S if rate else _RATE_FAIL_TTL_S
        if (time.time() - at) < ttl:
            return rate or None
    for pid in _RATE_ANCHORS:
        try:
            in_lkr = await client.call("product", product_id=pid, currency="LKR")
            in_cur = await client.call("product", product_id=pid, currency=cur)
            a = float((in_lkr.get("price") or {}).get("amount") or 0)
            b = float((in_cur.get("price") or {}).get("amount") or 0)
        except Exception:
            continue
        if a > 0 and b > 0:
            rate = a / b
            if 1.0 < rate < 100000.0:      # sanity: LKR is worth less than any peer
                _RATE_CACHE[cur] = (rate, time.time())
                logger.info("price-bound rate %s -> LKR %.2f (anchor %s)", cur, rate, pid)
                return rate
    _RATE_CACHE[cur] = (0.0, time.time())
    logger.warning("could not derive an LKR rate for %s; filtering price locally", cur)
    return None


@mcp.tool(
    name="kapruka_search_products",
    annotations={
        "title": "Search Kapruka Products",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def kapruka_search_products(params: SearchProductsInput) -> str:
    """Search for products on Kapruka.com by keyword, with optional category filter and pagination.

    Returns a ranked list of matching products with prices, stock status, images, and URLs.
    Supports cursor-based pagination — pass next_cursor from one response into the next call.
    Pagination is capped at 3 pages per query to discourage catalog enumeration; for broader
    discovery, refine the query or filter by category instead.

    Queries must be at least 3 characters and contain specific terms — pure stopword queries
    (e.g. "the", "a an") are rejected.

    By default, category landing pages (CATSYM entries with price=0) are filtered out so results
    contain only purchasable products. Set include_stubs=true to include them.

    Search results carry NO delivery-scope information. Food, hotel cakes and liquor
    are delivered only to selected cities — never infer deliverability from a search
    hit. Before quoting delivery on a specific item, call kapruka_get_product (read
    `delivery.island_wide`) or kapruka_check_delivery with `product_id`.

    Args:
        params (SearchProductsInput):
            - q (str): Search query (e.g. 'birthday cake', 'roses', 'tea gift'). Min 3 chars.
            - category (Optional[str]): Category filter (e.g. 'Birthday', 'Flowers')
            - limit (int): Results per page, 1–50 (default 10)
            - cursor (Optional[str]): Pagination cursor from previous response
            - currency (str): LKR (default), USD, GBP, AUD, CAD, EUR
            - min_price (Optional[float]): Min price (inclusive) in the requested currency
            - max_price (Optional[float]): Max price (inclusive) in the requested currency
            - in_stock_only (bool): Restrict to in-stock items (default false)
            - sort (str): 'relevance' | 'price_asc' | 'price_desc' | 'newest' | 'bestseller'
            - include_stubs (bool): Include category landing pages (default false)
            - response_format (str): 'markdown' (default) or 'json'

    Returns:
        str: Search results in the requested format.

        JSON schema:
        {
          "results": [
            {
              "id": str,
              "name": str,
              "summary": str,
              "price": {"amount": float | null, "currency": str},
              "compare_at_price": {"amount": float, "currency": str} | null,
              "in_stock": bool,
              "stock_level": str,
              "image_url": str | null,
              "category": {"id": str, "name": str, "slug": str},
              "rating": null,
              "ships_internationally": bool,
              "url": str
            }
          ],
          "next_cursor": str | null,    # null after page 3 even if upstream has more
          "applied_filters": {"q": str, "limit": int, "in_stock_only": bool}
        }

        Error: "Error: <message>" or "No products found for '<query>'" on failure.
    """
    upstream_cursor, page = _unwrap_cursor(params.cursor)

    # CATSYM stubs (price=0 category landing pages) cluster at the top of price-sorted
    # results upstream. Over-fetch when sort is numeric so post-filter still gives the
    # caller a full page of real products. Backend team chose to leave stub filtering
    # to us rather than change the API.
    upstream_limit = params.limit
    needs_overfetch = (
        not params.include_stubs
        and params.sort in ("price_asc", "price_desc", "bestseller")
    )
    if needs_overfetch:
        upstream_limit = min(params.limit * 3, 50)

    client = KaprukaClient()

    # Price bounds: upstream compares them in LKR (see _lkr_per_unit). Convert,
    # and if the rate cannot be read, send no bounds and filter locally instead
    # — a bound the server would misread is worse than one we apply ourselves.
    min_p, max_p = params.min_price, params.max_price
    up_min, up_max = min_p, max_p
    price_rate = 1.0
    price_local_only = False
    if (min_p is not None or max_p is not None) and (params.currency or "LKR").upper() != "LKR":
        rate = await _lkr_per_unit(client, params.currency)
        if rate:
            price_rate = rate
            up_min = min_p * rate if min_p is not None else None
            up_max = max_p * rate if max_p is not None else None
        else:
            up_min = up_max = None
            price_local_only = True
        # Locally-bounded searches need headroom, since upstream is unfiltered.
        if price_local_only:
            upstream_limit = min(max(upstream_limit, params.limit) * 4, 50)

    try:
        data = await client.call(
            "products_search",
            q=params.q,
            limit=upstream_limit,
            category=params.category,
            cursor=upstream_cursor,
            currency=params.currency,
            min_price=up_min,
            max_price=up_max,
            in_stock_only="true" if params.in_stock_only else None,
            sort=params.sort if params.sort != "relevance" else None,
        )
    except Exception as e:
        return handle_api_error(e)

    def _in_budget(row: dict) -> bool:
        """The returned price is already in the caller's currency, so this is the
        authoritative check — it also catches rounding in the converted bound."""
        amt = (row.get("price") or {}).get("amount")
        if amt is None:
            return True
        try:
            amt = float(amt)
        except (TypeError, ValueError):
            return True
        if min_p is not None and amt < float(min_p) - 0.005:
            return False
        if max_p is not None and amt > float(max_p) + 0.005:
            return False
        return True

    def _usable(payload: dict) -> list[dict]:
        rows: list[dict] = payload.get("results", [])
        if not params.include_stubs:
            rows = [r for r in rows if not _is_category_stub(r.get("id", ""))]
        if min_p is not None or max_p is not None:
            rows = [r for r in rows if _in_budget(r)]
        return rows[: params.limit]

    results = _usable(data)

    # CATEGORY FALLBACK (2026-09-20, diagnosis corrected 2026-09-21).
    # `category` is the website's SUBCATEGORY FACET (`subcat=` in
    # srilanka_online_search.jsp), not a department: 'Kapruka Cakes' and
    # 'Fresh Flowers' work and reproduce the site's own result list exactly,
    # while 'Cakes'/'Flowers' — until 2026-09-21 two of the three examples in
    # this tool's parameter description — match nothing. The valid set depends
    # on the query, and nothing in the API advertises it: the search response
    # carries no facet list (upstream returns only results, next_cursor,
    # total_estimate, applied_filters), and kapruka_list_categories returns a
    # different vocabulary whose 'cakes'/'flowers' entries are not searchable.
    # Measured over 5 days of live agent traffic: 92 searches carried a
    # category and 75 came back empty (Cakes 40/40, Flowers 23/23), every one a
    # customer asking for a cake or flowers. Retry once without the filter so a
    # wrong facet name costs relevance, never the whole result set.
    category_dropped = False
    if not results and params.category:
        try:
            data = await client.call(
                "products_search",
                q=params.q,
                limit=upstream_limit,
                cursor=upstream_cursor,
                currency=params.currency,
                min_price=up_min,
                max_price=up_max,
                in_stock_only="true" if params.in_stock_only else None,
                sort=params.sort if params.sort != "relevance" else None,
            )
        except Exception as e:
            return handle_api_error(e)
        results = _usable(data)
        category_dropped = bool(results)

    if not results:
        suffix = f" in category '{params.category}'" if params.category else ""
        return f"No products found for '{params.q}'{suffix}."

    next_cursor = _wrap_cursor(data.get("next_cursor"), page)

    if params.response_format == "json":
        return json.dumps(
            {
                "results": results,
                "next_cursor": next_cursor,
                "applied_filters": data.get("applied_filters", {}),
                **({"category_filter_dropped": params.category} if category_dropped else {}),
                **({"price_bounds_converted_to_lkr": round(price_rate, 4)}
                   if price_rate != 1.0 else {}),
                **({"price_filtered_locally": True} if price_local_only else {}),
            },
            indent=2,
            ensure_ascii=False,
        )

    # ── Markdown format
    cat_label = "" if category_dropped else (f" in **{params.category}**" if params.category else "")
    lines: list[str] = [
        f"## Kapruka search: \"{params.q}\"{cat_label}",
        f"Showing {len(results)} results ({params.currency})",
    ]
    if category_dropped:
        lines.append(
            f"_Nothing matched in category '{params.category}', so the category filter was "
            f"dropped and these are results for \"{params.q}\" across the catalogue. "
            f"Do not re-send that category._"
        )
    lines.append("")

    for i, r in enumerate(results, 1):
        rid = r.get("id", "")
        if _is_category_stub(rid):
            landing = _extract_catsym_url(r.get("summary", "")) or r.get("url", "")
            lines.append(f"**{i}. {r.get('name')}** _(browse page)_")
            if landing:
                lines.append(f"   [Browse category]({landing})")
        else:
            price_str = _fmt_price(r.get("price"))
            stock_str = _stock_label(r)
            intl = " · ships internationally" if r.get("ships_internationally") else ""
            lines.append(f"**{i}. {r.get('name')}**")
            lines.append(f"   ID: `{rid}` · {price_str} · {stock_str}{intl}")
            url = r.get("url")
            if url:
                lines.append(f"   [View product]({url})")
        lines.append("")

    if next_cursor:
        lines.append(f"*More results available. Pass `cursor=\"{next_cursor}\"` for the next page.*")

    return "\n".join(lines)
