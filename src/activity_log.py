"""Activity logging for the Kapruka MCP server.

Per-request append to the `mcp_activity` table on the Eagle Postgres instance.
Logging is fire-and-forget — a bounded asyncio queue drained by a single
background task. If Postgres goes away, MCP requests still serve normally;
queued entries are dropped after the queue fills.

The ASGI middleware sniffs the JSON-RPC request body to extract MCP method
and tool name + arguments. Tool arguments are truncated to 4 KiB.
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
import time
from typing import Any, Optional

import asyncpg
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

# tool_args text is cast to jsonb in SQL so we can pass the raw JSON string.
_INSERT_SQL = """
INSERT INTO mcp_activity (
    client_ip, user_agent, session_id, mcp_method, tool_name, tool_args,
    status_code, latency_ms, error, request_bytes, response_bytes,
    forwarded_for, cf_ray, cf_country
) VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9, $10, $11, $12, $13, $14)
"""

_TRUNCATE_ARGS_BYTES = 4096
_QUEUE_MAX = 2000


# ── Logger / queue / worker ──────────────────────────────────────────────────


class ActivityLogger:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: Optional[asyncpg.Pool] = None
        self._queue: Optional[asyncio.Queue[dict]] = None
        self._worker: Optional[asyncio.Task] = None
        self._init_lock: Optional[asyncio.Lock] = None
        self._init_done = False
        self._dropped = 0

    async def ensure_started(self) -> None:
        """Lazy-init the pool + worker on first use. Idempotent.

        We don't hook ASGI lifespan because some Starlette/FastMCP wrappers
        expose lifespan only via a context manager and rebuffing event
        handlers fails. Init-on-first-request is simple and correct.
        """
        if self._init_done:
            return
        if self._init_lock is None:
            self._init_lock = asyncio.Lock()
        async with self._init_lock:
            if self._init_done:
                return
            try:
                # Eagle's Postgres requires TLS but presents no client cert.
                # asyncpg's built-in ssl="require" still pokes default cert
                # paths under the deploy user's home and trips on permission
                # errors, so we build a no-verify SSL context ourselves.
                sslctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                sslctx.check_hostname = False
                sslctx.verify_mode = ssl.CERT_NONE

                self._pool = await asyncpg.create_pool(
                    self._dsn,
                    min_size=1,
                    max_size=4,
                    command_timeout=10,
                    ssl=sslctx,
                )
                self._queue = asyncio.Queue(maxsize=_QUEUE_MAX)
                self._worker = asyncio.create_task(
                    self._drain(), name="activity-log-drain"
                )
                logger.info("activity_log: pool ready, worker started")
            except Exception as e:
                logger.warning("activity_log: init failed (%s) — logging disabled", e)
                self._pool = None
                self._queue = None
            finally:
                # Mark done either way so we don't hammer a dead DB on every request.
                self._init_done = True

    def enqueue(self, entry: dict) -> None:
        if self._queue is None:
            return
        try:
            self._queue.put_nowait(entry)
        except asyncio.QueueFull:
            # Drop oldest (likely a DB stall) and push the new one.
            self._dropped += 1
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(entry)
            except Exception:
                pass

    async def _drain(self) -> None:
        assert self._pool is not None and self._queue is not None
        while True:
            entry = await self._queue.get()
            try:
                async with self._pool.acquire() as conn:
                    await conn.execute(
                        _INSERT_SQL,
                        entry.get("client_ip"),
                        entry.get("user_agent"),
                        entry.get("session_id"),
                        entry.get("mcp_method"),
                        entry.get("tool_name"),
                        entry.get("tool_args"),
                        entry.get("status_code"),
                        entry.get("latency_ms"),
                        entry.get("error"),
                        entry.get("request_bytes"),
                        entry.get("response_bytes"),
                        entry.get("forwarded_for"),
                        entry.get("cf_ray"),
                        entry.get("cf_country"),
                    )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # Never crash the worker — log and move on.
                logger.warning("activity_log: insert failed: %s", e)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _decode(value: bytes) -> str:
    return value.decode("latin-1").strip()


def _extract_meta(scope: Scope, trusted_proxies: set[str]) -> dict:
    """Pull IP, UA, MCP session id, CF headers from request scope.

    IP resolution priority:
      1. CF-Connecting-IP — Cloudflare-set, can't be spoofed through CF
      2. X-Real-IP — set by Caddy (which already maps CF-Connecting-IP)
      3. First entry of X-Forwarded-For
      4. ASGI peer IP (after uvicorn proxy_headers rewriting)

    We accept these even without a trusted-proxy check because the app only
    listens on 127.0.0.1 — the only thing that can talk to it is local Caddy.
    """
    client = scope.get("client") or ("unknown", 0)
    peer_ip = client[0] if client else "unknown"

    user_agent = session_id = forwarded_for = cf_ray = cf_country = None
    cf_connecting_ip = real_ip = None
    for name, value in scope.get("headers") or []:
        if name == b"user-agent":
            user_agent = _decode(value)
        elif name == b"mcp-session-id":
            session_id = _decode(value)
        elif name == b"cf-connecting-ip":
            cf_connecting_ip = _decode(value)
        elif name == b"x-real-ip":
            real_ip = _decode(value)
        elif name == b"x-forwarded-for":
            forwarded_for = _decode(value)
        elif name == b"cf-ray":
            cf_ray = _decode(value)
        elif name == b"cf-ipcountry":
            cf_country = _decode(value)

    if cf_connecting_ip:
        client_ip = cf_connecting_ip
    elif real_ip:
        client_ip = real_ip
    elif forwarded_for:
        client_ip = forwarded_for.split(",")[0].strip() or peer_ip
    else:
        client_ip = peer_ip

    return {
        "client_ip": client_ip,
        "user_agent": user_agent,
        "session_id": session_id,
        "forwarded_for": forwarded_for,
        "cf_ray": cf_ray,
        "cf_country": cf_country,
    }


def _parse_jsonrpc(body: bytes) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Return (mcp_method, tool_name, tool_args_json_str_truncated)."""
    if not body:
        return None, None, None
    try:
        msg = json.loads(body.decode("utf-8"))
    except Exception:
        return None, None, None

    if isinstance(msg, list):
        return "batch", None, None
    if not isinstance(msg, dict):
        return None, None, None

    method = msg.get("method")
    if not isinstance(method, str):
        method = None

    tool_name = None
    tool_args_json: Optional[str] = None

    if method == "tools/call":
        params = msg.get("params") or {}
        if isinstance(params, dict):
            name = params.get("name")
            if isinstance(name, str):
                tool_name = name
            args = params.get("arguments")
            if args is not None:
                try:
                    args_str = json.dumps(args, ensure_ascii=False)
                    if len(args_str.encode("utf-8")) > _TRUNCATE_ARGS_BYTES:
                        tool_args_json = json.dumps(
                            {"_truncated": True, "preview": args_str[:1000]}
                        )
                    else:
                        tool_args_json = args_str
                except Exception:
                    pass

    return method, tool_name, tool_args_json


