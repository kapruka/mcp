# Handover: prove the upstream API bugs, in calls Kapruka can paste

**Written 2026-09-21.** For a Claude session working in `kapruka_mcp`.

## What you are being asked to do

Kapruka's own API (`commerce_phase1.jsp` and friends) returns wrong data for
some inputs. We know about three faults and suspect a dozen more. Your job is
**not** to fix anything. It is to produce a bug report their developers can act
on: for each fault, one request they can paste into a terminal, what it returns,
what it should have returned, and what it costs.

The MCP has been patching around these faults, which is why nobody upstream has
had to look at them. That has to stop being the plan. Every workaround in
`src/tools/products.py` is code we would delete the day the API is correct.

**Deliverable:** `docs/UPSTREAM_API_FAULTS.md` in this repo, written for someone
who has never heard of the MCP. Format is at the bottom of this file.

## Rules of engagement

1. **Read-only endpoints only.** `products_search`, `product`, `categories`,
   `product_related`, `delivery_cities`, `delivery_rates`, `delivery_check`,
   `order_tracking`. **Never call `create_order`** — it writes a real order into
   a real warehouse queue. If a suspected fault can only be proven by ordering,
   write it up as "needs a Kapruka-side test", do not place the order.
2. **Never put the API key in the report, a commit, or a terminal you paste
   from.** Every example uses `$KAPRUKA_API_KEY`, never the value.
3. **Test the API, not the MCP.** Two of the three known faults are now masked
   by the MCP, so a wrapper test will show you a clean result and you will
   conclude the bug is gone. It is not gone. Details below.
4. **One claim, one call.** A fault that needs three paragraphs of setup will be
   ignored. If you cannot reduce it to a single request plus a control request,
   you have not finished understanding it.
5. **Always include a control.** "This returns nothing" is not a bug report.
   "This returns nothing while *that* returns 90 matching products" is.

## How to call the API directly

The API lives on `www.kapruka.com`, behind Cloudflare, and answers on a JSP
endpoint with `?endpoint=<name>` selecting the operation. Auth is a bearer
token. **The default `curl`/`python-httpx` user agent is caught by Cloudflare
Bot Fight Mode**, so send a real one.

Run from the MCP host, where the credentials already live:

```bash
ssh -i ~/.ssh/javalounge_newserver_ed25519 roman@23.111.183.156
```

Then, as root (`roman` has passwordless sudo):

```bash
sudo -n bash -c 'set -a; . /srv/kapruka-mcp/.env; set +a
curl -sS \
  -H "Authorization: Bearer $KAPRUKA_API_KEY" \
  -H "User-Agent: kapruka-mcp/1.0" \
  -H "Accept: application/json" \
  "$KAPRUKA_API_BASE_URL/tools/commerce_phase1.jsp?endpoint=products_search&q=cake&currency=USD&max_price=32&limit=6"'
```

There is a ready-made probe harness at `/tmp/raw_probe.sh` on that host (also in
this session's scratchpad) that formats the results into one line per row. Copy
its `q()` helper rather than writing your own.

Three practical notes. The host's `python3` is old enough to reject a backslash
inside an f-string, so format with `%` instead. `products_search` defaults to
`in_stock_only=true` even when you do not send it, which you will see echoed
back in `applied_filters`. And `total_estimate` appears to cap at 100, so use it
for "many" versus "few", never as a count.

**Phase 2 endpoints** (`customer_details`, `order_history`,
`customer_addresses`) live on `commerce_phase2.jsp` with a different bearer
token, and **phase 3** (custom cakes) on `commerce_phase3.jsp` with a third. See
`src/api/client.py`. Phase 2 exposes customer data — only touch it if a fault
genuinely needs it, and never put a real customer's details in the report.

## Do not test through the MCP

`kapruka_search_products` now compensates for two of the faults below:

- **v0.4.4** converts a price bound into rupees before passing it on, so a
  dollar budget behaves correctly through the MCP and wrongly through the API.
- **v0.4.2/0.4.3** retry a search without the `category` filter when it comes
  back empty, so a bad facet name looks survivable.
- The MCP also **filters out `CATSYM*` rows**, which is why the third fault is
  invisible from up there.

If you need to see the raw behaviour and only have the MCP handy, pass
`include_stubs: true` and read the `price_bounds_converted_to_lkr` and
`price_filtered_locally` fields to know what it did. But prefer the direct call.

---

# Fault 1 — a price bound is compared in rupees whatever currency you asked for

**Confirmed at the API level 2026-09-21.** This is the strongest of the three;
lead the report with it.

The response converts the **prices it returns** into the requested currency, but
compares `min_price` / `max_price` against the raw rupee figure. So a customer's
dollar budget is silently tested against rupee numbers.

```bash
# A real customer budget: a cake under USD 32.
...endpoint=products_search&q=cake&currency=USD&max_price=32&limit=6
#   -> rows=5, priced=0, total_estimate=5
#      every row is a CATSYM category stub priced 0 (see Fault 3)
#      NOT ONE BUYABLE PRODUCT

# The same budget expressed in rupees, still asking for USD prices.
...endpoint=products_search&q=cake&currency=USD&max_price=9600&limit=6
#   -> rows=6, priced=3, total_estimate=90
#      CAKE00KA001912  USD 16.67  Pastel Grace Bento Ribbon Cake And Cupcake Set

# Control: the same number as a rupee bound.
...endpoint=products_search&q=cake&currency=LKR&max_price=9600&limit=6
#   -> rows=6, priced=3, total_estimate=90
#      CAKE00KA001912  LKR 4500   (the same product, the same set)
```

The USD-9600 and LKR-9600 searches return an **identical result set**. That is
the proof: the bound is not interpreted in the requested currency at all.

A second angle, useful because the absurdity is visible without arithmetic:

```bash
...endpoint=products_search&q=cake&currency=USD&max_price=100&limit=6
#   -> total_estimate=9, and the priced row is a greeting card at USD 0.30
#      A hundred-dollar cake budget returns a 30-cent card, because it was
#      read as "under 100 rupees".
```

`applied_filters` echoes `"max_price": 32.0` with no unit, so a caller cannot
even detect the mismatch from the response.

**What it should do:** compare the bound in the currency named by `currency`.

**What it cost:** most of the WhatsApp sales agent's traffic is overseas, so
every dollar budget a customer gave was tested against rupee numbers and
answered "we don't have that". Nine of the eighteen searches still returning
nothing in a five-day replay were this.

**Your job here:** re-confirm the three calls above from a clean shell, check
whether `min_price` has the same fault (we believe it does, prove it), and check
whether `delivery_rates` or any other endpoint takes a price in a currency.

---

# Fault 2 — an unknown category is answered with silence, not an error

`category` is the website's **subcategory facet** (`subcat=` in
`srilanka_online_search.jsp`), not a department name. Fine. The fault is what
happens when you get the name wrong:

```bash
# A department name, the obvious guess, and what our tool description used to suggest.
...endpoint=products_search&q=birthday%20cake&category=Cakes&limit=6
#   -> rows=0, total_estimate=0, applied_filters echoes "category": "Cakes"

# A facet name that cannot possibly exist.
...endpoint=products_search&q=birthday%20cake&category=ZZZ_NOT_A_REAL_FACET&limit=6
#   -> rows=0, total_estimate=0, applied_filters echoes it back unchanged

# Control: the real facet.
...endpoint=products_search&q=birthday%20cake&category=Kapruka%20Cakes&limit=6
#   -> rows=6, all six real cakes, LKR 4,850-8,800
```

A name that does not exist and a name that exists but matches nothing are the
**same response**: HTTP 200, zero rows, the bad value echoed back as if it had
been honoured. The caller has no way to tell "you made a typo" from "we have no
cakes".

**What it should do:** either reject an unknown facet (`400 invalid_category`
naming the valid set) or ignore it and say so in `applied_filters`. Either is
fine. Silence is not.

**Compounding fault, report it in the same section:** nothing anywhere
advertises the valid facet names. `products_search` returns no facet list, only
`results` / `next_cursor` / `total_estimate` / `applied_filters`, and
`kapruka_list_categories` is a different vocabulary whose own `cakes` and
`flowers` entries are not searchable. The website's own sidebar computes the
facet list for every query; **asking for it in the search response is the single
highest-value change on this list.** With the right facet, "birthday cake"
returns 10 correct cakes out of 10. Without it, 2 out of 7, with four greeting
cards and a rose vase above the cakes.

**What it cost:** 63 searches in five days used a department name, 75 came back
empty (Cakes 40 of 40, Flowers 23 of 23), and every one was a customer asking
for a cake or flowers.

**Your job here:** confirm the above, then find out whether `category` is
case-sensitive, whether partial names work (`birthday` alone did, check it
again), and whether the valid facet set is genuinely query-dependent or a fixed
list we could be handed once.

---

# Fault 3 — category shortcuts are returned as products, priced zero, and pass every filter

```bash
...endpoint=products_search&q=cake&currency=USD&max_price=32&limit=6
#   CATSYM00230  0 USD  See Top Selling Cakes
#   CATSYM00233  0 USD  See Top Selling Birthday Cakes
#   CATSYM00380  0 USD  Custom Printed Cakes
#   CATSYM00306  0 USD  See Top Selling Bride To Be Cakes
```

`CATSYM*` rows are navigation shortcuts, not things anyone can buy. They come
back from a **product** search, carry `in_stock: true`, are priced `0`, and
because they are priced 0 they **satisfy every `max_price` filter ever sent**.
In the USD-32 search above they are 5 of 5 results: a caller filtering for
buyable items is left with nothing, while the response claims five hits.

**What it should do:** leave them out of `products_search`, or mark them with a
type so a caller can drop them without pattern-matching on an ID prefix. We
currently drop anything whose ID starts `CATSYM`, which is exactly the kind of
guess that breaks silently when the prefix changes.

---

# Suspects — not yet proven, go get evidence

Ordered by how much they are costing. Each needs the same treatment: one call,
one control, expected versus actual. Drop any that turn out to be our fault, and
say so — a report that survives scrutiny is worth more than a long one.

