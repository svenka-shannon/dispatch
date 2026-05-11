"""Tests for the unified DependencyHealth framework (assistant/dependency_health.py).

Covers:
  - state machine: healthy → down → recovering → healthy
  - → escalated after max_attempts exhausted
  - → recovered-after-escalation (a later good probe self-resolves an escalation)
  - backoff timing between recovery attempts (mocked sleep + clock)
  - enabled_when SKIP gating (don't probe a disabled dep; reset incident if disabled mid-flow)
  - non-blocking: probe/recover offloaded via the injected to_thread
  - recovery-frequency alarm fires at >K recoveries/window
  - escalation SMS callback called with a specific message + rate-limited (one per incident)
  - tick() respects per-check interval_s and the overlap guard

Everything is mocked — no real subprocesses, no daemon, no SMS, no real clock/sleep.
"""
from __future__ import annotations

import asyncio

import pytest

from assistant.dependency_health import (
    DepState,
    DependencyCheck,
    DependencyHealthRunner,
    ProbeResult,
    RecoverResult,
    make_sms_escalator,
)


# ──────────────────────────────────────────────────────────────
# Test doubles
# ──────────────────────────────────────────────────────────────

class FakeClock:
    """Monotonic-ish clock we advance manually; injected `sleep` advances it."""
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def make_runner(producer=None, escalate=None):
    """Runner with a fake clock + a sleep that advances the clock (no real waiting),
    and a to_thread that just calls the fn inline (still async — exercises the path)."""
    clock = FakeClock()
    slept: list[float] = []

    async def fake_sleep(s: float) -> None:
        slept.append(s)
        clock.advance(s)

    async def inline_to_thread(fn, *a, **kw):
        return fn(*a, **kw)

    runner = DependencyHealthRunner(
        producer=producer,
        escalate_default=escalate,
        clock=clock,
        sleep=fake_sleep,
        to_thread=inline_to_thread,
    )
    return runner, clock, slept


class EscalationRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, dict]] = []

    def __call__(self, name, state, detail, diagnostics) -> None:
        self.calls.append((name, state, detail, dict(diagnostics)))


# ──────────────────────────────────────────────────────────────
# state machine
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_healthy_steady_state_no_transition():
    runner, _clock, _slept = make_runner()
    runner.register(DependencyCheck(
        name="dep", probe=lambda: ProbeResult.ok("fine"), recover=None,
        interval_s=10,
    ))
    assert runner.state_of("dep") == DepState.HEALTHY
    await runner.run_check("dep")
    assert runner.state_of("dep") == DepState.HEALTHY


@pytest.mark.asyncio
async def test_down_then_recover_to_healthy():
    state = {"down": True}
    recoveries: list[int] = []

    def probe():
        return ProbeResult.down("wedged") if state["down"] else ProbeResult.ok("back")

    def recover():
        recoveries.append(1)
        state["down"] = False
        return RecoverResult.ok("reset reconnected", recover_rc=0)

    runner, _clock, _slept = make_runner()
    runner.register(DependencyCheck(
        name="dep", probe=probe, recover=recover,
        interval_s=10, max_attempts=3, backoff_s=(0, 5, 10),
    ))
    await runner.run_check("dep")
    # Recovered on the first attempt → HEALTHY again, no escalation.
    assert runner.state_of("dep") == DepState.HEALTHY
    assert len(recoveries) == 1


@pytest.mark.asyncio
async def test_recover_succeeds_on_second_attempt_with_backoff():
    attempts = {"n": 0}

    def probe():
        return ProbeResult.down("wedged")

    def recover():
        attempts["n"] += 1
        if attempts["n"] < 2:
            return RecoverResult.fail("still wedged")
        return RecoverResult.ok("reconnected on retry")

    runner, _clock, slept = make_runner()
    runner.register(DependencyCheck(
        name="dep", probe=probe, recover=recover,
        interval_s=10, max_attempts=3, backoff_s=(0, 60, 240),
    ))
    await runner.run_check("dep")
    assert runner.state_of("dep") == DepState.HEALTHY
    assert attempts["n"] == 2
    # Backoff before attempt 1 was 0 (not slept), before attempt 2 was 60s.
    assert slept == [60]


