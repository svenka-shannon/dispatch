---
name: bus
description: Dispatch message bus (Kafka-on-SQLite) — schema, query patterns, anomaly detection, CLI usage. Reference for bug-finder, latency-finder, and debug skills. Trigger words - bus, bus events, bus query, bus schema, event bus.
---

# Dispatch Message Bus

The bus is a Kafka-on-SQLite event system at the core of Dispatch. All messages, session lifecycle events, health checks, and SDK tool calls flow through it.

## Architecture

- **Database**: `~/dispatch/state/bus.db` (SQLite with WAL mode)
- **Implementation**: `~/dispatch/bus/bus.py` (producer/consumer/topics)
- **CLI**: `~/dispatch/bus/cli.py` (or `~/dispatch/bin/bus`)
- **Search**: `~/dispatch/bus/search.py` (FTS5 full-text search)
- **Retention**: Hot tables auto-pruned, archive tables infinite retention
- **Timestamps**: ALL timestamps in bus.db are Unix **milliseconds** (ms since epoch)

## CLI Usage

## Searching for Past Messages / Conversations

**This is the #1 tool for "find that conversation" requests.** The bus records ALL messages (sent and received) across iMessage, Signal, and Dispatch App with full-text search. Use this BEFORE searching chat.db, dispatch-messages.db, or transcript files.

```bash
cd ~/dispatch

# Search for any past message by keyword (searches full message text)
uv run python -m bus.cli search "crosstrek paint" --limit 10

# Narrow to a specific chat
uv run python -m bus.cli search "birthday gift" --key "ab3876ca883949d2b0ce9c4cd5d1d633" --limit 10

# Search only received messages
uv run python -m bus.cli search "subaru" --type message.received --limit 10

# Search within a time window
uv run python -m bus.cli search "allston collision" --since 30 --limit 10
```

**Why bus search first:**
- Has full plaintext of ALL messages (iMessage `text` field is often NULL for group chats — only `attributedBody` blob exists in chat.db)
- Covers all backends (iMessage, Signal, Dispatch App) in one search
- FTS5 is fast and handles partial matches
- Archive tables preserve messages beyond the 7-day hot retention

**Fallback order if bus doesn't have it:**
1. `bus.cli search` (FTS on bus.db — covers all backends, always has plaintext)
2. `dispatch-messages.db` (Dispatch App messages)
3. `read-sms` CLI (parses attributedBody correctly — never query chat.db text field directly for group chats)
4. Transcript compaction files (`~/transcripts/*/.compactions/*.md`)


All CLI commands run from `~/dispatch`:

```bash
cd ~/dispatch

# --- Topic management ---
uv run python -m bus.cli topics                          # List all topics
uv run python -m bus.cli info <topic>                    # Topic details (partitions, offsets, record count)
uv run python -m bus.cli create-topic <name> [--partitions N] [--retention-days N]
uv run python -m bus.cli delete-topic <name>             # Nuclear — deletes ALL records

# --- Produce & consume ---
uv run python -m bus.cli produce <topic> '<json>' --type <type> --source <src> --key <key> [--headers '<json>']
uv run python -m bus.cli consume <topic> --group <grp> [--follow] [--from-beginning] [--max N]
uv run python -m bus.cli tail <topic> [--group <grp>]    # Follow from end (like tail -f)

# --- Replay & inspect (no consumer group, direct read) ---
uv run python -m bus.cli replay <topic> [--limit N] [--from-offset N] [--from-timestamp MS] [--partition N] [--type TYPE] [--source SRC]
uv run python -m bus.cli stats [--topic <topic>]         # Record counts, timestamps, archive sizes, consumer groups

# --- Consumer group management ---
uv run python -m bus.cli groups                          # Show groups with members, generations, assignments
uv run python -m bus.cli offsets [--group GRP] [--topic T]  # Show committed offsets and lag
uv run python -m bus.cli seek --group <grp> --topic <t> [--to-beginning|--to-end|--to-offset N|--to-timestamp MS]

# --- Maintenance ---
uv run python -m bus.cli prune                           # Delete records past retention (moves to archive)

# --- Scan reports ---
uv run python -m bus.cli reports [--scanner NAME] [--since DAYS] [--findings-only] [--severity LEVEL] [--limit N]
# Examples:
uv run python -m bus.cli reports --scanner bug-finder --since 7
uv run python -m bus.cli reports --findings-only --severity high

# --- Full-text search (FTS5) ---
uv run python -m bus.cli search "error timeout" [--topic T] [--type T] [--key K] [--source S] [--since DAYS] [--limit N]
uv run python -m bus.cli search-sdk "connection refused" [--session S] [--event-type T] [--tool T] [--chat-id C] [--since DAYS] [--limit N]
uv run python -m bus.cli fts-status                      # Check FTS index health and drift
uv run python -m bus.cli fts-rebuild                     # Drop and rebuild FTS indexes
```

