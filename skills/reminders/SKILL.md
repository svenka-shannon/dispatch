---
name: reminders
description: Create and manage reminders using native JSON system. Use when asked to set a reminder, check reminders, or schedule something for later.
---

# Reminders Skill

Native reminder system with local timezone support, crash safety, and automatic retry.

> **Use `claude-assistant remind` — it is the live path.** It stores to
> `~/dispatch/state/reminders.json` via `assistant/reminders.py`. No AppleScript,
> no Apple Reminders.app, no Automation permission required. This works today.
>
> **`scripts/add_reminder.py` and `scripts/poll_due.py` are DEAD CODE.** They
> drive Apple Reminders.app via `tell application "Reminders"`, nothing in the
> daemon calls them, and they were last touched 2026-03-20. Confirmed
> 2026-08-19: `assistant/cli.py` → `add_reminder_cli()` → JSON, never these.
>
> They also *hang ~25s+ and return nothing* on this machine, because
> `python3.14` has no Automation grant for `com.apple.reminders`. Do NOT request
> that permission to "fix" them — the capability is unused. Verify a path is
> live before escalating a permission request for it.

## Two Modes

Reminders operate in two modes:

1. **Legacy (contact) mode**: Injects a task into a contact's chat session. Requires `--contact`.
2. **Generalized (event) mode**: Produces any bus event on schedule. Uses `--event` with a JSON event template. No `--contact` needed.

**Legacy reminders without a contact are SILENTLY SKIPPED by the daemon.** Always use `--contact` for legacy mode.

---

**IMPORTANT**: Reminders are TASKS FOR CLAUDE TO EXECUTE, not text notifications to the user. When a reminder fires, Claude should DO the task described in the reminder title, then report results to the user.

Example: "Check the weather and text forecast" → Claude checks weather API, then texts the user the forecast.

## Add a Reminder (Legacy Mode)

```bash
# In 2 hours
claude-assistant remind add "Check the chess game" --contact "John Smith" --in 2h

# At specific time (local timezone)
claude-assistant remind add "Call Tokyo office" --contact "John Smith" --at "3pm"
claude-assistant remind add "Morning standup" --contact "John Smith" --at "9:30am"

# With cron (recurring)
claude-assistant remind add "Daily standup" --contact "John Smith" --cron "0 9 * * *"
claude-assistant remind add "Weekly review" --contact "John Smith" --cron "0 10 * * 1"

# With timezone override
claude-assistant remind add "Call Tokyo" --contact "John Smith" --at "9am" --tz "Asia/Tokyo"

# With target (fg=foreground, spawn=new agent)
claude-assistant remind add "Long analysis task" --contact "John Smith" --in 1h --target spawn
```

### Capture the ID at creation time

`remind add` prints a machine-capturable `ID: <id>` line. **Capture it** if you might need to cancel later, and mention it in your notes/reply:

```bash
ID=$(claude-assistant remind add "re-check the blocked step" --contact "+1..." --in 2m | awk '/^ID:/{print $2}')
echo "$ID"   # e.g. abc12345
```

### Target Types

- **fg** (default): Inject into the contact's foreground session. Session texts the user when starting/finishing.
- **spawn**: Create a fresh agent SDK session for this task. Good for isolated, long-running tasks.

## Add a Reminder (Event Mode)

Use `--event` with a JSON event template to fire any bus event on schedule. No `--contact` needed.

```bash
# Nightly script task at 2am
claude-assistant remind add "Nightly consolidation" --cron "0 2 * * *" \
  --event '{"topic":"tasks","type":"task.requested","key":"+1234567890","payload":{"task_id":"nightly-consolidation","title":"Nightly consolidation","requested_by":"+1234567890","notify":true,"timeout_minutes":30,"execution":{"mode":"script","command":["bash","-c","$HOME/dispatch/scripts/nightly-consolidation.sh"]}}}'

# One-off agent task in 10 minutes
claude-assistant remind add "Analyze logs" --in 10m \
  --event '{"topic":"tasks","type":"task.requested","key":"+1234567890","payload":{"task_id":"log-analysis","title":"Analyze logs","requested_by":"+1234567890","execution":{"mode":"agent","prompt":"Check ~/dispatch/logs/manager.log for errors in the last hour"}}}'
```

The `--event` JSON must have at minimum `topic` and `type` fields. For `task.requested` events, `payload.execution.mode` must be `"agent"` or `"script"`.

### Time Formats

**Relative (`--in`)**:
- `30m` - 30 minutes from now
- `2h` - 2 hours from now
- `1d` - 1 day from now
- `1w` - 1 week from now
- `2h30m` - 2 hours 30 minutes

