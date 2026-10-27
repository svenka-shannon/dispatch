---
name: bug-finder
description: Find bugs in codebases using parallel discovery and refinement subagents. Scans recent code changes, system health, test failures, and consistency issues. Trigger words - find bugs, bug finder, bug scan, what's broken, check for bugs.
---

# Bug Finder

Automatically discover bugs in any codebase using a 3-phase architecture: parallel discovery explorers, parallel refinement reviewers, and a compiled report.

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

- User says "find bugs", "what's broken", "check for bugs", "bug scan"
- Nightly cron via "--nightly" flag
- After a large merge or refactor to catch regressions

## How It Works

### Phase 1: Discovery (Parallel Explore Subagents)

Launch **4 parallel Explore subagents** simultaneously. Each focuses on a different bug surface area. All run with `subagent_type="Explore"` (fast, read-only) and `model="opus"`.

**IMPORTANT: All subagents (both discovery and refinement) MUST use `model: "opus"`.** Never use sonnet or haiku for bug finding.

**Scope:** The entire codebase is in scope for bugs, not just recent changes. Recent changes (last 24h) are the **primary signal** for where to focus exploration first, but explorers should also look at surrounding code, callers, integration points, and anything else that seems suspicious.

**Admin transcript context:** When targeting `~/dispatch/`, Explorer 1 (Recent Code Changes) MUST also read recent admin transcripts to understand the context of changes — what was being built, what problems were being solved, and what the admin's intent was. This prevents flagging intentional changes as bugs.
- First, look up the admin contact: run `~/.claude/skills/contacts/scripts/contacts list --tier admin` to get the admin's phone number dynamically.
- Read: `~/.claude/skills/sms-assistant/scripts/read-sms --chat "<admin_phone>" --limit 40` (replacing `<admin_phone>` with the number from the contacts lookup)
- This gives the last 40 messages from the admin's 1:1 chat, which contains discussion of recent changes, bug reports, and feature requests.
- Use this context to: (a) understand WHY code changed, (b) identify bugs the admin already knows about vs new ones, (c) find issues mentioned in conversation but not yet fixed.

**Determine the target directory first:**
- If the user specifies a project/directory, use that
- If running from a git repo, use the repo root (`git rev-parse --show-toplevel`)
- Otherwise, default to `~/dispatch/` (the main system)

#### Explorer 1: Recent Code Changes

Prompt for the Explore subagent:

```
You are a bug discovery agent. Analyze {TARGET_DIR} for potential bugs, with recent code changes as your starting focus.

1. Find what changed recently (your PRIMARY focus area):
   git -C {TARGET_DIR} log --oneline --since="24 hours ago" 2>/dev/null
   git -C {TARGET_DIR} diff HEAD~5 --stat 2>/dev/null
   git -C {TARGET_DIR} diff HEAD~5 2>/dev/null

   If HEAD is old (`git log -1 --format=%cd`), most recent work is probably UNCOMMITTED. In that case use
   `git -C {TARGET_DIR} diff --stat` (working tree vs HEAD), `git status --porcelain | grep '^??'`,
   and file mtimes (`find ... -mtime -7`) as the change signal instead of `git log`.

   If not a git repo, find recently modified files:
   find {TARGET_DIR} -name "*.py" -o -name "*.js" -o -name "*.ts" -o -name "*.sh" -mtime -1

2. For each changed file, read the FULL file (not just the diff). Also read callers and related files. Bugs can be ANYWHERE — not just in changed lines. Recent changes are your starting point but follow the code wherever it leads. Look for:
   - Logic errors (wrong conditions, off-by-one, inverted boolean logic)
   - Missing error handling (unhandled exceptions, missing null checks, no try/catch around I/O)
   - Edge cases (empty arrays, None/null values, empty strings, concurrent access)
   - Race conditions (TOCTOU, shared mutable state without locks)
   - Resource leaks (unclosed files, sockets, database connections)
   - Security issues (unsanitized input, hardcoded secrets, path traversal)
   - Regressions (deleted code that was needed, changed behavior without updating callers)
   - Documentation bugs in SKILL.md, CLAUDE.md, README.md (wrong commands, stale paths, incorrect instructions that would mislead future Claude sessions)
   - Markdown/config drift (SKILL.md describes behavior that the code doesn't match, or vice versa)

3. Return a JSON array of bug candidates:
   [
     {
       "id": "DISC1-001",
       "title": "Short description of the bug",
       "file": "absolute/path/to/file.py",
       "line_range": "42-58",
       "category": "logic_error|missing_error_handling|edge_case|race_condition|resource_leak|security|regression",
       "evidence": "The code does X but should do Y because Z",
       "severity_guess": "critical|high|medium|low",
       "confidence": "high|medium|low"
     }
   ]

Be thorough but precise. Only report things that are genuinely likely to cause incorrect behavior, crashes, or data loss. Do NOT flag style issues, missing comments, or theoretical concerns that require exotic conditions.
```

