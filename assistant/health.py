"""
Two-tier session healing: fast regex + deep Haiku analysis.

Tier 1 (every 60s): Regex scan of recent transcript entries for known fatal errors.
Tier 2 (every 5 min): Haiku LLM classification of recent assistant messages.

Fatal errors are unrecoverable — the bad data is baked into conversation context
and will cause the same API error on every retry. Only a session restart fixes them.
"""
from __future__ import annotations

import json
import logging
import os
import plistlib
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)
lifecycle_log = logging.getLogger("lifecycle")


class HaikuCallFailed(Exception):
    """Raised when a Haiku health check call fails (API error)."""
    pass

# ──────────────────────────────────────────────────────────────
# Tier 1: Regex-based fatal error detection
# ──────────────────────────────────────────────────────────────

# Pattern: (regex, label) — matched against assistant message text
FATAL_PATTERNS: list[tuple[str, str]] = [
    # Context-size patterns MUST come before generic invalid_request_400 catch-all
    # so check_fatal_regex (first-match) returns the specific label, enabling clean restart.
    (r"prompt is too long", "prompt_too_long"),
    (r"context_length_exceeded", "context_too_long"),
    (r"content size exceeds", "content_too_large"),
    (r"JSON message exceeded maximum buffer size", "buffer_overflow"),
    (r"API Error: 400.*invalid_request_error", "invalid_request_400"),
    (r"image dimensions exceed max allowed size", "image_too_large"),
    (r"Could not process image", "image_processing_failed"),
    (r"\"authentication_\w+\"", "auth_error"),
    (r"\"billing_error\"", "billing_error"),
]

# Compiled for performance (called every 60s across all sessions)
_COMPILED_PATTERNS = [(re.compile(p, re.IGNORECASE), label) for p, label in FATAL_PATTERNS]


def get_transcript_entries_since(
    session_cwd: str,
    session_id: Optional[str],
    since: datetime,
) -> list[dict[str, Any]]:
    """Read recent assistant message entries from the session's transcript JSONL.

    Reads from end of file for efficiency (sessions can have thousands of lines).
    Only returns type=assistant entries newer than `since`.
    """
    transcript_path = _find_transcript(session_cwd, session_id)
    if not transcript_path or not transcript_path.exists():
        return []

    try:
        # Read last 128KB — enough for ~5-10 min of activity
        with open(transcript_path, 'rb') as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            chunk = min(size, 131072)
            f.seek(size - chunk)
            tail = f.read().decode('utf-8', errors='replace')
    except OSError as e:
        log.warning(f"Failed to read transcript {transcript_path}: {e}")
        return []

    # Ensure since is timezone-aware for comparison
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)

    entries = []
    for line in tail.splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        if obj.get('type') != 'assistant':
            continue

        ts_str = obj.get('timestamp', '')
        if not ts_str:
            continue

        try:
            ts = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
            if ts > since:
                entries.append(obj)
        except ValueError:
            continue

    return entries


def check_fatal_regex(entries: list[dict[str, Any]]) -> Optional[str]:
    """Check assistant message entries for known fatal error patterns.

    Returns the pattern label if a fatal error is found, None otherwise.
    """
    for entry in entries:
        content = entry.get('message', {}).get('content', [])
        if not isinstance(content, list):
            continue

        for block in content:
            if not isinstance(block, dict):
                continue
            text = block.get('text', '')
            if not text:
                continue

            for pattern, label in _COMPILED_PATTERNS:
                if pattern.search(text):
                    return label

    return None


