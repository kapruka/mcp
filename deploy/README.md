# Deploying kapruka-mcp to production

Target server: `roman@23.111.183.156` (hostname `kp`, Ubuntu 22.04, 1 vCPU /
3.7 GB — shared with javalounge.lk under the same Caddy). Migrated here from
`204.168.201.127` on 2026-09-11.
Public hostname: `mcp.kapruka.com` (Cloudflare → Caddy → `127.0.0.1:3200` →
systemd unit `kapruka-mcp`, running as the system user `kapruka`).

SSH: `ssh -i ~/.ssh/javalounge_newserver_ed25519 roman@23.111.183.156`
(key-only; `roman` has NOPASSWD sudo).

## Layout on the box

| What | Where |
|---|---|
| App (src/, cli.py, pyproject.toml, cards/, .env) | `/srv/kapruka-mcp` (owner `kapruka`, .env 0600) |
| venv | `/srv/kapruka-mcp/.venv` (uv-managed CPython 3.12) |
| Python toolchain + uv cache | `/opt/kapruka-python` — must be outside `/home` (unit has `ProtectHome=true`) |
| uv binary | `/usr/local/bin/uv` |
| Card font | `fonts-dejavu-core` (apt) — without it Pillow's default face has no ≈ / • and the options cards print boxes (bit 2026-09-19 on this box) |
| Unit | `/etc/systemd/system/kapruka-mcp.service` (= `deploy/kapruka-mcp.service`) |
| Caddy | single `/etc/caddy/Caddyfile`; our vhost = `deploy/Caddyfile.snippet` |
| Staging copy used by bootstrap | `/home/roman/kapruka-mcp-staging` |

## Ongoing deploys

From your laptop (Git Bash on Windows):

```bash
bash deploy/sync-to-prod.sh
```

Ships `src/`, `cli.py`, `pyproject.toml` straight to the box (no jump host),
reinstalls deps with uv when `pyproject.toml` changed, restarts the unit, and
smoke-tests `/health` on the origin and via `https://mcp.kapruka.com`.

After any MCP-visible change also follow the registry checklist (well-known,
landing page, llms.txt, version bump, `scripts/publish-registry.sh`).

## One-time bootstrap of a fresh box

1. Stage the repo, a real `.env`, and `cards/` under
   `/home/roman/kapruka-mcp-staging` (this `deploy/` dir included).
2. Install uv for roman: `curl -LsSf https://astral.sh/uv/install.sh | sh`.
3. `sudo -n bash ./deploy/bootstrap-new-box.sh` — creates the `kapruka` user,
   installs Python 3.12 + venv, installs and starts the unit. The Caddy vhost
   is **deferred** so no ACME attempts fire before DNS moves.
4. Flip `mcp.kapruka.com` in Cloudflare to the new IP, then
   `sudo -n env DO_CADDY=1 bash ./deploy/bootstrap-new-box.sh`
   (`env` is required — sudo strips variables otherwise).
5. Verify: `curl -i https://mcp.kapruka.com/health`, an `initialize` call
   reports the new box's `serverInfo.version`, and
   `journalctl -u caddy | grep 'certificate obtained'`.

## .env keys

`KAPRUKA_API_BASE_URL`, `KAPRUKA_API_KEY`, `KAPRUKA_PHASE2_API_KEY`,
`KAPRUKA_PHASE2_ACCESS_TOKEN`, `MCP_HOST=127.0.0.1`, `MCP_PORT=3200`,
`LOG_LEVEL`, `PUBLIC_HOSTS`, `PUBLIC_ORIGINS`,
`ENABLE_DNS_REBINDING_PROTECTION=false`, `RATE_LIMIT_ENABLED`,
`RATE_LIMIT_PER_MINUTE`, `RATE_LIMIT_EXEMPT_IPS` (trusted tier, e.g. the
eagle-dashboard box), `TRUSTED_PROXIES=127.0.0.1,::1`, `ACTIVITY_DB_URL`
(Postgres on eagle). See `src/config/settings.py` for defaults.

## Useful operations

```bash
SSH='ssh -i ~/.ssh/javalounge_newserver_ed25519 roman@23.111.183.156'

$SSH 'sudo -n journalctl -u kapruka-mcp -f'          # tail logs
$SSH 'sudo -n systemctl restart kapruka-mcp'          # restart
$SSH 'sudo -n systemctl stop kapruka-mcp'             # stop
curl https://mcp.kapruka.com/stats                     # cache stats
```

## Connecting from Claude Desktop

```json
{
  "mcpServers": {
    "kapruka": { "url": "https://mcp.kapruka.com/mcp" }
  }
}
```

No auth needed for the free public read-only tier (60 req/min per IP).
