"""Outage briefing — let a session perceive its own downtime.

An SDK session cannot see a hole in its own timeline. When OAuth dies or the
daemon restarts, the session simply receives the next message as if nothing
happened. On 2026-08-31 OAuth died at 06:28:53 PDT and recovered ~08:57; a
reminder fired at 07:27 into a dead session, the turn errored in 54ms, and the
message was still marked delivered. When the user then texted "Login fixed",
the session had no evidence an outage had occurred and misread it as referring
to an unrelated login thread.

The daemon already records every fact needed (state/bus.db + auth_failure.json).
This module turns those facts into one short [SYSTEM] line that the session
prepends to its next turn.

DESIGN RULES
  1. NEVER infer an outage from idleness. An outage must be marked by an
     explicit signal — a session.auth_failure event / auth_failure.json flag,
     or a daemon restart gap. A session idle for 8 hours is NOT down, and must
     never be briefed. This is the primary false-positive guard.
  2. Blips shorter than MIN_OUTAGE_SECONDS are never reported.
  3. Read-only bus access, short timeout, every failure degrades to "no
     briefing". A briefing is never worth breaking a turn over.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from typing import Optional

from assistant.common import AUTH_FAILURE_FLAG, STATE_DIR

BUS_DB = STATE_DIR / "bus.db"

# Never brief for a blip. Guards against a 30-second token refresh hiccup or a
# fast `claude-assistant restart-sessions` being reported as an "outage".
MIN_OUTAGE_SECONDS = 120

# Cap the enumerated items so the briefing can never become a wall of text.
MAX_ITEMS = 5

# Don't look further back than this when reconstructing a window.
LOOKBACK_HOURS = 24


# ── bus access ────────────────────────────────────────────────────────────

def _conn() -> sqlite3.Connection:
    """Read-only connection, separate from the Producer's writer connection."""
    return sqlite3.connect(f"file:{BUS_DB}?mode=ro", uri=True, timeout=2.0)


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000).astimezone()


def _clock(dt: datetime) -> str:
    # Include the tz abbrev: the model reasons across EDT/PDT and a bare
    # "06:28" is exactly the kind of ambiguity that caused the misread.
    return dt.strftime("%H:%M %Z").strip()


