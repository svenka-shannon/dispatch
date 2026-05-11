# Self-Healing & Resilience — design doc

**Status:** draft / proposed
**Author:** Claude (with Eric)
**Created:** 2026-05-11
**Trigger:** the 2026-05-11 chrome-control outage — the iMessage assistant sat blocked for ~1 hour because the only recovery path required a human, and nothing retried or escalated in the meantime.

---

## 1. The 2026-05-11 outage (anatomy)

**Timeline:**
- ~12:08 — Eric tells the assistant "+1 and +2" (RiftAtlas signup task starts).
- ~12:08–12:15 — assistant works the task in Chrome; somewhere in here Chrome evicts the chrome-control extension's MV3 service worker mid-flow.
- ~12:15–13:00 — assistant idle. The wedge is invisible until the next `chrome` call.
- ~13:00 — Eric: "Stuck again?"
- ~13:08 — assistant runs `chrome reset` (old behavior), gets "click the extension's toolbar icon", relays that ask to Eric, then **parks indefinitely waiting**.
- ~13:11 — a background "wait for Chrome to recover" task times out; assistant logs "waiting on Eric's response" and does nothing further.
- ~13:30+ — Eric, in a separate terminal session, diagnoses + fixes it.

**Root cause (two layers):**
1. **chrome-control:** an evicted MV3 service worker can't be woken by an outside process, so `chrome reset` dead-ended on a manual extension-icon click. *(Fixed — see §2.)*
2. **System posture:** when a session is blocked on something it can't fix, it (a) had no auto-recovery, (b) didn't retry, (c) didn't escalate loudly, (d) just went idle. A wedged sub-component had no detection until a human or a chat command happened to hit it. *(This doc proposes the fixes.)*

**The lesson:** "ask the human and wait" is not a recovery strategy. Every external dependency needs: a probe, an automatic recovery, a bounded retry/backoff, and a *loud, specific* escalation when auto-recovery genuinely fails — and detection must not depend on someone tripping over the failure.

---

## 2. Already shipped (2026-05-11)

| Change | Commit | Effect |
|---|---|---|
| `chrome reset` self-heals: kills stuck `native_host.py`, then runs `chrome wake` (opens `chrome-extension://<id>/popup.html` via `open(1)` → forces the SW to start → reconnects → closes the tab) | `f65411e` | No more "click the toolbar icon". The existing "run `chrome reset` before bugging the user" guidance is now fully autonomous. |
| `chrome wake` / `chrome reload-extension` subcommands; `connectNativeHost()` singleton guard; race-safe `cmd_reset`; keepalive alarm `0.4→0.5` min | `f65411e` | Programmatic SW revival + reload; stops the alarm-vs-onDisconnect double-spawn that leaked zombie hosts and burned the SW crash budget. |
| Daemon polls chrome-control every ~90s; on wedge (and only if Chrome is running) auto-runs `chrome reset`; emits `health.chrome_check` bus event | `2270a66` | A wedge is detected + recovered in ≤~90s even with no active session. |

These cover the chrome-control case specifically. The rest of this doc generalizes the pattern and closes the "blocked session waits forever" gap.

---

## 3. Goals / SLOs

Concrete budgets to design and test against:

- **Detection:** any wedged external dependency is detected within **≤ 2 min** (independent of whether a chat session or human happens to hit it).
- **Auto-recovery:** recovery is *attempted* within **≤ 30 s** of detection.
- **Bounded retry:** up to **3 recovery attempts** with exponential backoff (e.g. 0s, 60s, 240s) before escalating.
- **Escalation:** if auto-recovery fails 3×, the admin gets an SMS within **≤ 5 min of first detection** containing a *specific* ask (per the existing "Self-Heal Before Escalating" rule in `~/.claude/CLAUDE.md`), not "X is broken".
- **No silent idle:** a session that told the user it would do something never just stops — it retries, escalates, or reports failure.
- **MTTR target:** P50 mean-time-to-recovery for known failure modes **< 2 min**, P95 **< 6 min** (excludes failures that genuinely require physical/human action, which must escalate immediately).

