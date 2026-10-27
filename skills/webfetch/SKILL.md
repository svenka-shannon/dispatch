---
name: webfetch
description: Web scraping with anti-bot bypass using scrapling. Fallback to chrome-control for auth. Trigger words - scrape, fetch, web, crawl, extract, blocked site.
---

# Web Scraping Guide

## Decision Tree

```
Need to scrape a website?
         │
         ▼
┌────────────────────────┐
│ 1. Try scrapling first │  ← Fastest (0.3-1s), handles 80% of sites
│    webfetch <url>      │     TLS fingerprint spoofing, no browser
└────────────────────────┘
         │
         ▼ (blocked by Cloudflare?)
┌────────────────────────┐
│ 2. scrapling Stealthy  │  ← Camoufox Firefox (1-4s)
│    webfetch <url>      │     Automatic fallback in CLI
│    (tier 2)            │
└────────────────────────┘
         │
         ▼ (output nearly empty? just "*Source: domain*" with no body?)
┌──────────────────────────────┐
│ 3. Use chrome fetch          │  ← JS-rendered page
│    chrome fetch <url>        │     (Shopify, React/Next.js SPA)
└──────────────────────────────┘
         │
         ▼ (need login / still blocked?)
┌────────────────────────┐
│ 4. Use chrome-control  │  ← Uses real Chrome with your session
│    chrome fetch <url>  │     See /chrome-control skill
└────────────────────────┘
```

## Silent Failure: Empty Output on JS-Rendered Pages

**IMPORTANT:** webfetch can exit with code 0 (success) but return nearly empty markdown — just `"*Source: domain*"` with no body content. This happens with JavaScript-rendered pages (Shopify stores, React/Next.js SPAs, Vue apps) where content is injected by JavaScript after page load. The HTML fetched is valid but has no pre-rendered text.

**How to detect it:** If webfetch output is only 1-2 lines or contains just the source header with no real text, the page requires JS rendering.

**Fix immediately — use `chrome fetch`:**
```bash
~/.claude/skills/chrome-control/scripts/chrome fetch "https://example.com"
```

`chrome fetch` uses your real Chrome browser which fully executes JavaScript, so product data, prices, descriptions, and other dynamic content are all present. It polls until the page text stops growing, so it does not return the empty-but-valid document an SPA hands back instantly. Add `--timeout 40` for slow pages.

**But try tier 2 with render flags FIRST — it is cheaper and often returns more.**

An empty tier-1 body is a JS-rendering failure, not necessarily a bot-detection
failure. The fix is usually *waiting*, and tier 2 can wait perfectly well:

```bash
webfetch <url> --tier 2 --network-idle --wait 5000
```

Measured on `booking.com/searchresults.html?ss=Boston` (2026-08-19):

| Approach | Result |
|---|---|
| tier 1 (HTTP only) | empty body, exit 0 — silent failure |
| **tier 2 + `--network-idle --wait 5000`** | **46 KB, real listings** ✅ |
| `chrome fetch` | 13.6 KB, real listings ✅ |

So tier 2 **does** beat booking.com. Do not conclude "scrapling can't do this"
from a bare tier-1 failure — that tests HTTP fetching, not scrapling.

### Why `network_idle` is non-negotiable for tier 2 (measured, powered)

Controlled comparison, booking.com, n=6 per arm, interleaved (2026-08-19):

| Arm | Served (HTTP 200 + listings) | Output |
|---|---|---|
| **strong** (`network_idle` + wait) | **6 / 6** | 58,374–59,987 bytes |
| weak (`--no-network-idle`) | **0 / 6** | 28 bytes, 0 listings |

Fisher exact two-tailed **p = 0.00216**. Replicated independently on a second
machine (6/6 vs 1/6); **pooled 12/12 vs 1/12, p = 0.0000096**.

The weak arm's pooled rate (1/12 = 0.083) is indistinguishable from the site's
~1/8 background serve rate — so **the weak form isn't a weaker fetch, it's no
fetch**. It just enters the lottery below.

Key insight: the nondeterminism is a property of the *unwaited* path. Give
Camoufox time to follow the challenge redirect and **the served-vs-challenged
outcome becomes deterministic — but the payload still wobbles** (byte counts
varied across all six served runs). Apparent "flakiness" was us sampling a race
we were losing.

⚠️ **Metric warning:** `grep -c` counts matching *LINES*, not occurrences. Both
agents measuring this independently reported a "flat, zero-variance" listing
count that was an artifact of that, on markup where everything sits on few lines.
Use `grep -o ... | wc -l` for occurrences, and compare raw byte counts, before
claiming a payload is stable.

