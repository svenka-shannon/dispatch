"""Session-initiated background investigations.

State CRUD, request building, result parsing, and rate-limit logic for the
investigation subsystem. Manager owns spawning the ephemeral session and
delivering results; this module is the data layer + originator-side helpers.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from assistant.bus_helpers import produce_event
from assistant.common import STATE_DIR

INFLIGHT_PATH = STATE_DIR / "investigations.json"
RECURSION_ENV_VAR = "CLAUDE_INVESTIGATION_TASK_ID"

PER_SESSION_MAX_INFLIGHT = 2
GLOBAL_MAX_PER_HOUR = 5
GLOBAL_RATE_WINDOW_SECONDS = 3600

_state_lock = threading.Lock()


class InvestigationError(Exception):
    pass


class RateLimitExceeded(InvestigationError):
    pass


class RecursiveDispatchError(InvestigationError):
    pass


BASH_DENY_PATTERNS_ALWAYS: tuple[str, ...] = (
    r"\bsend-sms\b",
    r"\bsend-signal\b",
    r"\bclaude-assistant\b.*\binject-prompt\b",
    r"\bclaude-assistant\b.*\b(kill-session|restart-session|stop|start)\b",
    r"\bclaude-assistant\b.*\bdispatch-investigation\b",
    r"\bremind\b\s+add",
)

BASH_DENY_PATTERNS_READ_ONLY: tuple[str, ...] = (
    r"\b(kill|pkill|killall)\b",
    r"\brm\s+-[rRf]+",
    r"\bchmod\b",
    r"\bchown\b",
    r"\btee\b",
    r"(^|\s)>\s*[/~]",
    r"(^|\s)>>\s*[/~]",
)


def bash_deny_patterns(allow_mutations: bool) -> tuple[str, ...]:
    if allow_mutations:
        return BASH_DENY_PATTERNS_ALWAYS
    return BASH_DENY_PATTERNS_ALWAYS + BASH_DENY_PATTERNS_READ_ONLY


BASH_DENY_PATTERNS_INVESTIGATOR = bash_deny_patterns(allow_mutations=False)


INVESTIGATOR_SYSTEM_PROMPT = """You are an autonomous diagnostic investigator.

A live session asked you to triage a problem on its behalf. Work the problem,
form a verdict, and report back in the exact result block format below. The
session is paused waiting for your verdict — keep moving.

OPERATING RULES
- Read-only by default. Do not edit, write, or modify files. Do not send
  messages on behalf of the user. Do not restart sessions or daemons.
- Stay within the time budget you were given. If you are running out of
  time, stop investigating and write a partial result with what you know.
- Do not dispatch further investigations. You are a leaf node.
- Prefer evidence over speculation. Cite the exact file path and line, the
  exact log line, the exact ps/lsof output, etc.

TRIAGE PATTERN
1. Reproduce or observe the symptom (logs, ps, lsof, /usr/bin/sample on a
   pid, sqlite reads from bus.db, transcript reads, etc.).
2. Read the relevant source to confirm the mechanism — don't guess.
3. Decide whether the situation is safe (no data loss, no in-flight work),
   recoverable, or dangerous.
4. Recommend the smallest action that resolves the immediate issue, then
   note any durable fix the originator should track separately.

OUTPUT FORMAT (mandatory — the originator parses this)

Emit your final answer as the very last assistant message, wrapped exactly:

=== INVESTIGATION RESULT ===
SYMPTOM: <one or two sentences — what is observably wrong>
ROOT CAUSE: <best-supported cause; say "unknown" if you cannot establish it>
RECOMMENDED ACTION: <smallest concrete next step the originator should take>
SAFETY: <safe | recoverable | dangerous — plus a one-line reason>
DURABLE FIX: <what should be tracked separately to prevent recurrence; "none" if N/A>
=== END ===