## Schema

### Hot Tables (retention-based, auto-pruned)

**records** (7-day default retention) — Business events: messages, health checks, session lifecycle

Table is `WITHOUT ROWID` with composite primary key.

| Column | Type | Description |
|--------|------|-------------|
| topic | TEXT | Event topic (e.g. "messages", "system"). NOT NULL |
| partition | INTEGER | Partition number within topic. NOT NULL |
| offset | INTEGER | Per-partition sequence number. NOT NULL |
| timestamp | INTEGER | Unix epoch milliseconds. NOT NULL |
| key | TEXT | Routing key (e.g. "imessage/+15555550100"). Nullable |
| type | TEXT | Event type (e.g. "message.received", "session.heartbeat"). Nullable |
| source | TEXT | Producer source (e.g. "imessage", "sdk", "health"). Nullable |
| payload | TEXT | JSON payload. NOT NULL |
| headers | TEXT | JSON headers. Nullable |

Primary key: `(topic, partition, offset)`

**sdk_events** (3-day retention) — Per-tool execution traces

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER | Auto-increment primary key |
| timestamp | INTEGER | Unix epoch milliseconds. NOT NULL |
| session_name | TEXT | Session identifier. NOT NULL |
| chat_id | TEXT | Contact/group chat ID. Nullable |
| event_type | TEXT | tool_use, tool_result, result, error. NOT NULL |
| tool_name | TEXT | Tool name (e.g. "Bash", "Read", "Edit"). Nullable |
| tool_use_id | TEXT | Unique tool use identifier. Nullable |
| duration_ms | REAL | Execution time in milliseconds. Nullable |
| is_error | INTEGER | 1 if error, 0 otherwise. Default 0 |
| payload | TEXT | JSON with details. Nullable |
| num_turns | INTEGER | Number of conversation turns. Nullable |

### FTS Tables (full-text search via FTS5)

**records_fts** — Full-text index over records + records_archive. Auto-populated by INSERT/DELETE triggers on both tables. Columns: topic, key, type, source, payload_text, timestamp, partition, offset_val.

**sdk_events_fts** — Full-text index over sdk_events + sdk_events_archive. Auto-populated by triggers. Columns: session_name, event_type, tool_name, payload_text, chat_id, timestamp, source_id.

Payload text extraction logic (for records_fts):
- `message.*` types: `json_extract(payload, '$.text')`
- `scan.*` types: `json_extract(payload, '$.summary')`
- `session.*` types: contact_name + session_name from payload
- `health.*` types: status + message from payload
- Everything else: first 4000 chars of raw payload

### Archive Tables (infinite retention)
- **records_archive** — Same schema as records + `archived_at INTEGER NOT NULL` column. Same composite PK.
- **sdk_events_archive** — Same schema as sdk_events + `archived_at INTEGER NOT NULL` column.

Auto-populated when records are pruned from hot tables. Use for longer-term baselines and trend analysis.