def extract_assistant_text(entries: list[dict[str, Any]], max_chars: int = 4000) -> str:
    """Extract text and tool use info from assistant message entries for Haiku analysis.

    Groups content by message entry with timestamps, includes both TextBlock content
    and tool_use block names so the classifier can see chronological progression
    and distinguish early productive work from later stuck behavior.
    """
    messages = []
    total = 0

    for entry in entries:
        ts_str = entry.get('timestamp', '')
        # Extract just HH:MM:SS for brevity
        time_label = ""
        if ts_str:
            try:
                from datetime import datetime
                ts = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
                time_label = ts.strftime("%H:%M:%S")
            except ValueError:
                pass

        content = entry.get('message', {}).get('content', [])
        if not isinstance(content, list):
            continue

        entry_parts = []
        for block in content:
            if not isinstance(block, dict):
                continue

            # Include tool_use blocks as "[tool: ToolName]" markers with brief input
            if block.get('type') == 'tool_use':
                tool_name = block.get('name', 'unknown')
                # Include a brief snippet of tool input for context
                tool_input = block.get('input', {})
                snippet = ""
                if isinstance(tool_input, dict):
                    for key in ('command', 'file_path', 'pattern', 'prompt', 'description'):
                        val = tool_input.get(key, '')
                        if val:
                            snippet = f" ({str(val)[:60]})"
                            break
                entry_parts.append(f"[tool: {tool_name}{snippet}]")
                continue

            text = block.get('text', '')
            if not text:
                continue
            entry_parts.append(text[:200])

        if not entry_parts:
            continue

        header = f"[{time_label}]" if time_label else "[msg]"
        msg_text = f"{header} {' | '.join(entry_parts)}"

        remaining = max_chars - total
        if remaining <= 0:
            break
        chunk = msg_text[:remaining]
        messages.append(chunk)
        total += len(chunk)

    return "\n".join(messages)


# ──────────────────────────────────────────────────────────────
# Tier 2: Haiku-based deep analysis
# ──────────────────────────────────────────────────────────────

HAIKU_PROMPT = """You are a session health monitor for an AI assistant. Analyze these recent assistant messages and determine if the session needs intervention.

The messages below are timestamped and contain text blocks AND [tool: ToolName] markers showing tool calls. Text between tool calls is internal narration (e.g. "Now update the style:"), NOT messages sent to the user. This is normal — the assistant narrates its plan between tool calls. Focus on the LATEST messages to determine current state.

FATAL means the session is broken and needs a restart:
- API errors baked into conversation context (image dimensions, context length, invalid content) that will repeat on every retry
- Authentication or billing errors
- Repeated identical or near-identical text messages (3+ times) WITHOUT meaningful tool calls between them — this indicates the session is stuck in a loop
- Session outputting the same "waiting" / "no response needed" message repeatedly

HEALTHY means the session is operating normally:
- Session is actively calling tools (Edit, Read, Bash, Grep, etc.) with different text narration between them — this means it's working, even if text blocks seem incomplete
- Rate limits (429) or server overload (529) — these are transient
- Tool execution failures where Claude tries alternatives
- Normal error handling and recovery
- A single error followed by successful work
- Internal narration like "Now let me update X:" followed by [tool: Edit] — this is normal workflow, NOT incomplete output
- Messages that end with ":" are typically followed by tool calls — this is normal

IMPORTANT: Err on the side of HEALTHY. Only mark FATAL when you are confident the session is genuinely broken. A session interleaving diverse tool calls with narration text is working fine — even if the text seems terse or incomplete.

Recent assistant messages (last 5 minutes):
{messages}

Respond with ONLY one of:
FATAL: <one-line reason>
HEALTHY"""


async def check_deep_haiku(entries: list[dict[str, Any]], session_name: str) -> tuple[Optional[str], str]:
    """Send recent assistant messages to Haiku for fatal error classification.

    Uses claude_agent_sdk.query() with model=haiku for a one-shot classification.
    Returns (diagnosis, reasoning) tuple:
      - diagnosis: the diagnosis string if fatal, None if healthy
      - reasoning: the raw Haiku response text (always returned for logging/display)
    """
    text = extract_assistant_text(entries)
    if not text or len(text.strip()) < 20:
        return None, ""

    try:
        from claude_agent_sdk import (
            query as sdk_query,
            ClaudeAgentOptions,
            AssistantMessage,
            TextBlock,
        )

        from pathlib import Path
        options = ClaudeAgentOptions(
            cli_path=Path.home() / ".local" / "bin" / "claude",  # Use system CLI for OAuth compat
            model="haiku",
            max_turns=1,
            permission_mode="bypassPermissions",
        )

        prompt = HAIKU_PROMPT.format(messages=text)
        result_text = ""
        async for message in sdk_query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        result_text += block.text

        result = result_text.strip()
        log.info(f"DEEP_HEAL | {session_name} | Haiku response: {result}")

        if result.startswith("FATAL:"):
            return result[6:].strip(), result
        return None, result

    except Exception as e:
        log.warning(f"DEEP_HEAL | {session_name} | Haiku call failed: {e}")
        raise HaikuCallFailed(str(e)) from e


