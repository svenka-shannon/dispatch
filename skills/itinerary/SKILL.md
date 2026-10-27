---
name: itinerary
description: Plan a trip day by day in a YAML file, catch scheduling mistakes (overlaps, impossible transit, closed days, booking deadlines), and export a real .ics calendar file that can be texted to the user and tapped to import into Apple/Google Calendar. Also makes a one-off .ics invite for a single event (dinner, show). Trigger words - itinerary, calendar invite, send invite, trip plan, day by day, schedule, agenda, calendar, ics, add to calendar, travel plan, what should I do on.
---

# Itinerary

Author a trip as a YAML plan, sanity-check it, and hand the user a calendar file.

## Why .ics and not the calendar API

Every direct calendar-write path on this machine is dead (verified 2026-08-22):

| Path | Status |
|---|---|
| `gws` CLI (google-suite skill) | not installed, no Google credentials |
| claude.ai Google Calendar MCP connector | needs interactive OAuth — unavailable in daemon sessions |
| Calendar.app via AppleScript | `osascript` times out with **-1712**; Automation permission ungranted |

**Do not retry those.** The path that works is generating a standard RFC 5545 `.ics`
file and sending it over iMessage. Tapping the attachment imports it into Apple
Calendar or Google Calendar, on phone or Mac, with no OAuth and no permissions.

```bash
~/.claude/skills/itinerary/scripts/itinerary ics ~/trips/tokyo.yaml --out /tmp/tokyo.ics
~/.claude/skills/sms-assistant/scripts/reply "Tokyo itinerary — tap to add to your calendar" --file /tmp/tokyo.ics
```

## The CLI

`~/.claude/skills/itinerary/scripts/itinerary` — uv script, single dependency
(PyYAML), stdlib `zoneinfo` for timezones and hand-written RFC 5545 output.

| Subcommand | Purpose | Exit codes |
|---|---|---|
| `validate <plan>` | does the file parse and make structural sense | 0 ok, 1 structural errors, 2 unloadable |
| `show <plan>` | readable day-by-day agenda on stdout | 0 ok |
| `check <plan>` | find the mistakes that ruin trips | 0 clean, 1 findings, 2 unloadable |
| `ics <plan>` | emit an RFC 5545 calendar file | 0 ok, 2 refused (structural errors) |
| `event --title ... --date ...` | one-off .ics invite for a single event, no plan file | 0 ok, 2 bad arguments |
| `init` | print a starter plan template | 0 ok |

### Typical flow

```bash
IT=~/.claude/skills/itinerary/scripts/itinerary

$IT init --out ~/trips/tokyo.yaml      # scaffold
$IT validate ~/trips/tokyo.yaml        # does it parse
$IT check ~/trips/tokyo.yaml           # does it actually work as a schedule
$IT show ~/trips/tokyo.yaml            # read it back to the user
$IT ics ~/trips/tokyo.yaml --out /tmp/tokyo.ics
~/.claude/skills/sms-assistant/scripts/reply "Here's the plan" --file /tmp/tokyo.ics
```

### validate

```bash
~/.claude/skills/itinerary/scripts/itinerary validate ~/.claude/skills/itinerary/examples/tokyo-2026.yaml
```
```
tokyo-2026.yaml: Tokyo 2026 — 10 day(s), 16 item(s), tz=Asia/Tokyo
0 error(s), 0 warning(s), 0 note(s)
OK — plan parses cleanly.
```

### show

```bash
~/.claude/skills/itinerary/scripts/itinerary show ~/.claude/skills/itinerary/examples/tokyo-2026.yaml
```
```
Tokyo 2026  (2026-08-27 → 2026-09-09, Asia/Tokyo)
=================================================

Thu 2026-08-27 — Arrival
------------------------
       13:55-15:00  Land HND + immigration & baggage
                    @ Haneda Airport (HND), Terminal 3, Ota-ku, Tokyo
       16:45-17:45  Nissan Crossing
                    @ Ginza Place, 5-8-1 Ginza, Chuo-ku, Tokyo
                    [~15 min transit to get here; free]
...
```

### check

The valuable one. Detects:

| Code | Level | What it means |
|---|---|---|
| `overlap` | error | two items on the same day overlap in time |
| `impossible-transit` | error | item B starts before A ends + B's `transit_min` |
| `closed-day` | error | scheduled on a weekday the place is closed (`closed_days`) |
| `closed-date` | error | scheduled on a specific closure date (`closed_dates`) |
| `deadline-passed` | error | `booking_deadline` already gone |
| `zero-length` / `duplicate-day` | error | malformed schedule |
| `deadline-soon` | warn | `booking_deadline` within 48h — book now |
| `deadline-after-event` | warn | deadline is later than the event itself |
| `missing-end` / `unknown-field` | warn | sloppy authoring, still exports |
| `free-day` / `big-gap` | info | empty day, or a gap ≥ `--gap-min` (default 240) |
| `leg-gap` / `unscheduled-date` | info | dates inside the span with no day entry |
| `out-of-order` / `crosses-midnight` | info | FYI about how the file was read |

