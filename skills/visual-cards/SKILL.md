---
name: visual-cards
description: Editorial-style infographic cards (IG/TikTok travel-guide aesthetic) for trip planning, option comparison, and any content where a long SMS text wall would be uglier than a styled image. Trigger words - travel plan, itinerary, day by day, options card, compare options, infographic, visual card, trip card.
---

# Visual Cards

Generate editorial-style infographic cards (~1080x1920 portrait, IG/TikTok-feed aesthetic) for content that's better read as an image than as a text wall. Send via iMessage with `reply --image`.

## When to Use

✅ **Travel itineraries** — day-by-day cards with hero photos of actual activities/places
✅ **Option comparisons** — hotel picks, product picks (coffee grinders, headphones), flight options ranked side-by-side
✅ **Any "shortlist" longer than 4-5 items** — once a text reply hits 10+ lines, switch to cards
✅ **Mixed content with photos that matter** — restaurants, neighborhoods, products

❌ NOT for quick yes/no answers, single facts, replies to direct questions
❌ NOT for things where the text IS the value (code, addresses, links to tap)

## Format Principles

Style baked in from a 4-iteration round with admin (May 2026):

1. **Full-bleed atmospheric hero photo** as background. Moody, evocative, on-topic — not generic stock.
2. **Polaroid-style activity tiles** floating above the hero (2-3 of them, tilted slightly, drop-shadowed) showing specific places/things mentioned in the text.
3. **Dark gradient overlay** at the bottom for text legibility (`linear-gradient(180deg, rgba(0,0,0,0.4) 0%, rgba(0,0,0,0.1) 45%, rgba(0,0,0,0.92) 100%)`)
4. **Editorial typography** — Playfair Display (serif) for "Day X" + subtitle, Inter (sans) for body. Big sizes (90-110px headline).
5. **Italic poetic line at the bottom** in serif italic, color slightly warm/off-white. Adds vibe.
6. **Eyebrow** (uppercase, letter-spaced, top-left): location + date.
7. **Page indicator tag** (pill, top-right): "1 / 4" style.
8. **Accent color**: warm orange `#f0883e` for bullet markers, eyebrow.
9. **Pure black backgrounds = bad vibe.** Always have a photo behind, gradient on top.

## Quickstart

```bash
# 1. Pick image IDs from Unsplash (search via the helper)
~/dispatch/skills/visual-cards/scripts/find-images "lower broadway nashville"

# 2. Author a card spec as YAML (see templates/example.yaml)
$EDITOR /tmp/my-card.yaml

# 3. Render to PNG → JPEG, ready to send
~/dispatch/skills/visual-cards/scripts/render /tmp/my-card.yaml --out /tmp/my-card.jpg

# 4. Send via iMessage
~/.claude/skills/sms-assistant/scripts/reply "caption" --image /tmp/my-card.jpg
```

## Card Specs (YAML)

Minimum:
```yaml
template: day-itinerary       # one of: day-itinerary, option-rank
hero_image: /path/to/hero.jpg # full-bleed background
eyebrow: "Nashville · Jul 3"  # uppercase, top-left
page_tag: "1 / 4"             # pill, top-right
title: "Day 1"                # big serif headline
subtitle: "Arrival + first honky tonk"
tiles:                        # 2 polaroid-style activity photos
  - { image: /path/to/hattie-bs.jpg, label: "Hattie B's" }
  - { image: /path/to/broadway.jpg, label: "Lower Broadway" }
bullets:
  - "Land BNA · Uber to downtown · drop bags"
  - "Dinner: Hattie B's hot chicken"
  - "Walk Lower Broadway"
poetic: "No itinerary. Just walk into whichever bar has the best fiddle."
```

### `option-rank` template (product / option comparison)