#### Explorer 2: System Health (Dispatch-specific)

**Only run this explorer when targeting ~/dispatch/ or the dispatch system.** Skip for other projects.

Prompt for the Explore subagent:

```
You are a system health bug discovery agent for the Dispatch personal assistant.

1. Run the debug CLI to check system health:
   debug status 2>/dev/null
   debug incident --since 24h 2>/dev/null

2. Check recent logs for errors:
   tail -100 ~/dispatch/logs/manager.log 2>/dev/null | grep -i "error\|exception\|traceback\|failed"
   # Under launchd the daemon logs to stderr → ~/dispatch/logs/launchd.log (huge — grep/tail only).
   # Check which file is actually live (newest mtime) before trusting either:
   ls -la ~/dispatch/logs/manager.log ~/dispatch/logs/launchd.log
   tail -2000 ~/dispatch/logs/launchd.log | grep -aiE "error|exception|traceback|failed"
   tail -50 ~/dispatch/logs/session_lifecycle.log 2>/dev/null
   tail -50 ~/dispatch/logs/watchdog.log 2>/dev/null

3. Check for crashed or unhealthy sessions:
   cat ~/dispatch/state/sessions.json 2>/dev/null

4. **Deep bus analysis** — Reference the `/bus` skill (`~/dispatch/skills/bus/SKILL.md`) for comprehensive query patterns, schema docs, and anomaly detection. Run ALL anomaly detection queries from that skill (sections 1-7 and 10-11). Key queries to ALWAYS run:

5. Check bus for failed events:
   sqlite3 ~/dispatch/state/bus.db "SELECT datetime(timestamp/1000,'unixepoch','localtime'), type, key, payload FROM records WHERE type LIKE '%failed%' OR type LIKE '%crashed%' OR type LIKE '%error%' ORDER BY timestamp DESC LIMIT 20" 2>/dev/null

5. **Deep bus pattern analysis** — query for anomalous event patterns that indicate bugs:

   a. Source volume anomalies (retry loops, spam, runaway producers):
   sqlite3 ~/dispatch/state/bus.db "SELECT source, type, COUNT(*) as cnt FROM records GROUP BY source, type ORDER BY cnt DESC LIMIT 20" 2>/dev/null

   b. Retry/consumer-retry spikes (indicates processing failures):
   sqlite3 ~/dispatch/state/bus.db "SELECT source, type, COUNT(*) as cnt, MIN(datetime(timestamp/1000,'unixepoch','localtime')) as first_seen, MAX(datetime(timestamp/1000,'unixepoch','localtime')) as last_seen FROM records WHERE source LIKE '%retry%' OR source LIKE '%dlq%' GROUP BY source, type ORDER BY cnt DESC" 2>/dev/null

   c. Event rate spikes per 15-min bucket (find bursts):
   sqlite3 ~/dispatch/state/bus.db "SELECT datetime((timestamp/900000)*900, 'unixepoch', 'localtime') as bucket, source, COUNT(*) as cnt FROM records GROUP BY bucket, source HAVING cnt > 50 ORDER BY cnt DESC LIMIT 20" 2>/dev/null

   d. Error payloads from retries (what errors caused the retries):
   sqlite3 ~/dispatch/state/bus.db "SELECT substr(payload, 1, 500), COUNT(*) as cnt FROM records WHERE source LIKE '%retry%' GROUP BY substr(payload, 1, 200) ORDER BY cnt DESC LIMIT 10" 2>/dev/null

   e. SDK event errors (tool failures):
   sqlite3 ~/dispatch/state/bus.db "SELECT tool_name, event_type, COUNT(*) as cnt, AVG(duration_ms) as avg_ms FROM sdk_events WHERE is_error=1 GROUP BY tool_name, event_type ORDER BY cnt DESC LIMIT 15" 2>/dev/null

   f. Disproportionate source ratios (one source >> all others suggests a bug):
   sqlite3 ~/dispatch/state/bus.db "SELECT source, COUNT(*) as cnt, ROUND(100.0 * COUNT(*) / (SELECT COUNT(*) FROM records), 1) as pct FROM records GROUP BY source ORDER BY cnt DESC LIMIT 15" 2>/dev/null

6. Look for patterns that indicate bugs:
   - Sessions that keep restarting (restart loops)
   - Messages that were received but never responded to
   - Heartbeat gaps (session went silent)
   - Error counts spiking
   - Memory/resource issues
   - Consumer-retry or DLQ events (indicates message processing failures — dig into payloads to find the root cause error)
   - Any single source producing >30% of all events is suspicious
   - Burst patterns: >100 events from one source in a 15-min window

6. Return a JSON array of bug candidates (same format as Explorer 1, but category can also be "system_health" or "operational"):
   [
     {
       "id": "DISC2-001",
       "title": "Short description",
       "file": "relevant file or 'system'",
       "line_range": "N/A for operational issues",
       "category": "system_health|operational|crash_loop|message_loss",
       "evidence": "What the logs/data show",
       "severity_guess": "critical|high|medium|low",
       "confidence": "high|medium|low"
     }
   ]
```

