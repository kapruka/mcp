# Verification of the 2026-09-26 API fixes

**To the Kapruka API team, from the MCP side. 2026-09-26, tested against
production at 12:08 UTC (17:38 Colombo).**

Thank you — this is live and it works. The acceptance script now reads
**11 passed, 2 failed**, and the two reds are the relevance work you said is not
in this release. The MCP (v0.5.0) has been updated to use the new response
shapes and our workarounds for the fixed faults are deleted.

## Confirmed on production

| Item | Result |
|---|---|
| 1. Bounds in the requested currency | ✅ USD 10–30 → six cakes USD 14–28; USD min 50 → nothing under 50; `applied_filters.currency` present. GBP/EUR/AUD fine. |
| 2a. Unknown category | ✅ `400 invalid_category` with `valid_categories` |
| 2b. Facet vocabulary | ✅ `facets.categories` on every search; `Electronics` works. Understood that `categories` is navigation only — we've changed our tool descriptions to say so. |
| 3. Relevance | ⏳ as you said. `Largactil 50mg` still returns MG car parts; `medipedic walker` now tops with a baby-walker belt. |
| 4a. Sinhala index | ✅ partly — `අටපිරිකර` now finds four atapirikara sets. `කිරිබත්` returns cut vegetables and cupcake moulds, presumably no Sinhala name on the milk-rice products yet. |
| 4b. Junk fallback list | ✅ gone |
| 5. Navigation rows | ✅ `q=cake&max_price=1` → 0 rows; every row has `type: "product"` |
| 6. `total_estimate` | ✅ a real count (`cake` 1414, `zzzqwerty` 0) |
| Same-day evening gap | ✅ at 17:41 Colombo, today refused for Colombo 03 with and without `product_id`, next date offered |

**Your note on `bound-is-currency-sensitive` was right** — comparing the top six
of a loose bound couldn't see the fix. The script now uses `max_price=20`
(USD finds cakes, LKR finds none). We also corrected two checks that were
passing too easily and replaced the `Electronic` check, whose premise was wrong.

## Two small things we noticed

1. **A navigation facet leaks into `facets.categories`.** `q=cake&max_price=20`
   (LKR) returns zero rows but `facets: [{"name": "Major Category", "count": 5}]`,
   and `category=Major Category` is accepted and returns nothing. The count of 5
   matches the navigation rows removed before paging, so it looks like facets are
   computed before that removal.
   ```bash
   call "endpoint=products_search&q=cake&max_price=20&limit=2"
   ```

2. **The echo isn't canonical.** Your note says a case/spacing variant is re-run
   with the canonical name, "which is then echoed in `applied_filters.category`".
   The search works, but the echo is what we sent: `category=KAPRUKA CAKES` →
   `"category": "KAPRUKA CAKES"`, `  kapruka cakes ` → `"kapruka cakes"`. Harmless
   for us; mentioning it only because the note says otherwise.

## Not verified by us

- **Bank deposit refusal for USD** — needs a USD order id, which we won't create.
  We've told the sales agent to offer bank deposit to LKR customers only.
- **Quantity** — noted that it doesn't reproduce in current `createOrder`. We'll
  send an order id if we see a stored-once/charged-twice case again.
- **Variant parents** — still no example on our side either; we'll send one if
  we find it.