# ──────────────────────────────────────────────────────────────
# Tier 3: Haiku-based stuck session investigation
# ──────────────────────────────────────────────────────────────

STUCK_INVESTIGATION_PROMPT = """You are investigating whether an AI assistant session is genuinely stuck or just working on a long task.

Context: A message was injected {stuck_minutes:.0f} minutes ago with no ResultMessage returned yet. The session process is alive.

Analyze the recent transcript entries below. These are the most recent tool calls, thinking, and messages from the session.

STUCK means the session is genuinely unresponsive or broken:
- No tool calls or thinking activity in the last 5+ minutes
- Session is in an error loop (same error repeating)
- Process is alive but producing no output at all
- Last activity was a tool call that seems to have hung (no result)

WORKING means the session is actively processing and should NOT be interrupted:
- Recent tool calls (Read, Grep, Bash, Agent, Write, Edit) — even if slow
- Thinking blocks indicate active reasoning
- Subagent operations in progress (Agent tool calls without results yet are normal — subagents can run 15+ min)
- Code exploration or research happening (multiple Read/Grep calls)
- The session is making forward progress, just slowly

Recent transcript entries (most recent last):
{entries}

Respond with ONLY one of:
STUCK: <one-line reason>
WORKING: <what it appears to be doing>"""


async def check_stuck_haiku(session_cwd: str, session_id: str | None,
                            session_name: str, stuck_minutes: float) -> tuple[bool, str]:
    """Investigate if a session is genuinely stuck using Haiku.

    Reads recent transcript JSONL entries and asks Haiku to classify
    whether the session is stuck or just working on something long.

    Returns (is_stuck, reasoning) tuple:
      - is_stuck: True if stuck (should restart), False if working (leave alone)
      - reasoning: the raw Haiku response text
    """
    try:
        transcript_path = _find_transcript(session_cwd, session_id)
        if not transcript_path or not transcript_path.exists():
            log.warning(f"STUCK_CHECK | {session_name} | No transcript found, assuming stuck")
            return True, "No transcript found, assuming stuck"

        # Read last 64KB of transcript for recent activity
        entries_text = ""
        with open(transcript_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            chunk = min(size, 65536)  # 64KB
            f.seek(size - chunk)
            raw = f.read().decode("utf-8", errors="replace")
            # Get last 30 valid JSON lines
            lines = [line for line in raw.strip().split("\n") if line.strip()]
            recent = lines[-30:]
            entries_text = "\n".join(recent)

        if not entries_text or len(entries_text.strip()) < 20:
            log.warning(f"STUCK_CHECK | {session_name} | Empty transcript, assuming stuck")
            return True, "Empty transcript, assuming stuck"

        from claude_agent_sdk import (
            query as sdk_query,
            ClaudeAgentOptions,
            AssistantMessage,
            TextBlock,
        )

        options = ClaudeAgentOptions(
            cli_path=Path.home() / ".local" / "bin" / "claude",
            model="haiku",
            max_turns=1,
            permission_mode="bypassPermissions",
        )

        prompt = STUCK_INVESTIGATION_PROMPT.format(
            stuck_minutes=stuck_minutes,
            entries=entries_text,
        )
        result_text = ""
        async for message in sdk_query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        result_text += block.text

        result = result_text.strip()
        log.info(f"STUCK_CHECK | {session_name} | Haiku verdict: {result}")

        if result.startswith("STUCK:"):
            return True, result
        return False, result

    except Exception as e:
        log.warning(f"STUCK_CHECK | {session_name} | Haiku investigation failed: {e}")
        raise HaikuCallFailed(str(e)) from e


# ──────────────────────────────────────────────────────────────
# Transcript file location
# ──────────────────────────────────────────────────────────────

def _find_transcript(session_cwd: str, session_id: Optional[str]) -> Optional[Path]:
    """Locate the active transcript JSONL for a session.

    Uses the SDK's project directory structure:
    ~/.claude/projects/{sanitized_cwd}/{session_id}.jsonl
    """
    if not session_cwd:
        return None

    # SDK sanitizes cwd: /Users/sven/transcripts/foo -> -Users-sven-transcripts-foo
    sanitized = session_cwd.replace("/", "-")
    if not sanitized.startswith("-"):
        sanitized = "-" + sanitized

    projects_dir = Path.home() / ".claude" / "projects" / sanitized

    if not projects_dir.exists():
        return None

    # If we have a session_id, use it directly
    if session_id:
        direct = projects_dir / f"{session_id}.jsonl"
        if direct.exists():
            return direct

    # Fallback: check sessions-index.json for most recent
    index_path = projects_dir / "sessions-index.json"
    if index_path.exists():
        try:
            with open(index_path) as f:
                index = json.load(f)
            entries = index.get("entries", [])
            if entries:
                entries.sort(key=lambda e: e.get("modified", ""), reverse=True)
                transcript_path = Path(entries[0]["fullPath"])
                if transcript_path.exists():
                    return transcript_path
        except (json.JSONDecodeError, KeyError):
            pass

    # Last resort: most recent .jsonl by mtime
    jsonl_files = list(projects_dir.glob("*.jsonl"))
    if jsonl_files:
        jsonl_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
        return jsonl_files[0]

    return None


# ──────────────────────────────────────────────────────────────
# chrome-control health monitoring
# ──────────────────────────────────────────────────────────────
#
# The chrome-control extension's MV3 service worker gets evicted/wedged
# periodically. When that happens `chrome ping` hangs (it has a 30s internal
# socket timeout) or exits non-zero. Recovery: `chrome reset` (SIGKILLs stuck
# native_host.py procs and, if the SW is gone, runs `chrome wake` to revive it).
#
# We do NOT auto-launch Chrome: if the user closed Chrome they did it
# deliberately. So we gate the whole check on Chrome already running.

# Path to the chrome CLI. Resolve via ~/.claude (the canonical symlink target
# in CLAUDE.md). No pre-existing constant for this in assistant/ — checked.
CHROME_CLI = Path.home() / ".claude" / "skills" / "chrome-control" / "scripts" / "chrome"

# Timeouts (seconds): ping gets a short one (its internal 30s is too slow for
# our ~1-2 min detection target); reset gets a generous one (it sleeps + waits
# for the SW to come back).
CHROME_PING_TIMEOUT = 12
CHROME_RESET_TIMEOUT = 30


def _chrome_app_running() -> bool:
    """True if the Google Chrome application is running. We never auto-launch it."""
    try:
        result = subprocess.run(
            ["pgrep", "-x", "Google Chrome"],
            capture_output=True, timeout=5,
        )
        return result.returncode == 0
    except Exception as e:
        log.debug(f"CHROME_CHECK | pgrep failed: {e}")
        # If we can't tell, be conservative and skip — don't risk relaunching Chrome.
        return False


def check_chrome_control(
    runner: Any = subprocess.run,
    app_running_fn: Any = None,
) -> dict[str, Any]:
    """Probe chrome-control health and recover it if wedged.

    Steps:
      1. If the Google Chrome app isn't running → skip (status=chrome_not_running).
      2. `chrome ping` with a short timeout. Exit 0 + "Connected ..." → healthy.
      3. Otherwise (timeout / non-zero) → `chrome reset` (which self-heals:
         kills stuck native_host, runs `chrome wake` if the SW is gone).

    `runner` / `app_running_fn` are injectable for testing — production callers
    should not pass them.

    Returns a dict:
        status: "ok" | "wedged" | "chrome_not_running" | "cli_missing"
        action_taken: "reset" | "none"
        detail: human-readable summary
        ping_rc / ping_output / ping_timed_out  (when a ping was attempted)
        reset_rc / reset_output / reset_timed_out  (when a reset was attempted)
    """
    if app_running_fn is None:
        app_running_fn = _chrome_app_running

    result: dict[str, Any] = {
        "status": "ok",
        "action_taken": "none",
        "detail": "",
    }

    if not app_running_fn():
        result["status"] = "chrome_not_running"
        result["detail"] = "Google Chrome app not running — skipping chrome-control check"
        return result

    if not CHROME_CLI.exists():
        result["status"] = "cli_missing"
        result["detail"] = f"chrome CLI not found at {CHROME_CLI}"
        return result

    # ── Step 2: ping ──────────────────────────────────────────
    ping_ok = False
    try:
        ping = runner(
            [str(CHROME_CLI), "ping"],
            capture_output=True, text=True, timeout=CHROME_PING_TIMEOUT,
        )
        result["ping_rc"] = ping.returncode
        out = ((ping.stdout or "") + (ping.stderr or "")).strip()
        result["ping_output"] = out
        result["ping_timed_out"] = False
        ping_ok = ping.returncode == 0 and "Connected to Chrome Control extension" in out
    except subprocess.TimeoutExpired:
        result["ping_timed_out"] = True
        result["ping_output"] = f"timed out after {CHROME_PING_TIMEOUT}s"
    except Exception as e:
        result["ping_timed_out"] = False
        result["ping_output"] = f"ping errored: {e}"

    if ping_ok:
        result["detail"] = "chrome ping ok"
        return result

    # ── Step 3: wedged → reset ────────────────────────────────
    result["status"] = "wedged"
    result["action_taken"] = "reset"
    try:
        reset = runner(
            [str(CHROME_CLI), "reset"],
            capture_output=True, text=True, timeout=CHROME_RESET_TIMEOUT,
        )
        result["reset_rc"] = reset.returncode
        result["reset_output"] = ((reset.stdout or "") + (reset.stderr or "")).strip()
        result["reset_timed_out"] = False
        result["detail"] = (
            f"chrome ping unhealthy; ran `chrome reset` (rc={reset.returncode})"
        )
    except subprocess.TimeoutExpired:
        result["reset_timed_out"] = True
        result["reset_output"] = f"timed out after {CHROME_RESET_TIMEOUT}s"
        result["detail"] = "chrome ping unhealthy; `chrome reset` timed out"
    except Exception as e:
        result["reset_timed_out"] = False
        result["reset_output"] = f"reset errored: {e}"
        result["detail"] = f"chrome ping unhealthy; `chrome reset` errored: {e}"

    return result


# ──────────────────────────────────────────────────────────────
# Disk space monitoring
# ──────────────────────────────────────────────────────────────

# Track last alert time to avoid spamming (alert at most once per 30 min)
_last_disk_alert_time: float = 0.0


def _get_apfs_container_space() -> tuple[int, int] | None:
    """Get APFS container total and free bytes via diskutil.

    On macOS with APFS, the container free space includes purgeable space
    (caches, snapshots, etc. that macOS can reclaim on demand). This matches
    what macOS Settings shows as "Available" and avoids false disk warnings.

    Returns (total_bytes, free_bytes) or None if not available.
    """
    if sys.platform != "darwin":
        return None
    try:
        result = subprocess.run(
            ["diskutil", "info", "-plist", "/"],
            capture_output=True, timeout=5,
        )
        if result.returncode != 0:
            return None
        plist = plistlib.loads(result.stdout)
        container_size = plist.get("APFSContainerSize")
        container_free = plist.get("APFSContainerFree")
        if container_size and container_free:
            return (container_size, container_free)
    except Exception as e:
        log.debug(f"APFS container query failed: {e}")
    return None


def check_disk_space(warn_pct: float = 90.0, critical_pct: float = 95.0) -> dict[str, Any]:
    """Check disk space on the root volume.

    On macOS with APFS, uses container-level free space which includes
    purgeable space (what macOS Settings shows as "Available"). Falls back
    to shutil.disk_usage on non-APFS or non-macOS systems.

    Returns dict with:
        total_gb, used_gb, free_gb, used_pct,
        warning (bool), critical (bool), message (str or None)
    """
    # Try APFS container space first (includes purgeable)
    apfs = _get_apfs_container_space()
    if apfs:
        total_bytes, free_bytes = apfs
        used_bytes = total_bytes - free_bytes
    else:
        usage = shutil.disk_usage("/")
        total_bytes = usage.total
        used_bytes = usage.used
        free_bytes = usage.free

    total_gb = total_bytes / (1024 ** 3)
    used_gb = used_bytes / (1024 ** 3)
    free_gb = free_bytes / (1024 ** 3)
    used_pct = (used_bytes / total_bytes) * 100

    result: dict[str, Any] = {
        "total_gb": round(total_gb, 1),
        "used_gb": round(used_gb, 1),
        "free_gb": round(free_gb, 1),
        "used_pct": round(used_pct, 1),
        "warning": used_pct >= warn_pct,
        "critical": used_pct >= critical_pct,
        "message": None,
    }

    if used_pct >= critical_pct:
        result["message"] = (
            f"DISK CRITICAL: {used_pct:.1f}% used "
            f"({free_gb:.1f}GB free of {total_gb:.0f}GB)"
        )
    elif used_pct >= warn_pct:
        result["message"] = (
            f"DISK WARNING: {used_pct:.1f}% used "
            f"({free_gb:.1f}GB free of {total_gb:.0f}GB)"
        )

    return result


def should_send_disk_alert() -> bool:
    """Rate-limit disk alerts to at most once per 30 minutes."""
    global _last_disk_alert_time
    import time
    now = time.time()
    if now - _last_disk_alert_time >= 1800:  # 30 minutes
        _last_disk_alert_time = now
        return True
    return False


# ──────────────────────────────────────────────────────────────
# Quota monitoring — alert admin when approaching usage limits
# ──────────────────────────────────────────────────────────────

# Thresholds to alert at (ascending order)
QUOTA_THRESHOLDS = [80, 90, 95]

# Track which thresholds have been alerted per quota type per reset cycle.
# Key: (quota_type, resets_at_iso) → set of thresholds already alerted
# Persisted to disk so alerts survive process restarts.
_quota_alerts_sent: dict[tuple[str, str], set[int]] = {}
_QUOTA_ALERTS_FILE = Path("~/dispatch/state/quota_alerts_sent.json").expanduser()


def _load_quota_alerts() -> None:
    """Load persisted quota alert state from disk."""
    global _quota_alerts_sent
    try:
        if _QUOTA_ALERTS_FILE.exists():
            raw = json.loads(_QUOTA_ALERTS_FILE.read_text())
            # JSON keys are strings — convert back to tuples and sets
            _quota_alerts_sent = {
                tuple(k.split("|", 1)): set(v)
                for k, v in raw.items()
            }
    except Exception as e:
        log.warning(f"QUOTA_ALERTS | Failed to load state: {e}")
        _quota_alerts_sent = {}


def _save_quota_alerts() -> None:
    """Persist quota alert state to disk."""
    try:
        # Convert tuple keys to strings for JSON serialization
        raw = {
            f"{k[0]}|{k[1]}": sorted(v)
            for k, v in _quota_alerts_sent.items()
        }
        _QUOTA_ALERTS_FILE.write_text(json.dumps(raw))
    except Exception as e:
        log.warning(f"QUOTA_ALERTS | Failed to save state: {e}")


def _get_oauth_token() -> str | None:
    """Get OAuth access token from macOS keychain."""
    try:
        raw = subprocess.check_output(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        import json as _json
        data = _json.loads(raw)
        return data.get("claudeAiOauth", {}).get("accessToken")
    except Exception:
        return None


_quota_cache: dict | None = None
_quota_cache_ts: float = 0.0
_last_attempt_ts: float = 0.0        # when we last tried the API (success or failure)
_QUOTA_BACKOFF_BASE: float = 900.0   # 15 min base interval
_QUOTA_BACKOFF_MAX: float = 7200.0   # 2 hour cap
_quota_backoff: float = 900.0        # current dynamic interval (adapts on failure)
_consecutive_failures: int = 0

import threading
_quota_lock = threading.Lock()


def _fetch_quota_from_api() -> dict | None:
    """Internal: actually hit the Anthropic OAuth API. Never call directly — use fetch_quota()."""
    token = _get_oauth_token()
    if not token:
        log.warning("QUOTA_CHECK | No OAuth token available")
        return None

    import ssl
    import urllib.request
    ctx = ssl.create_default_context()
    req = urllib.request.Request("https://api.anthropic.com/api/oauth/usage")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("anthropic-beta", "oauth-2025-04-20")
    req.add_header("Accept", "application/json")

    try:
        resp = urllib.request.urlopen(req, context=ctx, timeout=15)
        result = json.loads(resp.read().decode())
        return result
    except Exception as e:
        log.warning(f"QUOTA_CHECK | OAuth fetch failed: {e}")
        return None


def fetch_quota() -> tuple[dict | None, str | None, bool]:
    """Central quota accessor. Returns (usage_dict, updated_at_iso, is_fresh).

    Enforces adaptive backoff between API calls (15 min base, doubles on failure,
    caps at 2 hours, resets on success). All callers go through this single
    function — there is no other path to the Anthropic quota API.

    is_fresh=True only when the API was actually called and returned new data.
    """
    import time
    from datetime import datetime

    global _quota_cache, _quota_cache_ts, _quota_backoff, _consecutive_failures, _last_attempt_ts

    with _quota_lock:
        now = time.monotonic()

        # Respect backoff — don't hit API if we attempted recently (success OR failure)
        if (now - _last_attempt_ts) < _quota_backoff:
            if _quota_cache is not None:
                age = now - _quota_cache_ts
                updated_at = datetime.fromtimestamp(
                    time.time() - age
                ).isoformat()
                return _quota_cache, updated_at, False
            return None, None, False

        # Backoff window expired — attempt API call
        _last_attempt_ts = now
        result = _fetch_quota_from_api()
        if result is not None:
            _quota_cache = result
            _quota_cache_ts = now
            _quota_backoff = _QUOTA_BACKOFF_BASE  # reset on success
            _consecutive_failures = 0
            updated_at = datetime.now().isoformat()
            log.info("QUOTA_CHECK | Fresh fetch successful, next in %.0fs", _quota_backoff)
            return result, updated_at, True

        # API failed — increase backoff
        _consecutive_failures += 1
        old_backoff = _quota_backoff
        _quota_backoff = min(_quota_backoff * 2, _QUOTA_BACKOFF_MAX)
        log.debug("QUOTA_BACKOFF | increased to %.0fs after %d consecutive failures (was %.0fs)",
                  _quota_backoff, _consecutive_failures, old_backoff)

        # Return stale cache if we have one
        if _quota_cache is not None:
            age = now - _quota_cache_ts
            updated_at = datetime.fromtimestamp(
                time.time() - age
            ).isoformat()
            log.warning("QUOTA_CHECK | API failed, returning stale cache from %s", updated_at)
            return _quota_cache, updated_at, False

        return None, None, False


def get_quota_cached() -> tuple[dict | None, str | None]:
    """Read-only access to the quota cache. Never triggers an API call.

    Use this from IPC handlers and anywhere that just needs the latest known data.
    Returns (usage_dict, updated_at_iso).
    """
    import time
    from datetime import datetime

    with _quota_lock:
        if _quota_cache is not None:
            age = time.monotonic() - _quota_cache_ts
            updated_at = datetime.fromtimestamp(
                time.time() - age
            ).isoformat()
            return _quota_cache, updated_at
        return None, None


def get_quota_backoff_state() -> dict:
    """Return current backoff state for bus event payload."""
    with _quota_lock:
        return {
            "backoff_seconds": int(_quota_backoff),
            "consecutive_failures": _consecutive_failures,
        }


def seed_quota_cache(data: dict | None, updated_at: str | None):
    """Seed the in-memory cache from disk on startup.

    Does NOT reset backoff. Sets cache_ts so it's immediately stale —
    the first health check will trigger a fresh fetch.
    """
    import time
    if data is None:
        return
    global _quota_cache, _quota_cache_ts
    with _quota_lock:
        _quota_cache = data
        # Set to immediately stale so next fetch_quota() call hits the API
        _quota_cache_ts = time.monotonic() - _quota_backoff
        log.info("QUOTA_CHECK | Seeded cache from disk (immediately stale, will refresh on next check)")


# Back-compat alias — existing callers that import fetch_quota_oauth will get the new gated version
def fetch_quota_oauth() -> dict | None:
    """DEPRECATED: Use fetch_quota() instead. This wrapper exists for back-compat."""
    usage, _ts, _fresh = fetch_quota()
    return usage


def check_quota_thresholds(usage: dict) -> list[dict[str, Any]]:
    """Check all quota blocks against thresholds.

    Returns list of alerts to send, each with:
        quota_type, utilization, threshold, resets_at
    Only returns alerts that haven't been sent yet for this reset cycle.
    """
    global _quota_alerts_sent

    # Load persisted state on first call or after restart
    if not _quota_alerts_sent:
        _load_quota_alerts()

    alerts: list[dict[str, Any]] = []

    # Define which blocks to check
    blocks = [
        ("5-hour", usage.get("five_hour", {})),
        ("7-day all", usage.get("seven_day", {})),
        ("7-day sonnet", usage.get("seven_day_sonnet", {})),
        ("7-day opus", usage.get("seven_day_opus", {})),
    ]

    # Include extra usage if enabled
    eu = usage.get("extra_usage", {})
    if eu and eu.get("is_enabled") and eu.get("utilization") is not None:
        # Use current month as stable reset key (extra usage resets monthly)
        month_key = datetime.now(timezone.utc).strftime("%Y-%m")
        blocks.append(("extra usage", {
            "utilization": eu["utilization"],
            "resets_at": f"monthly:{month_key}",
        }))

    for quota_type, block in blocks:
        if not block:
            continue
        util = block.get("utilization")
        if util is None:
            continue
        resets_at = block.get("resets_at", "unknown")

        # Normalize resets_at to minute precision for stable cache keys.
        # The API returns varying microseconds (e.g., .358340 vs .014720)
        # which would defeat dedup if used as-is.
        normalized_resets = resets_at
        if resets_at != "unknown":
            try:
                _rt = datetime.fromisoformat(resets_at)
                normalized_resets = _rt.replace(second=0, microsecond=0).isoformat()
            except (ValueError, TypeError):
                pass

        cache_key = (quota_type, normalized_resets)

        # Clean up old entries: if resets_at has passed, remove from cache
        if resets_at != "unknown":
            try:
                reset_dt = datetime.fromisoformat(resets_at)
                if reset_dt.tzinfo is None:
                    reset_dt = reset_dt.replace(tzinfo=timezone.utc)
                if reset_dt < datetime.now(timezone.utc):
                    _quota_alerts_sent.pop(cache_key, None)
                    continue  # Block has reset, skip
            except (ValueError, TypeError):
                pass

        sent = _quota_alerts_sent.get(cache_key, set())

        # Find all newly crossed thresholds
        newly_crossed = [t for t in QUOTA_THRESHOLDS if util >= t and t not in sent]

        if newly_crossed:
            # Only alert for the highest newly crossed threshold
            # (e.g. if first seen at 92%, alert 90% not both 80% and 90%)
            highest = max(newly_crossed)
            alerts.append({
                "quota_type": quota_type,
                "utilization": util,
                "threshold": highest,
                "resets_at": resets_at,
            })
            # Mark ALL crossed thresholds as sent (including lower ones)
            sent.update(newly_crossed)

        if sent:
            _quota_alerts_sent[cache_key] = sent

    # Prune stale entries (keep only entries with future reset times)
    stale_keys = []
    for key in _quota_alerts_sent:
        qt, ra = key
        if ra == "unknown":
            continue
        try:
            reset_dt = datetime.fromisoformat(ra)
            if reset_dt.tzinfo is None:
                reset_dt = reset_dt.replace(tzinfo=timezone.utc)
            if reset_dt < datetime.now(timezone.utc):
                stale_keys.append(key)
        except (ValueError, TypeError):
            stale_keys.append(key)
    for k in stale_keys:
        _quota_alerts_sent.pop(k, None)

    # Persist state to disk so it survives process restarts
    _save_quota_alerts()

    return alerts


def format_quota_alert(alert: dict[str, Any]) -> str:
    """Format a single quota alert into an SMS message."""
    util = alert["utilization"]
    threshold = alert["threshold"]
    quota_type = alert["quota_type"]
    resets_at = alert["resets_at"]

    # Format reset time
    reset_str = "unknown"
    if resets_at and resets_at.startswith("monthly:"):
        reset_str = "monthly"
    elif resets_at and resets_at != "unknown":
        try:
            dt = datetime.fromisoformat(resets_at)
            local = dt.astimezone()
            reset_str = local.strftime("%I:%M %p %Z").lstrip("0")
        except (ValueError, TypeError):
            reset_str = resets_at

    emoji = "🔴" if threshold >= 95 else "🟠" if threshold >= 90 else "🟡"
    return f"{emoji} {quota_type}: {util:.0f}% used (resets {reset_str})"