---

## 4. Design

### 4.1 Unified dependency health & recovery framework

**Problem:** health logic is scattered — `assistant/health.py` does SDK-session checks (fast regex + deep Haiku), there's a separate signal-cli health check, a disk/FD check, and now an ad-hoc chrome-control check. Each has its own cadence, logging, and (mostly absent) recovery/escalation. New dependencies get bolted on inconsistently.

**Proposal:** a small `DependencyHealth` registry. Each external dependency registers a descriptor:

```python
@dataclass
class DependencyCheck:
    name: str                       # "chrome_control", "signal_cli", "bus_consumers", ...
    probe: Callable[[], ProbeResult] # returns OK / DEGRADED / DOWN / SKIP(reason)
    recover: Callable[[], RecoverResult] | None
    interval_s: int                  # poll cadence
    max_attempts: int = 3            # before escalation
    backoff_s: tuple[int, ...] = (0, 60, 240)
    escalate: Callable[[Context], None] | None  # default: SMS admin with structured detail
    enabled_when: Callable[[], bool] | None     # e.g. "only if Chrome.app is running"
```

The daemon runs each check on its cadence (off the main loop, via `asyncio.to_thread` / executor with a per-check timeout — never block message routing). State machine per dependency: `HEALTHY → DEGRADED → DOWN → RECOVERING → (HEALTHY | ESCALATED)`. Every transition + recovery attempt → a `health.dependency` bus event (`name`, `state`, `attempt`, `action_taken`, `detail`, diagnostics) and a manager-log line (logs authoritative). Healthy steady-state is DEBUG-only (no spam).

