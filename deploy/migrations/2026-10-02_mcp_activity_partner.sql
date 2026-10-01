-- Trusted partners (X-Partner-Key): record which partner and which of its
-- customers made each MCP request. Run once on the Eagle Postgres, as the
-- owner of mcp_activity (the MCP's app role is INSERT-only and can't ALTER).
--
-- Safe to run before or after deploying the MCP code: the MCP checks for these
-- columns when its logger starts and only writes them once they exist, so it
-- needs one restart after this runs (sudo systemctl restart kapruka-mcp).
-- Additive only: existing readers of mcp_activity are unaffected.

ALTER TABLE mcp_activity
    ADD COLUMN IF NOT EXISTS partner             text,
    ADD COLUMN IF NOT EXISTS partner_customer_id text;

COMMENT ON COLUMN mcp_activity.partner IS
    'Trusted partner name when the request carried a valid X-Partner-Key (never the key); NULL for public / IP-allowlisted callers.';
COMMENT ON COLUMN mcp_activity.partner_customer_id IS
    'Opaque X-Partner-Customer-Id sent by that partner; NULL otherwise.';
