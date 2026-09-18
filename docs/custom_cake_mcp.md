# Kapruka commerce API — custom cakes over chat

**Audience:** MCP server / WhatsApp agent developer. Paste this whole file into Claude Code as the brief for adding the custom cake tools.
**Status:** implemented on the Kapruka side (`web/tools/commerce_phase3.jsp` + `kapruka.customCake.customCakeBean`).
**Base URL (custom_cake_* tools):** `https://www.kapruka.com/tools/commerce_phase3.jsp`
**Auth:** `Authorization: Bearer <PHASE3_AGENT_TOKEN>` — the same phase3 agent token used for `bank_deposit_info` (NOT the phase1/2 token). Closed MCP only; do not expose these tools on the public MCP.
**Ordering** the quoted cake uses the existing phase1 `create_order` (`commerce_phase1.jsp`, phase1 token) — there is no separate cake checkout endpoint.
**All calls:** `POST`, `Content-Type: application/json`, endpoint chosen with `?endpoint=`. Errors use the standard envelope `{"error":{"code","message","details","retryable"}}`.

---

## 1. What this is

The website's "Customized Cakes" page (`/shops/cakes/customCakes/personalise_cakes.jsp`) lets a customer upload a picture of the cake they want and get a price from Kapruka staff. The same flow is now available to the agent. It is a **quote flow, not an instant purchase**:

1. Customer describes the cake + sends a picture -> agent calls `custom_cake_request`.
2. Kapruka staff price it in the admin panel (same queue as website requests). This takes minutes to hours, staff working hours.
3. The customer gets an **SMS** (and an e-mail if one was given) saying the quote is ready. The agent can poll `custom_cake_status`.
4. Customer accepts -> the cake is ordered through the normal order placement: phase1 `create_order` with a custom cake cart line (section 2.4), alone or together with catalogue items -> `checkout_url` (card etc.) and an `order_id` that works with the existing `bank_deposit_info` bank-deposit flow.

The agent **never sets or promises a price**. The price always comes from the staff quote on the server.

---

## 2. Endpoints

### 2.1 `custom_cake_options` — what the customer can choose

Body: `{}`

```json
{
  "available": true,
  "flavours": [{"value":"chocolate","label":"Chocolate"},{"value":"vanilla","label":"Ribbon"},{"value":"coffee","label":"Coffee"}],
  "sizes": ["1 KG","1.5 KG","2 KG","2.5 KG"],
  "icing_colors": ["Vanilla","red","blue","chocolate","black"],
  "delivery_types": ["delivered","pickup"],
  "pickup_locations": [{"id":0,"name":"Kapruka - Mirihana","address":"...","city":"Mirihana","times":"..."}],
  "greeting_min_length": 3, "greeting_max_length": 30,
  "image_formats": ["jpg","png"], "image_max_bytes": 5242880,
  "quote_valid_days": 2
}
```

- Call this instead of hardcoding choices. Show the customer the flavour **label**, send the **value** (the label is also accepted).
- `available: false` means the service is switched off — tell the customer custom cakes are unavailable right now; `custom_cake_request` will answer `503 service_unavailable`.

### 2.2 `custom_cake_request` — submit for a quote

```json
{
  "idempotency_key": "<uuid v4>",
  "customer": { "name": "Nimal Perera", "phone": "+94771234567", "email": "optional@example.com" },
  "flavour": "chocolate",
  "size": "2 KG",
  "greeting": "Happy Birthday Amma",
  "icing_color": "red",
  "delivery_date": "2026-12-24",
  "delivery_type": "delivered",
  "city": "Kandy",
  "image_base64": "<base64 of the JPG/PNG the customer sent>"
}
```