@pytest.mark.asyncio
async def test_escalates_after_max_attempts():
    rec = EscalationRecorder()

    runner, _clock, slept = make_runner(escalate=rec)
    runner.register(DependencyCheck(
        name="dep",
        probe=lambda: ProbeResult.down("wedged", probe_rc=7),
        recover=lambda: RecoverResult.fail("nope", recover_rc=1),
        interval_s=10, max_attempts=3, backoff_s=(0, 60, 240),
    ))
    await runner.run_check("dep")
    assert runner.state_of("dep") == DepState.ESCALATED
    # Backoffs: 0 (attempt1), 60 (attempt2), 240 (attempt3).
    assert slept == [60, 240]
    # Exactly one escalation SMS for this incident.
    assert len(rec.calls) == 1
    name, state, detail, diag = rec.calls[0]
    assert name == "dep"
    assert state == "ESCALATED"
    assert "auto-recovery failed after 3 attempts" in detail
    # Probe diagnostics carried through to the escalation.
    assert diag.get("probe_rc") == 7


@pytest.mark.asyncio
async def test_no_recover_configured_escalates_immediately():
    rec = EscalationRecorder()
    runner, _clock, slept = make_runner(escalate=rec)
    runner.register(DependencyCheck(
        name="dep", probe=lambda: ProbeResult.down("FDs exhausted", fd_actual=300),
        recover=None, interval_s=10,
    ))
    await runner.run_check("dep")
    assert runner.state_of("dep") == DepState.ESCALATED
    assert slept == []  # no recovery attempts
    assert len(rec.calls) == 1
    assert "no automatic recovery available" in rec.calls[0][2]


@pytest.mark.asyncio
async def test_probe_escalate_now_skips_recovery():
    rec = EscalationRecorder()
    attempted = {"recover": 0}

    def recover():
        attempted["recover"] += 1
        return RecoverResult.ok()

    runner, _clock, _slept = make_runner(escalate=rec)
    runner.register(DependencyCheck(
        name="dep",
        probe=lambda: ProbeResult.down("errored extension", escalate_now=True, disable_reason="needs a reload"),
        recover=recover, interval_s=10,
    ))
    await runner.run_check("dep")
    assert runner.state_of("dep") == DepState.ESCALATED
    assert attempted["recover"] == 0
    assert len(rec.calls) == 1
    assert rec.calls[0][3].get("disable_reason") == "needs a reload"


@pytest.mark.asyncio
async def test_recover_escalate_now_stops_retrying():
    rec = EscalationRecorder()
    attempts = {"n": 0}

    def recover():
        attempts["n"] += 1
        # First recovery attempt reveals a human-required state.
        return RecoverResult.fail("chrome reset rc=1 — extension errored", escalate_now=True, recover_rc=1)

    runner, _clock, _slept = make_runner(escalate=rec)
    runner.register(DependencyCheck(
        name="dep", probe=lambda: ProbeResult.down("wedged"),
        recover=recover, interval_s=10, max_attempts=3, backoff_s=(0, 60, 240),
    ))
    await runner.run_check("dep")
    assert runner.state_of("dep") == DepState.ESCALATED
    assert attempts["n"] == 1  # did not retry
    assert len(rec.calls) == 1


@pytest.mark.asyncio
async def test_recovered_after_escalation():
    rec = EscalationRecorder()
    state = {"down": True}

    runner, _clock, _slept = make_runner(escalate=rec)
    runner.register(DependencyCheck(
        name="dep",
        probe=lambda: ProbeResult.down("wedged") if state["down"] else ProbeResult.ok("human fixed it"),
        recover=lambda: RecoverResult.fail("can't fix"),
        interval_s=10, max_attempts=2, backoff_s=(0, 60),
    ))
    # Cycle 1: down → recovery fails twice → ESCALATED.
    await runner.run_check("dep")
    assert runner.state_of("dep") == DepState.ESCALATED
    assert len(rec.calls) == 1

    # Human fixes it out of band; next probe sees it healthy.
    state["down"] = False
    await runner.run_check("dep")
    assert runner.state_of("dep") == DepState.HEALTHY
    # No additional escalation SMS.
    assert len(rec.calls) == 1


