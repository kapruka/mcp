# New: international gifts (for Aloka, Afzal and any agent using the Kapruka MCP)

**Live from 20 October 2026 (MCP 0.9.0).**

Customers can now send a gift to someone **outside Sri Lanka**. Kapruka's partner
in that country delivers it; it is not shipped from Sri Lanka.

| Country | Gifts in stock (20 Oct) | Delivery fee per order |
|---|---|---|
| UAE | 37 (cakes, flowers, hampers, chocolates) | LKR 13,020 / USD 40.68. **Only** Abu-Dhabi, Dubai, Sharjah, Ajman, Ras al Khaimah, Al Ain (Ras al Khaimah and Al Ain cost more) |
| Australia | 55 (mostly cakes) | **Only** 14 Melbourne-area suburbs: Dandenong, Noble Park, Hallam, Hampton Park, Springvale, Clayton (LKR 3,200), Narre Warren, Clyde, Glen Waverley, Mount Waverley (LKR 4,800), Cranbourne, Officer, Beaconsfield, Pakenham (LKR 6,400) |
| USA | 11 | LKR 22,720 / USD 71 |
| Canada | 16 | USD 21 |
| UK | 10 (cakes) | USD 28.57 |
| France, New Zealand, India | **none right now** | — |

The tools always give the current numbers; this table is only a guide.

## When to use it

Use it whenever the **recipient** is abroad: "send flowers to my sister in Dubai",
"a cake for my son in Melbourne", "a hamper for my parents in Toronto". If the
recipient is in Sri Lanka, use the normal tools as before.

## How

1. **`kapruka_gift_countries`** shows the countries, the gifts in stock, the
   delivery fee and the zones. Confirm the recipient's country. If the store is
   empty (France, New Zealand, India), say so plainly.
2. **`kapruka_gift_country_products(country)`** returns the **whole store**,
   grouped by type, with prices. Read it and pick suitable gifts for the
   customer. These products are **not** in `kapruka_search_products`, so don't
   search for them. You can narrow the store with `type` ("Cakes", "Flowers",
   "Hampers").
3. **Australia and the UAE: ask which listed area the recipient is in.**
   Delivery goes **only** to the listed zones. If theirs isn't listed, say
   Kapruka can't deliver there; don't guess the nearest one. Small spelling
   differences are fine: "abu dhabi" is matched to "Abu-Dhabi".
4. **Collect the details:**
   - the recipient's name and phone (their local number, e.g. +971…);
   - their street address, city/town and **postcode/ZIP**;
   - the delivery date, **at least 3 days from today**;
   - the sender's name, and their email if you have it;
   - optionally, a gift message.
5. **Optional:** `kapruka_check_delivery(product_id, zone)` confirms the date
   and the exact delivery fee. No city is needed for these gifts.
6. **Order with `kapruka_create_order`:**
   - `delivery.type: "international_gift"`
   - `delivery.country`, `delivery.zone` (Australia and the UAE only),
     `delivery.zip`, `delivery.address`, `delivery.city`, `delivery.date`
   - `sender.email` if you have it

   Then quote the totals from the answer and send the payment link. Example:
   "Delivered in Dubai, UAE on 24 October. USD 136.65 for the gift plus
   USD 40.68 delivery, USD 177.33 in total."

## Rules

- **One country per order.** Never mix international gifts with Sri Lanka items
  or custom cakes. If a customer wants both, place two orders.
- **Quantity 1 per line.**
- **The delivery fee is per order,** whatever the number of items.
- **No discounts** apply to these stores, and **no economy delivery** either.
- **Prices are in LKR or USD.** A customer asking in GBP, AUD or EUR sees USD.
- **If an order is refused,** the error says exactly why: wrong zone (with the
  list), date too early (with the earliest date), or mixed cart. Nothing has
  been created; fix the detail with the customer and try again.

## Known content gaps (Kapruka is fixing these)

- **"In Sri Lanka" in Australian descriptions.** Some say "perfect for special
  occasions in Sri Lanka". Ignore that wording: these gifts are delivered in
  Australia.
- **Product types are still Kapruka's raw codes.** The tools turn them into
  tidy labels (Cakes, Flowers, Hampers, Chocolates, Fruit Baskets).