Exit is **1 if any error or warning** is present, 0 if only info notes. Use
`--strict` to fail on info too — good for gating automation.

```bash
~/.claude/skills/itinerary/scripts/itinerary check ~/.claude/skills/itinerary/examples/conflicts-demo.yaml
```
```
2026-08-24
  ✗ [duplicate-day] date 2026-08-24 appears twice (days[0] and days[1])
  ✗ [impossible-transit] Far Away C: needs 45 min transit after 'Lunch B' (ends 13:00) but starts 13:05 — 40 min short
  ✗ [overlap] Lunch B: overlaps 'Museum A' (10:00-12:00) by 30 min
  · [big-gap] 8h00m open between 'Far Away C' and 'Late night bar'
2026-08-25
  ✗ [closed-day] Closed-on-Tuesday Museum: scheduled on a Tuesday but closed tuesdays
  ✗ [deadline-passed] Deadline Already Passed Tour: booking deadline 2026-08-20 passed 2d ago
  ! [deadline-soon] Deadline In 2 Days Tour: booking deadline 2026-08-24 is in 2d — book now
...

7 error(s), 3 warning(s), 5 note(s)
FAILED — fix the items above.
```

Other flags:
```bash
$IT check plan.yaml --json                  # machine-readable {ok, counts, findings[]}
$IT check plan.yaml --today 2026-09-01      # pretend it's a different day (deadline testing)
$IT check plan.yaml --gap-min 120           # flag gaps of 2h+ instead of 4h+
$IT check plan.yaml --strict                # info notes fail too
```

### ics

```bash
~/.claude/skills/itinerary/scripts/itinerary ics ~/.claude/skills/itinerary/examples/tokyo-2026.yaml --out /tmp/tokyo.ics
# wrote /tmp/tokyo.ics — 16 event(s), 6013 bytes

$IT ics plan.yaml                              # stdout instead of a file
$IT ics plan.yaml --reminder-min 30            # add a 30-min DISPLAY alarm per timed event
$IT ics plan.yaml --dtstamp 2026-08-22T12:00:00Z --out a.ics   # reproducible output
```

What the writer guarantees:

- **Timezone-correct.** Timed events are converted from the plan's `timezone` to
  UTC and written with a `Z` suffix (`DTSTART:20260827T045500Z` for 13:55
  Asia/Tokyo). No VTIMEZONE block is needed and there is nothing for the client
  to get wrong — a reader in `America/Los_Angeles` sees 21:55 on Aug 26, correctly.
- **All-day items** use `DTSTART;VALUE=DATE:` with an exclusive `DTEND`.
- **CRLF** line endings, folded at **75 octets** without splitting a UTF-8
  character (Japanese text and em dashes survive).
- **TEXT escaping** of `\`, `;`, `,` and newlines in SUMMARY/LOCATION/DESCRIPTION.
  `URL` is a URI value and is deliberately *not* escaped.
- **Stable `UID`** — SHA-1 of title + timezone + date + start + item title, so
  re-exporting and re-importing updates events instead of duplicating them.
- `DTSTAMP`, `PRODID`, `VERSION:2.0`, `CALSCALE`, `X-WR-CALNAME`, `X-WR-TIMEZONE`.
- `url`, `notes`, `cost`, transit time and booking deadline go into `DESCRIPTION`;
  `location` into `LOCATION`.

`ics` refuses (exit 2) if the plan has **structural** errors, but it will still
export a plan with scheduling conflicts — run `check` first and tell the user.

### event (single-event invite)

For one dinner, show or appointment you don't need a plan file. **To text someone a
calendar invite:** run `itinerary event ...`, then `send-sms <number> --file <ics>`
(or `reply "..." --file <ics>` from their transcript dir).

```bash
$IT event --title "Dinner at Four Kings" --date 2026-11-08 --start 19:30 --duration 1h45 \
  --tz America/Los_Angeles --location "Four Kings, 710 Commercial St, San Francisco, CA 94108" \
  --notes "Party of 4, OpenTable conf #47201" --alarm 2h --out /tmp/four-kings.ics
