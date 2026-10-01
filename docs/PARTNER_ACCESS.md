# Trusted partner access (`X-Partner-Key`)

For first-party services that call the Kapruka MCP (`https://mcp.kapruka.com/mcp`)
from somewhere that **can't be IP-allowlisted** — e.g. **aloka**, Kapruka's web
shopping assistant on Cloudflare Workers, whose outbound IPs are Cloudflare's
shared pool. Eagle keeps using the IP allowlist and needs nothing from this page.

## What a partner key gives you

| | Public | Partner (valid key) |
|---|---|---|
| Requests | 60 / min per IP | 600 / min per partner, 60 / min per customer |
| `kapruka_create_order` | 30 / hour per IP | 300 / hour per partner, 10 / hour per customer |
| Hidden tools | none | the groups granted to you (`visual_search`, `custom_cake`) — listed in `tools/list` and callable |

Limits are configurable per partner. The per-customer limits apply only when you
send a customer ID (below).

## How to call

Send these headers on **every** request, including `initialize`:

```
X-Partner-Key: <your key>
X-Partner-Customer-Id: <opaque id of the end customer>     # optional, recommended
```

- **`X-Partner-Key`** — issued by Kapruka out of band. Keep it server-side (a
  Workers secret), never in browser code.
- **`X-Partner-Customer-Id`** — an opaque, stable ID for the shopper you're acting
  for, e.g. a salted hash of your session/user ID. 1–128 characters from
  `A–Z a–z 0–9 _ -`. Don't send emails or phone numbers. Without it, all your
  traffic shares one partner-wide bucket, so one busy shopper can use up
  everyone's allowance; with it, each shopper also gets their own cap.

```bash
curl -sS https://mcp.kapruka.com/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "User-Agent: aloka/1.0" \
  -H "X-Partner-Key: $KAPRUKA_PARTNER_KEY" \
  -H "X-Partner-Customer-Id: c_7f3a9e" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"aloka","version":"1"}}}' -i
```

To check your key, send it **without** a customer ID: the response carries
`RateLimit-Limit: 600` (your partner cap) instead of the public `60`. With a
customer ID the header reports the tighter of the two caps — the per-customer
one (60 by default) — and `RateLimit-Remaining` counts down for that customer.

## Rules worth knowing

- **A wrong key is not an error.** An invalid, missing or empty key is treated as
  public, with no hint about whether a key exists. If your hidden tools disappear
  or you see `RateLimit-Limit: 60`, check the key.
- **Sessions are bound to the key they were opened with.** After `initialize`
  with key K, every request on that `Mcp-Session-Id` must carry K. No key, a
  wrong key, or even your other (rotated) key gets
  `403 {"error":"session_identity_mismatch"}` — open a new session. A session
  opened *without* a key stays public even if you add a key later.
- **Rotation:** Kapruka can configure two keys for you at once. Switch your
  secret, let old sessions finish (or reopen them), then ask for the old key to
  be removed.
- **Rate limited:** `429` with `Retry-After` and
  `{"error":"rate_limit_exceeded"}` or `{"error":"order_rate_limit_exceeded"}`.
  The message says whether the partner-wide or per-customer cap was hit.
- Hidden tools behave as documented in their own descriptions (e.g.
  `kapruka_visual_search` has no stock or delivery info — confirm with
  `kapruka_get_product`).

## What Kapruka records

Each MCP request is logged with your **partner name** and the
**customer ID** you sent, so your usage can be told apart from other callers.
The key itself is never logged or stored — the server keeps only its SHA-256
hash, and strips the header before any request handling.

---

# Server configuration

| Variable | Example | Meaning |
|---|---|---|
| `PARTNER_KEYS` | `aloka:sha256:<64 hex>` | Who may authenticate. Entries separated by `;` or `,`. `name:sha256:<hex>` stores a hash (recommended); `name:<raw key>` (≥24 chars) also works. Repeat a name for rotation. |
| `PARTNER_SCOPES` | `aloka:visual_search,custom_cake` | Hidden-tool groups per partner; partners separated by `;`. Groups: `visual_search`, `custom_cake`. **Default: none** — a partner with no scopes gets higher limits but no hidden tools. |
| `PARTNER_LIMITS` | `aloka:rpm=600,orders_per_hour=300,customer_rpm=60,customer_orders_per_hour=10` | Per-partner overrides; partners separated by `;`. |
| `PARTNER_RATE_LIMIT_PER_MINUTE` | `600` | Default partner-wide requests/min. |
| `PARTNER_ORDER_RATE_LIMIT_PER_HOUR` | `300` | Default partner-wide create_order/hour. |
| `PARTNER_CUSTOMER_RATE_LIMIT_PER_MINUTE` | `60` | Default per-customer requests/min. |
| `PARTNER_CUSTOMER_ORDER_RATE_LIMIT_PER_HOUR` | `10` | Default per-customer create_order/hour. |

Make a key and its hash (run locally; give the **key** to the partner out of band,
put only the **hash** in `.env`):

```bash
python3 -c "import secrets,hashlib; k=secrets.token_urlsafe(32); print('KEY  (partner):', k); print('HASH (.env):   ', hashlib.sha256(k.encode()).hexdigest())"
```

How it fits together (code): `src/partners.py` (config, constant-time check),
`src/partner_auth.py` (outermost middleware: identity, header stripping, session
binding), `src/private_access.py` (hidden-tool gate = IP allowlist OR partner
scope), `src/middleware.py` + `src/order_rate_limit.py` (partner and
per-customer buckets), `src/activity_log.py` (partner columns),
`src/tools/customers.py` (`tools/list` per partner). Tests:
`tests/test_partner_access.py`.

### Why partners see hidden tools in `tools/list`

Standard MCP clients (including the Cloudflare Workers ones) build the model's
tool list from `tools/list`; a tool the client can't see is a tool its model
can't call unless someone hand-maintains the schema. So a partner's scoped
tools are listed for that partner. The public and IP-allowlisted callers (Eagle)
see exactly the list they saw before and keep calling hidden tools by name.
