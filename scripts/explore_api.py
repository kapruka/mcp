#!/usr/bin/env python3
"""Quick API exploration script — run this to understand Kapruka's response shapes."""

import asyncio
import json
import sys
from pathlib import Path

import os

import httpx

BASE_URL = os.environ.get("KAPRUKA_API_BASE_URL", "https://www.kapruka.com")
TOKEN = os.environ.get("KAPRUKA_API_KEY")
if not TOKEN:
    sys.exit("KAPRUKA_API_KEY env var required (get the value from .env or your password manager)")
JSP = "/tools/commerce_phase1.jsp"

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/json",
}


OUT = open(Path(__file__).parent / "api_responses.json", "w", encoding="utf-8")

def pp(label: str, data: object) -> None:
    print(f"\n{'='*60}")
    print(f"  {label}")
    print('='*60)
    payload = json.dumps(data, indent=2, ensure_ascii=False)
    # write to file (full fidelity) and stdout (ascii-safe for Windows console)
    OUT.write(f"\n# {label}\n{payload}\n")
    OUT.flush()
    safe = payload.encode("ascii", "replace").decode("ascii")
    print(safe)


async def explore(client: httpx.AsyncClient) -> None:
    async def get(params: dict) -> object:
        r = await client.get(BASE_URL + JSP, params=params, headers=HEADERS, timeout=15)
        print(f"  HTTP {r.status_code}  ({r.elapsed.total_seconds():.2f}s)")
        try:
            return r.json()
        except Exception:
            return {"_raw": r.text[:2000]}

    # ── 1. Product basic
    print("\n>>> product (basic)")
    data = await get({"endpoint": "product", "product_id": "cake00ka002034", "currency": "LKR"})
    pp("product basic — LKR", data)

    # ── 2. Product with type=specialgifts
    print("\n>>> product (type=specialgifts, USD)")
    data = await get({"endpoint": "product", "product_id": "cake00ka002034", "type": "specialgifts", "currency": "USD"})
    pp("product with type=specialgifts, USD", data)

    # ── 3. Search — broad query
    print("\n>>> products_search q=cake limit=5")
    data = await get({"endpoint": "products_search", "q": "cake", "limit": "5", "currency": "LKR"})
    pp("search: cake / LKR / limit=5", data)

    # ── 4. Search — with category
    print("\n>>> products_search q=cake category=Birthday limit=5")
    data = await get({"endpoint": "products_search", "q": "cake", "category": "Birthday", "limit": "5", "currency": "LKR"})
    pp("search: cake / Birthday / LKR / limit=5", data)

    # ── 5. Search — grab cursor from result 4 and paginate
    cursor = None
    if isinstance(data, dict):
        # common cursor field names
        for key in ("next_cursor", "cursor", "nextCursor", "next_page", "nextPage"):
            if data.get(key):
                cursor = data[key]
                break
    if cursor:
        print(f"\n>>> products_search — page 2 using cursor={cursor}")
        data = await get({"endpoint": "products_search", "q": "cake", "category": "Birthday", "limit": "5", "cursor": cursor, "currency": "LKR"})
        pp("search: page 2", data)
    else:
        print("\n  (no cursor found in search response — skipping pagination test)")

    # ── 6. Search — different queries to see variety
    for q in ["flowers", "chocolate", "tea"]:
        print(f"\n>>> products_search q={q} limit=3")
        data = await get({"endpoint": "products_search", "q": q, "limit": "3", "currency": "LKR"})
        pp(f"search: {q}", data)

    # ── 7. Categories — root
    print("\n>>> categories (root)")
    data = await get({"endpoint": "categories"})
    pp("categories root", data)

    # ── 8. Categories — try to find a real parent_id from result
    parent_id = None
    if isinstance(data, dict):
        items = data.get("categories") or data.get("items") or data.get("data") or []
        if isinstance(items, list) and items:
            first = items[0]
            if isinstance(first, dict):
                for key in ("id", "category_id", "categoryId", "cat_id"):
                    if first.get(key):
                        parent_id = str(first[key])
                        break

    if parent_id:
        print(f"\n>>> categories parent_id={parent_id} depth=2 include_empty=true")
        data = await get({"endpoint": "categories", "parent_id": parent_id, "depth": "2", "include_empty": "true"})
        pp(f"categories under parent {parent_id}", data)
    else:
        print(f"\n>>> categories parent_id=cat_12 depth=3 (from collection example)")
        data = await get({"endpoint": "categories", "parent_id": "cat_12", "depth": "3", "include_empty": "true"})
        pp("categories under cat_12", data)

    # ── 9. Search with USD to compare currency fields
    print("\n>>> products_search q=flowers limit=3 currency=USD")
    data = await get({"endpoint": "products_search", "q": "flowers", "limit": "3", "currency": "USD"})
    pp("search: flowers / USD", data)

    # ── 10. Try a different product_id to see if structure varies
    print("\n>>> product — try a flower product if we found one")
    product_id = None
    if isinstance(data, dict):
        items = data.get("products") or data.get("items") or data.get("results") or data.get("data") or []
        if isinstance(items, list) and items:
            first = items[0]
            if isinstance(first, dict):
                for key in ("product_id", "id", "productId", "sku"):
                    if first.get(key):
                        product_id = str(first[key])
                        break
    if product_id and product_id != "cake00ka002034":
        data = await get({"endpoint": "product", "product_id": product_id, "currency": "LKR"})
        pp(f"product detail for {product_id}", data)
    else:
        print("  (skipped — no new product_id found)")


async def main() -> None:
    print(f"Exploring Kapruka API at {BASE_URL}{JSP}")
    async with httpx.AsyncClient(follow_redirects=True) as client:
        await explore(client)
    print("\n\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
