---
name: places
description: Drive times/distances between addresses (drive-time CLI, no API key), plus place search guidance. goplaces (Google Places) is NOT installed. Trigger words - places, nearby, restaurant, cafe, directions, drive time, how long to drive, commute, distance, find, location, business, open now.
---

# Places Skill

## RULE: never guess travel times

**Any drive/travel minutes not produced by a tool must be explicitly labelled as an estimate**
(e.g. "~40 min (my estimate, not from a routing tool)"). For driving, run `drive-time` below
instead of guessing. When quoting `drive-time`, keep its labels: free-flow is "no live traffic"
and the traffic range is an estimate.

## Directions / Drive Times — use `drive-time` (works today, no API key)

```bash
~/.claude/skills/places/scripts/drive-time "123 Main St, Pacifica, CA" "340 Walnut St, Redwood City, CA"
~/.claude/skills/places/scripts/drive-time "San Francisco, CA" "Redwood City, CA" --depart 08:15
~/.claude/skills/places/scripts/drive-time "FROM" "TO" --json
```

- Geocoding: OpenStreetMap Nominatim (1 req/s, cached in `~/.cache/drive-time/geocode.json`);
  falls back to the free US Census geocoder when OSM lacks the house number. Output marks
  `[street only ...]` if neither matched the number. Check the printed From/To lines.
- Routing: public OSRM demo server -> miles + **free-flow minutes (no live traffic)**.
- Traffic range (fixed multipliers, an ESTIMATE): `--depart` in 07:00-10:00 or 16:00-19:00
  -> rush hour x1.3-1.6; no `--depart` -> "typical traffic" x1.3-1.6; off-peak `--depart` -> x1.05-1.2.
- Google Routes live traffic is added automatically **only** if a key exists (env
  `GOOGLE_MAPS_API_KEY` or Keychain service `google-maps-api-key`). None is configured on this
  Mac as of 2026-10-20, so output is OSRM-only.
- Driving only (no transit/walking). Exit 1 = geocode/route failure (message on stderr), 2 = bad args.
- Tested 2026-10-20: Pacifica->Redwood City 21.5 mi / 30 min free-flow; Pacifica->3500 Homestead Rd
  Santa Clara 39.2 mi / 50 min; SF->Redwood City 26.6 mi / 36 min.

---

# goplaces (Google Places) — NOT INSTALLED, reference only

> **STATUS (verified 2026-09-05, re-checked 2026-10-20): `goplaces` is NOT installed on this machine,
> and there is no Google API key in Keychain (`google-api-key` / `google-maps-api-key` both absent).**
> `which goplaces` → not found; nothing under `~/code/` or `~/.claude/`. Every command
> below will fail with `command not found` until someone installs it.
>
> **Fallback that works today:** use `WebSearch` for place names + hours (it reliably
> returns per-stall hours for Singapore hawker centres, restaurant hours, etc.), and
> `~/.claude/skills/webfetch/scripts/webfetch <url>` for specific pages. Note the
> built-in `WebFetch` tool is disabled — it errors out and tells you to use the
> webfetch CLI instead.
>
> Known Cloudflare-blocked (fail even at webfetch tier 2, don't bother): `rome2rio.com`,
> `mynetbizz.com`. For transit routing, WebSearch the station names instead.


Search for places and get location information using the `goplaces` CLI (Google Places API).

## API Key

The API key is stored in macOS Keychain. To use goplaces, export it first:

```bash
export GOOGLE_PLACES_API_KEY=$(security find-generic-password -s "google-api-key" -w)
```

## Commands

### Text Search
```bash
# Search for places
goplaces search "coffee shops in San Francisco"
goplaces search "best pizza near me" --open-now
goplaces search "restaurants" --min-rating 4.5

# With filters
goplaces search "cafe" --price-level 2 --open-now --limit 5
```

### Nearby Search
```bash
# Find places near a location
goplaces nearby "37.7749,-122.4194" --type restaurant --radius 500
goplaces nearby "Times Square, NYC" --type cafe
```

### Place Details
```bash
# Get full details for a place
goplaces details <place_id>
goplaces details ChIJN1t_tDeuEmsRUsoyG83frY4
```

### Autocomplete
```bash
# Get place suggestions as you type
goplaces autocomplete "star"
goplaces autocomplete "blue bottle coff"
```

### Directions/Routes
**Do not use `goplaces route` — not installed. Use `drive-time` (top of this file).**

### Photos

> **`scripts/place-photos` is BROKEN (checked 2026-10-20):** `--help` works, but every real run
> shells out to `goplaces` (not installed -> FileNotFoundError) and reads Keychain `google-api-key`
> (absent). Don't use it until goplaces + a key exist. For place photos, find images via
> WebSearch / webfetch instead.

```bash
# Get place details WITH photos (returns photo names)
goplaces details <place_id> --photos

# Get actual photo URL from photo name
goplaces photo "<photo_name>" --max-width 800
goplaces photo "places/ChIJ.../photos/ATCDNf..." --max-width 1200
```

### Downloading Photos for SMS
When sharing place photos via SMS, download to /tmp and use `reply --image`:

```bash
# Get photo URL (use --json and extract photo_uri)
export GOOGLE_PLACES_API_KEY=$(security find-generic-password -s "google-api-key" -w)
URL=$(goplaces photo "<photo_name>" --max-width 800 --json | jq -r '.photo_uri')

# Download to /tmp
curl -sL "$URL" -o /tmp/place_photo.jpg

# Send via SMS (from transcript directory)
~/.claude/skills/sms-assistant/scripts/reply "Place Name - details" --image /tmp/place_photo.jpg
```

This shows the actual image in iMessage instead of a URL link.

## Output Options

```bash
# JSON output for scripting
goplaces search "coffee" --json

# Plain text for parsing
goplaces search "coffee" --plain

# Limit results
goplaces search "restaurants" --limit 10
```

## Usage Pattern

Always export the API key before running commands:

```bash
export GOOGLE_PLACES_API_KEY=$(security find-generic-password -s "google-api-key" -w) && goplaces search "coffee near me"
```

## Notes

- The Places API incurs usage costs - use responsibly
- Results include place_id which can be used for detailed lookups
- Use `--open-now` to filter for currently open businesses
- Use `--min-rating` to filter by Google rating (1-5)

## Live busyness / lines
Google Maps shows a live "Popular times" reading ("LIVE As busy as it gets", "Less busy than usual") plus current hours. Read it with `chrome open "https://www.google.com/maps/search/<name>+<address>"`, wait ~10s, then `chrome text <tab> | grep -i -E "LIVE|busy|Closes"`. Use it when someone asks about lines or waits instead of guessing. Google's hours beat OpenStreetMap's (OSM hours can be stale).
