# Kapruka commerce API (phase1) — delivery-city limits update

**Audience:** MCP server developer. Paste this whole file into Claude Code as the brief for updating the MCP tools/agent.
**Status:** implemented and deployed on the Kapruka side. No token changes — the existing phase1 agent token keeps working.
**Base URL:** `https://www.kapruka.com/tools/commerce_phase1.jsp`  (all calls: `Authorization: Bearer <AGENT_TOKEN>`)

---

## 1. Why this update exists (the bug)

Kapruka delivers most gifts island-wide, but **food / restaurant items, hotel cakes and liquor are only deliverable to a limited set of cities** (typically the Colombo area). Until now every phase1 endpoint checked the city only against the *global* delivery network, so the agent had no way to know a product's limit and would promise e.g. "yes, this food can be delivered to Jaffna". The order would then be unfulfillable.

The API now (a) tells the agent each product's real deliverable-city scope, (b) lets it verify a city for a product before promising, and (c) **hard-rejects** `create_order` when any cart item cannot reach the chosen city. Same rule as the Kapruka website checkout — the whole order ships together, so it is deliverable only to the **intersection** of every item's allowed cities.

---

## 2. What changed — endpoint by endpoint

### 2.1 `GET ?endpoint=product` (and `product_detail`) — NEW `delivery` object

Every product response now includes a `delivery` object:

Island-wide product:
```json
"delivery": { "island_wide": true }
```

City-limited product (food / hotel cake / liquor …):
```json
"delivery": {
  "island_wide": false,
  "deliverable_city_count": 47,
  "deliverable_cities": ["Colombo 01", "Colombo 02", "Colombo 03", "Dehiwala", "Nugegoda", "..."]
}
```

- `deliverable_cities` is capped at **60** names; `deliverable_city_count` is the true total (use it to know whether the list was truncated).
- City names are the canonical Kapruka spellings (same as `delivery_cities`). Match case-insensitively.
- **Not present in `products_search` results** (too expensive to resolve per search hit). For any product you are about to quote delivery on, call `product` (or `delivery_check` with `product_id`) — that is the authority.
- Response is still cacheable (`Cache-Control: public, max-age=120`).

### 2.2 `GET ?endpoint=delivery_check` — NEW optional `product_id` parameter

```
GET ?endpoint=delivery_check&city=Jaffna&delivery_date=2026-09-20&product_id=amrith00100
```

Existing behaviour without `product_id` is **unchanged** (city-in-network + date-block check).

With `product_id`, the item's city limit is folded into the answer:

```json
{
  "city": "Jaffna",
  "now": "2026-09-11T13:40:00+05:30",
  "checked_date": "2026-09-20",
  "available": false,
  "reason": "This item is not delivered to Jaffna.",
  "item_deliverable": false,
  "deliverable_cities": ["Colombo 01", "Colombo 02", "..."],
  "rate": 350,
  "currency": "LKR"
}
```

Rules:
- `available` is now **date-available AND item-deliverable**. It is `false` if *either* fails.
- `reason` = the date-block message when the date is blocked, otherwise `"This item is not delivered to <City>."`. `next_available_date` still appears only for date blocks.
- `item_deliverable` (boolean) appears whenever `product_id` resolved to a real product — even when `available` is `true`.
- `deliverable_cities` (≤60) appears **only when `item_deliverable` is `false`**.
- An unknown `product_id` is silently ignored (no item fields, endpoint behaves as before) — check `item_deliverable` is present before relying on it.

### 2.3 `POST ?endpoint=create_order` — NEW hard rejection `city_not_deliverable_for_item`

If any cart item cannot be delivered to `delivery.city`, the order is **refused** (nothing is created):

```
HTTP 422
{
  "error": {
    "code": "city_not_deliverable_for_item",
    "message": "'Chicken Haryali Kebab' cannot be delivered to Jaffna.",
    "details": {
      "city": "Jaffna",
      "items": [ { "product_id": "amrith00100", "name": "Chicken Haryali Kebab" } ],
      "deliverable_cities": ["Colombo 01", "Colombo 02", "..."],
      "deliverable_city_count": 47
    }
  }
}
```

