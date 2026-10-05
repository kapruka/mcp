# Fault: the commerce API ignores website discounts, so agent orders charge full price

> **Resolved 2026-10-05.** Kapruka (Gayathri) fixed it the same day: the API now
> calls the cart's own discount method (`productPromotionsBean.getDiscountDetails`)
> wherever it prices a product. Verified on production: product/variants/USD,
> search bounds, the `delivery_check` fee value and `create_order` all use the
> discounted price. Their answer to the question below: the cart's number is
> authoritative; the feed `sale_price` is a Doofinder figure with different
> rounding, so treat it as approximate. MCP 0.6.1 shows the discount (was-price
> and % off) and reprices visual search's on-sale rows from the API.
> `discount_percent` / `discount_ends_at` are not in the API yet; corporate-account
> discounts are deliberately not applied (agent calls carry no company profile).
> Unpaid `create_order` sessions just expire, so the test order needs no cancelling.

**To the Kapruka API team. From the MCP side, 2026-10-05.** Everything below was
measured on production that day. All calls are read-only except one test checkout,
which was left unpaid and is marked "TEST – do not fulfil".

## In one paragraph

When a product is on sale on kapruka.com, the product page shows the discounted
price (e.g. **RS.3,250 → RS.2,250**) and the website cart charges it. The commerce
API (`commerce_phase1.jsp`) does not know about the discount. `product` and
`products_search` return the full price with `compare_at_price: null`, and
**`create_order` charges the full price**. So every customer ordering a discounted
product through an AI agent (Aloka on the web, Afzal on WhatsApp, the public MCP)
pays more than a customer on the website. For the bear below, that's **LKR 1,000
(44%) more**.

## Severity

- **724 products are on sale right now**, according to Kapruka's own product feed
  (the daily feed mirror on Eagle, refreshed 2026-10-05 02:01 UTC: 22,751 products,
  724 with `sale_price < price`). The average discount is 21.4%; the largest is 82%.
  Most are in Clothing (341), Electronics (95), Kitchen Appliances (54) and Home And
  Lifestyle (51).
- Their combined discount is **LKR 1,247,122** (one unit of each).
- **Orders through the MCP in the last 30 days:** 1,230 `create_order` checkouts
  (Afzal 1,117, public 90, Aloka 23). **49 order lines were for products that are
  discounted today.** If paid, those lines carried **LKR 44,135** in overcharges
  (Afzal 44 lines, LKR 33,654; Aloka 3, LKR 3,700; public 2, LKR 6,781, which
  includes our one test). This is an estimate: it uses today's sale prices, and not
  every checkout is paid.
- The overcharge is visible to the customer. Kapruka's own visual search already
  knows the sale price, so an agent can quote "LKR 2,250 (was 3,250)" and then send
  a payment link for LKR 3,250. That's a trust problem as well as a money one.

## Evidence

Three discounted products, each checked four ways:

| Product | Website `#pricelbl` | Website `#priceAfterDiscountlbl` | Feed `sale_price` | API `product` price | API `compare_at_price` |
|---|---|---|---|---|---|
| `EF_PC_SOFT0V2686POD00003` Breathing Teddy Bear | RS.3,250 | **RS.2,250** | 2,249.97 | **3,250** | `null` |
| `EF_PC_ELEC0V4463POD00144FDP` Samsung 43" Smart TV | RS.150,000 | **RS.130,000** | 130,500.00 | **150,000** | `null` |
| `EF_PC_ELEC0V1466P00015` 4DRC F10 Drone | RS.39,990 | **RS.21,990** | 21,994.50 | **39,990** | `null` |

The same is true of the `variants[]` entries (each a single `_default` variant
carrying the full price) and of `currency=USD` (the bear is USD 12.03, which is
3,250 converted, not 2,250).

**Checkout.** We created one unpaid order for the bear through `create_order`
(Colombo 03, delivery 2026-10-06, LKR):

```
order_ref  ORD-20261005-CFSN     order_id  7AF83EU2CFSN   (unpaid; link expired 17:48 LK)
summary    items_total 3250 · delivery_fee 300 · grand_total 3550 · LKR
```

The website cart charges **2,250** for the same item, so `create_order` charges **1,000 more**.

## Reproduce

