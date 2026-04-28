---
title: Session-Initiated Background Investigations
version: 1
created: '2026-04-28T11:59:26.483597'
chat_id: null
contact: null
backend: null
status: active
tags: [self-heal, ephemeral-tasks, sdk-sessions, infrastructure]
implementation_path: null
depends_on: []
last_review_score: 5.6
---

## Goal

Let SDK contact sessions dispatch fresh admin-tier ephemeral sessions to **triage novel failures autonomously**, with structured results injected back into the originating session — so the assistant can self-heal problems it has never seen before instead of texting the admin for manual debugging.

**Concrete trigger:** today's chrome-control incident, where the agent texted the admin "service worker hung again" instead of running `ps`, sampling the runaway native_host PID, reading the source, and proposing a kill. A Claude Code session (this one) had the tools to do that triage; SDK contact sessions don't have an ergonomic equivalent.

**Success criteria:**
1. From any SDK session, `claude-assistant dispatch-investigation "<prompt>"` returns a `task_id` immediately and queues a background investigator.
2. The investigator runs to completion (or timeout/cancel), producing a structured result block.
3. The result is injected back into the originating session, ready to be acted on next turn.
4. The investigator cannot mutate state, send messages, or dispatch its own investigations unless `--allow-mutations` was passed.
5. End-to-end audit trail in bus.db plus a transcript at `~/transcripts/investigations/<task_id>/`.

## Steps

### Phase 1 — Plumbing (F1–F4, structured per `/tmp/corrected-spec.md`)

**F1 — Capture last assistant text on `SDKSession`**
- [ ] Add `self.last_assistant_text: str | None = None` next to `self._error_count` in `SDKSession.__init__` (`sdk_session.py:217`).
- [ ] In `_handle_message` `TextBlock` branch (`sdk_session.py:1048-1051`), after the `OUT |` log line, store the latest text. Last `TextBlock` of the run wins — that's the structured result we want to extract.

**F2 — Extend `create_ephemeral_session` with prompt and tool overrides**
- [ ] Add three keyword-only params to `create_ephemeral_session` (`sdk_backend.py:1421`): `system_prompt_override: str | None = None`, `disallowed_tools: tuple[str, ...] = ()`, `bash_deny_regex: tuple[str, ...] = ()`.
- [ ] Plumb through `SDKSession.__init__` (store as `self._sp_override`, `self._extra_disallowed`, `self._bash_deny_regex`).
- [ ] In `_build_options` (`sdk_session.py:728`): set `opts.disallowed_tools` from the new field; if `_sp_override` set, route to SDK's `system_prompt` and skip the inline first-message seeding in `create_ephemeral_session` (`sdk_backend.py:1492-1505`).
- [ ] Extend `_permission_check` with a Bash deny-regex branch that returns `PermissionResultDeny` on match. Co-locate with existing favorite-tier checks (`sdk_session.py:818-862`). Wire `opts.can_use_tool = self._permission_check` whenever `_bash_deny_regex` is non-empty.
- [ ] Originator passes the canonical lockdown set: `disallowed_tools=("Edit","Write","NotebookEdit")` and a deny-regex covering `send-sms|send-signal|reply|inject-prompt|kill-session|restart-session|remind add|rm -rf|dispatch-investigation|kill|pkill`. (`--allow-mutations` drops Edit/Write and the messaging regex but keeps `dispatch-investigation` blocked.)

**F3 — Investigation hook in `_supervise_ephemeral_tasks` + recursion gate in `_handle_task_requested`**
- [ ] New method `Manager._handle_investigation_completed(task_id, info, session, status)` where `status ∈ {"completed","failed","timeout"}`. Reads `info["investigation"]` + `info["notify_session"]`; pulls `session.last_assistant_text`; parses the `=== INVESTIGATION RESULT === ... === END ===` block (regex with `re.DOTALL`); falls back to whole text on miss with `parse_status="completed_unparsed"`. Builds the `<investigation-result task_id=... status=...>` wrapper and calls `await self.sessions.sessions[notify_session].inject(wrapped)` if alive (skip + log if dead).
- [ ] Insert the call **before** every `kill_ephemeral_session` site in `_supervise_ephemeral_tasks` (`manager.py:3506-3510`, `:3543-3546`, `:3571`) and before the synthetic `task.failed` produced from `_handle_task_requested` when ephemeral creation raises (`:3262-3265`).
- [ ] Recursion gate: in `_handle_task_requested` (`manager.py:3179`), reject when `requested_by` looks like an ephemeral key AND `payload.get("investigation")` is truthy. Produce `investigation.rejected_recursive` and skip.