# ── Middleware ───────────────────────────────────────────────────────────────


class ActivityLogMiddleware:
    """ASGI middleware that logs each MCP HTTP request asynchronously."""

    def __init__(
        self,
        app: ASGIApp,
        log: ActivityLogger,
        trusted_proxies: list[str] | None = None,
        watched_prefixes: tuple[str, ...] = ("/mcp",),
    ) -> None:
        self.app = app
        self.log = log
        self.trusted_proxies = set(trusted_proxies or ["127.0.0.1", "::1"])
        self.watched_prefixes = watched_prefixes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if not any(path.startswith(p) for p in self.watched_prefixes):
            await self.app(scope, receive, send)
            return

        # Best-effort lazy init of the pool. If the DB is unreachable we
        # mark init as done and silently skip future logging.
        await self.log.ensure_started()

        meta = _extract_meta(scope, self.trusted_proxies)
        start = time.monotonic()

        body_chunks: list[bytes] = []

        async def wrapped_receive() -> Message:
            msg = await receive()
            if msg.get("type") == "http.request":
                chunk = msg.get("body") or b""
                if chunk:
                    body_chunks.append(chunk)
            return msg

        status_code = 0
        response_bytes = 0

        async def wrapped_send(message: Message) -> None:
            nonlocal status_code, response_bytes
            mtype = message.get("type")
            if mtype == "http.response.start":
                status_code = int(message.get("status", 0))
            elif mtype == "http.response.body":
                response_bytes += len(message.get("body") or b"")
            await send(message)

        error_msg: Optional[str] = None
        try:
            await self.app(scope, wrapped_receive, wrapped_send)
        except Exception as e:
            error_msg = f"{type(e).__name__}: {str(e)[:500]}"
            raise
        finally:
            latency_ms = int((time.monotonic() - start) * 1000)
            request_body = b"".join(body_chunks)
            mcp_method, tool_name, tool_args = _parse_jsonrpc(request_body)

            self.log.enqueue(
                {
                    **meta,
                    "mcp_method": mcp_method,
                    "tool_name": tool_name,
                    "tool_args": tool_args,
                    "status_code": status_code,
                    "latency_ms": latency_ms,
                    "error": error_msg,
                    "request_bytes": len(request_body),
                    "response_bytes": response_bytes,
                }
            )
