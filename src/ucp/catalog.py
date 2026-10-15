"""UCP Catalog: search_catalog, lookup_catalog, get_product.

Backed by the same commerce API as the Kapruka MCP tools (`products_search`,
`product`). Prices are what checkout charges (website discounts applied);
`list_price` carries the pre-discount price when an item is on sale.

Identifiers: a Kapruka product ID, or a variant ID (`<product>_TC<n>`). A
product with a single `_default` variant exposes that variant under the
product ID, which is what checkout takes.
"""

from __future__ import annotations

import asyncio
import re
from typing import Optional

from src.api.client import KaprukaClient
from src.tools.products import SUPPORTED_CURRENCIES, _unwrap_cursor, _validate_query, _wrap_cursor
from src.ucp import STOREFRONT
from src.ucp.money import to_major, to_minor
from src.ucp.negotiation import UcpError

DEFAULT_LIMIT = 10
MAX_LIMIT = 50
MAX_LOOKUP_IDS = 20
_SELLER = {"name": "Kapruka"}
# Variant attributes that are not buyer-facing options.
_NOT_OPTIONS = {"weight"}


def presentment_currency(context: Optional[dict]) -> str:
    cur = str((context or {}).get("currency") or "").upper()
    return cur if cur in SUPPORTED_CURRENCIES else "LKR"


def _price(money: Optional[dict], currency: str) -> Optional[dict]:
    if not isinstance(money, dict) or money.get("amount") is None:
        return None
    cur = money.get("currency") or currency
    return {"amount": to_minor(money["amount"], cur), "currency": cur}


def _description(text: Optional[str], fallback: str) -> dict:
    text = (text or "").strip()
    return {"plain": text or fallback}


def _categories(category) -> list[dict]:
    if isinstance(category, dict) and category.get("name"):
        return [{"value": category["name"], "taxonomy": "merchant"}]
    return []


def _media(urls) -> list[dict]:
    if isinstance(urls, str):
        urls = [urls]
    return [{"type": "image", "url": u} for u in (urls or []) if isinstance(u, str) and u.startswith("http")]


def _variant_id(product_id: str, variant: dict) -> str:
    vid = variant.get("id") or product_id
    return product_id if vid.endswith("_default") else vid


def _variant_options(variant: dict) -> list[dict]:
    attrs = variant.get("attributes") or {}
    return [
        {"name": k.capitalize(), "label": str(v)}
        for k, v in attrs.items() if k not in _NOT_OPTIONS and v not in (None, "")
    ]


def _variant(product: dict, v: dict, currency: str) -> dict:
    out = {
        "id": _variant_id(product["id"], v),
        "sku": v.get("sku") or product["id"],
        "title": v.get("name") if v.get("name") and v.get("name") != "Default" else product.get("name", ""),
        "description": _description(product.get("summary") or product.get("description"), product.get("name", "")),
        "url": product.get("url"),
        "price": _price(v.get("price"), currency) or _price(product.get("price"), currency),
        "availability": {"available": bool(v.get("in_stock", product.get("in_stock")))},
        "seller": _SELLER,
    }
    if v.get("compare_at_price"):
        out["list_price"] = _price(v["compare_at_price"], currency)
    opts = _variant_options(v) if len(product.get("variants") or []) > 1 else []
    if opts:
        out["options"] = opts
    return {k: val for k, val in out.items() if val is not None}


def _range(prices: list[dict]) -> Optional[dict]:
    prices = [p for p in prices if p]
    if not prices:
        return None
    return {"min": min(prices, key=lambda p: p["amount"]), "max": max(prices, key=lambda p: p["amount"])}


def product_from_detail(d: dict, currency: str, variants: Optional[list[dict]] = None) -> dict:
    """A UCP product from the commerce API `product` answer."""
    raw = d.get("variants") or [{"id": d["id"], "price": d.get("price"),
                                 "compare_at_price": d.get("compare_at_price"), "in_stock": d.get("in_stock")}]
    product = {**d, "variants": raw}
    vs = variants if variants is not None else [_variant(product, v, currency) for v in raw]
    out = {
        "id": d["id"],
        "title": d.get("name", ""),
        "description": _description(d.get("description") or d.get("summary"), d.get("name", "")),
        "url": d.get("url"),
        "categories": _categories(d.get("category")),
        "price_range": _range([v["price"] for v in vs]) or {"min": _price(d.get("price"), currency),
                                                            "max": _price(d.get("price"), currency)},
        "media": _media(d.get("images") or d.get("image_url")),
        "variants": vs,
    }
    lists = [v.get("list_price") for v in vs if v.get("list_price")]
    if lists:
        out["list_price_range"] = _range(lists)
    options = _product_options(raw) if len(raw) > 1 else []
    if options:
        out["options"] = options
    rating = d.get("rating")
    if isinstance(rating, dict) and rating.get("value") is not None:
        out["rating"] = {"value": rating["value"], "scale_max": rating.get("scale_max", 5),
                         **({"count": rating["count"]} if rating.get("count") is not None else {})}
    return {k: v for k, v in out.items() if v not in (None, [])} | {"variants": vs}


