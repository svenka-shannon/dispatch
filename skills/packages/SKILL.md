---
name: packages
description: Track packages and shipments across USPS, FedEx, UPS and OnTrac, and set up daily delivery watchers. Use when asked where a package is, when something will arrive, to check tracking numbers, or to be notified before a delivery. Trigger words - package, tracking, tracking number, shipment, delivery, where is my order, did it arrive, USPS, FedEx, UPS, when will it get here.
---

# Packages

On-demand tracking and delivery watchers.

## Check tracking now

```bash
~/dispatch/bin/track 383700923102                      # carrier auto-detected
~/dispatch/bin/track 383700923102 383700935080         # many at once
~/dispatch/bin/track --carrier usps 9534616388586260386892
~/dispatch/bin/track --json 1Z999AA10123456784         # structured output
~/dispatch/bin/track --raw <num>                       # dump page text (parser debugging)
```

Output: `<tracking>  <CARRIER>  <status>  [delivered <ts> | eta <date>]`

Carrier is inferred from the number format (`1Z…` = UPS, 12/15 digits = FedEx,
20-22 digits starting `9` = USPS). Override with `--carrier` when it guesses wrong.

## Why it drives Chrome

**All three carriers block plain HTTP scraping.** USPS returns 403; FedEx
returns a bot-wall page or a "we can't find that tracking number" error even for
valid numbers. Do not waste time on `curl` or the `webfetch` skill for carrier
sites — `track` drives the chrome-control extension, which is the only path that
works. This means **Chrome must be running** on this machine.

The results table also renders asynchronously, so a single fetch usually returns
just the page nav. `track` retries until every requested number appears in the
page text.

## Parser gotchas (learned the hard way)

- **The USPS page contains a progress-bar legend listing EVERY status label** —
  "Delivered", "Out for Delivery", "Preparing for Delivery", "In Transit" — no
  matter what the real state is. Grepping the page for `delivered` reports a
  delivery that never happened. The real status is only in the prose blurb
  ("Your package is moving within the USPS network… currently in transit"), so
  the parser only considers lines longer than 40 chars.
- **FedEx's summary table is tab-delimited within lines**, not one field per
  line. Flatten on `\t` before parsing. Columns are:
  `TRACKING, SHIP DATE, SHIPPER, RECIPIENT, STATUS, DELIVERED, SCHEDULED, SERVICE`.
  A date carrying a *time* is the delivery stamp; a bare date is the ETA.
- **Only FedEx can be batched.** It has a real multi-package summary table.
  USPS and UPS render one narrative per page, so batching them would smear one
  package's status across all of them. `track` fetches those one at a time.

## Delivery watcher (use this one)

`~/dispatch/bin/package-watch` watches ANY number of packages across all three
carriers. Config is a JSON list, so adding a package is a one-line edit:

```bash
# ~/dispatch/state/package-watch.json
[{"tracking": "383828713189", "label": "seat bracket", "contact": "+1..."}]
```

Runs 4x/day (08:15/12:15/16:15/20:15) via `com.dispatch.package-watch`. It
notifies on three events, once each: the first time an ETA exists, when the ETA
is <=1 day out, and on delivery. State: `state/package-watch.state.json`.

It builds on `track --json` rather than scraping, and **treats a non-zero
send-sms exit as a hard failure — it deliberately does NOT record state when a
send fails**, so a dropped notification retries on the next run instead of being
silently lost.

### Why it replaced usps-watch (2026-09-22) — two silent failures on one package
1. **Delivered-state blindness.** The old watcher grepped the USPS page prose
   for delivery. Once a package IS delivered USPS **drops the "Expected Delivery
   by" block**, so the date parse fell through to `could not parse date` and the
   delivery notice never fired. A delivered package logs that error forever.
2. **Unverified send.** It called send-sms and never checked the exit code. It
   logged `1-day notice sent` for a message with **no corresponding row in
   chat.db** — the owner got nothing, and the log claimed success.
Net effect: the owner was never told about either the imminent delivery or the
actual delivery, while the log looked healthy. Both failure modes are now
covered. `usps-watch` is retired (LaunchAgent unloaded); keep it only as a
reference for the USPS page quirks documented above.

## Daily delivery watcher (LEGACY — usps-watch, retired 2026-09-22)

`~/dispatch/bin/usps-watch` checks one hardcoded USPS number every morning at
9:15 via a LaunchAgent, and texts the owner when the ETA drops to 1 day out,
then again on actual delivery. State lives in `~/dispatch/state/usps-<num>.state`
so it never double-notifies; logs in `~/dispatch/logs/usps-watch.log`.

To watch a different package, copy it and change `TRACK`, `LABEL` and `NUM`, or
generalise it to call `track --json` instead of re-scraping.

If a watcher goes quiet, the usual cause is Chrome not running.

## ⚠️ Every purchase goes on the watcher

**The watcher only knows about packages someone adds to `package-watch.json`.** An empty file makes every run log `config is empty` and send nothing, even while orders are in flight. If the owner asks why the trackers aren't firing, check this file first.

Rule: **the moment you place an order (payments skill, restock-watch buys, Walmart, Amazon...), add it to the watcher in the same turn**, even before tracking exists:

```json
{"order": "Store #12345", "label": "what it is (store)", "ordered": "YYYY-MM-DD", "contact": "+1..."}
```

An entry with no `tracking` shows up in the digest as "no tracking number known, Nd since order", plus a ⚠️ chase flag after `alert_after_days` (default 7). When the shipping email arrives, add `"tracking": "..."` to the entry. Shipping emails land in the assistant's Gmail. Search it in Chrome (`mail.google.com/#search/<order#>`) when the IMAP `email` CLI has no app password.

## UPS parsing
`chrome fetch --min-chars 1200` returns as soon as the nav/footer text passes 1200 chars, before UPS's tracking widget renders, which used to make every UPS lookup `status: null`. UPS therefore uses `fetch_ups` (open a tab, poll until the number is on the page, then close it) plus `parse_ups`. The status is the line above the number; only an exact "Delivered..." counts as delivered, never a heading that merely contains "deliver". The ETA comes from "Estimated delivery" → "Tomorrow, October 26 by 9:00 P.M.", normalized to `M/D/YY`, with `delivery_window` = "by 9:00 P.M.". Never match the progress-legend stage words.

## OnTrac
`track` detects OnTrac numbers (`1LS...` ex-LaserShip, or `C`/`D` + 14 digits) and reads `ontrac.com/tracking/?number=` in a real tab. The fields are status (above "GOING TO"), `eta` from "EXPECTED" (M/D/YY), and location/as_of from "Most Recent Tracking Event". Brick ships OnTrac.