### Indexes

**records:**
- Primary key: `(topic, partition, offset)` — clustered (WITHOUT ROWID)
- `idx_records_topic_ts`: `(topic, timestamp)`
- `idx_records_key`: `(topic, key) WHERE key IS NOT NULL`
- `idx_records_type`: `(topic, type) WHERE type IS NOT NULL`
- `idx_records_source`: `(topic, source) WHERE source IS NOT NULL`
- `idx_records_chat_id`: `(topic, json_extract(payload, '$.chat_id')) WHERE topic = 'messages'` — functional index for message lookups by chat_id

**sdk_events:**
- `idx_sdk_session`: `(session_name, timestamp)`
- `idx_sdk_type`: `(event_type)`
- `idx_sdk_tool`: `(tool_name) WHERE tool_name IS NOT NULL`

**records_archive:**
- Primary key: `(topic, partition, offset)` — clustered (WITHOUT ROWID)
- `idx_archive_topic_ts`: `(topic, timestamp)`
- `idx_archive_type`: `(topic, type) WHERE type IS NOT NULL`
- `idx_archive_key`: `(topic, key) WHERE key IS NOT NULL`
- `idx_archive_archived_at`: `(archived_at)`

**sdk_events_archive:**
- `idx_sdk_archive_session`: `(session_name, timestamp)`
- `idx_sdk_archive_tool`: `(tool_name) WHERE tool_name IS NOT NULL`
- `idx_sdk_archive_archived_at`: `(archived_at)`

## Common Event Types

| Type | Source | Description |
|------|--------|-------------|
| message.received | imessage/signal/discord | Incoming message from a contact |
| message.sent | sdk_session | Outgoing message to a contact |
| message.delivered | sdk_session | Message confirmed delivered |
| session.heartbeat | sdk | Session is alive |
| session.created | daemon | New session spawned |
| session.restarted | daemon | Session restarted |
| session.killed | daemon | Session terminated |
| session.injected | inject | Prompt injected into session |
| health.fast_check_completed | health | 100ms health check |
| health.check_completed | health | Standard health check |
| health.deep_check_completed | health | Deep health check |
| health.service_restarted | health | Service auto-restarted |
| scan.completed | bug-finder/latency-finder | Nightly scan results |
| compaction.triggered | compaction | Bus compaction ran |

## Anomaly Detection Queries

### 1. Source Volume Distribution (find runaway producers)
```sql
SELECT source, COUNT(*) as cnt,
  ROUND(100.0 * COUNT(*) / (SELECT COUNT(*) FROM records), 1) as pct
FROM records GROUP BY source ORDER BY cnt DESC LIMIT 15;
```
**Red flag:** Any single source >30% of total events, or any unexpected source appearing.

### 2. Retry & DLQ Spikes (processing failures)
```sql
SELECT source, type, COUNT(*) as cnt,
  MIN(datetime(timestamp/1000,'unixepoch','localtime')) as first_seen,
  MAX(datetime(timestamp/1000,'unixepoch','localtime')) as last_seen
FROM records
WHERE source LIKE '%retry%' OR source LIKE '%dlq%'
GROUP BY source, type ORDER BY cnt DESC;
```
**Red flag:** Any consumer-retry count >10 indicates a processing bug. Check payloads for the error.

### 3. Error Payload Analysis (what's actually failing)
```sql
SELECT substr(payload, 1, 500) as error_preview, COUNT(*) as cnt
FROM records WHERE source LIKE '%retry%'
GROUP BY substr(payload, 1, 200) ORDER BY cnt DESC LIMIT 10;
```
Look for repeated error patterns like `KeyError`, `ConnectionRefused`, `Timeout`.

