"""Universal Commerce Protocol (UCP) for Kapruka — Level 1.

Agents (UCP "platforms") discover Kapruka through the business profile at
https://www.kapruka.com/.well-known/ucp (a static copy of profile.business_profile()
hosted by the website) and talk to the endpoints served here:

  REST  https://mcp.kapruka.com/ucp/v1   (transport_rest)
  MCP   https://mcp.kapruka.com/ucp/mcp  (transport_mcp)

Capabilities: Catalog search + lookup, Checkout, and the Fulfillment extension
(standard delivery dates and economy delivery as options). Level 1 takes no
payment: once a checkout has everything Kapruka needs, it moves to
`requires_escalation` and `continue_url` is Kapruka's own pay page (a
create_order checkout link). Payment inside the agent (payment handlers, order
webhooks, identity linking) is Level 2.

Spec: https://ucp.dev/2026-08-25/ (pinned release; see VERSION).
"""

VERSION = "2026-08-25"

STOREFRONT = "https://www.kapruka.com/"