If you cannot determine a field, write "unknown" on that line — don't omit
the line. The originator's parser is regex-based and tolerates anything in
between, but every labeled line must appear."""


def _load_state() -> dict[str, Any]:
    if not INFLIGHT_PATH.exists():
        return {"inflight": {}, "global_recent": []}
    try:
        data = json.loads(INFLIGHT_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {"inflight": {}, "global_recent": []}
    data.setdefault("inflight", {})
    data.setdefault("global_recent", [])
    return data


def _save_state(state: dict[str, Any]) -> None:
    INFLIGHT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = INFLIGHT_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(INFLIGHT_PATH)


def _prune_global_recent(state: dict[str, Any], now: float) -> None:
    cutoff = now - GLOBAL_RATE_WINDOW_SECONDS
    state["global_recent"] = [t for t in state["global_recent"] if t >= cutoff]


def _check_rate_limits(state: dict[str, Any], from_session: str, now: float) -> None:
    _prune_global_recent(state, now)

    if len(state["global_recent"]) >= GLOBAL_MAX_PER_HOUR:
        raise RateLimitExceeded(
            f"Global investigation rate limit hit "
            f"({GLOBAL_MAX_PER_HOUR}/hour). Try again later."
        )

    inflight_for_session = sum(
        1 for entry in state["inflight"].values()
        if entry.get("from_session") == from_session
        and entry.get("status") in ("requested", "running")
    )
    if inflight_for_session >= PER_SESSION_MAX_INFLIGHT:
        raise RateLimitExceeded(
            f"Session {from_session} already has {inflight_for_session} "
            f"investigations in flight (max {PER_SESSION_MAX_INFLIGHT})."
        )


def _detect_recursion(from_session: str) -> None:
    parent_task = os.environ.get(RECURSION_ENV_VAR)
    if parent_task:
        raise RecursiveDispatchError(
            f"Refusing to dispatch from inside an investigation "
            f"(parent task_id={parent_task})."
        )
    if from_session.startswith("ephemeral-"):
        raise RecursiveDispatchError(
            f"Refusing to dispatch from ephemeral session {from_session}."
        )


def build_investigation_request(
    originator_session: str,
    prompt: str,
    *,
    allow_mutations: bool = False,
    timeout_minutes: int = 15,
    task_id: str | None = None,
) -> dict[str, Any]:
    """Build the task.requested payload for an investigation."""
    if not task_id:
        task_id = f"investigation-{uuid4().hex[:12]}"
    title = prompt.strip().splitlines()[0][:80] if prompt.strip() else "Investigation"

    return {
        "task_id": task_id,
        "title": f"[investigation] {title}",
        "requested_by": originator_session,
        "instructions": prompt,
        "notify": False,
        "timeout_minutes": timeout_minutes,
        "investigation": True,
        "notify_session": originator_session,
        "allow_mutations": allow_mutations,
        "execution": {
            "mode": "agent",
            "prompt": prompt,
        },
    }


def dispatch(
    producer: Any,
    prompt: str,
    from_session: str,
    *,
    allow_mutations: bool = False,
    timeout_minutes: int = 15,
) -> str:
    """Validate, rate-limit, persist, and emit a task.requested event.

    Returns the task_id. Raises RateLimitExceeded or RecursiveDispatchError.
    """
    if not prompt or not prompt.strip():
        raise InvestigationError("prompt cannot be empty")
    if not from_session:
        raise InvestigationError("from_session cannot be empty")

    _detect_recursion(from_session)

    now = time.time()
    with _state_lock:
        state = _load_state()
        _check_rate_limits(state, from_session, now)

        payload = build_investigation_request(
            from_session, prompt,
            allow_mutations=allow_mutations,
            timeout_minutes=timeout_minutes,
        )
        task_id = payload["task_id"]

        state["inflight"][task_id] = {
            "task_id": task_id,
            "from_session": from_session,
            "prompt": prompt,
            "allow_mutations": allow_mutations,
            "timeout_minutes": timeout_minutes,
            "created_at": now,
            "status": "requested",
        }
        state["global_recent"].append(now)
        _save_state(state)

    produce_event(
        producer, "tasks", "task.requested", payload,
        key=from_session, source="investigation",
    )
    return task_id


def mark_status(task_id: str, status: str, **fields: Any) -> None:
    """Update an in-flight entry. status ∈ {requested,running,completed,failed,timeout,cancelled}."""
    with _state_lock:
        state = _load_state()
        entry = state["inflight"].get(task_id)
        if entry is None:
            return
        entry["status"] = status
        entry["updated_at"] = time.time()
        entry.update(fields)
        _save_state(state)


def remove(task_id: str) -> None:
    with _state_lock:
        state = _load_state()
        state["inflight"].pop(task_id, None)
        _save_state(state)


def get_status(task_id: str) -> dict[str, Any] | None:
    with _state_lock:
        state = _load_state()
        entry = state["inflight"].get(task_id)
        return dict(entry) if entry else None


def list_inflight(from_session: str | None = None) -> list[dict[str, Any]]:
    with _state_lock:
        state = _load_state()
        entries = list(state["inflight"].values())
    if from_session:
        entries = [e for e in entries if e.get("from_session") == from_session]
    entries.sort(key=lambda e: e.get("created_at", 0), reverse=True)
    return entries


def cancel(task_id: str) -> bool:
    """Mark cancelled in state. The manager is responsible for actually killing
    the running ephemeral session — call mark_status("cancelled") here so the
    list view reflects intent immediately.
    """
    with _state_lock:
        state = _load_state()
        entry = state["inflight"].get(task_id)
        if entry is None:
            return False
        if entry.get("status") in ("completed", "failed", "timeout", "cancelled"):
            return False
        entry["status"] = "cancelled"
        entry["updated_at"] = time.time()
        _save_state(state)
    return True


_RESULT_BLOCK_RE = re.compile(
    r"===\s*INVESTIGATION RESULT\s*===\s*(.*?)\s*===\s*END\s*===",
    re.DOTALL,
)
_FIELD_RES: dict[str, re.Pattern[str]] = {
    "symptom": re.compile(r"^\s*SYMPTOM\s*:\s*(.+?)\s*$", re.MULTILINE),
    "root_cause": re.compile(r"^\s*ROOT\s*CAUSE\s*:\s*(.+?)\s*$", re.MULTILINE),
    "recommended_action": re.compile(r"^\s*RECOMMENDED\s*ACTION\s*:\s*(.+?)\s*$", re.MULTILINE),
    "safety": re.compile(r"^\s*SAFETY\s*:\s*(.+?)\s*$", re.MULTILINE),
    "durable_fix": re.compile(r"^\s*DURABLE\s*FIX\s*:\s*(.+?)\s*$", re.MULTILINE),
}


def parse_result_block(text: str) -> dict[str, Any]:
    """Extract structured fields from an investigator's last assistant text.

    Returns a dict with: symptom, root_cause, recommended_action, safety,
    durable_fix, raw, parse_status. parse_status:
        "complete" — block found and every required field present
        "partial"  — block found but some fields missing
        "missing"  — no === INVESTIGATION RESULT === block at all
    """
    out: dict[str, Any] = {
        "symptom": None,
        "root_cause": None,
        "recommended_action": None,
        "safety": None,
        "durable_fix": None,
        "raw": text or "",
        "parse_status": "missing",
    }

    if not text:
        return out

    block_match = _RESULT_BLOCK_RE.search(text)
    if not block_match:
        return out

    block = block_match.group(1)
    found = 0
    for field, pat in _FIELD_RES.items():
        m = pat.search(block)
        if m:
            out[field] = m.group(1).strip()
            found += 1

    out["parse_status"] = "complete" if found == len(_FIELD_RES) else "partial"
    return out


def format_result_for_injection(task_id: str, status: str, parsed: dict[str, Any]) -> str:
    """Wrap a parsed result for injection into the originator's session."""
    if parsed.get("parse_status") == "missing":
        body = parsed.get("raw") or "(no investigator output captured)"
    else:
        lines = [
            f"SYMPTOM: {parsed.get('symptom') or 'unknown'}",
            f"ROOT CAUSE: {parsed.get('root_cause') or 'unknown'}",
            f"RECOMMENDED ACTION: {parsed.get('recommended_action') or 'unknown'}",
            f"SAFETY: {parsed.get('safety') or 'unknown'}",
            f"DURABLE FIX: {parsed.get('durable_fix') or 'none'}",
        ]
        body = "\n".join(lines)

    parse_status = parsed.get("parse_status", "missing")
    return (
        f'<investigation-result task_id="{task_id}" status="{status}" '
        f'parse_status="{parse_status}">\n'
        f"{body}\n"
        f"</investigation-result>"
    )