def _product_options(raw_variants: list[dict]) -> list[dict]:
    names: dict[str, list[str]] = {}
    for v in raw_variants:
        for o in _variant_options(v):
            labels = names.setdefault(o["name"], [])
            if o["label"] not in labels:
                labels.append(o["label"])
    return [{"name": n, "values": [{"label": label} for label in labels]} for n, labels in names.items()]


def product_from_row(row: dict, currency: str) -> dict:
    """A UCP product from a `products_search` row (single featured variant)."""
    price = _price(row.get("price"), currency)
    variant = {
        "id": row["id"],
        "sku": row["id"],
        "title": row.get("name", ""),
        "description": _description(row.get("summary"), row.get("name", "")),
        "url": row.get("url"),
        "price": price,
        "availability": {"available": bool(row.get("in_stock"))},
        "seller": _SELLER,
    }
    if row.get("compare_at_price"):
        variant["list_price"] = _price(row["compare_at_price"], currency)
    variant = {k: v for k, v in variant.items() if v is not None}
    out = {
        "id": row["id"],
        "title": row.get("name", ""),
        "description": _description(row.get("summary"), row.get("name", "")),
        "url": row.get("url"),
        "categories": _categories(row.get("category")),
        "price_range": {"min": price, "max": price} if price else None,
        "media": _media(row.get("image_url")),
        "variants": [variant],
    }
    if variant.get("list_price"):
        out["list_price_range"] = {"min": variant["list_price"], "max": variant["list_price"]}
    return {k: v for k, v in out.items() if v not in (None, [])} | {"variants": [variant]}


# ── search_catalog ───────────────────────────────────────────────────────────


async def search(req: dict) -> dict:
    """Returns {"products", "pagination", "messages"?} (no `ucp`; the transport adds it)."""
    query = req.get("query")
    filters = req.get("filters") if isinstance(req.get("filters"), dict) else {}
    context = req.get("context") if isinstance(req.get("context"), dict) else {}
    if not isinstance(query, str) or not query.strip():
        raise UcpError("invalid_request",
                       "Kapruka's catalog search needs a `query` (at least 3 characters).",
                       kind="outcome", severity="recoverable", continue_url=STOREFRONT)
    try:
        q = _validate_query(query)
    except ValueError as e:
        raise UcpError("invalid_request", str(e), kind="outcome", severity="recoverable",
                       continue_url=STOREFRONT)
    currency = presentment_currency(context)
    pagination = req.get("pagination") if isinstance(req.get("pagination"), dict) else {}
    try:
        limit = max(1, min(int(pagination.get("limit") or DEFAULT_LIMIT), MAX_LIMIT))
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    upstream, page = _unwrap_cursor(pagination.get("cursor"))

    messages: list[dict] = []
    price = filters.get("price") if isinstance(filters.get("price"), dict) else {}
    min_p = max_p = None
    if price:
        if context.get("currency") and str(context["currency"]).upper() == currency:
            min_p = to_major(price["min"], currency) if isinstance(price.get("min"), int) else None
            max_p = to_major(price["max"], currency) if isinstance(price.get("max"), int) else None
        else:
            messages.append({"type": "warning", "code": "filter_ignored", "path": "$.filters.price",
                             "content": "The price filter was not applied: send context.currency "
                                        f"({', '.join(SUPPORTED_CURRENCIES)}) to denominate it."})
    cats = filters.get("categories") if isinstance(filters.get("categories"), list) else []
    category = cats[0] if cats and isinstance(cats[0], str) else None

    client = KaprukaClient()

    async def run(cat):
        return await client.call(
            "products_search", q=q, limit=limit, category=cat, cursor=upstream, currency=currency,
            min_price=min_p, max_price=max_p,
        )

    try:
        data = await run(category)
    except Exception as e:
        if category and "invalid_category" in str(getattr(getattr(e, "response", None), "text", "")):
            data = await run(None)
            messages.append({"type": "warning", "code": "filter_ignored", "path": "$.filters.categories",
                             "content": f"'{category}' is not a category for this search; it was ignored."})
        else:
            raise _upstream(e)

    rows = [r for r in data.get("results") or [] if isinstance(r, dict) and r.get("id")
            and (r.get("type") in (None, "product"))]
    products = [product_from_row(r, currency) for r in rows]
    cursor = _wrap_cursor(data.get("next_cursor"), page)
    out: dict = {
        "products": products,
        "pagination": {"has_next_page": bool(cursor), **({"cursor": cursor} if cursor else {})},
    }
    total = data.get("total_estimate")
    if isinstance(total, int) and total >= 0:
        out["pagination"]["total_count"] = total
    if messages:
        out["messages"] = messages
    return out


