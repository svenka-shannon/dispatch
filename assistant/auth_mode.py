"""
Auth mode management for SDK sessions.

The dispatch daemon authenticates the embedded `claude` CLI in one of two ways:

  - **oauth** (default): the CLI uses the OAuth token from the macOS keychain
    (set up via `claude login`). Usage counts against the Max plan quota.
  - **api_key**: ANTHROPIC_API_KEY is set in the daemon's env, so the CLI
    falls back to API credit billing.

When an SDK session crashes with an OAuth quota-exhausted error, this module
promotes ANTHROPIC_API_KEY_FALLBACK into ANTHROPIC_API_KEY (so subsequent
session restarts inherit the API key) and writes state/auth_mode.json so the
choice survives daemon restarts.

To flip back to OAuth after the quota window resets, run:

    claude-assistant auth reset

That deletes auth_mode.json — the next daemon restart will run on OAuth again.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from typing import Any

from assistant.common import STATE_DIR

log = logging.getLogger(__name__)

AUTH_MODE_FILE = STATE_DIR / "auth_mode.json"

# Patterns that indicate the OAuth quota has been exhausted by the underlying
# `claude` CLI. These come back as the error string when the CLI exits or as
# parts of API error responses surfaced through claude_agent_sdk.
#
# Examples we want to match:
#   "Claude usage limit reached. Your limit will reset at 3pm (PT)."
#   "5-hour usage limit reached"
#   "Weekly usage limit reached"
#   "rate_limit_error" + OAuth context
_QUOTA_PATTERNS = (
    re.compile(r"usage\s+limit\s+reached", re.IGNORECASE),
    re.compile(r"5-?\s*hour\s+(usage\s+)?limit", re.IGNORECASE),
    re.compile(r"7-?\s*day\s+(usage\s+)?limit", re.IGNORECASE),
    re.compile(r"weekly\s+(usage\s+)?limit", re.IGNORECASE),
    re.compile(r"oauth.*rate.?limit|rate.?limit.*oauth", re.IGNORECASE | re.DOTALL),
    re.compile(r"plan\s+limit\s+(reached|exceeded)", re.IGNORECASE),
)


def is_oauth_quota_error(error_text: str) -> bool:
    """Return True if `error_text` looks like an OAuth quota exhaustion.

    Conservative — matches the strings claude CLI produces when the Max plan
    quota is hit. Won't match generic API rate limits (those go through a
    different code path; see _detect_block_limit in sdk_session).
    """
    if not error_text:
        return False
    return any(p.search(error_text) for p in _QUOTA_PATTERNS)


def current_mode() -> dict[str, Any]:
    """Read the persisted auth mode. Defaults to {'mode': 'oauth'}."""
    if not AUTH_MODE_FILE.exists():
        return {"mode": "oauth"}
    try:
        return json.loads(AUTH_MODE_FILE.read_text())
    except (json.JSONDecodeError, OSError) as e:
        log.warning(f"AUTH_MODE_READ_FAILED | {e} — defaulting to oauth")
        return {"mode": "oauth"}


def is_api_key_mode() -> bool:
    return current_mode().get("mode") == "api_key"


def write_mode(mode: str, *, reason: str = "", error_text: str = "",
               session_name: str = "") -> None:
    """Persist the active auth mode to state/auth_mode.json."""
    AUTH_MODE_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "mode": mode,
        "since": datetime.now().isoformat(),
    }
    if reason:
        payload["reason"] = reason
    if session_name:
        payload["triggered_by_session"] = session_name
    if error_text:
        # Truncate so the file stays readable in `cat`.
        payload["error_text"] = error_text[:500]
    AUTH_MODE_FILE.write_text(json.dumps(payload, indent=2))
    log.info(f"AUTH_MODE_WRITTEN | mode={mode} reason={reason}")


def clear() -> bool:
    """Remove auth_mode.json. Returns True if a file was removed."""
    if AUTH_MODE_FILE.exists():
        AUTH_MODE_FILE.unlink()
        log.info("AUTH_MODE_CLEARED")
        return True
    return False


def promote_fallback_key() -> bool:
    """Copy ANTHROPIC_API_KEY_FALLBACK into ANTHROPIC_API_KEY in os.environ.

    Returns True if promotion happened. False means no fallback is configured —
    the daemon will continue to fail under OAuth until either the quota resets
    or the user adds ANTHROPIC_API_KEY_FALLBACK to ~/.secrets.env.
    """
    fallback = os.environ.get("ANTHROPIC_API_KEY_FALLBACK")
    if not fallback:
        log.warning("AUTH_MODE_PROMOTE_FAILED | ANTHROPIC_API_KEY_FALLBACK is not set in env")
        return False
    os.environ["ANTHROPIC_API_KEY"] = fallback
    log.info("AUTH_MODE_PROMOTED | ANTHROPIC_API_KEY now sourced from fallback")
    return True


def apply_at_startup() -> str:
    """Honor state/auth_mode.json at daemon startup.

    If mode=api_key, promote the fallback key into env so SDK subprocesses
    spawned during this run inherit it. Returns the active mode string (used
    by callers for startup logging).
    """
    info = current_mode()
    mode = info.get("mode", "oauth")
    if mode == "api_key":
        if promote_fallback_key():
            log.info(
                "AUTH_MODE_STARTUP | resumed api_key mode "
                f"(since={info.get('since')}, reason={info.get('reason')})"
            )
        else:
            log.error(
                "AUTH_MODE_STARTUP | wanted api_key but ANTHROPIC_API_KEY_FALLBACK "
                "is not set; falling back to oauth"
            )
            return "oauth"
    return mode
