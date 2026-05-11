# Dispatch - Personal Assistant System

**GitHub:** https://github.com/svenka-shannon/dispatch (fork) — upstream: https://github.com/svenflow/dispatch

You are a personal assistant with full access to this computer. Act like a human would. Your name comes from config.local.yaml (assistant.name).

## Git workflow

This repo is a fork. Some changes are local-only (machine paths, fork-specific config, this CLAUDE.md); others are worth sending upstream. We don't decide upfront — we cherry-pick when ready.

**Remotes:**
- `origin` → svenka-shannon/dispatch (the fork — push target)
- `upstream` → svenflow/dispatch (canonical — never push)

**Default flow — commit straight to fork `main`:**
```bash
git add <files>
git commit -m "..."
git push origin main
```
Keep commits **atomic** (one logical change each) so a single commit can be cherry-picked cleanly later. If a commit is fork-only by nature (references local paths, edits this CLAUDE.md, tweaks `config.local.yaml`, etc.), prefix the subject with `[fork]` so it's obvious it shouldn't be upstreamed.

**Upstreaming a commit (case-by-case, after the fact):**
```bash
git fetch upstream
git checkout -b upstream/<topic> upstream/main
git cherry-pick <sha>          # commit from fork main
git push origin upstream/<topic>
gh pr create --repo svenflow/dispatch --base main \
  --head svenka-shannon:upstream/<topic> --web
git checkout main              # back to fork main
```
The branch is *only* a vehicle for the upstream PR — don't merge it back into fork main (the commit is already there). Delete it after the PR closes.

**Pulling upstream changes in:**
```bash
git fetch upstream
git merge upstream/main        # or: git rebase upstream/main
git push origin main
```

## File Organization

Put things where they belong:
- **Code** → `~/code/`
- **Documents** → `~/Documents/`
- **Skills** → `~/.claude/skills/`
- **This project** → `~/dispatch/`

## Python Environment

**ALWAYS use `uv` for Python package management, NEVER pip3.**

This project uses a venv at `~/dispatch/.venv/`

```bash
cd ~/dispatch

# Install packages - ALWAYS add to pyproject.toml, never bare uv pip install
# 1. Edit pyproject.toml to add the dependency
# 2. Run: uv sync

# Run scripts
uv run python script.py
```

**CRITICAL: Never install packages with bare `uv pip install`.** Always add dependencies to `pyproject.toml` and run `uv sync`. This ensures deps survive venv recreation and are tracked in version control.

## Communication

You communicate with humans via SMS/iMessage using the skills in `~/.claude/skills/`:
- `/contacts` - Manage contacts and tiers
- `/sms-assistant` - Read and send messages

## Tier System

Contacts are organized by tier (via macOS Contacts groups):
- **admin** - Full access, own SDK session (e.g., the owner)
- **partner** - Special privileges, own SDK session (e.g., the partner)
- **favorite** - Own SDK session, restricted tools
- **family** - Own SDK session, read-only mutations need approval

## Architecture

The system uses the **Claude Agent SDK** (`claude_agent_sdk`) to manage sessions. Each contact gets an in-process `ClaudeSDKClient` instance — no tmux, no subprocess shells.

```
~/dispatch/
├── assistant/
│   ├── manager.py       # Main daemon: polls messages, routes to sessions
│   ├── sdk_backend.py   # SDKBackend: manages all SDK sessions
│   ├── sdk_session.py   # SDKSession: wraps ClaudeSDKClient per contact
│   ├── backends.py      # BackendConfig: messaging backend definitions (imessage/signal/test)
│   ├── cli.py           # CLI entrypoint (claude-assistant command)
│   ├── common.py        # Shared constants and helpers
│   ├── bus_helpers.py   # Bus event production helpers (produce_event, event taxonomy v6)
│   ├── resources.py     # ResourceRegistry, ManagedSQLiteReader/Writer (centralized FD/connection lifecycle)
│   ├── health.py        # Health check logic (fast regex + deep LLM)
│   ├── reminders.py     # Reminder system
│   ├── readers.py       # Message readers (iMessage, Signal)
│   ├── perf.py          # Performance metric recording
│   └── config.py        # Configuration loader
├── bus/                  # Kafka-on-SQLite message bus
│   ├── bus.py           # Core bus: Producer (write queue + background thread), Consumer groups
│   ├── cli.py           # Bus CLI (create-topic, consume, stats, tail, export)
│   └── consumers.py     # Consumer framework with declarative configs
├── state/               # Persistent state
│   ├── last_rowid.txt   # Last processed iMessage ROWID
│   ├── sessions.json    # Maps chat_id → session metadata
│   └── bus.db           # SQLite bus database (records + sdk_events tables)
├── logs/
│   ├── manager.log      # Main daemon log
│   ├── session_lifecycle.log  # Session create/kill/restart events
│   ├── perf-YYYY-MM-DD.jsonl  # Performance metrics (daily rotation)
│   └── sessions/        # Per-session logs (john-smith.log, etc.)
├── plans/               # Architecture plans and design docs
├── .venv/               # Python virtual environment
└── CLAUDE.md            # This file
```

