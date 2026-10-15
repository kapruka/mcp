"""UCP over REST: https://mcp.kapruka.com/ucp/v1 (paths from UCP's rest.openapi.json).

  POST /checkout-sessions                 create_checkout   (201)
  GET  /checkout-sessions/{id}            get_checkout
  PUT  /checkout-sessions/{id}            update_checkout
  POST /checkout-sessions/{id}/complete   complete_checkout
  POST /checkout-sessions/{id}/cancel     cancel_checkout
  POST /catalog/search                    search_catalog
  POST /catalog/lookup                    lookup_catalog
  POST /catalog/product                   get_product

The agent names its profile in `UCP-Agent: profile="https://…"`; complete and
cancel need `Idempotency-Key`.
"""

from __future__ import annotations

import json

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from src.private_access import client_ip_from_request
from src.ucp import service
from src.ucp.negotiation import profile_url_from_header

PREFIX = "/ucp/v1"


async def _body(request: Request):
    raw = await request.body()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _respond(result: service.Result) -> Response:
    headers = {"Cache-Control": "no-store"}
    if result.error is not None and result.error.retry_after:
        headers["Retry-After"] = str(result.error.retry_after)
    return JSONResponse(result.body, status_code=result.http_status, headers=headers)


def _handler(op: str, *, with_body: bool):
    async def endpoint(request: Request) -> Response:
        payload = None
        if with_body:
            payload = await _body(request)
            if not isinstance(payload, dict):
                return JSONResponse({"code": "invalid_request", "content": "The body must be a JSON object."},
                                    status_code=400)
        result = await service.call(
            op,
            profile_url=profile_url_from_header(request.headers.get("ucp-agent")),
            resource_id=request.path_params.get("id"),
            payload=payload,
            idempotency_key=request.headers.get("idempotency-key") or None,
            ip=client_ip_from_request(request),
        )
        return _respond(result)
    return endpoint


ROUTES = [
    Route(f"{PREFIX}/checkout-sessions", _handler("create_checkout", with_body=True), methods=["POST"]),
    Route(f"{PREFIX}/checkout-sessions/{{id}}", _handler("get_checkout", with_body=False), methods=["GET"]),
    Route(f"{PREFIX}/checkout-sessions/{{id}}", _handler("update_checkout", with_body=True), methods=["PUT"]),
    Route(f"{PREFIX}/checkout-sessions/{{id}}/complete", _handler("complete_checkout", with_body=True),
          methods=["POST"]),
    Route(f"{PREFIX}/checkout-sessions/{{id}}/cancel", _handler("cancel_checkout", with_body=False),
          methods=["POST"]),
    Route(f"{PREFIX}/catalog/search", _handler("search_catalog", with_body=True), methods=["POST"]),
    Route(f"{PREFIX}/catalog/lookup", _handler("lookup_catalog", with_body=True), methods=["POST"]),
    Route(f"{PREFIX}/catalog/product", _handler("get_product", with_body=True), methods=["POST"]),
]