def _upstream(e: Exception) -> UcpError:
    return UcpError("upstream_unavailable", "Kapruka's catalog is temporarily unavailable.",
                    kind="transport", http_status=503, rpc_code=-32000, retry_after=5)


# ── lookup_catalog / get_product ─────────────────────────────────────────────

_VARIANT_SUFFIX = re.compile(r"_TC\d+$", re.IGNORECASE)


async def _fetch_product(client: KaprukaClient, ident: str, currency: str) -> Optional[dict]:
    """The commerce API answer for a product, resolving a variant ID to its parent.

    Returns {"product": <parent detail>, "variant_id": <id or None>} or None.
    """
    try:
        d = await client.call("product", product_id=ident, currency=currency)
    except Exception as e:
        if getattr(getattr(e, "response", None), "status_code", None) in (400, 404):
            return None
        raise _upstream(e)
    if _VARIANT_SUFFIX.search(ident):
        parent_id = _VARIANT_SUFFIX.sub("", ident)
        try:
            parent = await client.call("product", product_id=parent_id, currency=currency)
        except Exception:
            parent = None
        if parent and any((v.get("id") or "").upper() == ident.upper() for v in parent.get("variants") or []):
            return {"product": parent, "variant_id": next(
                v["id"] for v in parent["variants"] if v["id"].upper() == ident.upper())}
    return {"product": d, "variant_id": None}


def _price_ok(variant: dict, price_filter: dict) -> bool:
    amount = (variant.get("price") or {}).get("amount")
    if amount is None:
        return True
    lo, hi = price_filter.get("min"), price_filter.get("max")
    return (not isinstance(lo, int) or amount >= lo) and (not isinstance(hi, int) or amount <= hi)


async def lookup(req: dict) -> dict:
    ids = req.get("ids")
    if not isinstance(ids, list) or not ids or not all(isinstance(i, str) and i.strip() for i in ids):
        raise UcpError("invalid_request", "`ids` must be a non-empty array of product or variant IDs.",
                       kind="outcome", severity="recoverable")
    ids = list(dict.fromkeys(i.strip() for i in ids))
    if len(ids) > MAX_LOOKUP_IDS:
        raise UcpError("request_too_large", f"At most {MAX_LOOKUP_IDS} identifiers per lookup.",
                       kind="transport", http_status=400, rpc_code=-32602, continue_url=None)
    context = req.get("context") if isinstance(req.get("context"), dict) else {}
    filters = req.get("filters") if isinstance(req.get("filters"), dict) else {}
    price_filter = filters.get("price") if isinstance(filters.get("price"), dict) else {}
    currency = presentment_currency(context)
    client = KaprukaClient()
    found = await asyncio.gather(*(_fetch_product(client, i, currency) for i in ids))

    products: dict[str, dict] = {}       # parent id -> {"detail", "variants": {vid: inputs}}
    messages: list[dict] = []
    for ident, hit in zip(ids, found):
        if hit is None:
            messages.append({"type": "info", "code": "not_found", "content": ident})
            continue
        detail = hit["product"]
        entry = products.setdefault(detail["id"], {"detail": detail, "inputs": {}})
        raw = detail.get("variants") or [{"id": detail["id"]}]
        if hit["variant_id"]:
            entry["inputs"].setdefault(_variant_id(detail["id"], {"id": hit["variant_id"]}), []).append(
                {"id": ident, "match": "exact"})
        else:
            featured = next((v for v in raw if v.get("in_stock", True)), raw[0])
            vid = _variant_id(detail["id"], featured)
            match = "exact" if vid.upper() == ident.upper() else "featured"
            entry["inputs"].setdefault(vid, []).append({"id": ident, "match": match})

    out_products = []
    for pid, entry in products.items():
        detail = entry["detail"]
        all_vs = {v["id"]: v for v in (
            _variant({**detail, "variants": detail.get("variants") or [detail]}, raw, currency)
            for raw in (detail.get("variants") or [{"id": detail["id"], "price": detail.get("price"),
                                                    "compare_at_price": detail.get("compare_at_price"),
                                                    "in_stock": detail.get("in_stock")}])
        )}
        vs = []
        for vid, inputs in entry["inputs"].items():
            v = all_vs.get(vid)
            if v and _price_ok(v, price_filter):
                vs.append({**v, "inputs": inputs})
        if vs:
            out_products.append(product_from_detail(detail, currency, variants=vs))
    out: dict = {"products": out_products}
    if messages:
        out["messages"] = messages
    return out


