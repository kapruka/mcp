# Kapruka MCP — Activity Data Handover for Eagle Dash

**Audience:** Eagle reporting dashboard project
**Source system:** Kapruka MCP server at `https://mcp.kapruka.com`
**Updated:** 2026-05-06

This document is everything the dash project needs to build usage reports for the Kapruka MCP. Start at §1 (connection), then §3 (schema). §5 has copy-paste SQL for the reports we already know we want.

---

## 1. Connection

| Field | Value |
|---|---|
| Host | `23.111.183.104` |
| Port | `5432` |
| Database | `eagle` |
| User | `mcp_reports` |
| Password | **Not in this repo** — request from the MCP server owner (see §6) |
| SSL | **Required** (`sslmode=require`, no client cert) |

**Connection string template:**
```
postgresql://mcp_reports:<URL-ENCODED-PASSWORD>@23.111.183.104:5432/eagle?sslmode=require
```
URL-encode the password: `!` → `%21`, `#` → `%23`, `@` → `%40`, etc.

**Permissions:** `mcp_reports` is **SELECT-only** on `mcp_activity`. INSERT/UPDATE/DELETE are blocked at the role level — safe to use directly from the dashboard backend without a write firewall.

**Rotation:** Treat the password as production credentials. If it leaks, rotate via:
```sql
ALTER ROLE mcp_reports WITH PASSWORD '<new>';
```
Run as `eagle_root`.

---

## 2. What gets logged

One row per HTTP request to the MCP endpoint (`/mcp`). That includes:

- `initialize` — MCP session handshake (1 per new client connection)
- `notifications/initialized` — sent by the client right after initialize
- `tools/list` — client asking what tools exist
- `tools/call` — actual tool invocations (the high-value rows)
- Anything else MCP clients send (`ping`, `resources/list`, etc.)

**What is NOT logged:**
- Static asset hits (`/`, `/health`, `/stats`, `/robots.txt`) — middleware bypasses them
- Tool **responses** (we capture size in bytes, but not the response body)
- Anything if the Postgres pool is unreachable (logger silently no-ops; MCP keeps serving)

**Logging is async + best-effort.** Entries go through a bounded in-memory queue (max 2000) drained by a background task. Under sustained DB outage, oldest entries are dropped — so don't expect 100% completeness during incidents. Expect >99.9% in normal operation.

---

## 3. Table schema

```sql
CREATE TABLE mcp_activity (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    client_ip       INET,
    user_agent      TEXT,
    session_id      TEXT,
    mcp_method      TEXT,
    tool_name       TEXT,
    tool_args       JSONB,
    status_code     INT,
    latency_ms      INT,
    error           TEXT,
    request_bytes   INT,
    response_bytes  INT,
    forwarded_for   TEXT,
    cf_ray          TEXT,
    cf_country      TEXT
);
```

### Column-by-column