### 4. Event Rate Spikes (15-min buckets)
```sql
SELECT datetime((timestamp/900000)*900, 'unixepoch', 'localtime') as bucket,
  source, COUNT(*) as cnt
FROM records GROUP BY bucket, source
HAVING cnt > 50 ORDER BY cnt DESC LIMIT 20;
```
**Red flag:** >100 events from one source in a 15-min window is almost always a bug.

### 5. Source x Type Breakdown (find unexpected event patterns)
```sql
SELECT source, type, COUNT(*) as cnt
FROM records GROUP BY source, type ORDER BY cnt DESC LIMIT 30;
```
Compare against expected patterns. For example, `consumer-retry` + `message.received` = retry loop.

### 6. Failed/Error Events
```sql
SELECT datetime(timestamp/1000,'unixepoch','localtime') as ts,
  type, source, key, substr(payload, 1, 300) as payload
FROM records
WHERE type LIKE '%failed%' OR type LIKE '%crashed%' OR type LIKE '%error%'
ORDER BY timestamp DESC LIMIT 20;
```

### 7. SDK Tool Failures
```sql
SELECT tool_name, event_type, COUNT(*) as cnt, AVG(duration_ms) as avg_ms,
  MAX(duration_ms) as max_ms
FROM sdk_events WHERE is_error=1
GROUP BY tool_name, event_type ORDER BY cnt DESC LIMIT 15;
```

### 8. SDK Tool Performance (slowest tools)
```sql
SELECT tool_name, COUNT(*) as calls, AVG(duration_ms) as avg_ms,
  MAX(duration_ms) as max_ms,
  SUM(CASE WHEN is_error=1 THEN 1 ELSE 0 END) as errors
FROM sdk_events WHERE tool_name IS NOT NULL
GROUP BY tool_name ORDER BY avg_ms DESC LIMIT 15;
```

### 9. Per-Session Event Volume (find chatty sessions)
```sql
SELECT session_name, COUNT(*) as cnt, AVG(duration_ms) as avg_ms
FROM sdk_events GROUP BY session_name ORDER BY cnt DESC LIMIT 20;
```

### 10. Message Delivery Gaps (received but never responded)
```sql
SELECT r1.key as chat_id,
  datetime(r1.timestamp/1000,'unixepoch','localtime') as received_at,
  json_extract(r1.payload, '$.sender_name') as sender,
  substr(json_extract(r1.payload, '$.text'), 1, 100) as preview,
  r1.source as backend
FROM records r1
WHERE r1.type = 'message.received'
  AND r1.timestamp > (strftime('%s','now','-24 hours') * 1000)
  AND NOT EXISTS (
    SELECT 1 FROM records r2
    WHERE r2.key = r1.key AND r2.type = 'message.sent'
      AND r2.timestamp > r1.timestamp
      AND r2.timestamp < r1.timestamp + 300000
  )
ORDER BY r1.timestamp DESC LIMIT 20;
```

### 11. Session Restart Loops
```sql
SELECT key, COUNT(*) as restarts,
  MIN(datetime(timestamp/1000,'unixepoch','localtime')) as first,
  MAX(datetime(timestamp/1000,'unixepoch','localtime')) as last
FROM records WHERE type = 'session.restarted'
  AND timestamp > (strftime('%s','now','-24 hours') * 1000)
GROUP BY key HAVING restarts > 2 ORDER BY restarts DESC;
```

### 12. Hourly Event Trend (detect patterns over time)
```sql
SELECT datetime((timestamp/3600000)*3600, 'unixepoch', 'localtime') as hour,
  source, COUNT(*) as cnt
FROM records
WHERE timestamp > (strftime('%s','now','-24 hours') * 1000)
GROUP BY hour, source ORDER BY hour, cnt DESC;
```

## Resilience & self-healing queries