def _duration(seconds: float) -> str:
    mins = int(seconds // 60)
    return f"{mins // 60}h{mins % 60:02d}m" if mins >= 60 else f"{mins}m"


# ── window computation ────────────────────────────────────────────────────

def read_flag_started_at() -> Optional[datetime]:
    """Start time recorded in state/auth_failure.json.

    NOTE: _note_auth_failure OVERWRITES this file on every failing turn, so its
    `ts` is the LAST failure of the outage, not the first. Use it only as a
    fallback for auth_outage_start(), which reconstructs the true start.
    """
    try:
        return datetime.fromisoformat(json.loads(AUTH_FAILURE_FLAG.read_text())["ts"])
    except Exception:
        try:
            return _dt(int(AUTH_FAILURE_FLAG.stat().st_mtime * 1000))
        except Exception:
            return None


def auth_outage_start(session_name: str, before: Optional[datetime] = None) -> Optional[datetime]:
    """First session.auth_failure in the run of failures that is still open.

    Definition: the earliest auth failure that occurred AFTER this session's
    last clean turn (sdk.turn_complete with is_error=0). That anchor is what
    makes the window a real outage boundary rather than "whenever the flag file
    happened to get rewritten".

    Verified against the 2026-08-31 incident: last clean turn 06:01:47,
    auth failures at 06:28:53 and 07:27:35, recovery 08:57:42 -> returns
    06:28:53.
    """
    before = before or datetime.now().astimezone()
    floor = _ms(before - timedelta(hours=LOOKBACK_HOURS))
    try:
        with _conn() as c:
            row = c.execute(
                """
                SELECT MIN(timestamp) FROM records
                 WHERE topic = 'sessions'
                   AND type = 'session.auth_failure'
                   AND json_extract(payload, '$.session_name') = ?
                   AND timestamp <= ?
                   AND timestamp > COALESCE((
                         SELECT MAX(timestamp) FROM records
                          WHERE type = 'sdk.turn_complete'
                            AND json_extract(payload, '$.session_name') = ?
                            AND json_extract(payload, '$.is_error') IN (0, 'false')
                            AND timestamp <= ?
                       ), ?)
                """,
                (session_name, _ms(before), session_name, _ms(before), floor),
            ).fetchone()
    except Exception:
        return None
    return _dt(row[0]) if row and row[0] else None


_daemon_window: Optional[tuple] = None


def daemon_downtime_window() -> tuple[Optional[datetime], Optional[datetime]]:
    """(down_since, back_up_at) for the gap preceding THIS daemon process.

    down_since = newest bus record strictly older than the newest daemon.started
    event, i.e. the daemon's last sign of life before it went away.
    back_up_at = that daemon.started event.

    Returns (None, None) when the gap is under MIN_OUTAGE_SECONDS — a fast
    restart is not an outage worth telling the model about.

    Cached per process: the window is identical for every session recreated at
    startup, so it is computed once.
    """
    global _daemon_window
    if _daemon_window is not None:
        return _daemon_window
    result: tuple[Optional[datetime], Optional[datetime]] = (None, None)
    try:
        with _conn() as c:
            started = c.execute(
                "SELECT MAX(timestamp) FROM records WHERE type = 'daemon.started'"
            ).fetchone()
            if started and started[0]:
                up = started[0]
                prev = c.execute(
                    "SELECT MAX(timestamp) FROM records WHERE timestamp < ?", (up,)
                ).fetchone()
                if prev and prev[0] and (up - prev[0]) / 1000.0 >= MIN_OUTAGE_SECONDS:
                    result = (_dt(prev[0]), _dt(up))
    except Exception:
        result = (None, None)
    _daemon_window = result
    return result


# ── what the session missed ───────────────────────────────────────────────

def _reminders_during(chat_id: str, start: datetime, end: datetime) -> list[tuple[datetime, str]]:
    """reminder.due events for this chat inside the window.

    Everything in the window is suspect by construction: the session could not
    complete a turn, so a reminder injected into it was at best queued and at
    worst (today's case) marked delivered against a turn that died in 54ms.
    """
    try:
        with _conn() as c:
            rows = c.execute(
                """SELECT timestamp, payload FROM records
                    WHERE topic = 'reminders' AND type = 'reminder.due' AND key = ?
                      AND timestamp BETWEEN ? AND ?
                    ORDER BY timestamp""",
                (chat_id, _ms(start), _ms(end)),
            ).fetchall()
    except Exception:
        return []
    out = []
    for ts, payload in rows:
        try:
            title = json.loads(payload).get("title", "") or "(untitled)"
        except Exception:
            title = "(unparseable)"
        out.append((_dt(ts), title))
    return out


def _inbound_count(chat_id: str, start: datetime, end: datetime) -> int:
    try:
        with _conn() as c:
            row = c.execute(
                """SELECT COUNT(*) FROM records
                    WHERE topic = 'messages' AND type = 'message.received' AND key = ?
                      AND timestamp BETWEEN ? AND ?""",
                (chat_id, _ms(start), _ms(end)),
            ).fetchone()
        return int(row[0]) if row else 0
    except Exception:
        return 0


# ── rendering ─────────────────────────────────────────────────────────────

def render_briefing(
    *,
    chat_id: str,
    session_name: str,
    started_at: Optional[datetime],
    ended_at: Optional[datetime] = None,
    cause: str = "auth failure",
) -> Optional[str]:
    """Render the [SYSTEM] outage line, or None if there is nothing to report.

    Returns None (never raises) for: no start time, a sub-MIN_OUTAGE_SECONDS
    window, or an inverted window.
    """
    if started_at is None:
        return None
    ended_at = ended_at or datetime.now().astimezone()
    try:
        down = (ended_at - started_at).total_seconds()
    except TypeError:
        return None
    if down < MIN_OUTAGE_SECONDS:
        return None

    parts = [
        f"[SYSTEM] You were unreachable {_clock(started_at)}–{_clock(ended_at)} "
        f"({_duration(down)}, {cause}). You have a hole in this conversation: "
        f"messages you appear to be answering may refer to that outage rather "
        f"than to the last topic you remember."
    ]

    fired = _reminders_during(chat_id, started_at, ended_at)
    if fired:
        shown = fired[:MAX_ITEMS]
        listed = "; ".join(f'"{title}" (due {_clock(ts)})' for ts, title in shown)
        more = f" (+{len(fired) - len(shown)} more)" if len(fired) > len(shown) else ""
        parts.append(
            f"{len(fired)} reminder(s) fired during the window and may NOT have "
            f"reached you: {listed}{more}. Check whether each still needs action."
        )

    inbound = _inbound_count(chat_id, started_at, ended_at)
    if inbound:
        parts.append(f"{inbound} inbound message(s) also arrived during the window.")

    parts.append("Pick up any dropped work; do not apologise at length.")
    return " ".join(parts)
