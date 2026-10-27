---
name: deals
description: Search, compare, and rank deals across electronics retailers. Track price history and get alerts on price drops. Trigger words - deal, deals, price check, compare prices, best deal, sale, cheapest, best price, price tracker, price drop, deal score.
---

# Deals

Search, compare, and rank electronics deals across 10 retailers. Ranks products by a composite deal score (discount + reviews + specs).

## Quick Usage

```bash
# Search for deals (ranked by deal score)
node ~/.claude/skills/deals/scripts/deals.cjs search "Canon EOS R8"

# Search specific store
node ~/.claude/skills/deals/scripts/deals.cjs search "55 inch TV" --store bestbuy

# JSON output
node ~/.claude/skills/deals/scripts/deals.cjs search "RTX 4070" --json

# Limit results
node ~/.claude/skills/deals/scripts/deals.cjs search "gaming monitor" --top 3

# Price range filter
node ~/.claude/skills/deals/scripts/deals.cjs search "Onkyo receiver" --max-price 1000
node ~/.claude/skills/deals/scripts/deals.cjs search "4K TV" --min-price 500 --max-price 1500

# Add to watchlist with target price
node ~/.claude/skills/deals/scripts/deals.cjs watch "Canon EOS R8" --target 1100

# Check watchlist prices
node ~/.claude/skills/deals/scripts/deals.cjs check

# View price history
node ~/.claude/skills/deals/scripts/deals.cjs history "Canon EOS R8"

# Deal analysis (is this a good deal?)
node ~/.claude/skills/deals/scripts/deals.cjs analyze "Canon EOS R8"

# List/remove watchlist items
node ~/.claude/skills/deals/scripts/deals.cjs list
node ~/.claude/skills/deals/scripts/deals.cjs unwatch 3

# Store health check
node ~/.claude/skills/deals/scripts/deals.cjs health

# Cron (automated daily check + alerts)
node ~/.claude/skills/deals/scripts/deals.cjs cron
```

## Manufacturer / Retailer Price Verification CLIs

For "what does X actually cost today" research, these fetch **live** prices with the
exact URL to cite. Never answer pricing from memory.

```bash
# Logitech G — per-variant prices (PC vs PS vs Xbox) + stock.
# logitechg.com shows only a range ("$999.99 - $1,199.99") until a variant is
# clicked, so this drives Chrome to click each Platform button.
scripts/logi-variant-prices pro-racing-wheel rs50-base g923-trueforce-sim-racing-wheel
# slug = the tail of https://www.logitechg.com/en-us/shop/p/<slug>

# Thrustmaster marketing site — MSRP + "Buy now" eShop link + stock.
# Price lives in the #sticky-cart-price block, NOT in JSON-LD.
scripts/tm-price t598 t818 t300rs t248 t-lcm-pedals

# Thrustmaster official eShop (Magento) — real sale price, "was" price, SKU, stock.
# This is the authoritative US store; a product missing here (404) is a strong
# discontinuation signal even when the marketing page still lists an MSRP.
scripts/tm-eshop-price "https://eshop.thrustmaster.com/en_us/t300-gt-edition.html"

# Amazon — buybox price, list price, availability, seller (via Chrome).
scripts/amazon-price B0DKG7D5RB B07PFB72NL

# Best Buy — search listing (title | price | comp value | URL), and product pages.
scripts/bestbuy-search "thrustmaster t248" "logitech g pro racing wheel"
scripts/bestbuy-price "https://www.bestbuy.com/product/.../sku/6594092"
```

### Gotchas learned the hard way

- **Best Buy blocks Chrome** ("Frame with ID 0 is showing error page") and returns
  empty on `webfetch --tier 2`. Only plain `webfetch` (tier 1) works. On a BB
  product page the real price is JS-rendered, so `bestbuy-price` anchors on the
  **"Compare similar products"** block whose first entry is the current product.
  BB search pages are flaky — re-run, and expect some queries to return nothing.
- **Never grep a page for the first `$NN.NN`.** Both Logitech and Thrustmaster
  product pages are full of accessory/upsell tiles and "customers also viewed"
  prices. Always anchor on the product's own price container.
- **Thrustmaster product sitemaps are the best new-release radar:**
  `curl -s https://www.thrustmaster.com/product-sitemap{,2,3,4}.xml`, filter
  `/en-us/`, sort by `<lastmod>` desc. Surfaces new SKUs before any press coverage.
- **Amazon "Sold by Amazon Resale" / third-party sellers** wildly inflate EOL gear
  (e.g. a $249.99-MSRP pedal set listed at $481 from Amazon Japan). Always report
  the seller and distinguish it from MSRP.
