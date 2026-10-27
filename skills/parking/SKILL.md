---
name: parking
description: Find and compare paid parking near a destination (SpotHero), with total price incl. fees, walk time and rating. Use when asked to find parking, where to park, garage near X, or parking for an event. Trigger words - parking, park, spothero, garage, where do I park, lot.
---

# parking

```bash
~/.claude/skills/parking/scripts/spothero "<place or lat,lng>" --date YYYY-MM-DD|today|tomorrow --from HH:MM --to HH:MM [--sort price|walk|rating] [--limit N] [--json]
```
- Geocodes the place (OpenStreetMap), loads SpotHero's search in Chrome (no login needed to search),
  prints options with the TOTAL price SpotHero shows (fees included), walk time, rating, plus the
  search URL to send so the user can book.
- `--to` earlier than `--from` = ends the next day.
- **Book only after the owner picks an option; never above $100.**
- For a reply: send the top 3 by price, note the closest one if it's not among them, then the URL.
- If geocoding picks the wrong place, pass `lat,lng` directly.
- Quote prices exactly as SpotHero shows them. Never label the rate type (e.g. "event rate") unless the page does.

## Booking for the owner
Use case: the owner is DRIVING. He texts/dictates "parking near X" and should get short replies he can
read from CarPlay: first a one-line A/B/C choice (price vs distance), then, after he picks, the booking
confirmation below.
- Account: SpotHero on the assistant's own Google login (`identity assistant.email`, "Continue With
  Google"; Chrome profile 0 is logged in, no separate password). Card = Privacy card (see payments skill).
- Vehicle: the owner's car, saved on the SpotHero account (make/model/plate live there; read them
  from the account's vehicle list, never hardcode them here).
- Wallet: Privacy card saved on the SpotHero account. Card number, billing ZIP and cardholder name come
  from the payments skill / Keychain.
- Spend cap $100, but ALWAYS validate first: send 2-3 options as a price-vs-distance tradeoff in ONE
  short text (e.g. "A) 0.2mi $50  B) 1mi $25 — reply A/B"), and book only the one he picks.
- Booking confirmation = EXACTLY 2 texts, never `--long`:
  1. `booked ✅ $<total> ($<subtotal> + $<fee> fee) · <day> <start>-<end> (<N>h) · <car> · <garage> <apple maps link>`
  2. `<entry instruction>: <parking pass URL>`

  Apple maps link: `https://maps.apple.com/?daddr=<urlencoded address>`. Why 2 texts: one message
  can't fit the 220-char reply gate, and a split that drops price or hours just causes a follow-up question.
- Default when he doesn't give an end time: until +3h from arrival. Options to offer: closest,
  cheapest within ~10 min walk, and one middle option if meaningfully different.
- Card entry: Stripe iframe. Use `~/.claude/skills/payments/scripts/fill-card-iframe <tab>` after
  click-at on the card field (keeps the number out of argv), then `iframe-type <tab> <zip>`.
  Screenshots of the card form show the full number: delete them right after looking.
- React forms here ignore `chrome type`/`key`; set values with the native input value setter +
  dispatch "input" (see how the vehicle form was filled), then click the real button by coordinates.
- Changing the car: SpotHero's Edit Vehicle silently doesn't save, and a 2nd vehicle with the same
  plate is silently rejected. Remove first (… menu > Remove, then JS-click the dialog's
  "Remove Vehicle" button), then Add A Vehicle. Always reload and re-read the list to verify.

## Checkout gotchas
- `--date today` uses Pacific time on this Mac. Late in the evening it may mean the wrong day for the owner. Pass an explicit `--date YYYY-MM-DD` when booking for "tomorrow".
- Take a fresh `chrome screenshot` before EVERY `click-at`. The layout shifts between steps, so old coordinates miss.
- To find text on the page use `chrome text <tab> | grep -i "<text>"`. `chrome find` misses list items.
- On checkout, the saved vehicle isn't attached automatically. Open "Add Vehicle Now", select the car with `click-by-name "<Make Model>"` (a click-at on the radio doesn't stick), click Confirm, then check that the Vehicle box shows the plate.
- "Pay and Reserve" via `click-by-name` can silently do nothing. Use `click-at` on the button, then confirm the URL changed to `/receipt?id=...` before telling anyone it's booked. Never re-click without first checking the receipt page / email, to avoid double booking.
- Get the QR parking pass link from the receipt page HTML (`parking-pass/<rental_id>/?key=...`). You don't need Gmail for it. Entry instructions are on the pass page and in the confirmation email ("Parking Instructions").
- Phone field: use the owner's number (`identity owner.phone`) so SpotHero's SMS updates go to them.

## Arrival-time check (don't just flag it, fix it)
If the live ETA puts the owner at the garage before the reservation starts, **change the start time right away** (the pass page has Change under "Enter After"; then Check Availability) and tell them it's done. Don't only warn: QR gates reject passes before the start time, and the owner ends up taking a ticket at the drive-up rate.
- The time field is a native `<input type=time>`. Type the hour and minutes, then click the AM/PM segment and type `A` or `P`. Verify on a screenshot, because a wrong AM/PM also pushes the end time later.
- Cancelling: Request Cancellation, give a reason, choose the card (not SpotHero credit) for the refund, then Submit. It's a full refund before the start time.
