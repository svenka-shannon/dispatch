---
name: latency-finder
description: Find performance bottlenecks using parallel discovery and refinement subagents. Analyzes perf JSONL, bus.db sdk_events, bus.db records, and system resources for slow queries, tool executions, and processing delays. Trigger words - latency finder, latency scan, what's slow, performance scan, find bottlenecks.
---

# Latency Finder

Automatically discover performance bottlenecks in the Dispatch personal assistant system using a 3-phase architecture: parallel discovery explorers, parallel refinement reviewers, and a compiled report with actionable optimization suggestions.

## PII Policy

**NEVER suggest putting PII (names, phone numbers, emails, IPs, hostnames, URLs with hostnames, account usernames) directly into SKILL.md files, CLAUDE.md, or any checked-in code.** All identity and infrastructure values MUST be stored in `~/dispatch/config.local.yaml` (which is gitignored) and accessed via:

- In markdown/skill files: `!``identity dotpath`` ` dynamic prompts (resolved at load time by Claude Code)
- In Python: `from assistant.config import get; get("dotpath")`
- In shell: `~/dispatch/bin/identity dotpath`

When proposing fixes or new skills, if the proposal would include any PII or infrastructure-specific values (IPs, hostnames, URLs, usernames, real names), instead:
1. Add the value to `config.local.yaml` under an appropriate key
2. Reference it via `identity` in the skill/doc
3. Update `config.example.yaml` with a placeholder template

Examples of values that MUST go through identity:
- IP addresses, Tailscale hostnames, local network IPs
- Account emails, usernames, phone numbers
- Service URLs with hostnames (Plex, Caddy proxy, etc.)
- Real names of contacts or the system owner
- API keys, tokens (use keychain or secrets.env instead)

## When to Use

- User says "find bottlenecks", "what's slow", "latency scan", "performance scan"
- Nightly cron via "--nightly" flag
- After deploying changes that might affect performance
- System feels sluggish or users report slow responses

## Data Sources

The system has 4 key data sources for latency analysis:

1. **Perf JSONL** (`~/dispatch/logs/perf-YYYY-MM-DD.jsonl`) — Structured timing metrics recorded by `assistant/perf.py`. Daily rotation, 30-day retention. Use `~/dispatch/bin/perf-analyze` CLI for quick summaries, or DuckDB/jq for deeper analysis.

2. **bus.db sdk_events** (`~/dispatch/state/bus.db`, table `sdk_events`) — Per-tool execution traces with session_name, tool_name, duration_ms, is_error, payload. 3-day retention. Indexes on (session_name, timestamp), (event_type), (tool_name).

3. **bus.db records** (`~/dispatch/state/bus.db`, table `records`) — Business events with timestamps. Track message.received -> message.sent latency, session lifecycle timing. 7-day retention.

4. **System resources** — Process memory/CPU, open FDs, WAL file sizes, daemon health logs.

5. **Archive tables** (`bus.db`, tables `records_archive` and `sdk_events_archive`) — Same schema as hot tables plus `archived_at` column. infinite retention. Auto-populated when records are pruned from hot tables. Use for longer-term baselines and trend analysis beyond the hot table retention windows.

6. **Bus Skill Reference** — See the `/bus` skill (`~/dispatch/skills/bus/SKILL.md`) for comprehensive schema docs, anomaly detection queries, CLI usage, and event type reference. All explorers that query bus.db should reference this skill for standard patterns (retry spikes, source volume anomalies, event rate bursts, error payload analysis, etc.).

### Dynamic Baselines

**NEVER hardcode baseline thresholds.** Baselines are computed dynamically by reading historical perf data. The approach:

1. **Yesterday's p95 is today's baseline.** Compare today's metrics against yesterday's (or last N days' average) to detect regressions.
2. **Use `perf-analyze --days 2`** to get both today and yesterday side-by-side.
3. **Regression = 2x+ increase** in p95 from yesterday. A 3x+ increase is critical.
4. **Absolute thresholds only for binary metrics** like `wal_checkpoint_status` (0 = ok, != 0 = bad).
5. **If no historical data exists**, note it as "no baseline available" rather than guessing.

Explorers MUST compute their own baselines by querying historical data — never reference hardcoded threshold values.

## How It Works

### Phase 1: Discovery (Parallel Explore Subagents)

Launch **4 parallel Explore subagents** simultaneously. Each focuses on a different latency surface area. All run with `subagent_type="Explore"` and `model="opus"`.

**IMPORTANT: All subagents (both discovery and refinement) MUST use `model: "opus"`.** Never use sonnet or haiku.

**Admin transcript context:** When scanning `~/dispatch/`, Explorer 1 (Perf Metrics) MUST also read recent admin transcripts to understand the context of changes — what was being built, what problems were being solved, and what the admin's intent was. This prevents flagging intentional changes as regressions.
- First, look up the admin's chat ID dynamically: `~/.claude/skills/contacts/scripts/contacts list --tier admin` (use the phone/email from the first result)
- Read: `~/.claude/skills/sms-assistant/scripts/read-sms --chat "{ADMIN_CHAT_ID}" --limit 40`
- This gives the last 40 messages from the admin's 1:1 chat, which contains discussion of recent changes, performance complaints, and optimization intent.
- Use this context to: (a) understand WHY metrics changed, (b) identify latency issues the admin already knows about vs new ones, (c) find performance complaints mentioned in conversation but not yet fixed.

**Time window:** Default to last 24 hours. For multi-day scans, adjust queries:
- Perf JSONL: glob `~/dispatch/logs/perf-*.jsonl` and filter by date, or iterate over specific date files
- bus.db: change `-24 hours` to `-48 hours`, `-7 days`, etc. in the `strftime` expressions

#### Explorer 1: Perf Metrics Analysis

Prompt for the Explore subagent:

```
You are a performance analysis agent. Analyze perf metrics for the Dispatch personal assistant system.

1. Start with the perf-analyze CLI for a quick summary:
   ~/dispatch/bin/perf-analyze 2>/dev/null
   ~/dispatch/bin/perf-analyze --days 2 2>/dev/null

2. For deeper analysis, use DuckDB on the JSONL files (preferred over jq/awk for percentile calculations):

   # Overall metric health with percentiles
   duckdb -c "
     SELECT metric,
            COUNT(*) as count,
            ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY value), 1) as p50,
            ROUND(PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY value), 1) as p95,
            ROUND(PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY value), 1) as p99,
            ROUND(MAX(value), 1) as max
     FROM read_json_auto('$HOME/dispatch/logs/perf-$(date +%Y-%m-%d).jsonl')
     WHERE metric IN ('poll_cycle_ms','poll_gap_ms','inject_ms','contact_lookup_ms',
                       'message_staleness_ms','send_sms_ms','session_spawn_ms',
                       'session_wake_latency_ms','sdk_queue_depth','messages_batch_size',
                       'wal_checkpoint_status','gemini_vision_ms')
     GROUP BY metric
     ORDER BY p95 DESC
   " 2>/dev/null

   # If DuckDB is not installed, fall back to jq:
   # cat ~/dispatch/logs/perf-$(date +%Y-%m-%d).jsonl | jq 'select(.metric=="poll_cycle_ms")' ...

3. Tool execution analysis — find slow tools and skills:
   duckdb -c "
     SELECT tool,
            COALESCE(input.skill, '') as skill,
            COALESCE(input.cmd_name, '') as cmd,
            COUNT(*) as calls,
            ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY value), 1) as p50_ms,
            ROUND(PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY value), 1) as p95_ms,
            ROUND(MAX(value), 1) as max_ms,
            SUM(CASE WHEN is_error THEN 1 ELSE 0 END) as errors
     FROM read_json_auto('$HOME/dispatch/logs/perf-$(date +%Y-%m-%d).jsonl')
     WHERE metric = 'tool_execution'
     GROUP BY tool, skill, cmd
     HAVING calls >= 3
     ORDER BY p95_ms DESC
     LIMIT 30
   " 2>/dev/null

4. Find the top 20 slowest individual events:
   duckdb -c "
     SELECT ts, metric, value as ms, session, tool,
            COALESCE(input.cmd_name, '') as cmd
     FROM read_json_auto('$HOME/dispatch/logs/perf-$(date +%Y-%m-%d).jsonl')
     ORDER BY value DESC
     LIMIT 20
   " 2>/dev/null

5. Regression detection — compare today vs yesterday:
   duckdb -c "
     SELECT metric,
            ROUND(PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY value), 1) as p95_today
     FROM read_json_auto('$HOME/dispatch/logs/perf-$(date +%Y-%m-%d).jsonl')
     WHERE metric IN ('poll_cycle_ms','inject_ms','send_sms_ms','message_staleness_ms','contact_lookup_ms')
     GROUP BY metric
   " 2>/dev/null

   YESTERDAY=$(date -v-1d +%Y-%m-%d 2>/dev/null || date -d "yesterday" +%Y-%m-%d 2>/dev/null)
   if [ -f "$HOME/dispatch/logs/perf-${YESTERDAY}.jsonl" ]; then
     duckdb -c "
       SELECT metric,
              ROUND(PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY value), 1) as p95_yesterday
       FROM read_json_auto('$HOME/dispatch/logs/perf-${YESTERDAY}.jsonl')
       WHERE metric IN ('poll_cycle_ms','inject_ms','send_sms_ms','message_staleness_ms','contact_lookup_ms')
       GROUP BY metric
     " 2>/dev/null
   fi

6. Check for baseline violations by comparing today vs yesterday:
   - Use the regression detection query (step 5) results as the baseline
   - Flag any metric where today's p95 is 2x+ yesterday's p95 as a regression
   - Flag any metric where today's p95 is 3x+ yesterday's p95 as critical regression
   - For wal_checkpoint_status, any non-zero value is a violation (this is binary, not percentile-based)
   - If yesterday's data is unavailable, note "no baseline" rather than using hardcoded thresholds

7. Return a JSON array of latency candidates:
   [
     {
       "id": "PERF1-001",
       "title": "Short description of the bottleneck",
       "metric": "metric_name",
       "current_p95": 123.4,
       "baseline": 100,
       "severity_guess": "critical|high|medium|low",
       "confidence": "high|medium|low",
       "evidence": "What the data shows and why it matters",
       "affected_sessions": ["session names if specific"],
       "category": "daemon_loop|message_delivery|tool_execution|session_lifecycle|sms_send|queue_backup"
     }
   ]
```

#### Explorer 2: SDK Events Analysis (bus.db)

Prompt for the Explore subagent:

```
You are a tool execution performance analyst. Analyze the sdk_events table in bus.db for slow tool calls and session bottlenecks.

NOTE: bus.db has both hot tables (sdk_events: 3-day retention) and archive tables (sdk_events_archive: infinite retention).
For recent data (last 24h), query sdk_events. For longer-term baselines and trends, query sdk_events_archive.
You can UNION ALL across both tables for complete coverage.

1. Check what data is available (hot + archive):
   sqlite3 ~/dispatch/state/bus.db "SELECT 'hot' as source, COUNT(*), MIN(datetime(timestamp/1000,'unixepoch','localtime')), MAX(datetime(timestamp/1000,'unixepoch','localtime')) FROM sdk_events UNION ALL SELECT 'archive', COUNT(*), MIN(datetime(timestamp/1000,'unixepoch','localtime')), MAX(datetime(timestamp/1000,'unixepoch','localtime')) FROM sdk_events_archive"

2. Find the slowest tool executions in the last 24 hours:
   sqlite3 -header ~/dispatch/state/bus.db "
     SELECT session_name, tool_name, duration_ms, is_error,
            datetime(timestamp/1000,'unixepoch','localtime') as time,
            substr(payload, 1, 200) as payload_preview
     FROM sdk_events
     WHERE event_type = 'tool_use'
       AND timestamp > (strftime('%s','now','-24 hours') * 1000)
       AND duration_ms IS NOT NULL
     ORDER BY duration_ms DESC
     LIMIT 30
   "

3. Aggregate tool performance by tool name:
   sqlite3 -header ~/dispatch/state/bus.db "
     SELECT tool_name,
            COUNT(*) as call_count,
            ROUND(AVG(duration_ms), 1) as avg_ms,
            ROUND(MAX(duration_ms), 1) as max_ms,
            SUM(CASE WHEN is_error = 1 THEN 1 ELSE 0 END) as error_count,
            ROUND(SUM(duration_ms), 0) as total_ms
     FROM sdk_events
     WHERE event_type = 'tool_use'
       AND timestamp > (strftime('%s','now','-24 hours') * 1000)
       AND duration_ms IS NOT NULL
     GROUP BY tool_name
     ORDER BY total_ms DESC
   "

4. Find sessions with the most total tool time:
   sqlite3 -header ~/dispatch/state/bus.db "
     SELECT session_name,
            COUNT(*) as tool_calls,
            ROUND(SUM(duration_ms)/1000, 1) as total_seconds,
            ROUND(AVG(duration_ms), 1) as avg_ms,
            ROUND(MAX(duration_ms), 1) as max_ms
     FROM sdk_events
     WHERE event_type = 'tool_use'
       AND timestamp > (strftime('%s','now','-24 hours') * 1000)
       AND duration_ms IS NOT NULL
     GROUP BY session_name
     ORDER BY total_seconds DESC
   "

5. Look for error patterns correlated with latency:
   sqlite3 -header ~/dispatch/state/bus.db "
     SELECT tool_name, is_error,
            COUNT(*) as count,
            ROUND(AVG(duration_ms), 1) as avg_ms
     FROM sdk_events
     WHERE event_type = 'tool_use'
       AND timestamp > (strftime('%s','now','-24 hours') * 1000)
       AND duration_ms IS NOT NULL
     GROUP BY tool_name, is_error
     HAVING count > 3
     ORDER BY avg_ms DESC
   "

6. Find sequential tool calls with long gaps (Claude thinking time or queue delays).
   Note: If duration_ms is NULL for all records, use timestamp differences only (gap = t2 - t1):
   sqlite3 ~/dispatch/state/bus.db "
     SELECT s1.session_name,
            datetime(s1.timestamp/1000,'unixepoch','localtime') as t1,
            s1.tool_name as tool1,
            datetime(s2.timestamp/1000,'unixepoch','localtime') as t2,
            s2.tool_name as tool2,
            (s2.timestamp - s1.timestamp - COALESCE(s1.duration_ms, 0)) as gap_ms
     FROM sdk_events s1
     JOIN sdk_events s2 ON s1.session_name = s2.session_name
       AND s2.timestamp > s1.timestamp
       AND s2.id = (SELECT MIN(id) FROM sdk_events WHERE session_name = s1.session_name AND id > s1.id AND event_type = 'tool_use')
     WHERE s1.event_type = 'tool_use'
       AND s1.timestamp > (strftime('%s','now','-24 hours') * 1000)
       AND (s2.timestamp - s1.timestamp) > 10000
     ORDER BY gap_ms DESC
     LIMIT 20
   "
   IMPORTANT: Even if duration_ms is NULL, this query still works because it falls back to raw timestamp gaps. Always run this query — do NOT skip it just because duration_ms is NULL.

7. Return a JSON array of latency candidates:
   [
     {
       "id": "SDK1-001",
       "title": "Short description of the bottleneck",
       "metric": "tool_execution|inter_tool_gap|session_total_time",
       "tool_name": "tool name if specific",
       "current_value": 123.4,
       "baseline": "expected normal value",
       "severity_guess": "critical|high|medium|low",
       "confidence": "high|medium|low",
       "evidence": "What the data shows",
       "affected_sessions": ["session names"],
       "category": "slow_tool|high_error_rate|excessive_calls|long_gaps|session_bottleneck"
     }
   ]
```

#### Explorer 3: Message Delivery Latency (bus.db records)

Prompt for the Explore subagent:

```
You are a message delivery latency analyst. Measure end-to-end message delivery times using bus.db records. This covers both iMessage and Signal messages.

NOTE: bus.db has both hot tables (records: 7-day retention) and archive tables (records_archive: infinite retention).
For recent data (last 24h), query records. For longer-term trends, use UNION ALL with records_archive.

SCHEMA GOTCHAS (verified 2026-10-26):
- `records` has NO `id` column (PK is topic,partition,offset; WITHOUT ROWID). Never use `r.id`.
- Keys differ by type: message.received key = 'imessage/+1...', message.sent key = 'imessage:+1...'.
  NEVER join on `key`. Join on json_extract(payload,'$.chat_id') (present on both).
- message.received payload has `timestamp_ms` (user's original send time) → inbound staleness =
  r.timestamp - json_extract(payload,'$.timestamp_ms'). Measure BOTH bus→reply and send→reply.
- Count only replies with source='sdk_session' (daemon-sourced sends are system alerts).

1. Find message.received -> first sdk_session message.sent (same chat_id, within 5 min):
   sqlite3 -header ~/dispatch/state/bus.db "
     WITH rcv AS (
       SELECT timestamp ts, json_extract(payload,'$.chat_id') cid, source backend,
              json_extract(payload,'$.timestamp_ms') sent_ms
       FROM records WHERE type='message.received'
         AND timestamp > (strftime('%s','now','-24 hours') * 1000)),
     snt AS (
       SELECT timestamp ts, json_extract(payload,'$.chat_id') cid
       FROM records WHERE type='message.sent' AND source='sdk_session'
         AND timestamp > (strftime('%s','now','-25 hours') * 1000))
     SELECT rcv.cid, rcv.backend,
            datetime(rcv.ts/1000,'unixepoch','localtime') received_at,
            ROUND((rcv.ts - rcv.sent_ms)/1000.0,1) inbound_staleness_s,
            ROUND(((SELECT MIN(ts) FROM snt WHERE snt.cid=rcv.cid AND snt.ts>rcv.ts AND snt.ts<rcv.ts+300000) - rcv.ts)/1000.0,1) response_s
     FROM rcv ORDER BY response_s DESC LIMIT 30
   "

2. Received with no sdk_session reply within 5 min: run query 1 and filter `response_s IS NULL`
   (wrap it as a subquery). Exclude reactions / group chatter not addressed to the assistant.

3. Cold start: session.created keys are session names (e.g. 'ephemeral-...', 'imessage/+1...');
   match the first message.sent by chat_id = json_extract(session.created payload,'$.chat_id')
   (skip ephemeral-* sessions — they are tasks, not chats).

4. Check for session restart overhead (restarts add latency):
   sqlite3 -header ~/dispatch/state/bus.db "
     SELECT key as chat_id,
            COUNT(*) as restart_count,
            GROUP_CONCAT(datetime(timestamp/1000,'unixepoch','localtime'), ', ') as restart_times
     FROM records
     WHERE type = 'session.restarted'
       AND timestamp > (strftime('%s','now','-24 hours') * 1000)
     GROUP BY key
     ORDER BY restart_count DESC
   "

5. Response time by backend: aggregate query 1 (AVG/MAX of response_s and inbound_staleness_s) GROUP BY backend.
   Compare against the prior 6 days (change -24 hours to -7 days and exclude the last 24h) for a baseline.

6. Note: Message delivery gaps (long quiet periods) are NOT latency issues — they are simply idle periods. Do NOT report quiet periods as bottlenecks. Only report actual slow response times (received -> sent pairs with high latency).

7. Return a JSON array of latency candidates:
   [
     {
       "id": "MSG1-001",
       "title": "Description of the delivery bottleneck",
       "metric": "response_time|time_to_first_response|unanswered|restart_overhead",
       "current_value": 45.2,
       "baseline": "expected seconds",
       "severity_guess": "critical|high|medium|low",
       "confidence": "high|medium|low",
       "evidence": "What the data shows",
       "affected_sessions": ["chat_ids or session names"],
       "category": "slow_response|no_response|cold_start|restart_overhead|signal_specific"
     }
   ]
```

#### Explorer 4: System Resource & Daemon Health

Prompt for the Explore subagent:

```
You are a system resource analyst. Check for resource-related performance issues that affect latency.

1. Check daemon memory and resource usage:
   MANAGER_PID=$(pgrep -f "assistant.manager" | head -1)
   if [ -n "$MANAGER_PID" ]; then
     ps -o pid,rss,vsz,%mem,%cpu,etime -p $MANAGER_PID
     lsof -p $MANAGER_PID 2>/dev/null | wc -l
   fi

2. Check for WAL checkpoint issues (WAL buildup causes slow reads):
   ls -la ~/dispatch/state/bus.db-wal 2>/dev/null
   ls -la ~/Library/Messages/chat.db-wal 2>/dev/null
   # Compare WAL file sizes to previous days to detect growth trends
   # Also check wal_checkpoint_status metric from perf logs:
   cat ~/dispatch/logs/perf-$(date +%Y-%m-%d).jsonl 2>/dev/null | grep '"metric":"wal_checkpoint_status"' | jq -r 'select(.value != 0)' | head -10

3. Check recent poll cycle health:
   cat ~/dispatch/logs/perf-$(date +%Y-%m-%d).jsonl 2>/dev/null | grep '"metric":"poll_cycle_ms"' | tail -100 | jq -r '.value' | awk '{sum+=$1; n++; if($1>max)max=$1} END{print "last_100_polls: avg="sum/n, "max="max, "count="n}'

4. Check for CPU-intensive periods (poll_gap_ms spikes relative to yesterday's pattern):
   cat ~/dispatch/logs/perf-$(date +%Y-%m-%d).jsonl 2>/dev/null | grep '"metric":"poll_gap_ms"' | jq -r '[.ts, .value] | @tsv' | awk -F'\t' '$2 > 1000 {print $1, $2"ms"}' | tail -20

5. Check active session count over time:
   cat ~/dispatch/logs/perf-$(date +%Y-%m-%d).jsonl 2>/dev/null | grep '"metric":"active_sessions"' | jq -r '[.ts, .value] | @tsv' | tail -20

6. Check for Signal and Sven API health issues:
   sqlite3 ~/dispatch/state/bus.db "
     SELECT datetime(timestamp/1000,'unixepoch','localtime') as time, type,
            json_extract(payload, '$.reason') as reason
     FROM records
     WHERE (type LIKE 'signal%' OR type LIKE 'health.service%')
       AND timestamp > (strftime('%s','now','-24 hours') * 1000)
     ORDER BY timestamp DESC
     LIMIT 20
   "

7. Check dispatch-api health (bind failures indicate restart loop):
   tail -50 ~/dispatch/logs/dispatch-api.log 2>/dev/null | grep -i "error\|bind\|failed\|address already in use"

8. Check queue depth trends (compare against yesterday's pattern):
   cat ~/dispatch/logs/perf-$(date +%Y-%m-%d).jsonl 2>/dev/null | grep '"metric":"sdk_queue_depth"' | jq -r '[.ts, .value, .session] | @tsv' | sort -t$'\t' -k2 -rn | head -20

9. Return a JSON array of latency candidates:
   [
     {
       "id": "SYS1-001",
       "title": "Description of the resource bottleneck",
       "metric": "memory_rss|open_fds|wal_size|poll_gap|cpu_usage|queue_depth|wal_checkpoint",
       "current_value": 123,
       "baseline": "expected value",
       "severity_guess": "critical|high|medium|low",
       "confidence": "high|medium|low",
       "evidence": "What the data shows",
       "category": "memory_pressure|fd_leak|wal_buildup|cpu_contention|service_health|queue_backup"
     }
   ]
```

### Phase 2: Refinement (Parallel Subagents — One Per Candidate)

Collect all latency candidates from the 4 explorers. Deduplicate by metric+category (if two explorers found the same bottleneck, merge them and keep the higher severity version).

For each unique candidate, spawn a **refinement subagent** in parallel. Use `subagent_type="general-purpose"` and `model="opus"` (refinement agents may need to run code, test queries, or read source files to verify root causes).

**Cap at 15 refinement subagents.** If more than 15 candidates, prioritize by: severity_guess (critical first), then confidence (high first), then drop the rest with a note.

Prompt for each refinement subagent:

```
You are a performance verification agent. Investigate this latency candidate and determine if it's actionable.

Latency candidate:
- ID: {id}
- Title: {title}
- Metric: {metric}
- Current value: {current_value}
- Baseline: {baseline}
- Category: {category}
- Confidence: {confidence}
- Evidence from discovery: {evidence}
- Severity guess: {severity_guess}
- Affected sessions: {affected_sessions}

Your job:
1. Verify the data. Run the relevant queries yourself to confirm the numbers.

2. Determine root cause:
   - Is this a code issue (inefficient algorithm, missing index, unnecessary work)?
   - Is this a resource issue (memory pressure, FD exhaustion, WAL buildup)?
   - Is this an architectural issue (polling frequency, serial processing, missing caching)?
   - Is this a transient spike (one-off event, deployment, system restart)?
   - Is this expected behavior (large file, complex query, cold start)?

3. Estimate impact:
   - How many users/sessions are affected?
   - What's the user-visible effect (slow responses, timeouts, dropped messages)?
   - How often does this occur (constant, periodic, rare)?

4. Propose specific fixes with effort estimates. Fixes MUST be concrete:
   - Code change: specific file path, specific function name, specific change (e.g., "In assistant/sdk_session.py:_run_loop(), add a queue.task_done() call after processing each message")
   - Config change: specific parameter name, specific value, specific file
   - Architecture change: what to redesign, with specific entry points

   **"Investigate why X" is NOT a valid fix.** If you cannot identify a specific code change, use REFINE verdict instead.

5. Make a verdict:
   - **ACCEPT**: This is a real, actionable performance bottleneck. Root cause identified AND a specific fix proposed (file + function + change). If you can identify the root cause but not a specific fix, use REFINE.
   - **REFINE**: Data is concerning but root cause unclear, OR root cause is clear but the fix requires deeper investigation. Needs monitoring or further analysis.
   - **REFUTE**: This is not actionable — expected behavior, transient spike, or within acceptable bounds.

   **IMPORTANT**: Every ACCEPT verdict MUST include at least one concrete file path in the fix.files array. "Tool configuration" or "session prompt" are NOT valid file paths. If you cannot identify the file, use REFINE.

6. Return your verdict as JSON:
   {
     "id": "{id}",
     "verdict": "ACCEPT|REFINE|REFUTE",
     "title": "Final title (may be updated)",
     "severity": "critical|high|medium|low",
     "category": "{category}",
     "metric": "{metric}",
     "current_p95": value_or_null,
     "baseline_p95": value_or_null,
     "root_cause": "Why this is slow (ACCEPT only)",
     "impact": "User-visible effect and scope (ACCEPT only)",
     "fix": {
       "description": "What to change",
       "effort": "trivial|small|medium|large",
       "files": ["list of files to modify"],
       "priority": "immediate|next_sprint|backlog"
     },
     "reason": "Why this verdict (especially for REFUTE)"
   }

Be rigorous. A performance issue must cause measurable user-visible impact under normal operating conditions. Do not accept transient spikes, expected cold starts, or theoretical concerns.
```

### Phase 3: Fix Proposals (Single Subagent)

After refinement, spawn a **single Fix Proposal subagent** to analyze all accepted findings holistically. This agent needs visibility across ALL findings to cross-reference shared root causes and avoid proposing conflicting optimizations. Use `subagent_type="general-purpose"` and `model="opus"`.

**This phase is PLAN ONLY — no code changes are made.**

Prompt for the Fix Proposal subagent:

```
You are a performance fix planning agent. You have been given all accepted and needs-investigation latency findings from a system scan. Your job is to propose concrete optimization plans for each finding WITHOUT implementing any changes.

Accepted findings (ACCEPT and REFINE verdicts):
{ALL_ACCEPTED_AND_REFINED_FINDINGS_AS_JSON}

Your job:

1. **Cross-reference related findings.** Group bottlenecks that share a root cause or would be fixed by the same optimization. For example:
   - High poll_cycle_ms and high message_staleness_ms likely share a root cause (slow poll loop body)
   - A slow Bash tool execution and a high sdk_queue_depth in the same session are cause-and-effect
   - WAL buildup and slow sqlite3 reads are the same underlying issue
   - A session restart loop and cold start latency compound into the same user-visible problem
   Output a "related_groups" array where each group has a shared_root_cause and list of finding IDs.

2. **Triage each finding (or group) by complexity:**

   - 🟢 **Simple** — Isolated code change, <30 lines, no architectural impact. Examples:
     - Add an index to a sqlite3 query
     - Add LRU cache / memoization to a hot function
     - Add SO_REUSEADDR to prevent bind failures on restart
     - Increase WAL checkpoint frequency
     - Batch N individual queries into one
     - Add a timeout to a blocking call

   - 🟡 **Coordinated** — Multiple files change together, but the approach is clear. Examples:
     - Coalesce queue messages + add batch tracking across producer and consumer
     - Add connection pooling that touches the DB layer and all callers
     - Move a blocking I/O call to a thread pool, updating callers to await
     - Add a circuit breaker to an external service call + update health checks

   - 🔴 **Architectural** — Requires rethinking a design decision, needs a plan before code. Examples:
     - Moving from polling to event-driven (e.g., inotify/kqueue on chat.db)
     - Redesigning single-threaded session processing to be concurrent
     - Splitting a monolithic poll loop into separate async pipelines
     - Replacing synchronous message injection with an async queue

3. **For each finding, produce a fix proposal:**

   For 🟢 Simple fixes:
   - Exact file path
   - Exact function/method name (or line range)
   - Description of the exact change (what to add, remove, or modify)
   - Expected performance improvement (e.g., "reduces p95 from Xms to Yms")
   - Expected diff size estimate

   For 🟡 Coordinated fixes:
   - List ALL affected files with the change needed in each
   - Describe the coordination: what order to apply changes, what must stay consistent
   - Note any metrics that should improve and by how much
   - Identify tests that need updating or new benchmarks to add

   For 🔴 Architectural fixes:
   - Write up the design tradeoffs with latency impact estimates for each approach
   - Propose 2-3 approaches with effort estimates (hours/days)
   - Identify the entry points and blast radius of each approach
   - Recommend which approach to take and why
   - Note what can be done incrementally vs what requires a big-bang change
   - Include before/after latency projections for each approach

4. **Identify quick wins.** Flag any simple fix that would yield disproportionate latency improvement (e.g., a missing index causing 10x query slowdown). Mark these as "quick_win": true.

5. Return your fix proposals as JSON:
   {
     "related_groups": [
       {
         "group_id": "GRP-001",
         "shared_root_cause": "Description of the common root cause",
         "finding_ids": ["PERF1-001", "MSG1-002"]
       }
     ],
     "proposals": [
       {
         "proposal_id": "FIX-001",
         "finding_ids": ["PERF1-001"],
         "group_id": "GRP-001 or null if standalone",
         "title": "Short description of the optimization",
         "complexity": "simple|coordinated|architectural",
         "quick_win": false,
         "fix_plan": {
           "files": [
             {
               "path": "/absolute/path/to/file.py",
               "function": "function_name or null",
               "change": "Description of exact change"
             }
           ],
           "coordination_notes": "For coordinated: what order, what must stay consistent",
           "approaches": [
             {
               "name": "Approach name (architectural only)",
               "description": "What this approach entails",
               "effort": "2 hours | 1 day | 3 days",
               "latency_projection": "Expected p95 improvement",
               "tradeoffs": "Pros and cons"
             }
           ],
           "recommended_approach": "For architectural: which approach and why",
           "tests_to_update": ["paths to test files if applicable"],
           "estimated_diff_lines": 15,
           "expected_improvement": "Reduces p95 from X to Y for metric Z"
         },
         "side_effects": "Any known risks — could this hurt other metrics?",
         "prerequisites": "Other fixes or conditions that must be in place first",
         "rollback_plan": "How to detect if this fix makes things worse and revert"
       }
     ]
   }

Be specific and concrete. Every simple/coordinated fix must name exact files and functions. Vague proposals like "optimize the query" or "add caching" are not acceptable — say WHERE, WHAT, and HOW MUCH improvement is expected.
```

### Phase 4: Fix Refinement (Iterative Subagent Review)

Review each fix proposal using the **subagent-review pattern** (iterative review loop). Spawn a single review subagent with `subagent_type="general-purpose"` and `model="opus"`.

The reviewer iterates: **review → score → fix issues → re-review** until the proposal scores **9+/10** or **3 iterations** are reached (whichever comes first).

Prompt for the Fix Refinement subagent:

```
You are a performance fix proposal reviewer. You will review optimization proposals for latency bottlenecks found in a system scan. Your job is to ensure each proposal is sound, specific, and safe to deploy.

Original findings (for reference):
{ALL_ACCEPTED_AND_REFINED_FINDINGS_AS_JSON}

Fix proposals to review:
{FIX_PROPOSALS_JSON}

For EACH fix proposal, score it on these criteria (each 0-10):

1. **Root cause alignment** — Does the fix actually address the root cause of the latency, not just a symptom? For example, adding a cache in front of a slow query might mask the real issue (missing index).

2. **Complexity triage accuracy** — Is the complexity rating correct?
   - Is a "simple" fix truly isolated and <30 lines, or does it have hidden dependencies?
   - Is a "coordinated" fix actually architectural (requires rethinking a pipeline)?
   - Is an "architectural" proposal over-engineering what could be a simple index addition?
   - Are "quick_win" flags accurate? A quick win must be both simple AND high-impact.

3. **Side effect awareness** — Does the fix miss side effects?
   - Could it hurt other metrics? (e.g., aggressive caching reduces latency but increases memory)
   - Could it change correctness? (e.g., batching messages might reorder them)
   - Does the rollback plan make sense?

4. **Specificity** — Is it specific enough to implement right now?
   - Simple/coordinated: exact file paths, function names, precise change description?
   - Includes expected improvement numbers? (not just "faster" but "reduces p95 from 200ms to 50ms")
   - Architectural: well-defined approaches with entry points, effort estimates, latency projections?
   - "Optimize the poll loop" is NOT specific. "In manager.py:poll_messages(), replace per-message INSERT with executemany(), reducing p95 poll_cycle_ms from ~200ms to ~30ms" IS specific.

5. **Cross-reference correctness** — Related findings properly grouped? Would implementing one fix invalidate another? Prerequisites ordered correctly?

**Overall score** = average of the 5 criteria, rounded to nearest integer.

For each proposal:
- If score >= 9: PASS — proposal is ready
- If score < 9: REVISE — provide specific feedback, then output a revised proposal

After revising, re-score. Repeat until 9+ or 3 iterations.

Return your review as JSON:
{
  "reviews": [
    {
      "proposal_id": "FIX-001",
      "iteration": 1,
      "scores": {
        "root_cause_alignment": 8,
        "complexity_triage": 9,
        "side_effect_awareness": 7,
        "specificity": 6,
        "cross_reference": 9
      },
      "overall_score": 8,
      "verdict": "REVISE",
      "feedback": "Specificity: the fix says 'add caching' but doesn't specify cache size, TTL, or eviction policy. Expected improvement number is missing.",
      "revised_proposal": { ... same schema as original proposal, with issues fixed ... }
    }
  ],
  "final_proposals": [
    { ... each proposal in its final reviewed form ... }
  ]
}

Be rigorous. Hold every proposal to the standard: "Could someone implement this RIGHT NOW with only this description and access to the codebase, and be confident it won't regress other metrics?"
```

**Output of Phase 4** feeds into Phase 5 (Report). Each finding in the report includes its vetted fix proposal.

### Phase 5: Report

Collect all refinement results:
- Drop REFUTE verdicts (note them in a "Refuted" section for transparency)
- Group ACCEPT verdicts by severity: critical > high > medium > low
- List REFINE verdicts as "Needs Investigation"

**IMPORTANT report quality rules:**

1. **Explorer coverage**: For EACH of the 4 explorers, the report MUST either include findings from it or explicitly state "Explorer N ({name}): No anomalies detected." Do NOT silently drop an explorer's results.

2. **Explorer attribution**: Preserve the explorer source ID prefix (PERF1, SDK1, MSG1, SYS1) on each finding so readers know which data source produced it.

3. **Complete system summary**: The summary table MUST include ALL metrics found in today's perf data, plus yesterday's p95 as the baseline column. If a metric has no data, show "N/A" in its row. Never omit a metric.

4. **Consistent metric names**: Always use the exact metric names as they appear in the perf JSONL (e.g., `session_wake_latency_ms` not `session_wake_ms`). No abbreviations or variations.

5. **Specific file paths**: Every ACCEPT verdict MUST include at least one concrete file path (not "Tool configuration" or "session prompt"). If the refinement agent couldn't identify a specific file, it should be REFINE not ACCEPT.

6. **Specific fixes**: ACCEPT verdicts must propose fixes at the function level (e.g., "In `sdk_session.py:_run_loop()`, add a drain check before enqueuing"). "Investigate why X" is not an acceptable fix — that belongs in REFINE.

#### Report Format

```
Latency Scan Report — Dispatch System
Date: {date} | Window: last {hours}h
Explorers: 4 | Candidates: {N} | Accepted: {A} | Refuted: {R} | Needs Investigation: {I}

--- CRITICAL ---

1. [{source_id}] {title}
   Metric: {metric} | Current p95: {value} | Baseline: {baseline}
   Root cause: {root_cause}
   Impact: {impact}
   Fix proposal (FIX-{id}) [{complexity_emoji} {complexity}] — review score: {score}/10
     Files: {fix_files}
     Change: {fix_description}
     Expected improvement: {expected_improvement}
     Side effects: {side_effects}

--- HIGH ---
...

--- MEDIUM ---
...

--- LOW ---
...

--- EXPLORER COVERAGE ---
- Explorer 1 (Perf JSONL): {N} candidates | {summary}
- Explorer 2 (SDK Events): {N} candidates | {summary}
- Explorer 3 (Message Delivery): {N} candidates | {summary or "No anomalies detected"}
- Explorer 4 (System Resources): {N} candidates | {summary}

--- NEEDS INVESTIGATION ---
- [{source_id}] {title} — {reason}

--- REFUTED ({R} total) ---
- [{source_id}] {title} — {reason}

--- SYSTEM SUMMARY ---
| Metric | p50 | p95 | p99 | Max | Count | Yesterday p95 | Status |
|--------|-----|-----|-----|-----|-------|---------------|--------|
| (all metrics found in today's perf data, one row each) |
| (include tool_execution as an aggregate row) |
```

**Note:** The system summary MUST include ALL metrics found in today's perf data PLUS `tool_execution` as an aggregate row. The "Yesterday p95" column serves as the dynamic baseline. If yesterday's data is unavailable, show "N/A". Status is determined by comparing today's p95 to yesterday's p95: OK (< 2x), WARN (2-3x), CRIT (> 3x). For `wal_checkpoint_status`, any non-zero value is CRIT.

#### Output Modes

**Interactive mode** (default): Print the report to the conversation.

**Nightly mode** (prompt contains "--nightly"): Send the full report via SMS to admin using the reply CLI or send-sms with the admin's phone looked up from config. Do NOT truncate or summarize — the admin needs full details.

Only send if there are ACCEPT or REFINE verdicts. If everything was refuted or within baselines, skip the SMS and log "latency-finder nightly: clean scan, all metrics within baselines."

**CI mode** (prompt contains "--ci"): Exit with non-zero status if any critical or high bottlenecks are accepted. Print report to stdout.

#### Persist Results to Bus

**After generating the report (in ALL modes)**, publish a `scan.completed` event to the bus for historical tracking:

```bash
RUN_ID=$(date +%Y%m%d-%H%M)
~/dispatch/bus/cli.py produce system \
  '{"scanner":"latency-finder","run_id":"'"$RUN_ID"'","mode":"nightly","duration_seconds":DURATION,"summary":{"candidates":N,"accepted":A,"refuted":R,"needs_investigation":I},"findings":[ACCEPTED_AND_REFINED_FINDINGS_AS_JSON]}' \
  --type scan.completed --source latency-finder --key "scan-latency-finder-$RUN_ID"
```

The `findings` array should include all ACCEPT and REFINE verdicts with their full details (id, title, severity, category, metric, current_p95, baseline_p95, root_cause, fix, etc.). Refuted items go in `summary.refuted` count only, not in findings.

This enables `bus reports --scanner latency-finder` to query historical scan results (stored in archive indefinitely).

## Graceful Degradation

- **DuckDB not installed** — Fall back to jq/awk pipelines for perf JSONL analysis. Note the degradation.
- **No perf JSONL for today** — Fall back to yesterday's file, or most recent available. Note the data gap.
- **Empty sdk_events** — Explorer 2 reports "no sdk_events data available" and skips. Other explorers continue.
- **bus.db locked/missing** — Skip bus-dependent explorers, continue with perf JSONL analysis.
- **Explorer subagent fails** — Continue with results from other explorers. Note the failure.
- **Refinement subagent fails** — Note the failure, don't count as accepted or refuted.
- **Zero candidates** — Report "clean scan, no regressions detected vs yesterday" (good outcome).
- **Too many candidates (>15)** — Refine top 15 by severity+confidence, list the rest as "unreviewed."

## Calibration

### GOOD latency candidates (report these):
- Any metric with p95 2x+ higher than yesterday's p95 — regression
- Any metric with p95 3x+ higher than yesterday's p95 — critical regression
- Bash tool executions significantly slower than historical average for that skill
- Specific session consuming 80% of total tool execution time — resource hog
- Inter-tool gaps significantly longer than the session's historical pattern
- wal_checkpoint_status != 0 repeatedly — SQLite WAL issues causing slow reads
- One backend (Signal vs iMessage) responding 3x slower than the other — backend-specific bottleneck
- Service restart loops (health check showing repeated restarts)
- sdk_queue_depth trending upward over time (compare hour-by-hour)

### BAD candidates (do NOT report):
- Single transient spike during daemon startup — expected
- One-off slow tool execution for a clearly large operation — expected
- p50 values slightly above yesterday but p95 is stable — not impactful
- Long gap between messages during quiet hours (2am-8am) — idle period, not latency
- One-off API latency spike (Gemini, etc.) — external service variance
- Metrics that are stable day-over-day even if they seem "high" in absolute terms — that's the system's normal

## Examples

```bash
# Scan for performance bottlenecks
"find bottlenecks"
"what's slow"

# Nightly automated scan
"latency scan --nightly"

# Scan with longer time window
"latency scan --since 48h"

# After deploying a change
"check if performance regressed"

# CI check
"latency scan --ci"
```

## Historical Context (Required)

Before generating findings, you MUST gather historical context to avoid re-reporting known issues or missing important changes:

### Past Reports

Query previous scan reports from the bus for what was already found, fixed, or refuted:

```bash
cd ~/dispatch && uv run python -m bus.cli reports --scanner latency-finder --since 7
```

This shows past findings with their verdicts (accepted/refuted). Use this to:
- Skip issues that were already reported and are being tracked
- Understand patterns in what gets refuted (calibrate your signal)
- Build on previous findings rather than starting from scratch

### Recent Code Changes

Check git history for recent changes that provide context on what was built, fixed, or refactored:

```bash
cd ~/dispatch && git log --oneline --since="7 days ago" -- assistant/ bus/
cd ~/dispatch && git log --oneline --since="7 days ago" -- ~/.claude/skills/
```

This helps you:
- Understand WHY metrics changed (a refactor may cause temporary regressions)
- Identify fixes that were already landed (don't report fixed bugs)
- See what the admin was working on (provides intent behind changes)