- `customer.phone` — use the customer's WhatsApp number. It is where the quote SMS goes **and it is the key for every later call**.
- `delivery_type: "pickup"` -> send `pickup_location_id` (from options) instead of `city`.
- `city` must be a canonical Kapruka city (phase1 `delivery_cities`); otherwise `422 city_not_deliverable`.
- `greeting` is the text written on the cake, 3-30 characters, required.
- `image_base64` — raw base64 or a `data:image/...;base64,` URL, JPG or PNG, max 5 MB. The MCP/n8n side downloads the WhatsApp media and encodes it; the server does not fetch URLs.
- Retrying with the same `idempotency_key` (within 5 minutes) returns the same `request_id` and does not create a duplicate.

Success:
```json
{ "request_id": "1789714437864476831_customcake", "status": "pending_quote", "image_url": "https://www.kapruka.com/cms_temp/pharmacy/images/....jpg", "message": "..." }
```
**Keep `request_id` in the conversation state.**

### 2.3 `custom_cake_status` — has staff priced it yet?

Body: `{ "request_id": "...", "phone": "+94771234567" }`   (optional `?currency=LKR|USD`, default LKR)

Response (`quote` is present only for `quoted` / `expired`):
```json
{
  "request_id": "1789714437864476831_customcake",
  "status": "quoted",
  "image_url": "https://www.kapruka.com/cms_temp/pharmacy/images/1789714437864476831_customcake.jpg",
  "request": {
    "flavour": "chocolate", "size": "2 KG", "greeting": "Happy Birthday Amma", "icing_color": "red",
    "delivery_type": "delivered", "city": "Kandy", "delivery_address": "NA", "delivery_date": "2026-12-24"
  },
  "quote": {
    "items": [ { "name": "Custom 2KG chocolate cake", "quantity": 1, "total": 12500 }, { "name": "Sugar topper", "quantity": 2, "total": 1501 } ],
    "total": 14001, "currency": "LKR", "instructions": "",
    "quoted_at": "2026-09-18T12:23:57+05:30", "expires_at": "2026-09-20T12:23:57+05:30",
    "quote_url": "https://www.kapruka.com/general/preview_order.jsp?id=1789714437864476831_customcake",
    "delivery_fee_note": "Delivery fee is added when the order is placed (create_order)."
  }
}
```
- `request.delivery_address` is `"NA"` for delivered requests (the address is collected at order time) and `"<pickup name>, <pickup address>"` for pickup requests.

`status` values:
| status | meaning | agent behaviour |
|---|---|---|
| `pending_quote` | staff have not priced it yet | tell the customer it is being reviewed; do not poll more than every few minutes |
| `quoted` | `quote` object present | present items + total, ask whether to go ahead |
| `expired` | quote older than 2 days | offer to submit a new request |

`quote`: `{ "items":[{"name","quantity","total"}], "total": 14001, "currency": "LKR", "instructions": "...", "quoted_at", "expires_at", "quote_url" }`
- `total` is for the cake only — **the delivery fee is added when the order is placed**; say so.
- `instructions` is a note from staff to the customer (may be empty) — relay it.
- `quote_url` is the website page for the same quote; offer it if the customer prefers to pay on the website.
- `404 request_not_found` — wrong id/phone pair, or staff declined/removed the request. Ask the customer to contact Kapruka support or submit again.

### 2.4 Ordering the quoted cake — phase1 `create_order`

`create_order` (`commerce_phase1.jsp`, phase1 token, unchanged request/response) now accepts a second kind of cart line:

```json
"cart": [
  { "custom_cake_request_id": "1789714437864476831_customcake", "phone": "+94771234567" },
  { "product_id": "flowers00T1234", "quantity": 1 }
]
```