**Absolute (`--at`)**:
- `3pm`, `3:30pm` - today (or tomorrow if past)
- `15:00` - 24-hour format
- `2026-03-03 15:00` - specific datetime

**Cron (`--cron`)**:
- `0 9 * * *` - 9am daily
- `0 9,21 * * *` - 9am and 9pm daily
- `30 8 * * 1-5` - 8:30am weekdays
- `0 12 1 * *` - Noon on 1st of month

## List Reminders

```bash
# All reminders
claude-assistant remind list

# Filter by contact
claude-assistant remind list --contact "John Smith"

# Show failed reminders only
claude-assistant remind list --failed

# Full records as JSON (id, title, next_fire, schedule, contact/event) - use this from scripts
claude-assistant remind list --json
claude-assistant remind list --json | jq -r '.[] | select(.title | test("walmart"; "i")) | .id'
```

The table truncates long titles with `..` **only when printing to a terminal**; piped output shows full titles. Still, for anything scripted, use `--json`, not `grep`/`awk` over the table.

Output:
```
ID         Title                          Next Fire                 Contact/Event
--------------------------------------------------------------------------------
abc12345   Check chess game               2026-03-03 03:00 PM EST   John Smith
def67890   Daily standup                  2026-03-04 09:00 AM EST   John Smith
ghi11111   Nightly consolidation          2026-03-04 02:00 AM EST   Event: task.r..
```

## Cancel a Reminder

```bash
# By ID
claude-assistant remind cancel abc12345

# By exact full title (--force if several share that exact title)
claude-assistant remind cancel --title "Daily standup"
claude-assistant remind cancel --title "Daily standup" --force

# By case-insensitive substring of the FULL title - cancels ALL matches,
# prints each cancelled "id → title", exits 1 if nothing matched
claude-assistant remind cancel --match "walmart verification"
```

`cancel` exits non-zero (with an error on stderr) when the id/title/match hits nothing - check the exit code and the `Cancelled: <id> → <title>` lines.

**Cancelling safely (do NOT `list | grep | awk | xargs cancel`):** that pipeline has silently cancelled nothing twice (truncated titles + empty xargs input runs nothing and exits 0), letting stale reminders fire. Instead:
1. Use the `ID:` you captured from `remind add`, or `remind cancel --match "<distinctive substring>"`.
2. Verify it is gone before telling anyone it's cancelled: `claude-assistant remind list --json | jq -r '.[] | "\(.id) \(.title)"'`.

## Retry Failed Reminder

When a reminder fails 3 times, it's marked dead. To retry:

```bash
claude-assistant remind retry abc12345
```

## Preview Cron Schedule

```bash
claude-assistant remind next "0 9 * * *"
# Next 5 fire times for '0 9 * * *':
#   2026-03-04 09:00 AM EST
#   2026-03-05 09:00 AM EST
#   ...

claude-assistant remind next "0 9 * * *" --tz "Asia/Tokyo"
```

## Timezone Handling

- **Default**: System timezone (typically `America/New_York`)
- **Per-reminder override**: Use `--tz` flag
- **Cron patterns**: Evaluated in local time, handles DST automatically
- **Internal storage**: UTC (for reliable comparison)

## How It Works

1. Reminders stored in `~/dispatch/state/reminders.json`
2. Daemon polls every 5 seconds for due reminders
3. When due: produces `reminder.due` event to bus ("reminders" topic, keyed by chat_id) AND injects directly into session (dual path during transition)
4. Session executes task and reports results
5. On success: `once` reminders deleted, `cron` reminders advance to next fire time
6. On failure: retries 3 times with exponential backoff (1min, 2min, 4min)
7. After 3 failures: marked dead, admin alerted

### Bus Integration

Reminders produce `reminder.due` events to the "reminders" bus topic. Each event contains the full payload needed for a consumer to inject into a session (reminder_id, title, chat_id, tier, target, schedule info, timing). Currently the direct inject is the primary delivery path; the bus events enable future consumer-driven injection and analytics. See `plans/reminder-bus-producer.md` for the full design.

## Reliability Features

- **Atomic writes**: Crash-safe JSON persistence with fsync
- **File locking**: CLI and daemon share lock to prevent corruption
- **Catch-up**: Missed reminders (e.g., daemon restart) fire on startup (up to 24h late)
- **Retry with backoff**: Transient failures auto-retry
- **Admin alerts**: Dead reminders notify admin

## Migration from Reminders.app

The old Reminders.app-based system is deprecated. The native system:
- No longer polls SQLite databases
- No longer uses osascript
- No longer requires Reminders.app

Existing reminders in Reminders.app will not fire. Create new ones with `claude-assistant remind add`.
