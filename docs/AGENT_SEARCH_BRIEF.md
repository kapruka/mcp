# Brief for the Afzal agent session — what the API gets wrong and how to sell around it

**From the MCP side, 2026-09-21.** We spent today proving the upstream faults at
the API level (`docs/UPSTREAM_API_FAULTS.md`) and asking Kapruka to fix them
(`docs/KAPRUKA_API_FIX_REQUEST.md`). Until they do, some of it has to be handled
in how the agent searches and what it says. This is that list.

Three things first, so you don't duplicate work:

1. **The MCP already compensates** for price bounds in foreign currencies, wrong
   category names, and CATSYM navigation rows. Do not add prompt rules for those.
2. **Three things we told you were broken are not** — see "don't build these" at
   the bottom. They were our diagnosis errors and we've published corrections.
3. **One thing changed in the tool contract today** (v0.4.6): CAD is gone.

---

## The one rule that fixes the most

**Search the head noun alone, then check the results actually contain it.**

The API matches on *any* single token, including fragments inside unrelated
words, with no preference for rows matching the whole query. Extra words do not
narrow a search — they add wrong results:

```
q="Largactil"        -> 0 results          (correct: we don't stock it)
q="Largactil 50mg"   -> 87 results, all MG car parts
```

So:
- Send the noun the customer wants (`walker`, `gift card`, `ice cream`), not the
  full sentence they typed.
- **Before showing anything, check the head noun appears in the product name.**
  If it doesn't, we don't have it — say so. "medipedic walker" returns two
  bottles of Johnnie Walker above the actual medical walker; "Apple gift card"
  returns an iPhone and an apple juice; "staple gun" returns a toy gun.
- Never treat a non-empty result as proof the item exists.

This one behaviour change is worth more than everything else on this page.

---

## Sinhala: transliterate before you search

A Sinhala query does not return "no results" — it returns **the same four
unrelated rows every time**, claiming 90 matches:

```
q="අටපිරිකර"  (Buddhist alms goods) -> See Top Selling Liquor Products,
                                        Plush Toys, Roses, Shoes
q="atapirikara"                      -> the correct PIRIKARA products
```

`කිරිබත්` (milk rice) and `මල්` (flowers) return that identical liquor-first
list. Rules:

- Transliterate any Sinhala product word to Latin script before searching.
- Treat a result set that is all "See Top Selling …" rows as **no match**, not as
  results. That is the fallback list, not the catalogue.
- Offering whisky to someone asking for alms goods is the specific failure to
  avoid.

---

## Category: prefer not to send one

`category` is the website's subcategory facet, not a department. `Cakes` and
`Flowers` match nothing; `Kapruka Cakes` and `Fresh Flowers` work. Nothing in the
API publishes the valid names, and `Electronic` — a name Kapruka's own categories
endpoint returns, with a product count — matches nothing at all.

- **Default to omitting `category`.** The MCP retries without it when a filtered
  search comes back empty, so a wrong facet costs relevance, not the answer — but
  the retry costs a round trip and the first result page is the one the customer
  sees.
- If you do send one, use a name you have seen return rows. Known good:
  `Kapruka Cakes`, `Fresh Flowers`, `Books`, `Chocolates`, `Fruits`.
- In `response_format: "json"`, the MCP sets `category_filter_dropped` when it had
  to retry. If you see that, the facet name was wrong — don't reuse it.

---

## Money

- **Foreign-currency budgets now work.** Pass the customer's own currency and
  their own numbers — `currency: "USD", max_price: 30` means thirty dollars. The
  MCP converts the bound before it reaches the API and re-checks every row it
  returns. **Do not convert to rupees yourself**; that would double-convert.
- **Supported currencies are LKR, USD, GBP, AUD, EUR.** CAD was removed from the
  tools today — it had never worked; the API answers `invalid_currency`. Also
  rejected: JPY, SGD, AED, INR. For a Canadian customer, quote USD and say so.
- **Never quote a result count.** `total_estimate` is not a count — it caps at
  100 and returns a constant 90 for junk queries. "We have 90 options" is a
  sentence to never say.

---

## When a tool call fails

A tool result whose text starts with `Error` is a **failure**, not information.
The MCP still returns most upstream failures as ordinary text rather than a
flagged error, so it is easy to read one as content.

- Never relay an `Error …` string to the customer.
- Never treat it as "we have none of those" — it means we don't know.
- Retry once; if it fails again, tell the customer you're having trouble looking
  it up and offer to come back to them.

(We know this is ours to fix properly. It changes live behaviour for your agent,
so we're not shipping it without a decision.)

---

## Don't build these — we were wrong about them

Corrections we published today, so nobody writes a prompt rule for a fault that
isn't there:

| We said | Actually |
|---|---|
| Long phrases return nothing | `happy birthday ribbon cake for boy teenage` returns three correct cakes. The old failures almost certainly also carried `category=Cakes`. |
| No fuzzy matching on near-miss titles | `Mother Touch` and `Mothers Touch` both return rows. What they return is off-target — that's the head-noun problem above, not a missing fuzzy matcher. |
| Brand names aren't indexed | `Nawaloka` returns the right vouchers. The other brand misses we couldn't substantiate. |
| Failures arrive as HTTP 200 from Kapruka | Kapruka's statuses are correct (400/404 with clean envelopes). The 200s are ours. |

---

## What we're waiting on Kapruka for

Handed to their dev team today with a 13-check acceptance script
(`docs/kapruka_api_acceptance.sh`, currently 0/13 passing):

price bounds in the caller's currency · a real answer to an unknown category ·
**the facet list in the search response** · ranking on all query tokens ·
Sinhala in the index · navigation rows out of product search · a truthful
`total_estimate`.

The facet list is the one to push for. With the right facet, "birthday cake"
returns 10 correct cakes out of 10; without it, 2 out of 7.

Separately flagged to them as needing their own test environment: order quantity
being dropped (`#PRICEMISMATCH`), the same-day delivery hour branch, and bank
deposits for USD orders priced at 140 LKR/USD when their own API implies 269.95.