#### Explorer 3: Test Failures

Prompt for the Explore subagent:

```
You are a test coverage and failure discovery agent. Check {TARGET_DIR} for test issues.

1. Find test files:
   find {TARGET_DIR} -name "test_*.py" -o -name "*_test.py" -o -name "*.test.js" -o -name "*.test.ts" -o -name "*.spec.js" -o -name "*.spec.ts" 2>/dev/null | head -50

2. Find recently changed source files:
   git -C {TARGET_DIR} diff HEAD~5 --name-only 2>/dev/null | grep -v test

3. Check if changed files have corresponding tests. Flag files with no test coverage.

4. Try to run tests if a test runner is configured:
   - Python: look for pytest.ini, setup.cfg, pyproject.toml with [tool.pytest]. Run: cd {TARGET_DIR} && uv run pytest --tb=short -q 2>&1 | tail -50
   - Node: look for package.json with test script. Run: cd {TARGET_DIR} && npm test 2>&1 | tail -50
   - Shell: look for test scripts. Run them.
   Only run tests if a test framework is clearly configured. Do NOT install dependencies.

5. Return a JSON array of bug candidates:
   [
     {
       "id": "DISC3-001",
       "title": "Test failure or coverage gap",
       "file": "path to test or source file",
       "line_range": "if applicable",
       "category": "test_failure|missing_test_coverage",
       "evidence": "Test output or description of coverage gap",
       "severity_guess": "high|medium|low",
       "confidence": "high|medium|low"
     }
   ]

Prioritize actual test failures over missing coverage. A failing test is a confirmed bug signal.
```