### Measure n>1 — anti-bot responses are NOT deterministic

Measured 2026-08-19, tier 1 against booking.com, 8 consecutive runs:

| Result | Count |
|---|---|
| HTTP 202, 0 chars text | 7 |
| **HTTP 200, 45,541 bytes — served** | **1** |

**A single run has a ~12% chance of telling you the opposite story.** Two agents
independently posted contradictory booking.com numbers all day, and both were
honest n=1 reads of the same behaviour. Before claiming "site X can't be fetched"
or "tier N is broken", run it **at least 5 times** and report the distribution.

This is also the strongest argument for gating on extracted text rather than HTTP
status: on the run that gets served, a text gate simply sees content and stops.
It never has to infer *why* a response was thin — challenged, throttled, or
slow — it only measures what came back.

Precision matters on this claim: a **status-alone** rule would escalate an
already-served page 1 time in 8. A status check *AND-ed with a thin-text test* is
safe from that, because it can't fire when real text arrived — it's diagnostic
text, not a veto. The reason to still leave status out of the gate here is that
the advice it produced ("challenged → change transport") was itself wrong: tier 2
clears the challenge on its own given `network_idle`.
Reserve `chrome fetch` for pages needing your logged-in session or real-Chrome
fingerprint, not merely for JS rendering.

**Known JS-rendered site patterns:**
- Shopify stores (shop.hak5.org, any `myshopify.com` or custom Shopify domain)
- Next.js / React SPA product and listing pages
- Any site where webfetch returns under ~100 chars of actual content after the source header

Do NOT use `chrome open` + `sleep` + `chrome text` + `chrome close` — just use `chrome fetch <url>` directly.

## Quick Start

```bash
# Basic fetch (tries both tiers automatically)
~/.claude/skills/webfetch/scripts/webfetch "https://example.com"

# Force specific tier
~/.claude/skills/webfetch/scripts/webfetch "https://example.com" --tier 1  # scrapling HTTP only
~/.claude/skills/webfetch/scripts/webfetch "https://example.com" --tier 2  # scrapling Stealthy

# Raw HTML output
~/.claude/skills/webfetch/scripts/webfetch "https://example.com" --raw

# JS-rendered pages (Redfin, Zillow, SPAs) — wait for content to load
~/.claude/skills/webfetch/scripts/webfetch "https://redfin.com/..." --tier 2 --network-idle --wait 3000 --raw

# Wait for a specific CSS selector to appear
~/.claude/skills/webfetch/scripts/webfetch "https://zillow.com/..." --tier 2 --network-idle --wait-selector "[data-testid=\"price\"]"
```

### JS-Rendered Page Options (Tier 2 only)

These options only apply to tier 2 (StealthyFetcher/Camoufox) since tier 1 is HTTP-only:

| Flag | What it does |
|------|-------------|
| `--network-idle` | Wait until no network connections for 500ms — essential for SPAs |
| `--wait MS` | Extra millisecond delay after page loads (e.g. `--wait 3000` for 3s) |
| `--wait-selector CSS` | Wait for a specific CSS selector to appear before returning |

**For Redfin/Zillow listing pages**, always use: `--tier 2 --network-idle --wait 3000 --raw`
- `--network-idle` waits for React to finish rendering
- `--wait 3000` adds a buffer for late-loading photo carousels
- `--raw` returns full HTML (markdown conversion strips `<img>` tags)

## The Two Tiers

### Tier 1: Scrapling HTTP (Default)
**Best for:** Most public websites, news, blogs, product pages

- Uses `curl_cffi` with TLS fingerprint spoofing
- Impersonates real browser TLS handshakes
- No actual browser needed - pure HTTP
- **Speed:** 0.3-1 second
- **Success rate:** ~80% of sites

```python
from scrapling import Fetcher
fetcher = Fetcher()
page = fetcher.get("https://reddit.com/r/programming")
print(page.html_content)
```

### Tier 2: Scrapling Stealthy (Camoufox)
**Best for:** Cloudflare Turnstile, DataDome, aggressive anti-bot

- Uses Camoufox (Firefox fork with anti-detection)
- Full browser automation with stealth patches
- Bypasses most JavaScript challenges
- **Speed:** 1-4 seconds
- **Success rate:** ~95% of sites

```python
from scrapling import StealthyFetcher
fetcher = StealthyFetcher()
page = fetcher.fetch("https://ticketmaster.com", headless=True)
print(page.html_content)
```

### Fallback: Chrome Extension
**Best for:** JS-rendered pages (Shopify/SPA), sites requiring login, when scrapling fails