**F4 — Originator surface (`assistant/investigations.py` + CLI)**
- [ ] New module `assistant/investigations.py` with: `dispatch()`, `get_status()`, `list_inflight()`, `cancel()`, `parse_result_block()`, `RateLimitExceeded`, `RecursiveDispatchError`. State persisted at `state/investigations.json` (atomic write, malformed-JSON fallback).
- [ ] Investigator system prompt template lives as a Python constant in `investigations.py` (NOT in `~/.claude/skills/` — the contact session must not be able to read it and game it). Encodes the triage pattern, structured output contract, time budget, read-only directive.
- [ ] Four subcommands in `assistant/cli.py`: `dispatch-investigation`, `investigation-status`, `investigation-list`, `investigation-cancel`. Originator auto-detected from `cwd` (same trick as `reply`); `--from-session` override for testing. `--allow-mutations` opt-in.
- [ ] Dispatch path: CLI calls into the daemon over the existing IPC socket (NOT via bus produce). The daemon's IPC handler validates rate limits + recursion env-var guard (`CLAUDE_INVESTIGATION_TASK_ID`), then produces `task.requested` with `execution.mode="agent"`, `investigation: True`, `notify_session=<originator>`, `task_id=f"investigation-{uuid4().hex[:12]}"`, `notify=False` (we deliver via F3, not generic task notification). Per-`notify_session` cap: ≤2 concurrent investigations.

### Phase 2 — (folded into F2)

Tool restrictions are part of F2 above. No separate phase.

### Phase 3 — Tests (per `/tmp/test-scaffold.md`)
- [ ] `tests/unit/test_investigations.py` — state file CRUD, atomic write, rate limits (per-session + global window), recursive-dispatch rejection (env-var + originator-flag), threadsafe concurrent dispatch, parser cases (well-formed / partial / absent).
- [ ] `tests/integration/test_investigations.py` — full lifecycle happy path, admin-tier-regardless-of-originator, default read-only restrictions, `--allow-mutations` relaxation, failed/timeout status flags, cancel terminates ephemeral, originator next-turn consumes injection, bus audit trail in order, concurrent isolated investigations, transcript dir created, no resume context bleed.
- [ ] `class TestInvestigationSecurity` in the integration file — adversarial: subshell, eval, base64, write-then-exec, path obfuscation, kill/pkill/killall, filesystem mutations, send-paths under `no_messaging`, `--allow-mutations` still blocks recursion, env-var guard independent of regex, hallucinated admin override ignored.
- [ ] New conftest fixtures: `fake_investigator(mode=...)`, `fake_bus_clock`, `captured_inject_prompt`, `investigation_state_file`, `hostile_investigator_session`, `bus_in_tmp` with `drain_consumer_once()`.
- [ ] Live smoke at `tests/live/test_investigation_smoke.py` (gated on `CLAUDE_LIVE_TESTS=1`) for DR-1/DR-2/DR-5/DR-6 — runs real Opus, six canned scenarios.

### Phase 4 — Self-heal rule (SHIPPED)
- [x] Principle #3.25 added to `~/.claude/CLAUDE.md` directing all sessions to attempt documented recovery first, then dispatch an investigation, then escalate. Updates landed in `~/.claude/skills/sms-assistant/admin-rules.md` and `partner-rules.md`.
- [ ] Update `~/dispatch/CLAUDE.md` "Ephemeral Tasks" section to document the `investigation: true` task variant (after F1–F4 land).
- [ ] Add a `claude-assistant investigations` summary subcommand for history / success rate / durations.

### Phase 5 — Observability (DEFERRED until v1 ships)

Strike from v1. Revisit after we see real usage. Notes preserved here for future reference: bus event sub-types (`investigation.dispatched`, `.started`, `.completed`, `.injected`, `.rejected_recursive`, `.rate_limited`) on the existing `tasks` topic; bus-dashboard tile for outcome breakdown and median latency.