| Column | Type | What it means | Example | Notes |
|---|---|---|---|---|
| `id` | bigint | Surrogate PK, monotonically increasing | `42` | Use this for cursor pagination, not `ts`. |
| `ts` | timestamptz | When the request was logged. Stored UTC. | `2026-05-06 12:16:18.449+00` | Convert to `Asia/Colombo` for SL-local reports: `ts AT TIME ZONE 'Asia/Colombo'` |
| `client_ip` | inet | The real end-user IP. | `124.43.8.160` | Resolved with priority: `CF-Connecting-IP` → `X-Real-IP` → `X-Forwarded-For[0]` → ASGI peer. CF-Connecting-IP is set by Cloudflare and can't be spoofed through CF, so this is reliable for traffic that came through `mcp.kapruka.com`. |
| `user_agent` | text | Raw `User-Agent` header. | `python-httpx/0.28.1`, `Claude/2.0 (Mac)` | Tells you which MCP client family is hitting the server — Claude Desktop, ChatGPT, Cursor, custom SDK, scrapers. |
| `session_id` | text | MCP session id from the `Mcp-Session-Id` header. | `8c337adaf79b4b18a313b9d281f0d1e1` | One value per client connection. Use to stitch multi-call user journeys. NULL on the very first `initialize` call (issued in the response). |
| `mcp_method` | text | MCP JSON-RPC method name from the request body. | `tools/call`, `tools/list`, `initialize`, `notifications/initialized`, `ping` | NULL when body wasn't JSON or wasn't a single object. Value `batch` for JSON-RPC batches. |
| `tool_name` | text | Populated only when `mcp_method = 'tools/call'`. The specific tool invoked. | `kapruka_search_products`, `kapruka_check_delivery` | One of: `kapruka_search_products`, `kapruka_get_product`, `kapruka_list_categories`, `kapruka_list_delivery_cities`, `kapruka_check_delivery`. May grow over time. |
| `tool_args` | jsonb | The `arguments` object the agent passed. | `{"params":{"q":"birthday cake","limit":10}}` | Truncated at 4 KiB; in that case the value is `{"_truncated":true,"preview":"..."}`. Use JSONB operators (`->>`) to slice: e.g. `tool_args->'params'->>'q'` for the search query. |
| `status_code` | int | HTTP status code returned to the client. | `200`, `202`, `400`, `429`, `500` | `202` is normal for `notifications/*`. `429` = rate limited. `400` = bad input. `5xx` = server or upstream error. |
| `latency_ms` | int | Wall-clock time from request start to last response byte. | `460`, `1170` | Includes upstream Kapruka API call + cache lookup + JSON serialization. Cached responses are typically <50 ms; cold cache is 100–2000 ms depending on tool. |
| `error` | text | Exception type + message if an unhandled exception bubbled up. | `KaprukaAPIError: Unparseable response …` | Usually NULL. Non-NULL means our code crashed — separate from upstream-API errors that get caught and returned as 200 + error string in the result. Check `status_code >= 500` AND `error IS NOT NULL` for hard failures. |
| `request_bytes` | int | Size of request body sent by client. | `156`, `1024` | Useful for spotting abusive payload sizes. |
| `response_bytes` | int | Size of response body sent back. | `2400`, `15000` | Big responses = expensive search results or many products. |
| `forwarded_for` | text | Raw `X-Forwarded-For` header chain — for diagnostics. | `124.43.8.160, 172.70.92.219` | Don't use this for "who is the user" — `client_ip` already resolved that. Keep this column for debugging the proxy chain. |
| `cf_ray` | text | Cloudflare ray ID. | `9f7693e0292b4020-SIN` | Suffix indicates CF colo (e.g. `-SIN` = Singapore, `-CMB` = Colombo, `-LHR` = London). Useful for geo distribution by colo. |
| `cf_country` | text | ISO-2 country code from `CF-IPCountry`. | `LK`, `US`, `AU`, `GB` | Cloudflare's geolocation of the client IP. **Use this for geo reports, not GeoIP-on-ip-yourself** — CF's accuracy is well above public maxmind. |

### Indexes available

```sql
mcp_activity_ts_idx        (ts DESC)
mcp_activity_client_ip_idx (client_ip)
mcp_activity_tool_ts_idx   (tool_name, ts DESC) WHERE tool_name IS NOT NULL
mcp_activity_session_idx   (session_id)         WHERE session_id IS NOT NULL
```

These cover the obvious access patterns (recent activity, per-IP lookups, per-tool trends, per-session journeys). If a particular dashboard query is slow, add a partial index — coordinate with the Kapruka MCP owner.

---

## 4. Quirks and gotchas

1. **`client_ip` for non-CF traffic.** If anyone hits the origin directly (`204.168.201.127`) bypassing `mcp.kapruka.com`, `CF-Connecting-IP` is absent and `client_ip` falls back to whatever Caddy passed (likely the immediate peer). For the public dashboard, treat IPs without `cf_ray` as "non-CF" and exclude from geo reports if you want clean data.

2. **`tool_args` may be truncated.** Anything over 4 KiB (rare but possible for huge inputs) is replaced with `{"_truncated": true, "preview": "<first 1000 chars>"}`. Filter with `tool_args ? '_truncated'` to spot them.

