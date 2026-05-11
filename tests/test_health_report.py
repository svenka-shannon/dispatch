"""Tests for assistant/health_report.py — resilience / health-history aggregation.

Builds a temp bus.db with `records` rows mirroring what
`DependencyHealthRunner` (Phase 2) and the blocked-session watchdog (Phase 1)
emit, then asserts MTTR P50/P95, recovery counts, current-state, SLO flagging,
and the `--dep` filter.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from assistant import health_report as hr


# ──────────────────────────────────────────────────────────────────────
# Fixtures: a temp bus.db with a `records` table.
# ──────────────────────────────────────────────────────────────────────

@pytest.fixture
def bus_db(tmp_path: Path) -> Path:
    db = tmp_path / "bus.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE records (
            topic TEXT NOT NULL,
            partition INTEGER NOT NULL,
            offset INTEGER NOT NULL,
            timestamp INTEGER NOT NULL,
            key TEXT,
            type TEXT,
            source TEXT,
            payload TEXT NOT NULL,
            headers TEXT,
            PRIMARY KEY (topic, partition, offset)
        )
        """
    )
    conn.commit()
    conn.close()
    return db


class _Writer:
    """Tiny helper to append records, auto-incrementing offset."""

    def __init__(self, db: Path):
        self.db = db
        self._off = 0

    def add(self, topic: str, type_: str, ts_ms: int, payload: dict, key: str | None = None):
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO records (topic, partition, offset, timestamp, key, type, source, payload, headers) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (topic, 0, self._off, ts_ms, key, type_, "test", json.dumps(payload), None),
        )
        self._off += 1
        conn.commit()
        conn.close()

    def dep_event(self, name: str, ts_ms: int, *, state: str, prev_state: str,
                  action: str, detail: str = "", **extra):
        payload = {
            "schema_v": 1, "name": name, "state": state, "prev_state": prev_state,
            "action_taken": action, "detail": detail,
        }
        payload.update(extra)
        self.add("system", "health.dependency", ts_ms, payload, key=name)


# Convenient anchor: a fixed "now" in ms.
NOW = 1_700_000_000_000  # 2023-11-14T22:13:20Z
MIN = 60_000
HOUR = 3600_000


# ──────────────────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────────────────

def test_empty_db_yields_empty_report(bus_db: Path):
    conn = hr.open_bus_db(bus_db)
    report = hr.build_report(conn, hours=24, now_ms=NOW)
    conn.close()
    assert report.dependencies == {}
    assert report.transitions == []
    assert report.stuck_nudges == []
    assert report.slo_ok is True


def test_single_incident_mttr_and_current_state(bus_db: Path):
    w = _Writer(bus_db)
    start = NOW - 30 * MIN
    # Incident: HEALTHY→DOWN, then RECOVERING attempt 1, then recovered HEALTHY 90s later.
    w.dep_event("chrome_control", start, state="DOWN", prev_state="HEALTHY",
                action="probe", detail="ping timed out")
    w.dep_event("chrome_control", start + 5_000, state="RECOVERING", prev_state="DOWN",
                action="recover", detail="recovery attempt 1/3", attempt=1, max_attempts=3)
    w.dep_event("chrome_control", start + 90_000, state="HEALTHY", prev_state="RECOVERING",
                action="recover", detail="recovered on attempt 1/3 — reconnected",
                attempt=1, max_attempts=3, recovered=True)

    conn = hr.open_bus_db(bus_db)
    report = hr.build_report(conn, hours=24, now_ms=NOW)
    conn.close()

    s = report.dependencies["chrome_control"]
    assert s.current_state == "HEALTHY"
    assert s.incident_count == 1
    assert s.recovery_attempts == 1
    assert s.recoveries_1h == 1
    assert s.recoveries_24h == 1
    # MTTR = 90s for the single incident → P50 == P95 == 90.
    assert s.mttr_p50_s == pytest.approx(90.0)
    assert s.mttr_p95_s == pytest.approx(90.0)
    assert s.slo_misses == []
    assert report.slo_ok is True


