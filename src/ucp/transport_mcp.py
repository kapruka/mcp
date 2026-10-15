"""UCP over MCP: https://mcp.kapruka.com/ucp/mcp

A separate, stateless MCP server (streamable HTTP, JSON responses) whose tools
are exactly UCP's operations — create_checkout, get_checkout, update_checkout,
complete_checkout, cancel_checkout, search_catalog, lookup_catalog,
get_product — with arguments `{meta, id?, checkout|catalog}` as in UCP's
mcp.openrpc.json. It shares nothing with the Kapruka MCP at /mcp.

Business outcomes come back as `result.structuredContent` (plus the same JSON
as text); negotiation and protocol failures as JSON-RPC errors (-32001
discovery/version, -32000 protocol, -32602 bad params), per the UCP overview.
"""

from __future__ import annotations

import json
from typing import Any

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import McpError

from src.config.settings import settings
from src.private_access import client_ip_from_request
from src.ucp import VERSION, service
from src.ucp.negotiation import profile_url_from_meta

_META = {
    "type": "object",
    "description": "UCP request metadata: ucp-agent.profile (required), idempotency-key "
                   "(required for complete_checkout and cancel_checkout).",
    "properties": {
        "ucp-agent": {"type": "object", "properties": {"profile": {"type": "string"}},
                      "required": ["profile"]},
        "idempotency-key": {"type": "string"},
    },
    "required": ["ucp-agent"],
}
_ID = {"type": "string", "description": "Checkout session id."}
_CHECKOUT = {"type": "object", "description": f"UCP Checkout ({VERSION}) request payload.",
             "additionalProperties": True}
_CATALOG = {"type": "object", "description": f"UCP Catalog ({VERSION}) request payload.",
            "additionalProperties": True}


def _schema(**props) -> dict:
    return {"type": "object", "properties": {"meta": _META, **props},
            "required": ["meta", *props.keys()], "additionalProperties": False}


TOOLS = [
    types.Tool(name="create_checkout", description="Create a Kapruka checkout session.",
               inputSchema=_schema(checkout=_CHECKOUT)),
    types.Tool(name="get_checkout", description="Get a checkout session.", inputSchema=_schema(id=_ID)),
    types.Tool(name="update_checkout", description="Update (replace) a checkout session.",
               inputSchema=_schema(id=_ID, checkout=_CHECKOUT)),
    types.Tool(name="complete_checkout",
               description="Finalize the checkout. Kapruka takes payment on its own page: a complete "
                           "session returns status requires_escalation with continue_url.",
               inputSchema=_schema(id=_ID, checkout=_CHECKOUT)),
    types.Tool(name="cancel_checkout", description="Cancel a checkout session.", inputSchema=_schema(id=_ID)),
    types.Tool(name="search_catalog", description="Search Kapruka's catalog.",
               inputSchema=_schema(catalog=_CATALOG)),
    types.Tool(name="lookup_catalog", description="Look up products or variants by ID.",
               inputSchema=_schema(catalog=_CATALOG)),
    types.Tool(name="get_product", description="Full product detail with option selection.",
               inputSchema=_schema(catalog=_CATALOG)),
]
_TOOL_NAMES = {t.name for t in TOOLS}

server: Server = Server("kapruka-ucp", version=VERSION)


async def _list_tools(_req: types.ListToolsRequest) -> types.ServerResult:
    return types.ServerResult(types.ListToolsResult(tools=TOOLS))


def _client_ip() -> str:
    try:
        return client_ip_from_request(server.request_context.request)
    except LookupError:
        return "unknown"


async def _call_tool(req: types.CallToolRequest) -> types.ServerResult:
    name = req.params.name
    args: dict[str, Any] = req.params.arguments or {}
    if name not in _TOOL_NAMES:
        raise McpError(types.ErrorData(code=-32601, message=f"Unknown tool {name}"))
    meta = args.get("meta")
    if not isinstance(meta, dict):
        raise McpError(types.ErrorData(code=-32602, message="`meta` (with ucp-agent.profile) is required"))
    payload = args.get("catalog") if name in ("search_catalog", "lookup_catalog", "get_product") \
        else args.get("checkout")
    if name in ("create_checkout", "update_checkout", "complete_checkout", "search_catalog",
                "lookup_catalog", "get_product") and not isinstance(payload, dict):
        field = "catalog" if name.endswith(("catalog", "product")) else "checkout"
        raise McpError(types.ErrorData(code=-32602, message=f"`{field}` (an object) is required"))
    if name not in ("create_checkout", "search_catalog", "lookup_catalog", "get_product") \
            and not isinstance(args.get("id"), str):
        raise McpError(types.ErrorData(code=-32602, message="`id` (the checkout id) is required"))
    if isinstance(payload, dict) and name.endswith("_checkout"):
        payload = {k: v for k, v in payload.items() if k != "id"}   # MUST NOT carry id

    key = meta.get("idempotency-key")
    result = await service.call(
        name, profile_url=profile_url_from_meta(meta), resource_id=args.get("id"), payload=payload,
        idempotency_key=key if isinstance(key, str) and key else None, ip=_client_ip(),
    )
    if result.error is not None:
        e = result.error
        data = e.body()
        raise McpError(types.ErrorData(code=e.rpc_code, message=e.content, data=data))
    return types.ServerResult(types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(result.body, ensure_ascii=False))],
        structuredContent=result.body,
        isError=False,
    ))


server.request_handlers[types.ListToolsRequest] = _list_tools
server.request_handlers[types.CallToolRequest] = _call_tool

def new_session_manager() -> StreamableHTTPSessionManager:
    """One per app: a manager's run() may be entered only once."""
    return StreamableHTTPSessionManager(
        app=server,
        json_response=True,
        stateless=True,
        security_settings=TransportSecuritySettings(
            enable_dns_rebinding_protection=settings.enable_dns_rebinding_protection,
            allowed_hosts=settings.public_hosts,
            allowed_origins=settings.public_origins,
        ),
    )


class ASGIApp:
    def __init__(self, manager: StreamableHTTPSessionManager):
        self.manager = manager

    async def __call__(self, scope, receive, send) -> None:
        await self.manager.handle_request(scope, receive, send)
