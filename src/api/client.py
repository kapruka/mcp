"""Async HTTP client for the Kapruka REST API."""

import hashlib
import json
import logging
import re
from typing import Any

import httpx

from src.cache import cache
from src.config.settings import settings

logger = logging.getLogger(__name__)

_JSP_PATH = "/tools/commerce_phase1.jsp"

# Per-endpoint cache TTLs (seconds). 0 = uncached.
_TTL_BY_ENDPOINT: dict[str, float] = {
    "categories": 1800.0,        # 30 min — change rarely
    "product": 600.0,            # 10 min — price/stock can change
    "products_search": 300.0,    # 5 min — new products appear
    "product_related": 3600.0,   # 1 hour — recommendations stable
    "delivery_cities": 86400.0,  # 24 hours — change rarely
    "delivery_rates": 21600.0,   # 6 hours — change seasonally
    # delivery_check is uncached: response embeds "now" and depends on real-time clock
}


class KaprukaAPIError(Exception):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def handle_api_error(e: Exception) -> str:
    """Return a human-readable, actionable error string for any exception."""
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        if code == 400:
            return f"Error: Bad request — {e.response.text[:200]}. Check your input parameters."
        if code == 401:
            return "Error: Unauthorized. Check that KAPRUKA_API_KEY is set correctly."
        if code == 403:
            return "Error: Forbidden. Your API key does not have access to this resource."
        if code == 404:
            return "Error: Resource not found. Verify the product_id or category ID is correct."
        if code == 422:
            return f"Error: Validation failed — {e.response.text[:300]}."
        if code == 429:
            return "Error: Rate limit exceeded. Wait a moment before retrying."
        if code >= 500:
            return f"Error: Kapruka API server error (HTTP {code}). Try again later."
        return f"Error: API request failed with HTTP {code}."
    if isinstance(e, httpx.TimeoutException):
        return "Error: Request timed out. The Kapruka API may be slow — try again."
    if isinstance(e, httpx.ConnectError):
        return f"Error: Could not connect to Kapruka API at {settings.api_base_url}. Is the server running?"
    return f"Error: Unexpected error — {type(e).__name__}: {e}"


def _parse_response(response: httpx.Response) -> dict[str, Any]:
    """
    Parse API response, handling the known backend bug where some USD prices
    are serialised as `"amount": US$none` (invalid JSON).
    Falls back to stripping the bad token and re-parsing.
    """
    try:
        return response.json()
    except Exception:
        text = response.text
        fixed = re.sub(r':\s*US\$none\b', ': null', text)
        try:
            return json.loads(fixed)
        except Exception:
            raise KaprukaAPIError(
                f"Unparseable response from Kapruka API: {text[:300]}"
            )


def _cache_key(endpoint: str, params: dict[str, Any]) -> str:
    """Stable key from endpoint + sorted params."""
    payload = json.dumps(
        {"e": endpoint, "p": params}, sort_keys=True, default=str, ensure_ascii=False
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _ttl_for(endpoint: str) -> float:
    return _TTL_BY_ENDPOINT.get(endpoint, 0.0)


class KaprukaClient:
    """Stateless async client. Create one per request or share via lifespan."""

    def __init__(self) -> None:
        self._base = settings.api_base_url.rstrip("/")
        self._timeout = settings.request_timeout
        self._headers = {
            "Authorization": f"Bearer {settings.api_key}",
            "Accept": "application/json",
            # Default httpx UA ("python-httpx/...") is flagged by Cloudflare's
            # Bot Fight Mode on www.kapruka.com. Identify ourselves explicitly.
            "User-Agent": "kapruka-mcp/1.0 (+https://mcp.kapruka.com)",
        }

    async def call(self, endpoint: str, **params: Any) -> dict[str, Any]:
        """Call the JSP endpoint. Cached per (endpoint, params) when TTL>0."""
        clean_params = {k: v for k, v in params.items() if v is not None}
        ttl = _ttl_for(endpoint)

        if ttl > 0:
            key = _cache_key(endpoint, clean_params)
            hit = cache.get(key)
            if hit is not None:
                logger.debug("cache hit %s %s", endpoint, clean_params)
                return hit
        else:
            key = None

        query: dict[str, Any] = {"endpoint": endpoint, **clean_params}
        url = f"{self._base}{_JSP_PATH}"
        logger.debug("GET %s params=%s", url, query)
        async with httpx.AsyncClient(
            timeout=self._timeout, follow_redirects=True
        ) as client:
            response = await client.get(url, headers=self._headers, params=query)
            response.raise_for_status()
            data = _parse_response(response)

        if key is not None:
            cache.set(key, data, ttl)
        return data
