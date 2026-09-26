#!/usr/bin/env bash
# Acceptance tests for the Kapruka commerce API (commerce_phase1.jsp).
#
# Each check is one request plus the assertion we need to hold. Run it before
# you change anything to see the current state, and again afterwards. When all
# of them say PASS, the faults in UPSTREAM_API_FAULTS.md are fixed.
#
#   export KAPRUKA_API_KEY=<the phase-1 agent token>
#   bash kapruka_api_acceptance.sh
#
# Needs bash, curl and python3. A real User-Agent is required: Cloudflare Bot
# Fight Mode drops curl's default one.

set -uo pipefail
API="${KAPRUKA_API_BASE_URL:-https://www.kapruka.com}/tools/commerce_phase1.jsp"
: "${KAPRUKA_API_KEY:?set KAPRUKA_API_KEY first}"

PASS=0; FAIL=0

get() {  # get <querystring...>  -> body on stdout
  curl -sS -G "$@" \
    -H "Authorization: Bearer $KAPRUKA_API_KEY" \
    -H "User-Agent: kapruka-acceptance/1.0" \
    -H "Accept: application/json" "$API"
}

# check <id> <what must be true> <python assertion over `d`, printing a reason>
check() {
  local id="$1" want="$2" code="$3" body="$4"
  local out
  out=$(printf '%s' "$body" | python3 -c "
import json, sys
raw = sys.stdin.read()
try:
    d = json.loads(raw)
except Exception:
    print('FAIL|response was not JSON: ' + raw[:120]); sys.exit()
rows = d.get('results', [])
def price(r):
    return (r.get('price') or {}).get('amount')
$code
" 2>&1)
  if [ "${out%%|*}" = "PASS" ]; then
    PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m  %-34s %s\n' "$id" "${out#*|}"
  else
    FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m  %-34s %s\n' "$id" "${out#*|}"
    printf '        want: %s\n' "$want"
  fi
}

echo
echo "=== 1. price bounds must mean the currency the caller asked for ============"

check "usd-range-returns-something" \
  "a USD 10-30 budget returns cakes priced USD 10-30" \
  "
bad = [price(r) for r in rows if price(r) is not None and not (10 <= price(r) <= 30)]
if not rows:
    print('FAIL|0 rows: a \$10-30 cake has no answer')
elif bad:
    print('FAIL|rows outside the bound: %s' % bad[:5])
else:
    print('PASS|%d rows, all USD 10-30' % len(rows))
" \
  "$(get --data-urlencode 'endpoint=products_search' --data-urlencode 'q=cake' \
        --data-urlencode 'currency=USD' --data-urlencode 'min_price=10' \
        --data-urlencode 'max_price=30' --data-urlencode 'limit=6')"

check "usd-min-is-a-minimum" \
  "no row is cheaper than min_price when currency=USD" \
  "
under = [(r['id'], price(r)) for r in rows if price(r) is not None and price(r) < 50]
print('FAIL|rows below the USD 50 minimum: %s' % under[:3] if under else 'PASS|no row under USD 50')
" \
  "$(get --data-urlencode 'endpoint=products_search' --data-urlencode 'q=cake' \
        --data-urlencode 'currency=USD' --data-urlencode 'min_price=50' --data-urlencode 'limit=6')"

# The same NUMBER in two currencies must not mean the same thing. Use a bound
# that bites: comparing the top six of a loose bound cannot see the difference
# (the six best-ranked cakes are under both LKR 9600 and USD 9600).
A=$(get --data-urlencode 'endpoint=products_search' --data-urlencode 'q=cake'        --data-urlencode 'currency=USD' --data-urlencode 'max_price=20' --data-urlencode 'limit=6')
B=$(get --data-urlencode 'endpoint=products_search' --data-urlencode 'q=cake'        --data-urlencode 'currency=LKR' --data-urlencode 'max_price=20' --data-urlencode 'limit=6')
check "bound-is-currency-sensitive"   "max_price=20 finds cakes in USD and none in LKR (no cake costs 20 rupees)"   "
import json
other = json.loads(open('/tmp/_acc_b.json', encoding='utf-8').read())
lkr = other.get('results', [])
if not rows:
    print('FAIL|USD 20 found nothing')
elif lkr:
    print('FAIL|LKR 20 found %d rows' % len(lkr))
else:
    print('PASS|USD 20 -> %d cakes, LKR 20 -> none' % len(rows))
"   "$(printf '%s' "$B" > /tmp/_acc_b.json; printf '%s' "$A")"

echo
echo "=== 2. an unknown category must not be answered with silence =============="

check "unknown-facet-is-not-silent"   "400 invalid_category listing valid_categories, or the filter ignored and dropped from applied_filters"   "
err = d.get('error') or {}
af = d.get('applied_filters') or {}
if err.get('code') == 'invalid_category':
    vc = (err.get('details') or {}).get('valid_categories') or []
    print('PASS|invalid_category, %d valid names offered' % len(vc) if vc else 'FAIL|invalid_category but no valid_categories')
elif err:
    print('FAIL|unexpected error %s' % err.get('code'))
elif af.get('category') == 'ZZZ_NOT_A_REAL_FACET':
    print('FAIL|bad facet echoed back as if honoured (%d rows)' % len(rows))
else:
    print('PASS|filter ignored and dropped from applied_filters')
"   "$(get --data-urlencode 'endpoint=products_search' --data-urlencode 'q=birthday cake'         --data-urlencode 'category=ZZZ_NOT_A_REAL_FACET' --data-urlencode 'limit=6')"

check "search-publishes-its-facets" \
  "the search response carries the facet names valid for this query" \
  "
keys = sorted(d.keys())
has = [k for k in keys if 'facet' in k or 'categor' in k]
print('PASS|facet list present: %s' % has if has else 'FAIL|no facet list; top-level keys are %s' % keys)
" \
  "$(get --data-urlencode 'endpoint=products_search' --data-urlencode 'q=birthday cake' --data-urlencode 'limit=2')"

# The `categories` endpoint is site NAVIGATION, a different vocabulary from the
# search facets (Kapruka, 2026-09-26) — so the contract to test is that the
# facet names a search publishes are usable as its filter.
FACET=$(get --data-urlencode 'endpoint=products_search' --data-urlencode 'q=phone' --data-urlencode 'limit=1'   | python3 -c "import json,sys; f=(json.load(sys.stdin).get('facets') or {}).get('categories') or []; print(f[0]['name'] if f else '')")
check "published-facet-names-work"   "the first facet a search publishes works as its category filter"   "
if not '$FACET':
    print('FAIL|q=phone published no facets')
else:
    print('PASS|category=%s -> %d rows' % ('$FACET', len(rows)) if rows else 'FAIL|category=%s (published by the same search) -> 0 rows' % '$FACET')
"   "$(get --data-urlencode 'endpoint=products_search' --data-urlencode 'q=phone'         --data-urlencode "category=$FACET" --data-urlencode 'limit=3')"

echo
echo "=== 3. relevance: one matching token must not be enough ==================="

check "noise-token-cannot-invent-hits" \
  "'Largactil 50mg' behaves like 'Largactil' (0 rows), not like 'mg'" \
  "
auto = [r['name'] for r in rows if 'Mg Zs' in r.get('name','') or 'Mixer Grinder' in r.get('name','')]
if auto:
    print('FAIL|a prescription drug query returned car parts: %s' % auto[:2])
elif not rows:
    print('PASS|0 rows, same as the single-token control')
else:
    print('PASS|%d rows, none automotive' % len(rows))
" \
  "$(get --data-urlencode 'endpoint=products_search' --data-urlencode 'q=Largactil 50mg' --data-urlencode 'limit=4')"

check "head-noun-outranks-one-token"   "for 'medipedic walker' the top hit is a mobility walker, not liquor or a baby/toddler item"   "
top = (rows[0].get('name') if rows else '')
bad = [w for w in ('johnnie', 'label', 'baby', 'toddler', 'shoe', 'treadmill') if w in top.lower()]
if not rows:
    print('FAIL|0 rows (the catalogue has mobility walkers)')
elif bad or 'walker' not in top.lower():
    print('FAIL|top hit is %r' % top)
else:
    print('PASS|top hit is %r' % top)
"   "$(get --data-urlencode 'endpoint=products_search' --data-urlencode 'q=medipedic walker' --data-urlencode 'limit=5')"

echo
echo "=== 4. Sinhala ============================================================"

check "sinhala-is-indexed" \
  "the Sinhala spelling finds what the transliteration finds" \
  "
ids = [r['id'] for r in rows]
if any(i.startswith('PIRIKARA') for i in ids):
    print('PASS|found %s' % [i for i in ids if i.startswith('PIRIKARA')][:3])
elif d.get('total_estimate') in (0, None) and not rows:
    print('FAIL|0 rows (honest, but q=atapirikara finds PIRIKARA products)')
else:
    print('FAIL|returned %s claiming total_estimate=%s' % (ids[:3], d.get('total_estimate')))
" \
  "$(get --data-urlencode 'endpoint=products_search' --data-urlencode 'q=අටපිරිකර' --data-urlencode 'limit=4')"

check "no-junk-fallback-list" \
  "a query that matches nothing says so, instead of returning a default list" \
  "
ids = [r['id'] for r in rows]
if all(i.startswith('CATSYM') for i in ids) and ids:
    print('FAIL|fell back to %s with total_estimate=%s' % (ids[:3], d.get('total_estimate')))
else:
    print('PASS|no fallback list')
" \
  "$(get --data-urlencode 'endpoint=products_search' --data-urlencode 'q=කිරිබත්' --data-urlencode 'limit=4')"

echo
echo "=== 5. navigation rows must not be sold as products ======================="

check "catsym-not-in-product-search" \
  "a one-rupee budget finds nothing, because nothing costs one rupee" \
  "
stubs = [r['id'] for r in rows if r['id'].upper().startswith('CATSYM')]
if stubs:
    print('FAIL|%d navigation rows priced 0 passed max_price=1: %s' % (len(stubs), stubs[:3]))
else:
    print('PASS|%d rows, no CATSYM' % len(rows))
" \
  "$(get --data-urlencode 'endpoint=products_search' --data-urlencode 'q=cake' \
        --data-urlencode 'max_price=1' --data-urlencode 'limit=6')"

check "rows-carry-a-type" \
  "a caller can tell a product from a navigation link without reading the id" \
  "
r = rows[0] if rows else {}
t = [k for k in r if k in ('type','kind','result_type','is_product')]
print('PASS|type field: %s' % t if t else 'FAIL|no type field; row keys are %s' % sorted(r.keys()))
" \
  "$(get --data-urlencode 'endpoint=products_search' --data-urlencode 'q=cake' --data-urlencode 'limit=2')"

echo
echo "=== 6. total_estimate ====================================================="

check "total-estimate-is-a-count" \
  "total_estimate reflects matches, not a fallback constant" \
  "
te = d.get('total_estimate')
print('FAIL|q=gyro ball returns unrelated rows yet claims total_estimate=%s' % te if te == 90 else 'PASS|total_estimate=%s' % te)
" \
  "$(get --data-urlencode 'endpoint=products_search' --data-urlencode 'q=gyro ball' --data-urlencode 'limit=2')"

rm -f /tmp/_acc_b.json
echo
printf '  %d passed, %d failed\n\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
