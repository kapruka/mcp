#!/usr/bin/env bash
# Publish (or update) the Kapruka MCP server entry on the official MCP Registry.
#
# Prereqs (one-time):
#   1. mcp-publisher CLI installed (https://github.com/modelcontextprotocol/registry/releases)
#   2. Ed25519 keypair at $MCP_PUBLISHER_KEY (default: ~/kapruka-mcp-publisher.key.pem)
#   3. Matching `v=MCPv1; k=ed25519; p=<public-key>` TXT record live at the apex of
#      $MCP_PUBLISHER_DOMAIN (default: kapruka.com)
#   4. server.json at the repo root with a bumped `version` since the last publish.
#
# Usage:
#   scripts/publish-registry.sh
#
# Override defaults via env vars:
#   MCP_PUBLISHER_BIN     path to mcp-publisher binary
#   MCP_PUBLISHER_KEY     path to Ed25519 .pem private key
#   MCP_PUBLISHER_DOMAIN  domain whose TXT record holds the public key

set -euo pipefail

# ── Resolve binary ────────────────────────────────────────────────────────────
DEFAULT_BIN_CANDIDATES=(
    "${MCP_PUBLISHER_BIN:-}"
    "$(command -v mcp-publisher 2>/dev/null || true)"
    "$HOME/bin/mcp-publisher/mcp-publisher.exe"
    "$HOME/bin/mcp-publisher/mcp-publisher"
    "/c/Users/${USER:-${USERNAME:-}}/bin/mcp-publisher/mcp-publisher.exe"
)
BIN=""
for c in "${DEFAULT_BIN_CANDIDATES[@]}"; do
    if [[ -n "$c" && -x "$c" ]]; then BIN="$c"; break; fi
done
if [[ -z "$BIN" ]]; then
    echo "ERROR: mcp-publisher binary not found. Set MCP_PUBLISHER_BIN or install it." >&2
    echo "  https://github.com/modelcontextprotocol/registry/releases" >&2
    exit 1
fi

# ── Resolve key path ──────────────────────────────────────────────────────────
KEY_PATH="${MCP_PUBLISHER_KEY:-$HOME/kapruka-mcp-publisher.key.pem}"
if [[ ! -r "$KEY_PATH" ]]; then
    echo "ERROR: private key not readable at $KEY_PATH" >&2
    echo "  Set MCP_PUBLISHER_KEY to override the default location." >&2
    exit 1
fi

DOMAIN="${MCP_PUBLISHER_DOMAIN:-kapruka.com}"

# ── Locate server.json + sanity-check version sync ────────────────────────────
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

if [[ ! -f server.json ]]; then
    echo "ERROR: server.json not found at $REPO_ROOT/server.json" >&2
    exit 1
fi

JSON_VERSION="$(python -c "import json,sys; print(json.load(open('server.json'))['version'])")"
PYPROJECT_VERSION="$(python -c "import re,sys; m=re.search(r'^version\s*=\s*\"([^\"]+)\"', open('pyproject.toml').read(), re.M); print(m.group(1) if m else '')")"

if [[ -n "$PYPROJECT_VERSION" && "$JSON_VERSION" != "$PYPROJECT_VERSION" ]]; then
    echo "WARN: server.json version ($JSON_VERSION) != pyproject.toml version ($PYPROJECT_VERSION)" >&2
    echo "      Bump them together so package + registry stay aligned." >&2
fi

# ── Authenticate ──────────────────────────────────────────────────────────────
echo "── Authenticating against $DOMAIN via DNS ──"
PRIVATE_KEY="$(openssl pkey -in "$KEY_PATH" -noout -text \
                 | grep -A3 'priv:' | tail -n +2 | tr -d ' :\n')"
"$BIN" login dns --domain "$DOMAIN" --private-key "$PRIVATE_KEY"

# ── Publish ───────────────────────────────────────────────────────────────────
echo
echo "── Publishing $REPO_ROOT/server.json (version $JSON_VERSION) ──"
"$BIN" publish

# ── Verify ────────────────────────────────────────────────────────────────────
echo
echo "── Verifying via registry API ──"
SERVER_NAME="$(python -c "import json,sys; print(json.load(open('server.json'))['name'])")"
ENCODED_NAME="$(python -c "import urllib.parse,sys; print(urllib.parse.quote('$SERVER_NAME', safe=''))")"
PYTHONIOENCODING=utf-8 curl -sf "https://registry.modelcontextprotocol.io/v0/servers?search=$ENCODED_NAME" \
    | PYTHONIOENCODING=utf-8 python -c "
import json, sys
data = json.load(sys.stdin)
servers = data.get('servers', [])
if not servers:
    print('  [!] Server not found on registry yet -- try again in 30s')
    sys.exit(1)
for s in servers:
    srv = s['server']
    meta = s['_meta']['io.modelcontextprotocol.registry/official']
    flag = ' (latest)' if meta.get('isLatest') else ''
    print(f\"  [OK] {srv['name']} {srv['version']}{flag} -- {meta['status']}, published {meta['publishedAt']}\")
"
