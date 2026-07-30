#!/usr/bin/env bash
# Daily check for new rental listings in the FAIRMONT / north-Pacifica area.
# Pings the cherry-eric group chat (8164e7f75e994c769800691497b8c39a) when new ones appear.
#
# Criteria: 3+ bedrooms, <=$6000/mo, garage required, City == Pacifica,
#           and within the Fairmont / north-Pacifica geo box (lat 37.638-37.672, lng -122.495 to -122.456).
# State: ~/dispatch/state/fairmont-listings-seen.json (array of zpid strings)

set -uo pipefail

STATE_FILE="$HOME/dispatch/state/fairmont-listings-seen.json"
CHAT_ID="8164e7f75e994c769800691497b8c39a"
CHROME="$HOME/.claude/skills/chrome-control/scripts/chrome"
SEND="$HOME/.claude/skills/sms-assistant/scripts/send-sms"
LOG_DIR="$HOME/dispatch/state/fairmont-watch-logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/$(date +%Y-%m-%d).log"

echo "=== $(date) === fairmont rental watch run" >> "$LOG"

if [[ ! -f "$STATE_FILE" ]]; then
  echo '[]' > "$STATE_FILE"
fi

# Pacifica rentals: 3bd+, <=$6k, garage required. Map bounds cover north Pacifica.
SEARCH_URL='https://www.zillow.com/pacifica-ca/rentals/?searchQueryState=%7B%22pagination%22%3A%7B%7D%2C%22mapBounds%22%3A%7B%22north%22%3A37.69%2C%22south%22%3A37.62%2C%22east%22%3A-122.45%2C%22west%22%3A-122.51%7D%2C%22isMapVisible%22%3Atrue%2C%22filterState%22%3A%7B%22price%22%3A%7B%22max%22%3A6000%7D%2C%22mp%22%3A%7B%22max%22%3A6000%7D%2C%22beds%22%3A%7B%22min%22%3A3%7D%2C%22hasGarage%22%3A%7B%22value%22%3Atrue%7D%2C%22fr%22%3A%7B%22value%22%3Atrue%7D%2C%22fsba%22%3A%7B%22value%22%3Afalse%7D%2C%22fsbo%22%3A%7B%22value%22%3Afalse%7D%2C%22nc%22%3A%7B%22value%22%3Afalse%7D%2C%22cmsn%22%3A%7B%22value%22%3Afalse%7D%2C%22auc%22%3A%7B%22value%22%3Afalse%7D%2C%22fore%22%3A%7B%22value%22%3Afalse%7D%7D%2C%22isListVisible%22%3Atrue%7D'

# Fairmont / north-Pacifica geo box
LAT_MIN=37.638
LAT_MAX=37.672
LNG_MIN=-122.495
LNG_MAX=-122.456

TAB_JSON=$("$CHROME" open "$SEARCH_URL" 2>&1)
TAB_ID=$(echo "$TAB_JSON" | grep -oE 'tab [0-9]+' | awk '{print $2}')
if [[ -z "$TAB_ID" ]]; then
  echo "ERROR: failed to open Zillow tab. output: $TAB_JSON" >> "$LOG"
  exit 1
fi

sleep 9

