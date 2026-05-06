# Deploying kapruka-mcp to production

Target server: `deploy@204.168.201.127` (`reviewguru-prod`, Ubuntu 24.04).
Public hostname: `mcp.kapruka.com` (Caddy → `127.0.0.1:3200` → systemd unit).

## One-time bootstrap

Do these steps once on the server, as the `deploy` user.

### 1. DNS

Point `mcp.kapruka.com` (A record) at `204.168.201.127`. If Cloudflare is in
front, set the proxy to **DNS only** (grey cloud) until Caddy issues the cert,
then you can switch back to proxied if you want.

Verify:

```bash
dig +short mcp.kapruka.com   # → 204.168.201.127
```

### 2. Create the project directory and venv

SSH in, then:

```bash
sudo mkdir -p /srv/kapruka-mcp
sudo chown deploy:deploy /srv/kapruka-mcp
cd /srv/kapruka-mcp

python3 -m venv .venv
.venv/bin/pip install --upgrade pip
```

### 3. First code drop

From your laptop:

```bash
cd ~/mycode/kapruka_mcp
bash deploy/sync-to-prod.sh
```

The first run will fail at the "restart" step because the systemd unit doesn't
exist yet — that's expected. Continue with step 4.

### 4. Drop the env file

On the server:

```bash
cat > /srv/kapruka-mcp/.env <<'EOF'
KAPRUKA_API_BASE_URL=https://www.kapruka.com
KAPRUKA_API_KEY=<the real internal key>
MCP_HOST=127.0.0.1
MCP_PORT=3200
LOG_LEVEL=INFO
RATE_LIMIT_ENABLED=true
RATE_LIMIT_PER_MINUTE=60
TRUSTED_PROXIES=127.0.0.1,::1
EOF
chmod 600 /srv/kapruka-mcp/.env
```

### 5. Install the systemd unit

From your laptop, ship the unit file:

```bash
scp -i ../reviewsguru.lk/.deploy/reviewguru_ed25519 \
    deploy/kapruka-mcp.service \
    deploy@204.168.201.127:/tmp/kapruka-mcp.service
```

On the server:

```bash
sudo mv /tmp/kapruka-mcp.service /etc/systemd/system/kapruka-mcp.service
sudo systemctl daemon-reload
sudo systemctl enable --now kapruka-mcp
sudo systemctl status kapruka-mcp --no-pager
journalctl -u kapruka-mcp -n 30 --no-pager
```

Local smoke test:

```bash
curl -i http://127.0.0.1:3200/health
# expect: HTTP/1.1 200 OK + {"status":"ok"} + RateLimit-* headers
```

### 6. Add the Caddy vhost

From your laptop:

```bash
scp -i ../reviewsguru.lk/.deploy/reviewguru_ed25519 \
    deploy/Caddyfile.snippet \
    deploy@204.168.201.127:/tmp/kapruka-mcp.caddy
```

On the server:

```bash
sudo bash -c 'cat /tmp/kapruka-mcp.caddy >> /etc/caddy/Caddyfile'
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

Caddy will obtain a Let's Encrypt cert on first request to `mcp.kapruka.com`.

Final check from anywhere:

```bash
curl -i https://mcp.kapruka.com/health
```

## Ongoing deploys

Just run from your laptop:

```bash
bash deploy/sync-to-prod.sh
```

This syncs `src/`, `cli.py`, `pyproject.toml`, reinstalls deps if they changed,
restarts the systemd unit, and smoke-tests `/health`.

## Useful operations

```bash
# tail logs
ssh deploy@204.168.201.127 'journalctl -u kapruka-mcp -f'

# cache stats (hits/misses/size)
curl https://mcp.kapruka.com/stats

# restart
ssh deploy@204.168.201.127 'sudo systemctl restart kapruka-mcp'

# stop
ssh deploy@204.168.201.127 'sudo systemctl stop kapruka-mcp'
```

## Connecting from Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`
(or `%APPDATA%\Claude\claude_desktop_config.json` on Windows):

```json
{
  "mcpServers": {
    "kapruka": {
      "url": "https://mcp.kapruka.com/mcp"
    }
  }
}
```

No auth needed for the free public read-only tier (60 req/min per IP).