### Message Bus (Kafka-on-SQLite)

The system includes an event bus built on SQLite for audit trails, analytics, and future multi-consumer fanout.

**Architecture:**
- **Write queue**: `produce_event()` enqueues to an in-memory queue (~microseconds, fire-and-forget)
- **Background writer thread**: Drains queue, batches up to 100 records per transaction
- **WAL mode**: Concurrent reads during writes, no blocking
- **Two storage tiers**:
  - `records` table: Business events (messages, sessions, system) with 7-day retention
  - `sdk_events` table: Tool call traces with structured columns, 3-day retention

**Event taxonomy (v6) — 8 topics:**
- `messages`: message.received/sent/failed/ignored, reaction.received/ignored
- `sessions`: session.created/restarted/killed/crashed/injected/idle_killed/prewarmed/tier_mismatch, permission.denied, command.restart
- `system`: daemon.started/stopped, health.check_completed/failed, consumer.crashed, sdk.turn_complete, signal.connection_state, and more
- `reminders`: reminder.fired, task.requested
- `tasks`: task.started/completed/failed/timeout
- `messages.dlq`: dead-lettered messages (failed delivery, 30-day retention)
- `facts`: fact.created/updated/expired — structured contact facts (travel, events, preferences)
- `imessage.ui`: tapback reactions, typing indicators (1-day retention)

**Integration status:** Fully integrated. `produce_event()` is called throughout manager.py and sdk_backend.py — all message routing, session lifecycle, health checks, and system events are recorded. The `Producer` is initialized in Manager and registered with the ResourceRegistry for clean shutdown.

**Bus CLI:**
```bash
cd ~/dispatch
uv run python -m bus.cli stats        # Event counts and throughput
uv run python -m bus.cli tail         # Live tail of events
uv run python -m bus.cli export       # Export events as JSONL
```

**Consumer best practices — NEVER sleep in a handler:**

Consumer handlers must process events and return quickly. Long sleeps kill the consumer's heartbeat, causing it to go DEAD and stop processing all future events.

```python
# WRONG — blocks consumer thread, kills heartbeat, consumer goes DEAD
def handle_tweet_scheduled(records):
    for record in records:
        time.sleep((post_time - now).total_seconds())  # could be hours!
        post_tweet(record)

# CORRECT — consumer handles immediately; delay lives in the scheduler
def handle_tweet_scheduled(records):
    for record in records:
        post_tweet(record)  # event was scheduled to arrive at the right time
```

**The correct pattern for delayed delivery:** Use `claude-assistant remind add --event` to schedule the bus event for the desired future time. The reminder system fires it at the right moment; the consumer handles it immediately on arrival. This is exactly how `assistant/tweet_consumer.py` works. If an event somehow arrives stale (e.g., after a restart), check `scheduled_for` age and skip if >24h old.

Check consumer health with: `uv run python -m bus.cli groups` — look for DEAD status or very old heartbeats.

### Resource Lifecycle (ResourceRegistry)

All persistent resources (file handles, SQLite connections, subprocesses) are managed through a centralized `ResourceRegistry` in `assistant/resources.py`:

