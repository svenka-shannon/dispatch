"""
Unified dependency health & recovery framework (self-healing-resilience plan §4.1).

Every external dependency the daemon relies on (chrome-control, signal-cli, bus
consumers, SDK sessions, disk, FD leaks, ...) is described by a `DependencyCheck`:
a probe, an optional recovery, a poll cadence, a bounded retry/backoff schedule,
and an escalation callback. A `DependencyHealthRunner` holds the registry, runs
each check on its own cadence *off the main loop* (via `asyncio.to_thread`) with a
per-check timeout, and drives a small per-dependency state machine:

    HEALTHY → DEGRADED → DOWN → RECOVERING → (HEALTHY | ESCALATED)

On DOWN (or a DEGRADED that warrants action): attempt `recover()` up to
`max_attempts` with `backoff_s` between attempts; success → HEALTHY (+ recovery
event); exhausted → ESCALATED + `escalate()`. From ESCALATED we keep probing; a
later successful probe → HEALTHY + a "recovered after escalation" event.

Every transition + recovery attempt + escalation emits a `health.dependency` bus
event on the `system` topic and an authoritative manager-log line. Healthy
steady-state is DEBUG-only (no spam). A recovery-frequency alarm escalates if a
dependency self-recovers > K times/hour ("auto-recovery must never silently mask
a worsening problem").

Pure logic — no module-level side effects. The daemon wires
`build_default_registry()` into `Manager.run()`; tests construct registries with
mocked probes/recoveries/clock/sleep/SMS.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Awaitable, Callable, Optional

log = logging.getLogger(__name__)
lifecycle_log = logging.getLogger("lifecycle")


# ──────────────────────────────────────────────────────────────
# Probe / recover result types
# ──────────────────────────────────────────────────────────────

class ProbeStatus(str, Enum):
    OK = "ok"
    DEGRADED = "degraded"   # working but impaired — may warrant action
    DOWN = "down"           # not working — recovery needed
    SKIP = "skip"           # not applicable right now (e.g. Chrome.app not running)


@dataclass
class ProbeResult:
    status: ProbeStatus
    detail: str = ""
    # Free-form diagnostics carried into the bus event (probe_rc, probe_output, ...).
    diagnostics: dict[str, Any] = field(default_factory=dict)
    # If the probe found the *one genuinely human-required* state (e.g. errored
    # extension), it can request immediate escalation instead of recovery attempts.
    escalate_now: bool = False

    @classmethod
    def ok(cls, detail: str = "", **diag: Any) -> "ProbeResult":
        return cls(ProbeStatus.OK, detail, dict(diag))

    @classmethod
    def degraded(cls, detail: str, **diag: Any) -> "ProbeResult":
        return cls(ProbeStatus.DEGRADED, detail, dict(diag))

    @classmethod
    def down(cls, detail: str, *, escalate_now: bool = False, **diag: Any) -> "ProbeResult":
        return cls(ProbeStatus.DOWN, detail, dict(diag), escalate_now=escalate_now)

    @classmethod
    def skip(cls, reason: str) -> "ProbeResult":
        return cls(ProbeStatus.SKIP, reason)


@dataclass
class RecoverResult:
    success: bool
    detail: str = ""
    diagnostics: dict[str, Any] = field(default_factory=dict)
    # Set when the recovery attempt itself revealed a human-required state — skip
    # the remaining attempts and escalate immediately.
    escalate_now: bool = False

    @classmethod
    def ok(cls, detail: str = "", **diag: Any) -> "RecoverResult":
        return cls(True, detail, dict(diag))

    @classmethod
    def fail(cls, detail: str, *, escalate_now: bool = False, **diag: Any) -> "RecoverResult":
        return cls(False, detail, dict(diag), escalate_now=escalate_now)


# ──────────────────────────────────────────────────────────────
# State machine
# ──────────────────────────────────────────────────────────────

class DepState(str, Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    DOWN = "DOWN"
    RECOVERING = "RECOVERING"
    ESCALATED = "ESCALATED"


# ──────────────────────────────────────────────────────────────
# DependencyCheck descriptor
# ──────────────────────────────────────────────────────────────

# Escalate callback signature: escalate(name, state, detail, diagnostics) -> None
EscalateFn = Callable[[str, str, str, dict[str, Any]], None]


@dataclass
class DependencyCheck:
    """Descriptor for one external dependency.

    probe / recover may be sync (run via to_thread) or async (awaited directly).
    """
    name: str
    probe: Callable[[], ProbeResult] | Callable[[], Awaitable[ProbeResult]]
    recover: Optional[Callable[[], RecoverResult] | Callable[[], Awaitable[RecoverResult]]] = None
    interval_s: int = 120
    max_attempts: int = 3
    backoff_s: tuple[int, ...] = (0, 60, 240)
    # escalate(name, state, detail, diagnostics). None → runner uses its default
    # (SMS the admin with a structured, specific message).
    escalate: Optional[EscalateFn] = None
    enabled_when: Optional[Callable[[], bool]] = None
    # Per-check probe timeout (seconds). Recovery gets recover_timeout_s.
    probe_timeout_s: float = 20.0
    recover_timeout_s: float = 60.0
    # DEGRADED that warrants the recovery path (vs. just logging it as impaired).
    recover_on_degraded: bool = False
    # Recovery-frequency alarm: if recover() succeeded > recovery_alarm_k times in
    # the rolling window, escalate ("self-recovered Nx in the last hour").
    recovery_alarm_k: int = 5
    recovery_window_s: int = 3600
    # Don't re-SMS every cycle while ESCALATED — re-escalate at most this often.
    reescalate_after_s: int = 4 * 3600


# ──────────────────────────────────────────────────────────────
# Per-dependency runtime state
# ──────────────────────────────────────────────────────────────

class _DepRuntime:
    def __init__(self, check: DependencyCheck):
        self.check = check
        self.state: DepState = DepState.HEALTHY
        self.last_run: float = 0.0          # monotonic time of last probe cycle
        self.running: bool = False           # guard against overlapping runs
        self.attempt: int = 0                # recovery attempts made in current incident
        self.last_escalation_at: float = 0.0  # monotonic; for re-escalation rate limit
        self.escalated: bool = False         # currently in an escalated incident
        # Rolling window of monotonic timestamps of *successful* recoveries.
        self.recovery_times: deque[float] = deque()
        # Whether the recovery-frequency alarm has already fired for the current burst.
        self.alarm_fired: bool = False


# ──────────────────────────────────────────────────────────────
# Runner
# ──────────────────────────────────────────────────────────────

class DependencyHealthRunner:
    """Holds the registry; ticks checks on their cadence; drives the state machine.

    Designed to be cheap to call frequently — `tick()` is invoked from the daemon
    poll loop (or any scheduler) and only does work for checks whose interval has
    elapsed. Each check's probe/recover runs off the main loop via
    `asyncio.to_thread` with a per-check timeout, guarded against overlap.

    Injectables (for tests): `clock` (monotonic-like), `sleep` (async sleep),
    `to_thread` (defaults to asyncio.to_thread), `escalate_default` (SMS path).
    """

    def __init__(
        self,
        producer: Any = None,
        *,
        escalate_default: Optional[EscalateFn] = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        to_thread: Optional[Callable[..., Awaitable[Any]]] = None,
    ):
        self._producer = producer
        self._escalate_default = escalate_default
        self._clock = clock
        self._sleep = sleep
        self._to_thread = to_thread or asyncio.to_thread
        self._checks: dict[str, _DepRuntime] = {}

    # ── registry ──────────────────────────────────────────────

    def register(self, check: DependencyCheck) -> None:
        if check.name in self._checks:
            raise ValueError(f"dependency check already registered: {check.name}")
        self._checks[check.name] = _DepRuntime(check)
        log.debug("DEP_HEALTH | registered check %s (interval=%ss)", check.name, check.interval_s)

    def names(self) -> list[str]:
        return list(self._checks.keys())

    def state_of(self, name: str) -> DepState:
        return self._checks[name].state

    def status_snapshot(self) -> dict[str, dict[str, Any]]:
        """For dashboards / diagnostics — current state per dependency."""
        out: dict[str, dict[str, Any]] = {}
        for name, rt in self._checks.items():
            out[name] = {
                "state": rt.state.value,
                "attempt": rt.attempt,
                "max_attempts": rt.check.max_attempts,
                "escalated": rt.escalated,
                "recoveries_in_window": len(rt.recovery_times),
            }
        return out

    # ── tick: dispatch due checks ─────────────────────────────

    def tick(self, *, create_task: Callable[..., Any] = asyncio.create_task) -> list[Any]:
        """Fire-and-forget any checks whose interval has elapsed.

        Returns the list of tasks created (mostly for tests).
        Overlapping runs of the same check are suppressed.
        """
        now = self._clock()
        tasks: list[Any] = []
        for name, rt in self._checks.items():
            if rt.running:
                continue
            if now - rt.last_run < rt.check.interval_s:
                continue
            rt.running = True
            rt.last_run = now
            t = create_task(self._run_check_guarded(rt), name=f"dep-health-{name}")
            tasks.append(t)
        return tasks

    async def _run_check_guarded(self, rt: _DepRuntime) -> None:
        try:
            # Hard ceiling over the whole probe→recover chain so a wedged
            # subprocess can never pin this task forever. Generous: probes +
            # max_attempts*recover + worst-case backoff.
            ceiling = (
                rt.check.probe_timeout_s
                + rt.check.max_attempts * rt.check.recover_timeout_s
                + sum(rt.check.backoff_s)
                + 30.0
            )
            await asyncio.wait_for(self.run_check(rt.check.name), timeout=ceiling)
        except asyncio.TimeoutError:
            log.error("DEP_HEALTH | %s | check cycle timed out — clearing guard", rt.check.name)
        except asyncio.CancelledError:
            # SDK/anyio cancel-scope leaks can bubble here; tolerate unless really cancelled.
            task = asyncio.current_task()
            if task is not None:
                while task.cancelling() > 0:
                    task.uncancel()
            log.warning("DEP_HEALTH | %s | CancelledError in check cycle (recovered)", rt.check.name)
        except Exception as e:  # noqa: BLE001 — one check failing must not kill the loop
            log.error("DEP_HEALTH | %s | check cycle errored: %s", rt.check.name, e)
        finally:
            rt.running = False

    # ── one full probe→(recover|escalate) cycle for a single dep ──

    async def run_check(self, name: str) -> ProbeResult:
        """Run one probe→recover→escalate cycle for `name`. Returns the probe result.

        Public so tests can drive a single cycle deterministically.
        """
        rt = self._checks[name]
        check = rt.check

        # enabled_when gate → SKIP (don't relaunch a Chrome the user closed, etc.)
        if check.enabled_when is not None:
            try:
                enabled = bool(await self._maybe_thread(check.enabled_when))
            except Exception as e:  # noqa: BLE001
                log.debug("DEP_HEALTH | %s | enabled_when errored: %s — treating as disabled", name, e)
                enabled = False
            if not enabled:
                # If we were mid-incident and the dep is now N/A, quietly reset.
                if rt.state != DepState.HEALTHY:
                    self._transition(rt, DepState.HEALTHY, "probe",
                                     "dependency no longer applicable (enabled_when=false) — incident reset",
                                     {})
                    rt.attempt = 0
                    rt.escalated = False
                    rt.alarm_fired = False
                return ProbeResult.skip("enabled_when=false")

        # Probe
        try:
            probe = await self._maybe_thread_with_timeout(check.probe, check.probe_timeout_s)
        except asyncio.TimeoutError:
            probe = ProbeResult.down(f"probe timed out after {check.probe_timeout_s}s",
                                     probe_timed_out=True)
        except Exception as e:  # noqa: BLE001
            probe = ProbeResult.down(f"probe errored: {e}", probe_error=str(e))

        # Decide whether the probe result warrants the recovery path.
        warrants_action = (
            probe.status == ProbeStatus.DOWN
            or (probe.status == ProbeStatus.DEGRADED and check.recover_on_degraded)
        )

        if not warrants_action:
            # Healthy / impaired-but-tolerated / skip.
            if probe.status == ProbeStatus.OK:
                if rt.state != DepState.HEALTHY:
                    # Recovered (possibly after escalation, possibly the human fixed it).
                    if rt.escalated:
                        self._transition(rt, DepState.HEALTHY, "probe",
                                         f"recovered after escalation — {probe.detail}",
                                         probe.diagnostics, recovered_after_escalation=True)
                    else:
                        self._transition(rt, DepState.HEALTHY, "probe",
                                         f"recovered — {probe.detail}", probe.diagnostics)
                    rt.attempt = 0
                    rt.escalated = False
                    rt.alarm_fired = False
                else:
                    log.debug("DEP_HEALTH | %s | HEALTHY — %s", name, probe.detail)
                return probe
            elif probe.status == ProbeStatus.DEGRADED:
                # Impaired but not actionable. Surface a DEGRADED state (transition only).
                if rt.state == DepState.HEALTHY:
                    self._transition(rt, DepState.DEGRADED, "probe",
                                     f"degraded (no recovery configured) — {probe.detail}",
                                     probe.diagnostics)
                else:
                    log.debug("DEP_HEALTH | %s | still DEGRADED — %s", name, probe.detail)
                return probe
            else:  # SKIP
                if rt.state != DepState.HEALTHY:
                    self._transition(rt, DepState.HEALTHY, "probe",
                                     "probe skipped — incident reset", {})
                    rt.attempt = 0
                    rt.escalated = False
                    rt.alarm_fired = False
                else:
                    log.debug("DEP_HEALTH | %s | SKIP — %s", name, probe.detail)
                return probe

        # ── actionable: DOWN (or DEGRADED-that-warrants-action) ────────────
        # First mark DOWN/DEGRADED if we weren't already mid-incident.
        target_down = DepState.DOWN if probe.status == ProbeStatus.DOWN else DepState.DEGRADED
        if rt.state in (DepState.HEALTHY,) or (
            rt.state in (DepState.DOWN, DepState.DEGRADED) and rt.state != target_down
        ):
            self._transition(rt, target_down, "probe", probe.detail, probe.diagnostics)
        elif rt.state == DepState.ESCALATED:
            # Still down after escalation — keep probing, re-escalate occasionally.
            self._maybe_reescalate(rt, probe.detail, probe.diagnostics)
            return probe
        elif rt.state == DepState.RECOVERING:
            # Should not normally re-enter here (run_check is serialized per dep),
            # but if it does, just log and bail — the in-flight cycle owns recovery.
            log.debug("DEP_HEALTH | %s | probe while RECOVERING — deferring to in-flight cycle", name)
            return probe
        else:
            log.debug("DEP_HEALTH | %s | still %s — %s", name, rt.state.value, probe.detail)

        # Probe explicitly demanded immediate escalation (the one human-required state).
        if probe.escalate_now:
            self._escalate(rt, probe.detail, probe.diagnostics)
            return probe

        # No recovery configured → escalate immediately.
        if check.recover is None:
            self._escalate(rt, f"{probe.detail} (no automatic recovery available)", probe.diagnostics)
            return probe

        # ── bounded retry/backoff recovery ────────────────────────────────
        for attempt_idx in range(check.max_attempts):
            rt.attempt = attempt_idx + 1
            # Backoff before this attempt (0 for the first).
            wait_s = check.backoff_s[attempt_idx] if attempt_idx < len(check.backoff_s) else check.backoff_s[-1]
            if wait_s > 0:
                await self._sleep(wait_s)

            self._transition(rt, DepState.RECOVERING, "recover",
                             f"recovery attempt {rt.attempt}/{check.max_attempts}"
                             + (f" (after {wait_s}s backoff)" if wait_s else ""),
                             {}, attempt=rt.attempt, max_attempts=check.max_attempts)
            try:
                rec = await self._maybe_thread_with_timeout(check.recover, check.recover_timeout_s)
            except asyncio.TimeoutError:
                rec = RecoverResult.fail(f"recovery timed out after {check.recover_timeout_s}s",
                                         recover_timed_out=True)
            except Exception as e:  # noqa: BLE001
                rec = RecoverResult.fail(f"recovery errored: {e}", recover_error=str(e))

            if rec.success:
                self._record_recovery(rt)
                # Re-probe to confirm? Trust recover()'s self-report — it's the
                # caller's job to only return success when the dep is actually back
                # (e.g. `chrome reset` exits 0 only on real reconnect).
                self._transition(rt, DepState.HEALTHY, "recover",
                                 f"recovered on attempt {rt.attempt}/{check.max_attempts} — {rec.detail}",
                                 rec.diagnostics, attempt=rt.attempt, max_attempts=check.max_attempts,
                                 recovered=True)
                rt.attempt = 0
                rt.escalated = False
                # Recovery-frequency alarm — auto-recovery must not mask a worsening problem.
                self._check_recovery_alarm(rt)
                return probe

            # This attempt failed — emit a recovery-attempt-result event (stays
            # RECOVERING) carrying the recover() diagnostics so the trail records
            # *why* the attempt failed.
            log.warning("DEP_HEALTH | %s | recovery attempt %d/%d failed — %s",
                        name, rt.attempt, check.max_attempts, rec.detail)
            self._emit(rt, DepState.RECOVERING.value, DepState.RECOVERING.value, "recover",
                       f"recovery attempt {rt.attempt}/{check.max_attempts} failed — {rec.detail}",
                       rec.diagnostics, attempt=rt.attempt, max_attempts=check.max_attempts,
                       recovered=False)
            if rec.escalate_now:
                # The recovery surfaced a human-required state — stop retrying.
                self._escalate(rt, rec.detail, rec.diagnostics)
                return probe

        # Exhausted all attempts.
        self._escalate(rt,
                       f"auto-recovery failed after {check.max_attempts} attempts — {probe.detail}",
                       probe.diagnostics)
        return probe

    # ── state transitions / events ────────────────────────────

    def _transition(self, rt: _DepRuntime, new_state: DepState, action: str,
                    detail: str, diagnostics: dict[str, Any], **extra: Any) -> None:
        prev = rt.state
        rt.state = new_state
        # Authoritative manager-log line (DEBUG for healthy steady-state handled by callers).
        msg = f"DEP_HEALTH | {rt.check.name} | {prev.value}→{new_state.value} | action={action} | {detail}"
        if new_state in (DepState.DOWN, DepState.DEGRADED, DepState.ESCALATED):
            log.warning(msg)
        elif new_state == DepState.RECOVERING:
            log.info(msg)
        elif new_state == DepState.HEALTHY and prev != DepState.HEALTHY:
            log.info(msg)
        else:
            log.debug(msg)
        try:
            lifecycle_log.info(
                "DEP_HEALTH | %s | %s→%s | %s", rt.check.name, prev.value, new_state.value, detail
            )
        except Exception:
            pass
        self._emit(rt, new_state.value, prev.value, action, detail, diagnostics, **extra)

    def _emit(self, rt: _DepRuntime, state: str, prev_state: str, action: str,
              detail: str, diagnostics: dict[str, Any], **extra: Any) -> None:
        payload: dict[str, Any] = {
            "schema_v": 1,
            "name": rt.check.name,
            "state": state,
            "prev_state": prev_state,
            "action_taken": action,           # probe | recover | escalate | none
            "detail": detail,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if extra:
            payload.update(extra)
        if diagnostics:
            # Keep diagnostics flat (probe_rc, probe_output, recover_rc, recover_output, ...).
            for k, v in diagnostics.items():
                payload[k] = v
        try:
            from assistant.bus_helpers import produce_event
            produce_event(self._producer, "system", "health.dependency", payload,
                          key=rt.check.name, source="dependency_health")
        except Exception as e:  # noqa: BLE001 — bus is a convenience layer; logs are authoritative
            log.debug("DEP_HEALTH | %s | bus emit failed (non-fatal): %s", rt.check.name, e)

    # ── escalation ────────────────────────────────────────────

    def _escalate(self, rt: _DepRuntime, detail: str, diagnostics: dict[str, Any]) -> None:
        first_time = not rt.escalated
        rt.escalated = True
        rt.last_escalation_at = self._clock()
        if rt.state != DepState.ESCALATED:
            self._transition(rt, DepState.ESCALATED, "escalate", detail, diagnostics)
        else:
            # Already escalated; emit a re-escalation event.
            self._emit(rt, DepState.ESCALATED.value, DepState.ESCALATED.value,
                       "escalate", f"still down — {detail}", diagnostics)
        if first_time:
            self._fire_escalation(rt, detail, diagnostics)
        # else: rate-limited; _maybe_reescalate handles periodic re-SMS.

    def _maybe_reescalate(self, rt: _DepRuntime, detail: str, diagnostics: dict[str, Any]) -> None:
        now = self._clock()
        if now - rt.last_escalation_at >= rt.check.reescalate_after_s:
            rt.last_escalation_at = now
            self._emit(rt, DepState.ESCALATED.value, DepState.ESCALATED.value,
                       "escalate", f"still down (re-escalation) — {detail}", diagnostics)
            self._fire_escalation(rt, f"STILL DOWN — {detail}", diagnostics)
        else:
            log.debug("DEP_HEALTH | %s | still ESCALATED — %s", rt.check.name, detail)

    def _fire_escalation(self, rt: _DepRuntime, detail: str, diagnostics: dict[str, Any]) -> None:
        fn = rt.check.escalate or self._escalate_default
        if fn is None:
            log.error("DEP_HEALTH | %s | ESCALATED but no escalation callback configured: %s",
                      rt.check.name, detail)
            return
        try:
            fn(rt.check.name, rt.state.value, detail, diagnostics)
        except Exception as e:  # noqa: BLE001
            log.error("DEP_HEALTH | %s | escalation callback failed: %s", rt.check.name, e)

    # ── recovery-frequency alarm ──────────────────────────────

    def _record_recovery(self, rt: _DepRuntime) -> None:
        now = self._clock()
        rt.recovery_times.append(now)
        cutoff = now - rt.check.recovery_window_s
        while rt.recovery_times and rt.recovery_times[0] < cutoff:
            rt.recovery_times.popleft()

    def _check_recovery_alarm(self, rt: _DepRuntime) -> None:
        n = len(rt.recovery_times)
        if n > rt.check.recovery_alarm_k and not rt.alarm_fired:
            rt.alarm_fired = True
            window_min = int(rt.check.recovery_window_s / 60)
            detail = (
                f"{rt.check.name} has self-recovered {n}× in the last {window_min} min — "
                f"something deeper is wrong; auto-recovery is masking a worsening problem"
            )
            log.error("DEP_HEALTH | %s | RECOVERY-FREQUENCY ALARM | %s", rt.check.name, detail)
            self._emit(rt, rt.state.value, rt.state.value, "escalate", detail,
                       {"recoveries_in_window": n, "recovery_alarm_k": rt.check.recovery_alarm_k})
            self._fire_escalation(rt, detail, {"recoveries_in_window": n})
        elif n <= rt.check.recovery_alarm_k:
            # Below threshold again → re-arm the alarm.
            rt.alarm_fired = False

    # ── helpers ───────────────────────────────────────────────

    async def _maybe_thread(self, fn: Callable[[], Any]) -> Any:
        """Run fn — awaiting if it returns a coroutine, else offloading to a thread."""
        import inspect
        if inspect.iscoroutinefunction(fn):
            return await fn()
        result = await self._to_thread(fn)
        if asyncio.iscoroutine(result):
            return await result
        return result

    async def _maybe_thread_with_timeout(self, fn: Callable[[], Any], timeout_s: float) -> Any:
        return await asyncio.wait_for(self._maybe_thread(fn), timeout=timeout_s)


# ──────────────────────────────────────────────────────────────
# Default escalation: SMS the admin (NOT via a chat session)
# ──────────────────────────────────────────────────────────────

def make_sms_escalator(send_sms: Callable[[str, str], Any],
                       admin_phone: Optional[str]) -> EscalateFn:
    """Build the default escalation callback: shell out to send-sms to the admin.

    `send_sms(phone, message)` is the daemon's own helper (Manager._send_sms),
    which does NOT route through a chat session. `admin_phone` comes from config
    (owner.phone). If neither is available, escalations are log-only.

    Rate-limiting of *re-escalation* is handled by the runner; this callback just
    sends one structured SMS per invocation.
    """
    def _escalate(name: str, state: str, detail: str, diagnostics: dict[str, Any]) -> None:
        if not admin_phone:
            log.error("DEP_HEALTH | %s | ESCALATED (%s) but no admin phone configured: %s",
                      name, state, detail)
            return
        # Build a structured, *specific* message — concrete ask, not "X is broken".
        # The detail already carries the specifics (per-check escalate callbacks
        # craft the precise fix; this default wraps it).
        body = f"[SVEN] 🚨 dependency '{name}' {state}\n{detail}"
        # Append the most useful diagnostics if present and not already in detail.
        extras = []
        for k in ("probe_rc", "probe_output", "recover_rc", "recover_output", "disable_reason"):
            v = diagnostics.get(k)
            if v not in (None, ""):
                extras.append(f"{k}={v}")
        if extras:
            body += "\n" + " | ".join(extras)
        try:
            send_sms(admin_phone, body)
        except Exception as e:  # noqa: BLE001
            log.error("DEP_HEALTH | %s | escalation SMS failed: %s", name, e)
    return _escalate