@pytest.mark.asyncio
async def test_escalation_rate_limited_while_still_down():
    rec = EscalationRecorder()
    runner, clock, _slept = make_runner(escalate=rec)
    runner.register(DependencyCheck(
        name="dep", probe=lambda: ProbeResult.down("wedged"),
        recover=lambda: RecoverResult.fail("nope"),
        interval_s=10, max_attempts=1, backoff_s=(0,),
        reescalate_after_s=3600,
    ))
    await runner.run_check("dep")
    assert runner.state_of("dep") == DepState.ESCALATED
    assert len(rec.calls) == 1

    # Probe again immediately — still down, but within reescalate window → no new SMS.
    await runner.run_check("dep")
    assert len(rec.calls) == 1

    # Advance past the re-escalation window → one more SMS.
    clock.advance(3601)
    await runner.run_check("dep")
    assert len(rec.calls) == 2
    assert "STILL DOWN" in rec.calls[1][2]


# ──────────────────────────────────────────────────────────────
# enabled_when gating
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_enabled_when_false_skips_probe():
    probed = {"n": 0}

    def probe():
        probed["n"] += 1
        return ProbeResult.down("would be down")

    runner, _clock, _slept = make_runner()
    runner.register(DependencyCheck(
        name="dep", probe=probe, recover=lambda: RecoverResult.ok(),
        interval_s=10, enabled_when=lambda: False,
    ))
    result = await runner.run_check("dep")
    assert result.status.value == "skip"
    assert probed["n"] == 0
    assert runner.state_of("dep") == DepState.HEALTHY


@pytest.mark.asyncio
async def test_disabled_mid_incident_resets():
    enabled = {"v": True}
    rec = EscalationRecorder()
    runner, _clock, _slept = make_runner(escalate=rec)
    runner.register(DependencyCheck(
        name="dep", probe=lambda: ProbeResult.down("wedged"),
        recover=lambda: RecoverResult.fail("nope"),
        interval_s=10, max_attempts=1, backoff_s=(0,),
        enabled_when=lambda: enabled["v"],
    ))
    await runner.run_check("dep")
    assert runner.state_of("dep") == DepState.ESCALATED

    # The dep becomes not-applicable (e.g. Chrome closed) → incident reset to HEALTHY.
    enabled["v"] = False
    await runner.run_check("dep")
    assert runner.state_of("dep") == DepState.HEALTHY


# ──────────────────────────────────────────────────────────────
# recovery-frequency alarm
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_recovery_frequency_alarm_fires_above_k():
    rec = EscalationRecorder()
    runner, clock, _slept = make_runner(escalate=rec)
    runner.register(DependencyCheck(
        name="flappy",
        probe=lambda: ProbeResult.ok(),  # default; flipped per-cycle below
        recover=lambda: RecoverResult.ok("reset"),
        interval_s=10, max_attempts=1, backoff_s=(0,),
        recovery_alarm_k=3, recovery_window_s=3600,
    ))
    rt = runner._checks["flappy"]

    # Force 4 down→recover→healthy cycles within the window. K=3 so the 4th trips the alarm.
    flip = {"down": True}
    rt.check.probe = lambda: ProbeResult.down("wedged") if flip["down"] else ProbeResult.ok("ok")

    def do_recover():
        flip["down"] = False
        return RecoverResult.ok("reset")
    rt.check.recover = do_recover

    for i in range(4):
        flip["down"] = True
        await runner.run_check("flappy")
        assert runner.state_of("flappy") == DepState.HEALTHY
        clock.advance(60)  # still within the 1h window

    # The alarm should have escalated exactly once (on the 4th recovery).
    alarm_calls = [c for c in rec.calls if "self-recovered" in c[2]]
    assert len(alarm_calls) == 1
    assert "self-recovered 4×" in alarm_calls[0][2]