- **`ResourceRegistry`**: Wraps `AsyncExitStack` with named resource tracking, FD leak detection, and safe cleanup (handles non-idempotent `close()` like SQLite). Manager's `run()` method uses `async with ResourceRegistry()` so all resources are cleaned up on shutdown via LIFO ordering.
- **`ManagedSQLiteReader`**: Single read-only connection on a dedicated `ThreadPoolExecutor(1)` thread. Used for `chat.db` reads — eliminates connection contention.
- **`ManagedSQLiteWriter`**: Single write connection on a dedicated thread. Used for bus.db writes.
- **FD leak detection**: Calibrates `/dev/fd/` baseline at startup, checks delta every 5 minutes during health checks.
- **Safe cleanup**: All cleanup callbacks wrapped in `_safe_cleanup()` to handle double-close errors (sqlite3.ProgrammingError), subprocess ProcessLookupError, etc.

### Ephemeral Tasks (Scheduled Agents)

The system supports **ephemeral tasks** — short-lived Claude agents or scripts that execute autonomously and auto-terminate. They're triggered by cron reminders that fire `task.requested` events to the bus.

**Two execution modes:**
- **Script mode**: Runs a shell command as a subprocess. No Claude session needed. Good for deterministic work (consolidation scripts, data exports, cleanup).
- **Agent mode**: Spins up a full SDK session (same `SDKSession` as chat sessions) with admin tier and all skills. Good for LLM-powered work (skillify analysis, research, summarization).

**Task lifecycle:**
```
Reminder fires → task.requested event on "tasks" topic
  → Task consumer picks it up
  → Dedup check (both agent + script tasks tracked)
  → Script: spawns subprocess | Agent: creates ephemeral SDKSession
  → task.started event + optional notification to requester
  → Runs to completion or timeout
  → task.completed/failed/timeout event + optional notification
  → Agent: session auto-killed | Script: process group cleaned up
```

**Full audit trail**: Ephemeral sessions produce all standard bus events (sdk.turn_complete, session.created, etc.) plus task-specific events. Query with: `bus tail --topic tasks`

**Adding a new scheduled task:**

Use `claude-assistant remind add --cron --event` to create scheduled tasks. This is the only supported method.

1. For script-mode: create a shell script in `~/dispatch/scripts/`
2. Create the reminder via CLI:
```bash
# Recurring nightly task at 3am ET
claude-assistant remind add "My nightly task" --cron "0 3 * * *" \
  --event '{"topic":"tasks","type":"task.requested","key":"+1...","payload":{"task_id":"my-task-id","title":"My nightly task","requested_by":"+1...","instructions":"What to do","notify":true,"timeout_minutes":30,"execution":{"mode":"script","command":["bash","-c","$HOME/dispatch/scripts/my-task.sh"]}}}'

# For agent mode, use "prompt" instead of "command" in the execution block:
claude-assistant remind add "My agent task" --cron "0 3 * * *" \
  --event '{"topic":"tasks","type":"task.requested","key":"+1...","payload":{"task_id":"my-agent-task","title":"My agent task","requested_by":"+1...","notify":true,"timeout_minutes":30,"execution":{"mode":"agent","prompt":"Run /some-skill and send results via SMS"}}}'

# One-off task in 10 minutes
claude-assistant remind add "Check something" --in 10m \
  --event '{"topic":"tasks","type":"task.requested","key":"+1...","payload":{...}}'
```
3. Verify with `claude-assistant remind list`

> **DEPRECATED:** `scripts/setup-nightly-tasks.py` was the old way to manage nightly tasks. Do NOT use it for new tasks. All task management should go through `claude-assistant remind add --event`.

**Current scheduled tasks:** Run `claude-assistant remind list` to see all registered nightly tasks. Tasks use the `nightly-*` task_id convention.

**Key files:**
- `assistant/manager.py` — `_run_task_consumer()`, `_handle_task_requested()`, `_run_script_task()`, `_supervise_ephemeral_tasks()`
- `assistant/sdk_backend.py` — `create_ephemeral_session()`, `kill_ephemeral_session()`
- `assistant/reminders.py` — `create_reminder()` with event templates, `validate_event_template()`
- ~~`scripts/setup-nightly-tasks.py`~~ — DEPRECATED: use `claude-assistant remind add --event` instead
- `scripts/nightly-consolidation.sh` — wrapper for consolidation scripts
- `plans/ephemeral-tasks-and-scheduler.md` — full design doc

### Reminders System

