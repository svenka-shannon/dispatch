"""Fixtures for the chaos / resilience smoke suite.

These tests deliberately *break* live components on this machine (kill Chrome's
native host, kill signal-cli, crash the bus consumer, exhaust FDs, …) and then
assert the self-healing-resilience loops recover within the §3 SLOs of
``plans/self-healing-resilience.md``.

EVERY destructive test is gated on ``CLAUDE_LIVE_TESTS=1`` (the existing
live-test pattern — see ``tests/live/test_investigation_smoke.py``). When that
env var is unset, the destructive bodies are *skipped* and only the
mocked/structural variants run. The runner ``scripts/chaos-test.sh`` is the
intended entrypoint; it sets the env var and refuses the most disruptive tests
without an explicit ``--yes-i-mean-it``.

This conftest stays import-clean even when the env var is unset so the suite
*collects* cleanly in a normal ``pytest`` run.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Callable

import pytest

# ── Gating ──────────────────────────────────────────────────────────────

LIVE = os.environ.get("CLAUDE_LIVE_TESTS") == "1"

# A second, *louder* gate for the most disruptive tests (quit Chrome.app, kill
# signal-cli, crash the bus consumer). The runner script sets this when the
# operator passed --yes-i-mean-it. A plain ``CLAUDE_LIVE_TESTS=1 pytest`` will
# still skip these so a careless invocation can't take the machine down.
DESTRUCTIVE_OK = os.environ.get("CLAUDE_CHAOS_DESTRUCTIVE") == "1"

requires_live = pytest.mark.skipif(
    not LIVE,
    reason="chaos test — gated on CLAUDE_LIVE_TESTS=1 (run via scripts/chaos-test.sh)",
)

requires_destructive = pytest.mark.skipif(
    not (LIVE and DESTRUCTIVE_OK),
    reason=(
        "highly disruptive chaos test — needs CLAUDE_LIVE_TESTS=1 *and* "
        "CLAUDE_CHAOS_DESTRUCTIVE=1 (set by `scripts/chaos-test.sh --yes-i-mean-it`)"
    ),
)


# ── Common paths ────────────────────────────────────────────────────────

REPO = Path(__file__).resolve().parents[2]
BUS_DB = Path.home() / "dispatch" / "state" / "bus.db"
CHROME_CLI = Path.home() / ".claude" / "skills" / "chrome-control" / "scripts" / "chrome"
SIGNAL_SOCKET = Path("/tmp/signal-cli.sock")
NATIVE_HOST_PATTERN = "chrome-control/scripts/native_host"


# ── Polling helper (generous timeouts, no fixed sleeps) ─────────────────

def poll_until(
    pred: Callable[[], bool],
    *,
    timeout: float,
    interval: float = 1.0,
    desc: str = "",
) -> bool:
    """Poll ``pred()`` until it returns truthy or ``timeout`` seconds elapse.

    Returns the final truthiness. Callers assert on the return value so a
    failure surfaces a clear ``desc`` rather than a generic timeout.
    """
    deadline = time.monotonic() + timeout
    last = False
    while time.monotonic() < deadline:
        try:
            last = bool(pred())
        except Exception:
            last = False
        if last:
            return True
        time.sleep(interval)
    # one last shot right at the deadline
    try:
        return bool(pred())
    except Exception:
        return False


# ── Process / socket helpers ────────────────────────────────────────────

def pgrep_count(pattern: str) -> int:
    """Number of processes (owned by this uid) whose argv matches ``pattern``."""
    try:
        out = subprocess.run(
            ["pgrep", "-u", str(os.getuid()), "-fc", pattern],
            capture_output=True, text=True, timeout=10,
        )
        return int((out.stdout or "0").strip() or "0")
    except Exception:
        return 0


def pgrep_pids(pattern: str) -> list[int]:
    try:
        out = subprocess.run(
            ["pgrep", "-u", str(os.getuid()), "-f", pattern],
            capture_output=True, text=True, timeout=10,
        )
        return [int(p) for p in (out.stdout or "").split() if p.strip().isdigit()]
    except Exception:
        return []


def socket_connectable(path: Path, timeout: float = 3.0) -> bool:
    import socket as _socket
    if not path.exists():
        return False
    s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    try:
        s.settimeout(timeout)
        s.connect(str(path))
        return True
    except OSError:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def chrome_ping_connected(timeout: float = 12.0) -> bool:
    """True iff ``chrome ping`` reports a live connection."""
    if not CHROME_CLI.exists():
        return False
    try:
        p = subprocess.run(
            [str(CHROME_CLI), "ping"], capture_output=True, text=True, timeout=timeout,
        )
        out = (p.stdout or "") + (p.stderr or "")
        return p.returncode == 0 and "Connected to Chrome Control extension" in out
    except Exception:
        return False


def chrome_reset() -> subprocess.CompletedProcess:
    """Run ``chrome reset`` (self-heal). Returns the CompletedProcess; rc==0
    means it actually reconnected (CLI hardened in dbe721b)."""
    return subprocess.run(
        [str(CHROME_CLI), "reset"], capture_output=True, text=True, timeout=90,
    )


def chrome_app_running() -> bool:
    try:
        out = subprocess.run(["pgrep", "-x", "Google Chrome"], capture_output=True, text=True, timeout=5)
        return out.returncode == 0
    except Exception:
        return False


# ── bus.db readers (read-only — never mutate the live bus) ──────────────

def read_records(*, topic: str | None = None, type_: str | None = None, limit: int = 200) -> list[dict]:
    """Read recent records from the live bus.db as plain dicts (newest first).

    Read-only sqlite connection; we never touch the bus from a test.
    """
    import json
    import sqlite3
    if not BUS_DB.exists():
        return []
    conn = sqlite3.connect(f"file:{BUS_DB}?mode=ro", uri=True)
    try:
        clauses, params = [], []
        if topic:
            clauses.append("topic = ?")
            params.append(topic)
        if type_:
            clauses.append("type = ?")
            params.append(type_)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT topic, type, source, timestamp, payload FROM records{where} ORDER BY offset DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    out: list[dict] = []
    for topic_, type__, source, ts, payload in rows:
        try:
            data = json.loads(payload) if payload else {}
        except Exception:
            data = {}
        out.append({"topic": topic_, "type": type__, "source": source, "timestamp": ts, "payload": data})
    return out


def dependency_events(name: str, *, limit: int = 500) -> list[dict]:
    """``health.dependency`` events for a given dependency ``name`` (newest first)."""
    evs = read_records(topic="system", type_="health.dependency", limit=limit)
    return [e for e in evs if e["payload"].get("name") == name]


def bus_consumer_groups() -> list[dict]:
    """Snapshot of consumer groups from the live bus (read-only)."""
    try:
        from bus.bus import Bus
        b = Bus(str(BUS_DB))
        try:
            return b.list_consumer_groups()
        finally:
            b.close()
    except Exception:
        return []
