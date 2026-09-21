# Faults in the Kapruka commerce API

**For the Kapruka API developers. Written 2026-09-21, every call below re-run against production that day.**

All examples are one `curl` you can paste. `$KAPRUKA_API_KEY` is the phase-1 agent
token; set it in your shell, it is never written down here. A real `User-Agent` is
required — Cloudflare Bot Fight Mode drops the `curl` default.

```bash
API=https://www.kapruka.com/tools/commerce_phase1.jsp
call() { curl -sS -H "Authorization: Bearer $KAPRUKA_API_KEY" \
              -H "User-Agent: kapruka-mcp/1.0" -H "Accept: application/json" "$API?$1"; }
```

Seven faults, severest first, then the ones we could not test alone, then four
things we told you were broken that turned out to be ours.

---

## 1. A price bound is compared in rupees no matter which currency was asked for

**Severity:** every overseas customer's budget was answered wrong. About 62% of the
chat agent's customers are abroad and quote dollars. Nine of the eighteen searches
still returning nothing in a five-day replay were this.

The prices you **return** are converted to the requested currency. The
`min_price` / `max_price` you **compare against** are not — they are matched
against the raw rupee figure.

**Reproduce:**
```bash
call "endpoint=products_search&q=cake&currency=USD&min_price=10&max_price=30&limit=5"
```

**Returns:** `rows=0, total_estimate=0`. A cake between ten and thirty dollars —
the single most ordinary request this API will ever get — has no answer.

**Control:** the same span in rupee numbers, still asking for USD prices, and the
same numbers as a rupee bound:
```bash
call "endpoint=products_search&q=cake&currency=USD&max_price=9600&limit=6"
call "endpoint=products_search&q=cake&currency=LKR&max_price=9600&limit=6"
```
Both return `total_estimate=90` and the **identical** result set — `CAKE00KA001912`
at USD 16.67 in the first, the same product at LKR 4500 in the second. A bound of
9600 means the same thing whether the caller said dollars or rupees, which is the
whole proof.

`min_price` is broken the same way and shows it without needing a control at all:
```bash
call "endpoint=products_search&q=cake&currency=USD&min_price=50&limit=6"
#  CAKE00KA001912   16.67 USD   Pastel Grace Bento Ribbon Cake And Cupcake Set
```
A minimum of fifty dollars returns a cake priced sixteen dollars — the response
contradicts its own filter. (16.67 USD is 4500 LKR, which clears a bound read as
"50 rupees".)

**Expected:** compare the bound in the currency named by `currency`.

**Also:** `applied_filters` echoes `"max_price": 32.0` with no unit, so a caller
cannot detect the mismatch from the response either.

**Where we think it is:** the conversion is applied to the row price on the way
out and never to the bound on the way in.

**What we do meanwhile:** we read your own price list to derive a rate, convert
the caller's bound to rupees before sending it, and re-check every returned row
against the caller's real bound. Roughly 60 lines in `src/tools/products.py` we
would delete the day this is fixed.

---

## 2. An unknown category is answered with silence, and nothing publishes the valid names

**Severity:** 92 searches carried a category filter in five days and 75 returned
nothing — Cakes 40 of 40, Flowers 23 of 23. Every one was a customer asking for a
cake or for flowers.

`category` is the website's subcategory facet (`subcat=` in
`srilanka_online_search.jsp`), not a department name. That is fine. The fault is
what a wrong name does:

**Reproduce:**
```bash
call "endpoint=products_search&q=birthday%20cake&category=Cakes&limit=6"
call "endpoint=products_search&q=birthday%20cake&category=ZZZ_NOT_A_REAL_FACET&limit=6"
```

**Returns:** both — `HTTP 200`, `rows=0`, `total_estimate=0`, and the bad value
echoed back in `applied_filters` as if it had been honoured. A typo and an empty
shelf are indistinguishable.

**Control:**
```bash
call "endpoint=products_search&q=birthday%20cake&category=Kapruka%20Cakes&limit=6"
#  6 real cakes, LKR 4,850-8,800, total_estimate=100
```

**Expected:** reject an unknown facet (`400 invalid_category`, naming the valid
set) or ignore it and say so in `applied_filters`. Either is fine; silence is not.
You already normalise the value — `category=%20Kapruka%20Cakes%20` comes back
echoed as the trimmed `"Kapruka Cakes"`, and matching is case-insensitive — so the
facet is in your hand at the moment you could validate it.

**The half that matters more: nothing anywhere lists the valid facet names.**
`products_search` returns only `results`, `next_cursor`, `total_estimate`,
`applied_filters` — no facet list, though the website's own sidebar computes one
for every query. The separate `categories` endpoint is a *different vocabulary*
(65 names like Automobile, Books, Electronic) that only partly overlaps: `Books`,
`Chocolates` and `Fruits` work as facets, `Electronic` does not.

```bash
call "endpoint=products_search&q=phone&category=Electronic&limit=3"   # rows=0
call "endpoint=products_search&q=phone&limit=3"                       # 3 phones, ELEC00A6107...
```
`Electronic` is a name you publish, with a `product_count`, that silently matches
nothing — and the caller cannot tell whether they mis-spelled the facet or you
are out of phones.