Reminders are a **generalized scheduling system** — "cron for the bus." A reminder can:
- **Legacy mode**: Inject a message into a chat session at a scheduled time (contact + target)
- **Generalized mode**: Produce ANY bus event at a scheduled time (event template)

**How it works:**
```
ReminderPoller (every 5s) → checks next_fire times
  → Due? → Legacy: inject into session directly
          → Generalized: produce event to bus (any topic, any type)
  → Advance next_fire (cron) or delete (once)
  → Save state to reminders.json
```

**Creating reminders (two modes):**
```bash
# Legacy mode (contact + target): inject into a chat session
claude-assistant remind add "Check the deploy" --contact "+1..." --in 10m
claude-assistant remind add "Morning check-in" --contact "+1..." --cron "0 9 * * *"
claude-assistant remind add "Background task" --contact "+1..." --in 1h --target bg

# Event mode (--event): fire any bus event on schedule (no --contact needed)
claude-assistant remind add "Nightly task" --cron "0 2 * * *" \
  --event '{"topic":"tasks","type":"task.requested","key":"+1...","payload":{...}}'
```

**Listing reminders:** `claude-assistant remind list` shows both modes. Event-mode reminders display "Event: task.requested" (or similar) in the Contact/Event column instead of a contact name.

**Key files:**
- `assistant/reminders.py` — Core module (create, validate, fire, schedule)
- `~/.claude/skills/reminders/SKILL.md` — User-facing docs
- `state/reminders.json` — Persistent state (reminders + config)

### Disk Monitoring (APFS)

Disk warnings use APFS container metrics (not `shutil.disk_usage`) to include purgeable space — matching what macOS shows as "Available." See `_get_apfs_container_space()` in `health.py`. This avoids false alarms where `shutil` reports low space but macOS shows much more available due to purgeable files.

### Key design: no auto-send

SDK sessions do NOT auto-send text output as SMS. Claude calls `send-sms` explicitly via Bash tool when it wants to message the user — same as the old tmux setup. The SDK just manages the Claude process lifecycle.

### Session flow

1. Message arrives in chat.db → daemon polls it
2. Contact lookup → tier determination
3. `produce_event()` records `message.received` to bus (audit trail)
4. `SDKBackend.inject_message()` → creates session on-demand if needed
5. Message wrapped in SMS tags → queued into `SDKSession._message_queue`
6. `SDKSession._run_loop()` pulls from queue → `client.query()` → processes response
7. Claude calls `send-sms` via Bash when it wants to reply

### Image handling (Gemini Vision)

When a message includes an image attachment:
1. Image saved locally (iMessage: `~/Library/Messages/Attachments/`, Signal: `~/.local/share/signal-cli/attachments/`)
2. `MessageReader` retrieves conversation context around the image timestamp
3. Context + image sent to Gemini for description
4. Description injected into Claude session as additional context

Supported backends: iMessage, Signal, sven-app (via `supports_image_context` flag in `BackendConfig`)

### Session resume

Sessions save their `session_id` to `sessions.json` on shutdown. On restart, `ClaudeSDKClient` resumes from that ID so conversation context is preserved.

## Running the System

```bash
claude-assistant start      # Start the daemon
claude-assistant stop       # Stop the daemon
claude-assistant restart    # Restart the daemon
claude-assistant status     # Show status and active sessions
claude-assistant logs       # Tail the log file
claude-assistant attach <session>  # Tail a session's log file

claude-assistant install    # Install LaunchAgent (auto-start on boot)
claude-assistant uninstall  # Remove LaunchAgent
```

The manager daemon:
1. Polls Messages.app chat.db every 100ms
2. Looks up sender's tier via contacts CLI
3. Routes to SDK session (created on-demand)
4. Health checks sessions every 5 minutes, auto-restarts if unhealthy
5. Kills idle sessions after 2 hours

## Watchdog (Auto-Recovery)

The watchdog monitors the daemon and automatically recovers from crashes. It runs as a separate LaunchAgent, checking daemon health every 60 seconds.

### What it does

1. **Detects daemon crashes** - Runs `claude-assistant status` every 60s
2. **Spawns healing Claude** - On crash, spawns a Claude instance with `--dangerously-skip-permissions` to diagnose and fix
3. **Notifies admin** - Sends SMS to admin at each recovery attempt
4. **Exponential backoff** - Waits progressively longer between attempts (60s, 120s, 240s, ...)
5. **Gives up gracefully** - After 5 consecutive failures, alerts admin for manual intervention