3. **Rate-limited requests are still logged.** `status_code = 429` rows mean the client was rate-limited at the Python layer (60/min per IP). They're useful — they show abuse patterns. They will NOT have a `tool_name` because the request never reached the MCP dispatch.

4. **`session_id` is NULL for the very first `initialize` request** because the server issues the session id in the response. Subsequent requests in the same session carry it. Don't `GROUP BY session_id` without filtering NULL — or you'll bucket all initial connections together.

5. **Multiple sessions per IP.** A single user can open many sessions (each connection = new session id). For "who is using this," `client_ip` is the user-level grain; `session_id` is the conversation-level grain.

6. **`user_agent` is not a unique client fingerprint.** Many MCP clients send the same SDK UA (`python-httpx/0.x` is common). Don't assume same UA = same person.

7. **Backfill / retention.** No retention policy is configured today. Table is append-only. Coordinate with Kapruka MCP owner if you want a TTL (e.g. drop rows older than 12 months).

8. **Schema may grow.** Columns will be added (never removed or renamed without notice). Write your queries with explicit column lists, not `SELECT *`, so dashboards don't break when new columns appear.

---

## 5. Starter queries for common reports

### Traffic volume — daily requests, last 30 days
```sql
SELECT
  date_trunc('day', ts AT TIME ZONE 'Asia/Colombo')::date AS day,
  COUNT(*) AS requests,
  COUNT(*) FILTER (WHERE mcp_method = 'tools/call') AS tool_calls,
  COUNT(DISTINCT client_ip) AS unique_ips,
  COUNT(DISTINCT session_id) AS sessions
FROM mcp_activity
WHERE ts >= NOW() - INTERVAL '30 days'
GROUP BY 1
ORDER BY 1;
```

### Tool popularity — last 7 days
```sql
SELECT
  tool_name,
  COUNT(*) AS calls,
  COUNT(DISTINCT client_ip) AS unique_users,
  ROUND(AVG(latency_ms))::int AS avg_ms,
  PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY latency_ms)::int AS p95_ms
FROM mcp_activity
WHERE ts >= NOW() - INTERVAL '7 days'
  AND tool_name IS NOT NULL
GROUP BY tool_name
ORDER BY calls DESC;
```

### Top users by IP — last 24 hours
```sql
SELECT
  client_ip,
  cf_country,
  COUNT(*) AS requests,
  COUNT(DISTINCT session_id) AS sessions,
  COUNT(DISTINCT tool_name) FILTER (WHERE tool_name IS NOT NULL) AS distinct_tools,
  MIN(ts) AS first_seen,
  MAX(ts) AS last_seen
FROM mcp_activity
WHERE ts >= NOW() - INTERVAL '24 hours'
GROUP BY 1, 2
ORDER BY requests DESC
LIMIT 50;
```

### Geo distribution — last 30 days
```sql
SELECT
  cf_country,
  COUNT(DISTINCT client_ip) AS unique_users,
  COUNT(*) AS requests
FROM mcp_activity
WHERE ts >= NOW() - INTERVAL '30 days'
  AND cf_country IS NOT NULL
GROUP BY 1
ORDER BY unique_users DESC;
```

### Hourly heatmap — when do people use the MCP (Asia/Colombo time)
```sql
SELECT
  EXTRACT(DOW  FROM ts AT TIME ZONE 'Asia/Colombo')::int AS dow,    -- 0 = Sunday
  EXTRACT(HOUR FROM ts AT TIME ZONE 'Asia/Colombo')::int AS hour,
  COUNT(*) AS requests
FROM mcp_activity
WHERE ts >= NOW() - INTERVAL '14 days'
  AND mcp_method = 'tools/call'
GROUP BY 1, 2
ORDER BY 1, 2;
```