If tiers 1-2 fail or return empty output, use the Chrome extension which controls your actual browser:

```bash
~/.claude/skills/chrome-control/scripts/chrome fetch "https://example.com"
```

See `/chrome-control` skill for full docs.

## Screenshots

Use chrome-control for screenshots:
```bash
~/.claude/skills/chrome-control/scripts/chrome screenshot
```

## When to Use Each Approach

| Scenario | Recommended Approach |
|----------|---------------------|
| Public website, no login | Tier 1 (scrapling HTTP) |
| Cloudflare protection | Tier 2 (scrapling Stealthy) |
| webfetch returns empty/minimal output | chrome fetch (JS-rendered page) |
| Shopify / React SPA / Next.js store | chrome fetch directly (skip webfetch) |
| Need logged-in content | Chrome extension |
| Need to click/interact | Chrome extension |
| Need screenshot | Chrome extension |

## Common Sites and What Works

| Site | Tier 1 | Tier 2 | Chrome Ext | Notes |
|------|--------|--------|------------|-------|
| Reddit | ✅ | ✅ | ✅ | All work |
| Amazon | ✅ | ✅ | ✅ | All work |
| Zillow | ✅ | ✅ | ✅ | Rate limits on rapid requests |
| Ticketmaster | ✅ | ✅ | ✅ | All work |
| LinkedIn | ✅ | ✅ | ✅ | Login required for full profiles |
| Twitter/X | ❌ | ❌ | ✅ | Requires login |
| Instagram | ❌ | ❌ | ✅ | Requires login |
| Gmail | ❌ | ❌ | ✅ | Requires login |
| Shopify stores (e.g. shop.hak5.org) | ❌ empty | ❌ empty | ✅ | JS-rendered, use chrome fetch |

## Performance Comparison

| Tier | Speed | Memory | Success Rate |
|------|-------|--------|--------------|
| 1 (scrapling HTTP) | 0.3-1s | ~50MB | 80% |
| 2 (scrapling Stealthy) | 1-4s | ~400MB | 95% |
| Chrome extension | 3-8s | (uses Chrome) | 100%* |

\* If you can see it in Chrome

## Token Efficiency

**ALWAYS use webfetch over raw `curl` or `wget`.** Scrapling returns extracted markdown content, not raw HTML. This dramatically reduces token usage:

- **Raw HTML** (curl/wget): ~700K tokens for a single page
- **Extracted markdown** (webfetch): ~2.6K tokens for the same page

That's a **~270x reduction** in token consumption. Using raw HTML wastes quota and context window on boilerplate markup, scripts, and styling that contain zero useful information.

## Dependencies

Installed automatically via uv script header:
- `scrapling[all]>=0.4` - HTTP fetcher + Camoufox
- `markdownify>=0.11` - HTML to markdown
- `beautifulsoup4>=4.12` - HTML parsing

First run will install camoufox browser (~300MB).

---

## Japanese sites: encoding is per-site, do NOT blanket-apply iconv

Verified 2026-08-27 by fetching both sites directly.

| Site | Encoding | Fetch |
|---|---|---|
| **jalan.net** | **Windows-31J / CP932** | `curl -sL -A "<browser UA>" URL \| iconv -f CP932 -t UTF-8` |
| **tabelog.com** | **UTF-8** | plain `curl -sL -A "<browser UA>" URL` — **no iconv** |

`webfetch` (scrapling) dies on jalan with `unknown encoding: 'Windows-31J'`, which is why the iconv workaround exists. **That workaround is specific to jalan.** Applying `iconv -f CP932` to Tabelog corrupts every Japanese character — Tabelog's response header is literally `text/html; charset=utf-8`.

I propagated the wrong advice into several research briefs before checking. Confirm with:

```bash
curl -sL -A "<browser UA>" "$URL" -o /tmp/x.html && file -I /tmp/x.html
```

If it says `charset=utf-8`, skip iconv. Rakuten and the official hotel/shop sites are UTF-8 too.

Also worth knowing for Japanese research:
- **Tabelog closure markers:** look for `閉店` and `掲載保留` (delisted — treat as gone). A page loading fine does not mean the shop is open.
- Rakuten per-room specs live at `hotel.travel.rakuten.co.jp/hotelinfo/plan/<id>?f_flg=ROOM`; the structured checkbox fields are unreliable, the **free-text 客室説明 is the trustworthy field**.
- じゃらん hotel-level amenity grids (`バストイレ別 ○/×`) are **property-wide**, not per-room — do not read them as applying to the cheapest room.