### Installation

```bash
~/dispatch/bin/watchdog-install    # Install and start watchdog
~/dispatch/bin/watchdog-uninstall  # Stop and remove watchdog
~/dispatch/bin/watchdog-status     # Check watchdog status
```

The install script copies `launchd/com.dispatch.watchdog.plist` to `~/Library/LaunchAgents/` and loads it.

### How the healer works

When the daemon is down:
1. Watchdog acquires a lock to prevent duplicate healers
2. Spawns Claude with a recovery prompt that:
   - Restarts daemon via `launchctl kickstart` (has Full Disk Access)
   - Checks logs for crash cause
   - Restarts recently-active sessions
   - Sends status SMS to admin
3. Healer has 15-minute timeout
4. If recovery succeeds, crash counter resets
5. If recovery fails, backs off and retries next cycle

### Files

- `bin/watchdog` - Main watchdog script
- `bin/watchdog-install` - Installation script
- `bin/watchdog-uninstall` - Uninstall script
- `bin/watchdog-status` - Status check
- `launchd/com.dispatch.watchdog.plist` - LaunchAgent plist
- `logs/watchdog.log` - Watchdog activity log
- `logs/watchdog-launchd.log` - launchd stdout/stderr

### Troubleshooting

```bash
# Check if watchdog is running
launchctl list com.dispatch.watchdog

# View recent watchdog activity
tail -20 ~/dispatch/logs/watchdog.log

# Force daemon restart (bypasses backoff)
rm -f /tmp/dispatch-watchdog-crashes.txt
~/dispatch/bin/watchdog
```

## Dependency Health Framework

External dependencies the daemon relies on (chrome-control's MV3 service worker,
signal-cli, bus consumers, disk, FD usage) are all monitored by a single
`DependencyHealthRunner` (`assistant/dependency_health.py`), with the concrete
checks defined in `assistant/dependency_checks.py`. Each dependency is a
`DependencyCheck` (dataclass): a `probe` (→ `OK`/`DEGRADED`/`DOWN`/`SKIP`), an
optional `recover`, a poll `interval_s`, a bounded retry schedule
(`max_attempts`, `backoff_s`), an `escalate` callback (default: SMS the admin —
`Manager._send_sms` → `send-sms` CLI, NOT via a chat session), and an
`enabled_when` gate (e.g. `chrome_control` only when Chrome.app is running).

Per-dependency state machine: `HEALTHY → DEGRADED → DOWN → RECOVERING →
(HEALTHY | ESCALATED)`. On `DOWN`: attempt `recover()` up to `max_attempts` with
`backoff_s` between attempts; success → `HEALTHY` (+ recovery event); exhausted
→ `ESCALATED` + escalate. From `ESCALATED` it keeps probing; a later good probe
→ `HEALTHY` ("recovered after escalation"). Escalations are rate-limited per
dependency (one SMS per incident, re-SMS at most every few hours while still
down). A **recovery-frequency alarm** escalates if a dependency self-recovers
>5×/hour ("auto-recovery must never silently mask a worsening problem").

Probes/recoveries run **off the main loop** (`asyncio.to_thread`) with per-check
timeouts and overlap guards — they never block message routing. The runner is
ticked from the poll loop every ~15s (`Manager._run_with_registry` builds it via
`build_default_registry(self)`; the poll loop calls `self._dep_health_runner.tick()`).
Every transition + recovery attempt + escalation emits a `health.dependency` bus
event (`system` topic) and an authoritative manager-log line; healthy
steady-state is DEBUG-only.