## Derisking

| # | Risk | Critical | Status | Result |
|---|------|----------|--------|--------|
| 1 | SDK harness blocks `disallowed_tools` + Bash deny-regex before model can invoke them | yes | concrete mechanism (disallowed_tools + can_use_tool); needs integration test | |
| 2 | Investigation prompt produces parseable `=== RESULT ===` block; parser fallback ships unparsed text on miss | yes | parser fallback designed; live emit-rate untested | |
| 3 | `inject-prompt` round-trip into originator's `_message_queue` | no | largely moot — direct `session.inject` is battle-tested in production | |
| 4 | Recursive dispatch prevention escape-proof (Bash inject-prompt + bus task.requested vectors) | yes | two new vectors identified — handled by F2 deny-regex + F3 recursion gate | |
| 5 | 5–15 min budget enough for typical triage without burning excessive tokens | no | unchanged — `timeout_minutes` already enforced; live measurement TBD | |
| 6 | Bus injection latency < 3s | n/a | moot for v1 — F3 bypasses bus, uses direct `session.inject` | |
| 7 | Rate limits + cooldown don't deadlock on contention | no | unchanged — hooks into existing `_completed_task_times` cooldown | |

### Derisk Details

#### DR-1: Tool restrictions enforced at SDK harness, not just prompt
- **Hypothesis**: `disallowed_tools` (translated by the CLI to `--disallowedTools`) and `can_use_tool` Bash deny-regex block invocation before the model can run them, regardless of what the system prompt says.
- **Method**: Adversarial integration test (`TestInvestigationSecurity`) verifies deny-regex *config* is correct; live smoke test (`tests/live/`) boots a real ephemeral with hostile prompt and confirms no mutations occur.
- **If busted**: fall back to prompt-only enforcement for v1 behind a feature flag; require admin approval before any investigator-proposed mutation; document prominently in `dispatch-investigation --help`.

#### DR-2: Structured output contract is reliable
- **Hypothesis**: With the template + final-message directive in `system_prompt_override`, models emit the block in 95%+ of runs (success / partial / timeout / failure paths).
- **Method**: Run six canned scenarios against real Opus (chrome runaway, signal-cli wedge, daemon OOM, slow-but-healthy, contradictory evidence, deliberate timeout). Score block-present / fields-filled / parseable / action-specific.
- **If busted**: switch to synthetic `submit_result(symptom, root_cause, action, safety, durable_fix)` tool. Strictly more reliable, more harness complexity. Parser fallback (`parse_status="completed_unparsed"`) means we degrade rather than fail.

#### DR-3: Result injection round-trip works
- **Status**: largely moot. `SDKSession.inject` is the same path used by `_notify_task_event` and runs hourly in production. Risk reduces to "is `notify_session` still alive?" — handled by liveness check + log.
- **Method**: Integration test kills originator mid-investigation; confirm graceful skip.

#### DR-4: Recursive dispatch prevention is escape-proof
- **Hypothesis**: An investigator cannot dispatch its own investigation via (a) `Bash` calling `claude-assistant inject-prompt` / `dispatch-investigation`, (b) producing `task.requested` directly to the bus, or (c) shell escapes (subshell, eval, base64, write-then-exec).
- **Method**: Adversarial test class plus the recursion gate in `_handle_task_requested` (rejects ephemeral-keyed `task.requested` with `investigation: True`). Belt-and-suspenders: `CLAUDE_INVESTIGATION_TASK_ID` env var set on ephemerals → CLI itself refuses.
- **If busted**: tighten regex; consider a one-shot capability token issued at dispatch time and consumed by the daemon (revoked once spent).

#### DR-5: Time budget is reasonable
- **Status**: unchanged. `timeout_minutes` already enforced at `manager.py:3560`. Default 15 min. Per-investigation token budget measured during DR-2 live runs.

#### DR-6: Bus injection latency
- **Status**: moot for v1. F3 calls `session.inject` directly — bypasses bus entirely on the result-delivery path. Bus is used only for the `task.requested` kickoff (already <100ms).

#### DR-7: Concurrency/rate limits behave under contention
- **Hypothesis**: With max 2 in-flight per session, a 3rd dispatch from the same originator raises `RateLimitExceeded` cleanly.
- **Method**: Unit test with concurrent threads; integration test with two parallel originators verifying isolation.

