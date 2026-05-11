"""
Concrete `DependencyCheck` descriptors for the daemon's external dependencies.

`build_default_registry(manager)` returns a `DependencyHealthRunner` with the
migrated checks registered:

  - chrome_control : `chrome ping` → `chrome reset`; gated on Chrome.app running;
                     escalate on the errored-extension state (the one true
                     human-required case) with the exact fix from `chrome wake`.
  - signal_cli     : JSON-RPC socket reachable + daemon process alive →
                     restart the signal-cli daemon with `--receive-mode on-connection`.
  - bus_consumers  : ConsumerRunner thread alive + no DEAD members in the bus
                     consumer registry → restart the consumer thread.
  - disk           : APFS-aware disk usage; recovery = clean known temp dirs;
                     else escalate.
  - fd_leak        : untracked FD growth via ResourceRegistry; no auto-recovery
                     (a real FD leak needs a code fix) → escalate.

The probe/recover callables capture the `Manager` instance via closures so they
can reach `_send_sms`, the consumer thread, the resource registry, etc. — but
they never touch the asyncio loop (they run in worker threads).

`sdk_sessions` is intentionally NOT migrated here — `health.py`'s fast/deep
verdict logic is load-bearing and stays as the daemon's own periodic check. See
the module docstring note + the agent report.
"""
from __future__ import annotations

import logging
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

from assistant.common import SIGNAL_CLI, SIGNAL_SOCKET
from assistant.dependency_health import (
    DependencyCheck,
    DependencyHealthRunner,
    ProbeResult,
    RecoverResult,
    make_sms_escalator,
)
from assistant.health import (
    CHROME_CLI,
    CHROME_PING_TIMEOUT,
    CHROME_RESET_TIMEOUT,
    _chrome_app_running,
    check_disk_space,
)

log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────
# chrome_control
# ──────────────────────────────────────────────────────────────

# Signals in `chrome wake`/`chrome reset` output that mean "Chrome marked the
# extension errored/disabled — only a manual reload at chrome://extensions/ fixes it".
_ERRORED_EXTENSION_SIGNALS = (
    "extension DISABLED",
    "extension DISABLED/errored",
    "did not come back within 15s",
    "reload",  # broad; combined with rc!=0 below
)

_CHROME_RELOAD_FIX = (
    "Reload chrome-control once: open chrome://extensions/ → enable Developer mode "
    "→ find \"Chrome Control\" → click the reload ⟳ icon (or toggle it off/on). "
    "After that one manual reload it self-heals again."
)


def _chrome_probe() -> ProbeResult:
    """`chrome ping` with a short timeout. Healthy iff rc==0 and 'Connected ...'."""
    if not CHROME_CLI.exists():
        # Treat a missing CLI as SKIP rather than DOWN — nothing the daemon can do.
        return ProbeResult.skip(f"chrome CLI not found at {CHROME_CLI}")
    try:
        p = subprocess.run(
            [str(CHROME_CLI), "ping"],
            capture_output=True, text=True, timeout=CHROME_PING_TIMEOUT,
        )
        out = ((p.stdout or "") + (p.stderr or "")).strip()
        if p.returncode == 0 and "Connected to Chrome Control extension" in out:
            return ProbeResult.ok("chrome ping ok", probe_rc=0, probe_output=out)
        return ProbeResult.down(
            f"chrome ping unhealthy (rc={p.returncode})",
            probe_rc=p.returncode, probe_output=out, probe_timed_out=False,
        )
    except subprocess.TimeoutExpired:
        return ProbeResult.down(
            f"chrome ping timed out after {CHROME_PING_TIMEOUT}s",
            probe_timed_out=True, probe_output=f"timed out after {CHROME_PING_TIMEOUT}s",
        )
    except Exception as e:  # noqa: BLE001
        return ProbeResult.down(f"chrome ping errored: {e}", probe_error=str(e))


