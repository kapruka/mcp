"""
Kapruka MCP Server

Exposes the Kapruka.com REST API as MCP tools for LLMs and third-party clients.
Transport: streamable HTTP, fronted by Caddy at https://mcp.kapruka.com.
"""

import logging
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.server.streamable_http_manager import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

from src.activity_log import ActivityLogger, ActivityLogMiddleware
from src.cache import cache
from src.config.settings import settings
from src.middleware import RateLimitMiddleware
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
        "You are connected to the Kapruka MCP server, which provides read-only access "
        "to Kapruka.com — Sri Lanka's largest e-commerce platform. Use the available "
        "tools to search products, browse categories, and look up product details. "
        "This is a free public tier; treat results as cached for up to 30 minutes."
    ),
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=settings.public_hosts,
        allowed_origins=settings.public_origins,
    ),
)

# ── Tool modules: importing them registers their @mcp.tool decorators.
from src.tools import categories, delivery, products  # noqa: F401, E402


async def _landing(_request: Request) -> HTMLResponse:
    return HTMLResponse(_LANDING_HTML)


async def _health(_request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


async def _stats(_request: Request) -> JSONResponse:
    return JSONResponse({"cache": cache.stats()})


def build_app() -> Starlette:
    """Compose the MCP Starlette app with our health routes + middleware."""
    app: Starlette = mcp.streamable_http_app()

    app.router.routes.insert(0, Route("/", _landing, methods=["GET"]))
    app.router.routes.insert(1, Route("/health", _health, methods=["GET"]))
    app.router.routes.insert(2, Route("/stats", _stats, methods=["GET"]))
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
        )
        logger.info(
            "Rate limit: %d req/min per IP (trusted proxies: %s)",
            settings.rate_limit_per_minute,
            settings.trusted_proxies,
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
        proxy_headers=True,
        forwarded_allow_ips=",".join(settings.trusted_proxies),
    )


if __name__ == "__main__":
    main()