The self-healing framework (`plans/self-healing-resilience.md`) leaves a structured trail:
`health.dependency` (topic `system`) — per-dependency state machine
`HEALTHY→DEGRADED→DOWN→RECOVERING→(HEALTHY|ESCALATED)`; fields include `name`,
`state`, `prev_state`, `action_taken` (`probe|recover|escalate|none`), `detail`,
`attempt`/`max_attempts` (during RECOVERING), `recovered`/`recovered_after_escalation`,
`recoveries_in_window`/`recovery_alarm_k` (recovery-frequency alarm), plus flattened
diagnostics (`probe_rc`, `probe_output`, `recover_rc`, `disable_reason`, `dead_members`,
`used_pct`, `free_gb`, `fd_actual`, `fd_delta`, ...). And `session.stuck_nudge`
(topic `sessions`) — the blocked-session watchdog: `session_name`, `chat_id`,
`idle_minutes`, `detection` (`marker|heuristic`), `contact_name?`, `committed_text?`.

> **Most of these are one CLI away:** `claude-assistant health-history [--hours N] [--dep <name>] [--limit N] [--json]`
> renders the per-dependency table (current state, recoveries 1h/24h, MTTR P50/P95,
> last escalation, recovery-frequency alarm), a transition tail, and a one-line SLO
> check. Use the raw SQL below for ad-hoc slicing the CLI doesn't cover.

```bash
# Quick look — all health.dependency events in the last 24h
uv run python -m bus.cli replay system --limit 200 \
  | grep health.dependency
# Or full-text search across the trail
uv run python -m bus.cli search "health.dependency" --topic system --limit 100
```

### R1. Recovery events in the last 24h (what self-healed, and how)
```sql
SELECT datetime(timestamp/1000,'unixepoch','localtime') AS ts,
  key AS dep,
  json_extract(payload,'$.action_taken') AS action,
  json_extract(payload,'$.detail') AS detail
FROM records
WHERE type = 'health.dependency'
  AND timestamp > (strftime('%s','now','-24 hours') * 1000)
  AND (json_extract(payload,'$.action_taken') = 'recover'
       AND (json_extract(payload,'$.recovered') = 1
            OR json_extract(payload,'$.recovered_after_escalation') = 1))
ORDER BY ts;
```

### R2. MTTR per dependency (pair DOWN/DEGRADED → next HEALTHY by name)
```sql
-- For each impaired-start event, find the next HEALTHY for the same dep and
-- diff the timestamps. (Roughly what `claude-assistant health-history` does;
-- the CLI also de-dups overlapping incidents and computes P50/P95.)
WITH ev AS (
  SELECT key AS dep, timestamp AS ts,
         json_extract(payload,'$.state') AS state
  FROM records
  WHERE type = 'health.dependency'
    AND timestamp > (strftime('%s','now','-7 days') * 1000)
),
starts AS (
  SELECT dep, ts FROM ev WHERE state IN ('DOWN','DEGRADED')
),
recovered AS (
  SELECT s.dep, s.ts AS down_ts,
         (SELECT MIN(h.ts) FROM ev h
          WHERE h.dep = s.dep AND h.state = 'HEALTHY' AND h.ts > s.ts) AS up_ts
  FROM starts s
)
SELECT dep,
  COUNT(*) AS incidents,
  ROUND(AVG((up_ts - down_ts)/1000.0),1) AS mean_recovery_s,
  MIN((up_ts - down_ts)/1000) AS fastest_s,
  MAX((up_ts - down_ts)/1000) AS slowest_s
FROM recovered WHERE up_ts IS NOT NULL
GROUP BY dep ORDER BY slowest_s DESC;
```

### R3. Recovery-attempt rate / which deps are flapping
```sql
-- A spike here = an underlying problem getting masked by auto-recovery.
SELECT key AS dep,
  datetime((timestamp/3600000)*3600,'unixepoch','localtime') AS hour,
  COUNT(*) AS recovery_attempts
FROM records
WHERE type = 'health.dependency'
  AND json_extract(payload,'$.action_taken') = 'recover'
  AND json_extract(payload,'$.state') = 'RECOVERING'
  AND json_extract(payload,'$.recovered') IS NULL   -- the per-attempt RECOVERING line
  AND timestamp > (strftime('%s','now','-24 hours') * 1000)
GROUP BY dep, hour
HAVING recovery_attempts > 3
ORDER BY recovery_attempts DESC;
```