## Notes

### Implementation status (2026-04-28)
- **Phase 4 SHIPPED**: self-heal principle #3.25 in `~/.claude/CLAUDE.md`; admin-rules.md and partner-rules.md updated.
- **Phase 1 (F1–F4) in flight**: parallel implementation agents working concurrently against `/tmp/corrected-spec.md`.
- **Phase 3 tests** scheduled after F1–F4 land. Scaffold at `/tmp/test-scaffold.md`.
- **Phase 5 deferred** until v1 ships and we have real usage data.
- Plan was rewritten 2026-04-28 to reflect code-verification findings (six original claims about the codebase were wrong). Status stays `active` — flip to `implementing` once F1–F4 land.

### References
- Corrected technical spec (with file:line citations from `manager.py`, `sdk_backend.py`, `sdk_session.py`, `bus_helpers.py`): `/tmp/corrected-spec.md`.
- Test scaffold (unit + integration + adversarial structure, fixture list, mock strategy, coverage gaps): `/tmp/test-scaffold.md`.

### Architecture decision: piggyback on existing ephemeral-task path
The dispatch system already has agent-mode ephemeral tasks (`_handle_task_requested`, `create_ephemeral_session`). New work is **not** a parallel system — it's a specialization with two new payload fields (`investigation: true`, `notify_session`) and a new branch in `_supervise_ephemeral_tasks` (NOT `_handle_task_completed`, which doesn't exist — task completion is detected in the supervisor poll loop). This keeps the audit story coherent (all events on `tasks` topic) and reuses timeout/cleanup machinery.

### Architecture decision: read-only by default, mutations opt-in
First-run behavior is *diagnose, don't fix.* The investigator returns "RECOMMENDED ACTION: chrome reset" but the originating session decides whether to run it. This protects against a hallucinating investigator nuking state. `--allow-mutations` is for cases where the user explicitly trusts the agent to act.

### Architecture decision: structured output via template, not a synthetic tool
v1 ships with the `=== RESULT ===` template + parser fallback (unparsed text shipped through with `parse_status="completed_unparsed"`). If DR-2 emit rate is below threshold, we fall back to a synthetic `submit_result` tool — strictly more reliable but adds harness complexity.

### Architecture decision: capture via `last_assistant_text`, not stop hook
The existing `_stop_hook` is contact-session-specific (it nags about `send-sms`) and `context.response.messages` is unstable. Storing the latest `TextBlock` text in `self.last_assistant_text` from `_handle_message` is cheaper, doesn't fight the existing hook, and survives the imminent `kill_ephemeral_session`-driven `shutil.rmtree` of the ephemeral cwd.

### Open question: should the originator block until the investigation completes?
Default is no — fire and continue. Cases exist where the originator can't make progress without the result. Could add `--blocking` later; not in v1.

### Open question: scope of investigator's context
v1: investigator gets ONLY the prompt + admin tier + full skill access — clean slate, no contact-session history. Pro: focused. Con: can't reason about "what was the user trying to do." If DR-2 reveals this matters, add `--include-context "<summary>"`.

### Cost concern
At ~30k input + 10k output tokens per investigation on Opus, each is ~$0.40 rough. Per DR-5 most investigations finish fast and well under that budget. At a 5/hour cap, worst case $2/hour, $48/day if pegged — but realistic usage will be << 1/hour. Worth metering. The summary subcommand (Phase 4) will show it to the admin.

### What we explicitly are NOT building in v1
- Investigators that talk to each other (multi-agent)
- Long-running investigations beyond 30 min
- Investigations that resume across sessions
- Investigation-suggested code edits applied automatically (always require admin approval)
- A Claude Code-style Task/TaskList/TaskOutput tool surface
- Bus event sub-types or dashboard tile (Phase 5 — deferred)

### Connection to today's chrome-control work
The `chrome reset` subcommand and the SKILL.md "Recovery" section we shipped today are the *known-recovery* leg of the strategy. This plan is the *unknown-recovery* leg. Both are needed. Principle #3.25 in the global CLAUDE.md (shipped) references both: try documented recovery, then dispatch an investigation, then escalate.
