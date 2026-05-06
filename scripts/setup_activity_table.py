"""One-shot DDL: creates the mcp_activity table on Eagle Postgres.

Run with eagle_root credentials. Idempotent — safe to re-run.
"""

import os
import sys

import psycopg2

DDL = """
CREATE TABLE IF NOT EXISTS mcp_activity (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    client_ip       INET,
    user_agent      TEXT,
    session_id      TEXT,
    mcp_method      TEXT,                  -- e.g. 'tools/call', 'tools/list', 'initialize'
    tool_name       TEXT,                  -- populated when method = 'tools/call'
    tool_args       JSONB,                 -- arguments passed to the tool
    status_code     INT,                   -- HTTP status returned to client
    latency_ms      INT,
    error           TEXT,                  -- non-null when something went wrong
    request_bytes   INT,
    response_bytes  INT,
    forwarded_for   TEXT,                  -- raw X-Forwarded-For chain, for diagnostics
    cf_ray          TEXT,                  -- Cloudflare ray ID if present
    cf_country      TEXT                   -- CF-IPCountry header
);

-- Reporting indexes
CREATE INDEX IF NOT EXISTS mcp_activity_ts_idx           ON mcp_activity (ts DESC);
CREATE INDEX IF NOT EXISTS mcp_activity_client_ip_idx    ON mcp_activity (client_ip);
CREATE INDEX IF NOT EXISTS mcp_activity_tool_ts_idx      ON mcp_activity (tool_name, ts DESC) WHERE tool_name IS NOT NULL;
CREATE INDEX IF NOT EXISTS mcp_activity_session_idx      ON mcp_activity (session_id) WHERE session_id IS NOT NULL;

COMMENT ON TABLE mcp_activity IS
    'Per-request activity log for the Kapruka MCP server. Append-only, used for usage analytics.';

-- Grants for the runtime user
GRANT USAGE  ON SCHEMA public                TO eagle_app;
GRANT INSERT ON mcp_activity                 TO eagle_app;
GRANT USAGE  ON SEQUENCE mcp_activity_id_seq TO eagle_app;
"""


def main() -> None:
    pwd = os.environ.get("EAGLE_ROOT_PASSWORD")
    if not pwd:
        print("Set EAGLE_ROOT_PASSWORD env var first.", file=sys.stderr)
        sys.exit(1)

    conn = psycopg2.connect(
        host="23.111.183.104",
        port=5432,
        dbname="eagle",
        user="eagle_root",
        password=pwd,
        connect_timeout=10,
    )
    conn.autocommit = True
    with conn, conn.cursor() as cur:
        cur.execute(DDL)
        cur.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = 'mcp_activity' ORDER BY ordinal_position"
        )
        cols = cur.fetchall()
        print(f"mcp_activity columns ({len(cols)}):")
        for name, dtype in cols:
            print(f"  - {name:<16} {dtype}")
        cur.execute("SELECT COUNT(*) FROM mcp_activity")
        print(f"\nrow count: {cur.fetchone()[0]}")
    print("\n✓ DDL applied successfully.")


if __name__ == "__main__":
    main()