def _chrome_recover() -> RecoverResult:
    """`chrome reset` — self-heals: kills stuck native_host, runs `chrome wake`.

    Exit 0 → actually reconnected (the CLI was hardened to only exit 0 on real
    reconnect, commit dbe721b). Non-zero → likely the errored-extension state →
    escalate with the exact fix.
    """
    if not CHROME_CLI.exists():
        return RecoverResult.fail(f"chrome CLI not found at {CHROME_CLI}")
    try:
        p = subprocess.run(
            [str(CHROME_CLI), "reset"],
            capture_output=True, text=True, timeout=CHROME_RESET_TIMEOUT,
        )
        out = ((p.stdout or "") + (p.stderr or "")).strip()
        if p.returncode == 0:
            return RecoverResult.ok("chrome reset reconnected", recover_rc=0, recover_output=out)
        # Non-zero → look for the errored-extension signal; if present, the only
        # fix is a manual reload — escalate immediately with the precise ask.
        disable_reason = _extract_disable_reason(out)
        errored = any(sig in out for sig in _ERRORED_EXTENSION_SIGNALS) or "errored" in out.lower()
        detail = (
            f"`chrome reset` exited {p.returncode} — chrome-control extension appears errored/disabled. "
            + _CHROME_RELOAD_FIX
        )
        if disable_reason:
            detail += f" (Chrome disable reason: {disable_reason})"
        return RecoverResult.fail(
            detail,
            escalate_now=errored,
            recover_rc=p.returncode, recover_output=out,
            **({"disable_reason": disable_reason} if disable_reason else {}),
        )
    except subprocess.TimeoutExpired:
        return RecoverResult.fail(
            f"`chrome reset` timed out after {CHROME_RESET_TIMEOUT}s",
            recover_timed_out=True, recover_output=f"timed out after {CHROME_RESET_TIMEOUT}s",
        )
    except Exception as e:  # noqa: BLE001
        return RecoverResult.fail(f"`chrome reset` errored: {e}", recover_error=str(e))