#### Explorer 4: Consistency Issues

Prompt for the Explore subagent:

```
You are a consistency checker. Look for structural and reference integrity issues in {TARGET_DIR}.

1. Check for stale references and broken imports:
   - Read key entry points and check that imported modules/files exist
   - Look for references to files/paths that don't exist
   - Check for TODO/FIXME/HACK/XXX comments that indicate known issues:
     grep -rn "TODO\|FIXME\|HACK\|XXX\|BROKEN\|WORKAROUND" {TARGET_DIR} --include="*.py" --include="*.js" --include="*.ts" --include="*.sh" 2>/dev/null | head -30

2. **Check markdown docs for bugs** (SKILL.md, CLAUDE.md, README.md, etc.):
   - Wrong commands or CLI usage examples (would fail if a future Claude session runs them)
   - Stale file paths that no longer exist
   - Instructions that contradict the actual code behavior
   - Incorrect descriptions of system architecture or data flow
   - Missing or outdated env vars, config keys, or API endpoints
   - These are REAL bugs — wrong docs mislead future Claude sessions and cause cascading failures

3. If this is ~/.claude/skills/ or dispatch:
   - Check that every SKILL.md has valid YAML frontmatter (--- delimited block with name and description)
   - Check that scripts referenced in SKILL.md files actually exist
   - Check that scripts are executable: find {TARGET_DIR} -name "*.sh" -o -name "*.py" | head -20, then check permissions

3. Check for configuration drift:
   - Are there .env.example files with keys not in .env?
   - Are there config files referencing paths/hosts that look stale?
   - JSON/YAML syntax errors in config files

4. Check for dead code indicators:
   - Files not imported/referenced anywhere
   - Functions defined but never called (check a few key files, don't exhaustively scan everything)

5. Return a JSON array of bug candidates:
   [
     {
       "id": "DISC4-001",
       "title": "Description of consistency issue",
       "file": "path to file",
       "line_range": "if applicable",
       "category": "broken_import|stale_reference|missing_file|config_drift|dead_code",
       "evidence": "What's inconsistent and why it matters",
       "severity_guess": "critical|high|medium|low",
       "confidence": "high|medium|low"
     }
   ]

Focus on issues that will cause runtime failures. A missing import that gets hit in production is critical. A TODO comment is informational.
```

### Phase 2: Refinement (Parallel Subagents — One Per Bug Candidate)

Collect all bug candidates from the 4 explorers. Deduplicate by file+line_range (if two explorers found the same issue, merge them and keep the higher confidence version).

For each unique bug candidate, spawn a **refinement subagent** in parallel. Use `subagent_type="general-purpose"` and `model="opus"` (these may need to run code or tests).

**Cap at 15 refinement subagents.** If more than 15 candidates, prioritize by: severity_guess (critical first), then confidence (high first), then drop the rest with a note. Closely related candidates (same file or same root cause, e.g. several doc bugs or several stale tests) can share one refinement subagent that returns one verdict per ID. That lets more candidates fit under the cap.

**Passing results between phases:** subagents may be blocked from writing files, so don't rely on a subagent to save its JSON for the next phase. As each refinement verdict arrives, append a condensed line (ID, verdict, severity, file:lines, root cause, fix) to `/tmp/bugfinder-<run>/verdicts.md` yourself. Write the Phase 3 proposals to a file yourself as well, and point the Phase 3/4 agents at those files.

**Refinement/fix subagents are READ-ONLY.** Say so explicitly in every prompt: no edits, commits, restarts, or sends. Note that GNU `timeout` isn't installed on this machine.

Prompt for each refinement subagent:

```
You are a bug verification agent. Investigate this bug candidate and determine if it's real.

Bug candidate:
- ID: {id}
- Title: {title}
- File: {file}
- Lines: {line_range}
- Category: {category}
- Evidence from discovery: {evidence}
- Severity guess: {severity_guess}

Your job:
1. Read the relevant file(s) — the full file, not just the reported lines. Understand the context.

2. Try to determine if this is a real bug:
   - Trace the code path. Does the reported issue actually trigger in practice?
   - Check if there's error handling elsewhere that catches this case
   - Check if there's a test that covers this case
   - If possible, run the code or test to reproduce the issue:
     - For Python: uv run python -c "..." or uv run pytest path/to/test.py::specific_test
     - For Node: node -e "..." or npm test -- --grep "pattern"
     - For shell: run the script with safe test inputs

3. Make a verdict:
   - **ACCEPT**: This is a confirmed or highly likely bug. Explain the root cause, impact, and suggested fix.
   - **REFINE**: This needs more investigation — the code is suspicious but I can't confirm. Explain what's unclear.
   - **REFUTE**: This is NOT a bug. Explain why (e.g., handled elsewhere, intentional behavior, unreachable code path).

4. Return your verdict as JSON:
   {
     "id": "{id}",
     "verdict": "ACCEPT|REFINE|REFUTE",
     "title": "Final title (may be updated from discovery)",
     "severity": "critical|high|medium|low",
     "category": "{category}",
     "file": "{file}",
     "line_range": "{line_range}",
     "root_cause": "Why this bug exists (ACCEPT only)",
     "impact": "What goes wrong when this bug triggers (ACCEPT only)",
     "reproduction": "Steps or command to reproduce (ACCEPT only, if possible)",
     "suggested_fix": "How to fix it (ACCEPT only)",
     "affected_files": ["list", "of", "files"],
     "reason": "Why this verdict (especially important for REFUTE)"
   }

Be rigorous. A bug must be something that causes incorrect behavior, crashes, data loss, or security issues under realistic conditions. Do not accept style issues, theoretical concerns requiring exotic conditions, or intentional tradeoffs documented in code comments.

**IMPORTANT: When fixing dead code or unused files/functions, recommend DELETION not deprecation.** Just delete the dead code — don't add deprecation warnings or keep it around "for reference." Dead code is noise.
```

### Phase 3: Fix Proposals (Single Subagent)

After refinement, spawn a **single Fix Proposal subagent** to analyze all accepted findings holistically. This agent needs visibility across ALL findings to cross-reference shared root causes and avoid proposing conflicting fixes. Use `subagent_type="general-purpose"` and `model="opus"`.

**This phase is PLAN ONLY — no code changes are made.**

Prompt for the Fix Proposal subagent:

