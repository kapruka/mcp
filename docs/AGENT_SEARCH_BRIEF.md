# Brief for the Afzal agent — Kapruka MCP v0.5.0 (2026-09-26)

**Replaces the 2026-09-21 brief.** Kapruka fixed most of the API faults we
reported, it is live on production, and the MCP has been updated to match.
Several rules from the old brief are now wrong — this is the current set.

---

## What changed for you

| Area | Before (v0.4.x) | Now (v0.5.0) |
|---|---|---|
| **Category filter** | `Cakes` silently matched nothing; the MCP retried without it | Every search lists its valid facet names. A wrong name is dropped, you get results anyway, **plus the list of names that do work** |
| **Foreign-currency budgets** | MCP converted to rupees behind the scenes | The API bounds in your currency natively. Pass the customer's own number in their own currency |
| **Sinhala** | Always returned the same junk list (liquor, toys, roses, shoes) | Works for products that have a Sinhala name (`අටපිරිකර` → the atapirikara sets). Others return loosely related results |
| **Category shortcut rows** | Filtered by the MCP | Gone from the API entirely |
| **Same-day delivery** | Offered all evening | Correct, and item-aware when you pass `product_id` |
| **Relevance** | Any single word matched | **Unchanged** — still your job (see rule 1) |

---

## Rules

### 1. Search the head noun, then check the names contain it — still the most important rule

Kapruka deferred this fix. The API still matches any one word, and it strips
digits before searching, so `Largactil 50mg` is searched as `Largactil mg` and
returns MG car parts. `medipedic walker` now puts a baby-walker belt first.

- Send the thing the customer wants (`walker`, `gift card`, `ice cream`), not
  their whole sentence. Don't put quantities or strengths (`50mg`, `2kg`) in `q`.
- **Before presenting results, check the product names contain the head noun.**
  If none do, we don't have it — say so.
- A non-empty result is not proof the item exists.

### 2. Categories: search first, then narrow with a facet the search gave you

Every search now ends with a line like:

> _Narrow with `category` (facets for this search): Kapruka Cakes (292),
> Greeting Cards (244), Birthday (92), …_

(In `response_format: "json"`: `facets.categories`, a list of `{name, count}`.)

- To narrow, re-search with one of **those** names — e.g. `category: "Kapruka Cakes"`
  for cakes only. Case and spacing don't matter.
- **Never** use department words (`Cakes`, `Flowers`) or names from
  `kapruka_list_categories` — that tool is the site's navigation, a different
  vocabulary (`Electronic` there vs `Electronics` in search). Use it only to send a
  customer a browse link.
- If you send a wrong name, the MCP drops it, still returns results, and says:
  _"'Cakes' is not a category for this search … Categories that do exist: Kapruka
  Cakes, …  Do not re-send 'Cakes'."_ In JSON: `category_filter_dropped` and
  `valid_categories`. Pick from that list next time.
- A real facet that returns **no products** now means genuinely none (e.g. no
  cakes under that budget). The MCP no longer silently widens it to other
  categories — tell the customer, or relax the budget.

### 3. Money

- **Pass the customer's budget exactly as they said it**: `currency: "USD",
  max_price: 30` means thirty dollars. Don't convert to rupees — that was never
  right, and now it would search "under $8,100".
- Supported currencies: **LKR, USD, GBP, AUD, EUR**. For Canadian customers,
  quote USD and say so.
- **Bank deposit is for LKR orders only.** Kapruka now refuses bank-deposit
  details for a USD order (`400 invalid_currency`). USD/overseas customers pay by
  card through the checkout link.
- `total_estimate` is now an honest count of index hits — but because of rule 1
  those hits include one-word matches. Still don't say "we have 700 options".

### 4. Delivery dates

- Once an item is chosen, **always pass `product_id` to `kapruka_check_delivery`**.
  It now applies that item's own same-day rule: restaurant/vendor food can go
  today until late afternoon; ordinary items go next day; ordinary same-day is
  only possible early in the morning near Colombo. A check without `product_id`
  can give a different answer.
- When `available` is false, **offer `next_available_date`**. Don't read meaning
  into the `reason` text — it's the website's wording and often says "slots are
  full" when the real cause is the cutoff time.

### 5. Sinhala

- Sinhala search now works for products that have a Sinhala name recorded
  (`අටපිරිකර` finds the atapirikara sets). Many products don't have one yet.
- So: try the Sinhala word; **if the results don't contain what was asked for,
  retry with the transliteration** (`atapirikara`, `kiribath`). Rule 1 applies —
  `කිරිබත්` currently returns cut vegetables and cupcake moulds.

### 6. When a tool fails

A tool result starting with `Error` is a failure, not information. Don't relay it,
don't treat it as "we have none". Retry once, then tell the customer you're having
trouble and will come back.

---

## Rules from the old brief that no longer apply

- ~~"Default to omitting `category`"~~ — now search first, then narrow with a
  listed facet.
- ~~"Transliterate Sinhala before you search"~~ — try Sinhala first, fall back to
  transliteration.
- ~~"Treat an all-'See Top Selling…' result as no match"~~ — those rows no longer
  appear.
- ~~`price_bounds_converted_to_lkr` / `price_filtered_locally` in JSON~~ — removed;
  there is no conversion any more.
- ~~"`total_estimate` is a constant 90 for junk"~~ — it's a real count now (see 3).

## Still waiting on Kapruka

Relevance (rule 1) and wider Sinhala coverage. When those land, rules 1 and 5
get simpler and we'll send an update.