- `details.items` lists **every** blocking item (product_id + name). `deliverable_cities` (≤40, with the true `deliverable_city_count`) is the city set the *whole cart* can go to (the intersection).
- **Mixed carts:** an island-wide gift + a limited food item to a city only the gift reaches → the whole order is rejected and the food item is named. This mirrors the website checkout.
- Ordering of checks in `create_order`: cart/recipient/delivery validation → city in network (`city_not_deliverable`, unchanged) → date block (`date_not_deliverable`, unchanged) → product stock → **this new check** → pricing/session. So `city_not_deliverable_for_item` is only returned for a valid, in-network city.
- Existing error codes are unchanged: `city_not_deliverable` (422, city not in network at all), `date_not_deliverable` (422), `product_out_of_stock` (409), `product_not_found` (404), etc.

For completeness (already live from an earlier update): the `create_order` success payload includes `"order_id"` alongside `checkout_url` / `order_ref`; it is what the bank-deposit endpoints take.

---

## 3. Required MCP / agent behaviour

Implement these rules in the MCP tools and the agent prompt:

1. **Never promise delivery of a product to a city without checking.** When a customer names a product and a city, call `delivery_check` with **both** `city` and `product_id` (or read `delivery.island_wide` from the `product` lookup you already made). Only say "yes" if `available` is `true`.
2. **Surface the limit proactively.** When presenting a product whose `delivery.island_wide` is `false`, tell the customer it is delivered only to selected cities (Colombo area) before they pick a city — do not wait for the order to fail. If `deliverable_city_count` is larger than the returned list, say "…and more" rather than treating the list as complete.
3. **On `item_deliverable: false` / `available: false`**, offer the customer the `deliverable_cities` (nearest / most likely ones first) or an alternative island-wide product. Do not retry `create_order` with the same city.
4. **Handle `create_order` 422 `city_not_deliverable_for_item` gracefully:** read `details.items` to tell the customer *which* item blocks the order, and `details.deliverable_cities` to suggest a city. Offer to (a) change the delivery city, or (b) remove/replace the blocking item. Never place a partial order silently — the API will not, and the agent should not pretend it did.
5. **Search results carry no delivery info.** Do not infer deliverability from `products_search`; resolve it via `product` / `delivery_check` for the specific item.
6. City matching is case-insensitive on the API side, but always send the canonical spelling from `delivery_cities` / `deliverable_cities` when you can (avoids `city_not_found` suggestions loops).

---

## 4. Test data (real products on kapruka.com)

| Product | Type | Expected |
|---|---|---|
| `amrith00100` | restaurant food | `delivery.island_wide=false`, 47 cities, **Jaffna NOT included**; `delivery_check … city=Jaffna&product_id=amrith00100` → `available:false`, `item_deliverable:false`; `create_order` to Jaffna → 422 `city_not_deliverable_for_item`; same item to `Colombo 03` → OK |
| `dinemore00100` | restaurant food | as above, 46 cities |
| `cake00ka001846` | standard Kapruka cake | `delivery.island_wide=true`; `delivery_check … city=Jaffna&product_id=cake00ka001846` → `item_deliverable:true`; `create_order` to Jaffna → OK |
| `cake00ka001846` + `amrith00100` in one cart, city Jaffna | mixed | 422, `details.items` names `amrith00100` only |

Note: some restaurant items are seasonal — if `create_order` returns `product_out_of_stock` for a test item, that check runs *before* the city check; pick another item from the same vendor.

Sample calls:
```bash
curl -s "https://www.kapruka.com/tools/commerce_phase1.jsp?endpoint=product&product_id=amrith00100" \
  -H "Authorization: Bearer <AGENT_TOKEN>"

curl -s "https://www.kapruka.com/tools/commerce_phase1.jsp?endpoint=delivery_check&city=Jaffna&product_id=amrith00100" \
  -H "Authorization: Bearer <AGENT_TOKEN>"
```

---

## 5. Task for Claude Code (MCP side)

1. Extend the `product` / `product_detail` tool result schema with the `delivery` object (`island_wide`, optional `deliverable_city_count`, `deliverable_cities[]`).
2. Add the optional `product_id` argument to the `delivery_check` tool and expose `item_deliverable` / `deliverable_cities` in its result.
3. Add `city_not_deliverable_for_item` to the `create_order` tool's error handling, parsing `details.items` and `details.deliverable_cities`.
4. Update the agent system prompt with the behaviour rules in section 3.
5. Add the section-4 cases to the MCP test suite.