Migrated checks: `chrome_control` (90s; probe `chrome ping`, recover `chrome
reset`, escalate on the errored-extension state — `chrome reset`/`wake` exits
non-zero — with the exact "reload at chrome://extensions/ → Developer mode →
reload ⟳" fix; replaces the old standalone `_run_chrome_control_check`),
`signal_cli` (300s; probe the JSON-RPC socket, recover = restart the signal-cli
daemon with `--receive-mode on-connection`; replaces the old 5-min ad-hoc
check), `bus_consumers` (300s; probe ConsumerRunner thread + DEAD registry
members, recover = rebuild + restart the consumer thread), `disk` (300s; probe
APFS-aware usage, recover = clean known temp dirs, else escalate), `fd_leak`
(300s; probe untracked FD growth via ResourceRegistry, no auto-recovery →
escalate). **Not migrated:** `sdk_sessions` — `health.py`'s fast-regex + deep-Haiku
verdict logic is load-bearing and stays as the daemon's own periodic check (its
recovery/escalation routing through the framework is deferred).

The blocked-session watchdog (`_run_stuck_session_check`, Phase 1) is a separate
concern — not a "dependency" — and stays as its own periodic check.

## Scheduling Tasks

**CRITICAL: NEVER create new LaunchAgents or launchd plists for scheduled tasks.** The ONLY LaunchAgents allowed are:
- `com.dispatch.daemon.plist` — the main daemon
- `com.dispatch.watchdog.plist` — the watchdog

**ALL scheduled/recurring tasks MUST use the reminder system** (`~/dispatch/state/reminders.json`). The daemon's poll loop fires reminders at their scheduled times — no separate processes needed.

```bash
# Add a nightly task at 2am ET
# Use mode "script" for shell commands, "agent" for Claude sessions
claude-assistant remind add "Task name" --cron "0 2 * * *" \
  --event '{"topic":"tasks","type":"task.requested","key":"...","payload":{...}}'

# List existing reminders
claude-assistant remind list
```

Current nightly tasks: run `claude-assistant remind list` to see all registered tasks. Tasks use the `nightly-*` task_id prefix convention.

## Structured Facts

Queryable facts about contacts (travel, events, preferences) stored in `bus.db`.

```bash
# CLI at ~/dispatch/scripts/fact
fact list --contact "+1..." --active          # List active facts
fact search "california"                      # Search by summary
fact upcoming --days 14                       # Upcoming temporal facts
fact save --contact "+1..." --type travel \   # Manual save
  --summary "Flying to SF" --details '{"destination": "San Francisco"}'
fact context --contact "+1..."                # Formatted for CLAUDE.md
fact inject --contact "+1..."                 # Write to CLAUDE.md
fact expire                                   # Expire past-due facts
```

Schema in migration `003_add_facts`. Plan at `~/dispatch/docs/plan-structured-facts.md`.
Bus events: `fact.created`, `fact.updated`, `fact.expired` on `facts` topic.

## Transcript Directories

Each contact has their own directory, organized by backend:

```
~/transcripts/
├── imessage/
│   ├── _15555550100/           # Phone number (+ replaced with _)
│   │   └── .claude/            # Directory with individual symlinks
│   │       ├── CLAUDE.md -> ~/.claude/CLAUDE.md
│   │       ├── SOUL.md -> ~/.claude/SOUL.md
│   │       ├── skills -> ~/.claude/skills
│   │       └── settings.json   # Per-session Claude Code settings
│   └── b3d258b9a4de447ca412eb335c82a077/  # Group UUID
│       └── .claude/            # Same structure
├── signal/
│   └── _15555550100/
│       └── .claude/            # Same structure
└── master/                     # Master session (unchanged)
```

Session names use the format `{backend}/{sanitized_chat_id}` (e.g., `imessage/_15555550100`).

SDK sessions run with `cwd` set to the transcript directory, so skills and CLAUDE.md are picked up automatically via the `.claude/` directory containing individual symlinks to `~/.claude/`.

## Session Startup

When a session is created (first message from contact):
1. `SDKSession` creates a `ClaudeSDKClient` with tier-appropriate tools and permissions
2. If a saved `session_id` exists in registry, resumes from it
3. If fresh session, injects system prompt (read SOUL.md, check history, respond naturally)
4. Session waits for messages on its async queue

## Testing

**CRITICAL: Always run integration tests when touching daemon/manager/CLI/backend code.**

```bash
cd ~/dispatch

# Run ALL tests - DO THIS BEFORE COMMITTING
uv run --group dev pytest tests/ -v

# Run just the core integration tests (fastest feedback loop)
uv run --group dev pytest tests/test_backends.py tests/test_session_lifecycle.py tests/test_registry.py tests/test_health_checks.py tests/test_message_routing.py tests/test_performance.py -v

# Run just unit tests
uv run --group dev pytest tests/unit/ -v
```

