---
name: events
description: Search upcoming concerts, shows, sports events. Score demand/sellout likelihood, save as structured facts, fire reminder alerts. Trigger words - events, concerts, tickets, shows, ticketmaster, sellout, upcoming events.
---

# Events Skill

Search for upcoming events (concerts, shows, sports, festivals), score their sellout likelihood, and set proactive alerts so you never miss tickets.

## CLI

```bash
~/.claude/skills/events/scripts/events <command> [args]
```

### Search Events

```bash
# Search by artist/keyword
events search "Radiohead" --location "Boston, MA"

# Search by genre
events search --genre "electronic" --location "Boston, MA"

# Search by venue
events search --venue "TD Garden" --days 90

# Limit results
events search "Taylor Swift" --limit 10
```

### Score Demand

The CLI scores each event's sellout likelihood (0-100) based on:
- **Artist popularity** — follower counts, streaming numbers
- **Venue capacity vs. typical draw** — small venue + big artist = high demand
- **Time to event** — events further out have more time to sell out
- **Historical patterns** — does this artist/genre typically sell out?

Score thresholds:
- 🔴 80-100: Very likely to sell out — buy now
- 🟡 50-79: Moderate demand — watch closely
- 🟢 0-49: Low demand — safe to wait

### Set Alerts

```bash
# Alert me 4 weeks before an event's on-sale date
events alert EVENT_ID --advance 4w

# Alert when sellout score crosses a threshold
events alert EVENT_ID --threshold 70

# List active alerts
events alerts

# Remove an alert
events alert-remove ALERT_ID
```

Alerts integrate with the reminders system — they inject a task into the admin's session when triggered.

### Save Events as Facts

Events can be saved to the structured facts DB for long-term tracking:

```bash
events save EVENT_ID
```

This creates a structured fact with type `event` containing artist, venue, date, sellout score, and ticket URL.

## Data Sources

1. **Ticketmaster Discovery API** — primary source for events, venues, pricing
2. **Web scraping (webfetch)** — fallback for venues/artists not on Ticketmaster
3. **Songkick / Bandsintown** — supplementary artist tour data

## Integration with Travel Intelligence

When a trip is detected (via travel-intelligence skill), events automatically searches for notable events at the destination during travel dates. High-demand events are surfaced proactively.

## Nightly Task

The events skill can run as a nightly task to:
1. Re-score saved events for sellout likelihood changes
2. Check for new events matching saved artist/genre preferences
3. Fire alerts for events crossing score thresholds
4. Remove past events from tracking

## User Preferences

Before presenting event results, check the user's structured facts for saved preferences:

```bash
# Check for saved artist/genre preferences
cat ~/dispatch/state/facts.db  # or use the facts CLI if available
```

Look for facts with type `preference` related to music/events. If the user has saved favorite artists or genres, prioritize those in results and proactively flag when those artists announce shows. Don't surface irrelevant events when the user has clear stated preferences.

## Architecture

- CLI: `~/.claude/skills/events/scripts/events` (Python, uv shebang)
- Uses Ticketmaster Discovery API (key stored in `~/.claude/secrets.env` as `TICKETMASTER_API_KEY`)
- Alerts stored as reminders via the reminders system
- Facts saved via structured facts pipeline

## Riftbound / TCG store events (playriftbound.com locator)
- The locator (`https://playriftbound.com/en-US/events?lat=..&lng=..&types=PRE_RIFT`) defaults to **20 mi**. URL params don't change that. Use the "20 mi." dropdown (native select: `chrome os-type <tab> --at X,Y --select --text 50 --enter`).
- **The "N/M players" and "EVENT IS CLOSED" fields are unreliable.** Many stores take sign-ups in person, by phone, on Discord or on their own site, so "0/40" can mean full. Only "FULL"/"WAITLIST" badges and the store's **own** signup page count. Open the event page and follow its registration link (e.g. Game Kastle uses events.gamekastle.com, which shows "N spots available").
- Store Discords are worth searching (e.g. "pre-rift"); stores post when sessions sell out. The assistant's Discord is in GK Redwood City's server.
- When the owner asks about "today", answer only about today, with a wide radius. Don't pad the answer with other days.
- `scripts/riftbound-slot-watch <event_id|url> --contact <chat> --until HH:MM --session <session_name> [--interval 300] [--label "..."]` polls a playriftbound.com event page every N seconds until the event starts. It texts on full→open (and open→full). Counts are only meaningful for stores that register on playriftbound.com.
  - **Always pass `--session`** with the owning chat's session name (e.g. `imessage/_15555550100`; see `claude-assistant status`). After each alert that actually sends (send-sms exit 0 + `SENT`), the watcher runs `claude-assistant inject-prompt <session> "FYI from riftbound-slot-watch: I already texted the owner at HH:MM: '<msg>'. Do NOT re-send it."` Don't re-send that alert.
  - It's a long-running loop, so it still runs detached (`nohup ... > /dev/null 2>&1 &`). A health-check reminder is **required** (e.g. `claude-assistant remind add "check riftbound-slot-watch is alive (pgrep -f riftbound-slot-watch; tail its log)" --contact <chat_id> --in 30m`). A bare `nohup &` with no reminder can die silently.
  - **The watcher texts out-of-band.** Before stopping or summarizing a watch, `tail ~/dispatch/logs/riftbound-slot-watch.log` (look for `ALERT` lines) and run `read-sms --chat <chat> --limit 10`. Never claim no alert went out based on `full=True` poll lines alone.