def _extract_disable_reason(text: str) -> str:
    """Pull a human-readable disable reason out of `chrome wake`'s output, if any.

    `chrome wake` prints things like '... extension DISABLED/errored (reason: needs a
    reload).' — extract the parenthetical.
    """
    import re
    m = re.search(r"reason[s]?:\s*([^).]+)", text, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return ""


# ──────────────────────────────────────────────────────────────
# signal_cli
# ──────────────────────────────────────────────────────────────

def _signal_probe(manager: Any) -> ProbeResult:
    """Healthy iff the daemon child process is alive AND the JSON-RPC socket is connectable."""
    daemon = getattr(manager, "signal_daemon", None)
    if daemon is None:
        return ProbeResult.skip("signal-cli daemon not managed (signal disabled or not started)")
    rc = daemon.poll()
    if rc is not None:
        return ProbeResult.down(f"signal-cli daemon process exited (rc={rc})", probe_rc=rc)
    if not SIGNAL_SOCKET.exists():
        return ProbeResult.down(f"signal-cli socket missing at {SIGNAL_SOCKET}")
    # Socket file exists — confirm something is actually listening.
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(3.0)
        s.connect(str(SIGNAL_SOCKET))
        s.close()
        return ProbeResult.ok("signal-cli daemon alive + socket connectable")
    except OSError as e:
        return ProbeResult.down(f"signal-cli socket exists but not connectable: {e}",
                                probe_error=str(e))


def _signal_recover(manager: Any) -> RecoverResult:
    """Restart the signal-cli daemon — with `--receive-mode on-connection` (critical).

    Delegates to `Manager._spawn_signal_daemon()` (which already passes the right
    flags) and `_start_signal_listener()`. Runs in a worker thread; both are
    synchronous.
    """
    try:
        # If the old process is still around, terminate it cleanly first.
        old = getattr(manager, "signal_daemon", None)
        if old is not None and old.poll() is None:
            try:
                old.terminate()
                old.wait(timeout=5)
            except Exception:
                try:
                    old.kill()
                except Exception:
                    pass
        proc = manager._spawn_signal_daemon()
        if not proc:
            return RecoverResult.fail("signal-cli daemon respawn failed (socket never came up)")
        manager.signal_daemon = proc
        try:
            manager._start_signal_listener()
        except Exception as e:  # noqa: BLE001
            return RecoverResult.fail(f"signal-cli respawned (pid={proc.pid}) but listener restart failed: {e}",
                                      recover_pid=proc.pid)
        return RecoverResult.ok(f"signal-cli daemon restarted (pid={proc.pid}, --receive-mode on-connection)",
                                recover_pid=proc.pid)
    except Exception as e:  # noqa: BLE001
        return RecoverResult.fail(f"signal-cli restart errored: {e}", recover_error=str(e))


# ──────────────────────────────────────────────────────────────
# bus_consumers
# ──────────────────────────────────────────────────────────────

def _bus_consumers_probe(manager: Any) -> ProbeResult:
    """Healthy iff the ConsumerRunner thread is alive and no DEAD members in the registry."""
    thread = getattr(manager, "_consumer_thread", None)
    if thread is not None and not thread.is_alive():
        return ProbeResult.down("bus ConsumerRunner thread is not alive")
    bus = getattr(manager, "_bus", None)
    if bus is None:
        # Thread alive but no bus handle to inspect — best-effort OK.
        return ProbeResult.ok("bus ConsumerRunner thread alive (registry not inspected)")
    try:
        groups = bus.list_consumer_groups()
    except Exception as e:  # noqa: BLE001
        return ProbeResult.degraded(f"could not inspect consumer registry: {e}", probe_error=str(e))
    dead = []
    for g in groups:
        for m in g.get("members", []):
            if not m.get("alive", True):
                dead.append(f"{g['group_id']}:{m['consumer_id']}")
    if dead:
        return ProbeResult.down(f"DEAD consumer members: {', '.join(dead)}", dead_members=dead)
    return ProbeResult.ok(f"bus consumers healthy ({len(groups)} groups)")


def _bus_consumers_recover(manager: Any) -> RecoverResult:
    """Rebuild the ConsumerRunner and restart its background thread."""
    try:
        # Stop the current runner (best-effort) so its DB connections are released.
        runner = getattr(manager, "_consumer_runner", None)
        if runner is not None:
            try:
                runner.stop()
            except Exception:
                pass
        # Rebuild + restart the thread.
        if hasattr(manager, "_init_consumers"):
            manager._consumer_runner = manager._init_consumers()
        if hasattr(manager, "_start_consumer_thread"):
            manager._consumer_thread = manager._start_consumer_thread()
            return RecoverResult.ok("bus ConsumerRunner rebuilt + thread restarted")
        return RecoverResult.fail("manager has no _start_consumer_thread — cannot restart")
    except Exception as e:  # noqa: BLE001
        return RecoverResult.fail(f"bus consumer restart errored: {e}", recover_error=str(e))


# ──────────────────────────────────────────────────────────────
# disk
# ──────────────────────────────────────────────────────────────

# Temp dirs we own and may safely clean when disk is critical (oldest-first).
_CLEANABLE_TEMP_GLOBS = [
    ("/tmp", "agent-output-*.txt"),
    ("/tmp", "*-sample.txt"),
]


def _disk_probe() -> ProbeResult:
    d = check_disk_space()
    if d["critical"]:
        return ProbeResult.down(d["message"] or "disk critical",
                                used_pct=d["used_pct"], free_gb=d["free_gb"])
    if d["warning"]:
        return ProbeResult.degraded(d["message"] or "disk warning",
                                    used_pct=d["used_pct"], free_gb=d["free_gb"])
    return ProbeResult.ok(f"disk ok ({d['used_pct']}% used, {d['free_gb']}GB free)")


def _disk_recover() -> RecoverResult:
    """Clean known temp dirs before escalating. If still critical, escalate."""
    freed = 0
    removed = 0
    for base, pattern in _CLEANABLE_TEMP_GLOBS:
        try:
            for p in Path(base).glob(pattern):
                try:
                    if p.is_file():
                        freed += p.stat().st_size
                        p.unlink()
                        removed += 1
                except OSError:
                    pass
        except OSError:
            pass
    d = check_disk_space()
    if d["critical"]:
        return RecoverResult.fail(
            f"cleaned {removed} temp files ({freed // (1024*1024)}MB) but disk still critical "
            f"({d['used_pct']}% used, {d['free_gb']}GB free) — needs a human to free space",
            freed_mb=freed // (1024 * 1024), removed=removed,
            used_pct=d["used_pct"], free_gb=d["free_gb"],
        )
    return RecoverResult.ok(
        f"cleaned {removed} temp files ({freed // (1024*1024)}MB); disk now {d['used_pct']}% used",
        freed_mb=freed // (1024 * 1024), removed=removed,
    )


# ──────────────────────────────────────────────────────────────
# fd_leak
# ──────────────────────────────────────────────────────────────

def _fd_leak_probe(manager: Any) -> ProbeResult:
    """Untracked FD growth via the ResourceRegistry's calibrated baseline."""
    reg = getattr(manager, "_resource_registry", None)
    if reg is None:
        return ProbeResult.skip("ResourceRegistry not initialized yet")
    try:
        status = reg.get_status()
    except Exception as e:  # noqa: BLE001
        return ProbeResult.degraded(f"could not read FD status: {e}", probe_error=str(e))
    actual = status.get("fd_actual", 0)
    baseline = status.get("fd_baseline", 0)
    tracked = status.get("fd_tracked", 0)
    delta = actual - baseline - tracked
    # Mirror health.py's thresholds: warn at +40 untracked, alarm at 200 absolute.
    if actual > 240 or delta > 80:
        return ProbeResult.down(
            f"FD leak: {actual} open ({delta} untracked over baseline {baseline}+{tracked})",
            fd_actual=actual, fd_baseline=baseline, fd_tracked=tracked, fd_delta=delta,
        )
    if actual > 200 or delta > 40:
        return ProbeResult.degraded(
            f"FD growth: {actual} open ({delta} untracked over baseline {baseline}+{tracked})",
            fd_actual=actual, fd_baseline=baseline, fd_tracked=tracked, fd_delta=delta,
        )
    return ProbeResult.ok(f"FDs ok ({actual} open, {delta} untracked)")


# ──────────────────────────────────────────────────────────────
# Registry builder
# ──────────────────────────────────────────────────────────────

def build_default_registry(manager: Any) -> DependencyHealthRunner:
    """Construct the DependencyHealthRunner with all migrated checks registered.

    `manager` is the live Manager instance — checks capture it for `_send_sms`,
    the consumer thread, the resource registry, signal daemon, etc.
    """
    from assistant import config as _config

    producer = getattr(manager, "_producer", None)
    admin_phone = None
    try:
        admin_phone = _config.get("owner.phone")
    except Exception:
        admin_phone = None

    def _send_sms(phone: str, message: str) -> Any:
        # Manager._send_sms shells out to send-sms (does NOT route through a chat session).
        return manager._send_sms(phone, message)

    escalator = make_sms_escalator(_send_sms, admin_phone)
    runner = DependencyHealthRunner(producer=producer, escalate_default=escalator)

    # ── chrome_control ────────────────────────────────────────
    runner.register(DependencyCheck(
        name="chrome_control",
        probe=_chrome_probe,
        recover=_chrome_recover,
        interval_s=90,                       # tight: catch a wedged MV3 SW in ~1-2 min
        max_attempts=3,
        backoff_s=(0, 30, 90),               # `chrome reset` is heavy; modest waits
        enabled_when=_chrome_app_running,    # never relaunch a Chrome the user closed
        probe_timeout_s=CHROME_PING_TIMEOUT + 5,
        recover_timeout_s=CHROME_RESET_TIMEOUT + 10,
        recovery_alarm_k=5,
    ))

    # ── signal_cli ────────────────────────────────────────────
    runner.register(DependencyCheck(
        name="signal_cli",
        probe=lambda: _signal_probe(manager),
        recover=lambda: _signal_recover(manager),
        interval_s=300,                      # matches the prior 5-min cadence
        max_attempts=3,
        backoff_s=(0, 30, 120),
        probe_timeout_s=10.0,
        recover_timeout_s=45.0,              # Java daemon is slow to start
        recovery_alarm_k=4,
    ))

    # ── bus_consumers ─────────────────────────────────────────
    runner.register(DependencyCheck(
        name="bus_consumers",
        probe=lambda: _bus_consumers_probe(manager),
        recover=lambda: _bus_consumers_recover(manager),
        interval_s=300,
        max_attempts=3,
        backoff_s=(0, 30, 120),
        probe_timeout_s=10.0,
        recover_timeout_s=30.0,
        recovery_alarm_k=4,
    ))

    # ── disk ──────────────────────────────────────────────────
    runner.register(DependencyCheck(
        name="disk",
        probe=_disk_probe,
        recover=_disk_recover,
        interval_s=300,
        max_attempts=2,
        backoff_s=(0, 60),
        probe_timeout_s=10.0,
        recover_timeout_s=30.0,
        recovery_alarm_k=3,
        reescalate_after_s=6 * 3600,         # disk problems are slow; don't nag hourly
    ))

    # ── fd_leak ───────────────────────────────────────────────
    runner.register(DependencyCheck(
        name="fd_leak",
        probe=lambda: _fd_leak_probe(manager),
        recover=None,                        # a real FD leak needs a code fix → escalate
        interval_s=300,
        probe_timeout_s=10.0,
        reescalate_after_s=6 * 3600,
    ))

    return runner
