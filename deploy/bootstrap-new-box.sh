#!/usr/bin/env bash
# One-time bootstrap of a fresh production box. First used 2026-09-11 for
# roman@23.111.183.156 (Ubuntu 22.04, shared with javalounge.lk under Caddy).
#
# Pre-reqs (as roman):
#   - tar/rsync the repo + a real .env + cards/ into $STAGE (this deploy/ dir
#     must be there too — the unit and Caddy snippet are read from it)
#   - uv installed for roman: curl -LsSf https://astral.sh/uv/install.sh | sh
#   - roman has NOPASSWD sudo (/etc/sudoers.d/roman)
#
# Run:   sudo -n bash ./deploy/bootstrap-new-box.sh                 (app only)
#        sudo -n env DO_CADDY=1 bash ./deploy/bootstrap-new-box.sh  (+ Caddy vhost)
# sudo's env_reset STRIPS variables — `DO_CADDY=1 sudo bash ...` silently
# skips the Caddy step. Keep the vhost deferred until DNS points here, or
# Caddy burns failed ACME attempts against Let's Encrypt.
#
# Idempotent: safe to re-run (re-copies code, rebuilds the venv, restarts).
set -euo pipefail

# uv walks UP from the CWD looking for uv.toml; /home/roman is 0750, so a
# `sudo -u kapruka uv ...` started under /home/roman dies with EACCES.
cd /

STAGE=/home/roman/kapruka-mcp-staging
APP=/srv/kapruka-mcp
UV=/home/roman/.local/bin/uv

id kapruka >/dev/null 2>&1 || useradd --system --home-dir "$APP" --shell /usr/sbin/nologin kapruka
install -d -m 755 -o kapruka -g kapruka "$APP"
cp -a "$STAGE/src" "$STAGE/cli.py" "$STAGE/pyproject.toml" "$STAGE/cards" "$STAGE/.env" "$APP/"
chown -R kapruka:kapruka "$APP"; chmod 600 "$APP/.env"

# Python 3.12 must live OUTSIDE /home: the unit uses ProtectHome=true.
install -d -m 755 -o kapruka -g kapruka /opt/kapruka-python
install -m 755 "$UV" /usr/local/bin/uv
UVENV=(env UV_PYTHON_INSTALL_DIR=/opt/kapruka-python UV_CACHE_DIR=/opt/kapruka-python/cache)
(cd "$APP" && sudo -u kapruka -H "${UVENV[@]}" /usr/local/bin/uv python install 3.12)
sudo -u kapruka -H "${UVENV[@]}" bash -c "cd $APP && rm -rf .venv && uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -e ."

install -m 644 "$STAGE/deploy/kapruka-mcp.service" /etc/systemd/system/kapruka-mcp.service
systemctl daemon-reload; systemctl enable --now kapruka-mcp; sleep 6
systemctl is-active kapruka-mcp; curl -fsS http://127.0.0.1:3200/health; echo

[ "${DO_CADDY:-0}" = 1 ] || { echo "Caddy vhost step DEFERRED (run: sudo -n env DO_CADDY=1 bash $0)"; exit 0; }
grep -q 'mcp.kapruka.com {' /etc/caddy/Caddyfile || cat "$STAGE/deploy/Caddyfile.snippet" >> /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile && systemctl reload caddy
echo "INSTALL DONE. Caddy will fetch the LE cert on reload; check: journalctl -u caddy | grep 'certificate obtained'"

# Options-card renderer needs a real TTF (≈ and • glyphs) — Pillow's default face prints boxes.
sudo apt-get install -y fonts-dejavu-core