```bash
API=https://www.kapruka.com/tools/commerce_phase1.jsp
call() { curl -sS -H "Authorization: Bearer $KAPRUKA_API_KEY" \
              -H "User-Agent: kapruka-mcp/1.0" -H "Accept: application/json" "$API?$1"; }

# 1. The API's price for a discounted product
call "endpoint=product&product_id=EF_PC_SOFT0V2686POD00003" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['price'], d['compare_at_price'])"
#   today:    {'amount': 3250, 'currency': 'LKR'} None
#   expected: {'amount': 2250, 'currency': 'LKR'} {'amount': 3250, 'currency': 'LKR'}

# 2. The website's price for the same product
curl -sS -A "Mozilla/5.0" -L "https://www.kapruka.com/buyonline/breathing-teddy-bear/kid/ef_pc_soft0v2686pod00003" \
  | grep -oE 'id="(pricelbl|priceAfterDiscountlbl)"[^>]*>[^<]*'
#   pricelbl → RS.3,250    priceAfterDiscountlbl → RS.2,250

# 3. What checkout charges (creates an UNPAID checkout session: mark it as a test)
curl -sS -X POST -H "Authorization: Bearer $KAPRUKA_API_KEY" -H "User-Agent: kapruka-mcp/1.0" \
  -H "Content-Type: application/json" "$API?endpoint=create_order&currency=LKR" -d '{
  "idempotency_key": "'"$(python3 -c 'import uuid; print(uuid.uuid4())')"'",
  "cart": [{"product_id": "EF_PC_SOFT0V2686POD00003", "quantity": 1}],
  "recipient": {"name": "TEST Price Check", "phone": "+94771234567"},
  "delivery": {"address": "TEST - do not fulfil", "city": "Colombo 03", "date": "<tomorrow YYYY-MM-DD>",
               "instructions": "TEST ORDER - price check only, do not fulfil"},
  "sender": {"name": "TEST"}, "gift_message": "TEST - do not fulfil"}' \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['summary'])"
#   today:    items_total 3250
#   expected: items_total 2250 (what the website cart charges)
```

The two electronics IDs above reproduce the same way.

## What needs to change

The rule: **the API must price every product exactly as the website cart prices
it**, from the same code, so the two can't drift.

1. **`product` and `product_detail`**
   - `price` = the discounted price the customer pays (2,250).
   - `compare_at_price` = the pre-discount price (3,250) when a discount applies, `null` otherwise.
   - The same for every entry in `variants[]`.
   - The same in every currency: the USD price should be the discounted price converted.
2. **`products_search`**
   - Each row's `price` and `compare_at_price` as above.
   - `min_price` / `max_price` filtering and `price_asc` / `price_desc` sorting on the
     **discounted** price. Otherwise a "cakes under LKR 2,500" search misses this bear.
3. **`create_order`**
   - Charge the discounted price. `summary.items_total` must equal what the website
     cart shows for the same lines.
   - **The delivery fee must use the discounted value too.** The LKR fee is capped at
     25% of the item value (50% in group E), so the cap has to come from the price
     actually charged.
4. **`delivery_check`'s new fee block** (the 2026-10-03 JSP change). It copies
   `createOrder`'s pricing to compute `fee_items_value`, so it must change in the
   same release, or the quoted fee and the charged fee drift apart again.
5. **One source of truth.** Please call the same method the website product page
   uses to fill `#priceAfterDiscountlbl` and the cart uses to charge, rather than
   re-implementing the discount in `commerce_phase1.jsp`. The pricing path in
   `createOrder` (`getByIDViaCache` → `applyPricingLevelsBasedOnCountry`) appears to
   stop before the discount is applied; that's probably where this belongs.

Optional, but useful to the agents:

- `discount_percent` (e.g. 30) and `discount_ends_at` (ISO date) when known, so an
  agent can say "30% off until Sunday".

## A question we need answered

**Which discounted number is authoritative?** The website label and the feed
`sale_price` don't always match:

| Product | Website label | Feed `sale_price` |
|---|---|---|
| Samsung TV | 130,000 | 130,500.00 |
| Drone | 21,990 | 21,994.50 |
| Teddy bear | 2,250 | 2,249.97 |

It looks like the website rounds down, though not to a fixed step. Whatever the cart
charges is correct. Please make the API return that same number, and tell us if the
feed's `sale_price` is a different concept (visual search shows the feed value, so
agents may quote it).

## Acceptance checks (after the fix)

For each of the three products above:

| Check | Expect |
|---|---|
| `product` `price.amount` | = website `#priceAfterDiscountlbl` |
| `product` `compare_at_price.amount` | = website `#pricelbl` |
| `products_search` row for the same id | same `price` / `compare_at_price` as `product` |
| `products_search` with `max_price` between the two prices | the product is included |
| `create_order` `summary.items_total` (qty 1) | = website cart price |
| `create_order` with `currency=USD` | the discounted price converted, not the full price |
| A product with no discount (e.g. `EF_PC_KIDS0V18POD00002P`, 2,300) | unchanged; `compare_at_price` `null` |

## On the MCP side

- **No MCP change is needed once the API is fixed.** The MCP already shows
  `compare_at_price` as "Was LKR 3,250" in `kapruka_get_product` and passes it
  through in search results. Agents will see and quote the right prices, and
  checkout will charge them, as soon as the API returns them.
- **Until then** we are asking the agent owners to quote only the price checkout
  will charge, and to send customers to kapruka.com for items visual search shows
  as on sale.
- **Test order to ignore:** `ORD-20261005-CFSN` / `7AF83EU2CFSN` (unpaid, every field
  marked TEST). Please cancel it if it shows up in the queue.

Contact: the Kapruka MCP maintainers (mcp.kapruka.com). Happy to re-run every
check above on staging or production once a fix is ready.
