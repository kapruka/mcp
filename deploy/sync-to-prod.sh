#!/usr/bin/env bash
# Sync local source changes to production and restart the MCP service.
#
# Usage:
#   bash deploy/sync-to-prod.sh
#
# TOPOLOGY (since 2026-06: no direct laptop→prod SSH — the June key rotation
# removed the laptop's key from prod): laptop → Mac mini (Tailscale) → prod.
# The mini's ~/.ssh/config defines the `prod` alias (deploy@204.168.201.127,
# key ~/.ssh/reviewguru_prod). See reviewsguru.lk/DEPLOY.md for the same path.
#
# Restart is sudo-less: the unit runs as User=deploy with Restart=always, so
# killing the process brings it back on new code in ~5s. NOTE the real
# ExecStart is `cli.py server` — NOT `python -m src.server` as the unit file
# in this repo once said — so that's the pkill pattern.
#
# What gets synced: src/, cli.py, pyproject.toml (pip reinstall when changed).

set -euo pipefail

cd "$(dirname "$0")/.."

MINI="${MINI:-dulithherath@100.67.130.56}"

echo "==== 1/3 ship src/ + cli.py + pyproject.toml (via mini) ===="
tar --exclude='__pycache__' --exclude='*.pyc' -czf - \
    src/ cli.py pyproject.toml \
  | ssh "$MINI" "ssh prod 'cd /srv/kapruka-mcp && tar -xzf -'"

echo "==== 2/3 reinstall deps if pyproject.toml changed, then restart ===="
ssh "$MINI" "ssh prod 'bash -s'" <<'REMOTE'
set -euo pipefail
cd /srv/kapruka-mcp

marker=".venv/.last_install_hash"
current=$(sha256sum pyproject.toml | cut -d' ' -f1)
previous=$(cat "$marker" 2>/dev/null || echo "")

if [ "$current" != "$previous" ]; then
  echo "deps changed — reinstalling"
  .venv/bin/pip install --quiet --upgrade -e .
  echo "$current" > "$marker"
else
  echo "deps unchanged — skipping pip install"
fi

# Sudo-less restart: kill the deploy-owned process; systemd Restart=always
# brings it back on the new code. Bracket trick so the pattern can't match
# this ssh session's own command line.
pkill -u deploy -f 'cli\.py serve[r]' || true
sleep 7
systemctl is-active kapruka-mcp
REMOTE

echo "==== 3/3 smoke test ===="
ssh "$MINI" "ssh prod 'curl -fsS -o /dev/null -w \"  /health → HTTP %{http_code}\n\" http://127.0.0.1:3200/health'"

curl -fsS -o /dev/null -w "  https://mcp.kapruka.com/health → HTTP %{http_code}\n" \
  --max-time 10 https://mcp.kapruka.com/health

echo ""
echo "==== DONE ===="
