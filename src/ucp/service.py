"""One dispatcher for both UCP transports (REST and MCP).

`call(op, ...)` negotiates with the agent, runs the operation and returns a
Result. Transports only translate: REST status codes and headers, or MCP
`structuredContent` / JSON-RPC errors.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Optional

from src.ucp import catalog, checkout
from src.ucp.negotiation import Negotiated, UcpError, negotiate
from src.ucp.store import store

logger = logging.getLogger(__name__)

# op -> root capability it needs
ROOTS = {
    "create_checkout": checkout.ROOT,
    "get_checkout": checkout.ROOT,
    "update_checkout": checkout.ROOT,
    "complete_checkout": checkout.ROOT,
    "cancel_checkout": checkout.ROOT,
    "search_catalog": "dev.ucp.shopping.catalog.search",
    "lookup_catalog": "dev.ucp.shopping.catalog.lookup",
    "get_product": "dev.ucp.shopping.catalog.lookup",
}
_IDEMPOTENT = {"create_checkout", "update_checkout", "complete_checkout", "cancel_checkout"}
_KEY_REQUIRED = {"complete_checkout", "cancel_checkout"}


@dataclass
class Result:
    body: dict
    http_status: int = 200
    error: Optional[UcpError] = None     # transport-level failure (body is UcpError.body())


def _hash(op: str, resource_id, payload) -> str:
    raw = json.dumps([op, resource_id, payload], sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


async def _run(op: str, neg: Negotiated, resource_id, payload: dict, ip: str) -> tuple[dict, int]:
    if op == "create_checkout":
        return await checkout.create(payload, neg, ip), 201
    if op == "get_checkout":
        return await checkout.get(resource_id, neg, ip), 200
    if op == "update_checkout":
        return await checkout.update(resource_id, payload, neg, ip), 200
    if op == "complete_checkout":
        return await checkout.complete(resource_id, payload, neg, ip), 200
    if op == "cancel_checkout":
        return await checkout.cancel(resource_id, neg), 200
    if op == "search_catalog":
        out = await catalog.search(payload)
    elif op == "lookup_catalog":
        out = await catalog.lookup(payload)
    elif op == "get_product":
        out = await catalog.get_product(payload)
    else:
        raise UcpError("method_not_found", f"Unknown operation {op}.", http_status=404, rpc_code=-32601,
                       continue_url=None)
    return {"ucp": neg.ucp_block(ROOTS[op]), **out}, 200


async def call(op: str, *, profile_url: Optional[str], resource_id=None, payload=None,
               idempotency_key: Optional[str] = None, ip: str = "unknown") -> Result:
    try:
        if op not in ROOTS:
            raise UcpError("method_not_found", f"Unknown operation {op}.", http_status=404,
                           rpc_code=-32601, continue_url=None)
        if op in _KEY_REQUIRED and not idempotency_key:
            raise UcpError("invalid_request", f"{op} requires an idempotency key "
                           "(Idempotency-Key header, or meta[\"idempotency-key\"] over MCP).",
                           http_status=400, rpc_code=-32602, continue_url=None)
        neg = await negotiate(profile_url, ROOTS[op])
        payload = payload if payload is not None else {}
        if op in _IDEMPOTENT and idempotency_key:
            h = _hash(op, resource_id, payload)
            prior = store().idem_get(idempotency_key)
            if prior is not None:
                if prior[0] != h:
                    raise UcpError("idempotency_conflict",
                                   "This idempotency key was already used with a different request.",
                                   http_status=409, rpc_code=-32000, continue_url=None)
                return Result(prior[2], prior[1])
            body, status = await _run(op, neg, resource_id, payload, ip)
            store().idem_put(idempotency_key, h, status, body)
            return Result(body, status)
        body, status = await _run(op, neg, resource_id, payload, ip)
        return Result(body, status)
    except UcpError as e:
        if e.kind == "outcome":
            return Result(e.body(), e.http_status)
        return Result(e.body(), e.http_status, error=e)
    except Exception:
        logger.exception("ucp: %s failed", op)
        err = UcpError("internal_error", "Unexpected server error.", http_status=500, rpc_code=-32603,
                       continue_url=None)
        return Result(err.body(), 500, error=err)