- Logitech `.html` category URLs 404; use `/en-us/shop/c/<cat>` and `/en-us/shop/p/<slug>`.

## Supported Stores

| Store | Key | Method | Price Accuracy |
|-------|-----|--------|---------------|
| Apple | apple | Scrape (JSON) | 100% |
| Best Buy | bestbuy | API (with key) / Scrape | 100% (API) |
| Amazon | amazon | Scrape | Best effort |
| Walmart | walmart | Scrape | Best effort |
| Target | target | Scrape | Best effort |
| Newegg | newegg | Scrape | Best effort |
| B&H Photo | bhphoto | Scrape | Best effort |
| Micro Center | microcenter | Scrape | Best effort |
| Home Depot | homedepot | Scrape | Best effort |
| eBay | ebay | Scrape (Buy It Now) | Best effort |

## Deal Score Algorithm

See v2 algorithm below (this section kept for reference, v2 is the active implementation).

## Environment Variables

- `BESTBUY_API_KEY` - Best Buy Products API key (get at developer.bestbuy.com). Without this, Best Buy falls back to scraping.

## Database

SQLite at `~/.claude/skills/deals/data/deals.db`

Tables: products, watchlist, price_history, alert_log, store_health

## Alerts

- **target_hit**: Price <= watchlist target
- **price_drop**: Price dropped $50+ from last check
- **big_deal**: 30%+ discount from original price
- Deduplication: Same item + price won't re-alert within 24h
- Max 5 alerts per cron run to prevent spam
- Sends via iMessage (sendMessage.sh)

## Reddit Community Sentiment

Reddit integration is built into the CLI via `reddit.cjs`. It fires automatically in parallel with store searches -- no manual WebSearch needed.

- Searches top 3 relevant subreddits based on product category
- Fetches top comments from highest-scoring posts
- Runs keyword-based sentiment analysis (positive/negative/neutral)
- Produces a 0-100 community score
- Results shown in the CLI footer with sentiment summary + top post link

Category-to-subreddit mapping includes: tv, camera, monitor, desktop, laptop, headphones, phone, appliance, gaming, audio, receiver, networking, storage, printer, smartHome.

## Deal Score Algorithm (v2)

Products ranked 0-100 by 6-dimension composite score:

```
deal_score = (reviews * 0.30) + (specs * 0.25) + (brand * 0.15) +
             (priceValue * 0.10) + (discountDepth * 0.10) + (storeTrust * 0.10)
```

- **Customer Confidence** (30%): Star rating weighted by log-scaled review volume. Zero-review default=10, floor of 15 for reviewed products.
- **Spec Quality** (25%): Category-specific scoring templates (TV, camera, audio, generic)
- **Brand Reputation** (15%): Static brand tier scoring
- **Price Value** (10%): Discount % normalized, capped at 65%
- **Discount Depth** (10%): Savings as % of price (30% savings = max score)
- **Store Trust** (10%): Static retailer reliability + API verification bonus

## iMessage Output Rules (CRITICAL)

**EMOJIS**: The CLI output contains emojis (🔍💎🔥⭐💰🗣️📖⚠️📉). When sending via sendMessage.sh, send the CLI output AS-IS. NEVER strip, rewrite, or summarize the output.

**REVIEWS**: Every search pulls review scores from Best Buy (API), Amazon (scraper), Walmart (JSON), Newegg (Realtime API enrichment), Home Depot (GraphQL), and eBay. Always display star ratings and review counts. Footer shows 3 review source links (Rtings, Toms Hardware, etc.) from review-sources.cjs.

**REQUIRED OUTPUT SECTIONS**:
1. Header with search summary + price range
2. Ranked results with emojis, scores, reviews, URLs
3. Footer: lowest price, best value, Reddit signal, review sources, warnings

## Review Sources

Pre-populated in `review-sources.cjs` for 19 categories: monitor, tv, camera, audio, headphones, laptop, desktop, appliance, networking, storage, smartHome, phone, printer, gaming, gpu, peripherals, tablet, projector. Each has professional review sites, community forums, spec databases, and price history trackers.

## Notes

- API stores return verified real-time prices
- Scraped stores use CSS selectors with text fallback
- Circuit breaker disables stores for 6h after 5 hard failures or 8 soft failures
- Price range passthrough to store APIs (Amazon cents, Walmart min/max, Newegg PriceRange, eBay _udlo/_udhi, Best Buy salePrice filter)
- Monitor category uses scoreTVSpecs (resolution, panel, refresh, HDR, size)
- Per-store result limit: 15
- Product name truncation: 70 chars
- Always verify prices before purchasing
