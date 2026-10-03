# Delivery fee for a cart — `delivery_check` gives the checkout fee (MCP v0.6.0)

2026-10-03. Why `kapruka_check_delivery`'s rate never matched the fee checkout
charged, the fix in Kapruka's API, and how the MCP and its agents use it.

## The problem

`delivery_check` (commerce_phase1.jsp) printed `getCityRate(city,1,0).rate`, the
city's **base** rate. `create_order` (commerceOrderBean.createOrder ~L360) charges
`deliveryRatesBean.getPromoDeliveryFee(...)`, which is something else:

| Customer | Checkout delivery fee |
|---|---|
| LKR | `min(rate + rate_additional, max(300, 25% of the item value))`; city group **E** uses **50%** |
| USD (any non-LKR) | `(rate + rate_additional)` through `getInternationalDeliveryRate()` (divisor by city group A 150, B 170, C 220, D 103, E 200; legacy 140 constants), no cap |

Measured on 1,233 orders Afzal (WhatsApp agent) placed through this MCP: the LKR
formula reproduces 924 of 968 LKR orders exactly (the rest are 25% landing
between rate and rate + additional, also the formula), and the check's base rate
differed from the charged fee on **283 of 968 (29%)**. Examples:

- a cheap cart to a far city: Welimada, check said LKR 2,550, checkout charged LKR 300
- a big cart to a surcharged city: Kurunegala, LKR 1,290 → LKR 1,540
- overseas: about 1.4–2.8× the base rate converted at today's rate

## The API change (Kapruka, `web/tools/commerce_phase1.jsp`, JSP-only, hot deploy)

`delivery_check` takes three optional inputs and adds fee fields. `rate` is
unchanged, so existing callers are unaffected.

Inputs:

| Param | Meaning |
|---|---|
| `currency` | LKR (default) → LKR fee; USD/GBP/AUD/EUR → USD fee (checkout charges USD) |
| `cart` | `productId:qty[:i],...` — up to 30 lines; `i` = the cake has icing text (+USD 1 a unit, as createOrder) |
| `other_items_total` | LKR value of items not in `cart` (a quoted custom cake), added to the cart value |

Output (new):

| Field | Meaning |
|---|---|
| `delivery_fee` | what checkout will charge |
| `fee_currency` | `LKR` or `USD` |
| `fee_basis` | `cart` = exact for that cart · `max` = LKR with no cart value: `rate + rate_additional`, the most it can be · `fixed` = USD, the same for any cart |
| `fee_items_value` | the LKR item value used (`fee_basis=cart`) |
| `fee_cart_error` | the cart could not be priced (unknown product …) — fee falls back to `max` |

The block is the SAME computation with the SAME inputs as createOrder (same
`getByIDViaCache` → `applyPricingLevelsBasedOnCountry` pricing, same
`dollars2Rs` cart value, same `getPromoDeliveryFee` / `getInternationalDeliveryRate`
/ `rs2DollarsGetDoubleValue`). It never fails the check: an error just leaves the
fee fields out. Patch: `C:\KaprukaShoppingApp\docs\delivery_check_fee_2026-10-03.diff`.

**Keep the two in step:** if the checkout fee logic changes, change this block in
the same release. (Next JVM restart: move both into one commerceOrderBean method.)

### Verified (local Tomcat, local DB copy, 2026-10-03)

The block was run next to the real `commerceOrderBean.createOrder` on the same
carts. 14 of 14 matched the create_order summary to the cent: the LKR 300 floor,
the 25% cap, group E's 50% cap, rate + rate_additional, multi-line carts, and USD
for groups C, D and E. Cake icing could not be exercised locally (the local copy
does not load cakes); its code mirrors createOrder line for line.

## MCP v0.6.0

`kapruka_check_delivery` accepts `currency`, `cart` (`[{product_id, quantity, icing_text?}]`
— the create_order line shape) and `other_items_total`, and words the fee by its basis:

- `cart`: `- **Delivery fee for this cart: LKR 300** — exactly what kapruka_create_order will charge …`
- `max`: `- **Delivery fee: up to LKR 1,440** — checkout charges less on smaller carts … Pass `cart` for the exact fee.`
- `fixed`: `- **Delivery fee: USD 8.47** — what checkout charges overseas customers to this city, whatever the cart.`

Against an API without the fee fields it renders exactly as v0.5 (`flat rate LKR n`),
so the MCP can ship before the JSP.

## Rollout order (each step is safe on its own)

1. **MCP v0.6.0** — backwards compatible with the old JSP.
2. **Eagle** (branch `delivery-fee-for-cart`) — after the MCP: Afzal now sends
   `currency` on every check and v0.5's `extra="forbid"` would reject it.
3. **JSP** — any time after 1; from then on Afzal can quote the exact fee.
4. Then update Afzal's persona rule 7 ("No total and no delivery fee in this
   summary — only kapruka_create_order returns those") to allow a fee
   `kapruka_check_delivery` gave for this cart.