async def get_product(req: dict) -> dict:
    ident = req.get("id")
    if not isinstance(ident, str) or not ident.strip():
        raise UcpError("invalid_request", "`id` (a product or variant ID) is required.",
                       kind="outcome", severity="recoverable")
    context = req.get("context") if isinstance(req.get("context"), dict) else {}
    currency = presentment_currency(context)
    hit = await _fetch_product(KaprukaClient(), ident.strip(), currency)
    if hit is None:
        raise UcpError("not_found", f"No product with ID {ident}.", kind="outcome", http_status=404,
                       continue_url=STOREFRONT)
    detail = hit["product"]
    raw = detail.get("variants") or [{"id": detail["id"], "price": detail.get("price"),
                                      "compare_at_price": detail.get("compare_at_price"),
                                      "in_stock": detail.get("in_stock")}]
    full = {**detail, "variants": raw}
    variants = [(v, _variant(full, v, currency)) for v in raw]

    def opts_of(v):
        return {o["name"]: o["label"] for o in _variant_options(v)} if len(raw) > 1 else {}

    if hit["variant_id"]:
        featured = next(p for p in variants if p[0].get("id") == hit["variant_id"])
        selected = opts_of(featured[0])
    else:
        req_sel = [s for s in req.get("selected") or [] if isinstance(s, dict) and s.get("name")]
        selected = {s["name"]: str(s.get("label")) for s in req_sel}
        order = [p for p in req.get("preferences") or [] if isinstance(p, str)] or list(selected)
        order += [n for n in selected if n not in order]
        while selected and not any(all(opts_of(v).get(n) == lab for n, lab in selected.items())
                                   for v, _ in variants):
            drop = order.pop() if order else next(reversed(selected))
            selected.pop(drop, None)
        matching = [p for p in variants if all(opts_of(p[0]).get(n) == lab for n, lab in selected.items())]
        featured = next((p for p in matching if p[0].get("in_stock", True)), matching[0] if matching else variants[0])
        if not selected:
            selected = opts_of(featured[0])

    subset = [featured] + [p for p in variants if p is not featured
                           and all(opts_of(p[0]).get(n) == lab for n, lab in selected.items())]
    price_filter = ((req.get("filters") or {}).get("price") or {}) if isinstance(req.get("filters"), dict) else {}
    vs = [ucp for _, ucp in subset if _price_ok(ucp, price_filter if isinstance(price_filter, dict) else {})]
    if not vs:
        raise UcpError("not_found", "No variant of this product matches the filters.", kind="outcome",
                       severity="recoverable", continue_url=detail.get("url") or STOREFRONT)
    product = product_from_detail(detail, currency, variants=vs)

    if len(raw) > 1:
        product["selected"] = [{"name": n, "label": lab} for n, lab in selected.items()]
        options = []
        for opt in _product_options(raw):
            values = []
            for val in opt["values"]:
                trial = {**selected, opt["name"]: val["label"]}
                match = [v for v, _ in variants if all(opts_of(v).get(n) == lab for n, lab in trial.items())]
                values.append({"label": val["label"], "exists": bool(match),
                               "available": any(v.get("in_stock", True) for v in match)})
            options.append({"name": opt["name"], "values": values})
        product["options"] = options
    return {"product": product}