```
You are a fix planning agent. You have been given all accepted and needs-investigation bug findings from a codebase scan. Your job is to propose concrete fix plans for each finding WITHOUT implementing any changes.

Accepted findings (ACCEPT and REFINE verdicts):
{ALL_ACCEPTED_AND_REFINED_FINDINGS_AS_JSON}

Your job:

1. **Cross-reference related findings.** Group bugs that share a root cause or affect the same subsystem. For example:
   - Two race conditions in the same module likely need a single locking strategy, not two independent fixes
   - A missing error handler and a resource leak in the same code path should be fixed together
   - A doc bug in SKILL.md and a code bug it documents are related — fix both or neither
   Output a "related_groups" array where each group has a shared_root_cause and list of finding IDs.

2. **Triage each finding (or group) by complexity:**

   - 🟢 **Simple** — Isolated code change, <30 lines, no architectural impact. Examples:
     - Add a missing null check
     - Fix an inverted boolean condition
     - Add SO_REUSEADDR to a socket
     - Add LRU cache to a hot function
     - Fix a wrong command in SKILL.md or CLAUDE.md
     - Delete dead code or unused imports

   - 🟡 **Coordinated** — Multiple files change together, but the approach is clear. Examples:
     - Add error handling across 3 callers of the same function
     - Rename a moved file and update all references
     - Add a lock to shared state accessed from multiple modules
     - Update SKILL.md + code + tests together for a behavior change

   - 🔴 **Architectural** — Requires rethinking a design decision, needs a plan before code. Examples:
     - Changing from polling to event-driven
     - Restructuring session lifecycle management
     - Replacing a synchronous pipeline with async
     - Redesigning a data model that multiple subsystems depend on

3. **For each finding, produce a fix proposal:**

   For 🟢 Simple fixes:
   - Exact file path
   - Exact function/method name (or line range)
   - Description of the exact change (what to add, remove, or modify)
   - Expected diff size estimate

   For 🟡 Coordinated fixes:
   - List ALL affected files with the change needed in each
   - Describe the coordination: what order to apply changes, what must stay consistent
   - Note any tests that need updating
   - If this is a doc bug + code bug pair, specify both fixes

   For 🔴 Architectural fixes:
   - Write up the design tradeoffs
   - Propose 2-3 approaches with effort estimates (hours/days)
   - Identify the entry points and blast radius of each approach
   - Recommend which approach to take and why
   - Note what can be done incrementally vs what requires a big-bang change

4. **Identify doc-only bugs separately.** For findings where the bug is in SKILL.md, CLAUDE.md, or other markdown docs (wrong commands, stale paths, incorrect instructions), flag them as "doc_fix": true. These are often quick wins that prevent future Claude sessions from being misled.

5. Return your fix proposals as JSON:
   {
     "related_groups": [
       {
         "group_id": "GRP-001",
         "shared_root_cause": "Description of the common root cause",
         "finding_ids": ["DISC1-001", "DISC4-003"]
       }
     ],
     "proposals": [
       {
         "proposal_id": "FIX-001",
         "finding_ids": ["DISC1-001"],
         "group_id": "GRP-001 or null if standalone",
         "title": "Short description of the fix",
         "complexity": "simple|coordinated|architectural",
         "doc_fix": false,
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
               "tradeoffs": "Pros and cons"
             }
           ],
           "recommended_approach": "For architectural: which approach and why",
           "tests_to_update": ["paths to test files if applicable"],
           "estimated_diff_lines": 15
         },
         "side_effects": "Any known risks or things that could break",
         "prerequisites": "Other fixes or conditions that must be in place first"
       }
     ]
   }

Be specific and concrete. Every simple/coordinated fix must name exact files and functions. Vague proposals like "improve error handling" or "add tests" are not acceptable — say WHERE and WHAT.
```

### Phase 4: Fix Refinement (Iterative Subagent Review)

Review each fix proposal using the **subagent-review pattern** (iterative review loop). Spawn a single review subagent with `subagent_type="general-purpose"` and `model="opus"`.

The reviewer iterates: **review → score → fix issues → re-review** until the proposal scores **9+/10** or **3 iterations** are reached (whichever comes first).

Prompt for the Fix Refinement subagent:

