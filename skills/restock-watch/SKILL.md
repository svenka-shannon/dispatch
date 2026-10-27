---
name: restock-watch
description: Watch for a product to come back in stock or launch (Shopify store search + Gmail restock emails) and wake the owning session to buy it immediately. Trigger words - restock, back in stock, notify me when available, buy when available, drop, preorder alert, sold out watch.
---

# Restock Watch

`~/.claude/skills/restock-watch/scripts/restock-watch` runs every 2 minutes from LaunchAgent
`com.dispatch.restock-watch`. Each run:
- polls Shopify `/search/suggest.json` with plain HTTP (no browser);
- searches Sven's Gmail in Chrome, at most every 5 min.

What a hit does depends on its source:
- **Store hit** (Shopify product available, matches include/exclude/max price): injects
  `RESTOCK HIT ... Act NOW` with the owner's standing instructions.
- **Gmail lead**: each visible Gmail row is parsed into `{from, subject, snippet}` (textContent of `.yW [email]`, `.bog`, `.y2`).
  A new row (not in the baseline) is skipped when:
  - from+subject are both empty. The log says `gmail: empty row skipped`.
  - the subject or snippet matches `NOT_RESTOCK_RE` (order confirmation, your order, order #, shipped, delivered,
    receipt, tracking, invoice, thanks for your order, `Fwd:`/`Re:`). The log says `gmail: suppressed`.
    Set `"gmail_allow_orders": true` on a watch (or `add --gmail-allow-orders`) to let those rows through.

  Otherwise the script runs ONE storefront check (suggest.json, even when `store_poll` is false; there's no recurring poll).
  Then it injects `GMAIL HIT (unverified lead) ... From: | Subject: | Snippet: ... Storefront check: <available matches with
  price, or "no matching available product">`. This tells the session to open the email and confirm it's a restock
  notice before buying. There is no "Act NOW" wording for Gmail leads.

Each hit fires only once. State writes are atomic and locked, so a manual `set-query` can't be clobbered by the LaunchAgent run.

```bash
restock-watch add <name> --store https://shop.com --query "riftbound radiance" --include radiance \
   --exclude event,pre-rift,pack --max-price 160 --session imessage/_15555550100 \
   --gmail-query 'from:(gamersguild) radiance newer_than:3d' --instructions "buy 1, ship to ..., pay with Privacy card"
   # add appends " -order -receipt -shipped -tracking" to --gmail-query unless --raw-query
   # [--gmail-allow-orders] lets order/shipping-looking mail fire
restock-watch set-query <name> '<new gmail query>'   # sets gmail_query, resets gmail_baseline=null + gmail_last=0, keeps 'fired'
restock-watch check [name] | list | rm <name> | reset <name>
# set "store_poll": false in the state JSON to watch Gmail only
```
State: `~/dispatch/state/restock-watch.json`. Log: `~/dispatch/logs/restock-watch.log`.

## Gotchas
- Gmail keeps the hidden inbox rows in the DOM while a search is shown. Count only VISIBLE `tr.zA` rows
  (`offsetParent != null`).
- On the first Gmail run, existing matching threads become the baseline and are not treated as hits.
- **Scope the Gmail query to the store's sender and exclude order/shipping mail.** A bare keyword query
  (`radiance newer_than:3d`) fired on 2026-10-14 for Eric's *forwarded Riot Games "Your Order Was Shipped"* email
  and nearly triggered a duplicate $120 purchase. Use `from:(<store>)` plus `-shipped -"your order" -tracking`.
- **To change a watch's Gmail query, use `restock-watch set-query <name> '<query>'`.** Don't edit `gmail_query` in the JSON.
  `set-query` resets the baseline, so the next check re-baselines. Without that reset, old emails that only match the new query
  fire as fresh hits. On 2026-10-18 an Oct 16 Gamers Guild newsletter fired this way. `set-query` uses the query exactly as
  given: include the `-order -receipt -shipped -tracking` exclusions yourself.
- **On a hit, read the email before buying.** If it's an order/shipping/receipt for the same item, the owner
  already has one. Don't buy. Remove the watch and tell the owner.
- `chrome tabs` prints `<tabId>  <title>` with the URL on the next, indented line.

## Active watches
- `radiance-box-2` (2026-10-14, Eric): a SECOND Radiance booster display from gamersguildaz.com, same address/card/cap.
  Email only (`store_poll: false`). Gmail query excludes shipped/order/tracking/receipt/riot mail.
  As of 2026-10-14 Gamers Guild only lists Radiance pre-rift event tickets, not the display.
  2026-10-18: false hit #2. It fired on a **Walmart order confirmation** for unrelated Oreo wafers (empty title in the hit). Nothing was bought. The query now also requires "gamers guild" or from:gamersguildaz.com, and excludes -order -walmart.
  An **empty title** in a RESTOCK HIT is a red flag. Always open the email.
  2026-10-19 fix: Gmail rows are now parsed into from/subject/snippet. Empty rows are skipped and order/shipping mail is suppressed.
  Gmail wakes are now labelled `GMAIL HIT (unverified lead)` and include a storefront check.

## Past watches
- `radiance-box` (2026-10-10, Eric): one Riftbound Radiance booster display, about $120, from gamersguildaz.com
  (Gamers Guild AZ, Shopify), shipped to the owner's address. The owner signed the assistant's Gmail up for their notifications.
  Pre-authorized purchase, cap about $160 total. Remove the watch after buying.
  EMAIL ONLY (`store_poll: false`). Eric: "Stop, they are supposed to email you when available". Do not poll the store.
  Removed 2026-10-14. Eric got one from Riot Games Merch directly (order #1179324, $119.99, shipped UPS).
  The Gmail "hit" was that shipping email, so nothing was bought.

## Before any restock buy: check for orders the owner already placed
Watch instructions only know about the orders the owner remembered to mention. They may have also preordered the same item themselves, and a blind buy then produces an unwanted extra unit.
Before buying, search Gmail in Chrome for the product/store (`mail.google.com/#search/<product> OR <store>`, including the owner's forwards) and list any orders you find. If an order exists that the watch instructions don't mention, **text the admin and ask before buying.**
