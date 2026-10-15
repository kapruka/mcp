# UCP (Universal Commerce Protocol) at Kapruka — Level 1

Kapruka speaks UCP **2026-08-25** (https://ucp.dev/2026-08-25/), so any
UCP-capable shopping agent can search Kapruka's catalogue and build a checkout.
Level 1 takes no payment inside the agent: when a checkout is complete, the
agent hands the buyer to Kapruka's own pay page.

| What | Where |
|---|---|
| Business profile (discovery) | `https://www.kapruka.com/.well-known/ucp`, a static file in `C:\KaprukaShoppingApp\web\.well-known\ucp`. A mirror is served at `https://mcp.kapruka.com/.well-known/ucp`. |
| REST endpoint | `https://mcp.kapruka.com/ucp/v1` |
| MCP endpoint | `https://mcp.kapruka.com/ucp/mcp` (stateless; separate from the Kapruka MCP at `/mcp`) |
| Code | `src/ucp/` (profile, negotiation, catalog, checkout, store, transports) |
| Tests | `tests/test_ucp.py`. Every response is validated against the official schemas in `tests/ucp_schemas/`. |

## Capabilities

- **Catalog search** (`search_catalog`, `POST /catalog/search`): a `query` is
  required, at least 3 characters. `filters.price` is in minor units and is
  applied only when `context.currency` is sent; `filters.categories[0]` is
  used as a category. Prices are what checkout charges, and `list_price` holds
  the pre-discount price.
- **Catalog lookup** (`lookup_catalog`, `get_product`): accepts product IDs or
  variant IDs (`<product>_TC<n>`). `get_product` supports option selection
  (`selected`, `preferences`) and marks each option value with `available` and
  `exists`.
- **Checkout**, with the **Fulfillment** extension: create, get, update,
  complete, cancel.

Not offered at Level 1: Cart, Order, Discount codes, Identity Linking, payment
handlers.

## How a checkout works

1. The agent creates a checkout with `line_items` (`item.id` is a Kapruka
   product or variant ID). A product that has sizes or colours needs a variant
   ID, otherwise the checkout returns `variant_required`.
2. The agent adds `buyer` (first and last name are the sender on the gift card)
   and a shipping destination in `fulfillment.methods[0].destinations[0]`:
   - `street_address`
   - `address_locality`, which must be a Kapruka delivery city or one of its
     aliases (e.g. "Kollupitiya" becomes "Colombo 03")
   - the recipient's `first_name`, `last_name` and `phone_number`
   - `address_country` "LK"

   Missing or wrong fields come back as `recoverable` messages with a `path`.
3. Kapruka returns delivery options in `groups[0].options`:
   - `standard:YYYY-MM-DD`: the earliest open delivery date and the next ones,
     each priced at the fee checkout charges for this cart.
   - `economy`: the cheaper courier. It is offered only when Kapruka allows it
     for the cart and city, the order is in LKR, and it saves at least
     `ECONOMY_MIN_SAVING_LKR` (50). The order is dated 4 days ahead, and the
     option shows Kapruka's wording, e.g. "Deliver within 3 to 5 days; expected
     by Mon 19 Oct". Economy-only carts get only this option.

   The earliest standard date is selected by default. The agent can choose
   another option with `selected_option_id`.
4. Once everything is present, the MCP calls Kapruka's `create_order`.
   - The checkout becomes `requires_escalation` with a `payment_required`
     message (`requires_buyer_input`).
   - `continue_url` is Kapruka's pay page, valid for 60 minutes. A GET after it
     expires mints a fresh link.
   - Totals are Kapruka's: items, delivery, add-ons and total.
5. `complete_checkout` returns the same handoff. No order is placed until the
   buyer pays on kapruka.com; Kapruka then emails the order number.

**Currency.** The checkout is charged in LKR, or in USD when `context.currency`
is USD, GBP, AUD or EUR, or when `context.address_country` is not LK. All
amounts are in minor units, so LKR 2,250 is `225000`.

## Negotiation and safety

- **Profile on every request.** Agents must name their profile on every call:
  the `UCP-Agent: profile="https://…"` header for REST, or
  `meta["ucp-agent"].profile` for MCP.
- **Fetching agent profiles.** We fetch the profile over HTTPS only, without
  following redirects. Every address it resolves to must be public, so private,
  loopback and cloud-metadata addresses are refused. The fetch has a 5 s
  timeout and a 256 KiB limit. Profiles are cached for 60 s–1 h, and failures
  for 60 s. Unknown profiles are fetched at most 60 times a minute in total.
- **Version.** Only `ucp.version` 2026-08-25 is accepted. Any other version
  gets `version_unsupported`.
- **Idempotency.** `complete_checkout` and `cancel_checkout` require an
  idempotency key. A replayed key returns the same answer; reusing a key with a
  different request gets 409.
- **Rate limits.**
  - Kapruka checkout links (each one a `create_order` call) are limited to
    `UCP_MINT_PER_HOUR` (30) per client IP.
  - The server's usual limit of 60 requests a minute per IP also applies.
- **Sessions.** Sessions are kept for 6 hours in `UCP_DB_PATH`
  (`/srv/kapruka-mcp/data/ucp.sqlite3`).

Settings (`.env`): `UCP_ENABLED` (true), `UCP_PUBLIC_BASE`
(https://mcp.kapruka.com), `UCP_DB_PATH`, `UCP_MINT_PER_HOUR`.

## Hosting the profile on kapruka.com (website team)

The UCP spec has strict rules for `/.well-known/ucp`, and agents refuse the
profile if they aren't met.

1. **Serve the file** `web/.well-known/ucp` at
   `https://www.kapruka.com/.well-known/ucp` with HTTP 200.
2. **Content-Type: `application/json`.** The file has no extension, and today
   extensionless files are served with no content type (like
   `apple-app-site-association`).
3. **`Cache-Control: public, max-age=3600`.** It must not be `private`,
   `no-store` or `no-cache`. An `ETag` or `Last-Modified` header is also needed;
   Tomcat already sends both.
4. **No redirects.** UCP forbids 3xx on the profile. Right now
   `https://kapruka.com/...` (the bare domain) returns 301 to `www`. Add a
   Cloudflare exception so `https://kapruka.com/.well-known/ucp` is served
   directly. Either route that path to the origin without the redirect, or
   publish the same JSON on the bare domain.

   Cloudflare is the easiest place to fix 2–4:
   - A Response Header Transform Rule for the URI path
     `/.well-known/ucp`, setting `Content-Type` and `Cache-Control`.
   - An exception to the bare-domain redirect rule for that path.
5. **Keep the copy in sync.** When the profile changes (new capabilities or
   endpoints), regenerate the file with
   `python cli.py ucp-profile > C:\KaprukaShoppingApp\web\.well-known\ucp` and
   deploy it. A test fails if the file and the code disagree.

Check it with:

```bash
curl -sI https://www.kapruka.com/.well-known/ucp
curl -sI https://kapruka.com/.well-known/ucp
```

Both should return `200`, `content-type: application/json` and
`cache-control: public, max-age=3600`.

## Limitations of Level 1

- **No HTTP status on MCP errors.** Over MCP, negotiation and protocol errors
  are JSON-RPC errors with HTTP 200. The spec asks for the matching HTTP status
  as well. REST returns the proper status.
- **No agent authentication.** Agents are not authenticated: no HTTP Message
  Signatures, API keys or OAuth. Onboarding is permissionless, and limits are
  per IP.
- **No gift message.** UCP has no field for one, so orders are created without
  it.
- **No order follow-up.** Paid orders aren't linked back to the UCP checkout:
  no Order capability and no webhooks.

## Level 2 (later)

- **Payment inside the agent:**
  - a payment handler (e.g. Google Pay) that Kapruka's gateway accepts
  - signing keys in `keys[]`
  - a `create_order` variant that takes a payment token
- **Order capability** and signed order webhooks.
- **Identity linking** (OAuth) for Kapruka accounts.
- **Google Merchant Center onboarding:** UCP checkout on Google is US-only
  until 2027 and needs Google's approval.