**Migration:** fold the existing checks into this framework:
- `chrome_control` — probe `chrome ping` (12s timeout); recover `chrome reset`; `enabled_when` = Chrome.app running; escalate only on the *errored-extension* signal (the one true human-required case — see §4.3).
- `signal_cli` — probe the JSON-RPC socket; recover = restart signal-cli daemon with the correct `--receive-mode on-connection` flag; escalate after 3 fails.
- `sdk_sessions` — the existing fast/deep health check, but recovery (restart-session) and escalation routed through the framework with backoff (today a flapping session can restart-loop; circuit breaker exists but isn't unified).
- `bus_consumers` — probe `bus groups` for DEAD status / stale heartbeat; recover = restart the consumer runner; escalate after 3.
- `disk` / `fd_leak` — probe exists in `health.py`; add recovery where possible (e.g. clean known temp dirs, rotate logs) before escalation.
- `chrome_zombies` — sweep for orphaned `native_host.py` processes (the 28-started-vs-8-exited gap on 2026-05-11); reap any not owned by a live socket. Low-cadence (every few min).

### 4.2 Blocked-session watchdog (the "don't wait forever" fix)

**Problem:** the 2026-05-11 assistant *correctly* tried `chrome reset`, then asked the human, then **parked**. It never re-checked. It never escalated past one SMS. It just stopped.

**Proposal — two parts:**

1. **In-session discipline (skill/prompt level, partly already in `~/.claude/CLAUDE.md`):** when a session hits a tool that hangs/errors: (a) run the documented recovery and retry once; (b) if still stuck, *schedule a re-check* (via a short reminder / background task) rather than ending the turn idle — e.g. "retry the blocked step in 2 min"; (c) if still stuck after the recovery + retry, send the admin a *specific* ask AND keep a pending re-check alive so it self-resolves if the dep comes back; (d) never silently abandon a task the user is expecting. Tighten the relevant rule files (`admin-rules.md`, chrome-control SKILL.md, etc.) so this is unambiguous, and consider a "stuck task" checklist the session must follow.

2. **Daemon-level backstop:** the daemon already tracks per-session turn activity. Add: if a session has been idle for > N min *and* its last outbound message to the user contained a "I'll do X" / "waiting on you to do Y" commitment (heuristic: detectable phrases, or an explicit `pending_task` marker the session can set), the daemon nudges the session ("re-check whether the blocker cleared; if so continue, if not escalate with specifics"). This is the watchdog-for-stuck-tasks analogue of the daemon-restart watchdog. Cheap, and it would have turned the 1-hour gap into ~5 min.

### 4.3 chrome-control deeper hardening

Beyond what shipped in `f65411e` / `2270a66`:

- **Prevent eviction proactively:** the native-messaging port keeps the SW alive as long as messages flow. Today the SW pings the host every 30s (alarm-driven) and the host pongs — circular: if the alarm misfires, the SW idles out. Fix: have `native_host.py` send **unsolicited heartbeats every ~20s** so the port traffic — and thus the SW — survives even if the alarm hiccups. The 30s alarm stays as a backstop *for waking* an already-evicted SW.
- **Errored-extension detection:** after ~5 SW crashes Chrome marks the extension errored and stops auto-restarting it — `chrome wake` already detects this ("did not come back within 15s"). The framework should treat this as the *one* genuinely human-required state: escalate immediately with the exact ask ("reload chrome-control once at chrome://extensions/ — Developer mode → reload ⟳"), and ideally check `~/Library/Application Support/Google/Chrome/Default/Preferences` for the extension's `disable_reasons` to confirm and report it precisely.
- **Optional: `--remote-debugging-port` on Chrome.** Gives a CDP-based recovery path (open the extension page / inspect the SW target without `open(1)`) and observability into SW state. Trade-offs: a localhost debug port is a (modest) security surface and a slight perf cost. Evaluate; not obviously worth it given `open(1)` works — list as an option, not a commitment.
- **Self-heal-the-skill loop:** when `chrome reset` / `chrome wake` hits a new failure shape, the relevant session should update `skills/chrome-control/SKILL.md` (per the existing "Skill Self-Healing" rule) so the next session is smarter. Already policy; call it out here as part of the resilience story.

### 4.4 Observability & postmortem support

- **Bus events:** `health.dependency` (and the existing `health.chrome_check`, `health.haiku_verdict`, `health.circuit_breaker`, `health.quota_alert`) are the structured trail. Add a documented `bus query` recipe set: "recovery events in the last 24h", "MTTR per dependency", "recovery-attempt rate" (a spike = an underlying problem getting masked by auto-recovery — surface it).
- **Recovery-frequency alarm:** if any dependency needs recovery > K times/hour, escalate ("chrome-control has self-recovered 8× in the last hour — something deeper is wrong"). Auto-recovery should never *hide* a worsening problem.
- **Dashboard:** extend the command-center / bus dashboard with a "dependency health" panel — current state per dependency, last recovery, recovery-rate sparkline. (Low priority; the bus queries cover the need short-term.)

### 4.5 The full recovery chain (document it so there are no gaps)

```
external dep wedges        → DependencyHealth framework probes (≤2min) → recover() → escalate after 3×
SDK session unhealthy      → health.py fast/deep check → restart-session (backoff) → escalate
blocked session waits      → blocked-session watchdog → nudge re-check → escalate
daemon crashes             → com.dispatch.watchdog (LaunchAgent, 60s) → spawns healing Claude → escalate after 5×
watchdog dies              → its LaunchAgent KeepAlive relaunches it
machine reboots/sleeps     → com.dispatch.daemon LaunchAgent + RunAtLoad; deps re-probed on next cycle
```

No new LaunchAgents (per `~/dispatch/CLAUDE.md` — only `com.dispatch.daemon` and `com.dispatch.watchdog` are allowed); everything sub-daemon lives inside the daemon's loop.

---

## 5. Rollout

**Phase 0 — done:** chrome-control self-healing CLI + daemon chrome-control check (`f65411e`, `2270a66`).

**Phase 1 — blocked-session watchdog (highest leverage, smallest surface):**
- Tighten the in-session "stuck tool" discipline in `~/.claude/CLAUDE.md` + rule files + chrome-control SKILL.md (retry → schedule re-check → escalate-with-specifics → never idle-abandon).
- Daemon backstop: nudge a session that's been idle > N min with an outstanding commitment.
- Tests: simulate a session that hits a hung tool; assert it retries + escalates within budget.

**Phase 2 — DependencyHealth framework + migrate existing checks:**
- Build the registry; port `chrome_control`, `signal_cli`, `bus_consumers`, `sdk_sessions` (recovery routing), `disk`/`fd`, `chrome_zombies`.
- Unified `health.dependency` event; manager-log lines; recovery-frequency alarm.
- Tests: `test_dependency_health.py` — state machine, backoff, escalation, `enabled_when` gating, `to_thread` non-blocking; mock all subprocesses.

**Phase 3 — chrome-control deeper hardening:**
- Unsolicited native_host heartbeats; errored-extension detection + precise escalation; zombie reaping in the framework.
- Decide on `--remote-debugging-port` (likely defer).

**Phase 4 — observability:**
- `bus query` recovery/MTTR recipes (document in the relevant SKILL/CLAUDE files); dependency-health dashboard panel.

**Phase 5 — chaos testing — done (suite shipped; awaiting a babysat live run):**
- `tests/chaos/test_chaos_resilience.py` + `tests/chaos/conftest.py` — live smoke tests gated on `CLAUDE_LIVE_TESTS=1` (the most disruptive ones additionally on `CLAUDE_CHAOS_DESTRUCTIVE=1`): kill `native_host` (SW eviction wedge), kill Chrome.app (assert SKIP, no relaunch), errored-extension (mocked → escalates not loops), kill signal-cli (assert restart with `--receive-mode on-connection`), bus consumer crash (mocked recover + live no-DEAD-members invariant), FD leak (mocked → escalates, no auto-recovery), stuck-session nudge (mocked + structural live). Mocked/structural variants run in the normal `pytest` collection (`-m chaos`); the live set runs via `scripts/chaos-test.sh` (loud warning, interactive confirm, `--yes-i-mean-it` for destructive) and **should only be run after `claude-assistant restart` + `chrome reload-extension` with no chat session mid-task**. Docs in `CLAUDE.md` ("Chaos / resilience testing").

---

## 6. Open questions

1. **Blocked-commitment detection** — explicit marker the session sets (`pending_task` in session state) vs. a phrase heuristic on the last outbound message? Marker is cleaner but requires the session to remember to set it; heuristic is best-effort but zero-cooperation. Probably: support the marker, fall back to the heuristic.
2. **Escalation channel during a chrome-control outage** — SMS via `send-sms` doesn't depend on Chrome, so fine. But if the *daemon* is the thing escalating, confirm it has a clean path to `send-sms` that doesn't route through a chat session.
3. **Recovery-rate alarm thresholds** — start conservative (>5/hr per dep)? Tune from real data once `health.dependency` has history.
4. **Should the daemon ever `chrome reload-extension`?** The 90s check currently only does `chrome reset`. If `chrome wake` reports the errored state, a programmatic reload won't help (Chrome won't honor it for an errored extension) — so no; escalate instead. Confirm.
5. **Idle-session nudge interval N** — 5 min? 3? Balance against false positives (session legitimately waiting on a slow human reply). Maybe scale with tier / task type.

---

## 7. Non-goals

- Rewriting the daemon's architecture — this is additive (a registry + a watchdog + hardening), not a refactor.
- Recovering from failures that genuinely require physical/human action (errored extension, machine off, network down) — those must *escalate fast and specifically*, not be "auto-recovered".
- Auto-recovery that masks worsening problems — every recovery is loud (bus event + log), and repeated recoveries escalate.