```
You are a fix proposal reviewer. You will review fix proposals for bugs found in a codebase scan. Your job is to ensure each proposal is sound, specific, and safe to implement.

Original findings (for reference):
{ALL_ACCEPTED_AND_REFINED_FINDINGS_AS_JSON}

Fix proposals to review:
{FIX_PROPOSALS_JSON}

For EACH fix proposal, score it on these criteria (each 0-10):

1. **Root cause alignment** — Does the fix actually address the root cause of the bug, not just a symptom? Would the bug recur under different conditions if only this fix is applied?

2. **Complexity triage accuracy** — Is the complexity rating correct?
   - Is a "simple" fix truly isolated and <30 lines, or does it have hidden dependencies?
   - Is a "coordinated" fix actually architectural (requires design rethinking)?
   - Is an "architectural" proposal over-engineering what could be a simple fix?

3. **Side effect awareness** — Does the fix miss side effects?
   - Could it break callers or downstream consumers?
   - Does it change behavior that other code depends on?
   - For doc fixes: does the doc fix match what the code actually does (not what it should do)?

4. **Specificity** — Is it specific enough to implement right now?
   - Simple/coordinated: Does it name exact file paths, function names, and describe the precise change?
   - Architectural: Are the approaches well-defined with clear entry points and effort estimates?
   - "Add better error handling" is NOT specific. "In manager.py:poll_messages(), wrap the sqlite3.execute() call on line 142 in a try/except OperationalError that logs and retries once" IS specific.

5. **Cross-reference correctness** — Are related findings properly grouped? Are there ungrouped findings that share a root cause? Would implementing one fix invalidate another?

**Overall score** = average of the 5 criteria, rounded to nearest integer.

For each proposal:
- If score >= 9: PASS — proposal is ready
- If score < 9: REVISE — provide specific feedback on what to fix, then output a revised proposal with the issues addressed

After revising, re-score the revised proposal. Repeat until 9+ or 3 iterations.

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
      "feedback": "Specificity: the fix says 'add null check' but doesn't specify which variable or what the fallback behavior should be.",
      "revised_proposal": { ... same schema as original proposal, with issues fixed ... }
    }
  ],
  "final_proposals": [
    { ... each proposal in its final reviewed form ... }
  ]
}

Be rigorous. Hold every proposal to the standard: "Could someone implement this RIGHT NOW with only this description and access to the codebase?"
```

**Output of Phase 4** feeds into Phase 5 (Report). Each finding in the report includes its vetted fix proposal.

### Phase 5: Report

Collect all refinement results:
- Drop REFUTE verdicts (note them in a "Refuted" section for transparency)
- Group ACCEPT verdicts by severity: critical > high > medium > low
- List REFINE verdicts separately as "Needs Investigation"

#### Report Format

```
## Bug Scan Report — {TARGET_DIR}
Date: {date}
Explorers: 4 | Candidates: {N} | Accepted: {A} | Refuted: {R} | Needs Investigation: {I}

### Critical

1. **{title}** — {file}:{line_range}
   Root cause: {root_cause}
   Impact: {impact}
   Reproduction: {reproduction}
   Fix proposal (FIX-{id}) [{complexity_emoji} {complexity}] — review score: {score}/10
     Files: {fix_files}
     Change: {fix_description}
     Side effects: {side_effects}
   Affected files: {affected_files}

### High
...

### Medium
...

### Low
...

### Needs Investigation
- {title} — {file}: {reason for REFINE verdict}

### Refuted ({R} total)
- {title} — {reason}
```

#### Output Modes

**Interactive mode** (default): Print the report to the conversation.

**Nightly mode** (prompt contains "--nightly"): Deliver the FULL report to the admin as an attachment, plus one short text. The sms-assistant length guard (max 4 lines / 220 chars of plain text, see sms-assistant SKILL.md "Message shape") rules out pasting a multi-page report into a text, and splitting it trips the burst guard. Attachments are exempt. So:

1. Write the full, untruncated report to `~/Documents/bug-reports/bug-scan-<YYYY-MM-DD>.md`.
2. Convert it to PDF: `~/.claude/skills/md2pdf/scripts/convert.py ~/Documents/bug-reports/bug-scan-<date>.md`. Check page 1 with the Read tool.
3. Send ONE text of 220 chars or fewer (count, time-sensitive items first) with the PDF attached:

```bash
# Get admin chat ID (phone or email)
ADMIN_CHAT_ID=$(~/.claude/skills/contacts/scripts/contacts list --tier admin | head -1 | awk -F'|' '{gsub(/ /,"",$2); print "+1"$2}')
~/.claude/skills/sms-assistant/scripts/send-sms "$ADMIN_CHAT_ID" "<=220-char summary, urgent items first" --file ~/Documents/bug-reports/bug-scan-<date>.pdf
```

(`contacts list` prints `Name | 5551234567 | admin`, so `awk '{print $NF}'` returns the tier, not the number. Parse the middle column and check the result looks like a phone number before sending.)