def test_mttr_percentiles_over_multiple_incidents(bus_db: Path):
    w = _Writer(bus_db)
    # 5 incidents with durations 30, 60, 90, 120, 600 seconds.
    durations = [30, 60, 90, 120, 600]
    base = NOW - 20 * HOUR
    for i, d in enumerate(durations):
        t0 = base + i * HOUR  # space them an hour apart so they're separate
        w.dep_event("signal_cli", t0, state="DOWN", prev_state="HEALTHY",
                    action="probe", detail="socket dead")
        w.dep_event("signal_cli", t0 + 1_000, state="RECOVERING", prev_state="DOWN",
                    action="recover", detail="recovery attempt 1/3", attempt=1, max_attempts=3)
        w.dep_event("signal_cli", t0 + d * 1000, state="HEALTHY", prev_state="RECOVERING",
                    action="recover", detail="recovered", attempt=1, max_attempts=3, recovered=True)

    conn = hr.open_bus_db(bus_db)
    report = hr.build_report(conn, hours=24, now_ms=NOW)
    conn.close()

    s = report.dependencies["signal_cli"]
    assert s.incident_count == 5
    # Nearest-rank: P50 of [30,60,90,120,600] → rank ceil(0.5*5)=3 → 90.
    assert s.mttr_p50_s == pytest.approx(90.0)
    # P95 → rank ceil(0.95*5)=5 → 600.
    assert s.mttr_p95_s == pytest.approx(600.0)
    # 600s incident > SLO_INCIDENT_CEILING (720s)? No — 600 < 720. But P95 600 > 360 budget.
    assert any("P95" in m for m in s.slo_misses)
    assert report.slo_ok is False
    # None of these incidents are within the last hour except the last one's recovery
    # (base+4h+600s vs NOW=base+20h) — actually all are >1h ago.
    assert s.recoveries_24h == 5


def test_recovery_attempts_counted_per_attempt_not_per_event(bus_db: Path):
    w = _Writer(bus_db)
    t0 = NOW - 10 * MIN
    # 3 attempts: 2 fail, 3rd succeeds.
    w.dep_event("bus_consumers", t0, state="DOWN", prev_state="HEALTHY",
                action="probe", detail="consumer DEAD")
    # attempt 1
    w.dep_event("bus_consumers", t0 + 1_000, state="RECOVERING", prev_state="DOWN",
                action="recover", detail="recovery attempt 1/3", attempt=1, max_attempts=3)
    w.dep_event("bus_consumers", t0 + 2_000, state="RECOVERING", prev_state="RECOVERING",
                action="recover", detail="recovery attempt 1/3 failed", attempt=1, max_attempts=3, recovered=False)
    # attempt 2
    w.dep_event("bus_consumers", t0 + 62_000, state="RECOVERING", prev_state="RECOVERING",
                action="recover", detail="recovery attempt 2/3", attempt=2, max_attempts=3)
    w.dep_event("bus_consumers", t0 + 63_000, state="RECOVERING", prev_state="RECOVERING",
                action="recover", detail="recovery attempt 2/3 failed", attempt=2, max_attempts=3, recovered=False)
    # attempt 3 → success
    w.dep_event("bus_consumers", t0 + 300_000, state="RECOVERING", prev_state="RECOVERING",
                action="recover", detail="recovery attempt 3/3", attempt=3, max_attempts=3)
    w.dep_event("bus_consumers", t0 + 305_000, state="HEALTHY", prev_state="RECOVERING",
                action="recover", detail="recovered on attempt 3/3", attempt=3, max_attempts=3, recovered=True)

    conn = hr.open_bus_db(bus_db)
    report = hr.build_report(conn, hours=24, now_ms=NOW)
    conn.close()

    s = report.dependencies["bus_consumers"]
    assert s.recovery_attempts == 3
    assert s.recoveries_1h == 1
    assert s.incident_count == 1
    # Incident duration ≈ 305s — under the 720s incident ceiling but over the
    # 120s P50 budget, so it's an SLO miss (but not an "incident ceiling" one).
    assert s.mttr_p50_s == pytest.approx(305.0)
    assert any("P50" in m for m in s.slo_misses)
    assert not any("incident at" in m for m in s.slo_misses)


