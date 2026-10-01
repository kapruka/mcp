# Kapruka MCP Server

Python MCP server that wraps the Kapruka.com REST API and exposes it as tools for LLMs and third-party MCP clients.

## Setup

```bash
# 1. Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# 2. Install dependencies
pip install -e ".[dev]"

# 3. Configure environment
cp .env.example .env
# Edit .env with your Kapruka API URL and key
```

## Running

```bash
# Start the MCP server (streamable HTTP, default port 8000)
python cli.py server

# Start with stdio transport (for use with MCP Inspector)
python cli.py server --stdio

# Health-check the Kapruka REST API
python cli.py ping

# List registered MCP tools
python cli.py tools
```

## Testing with MCP Inspector

```bash
npx @modelcontextprotocol/inspector python cli.py server --stdio
```

## Project Structure

```
src/
  server.py        # FastMCP server entry point
  tools/           # One module per tool group (products, orders, …)
  api/
    client.py      # Async httpx client + error handling
  config/
    settings.py    # Env-based configuration
tests/             # pytest test suite
cli.py             # Developer CLI
```

## Trusted partners

First-party callers that can't be IP-allowlisted (e.g. Kapruka's web assistant
on Cloudflare Workers) authenticate with an `X-Partner-Key` header instead, with
optional per-customer limits via `X-Partner-Customer-Id`. Configured with
`PARTNER_KEYS` / `PARTNER_SCOPES` / `PARTNER_LIMITS`. See
[docs/PARTNER_ACCESS.md](docs/PARTNER_ACCESS.md) and the deploy steps in
[deploy/README.md](deploy/README.md#adding-a-trusted-partner).

## Running Tests

```bash
pytest
```