@pytest.mark.asyncio
async def test_recovery_frequency_alarm_does_not_fire_below_k():
    rec = EscalationRecorder()
    runner, clock, _slept = make_runner(escalate=rec)
    flip = {"down": True}

    def probe():
        return ProbeResult.down("wedged") if flip["down"] else ProbeResult.ok("ok")

    def recover():
        flip["down"] = False
        return RecoverResult.ok("reset")

    runner.register(DependencyCheck(
        name="dep", probe=probe, recover=recover,
        interval_s=10, max_attempts=1, backoff_s=(0,),
        recovery_alarm_k=5, recovery_window_s=3600,
    ))
    for _ in range(3):
        flip["down"] = True
        await runner.run_check("dep")
        clock.advance(60)
    assert [c for c in rec.calls if "self-recovered" in c[2]] == []


# ──────────────────────────────────────────────────────────────
# non-blocking via to_thread
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_probe_runs_via_injected_to_thread():
    used = {"to_thread": 0}

    async def counting_to_thread(fn, *a, **kw):
        used["to_thread"] += 1
        return fn(*a, **kw)

    runner = DependencyHealthRunner(
        producer=None, escalate_default=None,
        clock=FakeClock(), sleep=asyncio.sleep, to_thread=counting_to_thread,
    )
    runner.register(DependencyCheck(name="dep", probe=lambda: ProbeResult.ok(), interval_s=10))
    await runner.run_check("dep")
    # enabled_when not set, but the sync probe goes through to_thread.
    assert used["to_thread"] >= 1


@pytest.mark.asyncio
async def test_async_probe_is_awaited_directly():
    calls = {"n": 0}

    async def async_probe():
        calls["n"] += 1
        return ProbeResult.ok("async fine")

    used = {"to_thread": 0}

    async def counting_to_thread(fn, *a, **kw):
        used["to_thread"] += 1
        return fn(*a, **kw)

    runner = DependencyHealthRunner(
        producer=None, clock=FakeClock(), sleep=asyncio.sleep, to_thread=counting_to_thread,
    )
    runner.register(DependencyCheck(name="dep", probe=async_probe, interval_s=10))
    await runner.run_check("dep")
    assert calls["n"] == 1
    # Async probe is awaited directly, NOT pushed through to_thread.
    assert used["to_thread"] == 0


@pytest.mark.asyncio
async def test_probe_timeout_marks_down():
    rec = EscalationRecorder()

    async def slow_to_thread(fn, *a, **kw):
        await asyncio.sleep(10)  # exceeds probe_timeout_s
        return fn(*a, **kw)

    runner = DependencyHealthRunner(
        producer=None, escalate_default=rec,
        clock=FakeClock(), sleep=asyncio.sleep, to_thread=slow_to_thread,
    )
    runner.register(DependencyCheck(
        name="dep", probe=lambda: ProbeResult.ok(), recover=None,
        interval_s=10, probe_timeout_s=0.05,
    ))
    await runner.run_check("dep")
    assert runner.state_of("dep") == DepState.ESCALATED  # DOWN + no recover → escalate
    assert "timed out" in rec.calls[0][2]


# ──────────────────────────────────────────────────────────────
# tick(): interval + overlap guard
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tick_respects_interval_and_overlap_guard():
    runner, clock, _slept = make_runner()
    ran: list[str] = []

    async def slow_probe_cycle():
        ran.append("x")
        await asyncio.sleep(0)

    # Register a check; monkeypatch its _run_check_guarded path is awkward, so
    # instead drive tick() with a captured create_task that runs the coro now.
    runner.register(DependencyCheck(name="dep", probe=lambda: ProbeResult.ok(), interval_s=100))

    created: list[asyncio.Task] = []

    def create_task(coro, name=None):
        t = asyncio.ensure_future(coro)
        created.append(t)
        return t

    # First tick: due (last_run was set to clock - (interval - small) at register? no —
    # _DepRuntime starts with last_run=0, so now - 0 >> interval → due).
    tasks = runner.tick(create_task=create_task)
    assert len(tasks) == 1
    await asyncio.gather(*created)

    # Immediately tick again: interval not elapsed → no new task.
    created.clear()
    tasks = runner.tick(create_task=create_task)
    assert tasks == []

    # Advance past interval → due again.
    clock.advance(101)
    tasks = runner.tick(create_task=create_task)
    assert len(tasks) == 1
    await asyncio.gather(*created)