**Returning the facet list in the search response is the single highest-value
change on this page.** With the right facet, "birthday cake" returns 10 correct
cakes out of 10. Without it, 2 out of 7, with four greeting cards and a rose vase
ranked above the cakes.

The names are a fixed vocabulary, not something computed per query — `q=cake&
category=Fresh%20Flowers` cheerfully returns flowers — so handing us the list once
would also do.

**What we do meanwhile:** when a search with a category comes back empty we silently
re-run it without the filter, so a wrong facet costs relevance instead of the whole
answer.

---

## 3. One matching token is enough to be a result

**Severity:** the agent answers confidently and wrongly. From real customer chats:
gyro ball → gyro bowl, Apple gift card → an iPhone and an apple juice, staple gun →
a toy gun, ice cream → a Play-Doh ice cream playset, medipedic walker → Johnnie
Walker.

**Reproduce:**
```bash
call "endpoint=products_search&q=Largactil%2050mg&limit=4"
```

**Returns:** `total_estimate=87`, and the top four are car parts —
`Mg Zs Front Wiper Pair`, `Mg Zs Welcome Lights`, `Mg Zs 3d Car Mats`,
`Usha Imprezza Mg 3576 Mixer Grinder`. Someone asked for a prescription
antipsychotic and was shown windscreen wipers.

**Control:** the same query with the noise token removed:
```bash
call "endpoint=products_search&q=Largactil&limit=4"     # rows=0, total_estimate=0
```
You do not stock Largactil, and alone the API says so correctly. Adding `50mg`
turns an honest "no" into 87 confident wrong answers, because `mg` matches inside
`Mg Zs`.

A second example carries its own control inside one response:
```bash
call "endpoint=products_search&q=medipedic%20walker&limit=5"
#  1 Johnnie Walker Red Label      2 Johnnie Walker Black Label
#  3 Baby Walker Helper Hand Held  4 Quantum T101 Walker Treadmill
#  5 MOVING WALKER WITH WHEEL (FS912L)   <- the product the customer wanted
```
The right answer is in your index, ranked fifth, behind two bottles of whisky.

**Expected:** rank a row matching every query token above a row matching one; do
not match a token as a substring inside an unrelated word; and where nothing
matches the head noun, prefer returning nothing. `q=chocolate%20umbrella` returns
four umbrellas and no chocolate, which shows the same rule from the other side.

**Where we think it is:** the matcher looks like OR across tokens with substring
matching and no head-noun weighting.

**What we do meanwhile:** nothing — we cannot fix relevance from outside. This is
the fault that makes the agent look stupid to a customer.

---

## 4. A query in Sinhala returns the same five unrelated rows every time

**Severity:** six Sinhala queries in five days, none of which got a usable answer.
Sinhala is the first language of most of the customers buying for delivery inside
Sri Lanka.

**Reproduce:**
```bash
curl -sS -G --data-urlencode "q=අටපිරිකර" \
  -H "Authorization: Bearer $KAPRUKA_API_KEY" -H "User-Agent: kapruka-mcp/1.0" \
  "$API?endpoint=products_search&limit=4"
```

**Returns:** `total_estimate=90` and four category shortcuts —
`See Top Selling Liquor Products`, `Plush Toys`, `Roses`, `Shoes`. Not "no
results": a confident ninety-match claim, topped by liquor, for a request for
Buddhist alms goods. `කිරිබත්` (milk rice) and `මල්` (flowers) return **the same
four rows**.

**Control:** the same word transliterated:
```bash
call "endpoint=products_search&q=atapirikara&limit=4"
#  See Top Selling Pirikara, PIRIKARA0186, PIRIKARA0190, PIRIKARA0191 — correct
```

**The request is fine; the index is the problem.** `applied_filters` echoes the
Sinhala back intact (`"q": "අටපිරිකර"`),
so encoding survived the wire. Nothing in the index matched, and instead of
saying so the search fell back to a default list.

**Expected:** index the Sinhala names you already print on the site, or at minimum
return zero rows and `total_estimate=0` when nothing matched, as `q=zzzqwerty`
correctly does.

---

## 5. Category shortcuts come back as products, priced zero, and pass every filter

**Severity:** they crowd out real products and they defeat price filtering
entirely. In the USD-32 search in fault 1 they were 5 of 5 results.

**Reproduce:**
```bash
call "endpoint=products_search&q=cake&max_price=1&limit=6"
```

**Returns:** `rows=5, total_estimate=5` — a budget of **one rupee** finds five
"products":
```
CATSYM00230  0 LKR  See Top Selling Cakes          in_stock: true, stock_level: "low"
CATSYM00233  0 LKR  See Top Selling Birthday Cakes
CATSYM00380  0 LKR  Custom Printed Cakes
```

`CATSYM*` rows are navigation links, not things anyone can buy. They are returned
by a **product** search, marked `in_stock: true` with a stock level, and priced
`0` — so they satisfy every `max_price` ever sent.

