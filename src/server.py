"""
Kapruka MCP Server

Exposes the Kapruka.com REST API as MCP tools for LLMs and third-party clients.
Transport: streamable HTTP, fronted by Caddy at https://mcp.kapruka.com.
"""

import logging
import re
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.server.streamable_http_manager import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.routing import Route

from src.activity_log import ActivityLogger, ActivityLogMiddleware
from src.cache import cache
from src.config.settings import settings
from src.middleware import RateLimitMiddleware
from src.order_rate_limit import OrderRateLimitMiddleware
from src.well_known import well_known_mcp, well_known_mcp_options

_STATIC_DIR = Path(__file__).parent / "static"
_LANDING_HTML = (_STATIC_DIR / "index.html").read_text(encoding="utf-8")

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

mcp = FastMCP(
    "kapruka_mcp",
    instructions=(
        "You are connected to the Kapruka MCP server for Kapruka.com — Sri Lanka's "
        "largest e-commerce platform. Use the tools to search products, browse "
        "categories, look up product details, quote delivery, create guest-checkout "
        "orders (pay link) and track orders. This is a free public tier; treat "
        "results as cached for up to 30 minutes.\n\n"
        "DELIVERY-CITY LIMITS — follow these rules:\n"
        "1. Most gifts ship island-wide, but restaurant food, hotel cakes and liquor "
        "are delivered only to selected cities (typically the Colombo area). Never "
        "promise delivery of a product to a city without checking: call "
        "kapruka_check_delivery with BOTH city and product_id (or read "
        "delivery.island_wide from kapruka_get_product). Say yes only if "
        "available is true.\n"
        "2. Surface the limit proactively: when a product's delivery.island_wide is "
        "false, tell the customer it is delivered only to selected cities before "
        "they pick a city. If deliverable_city_count is larger than the list you "
        "were given, say 'and more' — the list is truncated.\n"
        "3. On item_deliverable=false / available=false, offer the returned "
        "deliverable_cities (nearest first) or an island-wide alternative. Do not "
        "retry kapruka_create_order with the same city.\n"
        "4. An order ships as ONE shipment, so the whole cart must be deliverable "
        "to the city (intersection of every item's cities). If kapruka_create_order "
        "returns city_not_deliverable_for_item, nothing was created: tell the "
        "customer which item blocks it and offer to change the city or remove/"
        "replace that item. Never pretend a partial order was placed.\n"
        "5. Search results carry no delivery info — resolve it per item via "
        "kapruka_get_product or kapruka_check_delivery.\n"
        "6. Send canonical city spellings from kapruka_list_delivery_cities / "
        "deliverable_cities."
    ),
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=settings.enable_dns_rebinding_protection,
        allowed_hosts=settings.public_hosts,
        allowed_origins=settings.public_origins,
    ),
)

# ── Tool modules: importing them registers their @mcp.tool decorators.
from src.tools import cards, categories, customers, delivery, orders, products  # noqa: F401, E402
from src.cards import card_path  # noqa: E402


async def _landing(_request: Request) -> HTMLResponse:
    return HTMLResponse(_LANDING_HTML)


async def _health(_request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


async def _stats(_request: Request) -> JSONResponse:
    return JSONResponse({"cache": cache.stats()})


_CARD_NAME_RE = re.compile(r"^[a-f0-9]{16}\.jpg$")


async def _card(request: Request):
    """Serve a rendered options card. Content-addressed → cache forever."""
    name = request.path_params["name"]
    if not _CARD_NAME_RE.match(name):
        return JSONResponse({"error": "not found"}, status_code=404)
    path = card_path(name)
    if not path.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(
        path,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=604800, immutable"},
    )


def build_app() -> Starlette:
    """Compose the MCP Starlette app with our health routes + middleware."""
    app: Starlette = mcp.streamable_http_app()

    app.router.routes.insert(0, Route("/", _landing, methods=["GET"]))
    app.router.routes.insert(1, Route("/health", _health, methods=["GET"]))
    app.router.routes.insert(2, Route("/stats", _stats, methods=["GET"]))
    app.router.routes.insert(3, Route("/cards/{name}", _card, methods=["GET"]))
    app.router.routes.insert(3, Route("/.well-known/mcp.json", well_known_mcp, methods=["GET"]))
    app.router.routes.insert(4, Route("/.well-known/mcp.json", well_known_mcp_options, methods=["OPTIONS"]))

    # ── Activity logging (optional — disabled when ACTIVITY_DB_URL unset).
    # The middleware lazy-inits the pool on first request, so no lifespan plumbing.
    if settings.activity_db_url:
        activity_log = ActivityLogger(settings.activity_db_url)
        app.add_middleware(
            ActivityLogMiddleware,
            log=activity_log,
            trusted_proxies=settings.trusted_proxies,
        )
        logger.info("Activity logging: enabled (Postgres, lazy init)")
    else:
        logger.info("Activity logging: disabled (ACTIVITY_DB_URL not set)")

    if settings.rate_limit_enabled:
        app.add_middleware(
            RateLimitMiddleware,
            limit_per_minute=settings.rate_limit_per_minute,
            trusted_proxies=settings.trusted_proxies,
            exempt_ips=settings.rate_limit_exempt_ips,
            trusted_limit_per_minute=settings.rate_limit_trusted_per_minute,
        )
        logger.info(
            "Rate limit: %d req/min per IP (trusted proxies: %s; trusted IPs %s "
            "at %d req/min)",
            settings.rate_limit_per_minute,
            settings.trusted_proxies,
            settings.rate_limit_exempt_ips or "none",
            settings.rate_limit_trusted_per_minute,
        )
        # add_middleware wraps outermost-last, so this sits in front of the
        # per-minute limiter and runs first on every /mcp request.
        app.add_middleware(
            OrderRateLimitMiddleware,
            limit_per_hour=settings.order_rate_limit_per_hour,
            exempt_ips=settings.rate_limit_exempt_ips,
            trusted_limit_per_hour=settings.order_rate_limit_trusted_per_hour,
        )
        logger.info(
            "Order rate limit: %d/hour per IP for kapruka_create_order",
            settings.order_rate_limit_per_hour,
        )
    else:
        logger.warning("Rate limit DISABLED")

    return app


def main() -> None:
    import uvicorn

    logger.info(
        "Starting Kapruka MCP server on %s:%s", settings.mcp_host, settings.mcp_port
    )
    uvicorn.run(
        build_app(),
        host=settings.mcp_host,
        port=settings.mcp_port,
        log_level=settings.log_level.lower(),
        access_log=False,
        # Do NOT let uvicorn rewrite scope["client"] from X-Forwarded-For:
        # behind CF->Caddy the XFF chain ends with the CF edge IP, so uvicorn
        # replaced the peer with the EDGE address — our middlewares then saw a
        # non-trusted peer, skipped header inspection, and rate-limited whole
        # Cloudflare PoPs as single clients. All our middlewares (rate limit,
        # order limit, activity log) derive the real client themselves via
        # CF-Connecting-IP > X-Real-IP > XFF from the trusted local proxy.
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