### What to test when

| Changed file | Tests to run |
|---|---|
| `backends.py` | `test_backends.py` (config, routing, wrapping) |
| `common.py` | `test_backends.py` + `test_message_routing.py` (normalize, is_group, wrap) |
| `sdk_session.py` | `test_session_lifecycle.py` (lifecycle, health, errors, stop hook) |
| `sdk_backend.py` | `test_session_lifecycle.py` + `test_health_checks.py` + `test_performance.py` |
| `manager.py` | `test_message_routing.py` (TestMessageWatcher normalization) |
| `cli.py` | `test_registry.py` (registry operations used by CLI) |
| `bus_helpers.py` | `test_bus_helpers.py` (event production, sanitize/reconstruct) |
| `bus/bus.py` | `test_bus.py` (producer, consumer, write queue, retention) |
| `bus/consumers.py` | `test_consumers.py` (consumer groups, offsets, commit) |
| `resources.py` | `test_resources.py` (registry lifecycle, managed sqlite, FD leak detection) |
| `health.py` | `test_health_checks.py` |
| `reminders.py` | `unit/test_poll_due.py` + `unit/test_add_reminder.py` |

### Test Structure

```
tests/
├── conftest.py                  # Mock ClaudeSDKClient + shared fixtures
├── test_backends.py             # Backend config, routing, normalize, wrap (46 tests)
├── test_session_lifecycle.py    # Session start/stop/inject, health, errors, stop hook (34 tests)
├── test_registry.py             # Registry CRUD, persistence, concurrency (14 tests)
├── test_health_checks.py        # Health checks, idle reaping, exemptions (9 tests)
├── test_message_routing.py      # TestMessageWatcher normalization, file handling (16 tests)
├── test_performance.py          # Concurrency, throughput, leaks, lock fairness (19 tests)
├── test_bus.py                  # Bus core: producer, consumer, write queue, retention, sdk_events
├── test_bus_helpers.py          # Event production, sanitize/reconstruct, taxonomy
├── test_consumers.py            # Consumer groups, offsets, commit, fanout
├── test_resources.py            # ResourceRegistry, ManagedSQLite, FD leak detection (34 tests)
├── test_restart_notify.py       # Restart notification behavior (initiator vs passive)
├── unit/                        # Pure function tests (no I/O)
│   ├── test_poll_due.py         # Reminder tag parsing, timestamps
│   ├── test_add_reminder.py     # Time parsing
│   ├── test_memory.py           # Keyword matching
│   ├── test_transcript.py       # SMS extraction
│   └── test_read_transcript.py
├── integration/                 # Integration tests with test doubles
│   ├── conftest.py              # Fixtures: fake chatdb, test env
│   ├── test_sdk_backend.py      # SDK session lifecycle tests
│   └── test_example.py          # Example integration tests
├── bin/                         # Test doubles (fake CLIs)
│   ├── test-claude              # Fake Claude CLI (no API calls)
│   ├── test-sms                 # Fake SMS sender (logs only)
│   └── test-contacts            # Fake contacts CLI (in-memory)
└── fixtures/                    # Test data
```

### How the test mock works

Tests use a `FakeClaudeSDKClient` (in `conftest.py`) that replaces the real Agent SDK. This lets us test the full session lifecycle — creation, injection, queue processing, health checks, error handling — without hitting the Claude API. The mock supports configurable delays and errors for performance/reliability testing.

### Linting Hook

A PostToolUse hook automatically runs `ruff` and `ty` on Python files after edits.
Configure in `~/.claude/settings.json`.

## Type Checking with ty

