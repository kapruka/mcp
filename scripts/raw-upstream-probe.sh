#!/usr/bin/env bash
# Raw upstream probe, run ON the MCP host. No MCP layer in the path.
set -a; . /srv/kapruka-mcp/.env; set +a

cat > /tmp/fmt.py <<'PYEOF'
import json, sys
d = json.load(sys.stdin)
r = d.get("results", [])
real = [x for x in r if (x.get("price") or {}).get("amount")]
af = d.get("applied_filters")
te = d.get("total_estimate")
print("    rows=%d  priced=%d  total_estimate=%s" % (len(r), len(real), te))
print("    applied_filters=%s" % (json.dumps(af),))
for x in r[:4]:
    p = x.get("price") or {}
    name = (x.get("name") or "")[:44]
    print("      %-22s %-9s %-4s %s" % (x.get("id"), p.get("amount"), p.get("currency"), name))
PYEOF

q() {  # q <label> <querystring>
  local label="$1"; shift
  echo "--- $label"
  echo "    $*"
  curl -sS \
    -H "Authorization: Bearer $KAPRUKA_API_KEY" \
    -H "User-Agent: kapruka-mcp/1.0" -H "Accept: application/json" \
    "$KAPRUKA_API_BASE_URL/tools/commerce_phase1.jsp?endpoint=products_search&$*" \
  | python3 /tmp/fmt.py
}

echo "===== A. PRICE BOUND vs CURRENCY ====="
q "USD cap 32 (a real customer budget)"        "q=cake&currency=USD&max_price=32&limit=6"
q "USD cap 9600 (the same budget in RUPEES)"   "q=cake&currency=USD&max_price=9600&limit=6"
q "USD cap 100"                                "q=cake&currency=USD&max_price=100&limit=6"
q "LKR cap 9600 (control)"                     "q=cake&currency=LKR&max_price=9600&limit=6"
q "USD no cap (control)"                       "q=cake&currency=USD&limit=6"

echo
echo "===== B. CATEGORY FACET ====="
q "category=Cakes (a department name)"     "q=birthday%20cake&category=Cakes&limit=6"
q "category=Kapruka Cakes (a real facet)"  "q=birthday%20cake&category=Kapruka%20Cakes&limit=6"
q "category=ZZZ_NOT_A_REAL_FACET"          "q=birthday%20cake&category=ZZZ_NOT_A_REAL_FACET&limit=6"
q "no category (control)"                  "q=birthday%20cake&limit=6"