~/.claude/skills/sms-assistant/scripts/send-sms "$(identity owner.phone)" --file /tmp/four-kings.ics
```

- `--end 21:15` or `--duration 1h45` (also `2h`, `90m`). Default is 1h. An `--end` earlier than `--start` means the event ends the next day.
- Leave out `--start` for an all-day event.
- `--tz` is the **venue's** IANA zone (default America/Los_Angeles). Unlike `ics`, `event` writes local
  wall-clock time (`DTSTART;TZID=America/Los_Angeles:20261108T193000`) plus a real `VTIMEZONE` built
  from zoneinfo (the actual DST transitions for the year before through the year after), so the invite
  reads "7:30 PM PST" in the recipient's calendar.
- Optional: `--location`, `--notes`, `--url`, `--alarm 2h|30m` (DISPLAY reminder), `--dtstamp` (reproducible output), `--out` (default stdout).
- Verified 2026-10-19: the Four Kings example parses in `icalendar` and `vobject` as 19:30-21:15 at -08:00 (03:30Z), with a 2h alarm.
  To re-check a file: `uv run --with icalendar python -c "import icalendar,sys;print(icalendar.Calendar.from_ical(open(sys.argv[1]).read()).walk('VEVENT')[0]['DTSTART'].dt)" /tmp/four-kings.ics`

## Plan file format

```yaml
title: Tokyo 2026            # required-ish (falls back to the filename)
timezone: Asia/Tokyo         # REQUIRED, IANA name — zoneinfo must know it
default_transit_min: 0       # optional fallback for items without transit_min

days:
  - date: 2026-08-27         # required
    label: Arrival           # optional, shown in `show`
    notes: "optional day-level note"
    items:
      - start: "16:00"       # ALWAYS QUOTE TIMES (see gotcha below)
        end: "17:30"         # or duration_min: 90 (or omit → assumes 60 + warns)
        title: Nissan Crossing            # required
        location: "Ginza Place, 5-8-1 Ginza, Chuo-ku"
        url: "https://www.nissan.co.jp/crossing/"
        notes: "free, no reservation"
        cost: "¥1,600"
        transit_min: 10                   # minutes needed to GET here from the previous item
        booking_deadline: 2026-08-25      # optional
        closed_days: [monday]             # optional, weekday names
        closed_dates: [2026-09-07]        # optional, specific dates
      - title: Hotel check-in
        all_day: true                     # all-day items need no start/end
  - date: 2026-08-28
    items: []                             # empty day → reported as a free day
