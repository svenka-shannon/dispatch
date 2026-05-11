"""Per-session commitment markers.

When a chat session tells the user "I'll do X" / "waiting on you to do Y", it
should record an *outstanding commitment* so the daemon's blocked-session
watchdog can tell the difference between a session that's legitimately idle and
one that went quiet mid-task. The session sets the marker; the watchdog reads
it (and clears stale ones).

Storage: one tiny JSON file per session under ``state/commitments/``, keyed by
the session name (``{backend}/{sanitized_chat_id}`` — same value as
``SDKSession._session_name``). A file-based marker is deliberately simple: it
needs no daemon IPC, survives a daemon restart, and is trivially testable.

Schema (``state/commitments/<safe_name>.json``)::

    {"session_name": "imessage/_15555550100",
     "text": "waiting on Eric to click the chrome-control toolbar icon",
     "set_at": "2026-05-11T13:08:00+00:00"}
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from assistant.common import STATE_DIR

COMMITMENTS_DIR = STATE_DIR / "commitments"

# A commitment older than this is treated as stale (the session almost certainly
# resolved it but never cleared the marker, or it was abandoned). The watchdog
# garbage-collects these; it never nudges on a stale marker.
COMMITMENT_MAX_AGE_SECONDS = 6 * 3600  # 6 hours


def _safe_name(session_name: str) -> str:
    """Filesystem-safe filename stem for a session name."""
    return re.sub(r"[^A-Za-z0-9_.+-]", "_", session_name)


def _path_for(session_name: str) -> Path:
    return COMMITMENTS_DIR / f"{_safe_name(session_name)}.json"


def set_commitment(session_name: str, text: str) -> Path:
    """Record an outstanding commitment for ``session_name``. Returns the path."""
    COMMITMENTS_DIR.mkdir(parents=True, exist_ok=True)
    path = _path_for(session_name)
    payload = {
        "session_name": session_name,
        "text": text.strip(),
        "set_at": datetime.now(timezone.utc).isoformat(),
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(path)
    return path


def clear_commitment(session_name: str) -> bool:
    """Remove the commitment marker for ``session_name``. Returns True if one existed."""
    path = _path_for(session_name)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


def get_commitment(session_name: str) -> dict | None:
    """Return the commitment dict for ``session_name``, or None if not set/unreadable."""
    path = _path_for(session_name)
    try:
        raw = path.read_text()
    except (FileNotFoundError, OSError):
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or not data.get("text"):
        return None
    return data


def commitment_age_seconds(commitment: dict) -> float | None:
    """Seconds since the commitment was set, or None if the timestamp is unparseable."""
    set_at = commitment.get("set_at")
    if not isinstance(set_at, str):
        return None
    try:
        ts = datetime.fromisoformat(set_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds()


# ── Heuristic fallback ──────────────────────────────────────────────────
#
# When a session has no explicit marker, the watchdog falls back to scanning
# its last outbound text for a commitment phrase. Kept deliberately small and
# conservative — a false positive just means one harmless re-check nudge; we'd
# rather miss a few than nag a session that's legitimately waiting on a slow
# human reply.

_COMMITMENT_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bi['’]?ll\s+(?:get|do|check|look|handle|take care|sort|fix|retry|try|run|send|update|report|let you know|circle back|follow up)\b",
        r"\bi will\s+(?:get|do|check|look|handle|take care|sort|fix|retry|try|run|send|update|report|let you know|circle back|follow up)\b",
        r"\bworking on (?:it|that|this)\b",
        r"\bon it\b",
        r"\bwill (?:report|update|let you know|follow up|circle back|check back)\b",
        r"\bgetting (?:back|to it)\b",
        r"\bwaiting (?:on|for) you\b",
        r"\bonce you\b.*\b(?:do|click|reload|approve|confirm|reply|respond|let me know)\b",
        r"\blet me know (?:when|once|if)\b.*\band i['’]?ll\b",
        r"\bstand by\b",
        r"\bgive me a (?:sec|second|minute|moment)\b",
        r"\bhang tight\b",
    )
)

# Patterns that, if present, *cancel* a commitment match — the task is already
# reported as done/failed, so an idle session is fine. Kept narrow on purpose
# (e.g. "fixed it", not bare "fixed", which appears in "I'll get that fixed").
_RESOLVED_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\b(?:all\s+)?done\b",
        r"\bcompleted?\b(?!\s+yet)",
        r"\b(?:all\s+)?finished\b",
        r"\b(?:all\s+)?sorted\b",
        r"\bhandled (?:it|that|this)\b",
        r"\bfixed (?:it|that|this)\b",
        r"\bgave up\b",
        r"\bcouldn['’]?t\b",
        r"\bunable to\b",
        r"\b(?:has |it )?failed\b",
        r"\bescalat",  # already escalated
        r"\blet me know if you (?:need|want)\b",  # closing pleasantry
    )
)


def looks_like_commitment(text: str | None) -> str | None:
    """Return the matched snippet if ``text`` reads like an open commitment, else None.

    Conservative: ignores empty/short text; ignores text that also contains a
    "done/failed/escalated" marker (the commitment, if any, is already resolved).
    """
    if not text or not isinstance(text, str):
        return None
    stripped = text.strip()
    if len(stripped) < 4:
        return None
    # Only consider a reasonably small tail — commitments are short closing lines,
    # and scanning a giant message body invites false positives.
    tail = stripped[-600:]
    for resolved in _RESOLVED_PATTERNS:
        if resolved.search(tail):
            return None
    for pat in _COMMITMENT_PATTERNS:
        m = pat.search(tail)
        if m:
            # Return a little context around the match for the bus event.
            start = max(0, m.start() - 20)
            end = min(len(tail), m.end() + 40)
            return tail[start:end].strip()
    return None