def test_escalation_and_ongoing_incident(bus_db: Path):
    w = _Writer(bus_db)
    t0 = NOW - 15 * MIN
    w.dep_event("chrome_control", t0, state="DOWN", prev_state="HEALTHY",
                action="probe", detail="extension errored")
    # immediate escalation (the one human-required state)
    w.dep_event("chrome_control", t0 + 1_000, state="ESCALATED", prev_state="DOWN",
                action="escalate", detail="reload chrome-control at chrome://extensions/")
    # still down later (re-probe)
    w.dep_event("chrome_control", t0 + 5 * MIN, state="ESCALATED", prev_state="ESCALATED",
                action="escalate", detail="still down — extension errored")

    conn = hr.open_bus_db(bus_db)
    report = hr.build_report(conn, hours=24, now_ms=NOW)
    conn.close()

    s = report.dependencies["chrome_control"]
    assert s.current_state == "ESCALATED"
    assert s.last_escalation_detail.startswith("still down") or "reload" in s.last_escalation_detail
    assert s.ongoing_incident is not None
    assert s.ongoing_incident.escalated is True
    # 15 min ongoing > 12 min ceiling → SLO miss.
    assert any("ONGOING" in m for m in s.slo_misses)
    assert report.slo_ok is False
    # No closed incidents → no MTTR.
    assert s.incident_count == 0
    assert s.mttr_p50_s is None


def test_recovery_frequency_alarm_flagged(bus_db: Path):
    w = _Writer(bus_db)
    t0 = NOW - 30 * MIN
    # An escalate event carrying recoveries_in_window — the recovery-frequency alarm.
    w.dep_event("chrome_control", t0, state="HEALTHY", prev_state="HEALTHY",
                action="escalate",
                detail="chrome_control has self-recovered 6× in the last 60 min",
                recoveries_in_window=6, recovery_alarm_k=5)
    conn = hr.open_bus_db(bus_db)
    report = hr.build_report(conn, hours=24, now_ms=NOW)
    conn.close()
    s = report.dependencies["chrome_control"]
    assert s.recovery_alarm_fired is True
    assert any("alarm" in m.lower() for m in s.slo_misses)
    assert report.slo_ok is False


def test_dep_filter(bus_db: Path):
    w = _Writer(bus_db)
    t0 = NOW - 10 * MIN
    for name in ("chrome_control", "signal_cli"):
        w.dep_event(name, t0, state="DOWN", prev_state="HEALTHY", action="probe", detail="x")
        w.dep_event(name, t0 + 60_000, state="HEALTHY", prev_state="DOWN", action="recover",
                    detail="recovered", attempt=1, max_attempts=3, recovered=True)
    # Also a stuck nudge — should be excluded when --dep is set.
    w.add("sessions", "session.stuck_nudge", t0, {
        "schema_v": 1, "session_name": "imessage/_15555550100", "chat_id": "+15555550100",
        "idle_minutes": 6, "detection": "marker", "contact_name": "Test"})

    conn = hr.open_bus_db(bus_db)
    report = hr.build_report(conn, hours=24, dep="chrome_control", now_ms=NOW)
    conn.close()

    assert set(report.dependencies.keys()) == {"chrome_control"}
    assert report.stuck_nudges == []
    # transitions only contain chrome_control rows
    assert all(e.name == "chrome_control" for e in report.transitions if e.type == hr.DEPENDENCY_EVENT)
    assert not any(e.type != hr.DEPENDENCY_EVENT for e in report.transitions)


def test_stuck_nudges_surfaced(bus_db: Path):
    w = _Writer(bus_db)
    t0 = NOW - 5 * MIN
    w.add("sessions", "session.stuck_nudge", t0, {
        "schema_v": 1, "session_name": "imessage/_15555550100", "chat_id": "+15555550100",
        "idle_minutes": 7, "detection": "heuristic", "contact_name": "Eric",
        "committed_text": "waiting on Eric to click the toolbar icon"})
    conn = hr.open_bus_db(bus_db)
    report = hr.build_report(conn, hours=24, now_ms=NOW)
    conn.close()
    assert len(report.stuck_nudges) == 1
    ev = report.stuck_nudges[0]
    assert ev.payload["detection"] == "heuristic"
    assert ev.payload["contact_name"] == "Eric"
    # render shouldn't blow up
    txt = hr.render_report(report)
    assert "Stuck-session nudges" in txt
    assert "Eric" in txt


