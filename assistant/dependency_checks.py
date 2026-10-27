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
  - imessage_app_nap : the two macOS `defaults` keys that stop App Nap from
                     deferring Messages' inbound chat.db writes (18m23s hidden
                     message, POSTMORTEMS.md 2026-09-07); recovery re-applies
                     them via bin/setup-macos-defaults. Host state, so it decays
                     across reinstalls and macOS updates — hence a live check.
  - api_uplink     : degraded-uplink detector — 500KB control + 2MB test upload
                     to speed.cloudflare.com/__up. Small-passes/large-chokes is
                     the signature of the failure mode that silently kills
                     big-context API calls; no auto-recovery (nothing local
                     fixes an uplink) → escalate with the specific fix.
  - tcp_clock      : macOS 49.7-day tcp_now freeze — TIME_WAIT > 1000 → DOWN,
                     uptime ≥ 45d → DEGRADED; only a reboot fixes it → escalate.
  - fd_leak        : untracked FD growth via ResourceRegistry; no auto-recovery
                     (a real FD leak needs a code fix) → escalate.
  - screen_lock    : the macOS lock screen (display sleep → "require password
                     after N seconds") blinds every GUI-driven skill — chrome
                     screenshots, AX walks, cliclick — and nothing said so.
                     Probe `bin/screen-lock status` every minute; recovery types
                     the login password from Keychain into loginwindow (and,
                     with `screen_lock.harden: true`, turns the re-arming
                     settings off); escalate with the exact Keychain one-liner
                     when the password is not stored.

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

from assistant.common import ASSISTANT_DIR, AUTH_FAILURE_FLAG, SIGNAL_SOCKET, UV
from assistant.dependency_health import (
    DependencyCheck,
    DependencyHealthRunner,
    ProbeResult,
    ProbeStatus,
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
    """Healthy iff the ConsumerRunner thread is alive and the LIVE runner's own
    group members are registered, alive, and actually hold partitions.

    Only members owned by the live runner (``manager._consumer_runner.members()``)
    are judged. Members left behind by a previous daemon process / a crashed
    runner / a one-shot CLI consumer are ignored: they are not ours to "recover",
    and the old behaviour (DOWN on ANY dead member anywhere) fired a spurious
    DOWN + runner rebuild ~5 min after every restart, when the previous daemon's
    members' heartbeats aged out. The runner joins with exclusive=True, so such
    leftovers are purged from our groups on join anyway.

    Falls back to the legacy all-members check when the runner does not expose
    members() (older runner / test doubles).
    """
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

    owned = None
    runner = getattr(manager, "_consumer_runner", None)
    members_fn = getattr(runner, "members", None)
    if callable(members_fn):
        try:
            snap = members_fn()
            if isinstance(snap, dict):
                owned = snap
        except Exception:  # noqa: BLE001
            owned = None

    if owned is None:
        # Legacy: any DEAD member anywhere is DOWN.
        dead = []
        for g in groups:
            for m in g.get("members", []):
                if not m.get("alive", True):
                    dead.append(f"{g['group_id']}:{m['consumer_id']}")
        if dead:
            return ProbeResult.down(f"DEAD consumer members: {', '.join(dead)}", dead_members=dead)
        return ProbeResult.ok(f"bus consumers healthy ({len(groups)} groups)")

    rows = {
        g.get("group_id"): {m.get("consumer_id"): m for m in g.get("members", [])}
        for g in groups
    }
    dead: list[str] = []
    starved: list[str] = []
    ignored = 0
    for group, info in owned.items():
        cid = info.get("consumer_id")
        group_rows = rows.get(group, {})
        ignored += sum(1 for other in group_rows if other != cid)
        m = group_rows.get(cid)
        if m is None or not m.get("alive", True):
            dead.append(f"{group}:{cid}")
            continue
        if not info.get("assigned"):
            # 0 partitions is only a problem if the topic actually has some
            # (e.g. 'tweets' may not exist → 0 partitions is correct).
            try:
                n_parts = bus.topic_partition_count(info.get("topic"))
            except Exception:  # noqa: BLE001
                n_parts = 0
            if isinstance(n_parts, int) and n_parts > 0:
                starved.append(f"{group}:{cid}")
    ignored += sum(len(v) for k, v in rows.items() if k not in owned)
    if dead or starved:
        parts = []
        if dead:
            parts.append(f"DEAD consumer members: {', '.join(dead)}")
        if starved:
            parts.append(f"consumers holding 0 partitions: {', '.join(starved)}")
        return ProbeResult.down("; ".join(parts), dead_members=dead,
                                starved_members=starved, ignored_foreign_members=ignored)
    return ProbeResult.ok(
        f"bus consumers healthy ({len(owned)} runner groups; {ignored} foreign member(s) ignored)"
    )


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
# tcp_clock
# ──────────────────────────────────────────────────────────────
# macOS XNU bug: tcp_now is a 32-bit ms counter; at 2^32 ms of uptime
# (49d 17h 02m 47s) a monotonicity guard freezes it instead of wrapping.
# Frozen clock → TSval constant, time-based loss recovery never fires (big
# uploads die with a server RST ~15s after one lost packet), and TIME_WAIT
# never expires until ephemeral ports run out. Only a reboot clears it.
# POSTMORTEMS.md #24–#26 (2026-09-24): 19.5k TIME_WAIT, API storm from 09-18.
# Healthy TIME_WAIT is single/double digits; frozen crosses 1,000 in ~3h.

TCP_CLOCK_FREEZE_S = (2**32) / 1000.0          # 4,294,967.296 s ≈ 49.71 days
TCP_CLOCK_WARN_UPTIME_S = 45 * 86400           # nudge a reboot ~4.7 days early
TCP_CLOCK_TIME_WAIT_DOWN = 1000


def _boot_epoch() -> float | None:
    out = subprocess.run(["sysctl", "-n", "kern.boottime"], capture_output=True,
                         text=True, timeout=5).stdout
    # "{ sec = 1790284064, usec = 572082 } Thu Sep 24 14:07:44 2026"
    try:
        return float(out.split("sec =", 1)[1].split(",", 1)[0])
    except (IndexError, ValueError):
        return None


def _time_wait_count() -> int:
    out = subprocess.run(["netstat", "-an", "-p", "tcp"], capture_output=True,
                         text=True, timeout=20).stdout
    return sum(1 for line in out.splitlines() if line.rstrip().endswith("TIME_WAIT"))


def _tcp_clock_verdict(uptime_s: float, time_wait: int) -> ProbeResult:
    days = uptime_s / 86400
    freeze_in_d = (TCP_CLOCK_FREEZE_S - uptime_s) / 86400
    if time_wait > TCP_CLOCK_TIME_WAIT_DOWN:
        return ProbeResult.down(
            f"Mac TCP clock looks frozen (macOS 49.7-day bug): {time_wait} stuck "
            f"connections, up {days:.0f}d. Big uploads fail and ports run out. "
            f"Reboot while at the Mac (auto-login is off).",
            time_wait=time_wait, uptime_days=round(days, 2),
        )
    if uptime_s >= TCP_CLOCK_WARN_UPTIME_S:
        when = "is past" if freeze_in_d <= 0 else f"hits in {freeze_in_d:.1f}d"
        return ProbeResult.degraded(
            f"Mac up {days:.0f}d; macOS 49.7-day TCP clock freeze {when}. "
            f"Reboot soon while at the Mac (auto-login is off).",
            time_wait=time_wait, uptime_days=round(days, 2),
        )
    return ProbeResult.ok(
        f"tcp clock ok (up {days:.1f}d, {time_wait} TIME_WAIT, freeze in {freeze_in_d:.1f}d)",
        time_wait=time_wait, uptime_days=round(days, 2),
    )


def _tcp_clock_probe() -> ProbeResult:
    try:
        boot = _boot_epoch()
        tw = _time_wait_count()
    except Exception as e:  # noqa: BLE001
        return ProbeResult.skip(f"could not read uptime/netstat: {e}")
    if boot is None:
        return ProbeResult.skip("could not parse kern.boottime")
    return _tcp_clock_verdict(time.time() - boot, tw)
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
# imessage_app_nap
# ──────────────────────────────────────────────────────────────

# Host state, not code: App Nap suspends IMDPersistenceAgent when Messages.app is
# deep-backgrounded and macOS then DEFERS Messages' inbound chat.db writes. The
# poller sees nothing because the row genuinely is not in the database yet — on
# 2026-09-07 that hid one inbound message for 18m23s (POSTMORTEMS.md → "2026-09-07:
# Deferred chat.db inbound writes (App Nap)"). Two `defaults` keys turn it off.
#
# It lives here rather than install-only because machine state decays: a fresh
# install on a new Mac never had the keys, and a macOS update can reset them. The
# probe/recover both shell out to bin/setup-macos-defaults (single source of truth
# for the domains, keys and the relaunch caveat) rather than re-implementing the
# `defaults` calls.
SETUP_MACOS_DEFAULTS = ASSISTANT_DIR / "bin" / "setup-macos-defaults"
SETUP_MACOS_DEFAULTS_TIMEOUT = 60


def _run_setup_macos_defaults(check_only: bool) -> tuple[int, dict, str]:
    """Run bin/setup-macos-defaults --json. Returns (rc, parsed_payload, raw_stderr)."""
    cmd = [UV, "run", "--script", str(SETUP_MACOS_DEFAULTS), "--json"]
    if check_only:
        cmd.append("--check")
    p = subprocess.run(cmd, capture_output=True, text=True,
                       timeout=SETUP_MACOS_DEFAULTS_TIMEOUT)
    try:
        payload = json.loads(p.stdout)
    except ValueError:
        payload = {}
    return p.returncode, payload, (p.stderr or "").strip()


def _unset_keys(payload: dict) -> list[str]:
    return [f"{d['domain']} {d['key']}"
            for d in payload.get("defaults", []) if d.get("status") != "OK"]


def _app_nap_probe() -> ProbeResult:
    """DOWN while either App Nap default is unset — inbound chat.db writes can stall."""
    if not SETUP_MACOS_DEFAULTS.exists():
        return ProbeResult.skip(f"{SETUP_MACOS_DEFAULTS} not present")
    try:
        rc, payload, err = _run_setup_macos_defaults(check_only=True)
    except subprocess.TimeoutExpired:
        return ProbeResult.down("setup-macos-defaults --check timed out", probe_timed_out=True)
    except OSError as e:
        return ProbeResult.skip(f"could not run setup-macos-defaults: {e}")
    if rc == 0:
        return ProbeResult.ok(
            "App Nap disabled for Messages.app",
            relaunch_required=payload.get("relaunch_required"),
            messages_running=payload.get("messages_running"),
        )
    missing = _unset_keys(payload) or ["unknown (unparseable output)"]
    return ProbeResult.down(
        "App Nap is NOT disabled for Messages.app — inbound chat.db writes can be "
        f"deferred for many minutes (see POSTMORTEMS.md 2026-09-07). Unset: {', '.join(missing)}",
        probe_rc=rc, unset_keys=missing, probe_stderr=err,
    )


def _app_nap_recover() -> RecoverResult:
    """Write the defaults back. They are read AT LAUNCH — warn if Messages must relaunch.

    Deliberately does NOT quit Messages.app: killing the app the daemon reads
    from, unattended, is worse than one WARNING line. The keys apply on the next
    launch either way.
    """
    try:
        rc, payload, err = _run_setup_macos_defaults(check_only=False)
    except subprocess.TimeoutExpired:
        return RecoverResult.fail("setup-macos-defaults timed out")
    except OSError as e:
        return RecoverResult.fail(f"could not run setup-macos-defaults: {e}")
    if rc != 0:
        return RecoverResult.fail(
            f"setup-macos-defaults exited {rc}; still unset: {', '.join(_unset_keys(payload)) or 'unknown'}. "
            "Fix by hand: defaults write com.apple.MobileSMS NSAppSleepDisabled -bool YES && "
            "defaults write com.apple.messageshelper.MessageController SOAppNapDisabled -bool YES, "
            "then relaunch Messages.app",
            recover_rc=rc, recover_stderr=err,
        )
    changed = payload.get("changed", 0)
    if payload.get("relaunch_required"):
        log.warning(
            "DEP_HEALTH | imessage_app_nap | wrote %s App Nap default(s) — Messages.app "
            "(pid=%s) reads them AT LAUNCH, so they are NOT in effect yet. Quit and reopen "
            "Messages.app (or run: bin/setup-macos-defaults --restart-messages)",
            changed, payload.get("messages_pid"),
        )
    return RecoverResult.ok(
        f"wrote {changed} App Nap default(s)"
        + (" — Messages.app relaunch required for them to take effect"
           if payload.get("relaunch_required") else ""),
        changed=changed, relaunch_required=payload.get("relaunch_required"),
    )


# ──────────────────────────────────────────────────────────────
# api_uplink
# ──────────────────────────────────────────────────────────────

# 2026-09-21 FINDING — this probe's escalation text blames the wrong layer.
# It says "Fix: plug Ethernet into en0, or reboot modem/router; if it persists
# it's an ISP upstream issue." Measured on this box while the probe was
# ESCALATED, using `networkQuality -s -v`:
#     Uplink capacity        45.0 Mbps     <- bandwidth is FINE
#     Downlink capacity     423.6 Mbps
#     Uplink responsiveness  714 ms / 84 RPM  ("Low")   <- the actual fault
#     Idle latency            27 ms
# 45 Mbps up should move 2MB in <0.5s; observed 44-270 KB/s, i.e. 3-100x under
# capacity, timing out at 45s. That is textbook BUFFERBLOAT — latency collapses
# under sustained upload (27ms idle -> 714ms loaded), connections stall, peer
# resets (rc=56). Bufferbloat is the router's oversized buffers, so neither a
# modem reboot nor an ISP call fixes it; SQM/QoS (fq_codel / cake) does.
# TODO: have this probe run `networkQuality` on failure and branch its advice —
# low capacity => ISP; adequate capacity + low responsiveness => router SQM.
#
# The network uplink has a failure mode where sustained uploads >~1.5MB collapse
# to ~130KB/s and get RST ~15s in, while small uploads pass at ~3MB/s (verified
# repeatedly against speed.cloudflare.com/__up, Sep 2026). That silently kills
# Claude API calls from sessions with large contexts — the admin used to
# discover it via dead sessions. The probe uploads a small control (500KB) and
# a large test (2MB) to Cloudflare's upload endpoint:
#   - small fails            → SKIP  (network fully down / endpoint unreachable —
#                                     other checks cover total loss; don't page
#                                     for a third-party outage)
#   - small OK, large fails  → DEGRADED (the signature failure mode) → escalate
#                                     (recover_on_degraded=True + recover=None)
#   - both OK                → OK
# Nothing local can fix an uplink, so there is no recover(); the DEGRADED detail
# carries the specific admin ask.
UPLINK_TEST_URL = "https://speed.cloudflare.com/__up"
UPLINK_SMALL_BYTES = 500 * 1024        # control: passes even on the degraded uplink
UPLINK_LARGE_BYTES = 2 * 1024 * 1024   # test: chokes when the uplink is degraded
UPLINK_SMALL_MAX_TIME = 20
UPLINK_LARGE_MAX_TIME = 45

# 2026-09-21: this fault is INTERMITTENT, not binary. Root cause is a TCP flow
# holding >~480KB unacked stalling and being RST 15s later; capping
# net.inet.tcp.autosndbufmax to 393216 took 2MB uploads from 0/10 to 10/10, but a
# residual ~1-in-10 remains at larger sizes. Paging on a SINGLE failed probe sent
# the admin 16 wall-of-text SMS in two days for a condition that is already
# mitigated and self-clears. Require consecutive failures before escalating.
UPLINK_RETRIES = 2          # extra attempts after the first failure
UPLINK_RETRY_PAUSE_S = 3

# KEEP THIS SHORT. It is delivered to a human as an SMS. The long diagnostic
# narrative that used to live here (686 chars, incl. "re-test a NON-Cloudflare
# host", "Do NOT advise rebooting the router") was correct guidance aimed at the
# WRONG AUDIENCE — it paged the admin 16 times in two days with a wall of text.
# That guidance now lives in the comments above + the probe diagnostics, where
# the next engineer reads it. The SMS says only what the human can act on.
# NOTE: ethernet is NOT an option in this deployment — do not re-add it.
# 2026-09-24: this used to end "Not an ISP or router fault: verified." It was
# NOT verified. That conclusion rested on `netstat -s -p tcp` reporting zero
# retransmissions -- and that counter is dead on macOS 26.2, reporting 0 packets
# sent on a machine that is actively networking (confirmed on a second Mac, so
# it is the OS, not this box). A sender holding unacked data MUST retransmit
# after RTO, so "zero retransmits" was never a real observation.
#
# First rewrite kept "No router or ISP action needed", which is the SAME
# unverified claim with the word "verified" deleted. If something on the path
# is eating the burst -- the current leading hypothesis -- then router or ISP
# action may be exactly what is needed. An alert must not tell the operator
# where NOT to look while the cause is open.
#
# What survives is the actionable lever (the sysctl cap, measured) and the
# honest state of the question. "reboot" and "ethernet" stay banned: those were
# specific bad advice that wasted the admin's time, not a causal claim.
_UPLINK_ESCALATION_FIX = (
    "uplink degraded: 2MB upload failed 3x in a row (fault is intermittent and "
    "usually self-clears). Check net.inet.tcp.autosndbufmax is still 393216 — "
    "that cap is the known fix. Root cause not yet isolated — path vs local "
    "stack, pending a packet capture."
)


def _run_curl_upload(nbytes: int, max_time: int) -> dict[str, Any]:
    """POST `nbytes` zero bytes to UPLINK_TEST_URL via curl.

    The payload is generated in memory and piped through stdin
    (`--data-binary @-`) — no temp files. `-w` reports http_code + upload speed
    on stdout; the response body is discarded.

    Returns {rc, http_code, speed_bps, stderr, timed_out}. `rc is None` +
    `timed_out=True` means the subprocess-level timeout fired (curl itself
    wedged past --max-time).
    """
    cmd = [
        "curl", "-sS", "-o", "/dev/null",
        "--max-time", str(max_time),
        "-H", "Content-Type: application/octet-stream",
        "--data-binary", "@-",
        "-w", "%{http_code} %{speed_upload}",
        UPLINK_TEST_URL,
    ]
    try:
        p = subprocess.run(
            cmd, input=b"\x00" * nbytes, capture_output=True, timeout=max_time + 10,
        )
    except subprocess.TimeoutExpired:
        return {"rc": None, "http_code": "", "speed_bps": None,
                "stderr": f"curl wedged past --max-time (killed after {max_time + 10}s)",
                "timed_out": True}
    out = (p.stdout or b"").decode(errors="replace").strip()
    parts = out.split()
    http_code = parts[0] if parts else ""
    speed: float | None = None
    if len(parts) > 1:
        try:
            speed = float(parts[1].replace(",", "."))  # curl may emit locale decimals
        except ValueError:
            speed = None
    # curl exits non-zero when --max-time cuts a transfer (rc 28) — that counts
    # as the RST/stall failure mode, distinct from the subprocess-level timeout.
    return {"rc": p.returncode, "http_code": http_code, "speed_bps": speed,
            "stderr": (p.stderr or b"").decode(errors="replace").strip(),
            "timed_out": p.returncode == 28}


def _upload_ok(r: dict[str, Any]) -> bool:
    return r["rc"] == 0 and str(r["http_code"]).startswith("2")


def _fmt_speed(speed_bps: float | None) -> str:
    return f"{speed_bps / 1024:.0f}KB/s" if speed_bps is not None else "unknown speed"


def _api_uplink_probe() -> ProbeResult:
    """Small control upload, then large test upload. See the block comment above."""
    try:
        small = _run_curl_upload(UPLINK_SMALL_BYTES, UPLINK_SMALL_MAX_TIME)
    except OSError as e:
        return ProbeResult.skip(f"could not run curl: {e}")
    if not _upload_ok(small):
        # Network fully down or Cloudflare unreachable — not the degraded-uplink
        # signature, and not something to page the admin about (other checks
        # cover total network loss).
        return ProbeResult.skip(
            "uplink control upload (500KB) failed — network fully down or "
            f"speed.cloudflare.com unreachable (rc={small['rc']}, "
            f"http={small['http_code'] or '?'}, {small['stderr'] or 'no stderr'}); "
            "not paging for a possible third-party outage"
        )

    try:
        large = _run_curl_upload(UPLINK_LARGE_BYTES, UPLINK_LARGE_MAX_TIME)
    except OSError as e:
        return ProbeResult.skip(f"could not run curl for the 2MB test: {e}")
    # Intermittent-fault guard: a single failure is not newsworthy. Retry before
    # we wake a human — the residual fault clears on its own roughly 9 times in 10.
    attempts = 1
    while not _upload_ok(large) and attempts <= UPLINK_RETRIES:
        time.sleep(UPLINK_RETRY_PAUSE_S)
        attempts += 1
        try:
            large = _run_curl_upload(UPLINK_LARGE_BYTES, UPLINK_LARGE_MAX_TIME)
        except OSError:
            break

    small_txt = _fmt_speed(small["speed_bps"])
    large_txt = _fmt_speed(large["speed_bps"])
    if not _upload_ok(large):
        return ProbeResult.degraded(
            _UPLINK_ESCALATION_FIX
            + f" [2MB test: rc={large['rc']}, http={large['http_code'] or '?'}, "
            f"upload speed {large_txt}"
            + (", hit --max-time (stalled/RST)" if large["timed_out"] else "")
            + f"; 500KB control passed at {small_txt}]",
            probe_rc=large["rc"],
            probe_output=large["stderr"],
            probe_timed_out=large["timed_out"],
            http_code=large["http_code"],
            speed_upload_bps=large["speed_bps"],
            small_speed_upload_bps=small["speed_bps"],
        )
    return ProbeResult.ok(
        f"uplink ok (2MB upload at {large_txt}; 500KB control at {small_txt})",
        speed_upload_bps=large["speed_bps"],
        small_speed_upload_bps=small["speed_bps"],
    )


# ──────────────────────────────────────────────────────────────
# screen_lock
# ──────────────────────────────────────────────────────────────

# Host state again: `pmset displaysleep` (10 min here) darkens the display and
# "require password after N seconds" (`sysadminctl -screenLock status`, 300s
# here) then hands the screen to loginwindow. From that moment every GUI-driven
# skill is blind — chrome-control screenshots come back black, Accessibility
# walks find loginwindow instead of Chrome, cliclick types into the password
# field — and until this check existed nothing detected it, so the agent just
# looked broken. bin/screen-lock is the single source of truth (detect via
# CGSSessionScreenIsLocked, unlock by typing the Keychain-stored login password
# through a CGEvent HID tap, harden by turning the two re-arming settings off).
#
# Hardening is a security trade-off (the Mac stops asking for a password after
# sleep), so it is opt-in via `screen_lock.harden: true` in config.local.yaml.
# Without it the check still detects, unlocks and alerts.
SCREEN_LOCK_CLI = ASSISTANT_DIR / "bin" / "screen-lock"
SCREEN_LOCK_TIMEOUT = 30          # status / harden: a few subprocess calls
SCREEN_LOCK_UNLOCK_TIMEOUT = 60   # wake + type + up to 2 attempts × 6s verify
SCREEN_LOCK_KEYCHAIN_HINT = 'security add-generic-password -a "$USER" -s mac-login-password -w -U'
_SCREEN_LOCK_STORE_ASK = (
    "Auto-unlock needs the Mac login password in Keychain and it is not there. "
    "Unlock the Mac yourself (Chrome Remote Desktop or keyboard), then run once on it: "
    + SCREEN_LOCK_KEYCHAIN_HINT
    + " — after that it self-unlocks."
)


def _run_screen_lock(*args: str, timeout: int = SCREEN_LOCK_TIMEOUT) -> tuple[int, dict, str]:
    """Run bin/screen-lock <args> --json. Returns (rc, parsed_payload, raw_stderr)."""
    cmd = [UV, "run", "--script", str(SCREEN_LOCK_CLI), *args, "--json"]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    try:
        payload = json.loads(p.stdout)
    except ValueError:
        payload = {}
    return p.returncode, payload, (p.stderr or "").strip()


def _screen_lock_harden_enabled() -> bool:
    from assistant import config as _config
    try:
        return bool(_config.get("screen_lock.harden", False))
    except Exception:  # noqa: BLE001
        return False


def _screen_lock_diag(payload: dict) -> dict[str, Any]:
    return {
        "screenlock_setting": payload.get("screenlock_setting"),
        "screensaver_idle_s": payload.get("screensaver_idle_s"),
        "display_sleep_min": payload.get("display_sleep_min"),
        "password_stored": payload.get("password_stored"),
        "hardened": payload.get("hardened"),
    }


def _screen_lock_probe() -> ProbeResult:
    """DOWN while loginwindow has the screen; DEGRADED (opt-in) while the lock can re-arm."""
    if not SCREEN_LOCK_CLI.exists():
        return ProbeResult.skip(f"{SCREEN_LOCK_CLI} not present")
    try:
        rc, payload, err = _run_screen_lock("status")
    except subprocess.TimeoutExpired:
        return ProbeResult.down("screen-lock status timed out", probe_timed_out=True)
    except OSError as e:
        return ProbeResult.skip(f"could not run screen-lock: {e}")
    locked = payload.get("locked")
    if locked is None:
        return ProbeResult.skip(f"lock state unknown (rc={rc}): {err or 'no GUI session dictionary'}")
    diag = _screen_lock_diag(payload)
    stored = bool(payload.get("password_stored"))

    if locked:
        base = ("Mac screen is LOCKED — chrome-control, Messages.app and Accessibility "
                "automation are blind until it is unlocked. ")
        if not stored:
            return ProbeResult.down(base + _SCREEN_LOCK_STORE_ASK, escalate_now=True, **diag)
        return ProbeResult.down(base + "Typing the Keychain login password into loginwindow.", **diag)

    setting = payload.get("screenlock_setting")
    rearm = (f"require-password={setting}s, screensaver idle={payload.get('screensaver_idle_s') or 'never'}, "
             f"display sleep={payload.get('display_sleep_min')}min")
    if _screen_lock_harden_enabled() and not payload.get("hardened"):
        detail = f"screen unlocked but the lock will re-arm ({rearm}); screen_lock.harden is on"
        if not stored:
            # sysadminctl needs the password — nothing to retry until it is stored.
            return ProbeResult(ProbeStatus.DEGRADED, detail + ". " + _SCREEN_LOCK_STORE_ASK,
                               diag, escalate_now=True)
        return ProbeResult.degraded(detail, **diag)
    return ProbeResult.ok(f"screen unlocked ({rearm}; harden={'on' if _screen_lock_harden_enabled() else 'off'})", **diag)


def _screen_lock_recover() -> RecoverResult:
    """Unlock if locked, then (opt-in) harden so it stays that way."""
    try:
        _, status, _ = _run_screen_lock("status")
    except (subprocess.TimeoutExpired, OSError) as e:
        return RecoverResult.fail(f"screen-lock status failed before recovery: {e}")

    parts: list[str] = []
    diag: dict[str, Any] = {}
    if status.get("locked"):
        try:
            rc, payload, err = _run_screen_lock("unlock", timeout=SCREEN_LOCK_UNLOCK_TIMEOUT)
        except subprocess.TimeoutExpired:
            return RecoverResult.fail("screen-lock unlock timed out", recover_timed_out=True)
        except OSError as e:
            return RecoverResult.fail(f"could not run screen-lock unlock: {e}")
        diag.update(recover_rc=rc, unlock_method=payload.get("method"),
                    unlock_attempts=payload.get("attempts"))
        if rc == 3:
            return RecoverResult.fail(_SCREEN_LOCK_STORE_ASK, escalate_now=True, **diag)
        if rc != 0:
            return RecoverResult.fail(
                "auto-unlock FAILED: " + (payload.get("detail") or err or f"rc={rc}")
                + ". Unlock the Mac yourself (Chrome Remote Desktop or keyboard). If this repeats, "
                "check that ~/dispatch/.venv/bin/python is allowed under System Settings → Privacy & "
                "Security → Accessibility AND Input Monitoring, and that the Keychain password is current.",
                recover_output=payload.get("detail") or err, **diag,
            )
        parts.append(payload.get("detail") or "unlocked")
        log.warning("DEP_HEALTH | screen_lock | Mac was locked — %s", parts[-1])

    if _screen_lock_harden_enabled():
        try:
            rc, payload, err = _run_screen_lock("harden")
        except subprocess.TimeoutExpired:
            return RecoverResult.fail("screen-lock harden timed out", recover_timed_out=True, **diag)
        except OSError as e:
            return RecoverResult.fail(f"could not run screen-lock harden: {e}", **diag)
        diag["harden_rc"] = rc
        if rc == 3:
            return RecoverResult.fail(_SCREEN_LOCK_STORE_ASK, escalate_now=True, **diag)
        if rc != 0:
            bad = [f"{i['name']}={i.get('value')!r}" + (f" ({i['error']})" if i.get("error") else "")
                   for i in payload.get("items", []) if i.get("status") == "FAIL"]
            return RecoverResult.fail(
                "could not disable the lock: " + (", ".join(bad) or err or f"rc={rc}")
                + ". Fix by hand: System Settings → Lock Screen → 'Require password after screen "
                "saver begins or display is turned off' = Never, and Screen Saver → Start = Never.",
                recover_output=err, **diag,
            )
        changed = payload.get("changed", 0)
        if changed:
            parts.append(f"disabled require-password + idle screen saver ({changed} setting(s) changed)")
            log.warning("DEP_HEALTH | screen_lock | hardened host: %s", parts[-1])
        diag["harden_changed"] = changed

    return RecoverResult.ok("; ".join(parts) or "screen already unlocked", **diag)


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

    # ── imessage_app_nap ──────────────────────────────────────
    runner.register(DependencyCheck(
        name="imessage_app_nap",
        probe=_app_nap_probe,
        recover=_app_nap_recover,
        # Host defaults only change at install time or across a macOS update, but
        # the first tick runs seconds after daemon start — that's the install-time
        # / fresh-Mac safety net. Hourly after that.
        interval_s=3600,
        max_attempts=2,
        backoff_s=(0, 30),
        probe_timeout_s=SETUP_MACOS_DEFAULTS_TIMEOUT + 5,
        recover_timeout_s=SETUP_MACOS_DEFAULTS_TIMEOUT + 5,
        # If macOS keeps resetting these, a human needs to know — a `defaults
        # write` that has to be repeated is masking something worse.
        recovery_alarm_k=2,
        reescalate_after_s=6 * 3600,
    ))

    # ── api_uplink ────────────────────────────────────────────
    runner.register(DependencyCheck(
        name="api_uplink",
        probe=_api_uplink_probe,
        recover=None,                        # nothing local can fix an uplink
        # DEGRADED (small passes, 2MB chokes) + no recover → escalate immediately
        # with the specific fix carried in the probe detail.
        recover_on_degraded=True,
        interval_s=900,                      # costs real bandwidth — keep it infrequent
        # Two sequential curls: 20s + 45s --max-time, plus subprocess slack.
        probe_timeout_s=UPLINK_SMALL_MAX_TIME + UPLINK_LARGE_MAX_TIME + 25,
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

    # ── tcp_clock ─────────────────────────────────────────────
    runner.register(DependencyCheck(
        name="tcp_clock",
        probe=_tcp_clock_probe,
        recover=None,                        # only a reboot fixes it → escalate
        recover_on_degraded=True,            # 45-day uptime nudge escalates too
        interval_s=1800,
        probe_timeout_s=30.0,
        reescalate_after_s=12 * 3600,
    ))

    # ── screen_lock ───────────────────────────────────────────
    runner.register(DependencyCheck(
        name="screen_lock",
        probe=_screen_lock_probe,
        recover=_screen_lock_recover,
        interval_s=60,                       # a locked screen blinds every GUI skill — find it fast
        max_attempts=3,
        backoff_s=(0, 20, 60),
        retry_while_escalated_s=600,         # 2026-09-27: one transient failure left it locked 14h
        recover_on_degraded=True,            # DEGRADED = "will re-arm" (opt-in harden) → apply it
        probe_timeout_s=SCREEN_LOCK_TIMEOUT + 5,
        recover_timeout_s=SCREEN_LOCK_UNLOCK_TIMEOUT + SCREEN_LOCK_TIMEOUT + 10,
        # Unlocking 3×/hour means the lock keeps re-arming (harden is off or
        # something resets it) — the admin should hear about that, not have it
        # silently papered over.
        recovery_alarm_k=3,
        reescalate_after_s=2 * 3600,
    ))

    return runner
