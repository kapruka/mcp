# What we need changed in the Kapruka commerce API

**For the Kapruka API development team. 2026-09-21.**

Companion to `UPSTREAM_API_FAULTS.md`, which is the evidence. This one is the
work list: six changes, each with the test that proves it, in the order we would
do them.

## Run the tests first

`kapruka_api_acceptance.sh` (next to this file) is thirteen checks, one command,
no dependencies beyond bash/curl/python3:

```bash
export KAPRUKA_API_KEY=<the phase-1 agent token>
bash kapruka_api_acceptance.sh
```

Today it prints **0 passed, 13 failed**. Each failure names the actual value it
got, so you can watch them turn green one at a time. When it exits 0 you are
done. It is read-only — it never calls `create_order`.

Everything below uses:

```bash
API=https://www.kapruka.com/tools/commerce_phase1.jsp
call() { curl -sS -H "Authorization: Bearer $KAPRUKA_API_KEY" \
              -H "User-Agent: kapruka-mcp/1.0" -H "Accept: application/json" "$API?$1"; }
```

A real `User-Agent` is required — Cloudflare Bot Fight Mode drops curl's default.

---

## 1. Compare `min_price` / `max_price` in the requested currency

**Change:** you already convert the prices you return into `currency`. Convert
the bound the same way before comparing, or compare the row's converted price.

**Tests:** `usd-range-returns-something`, `usd-min-is-a-minimum`,
`bound-is-currency-sensitive`

```bash
call "endpoint=products_search&q=cake&currency=USD&min_price=10&max_price=30&limit=6"
```
| | |
|---|---|
| **Today** | `rows=0` — an ordinary "$10–30 cake" has no answer |
| **When fixed** | six cakes, every one priced between USD 10 and USD 30 |

```bash
call "endpoint=products_search&q=cake&currency=USD&min_price=50&limit=6"
```
| | |
|---|---|
| **Today** | first row `CAKE00KA001912` at **USD 16.67** — below the stated minimum |
| **When fixed** | no row under USD 50 |

The third check is the diagnostic: `max_price=9600` with `currency=USD` and with
`currency=LKR` currently return the **identical** result set. After the fix they
must differ, because 9600 dollars and 9600 rupees are not the same budget.

**Also:** put the unit in the echo. `applied_filters` says `"max_price": 32.0`
with no currency, so a caller cannot detect the mismatch from the response.

**Why first:** about 62% of the chat agent's customers are overseas and quote
dollars. Nine of eighteen searches that returned nothing in a five-day replay
were this.

---

## 2. Answer an unknown `category` honestly, and publish the facet names

Two changes, same endpoint. The second is the one that matters.

**Change 2a:** an unrecognised facet must either be rejected
(`400 invalid_category`, listing the valid set) or ignored and dropped from
`applied_filters`. Right now `category=Cakes` and
`category=ZZZ_NOT_A_REAL_FACET` produce byte-identical responses — HTTP 200,
zero rows, the bad value echoed back as though honoured — so a typo and an empty
shelf are indistinguishable.

You already normalise the value: `category=%20Kapruka%20Cakes%20` comes back
echoed as the trimmed `"Kapruka Cakes"`, and matching is case-insensitive. The
facet is in your hand at the moment you could validate it.

**Test:** `unknown-facet-is-not-silent`

```bash
call "endpoint=products_search&q=birthday%20cake&category=ZZZ_NOT_A_REAL_FACET&limit=6"
call "endpoint=products_search&q=birthday%20cake&category=Kapruka%20Cakes&limit=6"   # control: 6 real cakes
```

**Change 2b — the highest-value item on this page:** return the valid facet
names in the search response. The website sidebar already computes them for
every query; the API returns only `results`, `next_cursor`, `total_estimate`,
`applied_filters`. A caller currently has to guess.

The names are a fixed vocabulary, not per-query — `q=cake&category=Fresh Flowers`
cheerfully returns flowers — so a static list we can fetch once would also do.

**Test:** `search-publishes-its-facets`, and `published-category-names-work`:

```bash
call "endpoint=products_search&q=phone&category=Electronic&limit=3"   # today: 0 rows
call "endpoint=products_search&q=phone&limit=3"                      # today: 3 phones
```
`Electronic` is a name **you publish**, with a `product_count`, from the
`categories` endpoint — and it matches nothing as a facet. `Books`, `Chocolates`
and `Fruits` from the same list do work. The two vocabularies overlap partially
and nothing says which names are in which.

**Why:** 92 searches carried a category filter in five days and 75 returned
nothing — Cakes 40 of 40, Flowers 23 of 23, every one a customer asking for a
cake or flowers. With the right facet, "birthday cake" returns 10 correct cakes
out of 10; without it, 2 out of 7, behind four greeting cards and a rose vase.

---

## 3. Require more than one matching token

**Change:** rank a row matching every query token above a row matching one, and
stop matching a token as a substring inside an unrelated word. Where nothing
matches the head noun, returning nothing is the right answer.

**Tests:** `noise-token-cannot-invent-hits`, `head-noun-outranks-one-token`

```bash
call "endpoint=products_search&q=Largactil&limit=4"        # 0 rows — correct, you don't stock it
call "endpoint=products_search&q=Largactil%2050mg&limit=4" # 87 hits of MG car parts
```
| | |
|---|---|
| **Today** | adding `50mg` turns an honest "no" into `Mg Zs Front Wiper Pair`, `Mg Zs Welcome Lights`, `Mg Zs 3d Car Mats` |
| **When fixed** | the two queries agree: nothing, or nothing automotive |

