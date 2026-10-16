# International gifts: follow-up requests for the commerce API

**To:** Kapruka Java backend (Gayathri)
**From:** the Kapruka MCP maintainers
**Re:** "International gifts for the MCP" (2026-10-13)
**Date:** 2026-10-16

Thank you: the new endpoints are live on production and work as described. We
checked them read-only:
- `gift_countries` returns all 8 countries in USD and LKR.
- `gift_country_products` returns USA 11, Canada 16, Australia 55, UK 10,
  UAE 37, and France, New Zealand and India 0.
- `product` carries the `international_gift` block.
- `delivery_check` refuses store products with `international_gift_product`.

**We are building MCP support now, and none of the items below block us.**
They are gaps that make agents give vaguer or wrong answers. They are listed
by impact. Each one has the evidence and what we would like changed.

---

## 1. Australia products have no description (high)

**Evidence.** `gift_country_products` for Australia returns `summary: ""` for
**all 55** products. Every other country has summaries.

**Why it matters.** Agents choose gifts from the name and the summary. Without
a summary, "Classic Ribbon Cake 500g" and "Golden Marble Cake 500g" can only be
told apart by name. Agents also can't answer basic questions such as "is it
eggless?", "does it serve 10?" or "what's in the hamper?".

**Request.** Fill `summary` (one or two sentences) for the Australian store,
the same as the other stores.

## 2. No product type in the store listing (high)

**Evidence.** `gift_country_products` rows have only `id, name, summary, price,
in_stock, image_url, url`. In `product`, the category is the generic
`intgift`. The real type is only in `attributes.subtype` (e.g. `"Flowers"`).

**Why it matters.** "Flowers for my mum in Dubai" means reading 37 names and
guessing. A `type` lets the agent pick the right section straight away.

**Request.** Add `type` to every `gift_country_products` row, using the
`attributes.subtype` values (Cakes, Flowers, Hampers, Chocolates, Wine…). Also
use it as the `product` category instead of `intgift`.

## 3. `gift_countries` doesn't say which stores are empty (medium)

**Evidence.** France, New Zealand and India are listed with delivery fees, but
`gift_country_products` returns `count: 0` for them. An agent has to call once
per country to find out, or it offers a country it can't sell to.

**Request.** Add `product_count` (in-stock products) to each country in
`gift_countries`. Alternatively, add `open: false` when the count is 0.

## 4. The earliest date isn't published (medium)

**Evidence.** The 3-day rule is only in your note, and in the
`date_not_deliverable` error after an order attempt (`details.earliest_date`).

**Why it matters.** Agents must quote a date before ordering. If the rule
changes, or differs by country (partner holidays, no Sunday delivery), they
will be wrong.

**Request.**
- Add `earliest_date` (YYYY-MM-DD, Sri Lanka time) to each country in
  `gift_countries`.
- If partners don't deliver on certain days (Sundays, public holidays), add
  `blocked_dates` or `delivery_days`, or tell us the rule.

## 5. Zones need postcodes, and zone names should match loosely (medium)

**Evidence.** Australia's zones are 14 Melbourne suburbs, e.g. `Dandenong` and
`Narre Warren`. The UAE's are emirates written as `Abu-Dhabi` and
`Ras al Khaimah`.

**Why it matters.** A customer gives an address, not a zone. "3175 Dandenong
South" or "Abu Dhabi" (no hyphen) has to be matched by hand, and a near miss
becomes `invalid_parameter`.

**Request.**
- Add `postcodes: [...]` to each Australian zone, so a postcode identifies the
  zone, or tell us whether postcodes outside the list are refused.
- In `create_order`, match `delivery.zone` ignoring case, spaces and hyphens,
  so `abu dhabi` = `Abu-Dhabi`.

## 6. `product` describes store products as Sri Lankan deliveries (medium)

**Evidence.** For store products, `product` returns:
- `delivery: {"island_wide": false, "deliverable_city_count": 0, "deliverable_cities": []}`
- `shipping: {"ships_from": "LK", "ships_internationally": false}`

A client that doesn't know about `international_gift` reads this as "delivered
to no cities in Sri Lanka" and "ships from Sri Lanka, not abroad". Both are
wrong.

**Request.** For store products:
- `delivery`: `{"international": true, "country": "Australia"}`, or
  `island_wide: null`. The point is not to claim a Sri Lankan city limit.
- `shipping.ships_from`: the store country, not `LK`.

## 7. Delivery check for international gifts (low; useful later)

**Evidence.** `delivery_check` refuses store products with 422. The only way to
validate a date and zone today is to attempt `create_order`.

**Request.** Optional. Let `delivery_check` take a store product, or add
`gift_delivery_check(country, zone?, date)`, returning:
- `available`, `earliest_date`
- `delivery_fee` (zone-aware)

Agents could then confirm the date and fee before collecting payment details.

## 8. Small data fixes (low)

- **Stray backtick.** UAE product summaries start with a stray backtick, e.g.
  ``"`Offer your loved ones…"`` (`intGift00931`, and others in the UAE store).
- **Auth.** `gift_countries` and `gift_country_products` need the phase-3
  (custom cake) token; the phase-1 key returns `unauthorized`. These are public
  catalogue reads, the same as the website's store pages. If possible, allow
  them with the phase-1 key as well.

---

## Acceptance checks

| # | Check | Expect |
|---|---|---|
| 1 | `gift_country_products` Australia | Every product has a non-empty `summary` |
| 2 | Any store listing | Every row has `type` (Cakes / Flowers / Hampers / …); `product.category` is not `intgift` |
| 3 | `gift_countries` | Each country has `product_count`; France / NZ / India show 0 while empty |
| 4 | `gift_countries` | Each country has `earliest_date` = today + 3 (LK) unless its rule differs |
| 5 | `create_order` Australia, `zone: "dandenong"`; UAE `zone: "abu dhabi"` | Accepted (no `invalid_parameter`) |
| 5 | `gift_countries` Australia | Each zone has `postcodes` |
| 6 | `product` for a store product | No Sri Lankan city limit; `ships_from` is the store country |
| 8 | UAE summaries | No leading backtick |

We will re-run these on production once you tell us they are live. On our side,
the MCP will use any new field as soon as it appears, and works without them
in the meantime.
