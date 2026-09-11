#!/usr/bin/env bash
# Sync local source changes to production and restart the MCP service.
#
# Usage:
#   bash deploy/sync-to-prod.sh
#
# TOPOLOGY (since the 2026-09-11 migration): laptop -> roman@23.111.183.156
# directly, key ~/.ssh/javalounge_newserver_ed25519 (no more Mac-mini double
# hop). roman has NOPASSWD sudo. The app lives in /srv/kapruka-mcp owned by the
# system user `kapruka`; Python is a uv-managed 3.12 under /opt/kapruka-python
# because the unit sets ProtectHome=true (nothing runtime may live under /home).
# Caddy fronts it at mcp.kapruka.com (Cloudflare-proxied, Full strict).
#
# What gets synced: src/, cli.py, pyproject.toml (uv reinstall when changed).

set -euo pipefail

cd "$(dirname "$0")/.."

HOST="${MCP_HOST_SSH:-roman@23.111.183.156}"
KEY="${MCP_SSH_KEY:-$HOME/.ssh/javalounge_newserver_ed25519}"
SSH=(ssh -o BatchMode=yes -i "$KEY" "$HOST")

echo "==== 1/3 ship src/ + cli.py + pyproject.toml ===="
tar --exclude='__pycache__' --exclude='*.pyc' -czf - \
    src/ cli.py pyproject.toml \
  | "${SSH[@]}" 'sudo -n -u kapruka tar -xzf - -C /srv/kapruka-mcp'

echo "==== 2/3 reinstall deps if pyproject.toml changed, then restart ===="
"${SSH[@]}" 'sudo -n bash -s' <<'REMOTE'
set -euo pipefail
cd /srv/kapruka-mcp

marker=".venv/.last_install_hash"
current=$(sha256sum pyproject.toml | cut -d' ' -f1)
previous=$(cat "$marker" 2>/dev/null || echo "")

if [ "$current" != "$previous" ]; then
  echo "deps changed - reinstalling"
  sudo -u kapruka -H env UV_PYTHON_INSTALL_DIR=/opt/kapruka-python \
      UV_CACHE_DIR=/opt/kapruka-python/cache \
      /usr/local/bin/uv pip install --quiet --python .venv/bin/python -e .
  echo "$current" | sudo -u kapruka tee "$marker" >/dev/null
else
  echo "deps unchanged - skipping install"
fi

systemctl restart kapruka-mcp
sleep 5
systemctl is-active kapruka-mcp
REMOTE

echo "==== 3/3 smoke test ===="
"${SSH[@]}" 'curl -fsS -o /dev/null -w "  origin /health -> HTTP %{http_code}\n" http://127.0.0.1:3200/health'

curl -fsS -o /dev/null -w "  https://mcp.kapruka.com/health -> HTTP %{http_code}\n" \
  --max-time 10 https://mcp.kapruka.com/health

echo ""
echo "==== DONE ===="