For ranked picks with prices — build tiers, product shortlists, anything with a `name | note | price` shape.
Added 2026-09-18 (SKILL.md had referenced this template for months but the HTML file did not exist — if `render` says "template not found", that's the bug).

```yaml
template: option-rank
hero_image: /tmp/cards/hero-office.jpg
eyebrow: "Office audio · Sep 18"
page_tag: "3 / 5"
title: "The Pick"
subtitle: "What I'd actually buy"
rows:
  - { name: "Kanto ORA", note: "powered, USB-C in, desk-sized", price: "$349", pick: true, badge: "best value" }
  - { name: "JBL 305P MkII", note: "studio monitor, needs an interface", price: "$358" }
total: "All in: <b>$698</b> shipped"
poetic: "Buy the sub last. You'll know if you need it."
```

Row fields: `name`, `note`, `price`, `pick` (bool → orange highlight), `badge` (small uppercase label).
`total` is **not** HTML-escaped so you can use `<b>` — never interpolate untrusted text into it.
Keep it to **6-7 rows max**; past that the rows compress and it becomes the text-wall failure mode again.

### `listing-picks` template (marketplace / product listings with real photos)
For used-item finds and listings where the PHOTO is the point. 2-3 stacked rows, each with a big listing photo,
a numbered badge, price, title, meta and an italic "why" line. Added 2026-10-08.
```yaml
template: listing-picks
hero_image: /tmp/ct/ct1.jpg          # blurred as the background (use the first listing's photo)
eyebrow: "Coffee table watch · Oct 8"
page_tag: "Day 1"
title: "Today's standouts"
subtitle: "3 of ~330 listings near Pacifica"
listings:
  - { image: /tmp/ct/ct1.jpg, price: "$495", title: "Danish modern teak + tile", meta: "Mission, SF · 46×23×18 in · Craigslist", why: "Real vintage teak" }
poetic: "Reply 1, 2 or 3 for the link."
```

## Image Sourcing Playbook

**Always use SPECIFIC, on-topic photos.** Generic skylines are a fail mode (admin called it out: "the image(s) need to be relevant to the activities").

### Unsplash (free, no API key)

Search via the helper script:
```bash
~/dispatch/skills/visual-cards/scripts/find-images "<query>"
# returns 5-10 photo IDs that you can download
```

Direct photo download by ID:
```bash
curl -sL -A "Mozilla/5.0" "https://unsplash.com/photos/<ID>/download?force=true" -o photo.jpg
```

### Other options
- **Google Images via chrome-control** — for very specific things (a particular restaurant, hotel listing photo)
- **Airbnb / hotel listing photos** — when comparing lodging, grab hero photo from the actual listing
- **nano-banana (Gemini)** — generate something atmospheric when no real photo exists

## Multi-Card Decks

For trip itineraries (multi-day) or ranked-option lists (5 hotels), generate a deck (one card per day/option) and send sequentially. Number them via `page_tag` ("1 / 4", "2 / 4", etc.).

## Render Pipeline

The `render` script:
1. Reads YAML spec
2. Templates the right HTML file (`templates/day-itinerary.html` or `templates/option-rank.html`)
3. Substitutes spec values into the template
4. Calls `chrome --headless --screenshot` at 1080x1920
5. Converts PNG → JPEG via `sips` (smaller, iMessage-friendly)
6. Writes final JPEG to `--out` path

PNG to JPEG conversion is necessary — raw PNGs at 1080x1920 are 1-2MB and iMessage chokes; JPEG q=80 lands at ~250-400KB.

## The "white bar at bottom" debug saga (May 2026, Eric)

**Root cause was trivial and I missed it for 6+ rounds:** `body{background:#1a0f08}` set but no `html{background}`. Chrome headless renders the viewport with default WHITE behind the body. If body height doesn't perfectly match window-size (off by a few px due to font loading, sub-pixel rounding, etc.), the white viewport bleeds through at the bottom.

**Templates MUST have:**
```css
html{background:#0d0a08;margin:0;padding:0}
body{...;margin:0;padding:0;background:#0d0a08}
```

**First diagnostic step when something looks wrong:** check `bottom-left pixel of the file` via Pillow. If white, it's IN your file. If matching content, it's external (iMessage padding etc). Don't chase DPI/aspect/JPEG-vs-PNG theories until you've verified pixel-level whether the artifact is in the file or not.

```python
from PIL import Image
img = Image.open('your-card.png')
print(img.getpixel((0, img.height-1)))  # (255,255,255) = white IN FILE
```

## Render Pipeline Gotchas (lock these in)

- **DO NOT use `sips -Z <max>`** to "downscale" the rendered PNG. On macOS sips silently UPSCALES if you specify a max larger than the image (despite docs claiming it only shrinks). With chrome rendering at 1080×1620 and sips -Z 1920, output becomes 1280×1920 with the bottom 300px filled by Chrome's window-background (looks like a black/white bar). Just convert PNG→JPEG without resizing.
- **Always pass `--force-device-scale-factor=1`** to Chrome headless. Default Mac DPR is 2x, which makes the rendered PNG 2160×3240 instead of 1080×1620. Doesn't cause visual bugs per se (aspect preserved) but doubles file size for no reason.
- **Canvas height MUST match window-size height.** Body is `height:1620px`, Chrome flag is `--window-size=1080,1620`. If those drift apart, you get visible empty band at the bottom.

## Failure Modes (lessons learned, May 2026)

❌ **Pure black background, no hero photo** — sterile, "looks like shit" (admin verdict). Always have a photo behind the text.
❌ **Generic stock skyline as hero** — when activities are specific (Hattie B's, Lower Broadway), the photo should match. Otherwise "image(s) need to be relevant to the activities" (admin).
❌ **Long text wall instead of card** — admin: "this long text format isn't working for me." When the response would be 8+ bullet items, switch to cards.
❌ **Too many cards in one image** — 6 options crammed into one image becomes the same problem as a text wall. Use a deck (multiple images), not one giant compendium.
❌ **Reusing the same photo across cards in one deck** — admin, 2026-08-22: *"the same image is on two visual cards which kinda sucks."* Track which image IDs you've already sent in this conversation and never repeat one. A deck is read as a set; a repeat makes it look auto-generated.
❌ **Stock photos of the CATEGORY instead of the PLACE** — same message: *"the museum one is pictures of cars."* A card about Nissan Crossing and the Fuji Motorsports Museum needs photos *of Nissan Crossing and the Fuji Motorsports Museum*, not generic sports cars. Unsplash is the last resort, not the first.

**Get the real photo instead — this usually takes one command:**
```bash
# most venues expose an OGP image or hero shot right in the homepage HTML
curl -sL -A "Mozilla/5.0" "https://venue.example.jp/" \
  | grep -oE 'https?://[^"'"'"' ]+\.(jpg|jpeg|png)' | sort -u | head
```
Then `curl -o`, check it with Read, and `sips` to crop letterboxing (OGP images are often 1200×630 with bars) or to resize anything over 2000px. Only fall back to `find-images` when the venue publishes nothing usable — and if you do, **actually look at the result before shipping it**: an Unsplash search for "akihabara electronics" returns Shibuya Crossing, which is exactly the generic-skyline failure above.

## Testing

```bash
# render the example to verify the pipeline works
~/dispatch/skills/visual-cards/scripts/render \
  ~/dispatch/skills/visual-cards/templates/example.yaml \
  --out /tmp/example.jpg
open /tmp/example.jpg
```