**Expected:** leave them out of `products_search`, or give them a type field so a
caller can drop them without pattern-matching an ID prefix.

**What we do meanwhile:** we drop anything whose id starts `CATSYM`, and over-fetch
three pages deep on price-sorted searches to refill the gap. A guess about a string
prefix, which will break silently the day the prefix changes.

---

## 6. `total_estimate` is not a count and cannot be used as one

**Severity:** low on its own, but it is the only size signal you give, so it gets
believed.

```bash
call "endpoint=products_search&q=cake&limit=2"          # total_estimate=100
call "endpoint=products_search&q=Kidsmarket&limit=2"    # total_estimate=90
call "endpoint=products_search&q=gyro%20ball&limit=2"   # total_estimate=90
call "endpoint=products_search&q=zzzqwerty&limit=2"     # total_estimate=0
```
It caps at 100, and `90` recurs for unrelated queries that returned junk — it
tracks the fallback path, not the catalogue. `0` is the one value that means
something. Either make it a real count or rename it so nobody displays it.

---

# Needs a test on your side

**Quantity is dropped from orders.** In `commerceOrderBean.createOrder`, quantity
is priced but never written into `orderText` / `cartSnapShot`, so a "2 ×" line is
stored once, charged twice, and stamped `#PRICEMISMATCH`. We did not prove this,
deliberately — proving it means putting a real order into your warehouse queue.
Please reproduce it in your own test environment; we believe it is the most
expensive item on this page.

**Same-day delivery is offered all evening, and refused all afternoon.**
`delivery_check` branches on hour ≥ 17, < 5, and 5–17, so the 17:00–23:59 case for
*today* is never evaluated. At 15:13 Sri Lanka time today, both cities we tried
refused same-day:
```bash
curl -sS -G --data-urlencode "city=Colombo 03" \
  -H "Authorization: Bearer $KAPRUKA_API_KEY" -H "User-Agent: kapruka-mcp/1.0" \
  "$API?endpoint=delivery_check"
# "available": false, "reason": "Today's slots for Colombo 03 are full",
# "next_available_date": "2026-09-22"
```
Note the reason given is "slots are full", which is not what a clock branch means —
if the cutoff is noon, say the cutoff has passed. We have a capture running at
17:05 and 21:00 local to show the evening half; ask us for it, or call the
endpoint yourselves after 17:00 and see whether it offers today.

**Bank deposits for dollar orders are priced at a 2022 rate.**
`bank_deposit_info` converts a USD order at 140 LKR/USD. Your own product endpoint,
asked the same minute, implies **269.95**:
```bash
call "endpoint=product&product_id=CAKE00KA001912&currency=LKR"   # 4500
call "endpoint=product&product_id=CAKE00KA001912&currency=USD"   # 16.67  -> 269.95
call "endpoint=product&product_id=FLOWERS00T1875&currency=USD"   # 66.67  -> 269.99
```
Proving it end to end needs a real USD order id, which we will not create. Six
overseas orders are reported to have gone through at roughly half price.

**A parent product with variants is orderable.** We could not reproduce this
read-only: no `_TC<n>` ids appear in searches for clothing, perfume, watches,
sarees, hampers or bouquets, and every product we asked about returns exactly one
variant named `Default`. Please name a product that genuinely has variants and we
will retest. If such products exist, note that `products_search` rows carry no
variant field at all, so a caller cannot tell a parent from a leaf.

---

# Four things we blamed on you that are not yours

Listed so you do not go hunting. We were wrong about these and have corrected our
own notes.

- **"Failures arrive as HTTP 200."** Your errors carry correct statuses —
  `400 invalid_currency`, `404 product_not_found`, `400 invalid_parameter`,
  `404 city_not_found`, all with a clean `{"error":{...}}` envelope. The 200s are
  produced by our MCP layer, which returns an error *string* as a successful tool
  result. Ours to fix.
- **"Long phrases return nothing."** `q=happy birthday ribbon cake for boy teenage`
  returns three correct cakes today. The original failures almost certainly carried
  `category=Cakes` as well, which is fault 2.
- **"No fuzzy matching on near-exact titles."** `Mother Touch Gift Box` and
  `Mothers Touch Gift Box` both return four rows; neither returns nothing. What
  they return is off-target, which is fault 3, not a missing fuzzy matcher.
- **"Brand names are not indexed."** `Nawaloka` returns four Nawaloka vouchers
  correctly. `Tobi`, `Mithuri` and `Kidsmarket` return junk, but we cannot show the
  products exist, so we cannot call it a fault — and the junk is fault 3 again.

One more that is ours, mentioned only so nobody files it: our client advertises
**CAD**, which you answer `invalid_currency`. You support LKR, USD, GBP, EUR and
AUD; CAD, JPY, SGD, AED and INR are all rejected. That is our list to correct.

---

## If you fix one thing

Return the facet list in the search response (fault 2). It is the difference
between 10 correct cakes out of 10 and 2 out of 7, and it costs you one field.
