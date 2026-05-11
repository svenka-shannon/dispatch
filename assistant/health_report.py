"""Resilience / health-history reporting (self-healing-resilience plan §4.4).

Queries the bus ``records`` table for the structured self-healing trail and
aggregates it into a postmortem-friendly view:

- ``health.dependency`` (Phase 2 ``DependencyHealthRunner``) — the per-dependency
  state machine: HEALTHY→DEGRADED→DOWN→RECOVERING→(HEALTHY|ESCALATED), recovery
  attempts, escalations, the recovery-frequency alarm.
- ``session.stuck_nudge`` (Phase 1 blocked-session watchdog) — sessions nudged
  out of an idle-with-an-outstanding-commitment state.
- ``health.haiku_verdict`` / ``health.circuit_breaker`` / ``health.quota_alert``
  — the existing SDK-session health diagnostics, included in the transition tail.

MTTR is computed by pairing, per dependency, each entry into a non-HEALTHY
"impaired" state (DOWN or DEGRADED — whichever first marks the start of an
incident) with the next return to HEALTHY, in timestamp order. The wall-clock
gap between those two events is one incident's recovery time. P50/P95 are taken
over those gaps. Incidents still open at the end of the window are reported
separately (ongoing) and excluded from the percentiles.

Pure functions over a sqlite connection — no daemon, no IPC, no side effects.
The CLI subcommand (``claude-assistant health-history``) is a thin wrapper; the
aggregation lives here so it's unit-testable against a temp bus.db.

bus.db is WAL-mode; opening a second read-only connection while the daemon writes
is safe.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# ── SLO budgets (plan §3) ──────────────────────────────────────────────
# Detection ≤ 2 min, recovery attempted ≤ 30 s of detection, ≤ 3 attempts with
# exponential backoff (0/60/240 s), escalation within ≤ 5 min of first detection.
# MTTR target: P50 < 2 min, P95 < 6 min.
SLO_MTTR_P50_S = 120.0
SLO_MTTR_P95_S = 360.0
# Hard ceiling on a single incident before we flag it as an SLO miss: time to
# escalation budget (≈5 min) plus the worst-case backoff chain (0+60+240) plus
# probe slack — anything longer than ~12 min that wasn't a human-required state
# is a problem worth surfacing.
SLO_INCIDENT_CEILING_S = 12 * 60.0

# Event types we surface, by topic.
DEPENDENCY_EVENT = "health.dependency"
STUCK_NUDGE_EVENT = "session.stuck_nudge"
EXTRA_HEALTH_EVENTS = ("health.haiku_verdict", "health.circuit_breaker", "health.quota_alert")

# States that count as "impaired" (an incident is in progress).
_IMPAIRED_STATES = {"DOWN", "DEGRADED", "RECOVERING", "ESCALATED"}

DEFAULT_BUS_DB = Path.home() / "dispatch" / "state" / "bus.db"


# ──────────────────────────────────────────────────────────────────────
# Row model
# ──────────────────────────────────────────────────────────────────────

@dataclass
class HealthEvent:
    """One ``records`` row, payload decoded."""
    topic: str
    type: str
    timestamp_ms: int
    key: str | None
    payload: dict[str, Any]

    @property
    def dt(self) -> datetime:
        return datetime.fromtimestamp(self.timestamp_ms / 1000.0, tz=timezone.utc)

    @property
    def name(self) -> str | None:
        """Dependency name (health.dependency) — falls back to the record key."""
        return self.payload.get("name") or self.key

    @property
    def state(self) -> str | None:
        return self.payload.get("state")

    @property
    def prev_state(self) -> str | None:
        return self.payload.get("prev_state")

    @property
    def action_taken(self) -> str | None:
        return self.payload.get("action_taken")

    @property
    def detail(self) -> str:
        return str(self.payload.get("detail") or "")


# ──────────────────────────────────────────────────────────────────────
# DB access
# ──────────────────────────────────────────────────────────────────────

def open_bus_db(db_path: str | Path | None = None) -> sqlite3.Connection:
    """Open a read-only connection to bus.db (WAL — safe alongside daemon writes)."""
    path = Path(db_path) if db_path else DEFAULT_BUS_DB
    if not path.exists():
        raise FileNotFoundError(f"bus.db not found: {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_events(
    conn: sqlite3.Connection,
    *,
    since_ms: int,
    until_ms: int | None = None,
    types: Iterable[str] | None = None,
    dep: str | None = None,
) -> list[HealthEvent]:
    """Fetch + decode the relevant ``records`` rows in timestamp order.

    `types` defaults to all health/resilience event types. `dep` filters
    health.dependency rows to a single dependency name (other event types are
    dropped when `dep` is set, since they're not per-dependency).
    """
    if types is None:
        types = (DEPENDENCY_EVENT, STUCK_NUDGE_EVENT, *EXTRA_HEALTH_EVENTS)
    types = list(types)
    if not types:
        return []
    placeholders = ",".join("?" for _ in types)
    sql = (
        f"SELECT topic, type, timestamp, key, payload FROM records "
        f"WHERE type IN ({placeholders}) AND timestamp >= ?"
    )
    params: list[Any] = [*types, since_ms]
    if until_ms is not None:
        sql += " AND timestamp <= ?"
        params.append(until_ms)
    sql += " ORDER BY timestamp ASC, topic ASC, partition ASC, offset ASC"
    rows = conn.execute(sql, params).fetchall()
    events: list[HealthEvent] = []
    for r in rows:
        try:
            payload = json.loads(r["payload"]) if r["payload"] else {}
        except (json.JSONDecodeError, TypeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {"_raw": payload}
        ev = HealthEvent(
            topic=r["topic"], type=r["type"], timestamp_ms=int(r["timestamp"]),
            key=r["key"], payload=payload,
        )
        if dep is not None:
            # When filtering by dependency, only dependency rows for that dep.
            if ev.type != DEPENDENCY_EVENT or ev.name != dep:
                continue
        events.append(ev)
    return events


# ──────────────────────────────────────────────────────────────────────
# Aggregation
# ──────────────────────────────────────────────────────────────────────

@dataclass
class Incident:
    """One impaired→healthy span for a dependency (MTTR is its duration)."""
    name: str
    start_ms: int
    start_state: str
    end_ms: int | None = None        # None → still ongoing at end of window
    escalated: bool = False
    escalated_at_ms: int | None = None

    @property
    def duration_s(self) -> float | None:
        if self.end_ms is None:
            return None
        return max(0.0, (self.end_ms - self.start_ms) / 1000.0)

    @property
    def time_to_escalation_s(self) -> float | None:
        if self.escalated_at_ms is None:
            return None
        return max(0.0, (self.escalated_at_ms - self.start_ms) / 1000.0)


@dataclass
class DependencySummary:
    name: str
    current_state: str = "HEALTHY"
    last_event_ms: int | None = None
    recoveries_1h: int = 0
    recoveries_24h: int = 0
    recovery_attempts: int = 0          # all recover-action events in window
    mttr_p50_s: float | None = None
    mttr_p95_s: float | None = None
    incident_count: int = 0             # closed incidents (basis for MTTR)
    ongoing_incident: Incident | None = None
    last_escalation_ms: int | None = None
    last_escalation_detail: str = ""
    recovery_alarm_fired: bool = False
    incidents: list[Incident] = field(default_factory=list)
    # SLO misses: list of human-readable strings.
    slo_misses: list[str] = field(default_factory=list)


def _percentile(values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile (pct in [0,100]). Returns None for empty input."""
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    # Nearest-rank: ceil(pct/100 * N), 1-indexed.
    import math
    rank = max(1, math.ceil(pct / 100.0 * len(s)))
    return s[min(rank, len(s)) - 1]


def summarize_dependencies(
    events: list[HealthEvent],
    *,
    now_ms: int,
    window_start_ms: int,
) -> dict[str, DependencySummary]:
    """Aggregate health.dependency events into per-dependency summaries.

    Pairs impaired→HEALTHY transitions in time order to derive incidents/MTTR.
    `now_ms` is the wall-clock "now" used for the 1h/24h recovery counts and to
    measure ongoing incidents.
    """
    dep_events: dict[str, list[HealthEvent]] = {}
    for ev in events:
        if ev.type != DEPENDENCY_EVENT:
            continue
        name = ev.name
        if not name:
            continue
        dep_events.setdefault(name, []).append(ev)

    one_hour_ago = now_ms - 3600_000
    summaries: dict[str, DependencySummary] = {}

    for name, evs in dep_events.items():
        evs.sort(key=lambda e: e.timestamp_ms)
        summ = DependencySummary(name=name)
        # Current state = state of the last event.
        last = evs[-1]
        summ.current_state = last.state or "HEALTHY"
        summ.last_event_ms = last.timestamp_ms

        cur_incident: Incident | None = None
        for ev in evs:
            state = ev.state
            action = ev.action_taken
            detail = ev.detail

            # Track recoveries (successful) — payload carries recovered=True OR
            # recovered_after_escalation=True; also action_taken=recover entering HEALTHY.
            is_recovery_success = bool(
                ev.payload.get("recovered") is True
                or ev.payload.get("recovered_after_escalation") is True
            )
            # Count distinct recovery attempts. The runner emits exactly one
            # "recovery attempt N/M" RECOVERING transition per attempt (action=recover,
            # state=RECOVERING, has `attempt`, no `recovered` key); the subsequent
            # success/failure event also has action=recover but carries a `recovered`
            # key, so we don't double-count.
            if (
                action == "recover" and state == "RECOVERING"
                and "attempt" in ev.payload and "recovered" not in ev.payload
            ):
                summ.recovery_attempts += 1

            if is_recovery_success:
                if ev.timestamp_ms >= one_hour_ago:
                    summ.recoveries_1h += 1
                if ev.timestamp_ms >= window_start_ms:
                    summ.recoveries_24h += 1

            # Recovery-frequency alarm fired? It's an escalate-action event whose
            # payload carries recoveries_in_window / recovery_alarm_k.
            if action == "escalate" and "recoveries_in_window" in ev.payload:
                summ.recovery_alarm_fired = True

            # Escalations.
            if action == "escalate" or state == "ESCALATED":
                summ.last_escalation_ms = ev.timestamp_ms
                if detail:
                    summ.last_escalation_detail = detail
                if cur_incident is not None and not cur_incident.escalated:
                    cur_incident.escalated = True
                    cur_incident.escalated_at_ms = ev.timestamp_ms

            # Incident open/close based on state.
            if state in _IMPAIRED_STATES:
                if cur_incident is None:
                    cur_incident = Incident(
                        name=name, start_ms=ev.timestamp_ms,
                        start_state=state if state in ("DOWN", "DEGRADED") else "DOWN",
                    )
                    if state == "ESCALATED":
                        cur_incident.escalated = True
                        cur_incident.escalated_at_ms = ev.timestamp_ms
            elif state == "HEALTHY":
                if cur_incident is not None:
                    cur_incident.end_ms = ev.timestamp_ms
                    summ.incidents.append(cur_incident)
                    cur_incident = None
            # SKIP / other states: leave incident as-is (enabled_when reset emits HEALTHY).

        if cur_incident is not None:
            # Still impaired at end of window.
            summ.ongoing_incident = cur_incident
            summ.incidents.append(cur_incident)

        closed = [i for i in summ.incidents if i.end_ms is not None]
        summ.incident_count = len(closed)
        durations = [d for d in (i.duration_s for i in closed) if d is not None]
        summ.mttr_p50_s = _percentile(durations, 50)
        summ.mttr_p95_s = _percentile(durations, 95)

        # SLO checks.
        if summ.mttr_p50_s is not None and summ.mttr_p50_s > SLO_MTTR_P50_S:
            summ.slo_misses.append(
                f"MTTR P50 {summ.mttr_p50_s:.0f}s > {SLO_MTTR_P50_S:.0f}s budget"
            )
        if summ.mttr_p95_s is not None and summ.mttr_p95_s > SLO_MTTR_P95_S:
            summ.slo_misses.append(
                f"MTTR P95 {summ.mttr_p95_s:.0f}s > {SLO_MTTR_P95_S:.0f}s budget"
            )
        for inc in closed:
            d = inc.duration_s
            if d is not None and d > SLO_INCIDENT_CEILING_S:
                summ.slo_misses.append(
                    f"incident at {_fmt_ts(inc.start_ms)} took {d:.0f}s (> {SLO_INCIDENT_CEILING_S:.0f}s)"
                )
        if summ.ongoing_incident is not None:
            age = (now_ms - summ.ongoing_incident.start_ms) / 1000.0
            if age > SLO_INCIDENT_CEILING_S:
                summ.slo_misses.append(
                    f"ONGOING incident since {_fmt_ts(summ.ongoing_incident.start_ms)} ({age:.0f}s and counting)"
                )
        if summ.recovery_alarm_fired:
            summ.slo_misses.append("recovery-frequency alarm fired (auto-recovery masking a deeper problem)")

        summaries[name] = summ
    return summaries


@dataclass
class Report:
    window_hours: float
    now_ms: int
    window_start_ms: int
    dependencies: dict[str, DependencySummary]
    transitions: list[HealthEvent]      # the recent-transition tail (dep + extra health events)
    stuck_nudges: list[HealthEvent]
    slo_ok: bool                        # True if no dependency missed an SLO budget

    @property
    def all_slo_misses(self) -> list[str]:
        out: list[str] = []
        for name, summ in self.dependencies.items():
            for m in summ.slo_misses:
                out.append(f"{name}: {m}")
        return out


def build_report(
    conn: sqlite3.Connection,
    *,
    hours: float = 24.0,
    dep: str | None = None,
    transition_limit: int = 50,
    now_ms: int | None = None,
) -> Report:
    """End-to-end: query bus.db → aggregate → Report. Thin; the CLI wraps this."""
    if now_ms is None:
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    window_start_ms = now_ms - int(hours * 3600_000)
    events = fetch_events(conn, since_ms=window_start_ms, dep=dep)

    deps = summarize_dependencies(events, now_ms=now_ms, window_start_ms=window_start_ms)

    # Transition tail: dependency state-change events + the extra health events,
    # most recent last (or, for display, most recent first — caller decides).
    transitions = [
        e for e in events
        if e.type == DEPENDENCY_EVENT or e.type in EXTRA_HEALTH_EVENTS
    ]
    # Keep the most recent `transition_limit` (no truncation of content; just a tail).
    if transition_limit and transition_limit > 0:
        transitions = transitions[-transition_limit:]

    stuck = [e for e in events if e.type == STUCK_NUDGE_EVENT]

    slo_ok = all(not summ.slo_misses for summ in deps.values())
    return Report(
        window_hours=hours, now_ms=now_ms, window_start_ms=window_start_ms,
        dependencies=deps, transitions=transitions, stuck_nudges=stuck, slo_ok=slo_ok,
    )


# ──────────────────────────────────────────────────────────────────────
# Rendering (plain text — used by the CLI)
# ──────────────────────────────────────────────────────────────────────

def _fmt_ts(ms: int | None) -> str:
    if ms is None:
        return "—"
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def _fmt_dur(s: float | None) -> str:
    if s is None:
        return "—"
    if s < 90:
        return f"{s:.0f}s"
    m = s / 60.0
    if m < 90:
        return f"{m:.1f}m"
    return f"{m / 60.0:.1f}h"


def render_report(report: Report, *, dep_filter: str | None = None) -> str:
    lines: list[str] = []
    span = f"{report.window_hours:g}h"
    lines.append(
        f"Resilience / health history — last {span} "
        f"({_fmt_ts(report.window_start_ms)} → {_fmt_ts(report.now_ms)})"
        + (f"  [dep={dep_filter}]" if dep_filter else "")
    )
    lines.append("")

    # Per-dependency table.
    if not report.dependencies:
        lines.append("  (no health.dependency events in this window)")
    else:
        hdr = (
            f"  {'DEPENDENCY':<16} {'STATE':<10} {'REC 1h/24h':>10} "
            f"{'MTTR P50':>9} {'MTTR P95':>9} {'INCID':>6} {'ALARM':>6}  LAST ESCALATION"
        )
        lines.append(hdr)
        lines.append("  " + "-" * (len(hdr) - 2))
        for name in sorted(report.dependencies):
            s = report.dependencies[name]
            esc = (
                f"{_fmt_ts(s.last_escalation_ms)} — {s.last_escalation_detail}"
                if s.last_escalation_ms else "—"
            )
            lines.append(
                f"  {name:<16} {s.current_state:<10} "
                f"{f'{s.recoveries_1h}/{s.recoveries_24h}':>10} "
                f"{_fmt_dur(s.mttr_p50_s):>9} {_fmt_dur(s.mttr_p95_s):>9} "
                f"{s.incident_count:>6} {('YES' if s.recovery_alarm_fired else '—'):>6}  {esc}"
            )
            if s.ongoing_incident is not None:
                age = (report.now_ms - s.ongoing_incident.start_ms) / 1000.0
                lines.append(
                    f"  {'':<16} └─ ONGOING incident since {_fmt_ts(s.ongoing_incident.start_ms)} "
                    f"({_fmt_dur(age)} and counting)"
                )

    # SLO check (one line).
    lines.append("")
    if report.slo_ok:
        lines.append("  SLO: OK — every recovered dependency stayed within the §3 budgets.")
    else:
        lines.append("  SLO: MISSES —")
        for m in report.all_slo_misses:
            lines.append(f"    • {m}")

    # Stuck-session nudges.
    if report.stuck_nudges:
        lines.append("")
        lines.append(f"  Stuck-session nudges ({len(report.stuck_nudges)}):")
        for ev in report.stuck_nudges:
            p = ev.payload
            who = p.get("contact_name") or p.get("session_name") or ev.key or "?"
            det = p.get("detection", "?")
            idle = p.get("idle_minutes")
            idle_s = f"{idle}m idle" if idle is not None else "?"
            committed = p.get("committed_text") or ""
            committed = f" — \"{committed}\"" if committed else ""
            lines.append(f"    {_fmt_ts(ev.timestamp_ms)}  {who}  [{det}]  {idle_s}{committed}")

    # Transition tail.
    lines.append("")
    lines.append(f"  Recent transitions (last {len(report.transitions)}):")
    if not report.transitions:
        lines.append("    (none)")
    for ev in report.transitions:
        ts = _fmt_ts(ev.timestamp_ms)
        if ev.type == DEPENDENCY_EVENT:
            name = ev.name or "?"
            prev = ev.prev_state or "?"
            new = ev.state or "?"
            act = ev.action_taken or "?"
            extra = ""
            if "attempt" in ev.payload and "max_attempts" in ev.payload:
                extra = f" [{ev.payload['attempt']}/{ev.payload['max_attempts']}]"
            lines.append(f"    {ts}  {name:<16} {prev}→{new}{extra}  {act:<8}  {ev.detail}")
        else:
            # haiku_verdict / circuit_breaker / quota_alert — show the key fields.
            p = ev.payload
            short = ev.type.replace("health.", "")
            bits: list[str] = []
            for k in ("session_name", "verdict", "transition", "action_taken",
                      "quota_type", "utilization", "threshold", "restart_count", "check_type"):
                v = p.get(k)
                if v not in (None, ""):
                    bits.append(f"{k}={v}")
            lines.append(f"    {ts}  {short:<18} {' '.join(bits)}")

    return "\n".join(lines)