This project uses **ty** (Astral's Python type checker) to catch type errors. The `assistant/` directory must pass ty with zero errors.

### Running ty manually

```bash
cd ~/dispatch
uvx ty check --python .venv/bin/python assistant/
```

### Pre-commit hook

A git pre-commit hook automatically runs ty before each commit. If type checking fails, the commit is blocked.

To skip temporarily (not recommended):
```bash
git commit --no-verify
```

### Common type patterns

```python
# Optional parameters - ALWAYS use union syntax
def foo(name: str | None = None):  # ✓ Correct
def foo(name: str = None):          # ✗ Wrong - invalid-parameter-default

# Await async functions
result = await async_func()  # ✓ Correct
result = async_func()        # ✗ Wrong - returns coroutine, not result

# None checks before subscripting
value = d.get("key")
if value is not None:
    use(value["subkey"])  # ✓ ty knows value is not None
```

### Configuration

ty is configured in `pyproject.toml`:

```toml
[tool.ty.environment]
extra-paths = [
    "skills/contacts/scripts",
    "skills/reminders/scripts",
    # ... paths for dynamically imported modules
]
```

## Diagnostic Events (Bus)

**Sink authority:** Log files are authoritative (complete record, crash recovery, `tail -f`/`grep`). Bus has a structured subset for pattern queries across time. Bus is a convenience layer — if bus writes fail, events fall back to log-only silently.

| Event | Key Fields | Query |
|---|---|---|
| `health.haiku_verdict` | check_run_id, check_type (deep/stuck), session_name, chat_id, verdict (FATAL/HEALTHY/STUCK/WORKING), action_taken (restart/none) | `bus replay system --type health.haiku_verdict --limit 50` |
| `health.circuit_breaker` | check_run_id? (see below), session_name, chat_id, transition (opened/closed), restart_count | `bus replay system --type health.circuit_breaker --limit 50` |
| `health.quota_alert` | quota_type (5-hour/7-day all/7-day sonnet/7-day opus/extra usage), utilization, threshold, resets_at | `bus replay system --type health.quota_alert --limit 50` |
| `health.dependency` (source=`dependency_health`, key=`<name>`) | name (chrome_control/signal_cli/bus_consumers/disk/fd_leak), state (HEALTHY/DEGRADED/DOWN/RECOVERING/ESCALATED), prev_state, action_taken (probe/recover/escalate/none), attempt?/max_attempts? (when RECOVERING), recovered?/recovered_after_escalation? flags, detail, plus check-specific diagnostics (probe_rc, probe_output, probe_timed_out, recover_rc, recover_output, recover_timed_out, disable_reason, dead_members, used_pct, free_gb, fd_actual, ...). Emitted on every state transition + every recovery attempt + every escalation (incl. the recovery-frequency alarm — >5 self-recoveries/hr); healthy steady-state is log-DEBUG only. Replaces the old `health.chrome_check` event. | `bus replay system --type health.dependency --limit 50` · per-dep: `bus search chrome_control --topic system` · recoveries in 24h: filter `action_taken=recover` |
| `health.bus_check` | status:"ok" — startup canary only | N/A |
| `session.stuck_nudge` (topic: `sessions`) | session_name, chat_id, idle_minutes, detection (marker/heuristic), contact_name?, committed_text? — blocked-session watchdog nudged an idle session that still has an outstanding commitment (a `claude-assistant commitment set` marker, or a last message matching a small commitment-phrase list); the nudge tells the session to re-check the blocker / escalate with a specific ask. Idle threshold N is `watchdog.stuck_session_idle_minutes` in config (default 5); ~75s cadence; per-session 2×N back-off so it nudges once, then the session escalates. | `bus replay sessions --type session.stuck_nudge --limit 50` |

**Cross-event correlation:** `bus search "<check_run_id_uuid>" --topic system` — shows all events from the same health check cycle.

**Schema rules:**
- Producer: add new optional fields freely, no version bump. Breaking changes → bump `schema_v`.
- Consumer: use `.get()` for optional fields. On unexpected `schema_v`, log-warn and process best-effort — don't raise.

**`check_run_id` on circuit_breaker:** Present when transition occurs within a health check cycle. Absent means transition outside a cycle (e.g., `is_open()` auto-transitions open→half_open on timeout, which is not captured as an event). Not a bug — just a different code path.

## Related Repos

### browser-automation-benchmark
**Repo:** https://github.com/svenflow/browser-automation-benchmark
**Local:** `~/code/browser-automation-benchmark/`

Public benchmark suite comparing browser automation tools (chrome-control, browser-use, Playwright, Selenium). **When you make changes to chrome-control that affect its interface or performance, update the chrome-control driver in the benchmark repo too** (`~/code/browser-automation-benchmark/drivers/chrome_control_driver.py`) and push the changes.
