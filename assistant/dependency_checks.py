"""
Concrete `DependencyCheck` descriptors for the daemon's external dependencies.

`build_default_registry(manager)` returns a `DependencyHealthRunner` with the
migrated checks registered:

  - chrome_control : `chrome ping` → `chrome reset` → `chrome-heal --rung ax`
                     (AX-driven reload of the errored extension — the state that
                     previously required a human at chrome://extensions/); gated
                     on Chrome.app running; escalate only if the automated
                     reload also fails, with a rung-specific ask.
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

import json
import logging
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

from assistant.common import AUTH_FAILURE_FLAG, SIGNAL_SOCKET
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

# Automated equivalent of the manual reload: drives chrome://extensions/ via the
# macOS Accessibility API and presses the card's reload button. Runs as rung 2
# of the recover chain, after `chrome reset` fails with the errored-extension
# signal. Requires the daemon's python to be Accessibility-trusted (it is —
# TCC grants by responsible process, and the launchd-spawned daemon binary
# carries the grant).
CHROME_HEAL = Path.home() / "dispatch/scripts/chrome-heal"
CHROME_HEAL_TIMEOUT = 150  # page load + AX tree walk + reload + wake + ping polls

# chrome-heal failure prefixes → what the admin should actually do. Only
# AX_RELOAD_NO_RECOVERY genuinely needs hands on the machine.
_CHROME_HEAL_FIXES = {
    "AX_UNTRUSTED": (
        "chrome-heal has no Accessibility permission in the daemon's context — grant "
        "Accessibility to /Users/svenka/dispatch/.venv/bin/python (System Settings → "
        "Privacy & Security → Accessibility), then `claude-assistant restart`."
    ),
    "AX_NO_WEBAREA": (
        "chrome://extensions/ didn't render in Chrome's AX tree — a modal dialog is "
        "probably blocking Chrome's window. Check the screen (screen-sharing works) "
        "or restart Chrome."
    ),
    "AX_NO_RELOAD_BUTTON": (
        "the Chrome Control card has no Reload button — chrome://extensions layout "
        "may have changed, or the card is collapsed. Falls back to the manual fix: "
        + _CHROME_RELOAD_FIX
    ),
    "AX_RELOAD_NO_RECOVERY": (
        "the automated reload WAS pressed but the extension never came back — the "
        "unpacked source tree at ~/dispatch/skills/chrome-control/extension is "
        "likely broken on disk. This one genuinely needs investigation."
    ),
}


def _stamp_chrome_version() -> None:
    """Record that the extension was (re)loaded under the current Chrome."""
    cur = _chrome_installed_version()
    if cur:
        try:
            CHROME_VERSION_STATE.write_text(cur)
        except OSError:
            pass


def _run_chrome_heal() -> RecoverResult | None:
    """Rung 2 of chrome_control recovery: chrome-heal's AX reload.

    Returns a RecoverResult (ok or fail-with-escalation), or None if the
    chrome-heal script is missing (fall through to the legacy escalation).
    """
    if not CHROME_HEAL.exists():
        return None
    try:
        p = subprocess.run(
            [str(CHROME_HEAL), "--rung", "ax", "--json"],
            capture_output=True, text=True, timeout=CHROME_HEAL_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return RecoverResult.fail(
            f"chrome-heal timed out after {CHROME_HEAL_TIMEOUT}s — falling back to the "
            "manual fix. " + _CHROME_RELOAD_FIX,
            escalate_now=True, recover_method="ax_reload", recover_timed_out=True,
        )
    except Exception as e:  # noqa: BLE001
        return RecoverResult.fail(
            f"chrome-heal errored: {e}. " + _CHROME_RELOAD_FIX,
            escalate_now=True, recover_method="ax_reload", recover_error=str(e),
        )
    try:
        result = json.loads(p.stdout or "{}")
    except ValueError:
        result = {}
    summary = result.get("summary") or (p.stdout or p.stderr or "").strip()
    if p.returncode == 0 and result.get("ok"):
        # An AX reload re-registers the SW under the current Chrome binary.
        _stamp_chrome_version()
        return RecoverResult.ok(
            f"chrome-heal AX reload recovered: {summary}",
            recover_method="ax_reload", recover_rc=0, recover_output=summary,
        )
    # Failed — pick the specific admin ask from the diagnostic prefix.
    detail = ""
    for a in result.get("attempts", []):
        if a.get("rung") == "ax":
            detail = a.get("detail", "")
    fix = _CHROME_RELOAD_FIX
    for prefix, ask in _CHROME_HEAL_FIXES.items():
        if prefix in detail or prefix in summary:
            fix = ask
            break
    return RecoverResult.fail(
        f"automated AX reload failed — {fix} (chrome-heal: {summary})",
        escalate_now=True, recover_method="ax_reload",
        recover_rc=p.returncode, recover_output=(detail or summary),
    )


# The Chrome version the unpacked extension's SW registration was last
# (re)loaded under. When Chrome auto-updates, the stale registration is what
# strands the extension on the next cold start (the Jul 2-8 2026 outage) —
# so on a version change we proactively `chrome reload-extension` while
# everything is still healthy, then stamp the new version here.
CHROME_VERSION_STATE = Path.home() / "dispatch/state/chrome-extension-reloaded-under.txt"


def _chrome_installed_version() -> str | None:
    """CFBundleShortVersionString of the installed Chrome.app, or None."""
    try:
        import plistlib
        with open("/Applications/Google Chrome.app/Contents/Info.plist", "rb") as f:
            return str(plistlib.load(f).get("CFBundleShortVersionString") or "") or None
    except Exception:  # noqa: BLE001
        return None


def _chrome_version_skew() -> tuple[str, str] | None:
    """(stored, current) if Chrome's version changed since the last extension
    reload; None otherwise. First sighting just records the baseline."""
    cur = _chrome_installed_version()
    if not cur:
        return None
    try:
        stored = CHROME_VERSION_STATE.read_text().strip()
    except OSError:
        stored = ""
    if not stored:
        try:
            CHROME_VERSION_STATE.write_text(cur)
        except OSError:
            pass
        return None
    return (stored, cur) if stored != cur else None


def _chrome_probe() -> ProbeResult:
    """`chrome ping` with a short timeout. Healthy iff rc==0 and 'Connected ...'.

    Chrome.app not running is DOWN, not SKIP: on this machine Chrome is
    assistant infrastructure, and after a power-loss reboot nothing else
    launches it (no login items). The old `enabled_when=_chrome_app_running`
    gate meant the daemon silently never probed a closed Chrome — which is how
    the Jul 2–8 2026 outage went undetected for days. Recovery relaunches it;
    the recovery-frequency alarm (>5/hr) still escalates if someone is
    deliberately quitting Chrome and we keep fighting them.
    """
    if not CHROME_CLI.exists():
        # Treat a missing CLI as SKIP rather than DOWN — nothing the daemon can do.
        return ProbeResult.skip(f"chrome CLI not found at {CHROME_CLI}")
    if not _chrome_app_running():
        return ProbeResult.down(
            "Google Chrome is not running", chrome_running=False, probe_rc=None,
        )
    try:
        p = subprocess.run(
            [str(CHROME_CLI), "ping"],
            capture_output=True, text=True, timeout=CHROME_PING_TIMEOUT,
        )
        out = ((p.stdout or "") + (p.stderr or "")).strip()
        if p.returncode == 0 and "Connected to Chrome Control extension" in out:
            skew = _chrome_version_skew()
            if skew:
                # Healthy but running on a stale SW registration from the
                # pre-update binary — recover_on_degraded triggers a proactive
                # reload before the next cold start can strand it.
                return ProbeResult.degraded(
                    f"chrome ping ok, but Chrome updated {skew[0]} → {skew[1]} — "
                    "extension needs a proactive reload",
                    probe_rc=0, version_skew=True,
                    version_stored=skew[0], version_current=skew[1],
                )
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
    # Version-skew proactive reload: the probe reported DEGRADED (ping is fine
    # but Chrome updated under us). Reload the extension NOW, while it still
    # works, so the stale SW registration can't strand it on the next restart.
    skew = _chrome_version_skew()
    if skew and _chrome_app_running():
        ping = subprocess.run(
            [str(CHROME_CLI), "ping"], capture_output=True, text=True,
            timeout=CHROME_PING_TIMEOUT,
        )
        if ping.returncode == 0:
            r = subprocess.run(
                [str(CHROME_CLI), "reload-extension"], capture_output=True,
                text=True, timeout=CHROME_RESET_TIMEOUT,
            )
            rout = ((r.stdout or "") + (r.stderr or "")).strip()
            if r.returncode == 0:
                _stamp_chrome_version()
                return RecoverResult.ok(
                    f"proactively reloaded extension after Chrome update "
                    f"{skew[0]} → {skew[1]}",
                    recover_method="proactive_reload", recover_rc=0,
                    recover_output=rout,
                )
            return RecoverResult.fail(
                f"proactive `chrome reload-extension` after Chrome update "
                f"{skew[0]} → {skew[1]} failed (rc={r.returncode}) — if this keeps "
                "failing the next Chrome restart will likely strand the extension",
                recover_method="proactive_reload", recover_rc=r.returncode,
                recover_output=rout,
            )
        # ping is down after all — fall through to the normal DOWN chain.
    # Rung 0: Chrome isn't running at all (power-loss reboot, crash, user quit).
    # Launch it in the background and wake the extension. `-g` keeps it out of
    # the foreground so we don't steal focus from an active screen session.
    if not _chrome_app_running():
        subprocess.run(
            ["open", "-ga", "Google Chrome"], capture_output=True, timeout=15,
        )
        deadline = time.time() + 20
        while time.time() < deadline and not _chrome_app_running():
            time.sleep(1)
        if not _chrome_app_running():
            return RecoverResult.fail(
                "`open -ga 'Google Chrome'` did not start Chrome within 20s",
                recover_method="launch_chrome",
            )
        time.sleep(5)  # let the profile + extensions finish loading
        w = subprocess.run(
            [str(CHROME_CLI), "wake"], capture_output=True, text=True,
            timeout=CHROME_RESET_TIMEOUT,
        )
        wout = ((w.stdout or "") + (w.stderr or "")).strip()
        if w.returncode == 0:
            return RecoverResult.ok(
                "launched Google Chrome and woke the extension",
                recover_method="launch_chrome", recover_rc=0, recover_output=wout,
            )
        # Chrome is up but the extension didn't wake — fall through to the
        # reset → chrome-heal chain below, which handles the errored state
        # (the version-skew cold start lands exactly here).
    try:
        p = subprocess.run(
            [str(CHROME_CLI), "reset"],
            capture_output=True, text=True, timeout=CHROME_RESET_TIMEOUT,
        )
        out = ((p.stdout or "") + (p.stderr or "")).strip()
        if p.returncode == 0:
            return RecoverResult.ok("chrome reset reconnected", recover_rc=0, recover_output=out)
        # Non-zero → likely the errored-extension state. Before paging a human,
        # try the automated equivalent of the manual reload (chrome-heal's AX
        # rung presses the reload ⟳ button on chrome://extensions/ itself).
        disable_reason = _extract_disable_reason(out)
        errored = any(sig in out for sig in _ERRORED_EXTENSION_SIGNALS) or "errored" in out.lower()
        healed = _run_chrome_heal()
        if healed is not None:
            if not healed.success and disable_reason:
                healed.diagnostics["disable_reason"] = disable_reason
            return healed
        # chrome-heal missing — legacy behavior: escalate with the manual ask.
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
    """Rebuild the ConsumerRunner and restart its background thread.

    _start_consumer_thread publishes the new thread as manager._consumer_thread
    before it runs, which supersedes any surviving old thread (it exits at its
    next loop check instead of competing — two live runner threads fence each
    other's consumers forever).
    """
    try:
        # Stop the current runner (best-effort) so its DB connections are
        # released and the old thread's run_forever() returns cleanly.
        runner = getattr(manager, "_consumer_runner", None)
        if runner is not None:
            try:
                runner.stop()
            except Exception:
                pass
        # Rebuild + restart the thread (assigns manager._consumer_thread itself).
        if hasattr(manager, "_init_consumers"):
            manager._consumer_runner = manager._init_consumers()
        if hasattr(manager, "_start_consumer_thread"):
            manager._start_consumer_thread()
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
# oauth_token
# ──────────────────────────────────────────────────────────────

def _keychain_token_expires_at_ms() -> int | None:
    """Read claudeAiOauth.expiresAt (ms epoch) from the macOS keychain."""
    try:
        raw = subprocess.check_output(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            stderr=subprocess.DEVNULL,
            timeout=10,
        ).decode().strip()
        expires = json.loads(raw).get("claudeAiOauth", {}).get("expiresAt")
        return int(expires) if expires else None
    except Exception:  # noqa: BLE001
        return None


def _oauth_probe() -> ProbeResult:
    """DOWN while sessions are failing turns with OAuth auth errors.

    The flag file is raised by SDKSession when a turn's output matches an
    auth-failure pattern (Aug 2026: "OAuth session expired and could not be
    refreshed" — every message silently eaten for a day while the daemon
    looked healthy). There is no automated recovery — only a human re-login
    fixes a dead refresh token — so DOWN escalates straight to an SMS, which
    goes out via the send-sms CLI and does not need the API.

    The flag clears on the first successful turn, or here once the keychain
    shows a token that is valid again (re-login happened with no traffic since).
    """
    if not AUTH_FAILURE_FLAG.exists():
        return ProbeResult.ok("no auth failures flagged")
    try:
        flag = json.loads(AUTH_FAILURE_FLAG.read_text())
    except (OSError, ValueError):
        flag = {}

    expires_ms = _keychain_token_expires_at_ms()
    now_ms = int(time.time() * 1000)
    if expires_ms and expires_ms > now_ms:
        # Fresh credentials in the keychain — the user re-logged in.
        try:
            AUTH_FAILURE_FLAG.unlink(missing_ok=True)
        except OSError:
            pass
        return ProbeResult.ok("keychain token valid again; cleared auth-failure flag")

    return ProbeResult.down(
        "Claude OAuth session expired and could not be refreshed — every SDK "
        "turn is failing instantly and messages are being silently dropped. "
        "Fix: on the mini, run `claude` and `/login` (or `claude login`). "
        "This alert auto-clears once a turn succeeds.",
        flagged_at=flag.get("ts"),
        flagged_by=flag.get("session_name"),
        error_detail=flag.get("detail"),
        token_expires_at_ms=expires_ms,
    )


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
        # No enabled_when gate: Chrome-not-running is a DOWN we recover from
        # (relaunch) — required for unattended power-loss reboots. The
        # recovery-frequency alarm catches a human deliberately quitting Chrome.
        # Healthy-but-Chrome-updated probes come back DEGRADED and trigger the
        # proactive extension reload (version-skew is what strands the SW).
        recover_on_degraded=True,
        probe_timeout_s=CHROME_PING_TIMEOUT + 5,
        # reset (~30s) + chrome-heal AX rung (~150s) — the recover chain now
        # includes the automated chrome://extensions reload before escalating.
        recover_timeout_s=CHROME_RESET_TIMEOUT + CHROME_HEAL_TIMEOUT + 15,
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

    # ── oauth_token ───────────────────────────────────────────
    runner.register(DependencyCheck(
        name="oauth_token",
        probe=_oauth_probe,
        recover=None,                        # dead refresh token needs a human /login → escalate
        interval_s=120,                      # flag → SMS within ~2 min
        probe_timeout_s=15.0,
        reescalate_after_s=4 * 3600,
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
