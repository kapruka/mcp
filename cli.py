#!/usr/bin/env python3
"""
Kapruka MCP CLI — local testing and development tool.

Usage:
    python cli.py server          # Start the MCP server (HTTP + middleware)
    python cli.py server --stdio  # Start with stdio transport (for MCP Inspector)
    python cli.py ping            # Health-check the upstream Kapruka REST API
    python cli.py tools           # List all registered MCP tools
"""

import argparse
import asyncio
import json
import logging
import sys

from dotenv import load_dotenv

load_dotenv()


def cmd_server(args: argparse.Namespace) -> None:
    if args.stdio:
        from src.server import mcp

        print("[cli] Starting MCP server with stdio transport …", file=sys.stderr)
        mcp.run(transport="stdio")
        return

    from src.server import main as run_http

    run_http()


async def _ping() -> None:
    import httpx

    from src.config.settings import settings

    url = f"{settings.api_base_url.rstrip('/')}/health"
    print(f"[ping] GET {url}")
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(url)
        print(f"[ping] HTTP {r.status_code}")
        try:
            print(json.dumps(r.json(), indent=2))
        except Exception:
            print(r.text[:500])


def cmd_ping(_args: argparse.Namespace) -> None:
    asyncio.run(_ping())


def cmd_tools(_args: argparse.Namespace) -> None:
    from src.server import mcp  # noqa: F401 — imports register tools

    tool_map = mcp._tool_manager._tools if hasattr(mcp, "_tool_manager") else {}
    if not tool_map:
        print("No tools registered yet.")
        return
    print(f"Registered tools ({len(tool_map)}):\n")
    for name, tool in tool_map.items():
        print(f"  • {name}")
        desc = getattr(tool, "description", None) or ""
        first_line = desc.split("\n")[0].strip()
        if first_line:
            print(f"    {first_line}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kapruka-cli",
        description="Kapruka MCP development CLI",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_server = sub.add_parser("server", help="Start the MCP server")
    p_server.add_argument(
        "--stdio", action="store_true", help="Use stdio transport (for MCP Inspector)"
    )
    p_server.set_defaults(func=cmd_server)

    p_ping = sub.add_parser("ping", help="Health-check the Kapruka REST API")
    p_ping.set_defaults(func=cmd_ping)

    p_tools = sub.add_parser("tools", help="List all registered MCP tools")
    p_tools.set_defaults(func=cmd_tools)

    return parser


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
