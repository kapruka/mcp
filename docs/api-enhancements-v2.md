# Kapruka REST API — Enhancements for MCP v2

**Audience:** Kapruka Java backend team
**Owner (MCP side):** dulith@gmail.com
**Status:** Spec — ready for estimation
**Goal:** Enable LLM-powered shopping agents (Claude, ChatGPT, Cursor, etc.) to close real purchase conversations through `mcp.kapruka.com`. Without these, agents can only browse — they can't answer "can I get this delivered to Galle today?" or "show me cakes under Rs.5000."

---

## 1. Conventions (no change from existing endpoints)

- **Transport:** Same JSP gateway — `GET https://www.kapruka.com/tools/commerce_phase1.jsp?endpoint=<name>&...`
- **Auth:** Existing `Authorization: Bearer <key>` header (internal MCP key)
- **Response:** `application/json; charset=utf-8`
- **Errors:** Standard HTTP codes (400 bad request, 404 not found, 422 validation, 429 rate-limit, 5xx server). Body should be JSON: `{"error": {"code": "<machine_code>", "message": "<human>", "suggestions": [...]?}}`. The MCP layer already handles all these.
- **Timezone:** All timestamps and "today" logic must be computed server-side in **Asia/Colombo (UTC+05:30)**. Do not trust MCP-side clock.
- **Currency:** All delivery prices in **LKR** (delivery is intra–Sri Lanka only). Product prices continue to honor the existing `currency=` param.
- **Calendar:** Kapruka delivers every day. Special-occasion shutdowns (very rare) are out of scope for v2 — just assume daily operation.

**Order model assumption (confirmed with product owner):**
> An order is delivered as a single unit at a single rate. Kapruka does not partially fulfill or split shipments. The delivery rate is flat per order, regardless of item count.

---

## 2. New endpoints

### 2.1 `endpoint=delivery_cities` — list of supported delivery cities

**Why:** Lets the MCP layer (and the LLM agent) validate / autocomplete city names before asking for rates. Today the agent has no idea whether "Anuradhapura" or "Anuradapura" or "Anuradhapuraya" is the canonical form.

**Request params:** *(none)*

**Response:**
```json
{
  "cities": [
    { "name": "Colombo",      "aliases": ["Kolomba", "කොළඹ"] },
    { "name": "Anuradhapura", "aliases": ["Anuradapura"] },
    { "name": "Galle",        "aliases": ["ගාල්ල"] },
    { "name": "Kandy",        "aliases": ["මහනුවර"] }
  ]
}
```

**Field rules:**
- `name`: canonical English form, Title Case. Used as the `city=` value for the other delivery endpoints.
- `aliases`: optional list of common misspellings + Sinhala/Tamil names. Empty array if none.

**Recommended cache TTL (MCP side):** 24 hours. Cities change very rarely.

---

### 2.2 `endpoint=delivery_rates` — flat rates for a city

**Why:** Agents need to quote "Rs.500 same-day, Rs.350 parcel" *before* the user picks a product, so they can frame total cost.

**Request params:**
| Param | Type | Required | Notes |
|---|---|---|---|
| `city` | string | yes | Canonical name from `delivery_cities` (case-insensitive match acceptable) |

**Response:**
```json
{
  "city": "Anuradhapura",
  "currency": "LKR",
  "rates": {
    "same_day": { "available": true, "price": 800 },
    "parcel":   { "available": true, "price": 350, "eta_days": 3 }
  }
}
```

**Field rules:**
- `rates.same_day.available`: false if the city has no same-day route at all (e.g. far rural area).
- `rates.parcel.eta_days`: integer max number of days from order placement (you said "within 3 days").
- Prices are **flat per order** (regardless of item count) — see order model assumption in §1.

**Errors:**
- `404` `{"error": {"code": "city_not_found", "message": "Unknown city 'Anaradapura'", "suggestions": ["Anuradhapura"]}}`

**Recommended cache TTL (MCP side):** 6 hours. Rates change infrequently but you may want to bump them seasonally.

---

