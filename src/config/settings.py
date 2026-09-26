"""Configuration loaded from environment variables."""

import os

from dotenv import load_dotenv

load_dotenv()


def _bool(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _csv(key: str, default: list[str]) -> list[str]:
    raw = os.getenv(key)
    if not raw:
        return default
    return [s.strip() for s in raw.split(",") if s.strip()]


class Settings:
    # ── Upstream Kapruka API
    api_base_url: str = os.getenv("KAPRUKA_API_BASE_URL", "http://localhost:8080/api/v1")
    api_key: str = os.getenv("KAPRUKA_API_KEY", "")
    # Bearer token for commerce_phase2.jsp (customer-scoped endpoints).
    # Falls back to api_key when unset.
    phase2_api_key: str = os.getenv("KAPRUKA_PHASE2_API_KEY", "")
    # Caller-facing access token for the Phase 2 customer tools. When set,
    # every call to those tools must include a matching `access_token`
    # argument; when unset the tools refuse all calls (fail closed).
    phase2_access_token: str = os.getenv("KAPRUKA_PHASE2_ACCESS_TOKEN", "")
    # Bearer token for commerce_phase3.jsp (custom cakes, bank deposit). The
    # backend issues a separate agent token for phase 3; falls back to the
    # phase-2 then phase-1 keys so a unified token also just works.
    phase3_api_key: str = os.getenv("KAPRUKA_PHASE3_API_KEY", "")
    # Custom-cake tools are "closed MCP": callable only from first-party IPs.
    # Defaults to the trusted-tier list (eagle) so no extra config is needed;
    # override to split the two concerns.
    custom_cake_trusted_ips: list[str] = _csv(
        "CUSTOM_CAKE_TRUSTED_IPS", _csv("RATE_LIMIT_EXEMPT_IPS", [])
    )
    # Visual search (Eagle's caption/embedding search). A PRIVATE tool: hidden
    # from tools/list and callable only from these IPs (default: the trusted
    # tier, i.e. the eagle box). Additional to Doofinder search, never a
    # replacement — kapruka_search_products does not use any of this.
    eagle_vs_url: str = os.getenv("EAGLE_VS_URL", "")
    eagle_vs_api_key: str = os.getenv("EAGLE_VS_API_KEY", "")
    eagle_vs_timeout: float = float(os.getenv("EAGLE_VS_TIMEOUT", "5"))
    visual_search_trusted_ips: list[str] = _csv(
        "VISUAL_SEARCH_TRUSTED_IPS", _csv("RATE_LIMIT_EXEMPT_IPS", [])
    )
    # Cap for images fetched on behalf of the agent (brief: 5 MB before base64).
    custom_cake_image_max_bytes: int = int(
        os.getenv("CUSTOM_CAKE_IMAGE_MAX_BYTES", str(5 * 1024 * 1024))
    )

    # ── MCP server bind
    mcp_host: str = os.getenv("MCP_HOST", "127.0.0.1")
    mcp_port: int = int(os.getenv("MCP_PORT", "3200"))
    log_level: str = os.getenv("LOG_LEVEL", "INFO")

    # ── HTTP / request defaults
    request_timeout: float = float(os.getenv("REQUEST_TIMEOUT", "30"))
    default_page_size: int = 20
    max_page_size: int = 100

    # ── Rate limit (free public tier)
    rate_limit_per_minute: int = int(os.getenv("RATE_LIMIT_PER_MINUTE", "60"))
    rate_limit_enabled: bool = _bool("RATE_LIMIT_ENABLED", True)
    # Tighter, separate hourly cap for kapruka_create_order. Each call spins up
    # a payment session on the upstream backend — abuse posture is different
    # from read tools, so the global per-minute limiter isn't sufficient.
    order_rate_limit_per_hour: int = int(os.getenv("ORDER_RATE_LIMIT_PER_HOUR", "30"))
    # IPs we trust to set X-Real-IP / X-Forwarded-For (i.e. our reverse proxy).
    trusted_proxies: list[str] = _csv("TRUSTED_PROXIES", ["127.0.0.1", "::1"])
    # First-party client IPs (e.g. the eagle-dashboard WhatsApp sales agent)
    # that get the TRUSTED tier instead of the public tier on both limiters.
    # Not a full bypass: trusted callers still hit a (much higher) ceiling so
    # a retry loop on our own boxes can't hammer the service unbounded.
    rate_limit_exempt_ips: list[str] = _csv("RATE_LIMIT_EXEMPT_IPS", [])
    rate_limit_trusted_per_minute: int = int(
        os.getenv("RATE_LIMIT_TRUSTED_PER_MINUTE", "600")
    )
    order_rate_limit_trusted_per_hour: int = int(
        os.getenv("ORDER_RATE_LIMIT_TRUSTED_PER_HOUR", "300")
    )

    # ── MCP DNS-rebinding protection
    # The MCP SDK validates the Host + Origin headers on the streamable-http
    # endpoint. When fronted by Caddy on a public hostname, the public host
    # must be in this list or every request is rejected with 421.
    public_hosts: list[str] = _csv(
        "PUBLIC_HOSTS",
        ["127.0.0.1:*", "localhost:*", "[::1]:*"],
    )
    public_origins: list[str] = _csv(
        "PUBLIC_ORIGINS",
        ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"],
    )
    # DNS-rebinding protection (Host + Origin validation) defends *localhost* MCP
    # servers from malicious web pages. This server is public and fronted by Caddy
    # (which already pins the Host to mcp.kapruka.com), and we WANT browser-based
    # agents from any origin to reach it — otherwise hosted demos get a 403. So
    # allow turning the Host/Origin check off via env. Content-Type validation on
    # POST stays enforced regardless. Defaults to ON so self-hosters stay safe.
    enable_dns_rebinding_protection: bool = (
        os.getenv("ENABLE_DNS_REBINDING_PROTECTION", "true").strip().lower()
        not in ("false", "0", "no", "off")
    )

    # ── Activity logging (Postgres on Eagle)
    # When set, every MCP HTTP request is logged to the mcp_activity table.
    # Leave unset for local dev — logging is silently skipped.
    activity_db_url: str = os.getenv("ACTIVITY_DB_URL", "")


settings = Settings()