### R4. All escalations (with the specific ask)
```sql
SELECT datetime(timestamp/1000,'unixepoch','localtime') AS ts,
  key AS dep,
  json_extract(payload,'$.state') AS state,
  json_extract(payload,'$.detail') AS detail,
  json_extract(payload,'$.recoveries_in_window') AS recoveries_in_window
FROM records
WHERE type = 'health.dependency'
  AND json_extract(payload,'$.action_taken') = 'escalate'
  AND timestamp > (strftime('%s','now','-7 days') * 1000)
ORDER BY ts;
```

### R5. Stuck-session nudges — who, how often, marker vs heuristic
```sql
SELECT datetime(timestamp/1000,'unixepoch','localtime') AS ts,
  key AS chat_id,
  json_extract(payload,'$.contact_name') AS contact,
  json_extract(payload,'$.detection') AS detection,
  json_extract(payload,'$.idle_minutes') AS idle_min,
  json_extract(payload,'$.committed_text') AS committed
FROM records
WHERE type = 'session.stuck_nudge'
  AND timestamp > (strftime('%s','now','-7 days') * 1000)
ORDER BY ts;

-- Frequency by contact / detection mode:
SELECT json_extract(payload,'$.contact_name') AS contact,
  json_extract(payload,'$.detection') AS detection, COUNT(*) AS nudges
FROM records WHERE type = 'session.stuck_nudge'
  AND timestamp > (strftime('%s','now','-7 days') * 1000)
GROUP BY contact, detection ORDER BY nudges DESC;
```

### R6. Is anything DOWN / ESCALATED right now? (latest state per dep)
```sql
WITH latest AS (
  SELECT key AS dep, MAX(timestamp) AS ts
  FROM records WHERE type = 'health.dependency' GROUP BY key
)
SELECT l.dep,
  datetime(l.ts/1000,'unixepoch','localtime') AS as_of,
  json_extract(r.payload,'$.state') AS state,
  json_extract(r.payload,'$.detail') AS detail
FROM latest l
JOIN records r ON r.type = 'health.dependency' AND r.key = l.dep AND r.timestamp = l.ts
ORDER BY (state IN ('DOWN','ESCALATED','DEGRADED')) DESC, l.dep;
```

## Anomaly Pattern Reference

| Pattern | Likely Bug |
|---------|-----------|
| consumer-retry >>100 events | Message processing failure (check error payload for root cause) |
| Single source >30% of events | Runaway producer, retry loop, or spam |
| >100 events/source in 15-min bucket | Burst/spike — likely a bug not rate limiting |
| sdk_events with high is_error rate for one tool | Tool implementation bug |
| message.received with no message.sent | Dropped message or session crash |
| session.restarted >3x for same key in 24h | Session crash loop |
| DLQ events appearing | Permanent processing failures (messages exhausted retries) |

## Integration with Other Skills

- **bug-finder** (`~/dispatch/skills/bug-finder/SKILL.md`): Explorer 2 (System Health) runs all anomaly detection queries from this skill. References bus schema for retry/DLQ analysis, source volume checks, and event rate spikes.
- **latency-finder** (`~/dispatch/skills/latency-finder/SKILL.md`): Uses sdk_events for tool execution timing, records for message.received-to-message.sent latency. References archive tables for longer-term baselines beyond hot table retention windows.
- Both scanners publish `scan.completed` events to the bus (topic=system), queryable via `bus reports --scanner <name>`.

## Live Dashboard

The bus has a live dashboard at the dispatch API `/dashboard` endpoint (or via Tailscale). Shows stacked area charts for event rate by source, by type, and by chat, plus SDK calls and active tasks.