def test_window_excludes_old_events(bus_db: Path):
    w = _Writer(bus_db)
    # Event 48h ago — outside a 24h window.
    w.dep_event("disk", NOW - 48 * HOUR, state="DEGRADED", prev_state="HEALTHY",
                action="probe", detail="old")
    # Event 2h ago — inside.
    w.dep_event("disk", NOW - 2 * HOUR, state="HEALTHY", prev_state="HEALTHY",
                action="probe", detail="recent ok")
    conn = hr.open_bus_db(bus_db)
    report = hr.build_report(conn, hours=24, now_ms=NOW)
    conn.close()
    s = report.dependencies["disk"]
    # Only the recent HEALTHY event seen.
    assert s.current_state == "HEALTHY"
    assert s.incident_count == 0


def test_extra_health_events_in_transition_tail(bus_db: Path):
    w = _Writer(bus_db)
    t0 = NOW - 1 * HOUR
    w.add("system", "health.haiku_verdict", t0, {
        "check_run_id": "abc", "check_type": "deep", "session_name": "imessage/_1",
        "verdict": "HEALTHY", "action_taken": "none"})
    w.add("system", "health.circuit_breaker", t0 + 1_000, {
        "session_name": "imessage/_2", "transition": "opened", "restart_count": 4})
    w.add("system", "health.quota_alert", t0 + 2_000, {
        "quota_type": "7-day opus", "utilization": 91.2, "threshold": 90})
    conn = hr.open_bus_db(bus_db)
    report = hr.build_report(conn, hours=24, now_ms=NOW)
    conn.close()
    types = {e.type for e in report.transitions}
    assert types == {"health.haiku_verdict", "health.circuit_breaker", "health.quota_alert"}
    txt = hr.render_report(report)
    assert "haiku_verdict" in txt
    assert "circuit_breaker" in txt
    assert "quota_alert" in txt


def test_transition_limit_tails_most_recent(bus_db: Path):
    w = _Writer(bus_db)
    base = NOW - 2 * HOUR
    for i in range(10):
        w.dep_event("disk", base + i * MIN, state="HEALTHY" if i % 2 else "DEGRADED",
                    prev_state="DEGRADED" if i % 2 else "HEALTHY",
                    action="probe", detail=f"event {i}")
    conn = hr.open_bus_db(bus_db)
    report = hr.build_report(conn, hours=24, transition_limit=3, now_ms=NOW)
    conn.close()
    assert len(report.transitions) == 3
    # Most recent 3 → events 7, 8, 9.
    details = [e.detail for e in report.transitions]
    assert details == ["event 7", "event 8", "event 9"]


def test_open_bus_db_missing_file(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        hr.open_bus_db(tmp_path / "nope.db")


def test_recovered_after_escalation_closes_incident(bus_db: Path):
    w = _Writer(bus_db)
    t0 = NOW - 20 * MIN
    w.dep_event("chrome_control", t0, state="DOWN", prev_state="HEALTHY",
                action="probe", detail="errored")
    w.dep_event("chrome_control", t0 + 1_000, state="ESCALATED", prev_state="DOWN",
                action="escalate", detail="reload manually")
    # Human fixed it; next probe sees OK → recovered_after_escalation.
    w.dep_event("chrome_control", t0 + 8 * MIN, state="HEALTHY", prev_state="ESCALATED",
                action="probe", detail="recovered after escalation — ping ok",
                recovered_after_escalation=True)
    conn = hr.open_bus_db(bus_db)
    report = hr.build_report(conn, hours=24, now_ms=NOW)
    conn.close()
    s = report.dependencies["chrome_control"]
    assert s.current_state == "HEALTHY"
    assert s.incident_count == 1
    # Incident lasted 8min = 480s — within the 720s ceiling, but P50 480 > 120 budget.
    assert s.mttr_p50_s == pytest.approx(480.0)
    assert any("P50" in m for m in s.slo_misses)
    # recovered_after_escalation counts as a recovery in the window.
    assert s.recoveries_24h == 1
