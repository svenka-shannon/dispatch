---
name: opentable-watch
description: Check OpenTable availability for hard-to-book restaurants and watch for new tables (cancellations, daily drops), texting or auto-booking when a slot in the wanted window opens. Trigger words - opentable, reservation, rez, table for, book a table, four kings, cancellation watch, notify me when a table opens.
---

# opentable-watch

CLI: `~/.claude/skills/opentable-watch/scripts/opentable-watch`
State: `~/dispatch/state/opentable-watch.json`. Log: `~/dispatch/logs/opentable-watch.log`.
Scheduler: LaunchAgent `com.dispatch.opentable-watch` runs `check` every 20 min (was 5; cut after Akamai blocks).

```bash
opentable-watch avail <rid> --party 4 --date 2026-11-06            # one-off: slots + "future availability"
opentable-watch add <name> --rid <rid> --party 4 --days fri,sat,sun --contact <chat_id> \
   --windows "fri=18:00-20:00,sat=17:00-20:00,sun=17:00-20:00" --drop-time 12:00 --window 21 \
   [--autobook --session imessage/_<id> --instructions "..."]
opentable-watch check [name] | list | rm <name> | reset <name>
```

## How it reads availability
- It uses the **booking widget** page `https://www.opentable.com/booking/restref/availability?rid=<rid>&restref=<rid>&partySize=N&dateTime=YYYY-MM-DDTHH:MM`. It opens the page in a Chrome tab (chrome-control), polls the text until results render (a few seconds after load, via XHR), then closes the tab.
- The widget lists slots within ±3h of `dateTime`, or "All availability for today" when there are none near that time. A query at 19:30 covers a whole dinner service.
- The page also says "At the moment, there's no availability on this day" and lists "Future availability". The parser reads only the text after the party-size `<select>`, because the time `<select>` lists every half hour and would look like slots.
- Things that don't work: plain HTTP/curl (Akamai blocks it); the main `/r/<slug>` page (slots hidden behind JS and the cookie banner); `chrome js` (CSP blocks eval); `chrome fetch` (often returns before the results render).
- The restaurant id (`rid`) is in the `/r/<slug>` page HTML as `"rid":NNNN`.

## Bot limits (important)
On 2026-10-17, about 50 widget loads in 20 minutes got us **"Access Denied" (Akamai)**. After 15 minutes it worked for 1 load, then blocked again on the 2nd, so the block is sticky. The four-kings watch now uses `per_pass` 1, a 20-min interval and a 180-min cooldown. (Notify-me can't be a plan B for the assistant: it's app-only, see Booking.) At 21:52 PT the normal restaurant page (`/r/four-kings-san-francisco`) was **also** Access Denied in Chrome, while the opentable.com homepage still loaded. So the block covers restaurant and booking paths for this browser/IP, and "just use the site normally" doesn't get around it until it expires. The CLI:
- checks only `per_pass` dates per run (default 3, rotating through the window, plus any date 1-2 days out, which is when cancellation-fee churn happens)
- waits `gap_seconds` (20) between loads
- cools down for `cooldown_minutes` (60) after a block, and logs `BLOCKED`.
Don't run ad-hoc loops of `avail` on top of the watcher.

## Before --autobook (pre-flight, do this BEFORE arming a watch)
The autobook wake gives the session about 5 minutes to finish, so everything slow has to be done ahead of time:
1. **Signed in:** the OpenTable account is signed in on Sven's Chrome (open opentable.com and check the header shows the profile, not "Sign in"). If not, sign in (see the gotchas below).
2. **Card saved on the account:** a card must already be saved on the OpenTable profile, so checkout is just ticking the terms box. Typing the card into Stripe's iframes takes longer than the 5-min hold (this is how the first 5:30 slot was lost).
3. **Dry run:** for the same restaurant and party size, click through a currently open slot up to the "You're almost done!" payment page. Confirm the diner name, phone and saved card are prefilled, then back out **without** clicking Complete reservation. Only then run `add ... --autobook`.