```

Field notes:

- `transit_min` is **travel time to reach this item from the previous one**, not
  the time to leave it. That's the direction `check` uses.
- `end` earlier than `start` is read as crossing midnight (22:30 → 01:15 is 2h45m).
- Days are sorted by date automatically; items may be listed in any order (you
  get an `out-of-order` note).
- Unknown fields are warned about and ignored, so extra annotation is safe-ish.

### Gotcha: always quote times

Unquoted `16:00` is a YAML 1.1 sexagesimal **integer** (960), not a string.
The CLI recovers the intended time rather than silently scheduling nonsense, but
quote them anyway: `start: "16:00"`.

## Examples

- `~/.claude/skills/itinerary/examples/tokyo-2026.yaml` — real Tokyo trip
  (Aug 27 – Sep 2 2026, then Sep 7 – Sep 9), APA Hotel Akihabara Suehirocho
  Ekimae, Nissan Crossing, Fuji Motorsports Museum on a Saturday because the
  Gotemba shuttle is weekends-only, Nissan Global HQ Gallery in Yokohama carrying
  `closed_dates: [2026-09-07]` so it lands on Tuesday Sep 8 instead. Passes
  `check` with only info notes.
- `~/.claude/skills/itinerary/examples/conflicts-demo.yaml` — deliberately broken.
  Every detector fires. Useful as a regression test: `check` must exit 1 on it.

## Regression test

```bash
~/.claude/skills/itinerary/tests/verify-ics
```

Generates an .ics from the Tokyo example and round-trips it through two
independent parsers (`icalendar` and `vobject`, test-only deps pulled by uv),
asserting CRLF, the 75-octet fold, UTF-8-safe folding, escaping, UID uniqueness,
all-day DATE values, and that 13:55 Asia/Tokyo reads as 21:55 the previous day in
America/Los_Angeles. Run it after touching the .ics writer.

## Authoring tips for the assistant

1. Research the real opening hours and closing days **before** writing the plan,
   and put them in `closed_days` / `closed_dates`. Japanese museums closing on
   Mondays is a real trap.
2. Estimate `transit_min` honestly (Google Maps / the `places` skill). The
   impossible-transit check is only as good as those numbers.
3. Run `check` before showing anything to the user. Fix errors, mention warnings.
4. Send the `.ics` with `reply --file`, and optionally a `visual-cards` image for
   the pretty day-by-day version. Text + attachment beats a wall of text.
5. Keep the plan file somewhere durable (`~/trips/<name>.yaml`) so it can be
   edited and re-exported; UIDs are stable, so re-importing updates events.

## Adopting a trip that already exists (`import`)

**Added 2026-08-30 after this skill went unused for an entire 13-day trip.**
The Tokyo calendar was hand-written as a raw `.ics` on day one, this skill had no
way in, so two weeks of edits were made with ad-hoc Python. It drifted exactly as
you would expect: bare LF line endings instead of CRLF, DESCRIPTION lines over the
75-octet limit once Japanese text was added, and an event left on a date the
traveller was not in the country. None of that would have survived `check`.

```bash
IT=~/.claude/skills/itinerary/scripts/itinerary
$IT import /tmp/trip.ics --timezone Asia/Tokyo --title "Tokyo 2026" --out ~/trips/tokyo.yaml
$IT check ~/trips/tokyo.yaml        # now you get the conflict detector for free
$IT ics  ~/trips/tokyo.yaml --out /tmp/trip.ics   # regenerate, correctly
```

`import` unfolds RFC 5545 continuation lines, unescapes TEXT, honours `VALUE=DATE`,
`TZID=` and `Z`, and converts everything into the target timezone. Run it the
moment you notice you are hand-editing iCalendar. **If you are editing a `.ics`
with a text editor or a Python one-liner, you are doing it wrong** — import it.

**One timezone per plan.** A Tokyo→Singapore trip imported as `Asia/Tokyo` renders
the Singapore days in JST (an hour off local). For multi-leg trips either keep one
plan per leg, or accept the offset and put local times in the item title.

## The options ledger (`options`)

A trip is mostly *rejected* ideas. Without somewhere to put them you re-research
the same venue twice and re-suggest things already ruled out — on the Tokyo trip a
permanently-closed bar was suggested after being established as closed, and a shop
was recommended twice after being rejected on price.

Keep a sidecar `options.yaml` next to the plan:

```yaml
options:
  - title: BJW deathmatch, TOKYO SQUARE Itabashi
    status: candidate          # candidate | planned | booked | dead
    date: 2026-09-08
    location: 板橋区前野町6-8-10 — ときわ台 + 12 min walk
    hours: "doors 18:30, bell 19:00, 当日券 from 17:30"
    cost: "¥6,500 + ¥600 drink, cash"
    verified: 2026-08-30       # the date you last confirmed hours/closure
    source: https://bjw.co.jp/schedule/s2026-09-08/
  - title: The Lock Up, Shibuya
    status: dead
    kill_reason: all branches closed 2022 — still all over travel blogs