### 2.3 `endpoint=delivery_check` — "can I get an order to this city today?" feasibility

**Why:** This is the question every shopping agent asks. It combines current Sri Lanka time and the city's same-day cutoff into a single yes/no with the next available date if today is too late.

**Request params:**
| Param | Type | Required | Notes |
|---|---|---|---|
| `city` | string | yes | Canonical city name |
| `mode` | string | optional | `same_day` \| `parcel` \| `any` (default: `any`) |

**Note on perishability:** Backend does **not** need to expose a `perishable` flag per product or accept `product_id` here. The MCP layer heuristically marks products with codes starting `cake*`, `flower*`, or `combo*` as perishable and warns the agent that parcel mode is unreliable for them. This isn't 100% safe, but it covers the bulk of the perishable catalog without backend changes.

**Response:**
```json
{
  "city": "Anuradhapura",
  "now": "2026-04-27T14:23:00+05:30",
  "options": [
    {
      "mode": "same_day",
      "available": false,
      "reason": "Same-day cutoff for Anuradhapura is 11:00 — currently 14:23",
      "next_available_date": "2026-04-28",
      "price": 800,
      "currency": "LKR"
    },
    {
      "mode": "parcel",
      "available": true,
      "earliest_delivery_date": "2026-04-30",
      "price": 350,
      "currency": "LKR"
    }
  ]
}
```