## Booking (verified 2026-10-18 up to the final "Complete reservation" click, not submitted)
Account: an OpenTable account signed in on Sven's Chrome, with email svenka.shannon@gmail.com (sign-in is an emailed 6-digit code, no password). The profile is **Eric Maynard / +1 518-258-7876**, with "administrative professional who books for others" checked. Diner details come prefilled as Eric.

**Chrome-control gotchas on opentable.com:**
- `read`, `type` and `insert-text` don't reach the inputs (the auth form is an out-of-process iframe), and `iframe-click` finds nothing.
- What works: `click-by-name` for real buttons, `click-at <x> <y>` with screenshot coordinates (1:1), and **`cliclick t:<text>`** for typing. That needs Chrome to be frontmost (`chrome focus <tab>`, check with `lsappinfo front`).
- Sign-in: load `https://www.opentable.com/authenticate/start?rp=https%3A%2F%2Fwww.opentable.com%2F&srs=1&isFromBookingFlow=false` top-level. Type the email, Continue, then get the code from Gmail (`chrome fetch "https://mail.google.com/mail/u/0/#search/opentable"`; it's in the snippet). Type the code, Continue.
- "Notify me" on the website only shows a QR code. **Availability alerts are app-only**, so the assistant can't set them. The owner can, in their own app.

**Steps (≈1 min; OpenTable holds the slot for 5 min once a seating is selected):**
1. `chrome open "https://www.opentable.com/r/four-kings-san-francisco?covers=4&dateTime=YYYY-MM-DDTHH:MM"` (signed in). Scroll down one screen; the widget shows slot buttons under "Select a time".
2. Click the slot button (`click-at` from a screenshot).
3. "Available seating options" (e.g. Counter): click **Select**. This goes to "You're almost done!" with a 5-min hold.
4. The page shows diner = Eric Maynard. Fill the phone if it's blank. Occasion is optional.
5. **Card required** (Four Kings: $25/guest for cancels inside 24h or no-shows). Choose Credit card and fill it with `~/.claude/skills/payments/scripts/fill-card-iframe <tab>` (Privacy card; see the payments skill).
6. Tick "I agree to the restaurant's terms and conditions" (required). Leave the marketing opt-in unticked.
7. Click **Complete reservation**.
8. **If the hold is lost** ("this table is no longer available", or the 5-min timer runs out), don't stop and don't ask: book the **next-best matching slot right away** (the autobook wake text lists the other matches; otherwise pick the nearest slot inside the watch window from the widget), then tell the owner which slot you got and why.
**First real booking (2026-10-18):**
- Lost the 5:30 slot: filling the card took longer than the 5-min hold, so it showed "this table is no longer available". On OpenTable, `fill-card-iframe` only filled the card number. Stripe's exp/CVC/ZIP are separate iframes, so they had to be typed with `click-at` + `cliclick t:"$(security find-generic-password -s privacy-card-exp -w)"` (exp as MMYY), the same for cvv, and ZIP 94044.
- The card is now **saved on the OpenTable account** (Mastercard ending 0185, preselected), so step 5 is just ticking the terms box. Booking took about 1.5 min on the 7:30 retry.
- Seating can be Standard or Counter. Pick **Standard** for groups.
- Coordinates on the details page shift once the card form expands. Re-screenshot before each click.
- After any screenshot that shows the card form, delete it. Screenshot the confirmation, find the confirmation email in Gmail, and text the owner date/time/party/seating + confirmation #.
Seating note: the 9:30pm 4-tops at Four Kings were **Counter** seating (shoulder to shoulder, per the restaurant's note).

## Restaurants
- **Four Kings** (SF Chinatown, 710 Commercial St). rid `1329550`. OpenTable only, no phone/email bookings.
  New dates open **21 days out at noon PT** (itsfourkings.com). Dinner is Thu-Sun 5:30-11pm. Party cap is 6. There's a 10-seat walk-in counter, which is easiest after 9pm. Early slots go first and post-9pm slots linger. Cancellations cluster around 48h before the date.
  Watch `four-kings` (Eric, party 4, Fri 6-8 / Sat-Sun 5-8) BOOKED 2026-10-18: Sun Nov 8 7:30pm, Standard seating, conf #47201. Watch removed.
  Tables reserved 1h45 for parties up to 4. Running late: message on OpenTable, don't call; they hold 10 min.