@pytest.mark.asyncio
async def test_tick_suppresses_overlapping_run():
    runner, clock, _slept = make_runner()
    runner.register(DependencyCheck(name="dep", probe=lambda: ProbeResult.ok(), interval_s=1))
    rt = runner._checks["dep"]
    rt.running = True  # pretend a cycle is in flight
    clock.advance(100)

    def create_task(coro, name=None):
        coro.close()  # don't actually run it
        return None

    tasks = runner.tick(create_task=create_task)
    assert tasks == []  # overlap guard suppressed it


# ──────────────────────────────────────────────────────────────
# escalation SMS callback
# ──────────────────────────────────────────────────────────────

def test_make_sms_escalator_sends_specific_message():
    sent: list[tuple[str, str]] = []
    esc = make_sms_escalator(lambda phone, msg: sent.append((phone, msg)), "+15555550100")
    esc("chrome_control", "ESCALATED",
        "`chrome reset` exited 1 — extension errored. Reload at chrome://extensions/ → Developer mode → reload ⟳",
        {"recover_rc": 1, "disable_reason": "needs a reload"})
    assert len(sent) == 1
    phone, msg = sent[0]
    assert phone == "+15555550100"
    assert "chrome_control" in msg
    assert "ESCALATED" in msg
    assert "chrome://extensions/" in msg
    assert "recover_rc=1" in msg
    assert "disable_reason=needs a reload" in msg


def test_make_sms_escalator_no_phone_is_log_only():
    sent: list = []
    esc = make_sms_escalator(lambda phone, msg: sent.append((phone, msg)), None)
    esc("dep", "ESCALATED", "down", {})
    assert sent == []  # no phone → no SMS, just logs


def test_make_sms_escalator_swallows_send_failure():
    def boom(phone, msg):
        raise RuntimeError("send-sms broke")
    esc = make_sms_escalator(boom, "+15555550100")
    # Must not raise.
    esc("dep", "ESCALATED", "down", {})


# ──────────────────────────────────────────────────────────────
# bus events emitted on transitions
# ──────────────────────────────────────────────────────────────

class FakeProducer:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def send(self, topic, payload, key=None, type=None, source=None, headers=None):
        self.events.append({"topic": topic, "type": type, "source": source, "key": key, "payload": payload})


@pytest.mark.asyncio
async def test_emits_health_dependency_events_on_transitions():
    prod = FakeProducer()
    rec = EscalationRecorder()
    runner, _clock, _slept = make_runner(producer=prod, escalate=rec)
    runner.register(DependencyCheck(
        name="dep", probe=lambda: ProbeResult.down("wedged", probe_rc=2),
        recover=lambda: RecoverResult.fail("nope", recover_rc=3),
        interval_s=10, max_attempts=2, backoff_s=(0, 5),
    ))
    await runner.run_check("dep")
    types = [e["type"] for e in prod.events]
    # All on the system topic, source=dependency_health.
    assert all(e["topic"] == "system" and e["type"] == "health.dependency"
               and e["source"] == "dependency_health" for e in prod.events)
    # HEALTHY→DOWN, then 2× RECOVERING, then →ESCALATED.
    states = [e["payload"]["state"] for e in prod.events]
    assert states[0] == "DOWN"
    assert "RECOVERING" in states
    assert states[-1] == "ESCALATED"
    # schema_v + key present.
    assert all(e["payload"]["schema_v"] == 1 for e in prod.events)
    assert all(e["key"] == "dep" for e in prod.events)
    # Diagnostics flattened into payloads.
    assert any(e["payload"].get("probe_rc") == 2 for e in prod.events)
    assert any(e["payload"].get("recover_rc") == 3 for e in prod.events)
    # action_taken vocabulary.
    actions = {e["payload"]["action_taken"] for e in prod.events}
    assert actions <= {"probe", "recover", "escalate", "none"}