- The server turns that line into the quoted cake (name, picture reference and price from the staff quote). The agent never sends a price. Quantity is always 1.
- It can be the only line or mixed with catalogue lines — one order, one delivery fee. Catalogue lines get the usual checks; the whole order must be deliverable to the chosen city.
- `phone` must be the `customer.phone` of the request. Errors for that line: `404 request_not_found`, `409 quote_not_ready`, `410 quote_expired`.
- `delivery.city` / `delivery.date` / `delivery.address` are required as for any `create_order` — reuse `city`, `delivery_date` (and for pickup requests `delivery_address`) from `custom_cake_status` -> `request` unless the customer changed them.
- Response is the normal `create_order` response (`checkout_url`, `order_ref`, `order_id`, `summary`, `expires_at`). Bank deposit: pass `order_id` to `bank_deposit_info` exactly as for catalogue orders.
- `summary.items_total` can differ from the quoted total by a few rupees (the order is stored in USD and converted back) — quote the `summary` numbers when asking for payment.
- The placeholder product `cake00ka001` cannot be ordered with a plain `product_id` line (`404 product_not_found`) — only through its quote.

---

### 2.5 Error codes

Envelope: `{"error":{"code":"...","message":"...","details":{...},"retryable":false}}`. `details.field` names the offending field.

| HTTP | code | where | meaning / what to do |
|---|---|---|---|
| 400 | `missing_field` | all | a required field is absent or malformed (`idempotency_key` must be UUIDv4, `request_id`, `phone`, `customer.name`, `customer.phone`, `city`, `image_base64`) |
| 400 | `invalid_parameter` | request | `flavour` / `size` / `icing_color` not in options, `greeting` not 3-30 chars, `delivery_date` not `YYYY-MM-DD` or in the past, bad `delivery_type` / `pickup_location_id`, picture not a readable JPG/PNG |
| 400 | `invalid_email` | request | `customer.email` given but not an e-mail address |
| 400 | `invalid_currency` | status | `?currency=` other than LKR / USD |
| 401 | `unauthorized` | all | missing / wrong phase3 agent token |
| 404 | `request_not_found` | status, create_order | unknown `request_id`, wrong `phone`, or staff declined/removed the request (same answer on purpose) |
| 409 | `quote_not_ready` | create_order | cake line used before staff priced it |
| 410 | `quote_expired` | create_order | quote older than `quote_valid_days` - submit a new request |
| 413 | `invalid_parameter` | all | body over ~8 MB (keep the picture under 5 MB before base64) |
| 422 | `city_not_deliverable` | request | city not in Kapruka's delivery network - use phase1 `delivery_cities` |
| 503 | `service_unavailable` | request | custom cakes switched off (`available: false` in options) |
| 500 | `internal_error` | all | `retryable: true` - retry once with the SAME `idempotency_key` |

`create_order` also returns its usual errors (`product_not_found`, `product_out_of_stock`, `city_not_deliverable_for_item`, `date_not_deliverable`, ...) for the catalogue lines and delivery details.

---

## 3. Required agent behaviour

- Collect everything before calling `custom_cake_request`: picture, flavour, size, greeting text, icing colour, delivery or pickup, city / pickup location, date, customer name. Read choices from `custom_cake_options`.
- Never invent a price or a price range. Before the quote exists the only honest answer is "our team will send you the price".
- After `custom_cake_request`, tell the customer they will get an SMS when the quote is ready and can also just ask here.
- When the customer comes back, call `custom_cake_status` with the stored `request_id` and their number.
- Do not place the order (`create_order` with the cake line) until the customer has clearly accepted the quoted total.
- The customer can add catalogue items (flowers, chocolates, a card ...) to the same order: search them with the phase1 tools and put them in the same `cart` as the cake line. Offer this once after the quote is accepted; one delivery fee covers the whole order.

---

## 4. Tool definitions to add (MCP side)

| tool | endpoint | notes |
|---|---|---|
| `custom_cake_options` | `custom_cake_options` | no input |
| `custom_cake_request` | `custom_cake_request` | MCP server generates `idempotency_key`; image handed over as base64 by the media pipeline |
| `custom_cake_status` | `custom_cake_status` | `request_id`, `phone` |
| `create_order` (existing, phase1) | `create_order` | extend the cart item schema: either `product_id` (+`quantity`, `icing_text`) or `custom_cake_request_id` + `phone` |

Postman: `docs/postman/Kapruka-Commerce-Custom-Cake.postman_collection.json`.
