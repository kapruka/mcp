#!/usr/bin/env python3
"""Live smoke test — calls the real MCP tools against the live API."""
import asyncio
import os
import sys

os.environ.setdefault("KAPRUKA_API_BASE_URL", "https://www.kapruka.com")
if not os.environ.get("KAPRUKA_API_KEY"):
    sys.exit("KAPRUKA_API_KEY env var required (get the value from .env or your password manager)")

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

from src.tools.products import GetProductInput, SearchProductsInput, kapruka_get_product, kapruka_search_products
from src.tools.categories import ListCategoriesInput, kapruka_list_categories


async def main() -> None:
    print("=== kapruka_get_product ===")
    result = await kapruka_get_product(GetProductInput(product_id="cake00ka002034", currency="LKR"))
    print(result)

    print("\n=== kapruka_get_product (USD) ===")
    result = await kapruka_get_product(GetProductInput(product_id="cake00ka002034", currency="USD"))
    print(result)

    print("\n=== kapruka_search_products (cakes/birthday) ===")
    result = await kapruka_search_products(SearchProductsInput(q="cake", category="Birthday", limit=5))
    print(result)

    print("\n=== kapruka_search_products (pagination) ===")
    r1 = await kapruka_search_products(SearchProductsInput(q="flowers", limit=3, response_format="json"))
    import json
    data = json.loads(r1)
    cursor = data.get("next_cursor")
    print(f"Page 1 — {len(data['results'])} results, next_cursor={cursor}")
    if cursor:
        r2 = await kapruka_search_products(SearchProductsInput(q="flowers", limit=3, cursor=cursor))
        print("Page 2:")
        print(r2)

    print("\n=== kapruka_list_categories (root, depth=1) ===")
    result = await kapruka_list_categories(ListCategoriesInput(depth=1))
    print(result)

    print("\n=== kapruka_list_categories (flat) ===")
    result = await kapruka_list_categories(ListCategoriesInput(depth=1, flat=True, parent_id="cat_1"))
    print(result)

    print("\n=== CATSYM stub guard ===")
    result = await kapruka_get_product(GetProductInput(product_id="CATSYM00230"))
    print(result)

    print("\nSmoke tests passed.")


if __name__ == "__main__":
    asyncio.run(main())
