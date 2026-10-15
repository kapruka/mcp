"""Kapruka's UCP business profile (`/.well-known/ucp`).

The canonical copy is hosted by the website at
https://www.kapruka.com/.well-known/ucp — generate it with
`python cli.py ucp-profile > <web>/.well-known/ucp` whenever this changes. The
same document is also served at https://mcp.kapruka.com/.well-known/ucp.

Hosting rules (UCP overview, "Profile Requirements"): HTTPS, no redirects,
`Cache-Control: public, max-age>=60`, and an ETag or Last-Modified.
"""

from __future__ import annotations

import hashlib
import json

from starlette.requests import Request
from starlette.responses import Response

from src.config.settings import settings
from src.ucp import VERSION

_SPEC = f"https://ucp.dev/{VERSION}/specification"
_SCHEMAS = f"https://ucp.dev/{VERSION}/schemas"
_SERVICES = f"https://ucp.dev/{VERSION}/services"

# What Kapruka implements. Order, Cart, Discount and Identity Linking are not
# offered at Level 1 (see src/ucp/__init__.py).
CAPABILITIES: dict[str, dict] = {
    "dev.ucp.shopping.catalog.search": {
        "spec": f"{_SPEC}/shopping/catalog/search",
        "schema": f"{_SCHEMAS}/shopping/catalog_search.json",
    },
    "dev.ucp.shopping.catalog.lookup": {
        "spec": f"{_SPEC}/shopping/catalog/lookup",
        "schema": f"{_SCHEMAS}/shopping/catalog_lookup.json",
    },
    "dev.ucp.shopping.checkout": {
        "spec": f"{_SPEC}/shopping/checkout",
        "schema": f"{_SCHEMAS}/shopping/checkout.json",
    },
    "dev.ucp.shopping.fulfillment": {
        "spec": f"{_SPEC}/shopping/extensions/fulfillment",
        "schema": f"{_SCHEMAS}/shopping/fulfillment.json",
        "extends": "dev.ucp.shopping.checkout",
    },
}


def rest_endpoint() -> str:
    return settings.ucp_public_base.rstrip("/") + "/ucp/v1"


def mcp_endpoint() -> str:
    return settings.ucp_public_base.rstrip("/") + "/ucp/mcp"


def business_profile() -> dict:
    return {
        "ucp": {
            "version": VERSION,
            "services": {
                "dev.ucp.shopping": [
                    {
                        "version": VERSION,
                        "spec": f"{_SPEC}/overview/",
                        "transport": "rest",
                        "endpoint": rest_endpoint(),
                        "schema": f"{_SERVICES}/shopping/rest.openapi.json",
                    },
                    {
                        "version": VERSION,
                        "spec": f"{_SPEC}/overview/",
                        "transport": "mcp",
                        "endpoint": mcp_endpoint(),
                        "schema": f"{_SERVICES}/shopping/mcp.openrpc.json",
                    },
                ]
            },
            "capabilities": {
                name: [{"version": VERSION, **entry}] for name, entry in CAPABILITIES.items()
            },
            # Level 1: the buyer pays on Kapruka's own checkout (continue_url).
            "payment_handlers": {},
        }
    }


def profile_json() -> str:
    return json.dumps(business_profile(), indent=2) + "\n"


_HEADERS = {
    "Cache-Control": "public, max-age=3600",
    "Access-Control-Allow-Origin": "*",
    "X-Content-Type-Options": "nosniff",
}


async def well_known_ucp(request: Request) -> Response:
    body = profile_json()
    etag = '"' + hashlib.sha256(body.encode()).hexdigest()[:16] + '"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={**_HEADERS, "ETag": etag})
    return Response(body, media_type="application/json", headers={**_HEADERS, "ETag": etag})
