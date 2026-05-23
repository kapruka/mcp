"""Static `/.well-known/mcp.json` endpoint for MCP discovery.

Served at:
  https://mcp.kapruka.com/.well-known/mcp.json

Third-party MCP-aware clients and registries probe this URL to discover the
server's transport, authentication mode, and tool list without having to
initialise an MCP session first.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

# Static manifest. Bumped manually when tools / endpoints change.
WELL_KNOWN_MCP: dict = {
    "mcp_version": "1.0",
    "name": "Kapruka MCP",
    "description": (
        "Free public MCP server for Kapruka.com — Sri Lanka's largest "
        "e-commerce platform. Product, category, delivery and guest-checkout "
        "order tools."
    ),
    "provider": {
        "name": "Kapruka Holdings PLC",
        "url": "https://www.kapruka.com/",
        "contact": "hello@kapruka.com",
    },
    "endpoints": {
        "streamable_http": "https://mcp.kapruka.com/mcp",
    },
    "transports": ["streamable-http"],
    "authentication": {"type": "none"},
    "capabilities": {
        "tools": [
            {
                "name": "kapruka_search_products",
                "description": (
                    "Search the Kapruka catalog by keyword with optional category, "
                    "price range, stock, and sort filters."
                ),
            },
            {
                "name": "kapruka_get_product",
                "description": (
                    "Fetch full details for a single product by ID — name, price, "
                    "stock, variants, images, shipping, and a direct URL."
                ),
            },
            {
                "name": "kapruka_list_categories",
                "description": (
                    "List Kapruka's top-level product categories with browse URLs."
                ),
            },
            {
                "name": "kapruka_list_delivery_cities",
                "description": (
                    "Search Kapruka's Sri Lanka delivery network by canonical name "
                    "or vernacular alias."
                ),
            },
            {
                "name": "kapruka_check_delivery",
                "description": (
                    "Check whether an order can be delivered to a city on a given "
                    "date and at what flat LKR rate."
                ),
            },
            {
                "name": "kapruka_create_order",
                "description": (
                    "Create a guest-checkout order and return a click-to-pay URL. "
                    "No Kapruka account required; prices locked for 60 minutes."
                ),
            },
        ]
    },
    "rate_limits": {
        "per_ip": "60 requests per minute",
        "per_ip_orders": "30 create_order calls per hour",
    },
    "documentation": "https://mcp.kapruka.com/",
}


_COMMON_HEADERS = {
    "Cache-Control": "max-age=3600",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "X-Content-Type-Options": "nosniff",
}


async def well_known_mcp(_request: Request) -> JSONResponse:
    """GET handler — returns the static manifest as JSON."""
    return JSONResponse(WELL_KNOWN_MCP, headers=_COMMON_HEADERS)


async def well_known_mcp_options(_request: Request) -> Response:
    """OPTIONS handler — CORS preflight."""
    return Response(status_code=204, headers=_COMMON_HEADERS)