**Field rules per option:**
- `available`: boolean, can-this-mode-deliver-an-order-placed-right-now.
- `reason`: human-readable explanation when `available=false`. Cite the cutoff time and current time.
- `next_available_date` (only when `available=false`): ISO date when this mode will work — typically tomorrow.
- `earliest_delivery_date` (only when `available=true`, parcel mode): ISO date when the buyer can expect arrival (today + `eta_days`).
- For `same_day` when available, no date field is needed (it's today).

**Edge cases:**
- Late-night order (e.g. 23:30): same-day cutoff already passed → `same_day.available=false`, `next_available_date` = tomorrow.
- City with no same-day route: `same_day.available=false`, `reason` cites the absence (no `next_available_date`, since no date will help).

**Errors:**
- `404` `city_not_found` (same as 2.2)

**Recommended cache TTL (MCP side):** 0 (uncached). Real-time — `now` is part of the answer.

---

### 2.4 `endpoint=product_related` — related products for a given product

**Why:** Agents recommend "you might also like…" after a user shows interest. Without this, every recommendation requires a fresh search, which is wasteful and lower quality.

**Request params:**
| Param | Type | Required | Notes |
|---|---|---|---|
| `product_id` | string | yes | The seed product |
| `limit` | int | optional | Default 10, max 20 |
| `currency` | string | optional | Default LKR. Same enum as existing `products_search` |

**Response:** Reuse the **exact same item shape** as `products_search.results[]` so MCP can format identically.

```json
{
  "results": [
    {
      "id": "cake00ka002099",
      "name": "Chocolate Truffle Cake",
      "summary": "...",
      "price": { "amount": 4500, "currency": "LKR" },
      "compare_at_price": null,
      "in_stock": true,
      "stock_level": "high",
      "image_url": "https://www.kapruka.com/...",
      "category": { "id": "cat_cakes", "name": "Cakes", "slug": "cakes" },
      "rating": null,
      "ships_internationally": false,
      "url": "https://www.kapruka.com/..."
    }
  ]
}
```

**Field rules:**
- Algorithm choice is yours — co-purchase, same category + similar price, same vendor, ML recs, anything. Just be consistent.
- If the product has fewer than `limit` related products, return however many exist. Don't pad with random items.
- Return `[]` (200 OK with empty results) for products with no relations, not 404.

**Errors:**
- `404` `product_not_found`

**Recommended cache TTL (MCP side):** 1 hour. Recommendations are stable but should reflect catalog changes within a working day.

---

## 3. Extensions to existing `endpoint=products_search`

Add these query params (all optional, all backwards-compatible):

| Param | Type | Notes |
|---|---|---|
| `min_price` | number | Inclusive. Interpreted in the requested `currency`. |
| `max_price` | number | Inclusive. Interpreted in the requested `currency`. |
| `in_stock_only` | bool | Default `false`. When `true`, exclude out-of-stock items. |
| `sort` | enum | `relevance` (default) \| `price_asc` \| `price_desc` \| `newest` \| `bestseller` |

**Sort definitions to confirm with the team:**
- `relevance`: existing default ranking — leave as-is.
- `price_asc` / `price_desc`: by `price.amount` after FX conversion to the requested currency.
- `newest`: by product listing date (earliest publication on Kapruka.com), descending.
- `bestseller`: by lifetime order count or trailing-30-day order count — your call which is more useful.

**Response:** No shape change. The existing `applied_filters` block should reflect the new filters that were applied:

```json
"applied_filters": {
  "q": "cake",
  "limit": 20,
  "in_stock_only": true,
  "min_price": 1000,
  "max_price": 5000,
  "sort": "price_asc"
}
```

**No change to caching.** The existing 5-minute MCP TTL still applies; the filters become part of the cache key automatically.

---

## 4. Open questions / decisions for the team

1. **Bestseller sort scrape risk.** A naive `sort=bestseller` with deep pagination = "give me your top 1000 SKUs." We've capped MCP search to 3 pages × 20 = 60 results, so this is contained — but flagging in case you want to gate `bestseller` behind a "featured-only" cap (e.g. max 50 results regardless of pagination).

2. **City fuzzy matching.** When a user types "Anaradapura," should the **backend** do fuzzy match and return a 200 with the canonical city in the response, or should it 404 with `suggestions` and let the MCP retry? My recommendation: **404 + suggestions** — keeps responses cacheable and lets the LLM re-prompt the user. But either works.

3. **Same-day cutoff configuration.** Confirmed cutoffs are **per-city** (not per-product, not global). Where should they be stored — DB table `delivery_city_cutoffs`, properties file, or admin-configurable? Backend team's call — just confirm there's a single source of truth that ops can edit.

4. **Rate currency.** Confirmed delivery prices are LKR-only? Some expat-facing flows (gift to a Sri Lankan address, paid by an AUD card) may want delivery quoted in the buyer's currency. For v2 I'd say keep it LKR and let the MCP convert if needed, but want your read.

5. **Versioning.** New endpoints in the same JSP gateway, or a clean break to `/tools/commerce_v2.jsp`? Either is fine for the MCP — same client class either way.

6. **`product_related` algorithm transparency.** Should the response include a `relation_type` field per item (e.g. `"same_category"`, `"same_vendor"`, `"co_purchased"`) so the agent can say *why* it's recommending this? Optional but helps trust.

---

## 5. Out of scope for v2 (flagging so we don't accidentally build them)

- **Reviews / ratings.** We'll add later as `endpoint=product_reviews`. Don't block v2 on this.
- **Order placement / cart.** MCP stays read-only in this phase. Write-side is a separate security review.
- **User auth / personalisation.** No per-user state. All endpoints are anonymous-public.
- **Search suggest / autocomplete.** Agents can just search; not worth a dedicated endpoint yet.
- **Per-product perishable flag on backend.** MCP will heuristically detect via product code prefix (`cake*`, `flower*`, `combo*`) — not 100% accurate but acceptable for v2.
- **Holiday / Poya-day calendar.** Kapruka delivers daily. Special-occasion shutdowns are rare and out of scope.
- **Vendor warehouse / multi-hub fulfillment.** All delivery is treated as Kapruka-fulfilled from a single dispatch model (whole-order, single rate).

---

## 6. Rough delivery order suggestion

If estimation is tight, ship in this order — each is independently useful:

1. `delivery_cities` + `delivery_rates` — unlocks accurate cost quotes (1–2 days)
2. `products_search` filter extensions — biggest UX win for the smallest effort (1 day)
3. `product_related` — agent recommendation quality (2–3 days)
4. `delivery_check` — needs city-cutoff config table + real-time clock logic (2–3 days)

Total estimate: ~1.5 weeks one engineer, less if work is parallelized.