```

```bash
$IT options options.yaml                 # grouped by status
$IT options options.yaml --status dead   # what NOT to suggest again
$IT options options.yaml --check         # stale / unverified / duplicate / no-source
```

`--check` flags: anything `planned`/`booked` with no `verified:` date, anything
verified more than 14 days ago, duplicate titles, `dead` entries with no
`kill_reason` (they *will* get re-suggested), and live options with no `source:`.

## Verification playbook

Hard-won on the Tokyo trip. Every one of these caused a real failure.

**Sites that soft-fail are the main hazard.** `bluenote.co.jp/tokyo/en/guide/`,
`/first-time/` and `/information/` all return **HTTP 200 with homepage content**
instead of 404. So does akaboo's per-genre page set — four different titles
returned byte-identical HTML. A careless read turns boilerplate into a confident
answer.

> **Test:** fetch two URLs that should differ. If the content is identical, you
> are reading a fallback page, not data. Never quote a page you have not
> differenced.

**An empty API is not an empty world.** Riot's Riftbound locator returned zero
events within 200 miles of Tokyo. The correct reading is *"no shop has registered
an event"*, not *"there is no scene"* — the traveller had already attended a
community event organised on X. Same trap in Singapore, where the shop scene runs
on Telegram. Say what the source actually covers.

**Verify the venue still exists.** Permanently closed and still all over the
listicles: Robot Restaurant, Kawaii Monster Cafe, Gundam Cafe, Square Enix Cafe
Akihabara, The Lock Up (every branch), Alcatraz E.R., MEGA WEB, Honda Welcome
Plaza, Mode Off Akihabara. Others *moved* — Kagaya left Shimbashi, Body & Soul
left Minami-Aoyama, Ninja Akasaka became NINJA TOKYO in Otemachi. On 食べログ check
for `閉店` and `掲載保留`.

**Encoding is per-site.** tabelog is UTF-8 (plain curl). akaboo, b2-online and
jalan are Shift_JIS/CP932 — pipe through `iconv -f CP932 -t UTF-8`, or use
`chrome-control`'s `fetch`, which decodes correctly. Applying iconv to a UTF-8
site corrupts it.

**Venue names are not locations.** Tokyo Big Sight is four buildings served by two
stations hosting simultaneous unrelated events; "Comic City VEGA · Tokyo Big Sight"
put the traveller in an international toy show. Always record **hall + station +
exit**. Same for Makuhari Messe, Pacifico Yokohama, any department store.

**Resolve entry mechanics before the day**, not at the gate: advance-only or
day-of, price, cash-only, and the phrase to ask with (`一般入場券をください`).
Doujin events sell a pamphlet that *is* the admission.

**Quote door time and start time separately.** Blue Note's doors were 18:30 for a
19:30 set; conflating them caused a rushed dinner for nothing. And check whether
seating is by arrival — that venue assigns seats the day before, so arriving early
bought literally nothing.

**Travel time is door-to-door.** Station-to-station plus "a few minutes" is how you
get "~25 min" for a 50-minute journey. Include egress from a crowded venue.

**Give kana with kanji.** The traveller can read kanji but not always guess the
reading, and cannot search or say a name without it: `牛かつ あおな` → `ぎゅうかつ あおな`.

## Trip-shape checks worth running by hand

`check` catches overlaps, transit, closures and deadlines. These it cannot:

- **Date-line arithmetic.** A flight landing "same day" may cross the line. Verify
  the arrival *date* against the booking, not the departure date plus flight time.
  This trip had a whole day of events built on a one-day-early arrival.
- **Hotel gaps.** Check every night of the span is covered by a booking.
- **Free-cancellation deadlines** belong in the calendar as their own events.
- **Constraints that are not events** — a midnight work call caps the evening
  before; an early flight caps the night before.

## Trip-shape checks (`kind:` and `nights:`)

Added 2026-08-30. `check` used to look at one day at a time, so it could not see
the two mistakes that actually cost this trip real money and a wasted morning.
Mark the structural items and it catches both:

```yaml
      - start: "14:07"
        end: "15:15"
        title: Land HND
        kind: arrival          # arrival | departure | lodging
      - start: "16:00"
        end: "16:30"
        title: Check in, APA Akihabara
        kind: lodging
        nights: 5              # covers this night + 4 more
```

| Code | Level | Catches |
|---|---|---|
| `before-arrival` | error | an item dated before you land — **the date-line bug**. A flight leaving the US on the 27th arrives on the 28th; a whole evening was once planned on a day the traveller was still over the Pacific |
| `after-departure` | error | an item dated after you fly out |
| `uncovered-night` | error | a night between arrival and departure with no `kind: lodging` covering it — this trip went to the final week with two unbooked nights and nobody noticed structurally |
| `no-lodging` | info | nothing marked as lodging, so coverage was not checked |
| `lodging-no-nights` | warn | `kind: lodging` with no `nights:` |

`uncovered-night` only runs when there is at least one arrival **and** one
departure, so a partial plan does not produce noise.

## "Permanently closed" usually means "moved" — get the address from the venue itself

Five relocations hit one 13-day Tokyo trip, every one of them still showing the
old address in Google, blogs and directories:

| Venue | Was | Now |
|---|---|---|
| 16SHOTS game bar | 新宿2-3-8 | 新宿5-4-1 (moved Jul 2020) |
| かがや Kagaya | 新橋 | 旗の台 |
| 凧の博物館 Kite Museum | old Nihonbashi site | 日本橋室町1-8-3 |
| Body & Soul | 南青山 | 渋谷 (2021) |
| Ninja Akasaka | 赤坂 | NINJA TOKYO, 大手町 |

Google marking a venue "permanently closed" is evidence about **a listing**, not
about the business. It is equally likely to mean: closed, moved, or open-but-not-
open-right-now (a bar at 17:00 that opens at 19:30 shows "Closed").

**Procedure, in order:**
1. Fetch the venue's **own site** and read the address off it. This settles it.
2. If the site is dead, search `<name> 移転` and `<name> 閉店` — Japanese venues
   announce relocations loudly.
3. Only then treat it as closed.

**Never take an address from a blog post, a listicle or a directory.** Every one
of the five above is still mis-listed somewhere prominent. A stale address also
poisons everything downstream — a wrong 丁目 changes the neighbourhood, its
character, and the travel time.