### Top search queries — what are people looking for?
```sql
SELECT
  LOWER(tool_args -> 'params' ->> 'q') AS query,
  COUNT(*) AS searches,
  COUNT(DISTINCT client_ip) AS searchers
FROM mcp_activity
WHERE tool_name = 'kapruka_search_products'
  AND ts >= NOW() - INTERVAL '30 days'
  AND tool_args -> 'params' ->> 'q' IS NOT NULL
GROUP BY 1
ORDER BY searches DESC
LIMIT 50;
```

### Most-checked delivery cities
```sql
SELECT
  tool_args -> 'params' ->> 'city' AS city,
  COUNT(*) AS checks,
  COUNT(DISTINCT client_ip) AS unique_users
FROM mcp_activity
WHERE tool_name = 'kapruka_check_delivery'
  AND ts >= NOW() - INTERVAL '30 days'
GROUP BY 1
ORDER BY checks DESC
LIMIT 30;
```

### Client UA breakdown — which LLM platforms?
```sql
SELECT
  CASE
    WHEN user_agent LIKE 'Claude%'  THEN 'Claude'
    WHEN user_agent LIKE '%ChatGPT%' OR user_agent LIKE '%openai%' THEN 'ChatGPT'
    WHEN user_agent LIKE '%cursor%' THEN 'Cursor'
    WHEN user_agent LIKE 'python-httpx%' THEN 'python-httpx (custom client)'
    WHEN user_agent LIKE 'Mozilla%' THEN 'Browser'
    ELSE COALESCE(user_agent, '<unknown>')
  END AS client_family,
  COUNT(*) AS requests,
  COUNT(DISTINCT client_ip) AS unique_users
FROM mcp_activity
WHERE ts >= NOW() - INTERVAL '30 days'
GROUP BY 1
ORDER BY requests DESC;
```

### Error rate per tool
```sql
SELECT
  tool_name,
  COUNT(*) AS calls,
  COUNT(*) FILTER (WHERE status_code >= 400) AS http_errors,
  COUNT(*) FILTER (WHERE error IS NOT NULL)  AS exceptions,
  ROUND(100.0 * COUNT(*) FILTER (WHERE status_code >= 400 OR error IS NOT NULL)
              / NULLIF(COUNT(*), 0), 2) AS error_pct
FROM mcp_activity
WHERE tool_name IS NOT NULL
  AND ts >= NOW() - INTERVAL '7 days'
GROUP BY 1
ORDER BY calls DESC;
```

### Rate-limited IPs (potential abuse)
```sql
SELECT
  client_ip,
  cf_country,
  COUNT(*) AS rate_limited_hits,
  MIN(ts) AS first_429,
  MAX(ts) AS last_429
FROM mcp_activity
WHERE status_code = 429
  AND ts >= NOW() - INTERVAL '7 days'
GROUP BY 1, 2
HAVING COUNT(*) >= 5
ORDER BY rate_limited_hits DESC;
```

### Session-level analytics — average session length & calls per session
```sql
WITH sessions AS (
  SELECT
    session_id,
    client_ip,
    cf_country,
    MIN(ts) AS started,
    MAX(ts) AS ended,
    COUNT(*) AS calls
  FROM mcp_activity
  WHERE session_id IS NOT NULL
    AND ts >= NOW() - INTERVAL '7 days'
  GROUP BY 1, 2, 3
)
SELECT
  COUNT(*) AS sessions,
  ROUND(AVG(EXTRACT(EPOCH FROM (ended - started))))::int AS avg_seconds,
  ROUND(AVG(calls), 1) AS avg_calls_per_session,
  PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY calls) AS p95_calls
FROM sessions;
```

---

## 6. Contact

- **MCP server owner:** Dulith — `dulith@gmail.com`
- **Postgres host owner:** Eagle infra
- **Source code:** `mcp.kapruka.com` (private repo)
- **Public surface:** `https://mcp.kapruka.com` (landing page lists current tools)

If you need:
- A new column added to support a specific report → ping the MCP owner
- A retention policy → both teams should agree on a TTL before adding any DELETE job
- More throughput / reduced sampling → tell us; right now we log 100% of requests
