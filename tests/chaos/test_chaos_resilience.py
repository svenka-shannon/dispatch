"""Chaos / resilience smoke tests — Phase 5 of ``plans/self-healing-resilience.md``.

Each test follows the same shape:

    induce a failure  →  poll for recovery within the §3 SLO  →  assert

SLO budgets (``plans/self-healing-resilience.md`` §3):
  * Detection           ≤ ~2 min  (independent of a chat session / human hitting it)
  * Auto-recovery start  ≤ ~30 s after detection
  * Escalation (if it genuinely can't recover) ≤ ~5 min from first detection

For chrome-control the daemon's ``chrome_control`` check runs every ~90 s, so
the end-to-end chrome budget is ~2–3 min. We always use a polling loop with a
generous timeout — never a fixed sleep.

────────────────────────────────────────────────────────────────────────────
SAFETY — read before running anything in this file
────────────────────────────────────────────────────────────────────────────
EVERYTHING is gated. The ``@requires_live`` tests need ``CLAUDE_LIVE_TESTS=1``;
the most disruptive ones (quit Chrome.app, kill signal-cli, crash the bus
consumer) ALSO need ``CLAUDE_CHAOS_DESTRUCTIVE=1`` (``@requires_destructive``).
With no env vars set, only the mocked/structural tests run — the destructive
bodies are skipped. Use ``scripts/chaos-test.sh`` (which prints a loud warning
and requires ``--yes-i-mean-it`` for the destructive ones) rather than invoking
pytest directly.

A deliberate live chaos run should happen ONLY:
  1. after ``claude-assistant restart`` (so the Phase 1/2 self-healing loops
     are actually live in the running daemon), and
  2. after ``chrome reload-extension`` (so chrome-control is in a known-good
     state), and
  3. when no chat session is mid-task (these kill Chrome / signal-cli / the
     consumer on THIS machine) and someone is around to babysit it.

Several tests below are written as **mostly-mocked** because doing them live is
either unsafe to author cleanly (errored-extension simulation, FD exhaustion of
the live daemon) or doesn't need a real kill to be meaningful — those are
clearly labelled with ``MOCKED:`` / ``AUTHORED, UNSAFE TO RUN LIVE HERE:`` in
their docstrings.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tests.chaos.conftest import (  # noqa: E402  (conftest is the helper module here)
    BUS_DB,
    CHROME_CLI,
    NATIVE_HOST_PATTERN,
    SIGNAL_SOCKET,
    chrome_app_running,
    chrome_ping_connected,
    chrome_reset,
    dependency_events,
    pgrep_count,
    pgrep_pids,
    poll_until,
    requires_destructive,
    requires_live,
    socket_connectable,
)

# Every test in this module is part of the chaos suite (so it can be selected /
# excluded with `-m chaos`). Asyncio tests additionally carry the asyncio marker
# explicitly so they work under the project's strict asyncio_mode.
pytestmark = [pytest.mark.chaos]


# Budgets (seconds). Generous — these are SLO ceilings, not targets.
DETECT_BUDGET = 130          # ~2 min detection
RECOVER_AFTER_DETECT = 45    # ~30 s recovery start (slack for subprocess spawn)
ESCALATE_BUDGET = 320        # ~5 min from first detection
CHROME_E2E_BUDGET = 200      # ~3 min: 90 s check cadence + chrome reset + reconnect


# ════════════════════════════════════════════════════════════════════════
# 1. Chrome service-worker eviction  (LIVE — moderately disruptive)
# ════════════════════════════════════════════════════════════════════════

@requires_live
def test_chrome_sw_eviction_recovers_via_chrome_reset() -> None:
    """LIVE: simulate a wedged MV3 service worker by killing the ``native_host.py``
    process(es), then assert ``chrome ping`` is "Connected" again within the
    chrome SLO.

    This is the *daemon-independent* variant: we call ``chrome reset`` directly
    (the same recovery the daemon's ``chrome_control`` check would run) and
    assert it actually reconnects (rc==0, hardened in dbe721b). A companion
    assertion checks ``health.dependency`` events for ``name=chrome_control`` if
    the daemon happens to have run its check in the window — but we don't *rely*
    on the daemon here so the test is robust to daemon-not-restarted-yet.
    """
    if not CHROME_CLI.exists():
        pytest.skip("chrome-control CLI not present")
    if not chrome_app_running():
        pytest.skip("Chrome.app is not running — nothing to wedge (this is not the test for that)")

    # Induce: kill the native host (simulates the SW eviction wedge).
    pids = pgrep_pids(NATIVE_HOST_PATTERN)
    for pid in pids:
        try:
            os.kill(pid, 9)
        except (ProcessLookupError, PermissionError):
            pass

    # Recover: chrome reset should self-heal (kills any stuck host, wakes the SW).
    cp = chrome_reset()
    print(f"\n[chaos] chrome reset rc={cp.returncode}\n{(cp.stdout or '') + (cp.stderr or '')}")

    # Assert: ping reconnects within the chrome E2E budget.
    ok = poll_until(chrome_ping_connected, timeout=CHROME_E2E_BUDGET, interval=3.0,
                    desc="chrome ping reconnect after reset")
    assert ok, (
        "chrome ping never returned 'Connected' within the SLO after `chrome reset`; "
        f"reset rc={cp.returncode}, output={(cp.stdout or '') + (cp.stderr or '')!r}. "
        "If the extension is errored, this would need a manual reload at chrome://extensions/."
    )

    # Best-effort: if the daemon recorded a chrome_control incident in the
    # window, check it shows the DOWN→RECOVERING→HEALTHY shape. Don't fail the
    # test if the daemon hasn't been restarted with the Phase-2 loop yet.
    evs = dependency_events("chrome_control")
    if evs:
        states = [e["payload"].get("state") for e in reversed(evs[:30])]
        print(f"[chaos] recent chrome_control health.dependency states: {states}")
        # If we see any incident states at all, we expect HEALTHY to be the last one.
        if any(s in ("DOWN", "RECOVERING", "ESCALATED") for s in states):
            assert states[-1] in ("HEALTHY",), (
                f"chrome_control health.dependency trail ended in {states[-1]}, not HEALTHY: {states}"
            )
    else:
        print("[chaos] no health.dependency events for chrome_control yet "
              "(daemon not restarted with Phase-2 loop?) — skipping the bus-trail assertion")


# ════════════════════════════════════════════════════════════════════════
# 2. native_host kill  (LIVE — moderately disruptive)
# ════════════════════════════════════════════════════════════════════════

@requires_live
def test_native_host_kill_respawns_no_zombie_pileup() -> None:
    """LIVE: ``pkill -f chrome-control/scripts/native_host``; assert a fresh host
    respawns / ``chrome reset`` recovers it; assert NO zombie pileup
    (``pgrep -fc native_host`` settles back to 1 — the 28-vs-8 leak from the
    2026-05-11 outage must not recur).
    """
    if not CHROME_CLI.exists():
        pytest.skip("chrome-control CLI not present")
    if not chrome_app_running():
        pytest.skip("Chrome.app is not running")

    subprocess.run(["pkill", "-9", "-u", str(os.getuid()), "-f", NATIVE_HOST_PATTERN],
                   capture_output=True)

    # Let the SW respawn one, or force it via chrome reset.
    cp = chrome_reset()
    print(f"\n[chaos] chrome reset rc={cp.returncode}\n{(cp.stdout or '') + (cp.stderr or '')}")

    ok = poll_until(chrome_ping_connected, timeout=CHROME_E2E_BUDGET, interval=3.0,
                    desc="chrome ping reconnect after native_host kill")
    assert ok, "chrome never reconnected after native_host kill + reset"

    # No pileup: count must settle at exactly 1 (give it a few seconds to stabilise).
    def _exactly_one_host() -> bool:
        return pgrep_count(NATIVE_HOST_PATTERN) == 1

    settled = poll_until(_exactly_one_host, timeout=20.0, interval=2.0,
                         desc="native_host count settles to 1")
    n = pgrep_count(NATIVE_HOST_PATTERN)
    assert settled, f"native_host process count is {n}, expected exactly 1 (zombie pileup?)"


# ════════════════════════════════════════════════════════════════════════
# 3. Chrome.app quit  (LIVE + DESTRUCTIVE — most disruptive)
# ════════════════════════════════════════════════════════════════════════

@requires_destructive
def test_chrome_app_quit_check_skips_does_not_relaunch() -> None:
    """LIVE+DESTRUCTIVE: quit Chrome.app; assert the ``chrome_control`` check
    SKIPs (state stays HEALTHY/SKIP, recovery NOT attempted — the daemon must
    NOT relaunch a Chrome the user closed). Then relaunch Chrome + ``chrome
    wake`` to restore.

    This relies on the daemon's ``enabled_when=_chrome_app_running`` gate, so it
    only meaningfully passes when the daemon has been restarted with the Phase-2
    loop. If it hasn't, we still assert the *negative* property structurally:
    no ``health.dependency`` event with ``action_taken=recover`` for
    ``chrome_control`` appears while Chrome is down.
    """
    if not CHROME_CLI.exists():
        pytest.skip("chrome-control CLI not present")
    if not chrome_app_running():
        pytest.skip("Chrome.app is not running — can't test the quit path")

    # Snapshot the bus offset so we only look at events produced *after* the quit.
    n_before = len(dependency_events("chrome_control"))
    subprocess.run(["osascript", "-e", 'quit app "Google Chrome"'], capture_output=True)
    # Wait for it to actually be gone.
    assert poll_until(lambda: not chrome_app_running(), timeout=30.0, interval=1.0,
                      desc="Chrome.app quits"), "Chrome.app did not quit"

    try:
        # Watch for ~2.5 daemon check cycles (~230 s) to be sure a check ran.
        # During this window NO chrome_control recovery may be attempted: the
        # check's enabled_when gate must SKIP because Chrome.app isn't running.
        def _recovery_attempted_since_quit() -> bool:
            evs = dependency_events("chrome_control")
            new = evs[: max(0, len(evs) - n_before)] if len(evs) > n_before else []
            return any(e["payload"].get("action_taken") == "recover" for e in new)

        held = True
        deadline = time.time() + 230
        while time.time() < deadline:
            if _recovery_attempted_since_quit():
                held = False
                break
            time.sleep(15)
        assert held, (
            "the daemon attempted a chrome_control recovery while Chrome.app was "
            "QUIT — it must SKIP (enabled_when=_chrome_app_running), not relaunch a "
            "Chrome the user closed"
        )

        # If the daemon is running the Phase-2 loop, the runner state should be
        # HEALTHY (incident reset on SKIP) — we can't reach into the live daemon's
        # runner from a test process, so the bus-trail check above is our proxy.
        print("\n[chaos] no chrome_control recovery attempted while Chrome.app was quit — OK")
    finally:
        # Restore: relaunch Chrome and wake the extension.
        subprocess.run(["open", "-a", "Google Chrome"], capture_output=True)
        poll_until(chrome_app_running, timeout=30.0, interval=1.0, desc="Chrome.app relaunches")
        # Give the extension a beat to load, then wake it.
        time.sleep(5)
        try:
            subprocess.run([str(CHROME_CLI), "wake"], capture_output=True, text=True, timeout=60)
        except Exception:
            pass
        # Best-effort verify we're back; don't fail teardown if not.
        if poll_until(chrome_ping_connected, timeout=120.0, interval=3.0,
                      desc="chrome reconnects after relaunch"):
            print("[chaos] Chrome + extension restored")
        else:
            print("[chaos] WARNING: Chrome relaunched but extension not reconnected — "
                  "may need a manual `chrome reload-extension` or toolbar interaction")


# ════════════════════════════════════════════════════════════════════════
# 4. Extension disabled / errored  (MOCKED — unsafe to simulate live)
# ════════════════════════════════════════════════════════════════════════

def test_errored_extension_escalates_not_recovers() -> None:
    """MOCKED: when the chrome-control extension is errored/disabled (Chrome
    stops auto-restarting it after ~5 SW crashes), ``chrome reset`` exits
    non-zero and the only fix is a *manual* reload at chrome://extensions/. The
    ``chrome_control`` check must NOT loop on recovery — it must go to ESCALATED
    and the SMS escalator must be invoked with the precise fix.

    We can't safely error the live extension from a test, so this is a
    mostly-mocked variant: we drive the real ``DependencyHealthRunner`` with a
    real ``chrome_control``-shaped check whose ``recover`` returns the
    errored-extension ``RecoverResult`` (``escalate_now=True``), and assert the
    escalator fires exactly once with a message that names the chrome://extensions
    reload step. The live variant would: (a) crash the SW ~6×, (b) confirm
    Chrome marked it errored in Preferences, (c) assert the daemon escalates —
    but doing (a) on the live machine is exactly the wedge we're hardening
    against, so we don't.
    """
    import asyncio

    from assistant.dependency_health import (
        DependencyCheck,
        DependencyHealthRunner,
        DepState,
        ProbeResult,
        RecoverResult,
    )

    escalations: list[tuple[str, str, str, dict]] = []

    def escalator(name: str, state: str, detail: str, diag: dict) -> None:
        escalations.append((name, state, detail, diag))

    async def fake_sleep(_s: float) -> None:  # no real waiting
        return None

    async def inline_to_thread(fn, *a, **kw):
        return fn(*a, **kw)

    runner = DependencyHealthRunner(
        producer=None, escalate_default=escalator,
        sleep=fake_sleep, to_thread=inline_to_thread,
    )

    # chrome ping is DOWN; chrome reset reports the errored-extension state.
    errored_fix = (
        "Reload chrome-control once: open chrome://extensions/ → enable Developer mode "
        "→ find \"Chrome Control\" → click the reload ⟳ icon"
    )
    runner.register(DependencyCheck(
        name="chrome_control",
        probe=lambda: ProbeResult.down("chrome ping unhealthy (rc=1)", probe_rc=1),
        recover=lambda: RecoverResult.fail(
            f"`chrome reset` exited 1 — chrome-control extension appears errored/disabled. {errored_fix}",
            escalate_now=True, recover_rc=1, disable_reason="needs a reload",
        ),
        interval_s=0,
        backoff_s=(0, 0, 0),
        enabled_when=lambda: True,  # Chrome.app *is* running — the extension is the problem
    ))

    asyncio.run(runner.run_check("chrome_control"))

    assert runner.state_of("chrome_control") == DepState.ESCALATED, (
        f"expected ESCALATED on errored extension, got {runner.state_of('chrome_control')}"
    )
    assert len(escalations) == 1, f"expected exactly one escalation, got {len(escalations)}"
    name, state, detail, diag = escalations[0]
    assert name == "chrome_control"
    assert "chrome://extensions" in detail, f"escalation must name the manual reload step: {detail!r}"
    assert "reload" in detail.lower()
    # The runner must NOT have looped recovery attempts (escalate_now short-circuits).
    snap = runner.status_snapshot()["chrome_control"]
    assert snap["attempt"] <= 1, f"errored extension should not retry recovery; attempt={snap['attempt']}"


def test_chrome_recover_classifies_errored_output_as_escalate_now() -> None:
    """MOCKED: the real ``assistant.dependency_checks._chrome_recover`` must, on a
    non-zero ``chrome reset`` whose output contains an errored-extension signal,
    return ``RecoverResult.fail(escalate_now=True, ...)`` with the manual-reload
    fix in the detail. Patches ``subprocess.run`` so nothing real is touched.
    """
    import assistant.dependency_checks as dc

    fake_cp = MagicMock()
    fake_cp.returncode = 1
    fake_cp.stdout = (
        "Killed 1 native_host process(es), removed 0 orphan socket(s).\n"
        "Service worker did not come back within 15s — the extension is "
        "DISABLED/errored. Reload it once at chrome://extensions/ (reason: needs a reload).\n"
    )
    fake_cp.stderr = ""

    if not dc.CHROME_CLI.exists():
        pytest.skip("chrome CLI not present — _chrome_recover early-returns before subprocess")
    orig_run = dc.subprocess.run
    try:
        dc.subprocess.run = lambda *a, **kw: fake_cp  # type: ignore[assignment]
        rec = dc._chrome_recover()
    finally:
        dc.subprocess.run = orig_run  # type: ignore[assignment]

    assert rec.success is False
    assert rec.escalate_now is True, "errored-extension output must request immediate escalation"
    assert "chrome://extensions" in rec.detail
    assert rec.diagnostics.get("recover_rc") == 1


# ════════════════════════════════════════════════════════════════════════
# 5. signal-cli kill  (LIVE + DESTRUCTIVE)
# ════════════════════════════════════════════════════════════════════════

@requires_destructive
def test_signal_cli_kill_restarts_with_on_connection_mode() -> None:
    """LIVE+DESTRUCTIVE: kill the signal-cli daemon child; assert the
    ``signal_cli`` check restarts it, that it came back with
    ``--receive-mode on-connection`` (critical for socket push), and that
    ``/tmp/signal-cli.sock`` is connectable again — all within the SLO.

    The ``signal_cli`` check runs every 300 s, so the budget here is generous
    (~6 min). If the daemon hasn't been restarted with the Phase-2 loop, this
    test will time out — that's the signal to run it *after* a daemon restart.
    """
    pids = pgrep_pids("signal-cli")
    if not pids:
        pytest.skip("no signal-cli process running — signal disabled or not started")

    for pid in pids:
        try:
            os.kill(pid, 9)
        except (ProcessLookupError, PermissionError):
            pass

    # Recover: the daemon's signal_cli check (≤300 s cadence + restart time).
    SIGNAL_BUDGET = 6 * 60 + 30
    came_back = poll_until(
        lambda: bool(pgrep_pids("signal-cli")) and socket_connectable(SIGNAL_SOCKET),
        timeout=SIGNAL_BUDGET, interval=5.0, desc="signal-cli restart + socket connectable",
    )
    assert came_back, (
        "signal-cli did not come back within the SLO. If the daemon hasn't been "
        "restarted with the Phase-2 dependency_health loop yet, that's expected — "
        "rerun after `claude-assistant restart`."
    )

    # Verify the flag: the relaunched process must use --receive-mode on-connection.
    new_pids = pgrep_pids("signal-cli")
    args_blob = ""
    for pid in new_pids:
        try:
            p = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, timeout=5)
            args_blob += (p.stdout or "")
        except Exception:
            pass
    assert "--receive-mode" in args_blob and "on-connection" in args_blob, (
        f"relaunched signal-cli is missing `--receive-mode on-connection`; argv={args_blob!r}"
    )

    # Bus trail (best-effort): a signal_cli DOWN→...→HEALTHY incident should be recorded.
    evs = dependency_events("signal_cli")
    if evs:
        states = [e["payload"].get("state") for e in reversed(evs[:20])]
        print(f"\n[chaos] recent signal_cli health.dependency states: {states}")
        if any(s in ("DOWN", "RECOVERING") for s in states):
            assert states[-1] == "HEALTHY", f"signal_cli trail ended in {states[-1]}: {states}"


# ════════════════════════════════════════════════════════════════════════
# 6. bus consumer killed  (LIVE + DESTRUCTIVE)
# ════════════════════════════════════════════════════════════════════════

@requires_destructive
def test_bus_consumer_crash_restarts_no_dead_members() -> None:
    """LIVE+DESTRUCTIVE: simulate a crashed bus consumer runner; assert the
    ``bus_consumers`` check restarts it (no DEAD members in
    ``bus.list_consumer_groups()`` afterward) within the SLO.

    We can't reach into the live daemon to kill its consumer *thread* from a
    test process, so the live induction here is necessarily indirect: we mark a
    consumer group member DEAD-equivalent by... actually we can't safely do that
    either without corrupting the live registry. So this test, when run live,
    asserts the *steady-state* invariant (no DEAD members) and prints the group
    snapshot — the real crash-and-recover assertion is covered by the mocked
    variant ``test_bus_consumers_recover_rebuilds_runner`` below. Documented as
    AUTHORED, UNSAFE TO INDUCE LIVE HERE.
    """
    if not BUS_DB.exists():
        pytest.skip("bus.db not present")
    from tests.chaos.conftest import bus_consumer_groups
    groups = bus_consumer_groups()
    if not groups:
        pytest.skip("no consumer groups registered")
    dead = []
    for g in groups:
        for m in g.get("members", []):
            if not m.get("alive", True):
                dead.append(f"{g.get('group_id')}:{m.get('consumer_id')}")
    print("\n[chaos] consumer groups: "
          + ", ".join(f"{g.get('group_id')}({len(g.get('members', []))} members)" for g in groups))
    assert not dead, f"DEAD consumer members present: {dead} — the bus_consumers check should reap these"


def test_bus_consumers_recover_rebuilds_runner() -> None:
    """MOCKED: the real ``assistant.dependency_checks._bus_consumers_recover``,
    given a Manager-shaped object with ``_init_consumers`` / ``_start_consumer_thread``,
    must stop the old runner, rebuild it, restart the thread, and report success.
    All Manager bits are MagicMocks — nothing real is touched.
    """
    import assistant.dependency_checks as dc

    manager = MagicMock()
    old_runner = MagicMock()
    manager._consumer_runner = old_runner
    new_runner = MagicMock()
    new_thread = MagicMock()
    manager._init_consumers.return_value = new_runner
    manager._start_consumer_thread.return_value = new_thread

    rec = dc._bus_consumers_recover(manager)

    assert rec.success is True, f"recovery should succeed: {rec.detail}"
    old_runner.stop.assert_called_once()
    manager._init_consumers.assert_called_once()
    manager._start_consumer_thread.assert_called_once()
    assert manager._consumer_runner is new_runner
    assert manager._consumer_thread is new_thread


def test_bus_consumers_probe_flags_dead_member() -> None:
    """MOCKED: ``_bus_consumers_probe`` returns DOWN when the registry has a DEAD
    member, OK when all alive. Drives the real probe with a fake bus object.
    """
    import assistant.dependency_checks as dc
    from assistant.dependency_health import ProbeStatus

    manager = MagicMock()
    manager._consumer_thread = MagicMock()
    manager._consumer_thread.is_alive.return_value = True
    bus = MagicMock()
    manager._bus = bus

    # all alive → OK
    bus.list_consumer_groups.return_value = [
        {"group_id": "g1", "members": [{"consumer_id": "c1", "alive": True}]},
    ]
    r_ok = dc._bus_consumers_probe(manager)
    assert r_ok.status == ProbeStatus.OK, r_ok

    # one dead → DOWN
    bus.list_consumer_groups.return_value = [
        {"group_id": "g1", "members": [{"consumer_id": "c1", "alive": True},
                                       {"consumer_id": "c2", "alive": False}]},
    ]
    r_down = dc._bus_consumers_probe(manager)
    assert r_down.status == ProbeStatus.DOWN, r_down
    assert "g1:c2" in r_down.diagnostics.get("dead_members", [])


# ════════════════════════════════════════════════════════════════════════
# 7. FD leak / exhaustion sim  (MOCKED — must not exhaust the live daemon's FDs)
# ════════════════════════════════════════════════════════════════════════

def test_fd_leak_probe_escalates_no_auto_recovery() -> None:
    """MOCKED: simulate untracked-FD growth past the ``fd_leak`` alarm threshold
    by handing ``_fd_leak_probe`` a ResourceRegistry-shaped object whose
    ``get_status()`` reports a large untracked delta. Assert the probe returns
    DOWN, and that the registered ``fd_leak`` check has ``recover is None`` (a
    real FD leak needs a code fix) so the framework escalates rather than
    looping a no-op recovery.

    Exhausting the *live daemon's* FDs would be genuinely harmful, so we never
    do that — we exercise the decision logic instead. (A truly-live variant
    would have to leak FDs inside the daemon process, which we won't.)
    """
    import asyncio

    import assistant.dependency_checks as dc
    from assistant.dependency_health import (
        DependencyCheck,
        DependencyHealthRunner,
        DepState,
        ProbeStatus,
    )

    # ── probe classification ──
    manager = MagicMock()
    reg = MagicMock()
    manager._resource_registry = reg
    reg.get_status.return_value = {"fd_actual": 300, "fd_baseline": 50, "fd_tracked": 10}
    r = dc._fd_leak_probe(manager)
    assert r.status == ProbeStatus.DOWN, f"big untracked FD delta should be DOWN: {r}"
    assert r.diagnostics.get("fd_actual") == 300

    # healthy case
    reg.get_status.return_value = {"fd_actual": 60, "fd_baseline": 50, "fd_tracked": 5}
    assert dc._fd_leak_probe(manager).status == ProbeStatus.OK

    # ── the registered fd_leak check has no recover → escalates ──
    runner = dc.build_default_registry(MagicMock())
    fd_rt = runner._checks["fd_leak"]  # noqa: SLF001 — inspecting the descriptor
    assert fd_rt.check.recover is None, "fd_leak must have no auto-recovery (needs a code fix)"

    # Drive a cycle through a runner where the probe says DOWN — must ESCALATE.
    escalations: list = []

    async def fake_sleep(_s): return None
    async def inline_to_thread(fn, *a, **kw): return fn(*a, **kw)

    r2 = DependencyHealthRunner(producer=None, escalate_default=lambda *a: escalations.append(a),
                                sleep=fake_sleep, to_thread=inline_to_thread)
    r2.register(DependencyCheck(
        name="fd_leak",
        probe=lambda: dc.ProbeResult.down("FD leak: 300 open (240 untracked over baseline 50+10)",
                                          fd_actual=300),
        recover=None,
        interval_s=0,
    ))
    asyncio.run(r2.run_check("fd_leak"))
    assert r2.state_of("fd_leak") == DepState.ESCALATED
    assert len(escalations) == 1, "fd_leak with no recovery must escalate exactly once on first DOWN"


# ════════════════════════════════════════════════════════════════════════
# 8. Stuck-session sim  (MOCKED via test doubles + a structural live check)
# ════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_stuck_session_with_commitment_gets_nudged_mocked(tmp_path, monkeypatch) -> None:
    """MOCKED: re-exercise the Phase-1 blocked-session watchdog with the test
    doubles from ``tests/test_stuck_watchdog.py`` — a session idle > N min with a
    commitment marker gets a SYSTEM NUDGE injected, and the watchdog records the
    nudge timestamp (the ``session.stuck_nudge`` bus event is emitted via the
    module-level helper). This is the chaos-suite copy of the Phase-1 test so a
    ``chaos`` run also exercises the stuck-session path; the *live* timing
    variant is ``test_stuck_session_live_timing`` below.
    """
    import importlib
    from datetime import datetime, timedelta

    from assistant import commitments as cm
    importlib.reload(cm)
    monkeypatch.setattr(cm, "COMMITMENTS_DIR", tmp_path / "commitments")

    from assistant.manager import Manager

    class _FakeQueue:
        def qsize(self): return 0

    class _FakeSession:
        def __init__(self):
            self._session_name = "imessage/_15555550100"
            self.chat_id = "+15555550100"
            self.source = "imessage"
            self.contact_name = "Chaos Tester"
            self.is_busy = False
            self._message_queue = _FakeQueue()
            self.last_activity = datetime.now() - timedelta(minutes=12)
            self.last_assistant_text = "On it — waiting on you to reload chrome-control."
            self.injected: list[str] = []
        def is_alive(self): return True
        async def inject(self, text): self.injected.append(text)

    sess = _FakeSession()
    cm.set_commitment("imessage/_15555550100", "waiting on the user to reload chrome-control")

    m = MagicMock(spec=Manager)
    m._producer = MagicMock()
    m._shutdown_flag = False
    m._stuck_check_running = False
    m._last_stuck_nudge_at = {}
    m.sessions = MagicMock()
    m.sessions.sessions = {"+15555550100": sess}
    m._stuck_session_idle_minutes = MagicMock(return_value=5.0)

    await Manager._run_stuck_session_check.__get__(m, Manager)()

    assert len(sess.injected) == 1, "idle session with commitment should be nudged exactly once"
    assert "SYSTEM NUDGE" in sess.injected[0]
    assert "reload chrome-control" in sess.injected[0]
    assert "imessage/_15555550100" in m._last_stuck_nudge_at


@requires_live
def test_stuck_session_live_timing() -> None:
    """LIVE: structural — verify the daemon's blocked-session watchdog is wired
    and the commitment-marker store + CLI exist, then (if a real idle session
    with a commitment exists) check that a ``session.stuck_nudge`` event has
    fired within the configured threshold.

    We do NOT *create* a real stuck session here (that would require parking a
    live chat session for >5 min and is not worth the disruption). Instead we
    assert the plumbing: ``Manager._run_stuck_session_check`` exists, the
    ``commitments`` module + ``claude-assistant commitment`` subcommand exist,
    and — if the bus shows any recent ``session.stuck_nudge`` events — they have
    the expected shape. The full timing assertion belongs in a babysat live run
    where you intentionally set a commitment on a test session and let it idle.
    """
    from assistant.manager import Manager
    assert hasattr(Manager, "_run_stuck_session_check"), "blocked-session watchdog not wired into Manager"

    from assistant import commitments as cm
    for fn in ("set_commitment", "clear_commitment", "get_commitment", "looks_like_commitment"):
        assert hasattr(cm, fn), f"commitments module missing {fn}"

    # CLI subcommand present?
    cli_help = subprocess.run(
        ["uv", "run", "python", "-m", "assistant.cli", "commitment", "--help"],
        capture_output=True, text=True, cwd=str(Path.home() / "dispatch"), timeout=60,
    )
    assert cli_help.returncode == 0 and "set" in cli_help.stdout and "clear" in cli_help.stdout, (
        f"`claude-assistant commitment` subcommand not available: {cli_help.stdout}{cli_help.stderr}"
    )

    # If the bus has recent stuck_nudge events, sanity-check their shape.
    from tests.chaos.conftest import read_records
    nudges = read_records(topic="sessions", type_="session.stuck_nudge", limit=20)
    for e in nudges:
        p = e["payload"]
        # We don't know the exact payload schema across versions; just make sure
        # it references a session/chat in some way.
        assert any(k in p for k in ("session_name", "chat_id", "session", "chat")), (
            f"session.stuck_nudge payload looks malformed: {p}"
        )
    print(f"\n[chaos] blocked-session watchdog wired; {len(nudges)} recent stuck_nudge event(s) on the bus")