```bash
call "endpoint=products_search&q=medipedic%20walker&limit=5"
```
| | |
|---|---|
| **Today** | 1 Johnnie Walker Red Label, 2 Johnnie Walker Black Label, … 5 `MOVING WALKER WITH WHEEL` |
| **When fixed** | the medical walker is not ranked below whisky |

The right product is already in your index. This is ranking, not coverage.
`q=chocolate umbrella` shows the same rule from the other side: four umbrellas,
no chocolate.

**Why:** this is the fault that makes the agent look stupid to a customer. Real
chats: gyro ball → gyro bowl, Apple gift card → an apple juice, staple gun → a
toy gun, ice cream → a Play-Doh playset.

---

## 4. Index Sinhala, and never substitute a default list

Two changes again, and the second is independent of Sinhala.

**Change 4a:** index the Sinhala product names you already print on the site.

**Change 4b:** when nothing matches, return zero rows and `total_estimate=0` —
as `q=zzzqwerty` already correctly does. Do not fall back to a canned list.

**Tests:** `sinhala-is-indexed`, `no-junk-fallback-list`

```bash
curl -sS -G --data-urlencode "q=අටපිරිකර" \
  -H "Authorization: Bearer $KAPRUKA_API_KEY" -H "User-Agent: kapruka-mcp/1.0" \
  "$API?endpoint=products_search&limit=4"
call "endpoint=products_search&q=atapirikara&limit=4"   # control
```
| | |
|---|---|
| **Today** | Sinhala → `See Top Selling Liquor Products`, `Plush Toys`, `Roses`, `Shoes`, claiming `total_estimate=90`. `කිරිබත්` (milk rice) and `මල්` (flowers) return **the same four rows**. |
| **Control** | `atapirikara` → the correct `PIRIKARA0186/0190/0191` |
| **When fixed** | the Sinhala spelling finds the same products |

The request is fine — `applied_filters` echoes the script back intact
(`"q": "අටපිරිකර"`), so it is the index.
Offering liquor to someone asking for Buddhist alms goods is the part we would
most like to stop doing.

---

## 5. Keep navigation rows out of `products_search`

**Change:** exclude `CATSYM*` rows from product search, or give every row a type
field so a caller can drop them without pattern-matching an id prefix.

**Tests:** `catsym-not-in-product-search`, `rows-carry-a-type`

```bash
call "endpoint=products_search&q=cake&max_price=1&limit=6"
```
| | |
|---|---|
| **Today** | `rows=5` — a budget of **one rupee** finds five "products", each `in_stock: true`, `stock_level: "low"`, priced `0` |
| **When fixed** | zero rows |

Because they are priced 0 they satisfy every `max_price` ever sent. In the
USD-32 search from fault 1 they were 5 of 5 results.

---

## 6. Make `total_estimate` a count, or rename it

**Test:** `total-estimate-is-a-count`

It caps at 100, and `90` recurs for unrelated queries that returned junk —
`gyro ball`, `Kidsmarket`, three different Sinhala words all report 90. It
tracks the fallback path, not the catalogue. `0` is the only value that means
something today. It is the only size signal you expose, so it gets believed.

---

## Needs your test environment — we did not try these

- **Quantity is dropped from orders.** In `commerceOrderBean.createOrder`,
  quantity is priced but never written into `orderText` / `cartSnapShot`, so a
  "2 ×" line is stored once, charged twice, and stamped `#PRICEMISMATCH`. We did
  not prove it deliberately: proving it means putting a real order into your
  warehouse queue. We believe it is the most expensive item on this page.
- **Same-day delivery.** `delivery_check` branches on hour ≥ 17, < 5, and 5–17,
  so 17:00–23:59 for *today* is never evaluated. At 15:13 local both cities we
  tried refused same-day with `"reason": "Today's slots are full"` — which is not
  what a clock branch means. If the cutoff is noon, say the cutoff has passed.
  Call the endpoint after 17:00 and see whether it offers today.
- **Bank deposit FX.** `bank_deposit_info` converts USD orders at 140 LKR/USD.
  Your own product endpoint, asked the same minute, implies **269.95**:
  `CAKE00KA001912` is LKR 4500 and USD 16.67; `FLOWERS00T1875` is LKR 18000 and
  USD 66.67 → 269.99. Six overseas orders are reported to have gone through at
  roughly half price. Proving it end to end needs a real USD order id, which we
  will not create.
- **Variant parents.** We could not reproduce this read-only: no `_TC<n>` ids
  appear in searches for clothing, perfume, watches, sarees, hampers or bouquets,
  and every product we asked about returns one variant named `Default`. Please
  name a product that genuinely has variants and we will retest. Note that
  `products_search` rows carry no variant field at all, so a caller cannot tell a
  parent from a leaf.

## Things we are NOT asking you to fix

We checked these and they are ours or they are fine — listed so nobody spends a
day on them:

- **Your error statuses are correct.** `400 invalid_currency`,
  `404 product_not_found`, `400 invalid_parameter`, `404 city_not_found`, all
  with a clean `{"error":{...}}` envelope. The HTTP 200s we complained about come
  from our own MCP layer.
- **Long phrases work.** `q=happy birthday ribbon cake for boy teenage` returns
  three correct cakes.
- **`Nawaloka` searches correctly.** Other brand misses we could not substantiate.
- **CAD.** We were advertising it; you never supported it. You price LKR, USD,
  GBP, EUR and AUD, and reject CAD, JPY, SGD, AED and INR. We removed CAD from
  our client on 2026-09-21. Nothing for you to do unless you want to add it.
