#!/usr/bin/env bash
# Sync local source changes to the production server and restart the MCP service.
#
# Usage:
#   bash deploy/sync-to-prod.sh
#
# What gets synced:
#   - src/                    (Python sources)
#   - cli.py                  (entry point)
#   - pyproject.toml          (deps — triggers pip install if changed)
#
# What does NOT sync:
#   - .venv, __pycache__, .pytest_cache
#   - .env (server has its own /srv/kapruka-mcp/.env, not in repo)
#   - tests/, scripts/, deploy/
#
# First-time bootstrap on the server is documented in deploy/README.md.

set -euo pipefail

cd "$(dirname "$0")/.."

SERVER_USER=deploy
SERVER_HOST=204.168.201.127
SERVER_PATH=/srv/kapruka-mcp
KEY=../reviewsguru.lk/.deploy/reviewguru_ed25519

if [ ! -f "$KEY" ]; then
  echo "ERROR: SSH key not found at $KEY" >&2
  echo "Update KEY in this script or copy reviewguru_ed25519 to a known location." >&2
  exit 1
fi

echo "==== 1/3 ship src/ + cli.py + pyproject.toml ===="
tar --exclude='__pycache__' --exclude='*.pyc' -czf - \
    src/ cli.py pyproject.toml \
  | ssh -i "$KEY" "$SERVER_USER@$SERVER_HOST" \
    "cd $SERVER_PATH && tar -xzf -"

echo "==== 2/3 reinstall deps if pyproject.toml changed, then restart ===="
ssh -i "$KEY" "$SERVER_USER@$SERVER_HOST" 'bash -s' <<'REMOTE'
set -euo pipefail
cd /srv/kapruka-mcp

# Has pyproject.toml been touched since last install?
marker=".venv/.last_install_hash"
current=$(sha256sum pyproject.toml | awk '{print $1}')
previous=$(cat "$marker" 2>/dev/null || echo "")

if [ "$current" != "$previous" ]; then
  echo "deps changed — reinstalling"
  .venv/bin/pip install --quiet --upgrade -e .
  echo "$current" > "$marker"
else
  echo "deps unchanged — skipping pip install"
fi

sudo systemctl restart kapruka-mcp
sleep 2
sudo systemctl is-active kapruka-mcp
REMOTE

echo "==== 3/3 smoke test ===="
ssh -i "$KEY" "$SERVER_USER@$SERVER_HOST" \
  'curl -fsS -o /dev/null -w "  /health → HTTP %{http_code}\n" http://127.0.0.1:3200/health'

curl -fsS -o /dev/null -w "  https://mcp.kapruka.com/health → HTTP %{http_code}\n" \
  --max-time 10 https://mcp.kapruka.com/health || \
  echo "  (public URL not reachable yet — DNS/Caddy may still be propagating)"

echo ""
echo "==== DONE ===="