# Parse __NEXT_DATA__ for listResults + relaxedResults, filter to Pacifica + geo box
LISTINGS_RAW=$("$CHROME" js "$TAB_ID" '(() => {
  try {
    const d = JSON.parse(document.getElementById("__NEXT_DATA__").textContent);
    const sr = d.props.pageProps.searchPageState.cat1.searchResults;
    const all = (sr.listResults || []).concat(sr.relaxedResults || []);
    const LAT_MIN='"$LAT_MIN"', LAT_MAX='"$LAT_MAX"', LNG_MIN='"$LNG_MIN"', LNG_MAX='"$LNG_MAX"';
    const seen = {};
    const out = [];
    all.forEach(r => {
      const h = r.hdpData && r.hdpData.homeInfo;
      if (!h) return;
      if (seen[h.zpid]) return;
      const city = (h.city || "").toLowerCase();
      const lat = h.latitude, lng = h.longitude;
      if (city !== "pacifica") return;
      if (!(lat >= LAT_MIN && lat <= LAT_MAX && lng >= LNG_MIN && lng <= LNG_MAX)) return;
      seen[h.zpid] = true;
      const rawPrice = h.price || h.priceForHDP;
      const priceStr = (typeof rawPrice === "number") ? rawPrice.toLocaleString("en-US") : (rawPrice || "?");
      out.push({
        zpid: String(h.zpid),
        addr: (h.streetAddress || "") + ", " + (h.city || "") + ", " + (h.state || "") + " " + (h.zipcode || ""),
        price: "$" + priceStr + "/mo",
        stats: (h.bedrooms || "?") + "bd/" + (h.bathrooms || "?") + "ba/" + (h.livingArea || "?") + " sqft",
        url: r.detailUrl ? ("https://www.zillow.com" + (r.detailUrl.startsWith("http") ? "" : "") + r.detailUrl.replace(/^https:\/\/www\.zillow\.com/, "")) : ("https://www.zillow.com/homedetails/" + h.zpid + "_zpid/")
      });
    });
    return JSON.stringify(out);
  } catch (e) {
    return JSON.stringify({error: String(e)});
  }
})()' 2>&1)

"$CHROME" close "$TAB_ID" >/dev/null 2>&1

# chrome js returns JSON-encoded; may be double-wrapped string. Unwrap.
LISTINGS=$(echo "$LISTINGS_RAW" | python3 -c '
import sys, json
raw = sys.stdin.read().strip()
try:
    parsed = json.loads(raw)
    if isinstance(parsed, str):
        parsed = json.loads(parsed)
    if isinstance(parsed, dict) and "error" in parsed:
        print("[]"); sys.exit(0)
    print(json.dumps(parsed if isinstance(parsed, list) else []))
except Exception:
    print(raw if raw.startswith("[") else "[]")
')

echo "Fairmont-area candidates: $LISTINGS" >> "$LOG"

# Compute new (zpid not in state)
NEW_LISTINGS=$(python3 - <<PYEOF
import json
try:
    listings = json.loads('''$LISTINGS''')
except Exception:
    listings = []
try:
    with open("$STATE_FILE") as f:
        seen = set(json.load(f))
except Exception:
    seen = set()
new = [l for l in listings if l.get("zpid") and l["zpid"] not in seen]
print(json.dumps(new))
PYEOF
)

NEW_COUNT=$(echo "$NEW_LISTINGS" | python3 -c "import sys, json; print(len(json.load(sys.stdin)))")
echo "New listings: $NEW_COUNT" >> "$LOG"

if [[ "$NEW_COUNT" -gt 0 ]]; then
  MSG=$(python3 - <<PYEOF
import json
new = json.loads('''$NEW_LISTINGS''')
lines = [f"🏡 {len(new)} new Fairmont-area Pacifica rental{'s' if len(new) != 1 else ''} (3bd+, garage, ≤\$6k):\n"]
for l in new:
    lines.append(f"• {l.get('addr','?')}")
    lines.append(f"  {l.get('price','?')} · {l.get('stats','?')}")
    lines.append(f"  {l.get('url','?')}")
    lines.append("")
print("\n".join(lines).rstrip())
PYEOF
)
  "$SEND" "$CHAT_ID" "$MSG" >> "$LOG" 2>&1
  echo "Sent SMS" >> "$LOG"
fi

# Update state
python3 - <<PYEOF
import json
try:
    listings = json.loads('''$LISTINGS''')
except Exception:
    listings = []
try:
    with open("$STATE_FILE") as f:
        seen = set(json.load(f))
except Exception:
    seen = set()
seen.update(l["zpid"] for l in listings if l.get("zpid"))
with open("$STATE_FILE", "w") as f:
    json.dump(sorted(seen), f, indent=2)
PYEOF

echo "Done." >> "$LOG"