Put a "Do today / this week" section at the top of the report for anything with a deadline: live deliveries, reminders that fire soon, unbacked-up work.

Only send if there are ACCEPT or REFINE verdicts. If everything was refuted, skip the SMS and log "bug-finder nightly: clean scan, no bugs found."

**CI mode** (prompt contains "--ci"): Exit with non-zero status if any critical or high bugs are accepted. Print report to stdout.

#### Persist Results to Bus

**After generating the report (in ALL modes)**, publish a `scan.completed` event to the bus for historical tracking:

```bash
RUN_ID=$(date +%Y%m%d-%H%M)
~/dispatch/bus/cli.py produce system \
  '{"scanner":"bug-finder","run_id":"'"$RUN_ID"'","mode":"nightly","duration_seconds":DURATION,"summary":{"candidates":N,"accepted":A,"refuted":R,"needs_investigation":I},"findings":[ACCEPTED_AND_REFINED_FINDINGS_AS_JSON]}' \
  --type scan.completed --source bug-finder --key "scan-bug-finder-$RUN_ID"
```

The `findings` array should include all ACCEPT and REFINE verdicts with their full details (id, title, severity, category, file, root_cause, suggested_fix, etc.). Refuted items go in `summary.refuted` count only, not in findings.

This enables `bus reports --scanner bug-finder` to query historical scan results (stored in archive indefinitely).

## Graceful Degradation

- **Not a git repo** — Explorer 1 falls back to `find` for recent files, Explorer 3 skips "changed files" coverage check
- **No test framework** — Explorer 3 reports "no test framework found" and only checks for test file existence
- **debug CLI not available** — Explorer 2 skips (it's dispatch-specific anyway)
- **Explorer subagent fails** — Continue with results from other explorers. Note the failure in the report.
- **Refinement subagent fails** — Note the failure, don't count as accepted or refuted
- **Zero candidates** — Report "clean scan, no bug candidates found" (this is a good outcome)
- **Too many candidates (>15)** — Refine top 15 by severity+confidence, list the rest as "unreviewed" in report

## Calibration

### GOOD bug candidates (report these):
- `if x > 0` when it should be `if x >= 0` — logic error, will cause off-by-one
- File opened but never closed in error path — resource leak
- `except Exception: pass` silently swallowing errors — missing error handling
- Import references a module that was renamed/moved — broken import
- Test expects value A but code now returns value B — test failure = confirmed bug
- Session restart loop detected in logs — operational bug
- SKILL.md says "run `foo --bar`" but the script doesn't accept `--bar` flag — doc bug that misleads future sessions
- CLAUDE.md references a file path that was moved/deleted — stale reference causes errors
- SKILL.md describes a 3-step process but code actually does 4 steps — drift between docs and implementation

### BAD candidates (do NOT report):
- "This function doesn't have type hints" — style, not a bug
- "This variable name is unclear" — style
- "This could theoretically overflow if given 2^64 items" — unrealistic edge case
- "No test for this utility function" — low-value coverage gap
- "This TODO says 'fix later'" — known issue, not a discovery
- "This function is long" — complexity, not a bug

## Examples

```bash
# Scan dispatch system for bugs
"find bugs in ~/dispatch"

# Scan a specific project
"find bugs in ~/code/my-project"

# Nightly automated scan
"find bugs --nightly"

# After a big refactor
"find bugs in the recent changes to ~/dispatch/assistant"
```

## Historical Context (Required)

Before generating findings, you MUST gather historical context to avoid re-reporting known issues or missing important changes:

### Past Reports

Query previous scan reports from the bus for what was already found, fixed, or refuted:

```bash
cd ~/dispatch && uv run python -m bus.cli reports --scanner bug-finder --since 7
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
- Understand WHY code changed (a refactor may cause temporary regressions)
- Identify fixes that were already landed (don't report fixed bugs)
- See what the admin was working on (provides intent behind changes)