**Relevance is single-token OR matching with no head-noun weighting.** Real
examples from customer chats, all of which returned confident nonsense: gyro
ball → gyro bowl, Largactil 50mg → an MG car wiper, Apple gift card → fresh
apples, staple gun → taser gun, medipedic walker → Johnnie Walker, ice cream →
cakes. The user asked for a noun and got anything sharing one token. Prove it
with two or three of these and state the rule you think it is applying.

**Sinhala script returns nothing.** Six queries in Sinhala across five days, all
zero results, while the transliterated spelling works. Try `අටපිරිකර` and a
Sinhala book title. Check whether it is the index or the request encoding — if
`applied_filters` echoes the Sinhala back correctly, the request survived and
the index is the problem.

**Brand, author and vendor names are not indexed.** Nawaloka, Tobi, Yapa
Bandara, Mithuri and Kidsmarket all failed while the products existed and were
findable by description. Pick two, show the failed search and the product that
exists.

**No fuzzy matching on near-exact titles.** "Mother Touch Gift Box" finds
nothing; the catalogue holds "Mothers Touch Gift Box". One letter.

**Long phrases return nothing rather than degrading.** "happy birthday ribbon
cake for boy teenage" is empty while "ribbon cake" is not. Find where the cliff
is: at what word count does a query stop matching?

**`CAD` is advertised and rejected.** The MCP offers CAD in its supported list;
the API answers `Error (invalid_currency): Currency code is not in the supported
list`. Establish which currencies the API actually accepts, and whether any of
them lack a price list (a currency that returns LKR-equal prices is worse than
one that errors).

**Failures arrive as HTTP 200.** Throttles and errors come back `200` with
`isError: false` and a body starting `Error:` or `Error (code):`. A rate limit
should be `429` with `Retry-After`. This is also why two other Python clients on
that server silently lose 17–36% of their searches. Demonstrate it by tripping
the limit and showing the status line.

**Quantity is dropped from orders.** Already diagnosed in
`commerceOrderBean.createOrder`: quantity is priced but never written into
`orderText`/`cartSnapShot`, so a "2 ×" order is stored once, charged twice, and
stamped `#PRICEMISMATCH`. **Do not prove this by placing an order.** Write it up
from the existing evidence and let Kapruka reproduce it in their own test
environment.

**Same-day delivery is offered all evening.** `delivery_check` branches on hour
≥ 17, < 5, and 5–17, so the 17:00–23:59 case for *today* is never evaluated and
the API reports same-day available at 9pm. The same function refuses genuine
same-day orders from 05:00. Both halves are provable read-only by calling
`delivery_check` at different hours — do it from the server, whose clock is UTC,
and be explicit about which local hour each call represents.

**Bank deposits for dollar orders are priced at a 2022 exchange rate.**
`bank_deposit_info` converts a USD order at 140 LKR/USD, roughly half the real
rate. Read-only to check. Six overseas orders went through at that price.

**A parent product with variants is orderable.** Variants are child products
`<parent>_TC<n>`; search gives no signal that a product has them and the
ordering path accepts the bare parent, which reaches the warehouse as an
un-pickable line. Prove the *search* half read-only: show a product whose
`product` endpoint returns variants while `products_search` says nothing about
them.

---

## Report format

Write `docs/UPSTREAM_API_FAULTS.md`. One section per fault, severest first.
Assume the reader is a Kapruka developer with a terminal, no context, and
limited patience.

```markdown
## N. <one line saying what is wrong, in plain words>

**Severity:** what it costs, in customers or money, with a number if we have one.

**Reproduce:**
    <the exact request, with $KAPRUKA_API_KEY as a variable>

**Returns:**
    <the actual response, trimmed to what matters>

**Control:**
    <the request that proves the catalogue is not the problem>
    <its response>

**Expected:** <what a correct API returns here>

**Where we think it is:** <file/method if known, or "not investigated">

**What we do meanwhile:** <the MCP workaround, and that we would delete it>
```

Keep the whole document under two pages of screen. If a fault needs more room
than the template gives it, it is two faults.

## When you are done

Leave the report in the repo, commit it, and hand back a short list: which
faults you confirmed, which you disproved, and which you could not test without
Kapruka's help. Say plainly if a fault in this handover turned out to be wrong —
one of the claims in the original study ("price caps destroy relevance") was our
own bug, not theirs, and we published a correction. Better that than sending
Kapruka on a hunt for something we did to ourselves.

## Background, if you want it

- `eagle-dashboard/docs/product-search-tuning/AFZAL_SEARCH_MISSES_2026-09-20.md`
  — the five-day study of 591 customer conversations and 1,087 searches this all
  came from.
- `eagle-dashboard/docs/product-search-tuning/REPLAY_2026-09-21.md` — the 77
  failed searches replayed after the first fixes.
- `src/tools/products.py` — every workaround, each with a comment saying which
  fault it exists for.
- `tests/test_price_bounds.py` — what correct behaviour looks like, in
  executable form.
