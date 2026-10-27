#!/usr/bin/env python3
"""
Native Reminder System - Shared Module

This module is used by both the daemon (manager.py) and CLI (claude-assistant remind).
All reminder file operations go through this module to ensure consistent locking.

Design: v6 (9.2/10 review score)
- Cron runs in local time (handles DST automatically)
- Internal storage in UTC for comparison
- CLI displays local times
- Per-reminder timezone override
- Configurable constants
"""

import os
import sys
import json
import fcntl
import uuid
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from contextlib import contextmanager
from typing import Optional, Dict, Any, List

from zoneinfo import ZoneInfo

from croniter import croniter

# Paths
STATE_DIR = Path.home() / "dispatch/state"
REMINDERS_FILE = STATE_DIR / "reminders.json"
LOCK_FILE = STATE_DIR / "reminders.lock"
PID_FILE = STATE_DIR / "reminder-daemon.pid"

# macOS-specific full sync constant
F_FULLFSYNC = 51

# Default configuration
DEFAULT_CONFIG = {
    "default_timezone": "America/New_York",
    "max_retries": 3,
    "backoff_seconds": [60, 120, 240],
    "catch_up_max_hours": 24,
    "poll_interval_seconds": 5
}


def _fsync(fd: int):
    """Platform-aware fsync with true durability on macOS."""
    if sys.platform == 'darwin':
        try:
            fcntl.fcntl(fd, F_FULLFSYNC)
        except OSError:
            os.fsync(fd)  # Fallback
    else:
        os.fsync(fd)


@contextmanager
def reminders_lock():
    """
    Advisory lock for JSON access.

    CRITICAL: Must be used by BOTH daemon AND CLI for any reminder file operations.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(LOCK_FILE, 'w') as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def load_reminders() -> Dict[str, Any]:
    """
    Load reminders from JSON file.

    Must be called within reminders_lock().
    Returns dict with 'version', 'config', and 'reminders' keys.
    """
    if REMINDERS_FILE.exists():
        try:
            data = json.loads(REMINDERS_FILE.read_text())
            # Ensure config exists with defaults
            if "config" not in data:
                data["config"] = DEFAULT_CONFIG.copy()
            else:
                # Merge with defaults for any missing keys
                for key, value in DEFAULT_CONFIG.items():
                    if key not in data["config"]:
                        data["config"][key] = value
            return data
        except json.JSONDecodeError:
            pass
    return {
        "version": 1,
        "config": DEFAULT_CONFIG.copy(),
        "reminders": []
    }


def save_reminders(data: Dict[str, Any]):
    """
    Atomically save reminders to JSON file with crash safety.

    Must be called within reminders_lock().
    Uses temp file + fsync + rename pattern for durability.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp_path = REMINDERS_FILE.with_suffix('.tmp')

    with open(temp_path, 'w') as f:
        json.dump(data, f, indent=2)
        f.flush()
        _fsync(f.fileno())

    os.rename(temp_path, REMINDERS_FILE)

    # Sync directory entry for paranoia
    dir_fd = os.open(REMINDERS_FILE.parent, os.O_DIRECTORY)
    try:
        _fsync(dir_fd)
    finally:
        os.close(dir_fd)


def get_system_timezone() -> str:
    """Get local timezone name from system."""
    try:
        link = os.readlink('/etc/localtime')
        # /var/db/timezone/zoneinfo/America/New_York -> America/New_York
        return link.split('zoneinfo/')[-1]
    except:
        return "America/New_York"  # Sensible default


def parse_duration(duration_str: str) -> timedelta:
    """
    Parse duration string like '30m', '2h', '1d', '1w'.

    Supported formats:
    - Xm: minutes
    - Xh: hours
    - Xd: days
    - Xw: weeks
    - XhYm: combined (e.g., '2h30m')
    """
    total_seconds = 0
    pattern = r'(\d+)([mhdw])'

    for match in re.finditer(pattern, duration_str.lower()):
        value = int(match.group(1))
        unit = match.group(2)

        if unit == 'm':
            total_seconds += value * 60
        elif unit == 'h':
            total_seconds += value * 3600
        elif unit == 'd':
            total_seconds += value * 86400
        elif unit == 'w':
            total_seconds += value * 604800

    if total_seconds == 0:
        raise ValueError(f"Invalid duration format: {duration_str}")

    return timedelta(seconds=total_seconds)


def parse_time_string(time_str: str, tz_name: str) -> datetime:
    """
    Parse a time string to UTC datetime.

    Supported formats:
    - '3pm', '3:30pm', '15:00' (today or tomorrow if past)
    - '2026-03-03 15:00'
    - ISO format
    """
    tz = ZoneInfo(tz_name)
    now = datetime.now(tz)

    # Try ISO format first
    try:
        if 'T' in time_str or '-' in time_str:
            dt = datetime.fromisoformat(time_str.replace('Z', '+00:00'))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=tz)
            return dt.astimezone(timezone.utc)
    except:
        pass

    # Try time-only formats: 3pm, 3:30pm, 15:00
    time_patterns = [
        (r'^(\d{1,2}):(\d{2})\s*(am|pm)?$', True),   # 3:30pm, 15:00
        (r'^(\d{1,2})\s*(am|pm)$', False),            # 3pm
    ]

    for pattern, has_minutes in time_patterns:
        match = re.match(pattern, time_str.lower().strip())
        if match:
            hour = int(match.group(1))
            minute = int(match.group(2)) if has_minutes else 0
            ampm = match.group(3) if has_minutes else match.group(2)

            if ampm:
                if ampm == 'pm' and hour != 12:
                    hour += 12
                elif ampm == 'am' and hour == 12:
                    hour = 0

            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

            # If time is in the past today, schedule for tomorrow
            if target <= now:
                target += timedelta(days=1)

            return target.astimezone(timezone.utc)

    raise ValueError(f"Cannot parse time: {time_str}")


def next_cron_fire(pattern: str, tz_name: str) -> str:
    """
    Get next fire time for cron pattern in given timezone.

    Cron is evaluated in local time, result returned as UTC ISO string.
    Handles DST transitions automatically via zoneinfo.
    """
    tz = ZoneInfo(tz_name)
    now_local = datetime.now(tz)
    cron = croniter(pattern, now_local)
    next_local = cron.get_next(datetime)

    # croniter returns naive datetime representing local time
    if next_local.tzinfo is None:
        next_local = next_local.replace(tzinfo=tz)

    next_utc = next_local.astimezone(timezone.utc)
    return next_utc.isoformat().replace('+00:00', 'Z')


def format_for_display(utc_str: str, tz_name: str) -> str:
    """Format UTC timestamp for local display."""
    utc_dt = datetime.fromisoformat(utc_str.replace('Z', '+00:00'))
    tz = ZoneInfo(tz_name)
    local_dt = utc_dt.astimezone(tz)
    return local_dt.strftime("%Y-%m-%d %I:%M %p %Z")


def validate_event_template(event: Dict[str, Any]) -> None:
    """Validate a bus event template at creation time.

    Raises ValueError on invalid config. This prevents misconfigured
    reminders from failing silently weeks later at fire time.
    """
    if not isinstance(event, dict):
        raise ValueError("event template must be a dict")
    if "topic" not in event:
        raise ValueError("event template must have 'topic'")
    if "type" not in event:
        raise ValueError("event template must have 'type'")
    # Validate task.requested payloads more strictly
    if event["type"] == "task.requested":
        payload = event.get("payload", {})
        if not isinstance(payload, dict):
            raise ValueError("task.requested payload must be a dict")
        execution = payload.get("execution", {})
        if not isinstance(execution, dict):
            raise ValueError("task.requested must have execution dict")
        mode = execution.get("mode")
        if mode not in ("agent", "script"):
            raise ValueError(
                f"task.requested execution.mode must be 'agent' or 'script', got: {mode!r}"
            )
        if mode == "script" and "command" not in execution:
            raise ValueError("script tasks must have execution.command")
        if mode == "agent" and "prompt" not in execution:
            raise ValueError("agent tasks must have execution.prompt")


def create_reminder(
    title: str,
    contact: Optional[str] = None,
    schedule_type: str = "once",
    schedule_value: str = "",
    tz_name: Optional[str] = None,
    target: str = "fg",
    event: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Create a new reminder dict.

    Two modes:
    1. Legacy (contact + target): inject into a chat session on fire
    2. Generalized (event template): produce any bus event on fire

    Args:
        title: Reminder title/description
        contact: Contact name, phone, or chat_id (legacy mode)
        schedule_type: 'once' or 'cron'
        schedule_value: ISO datetime for 'once', cron pattern for 'cron'
        tz_name: Timezone override (uses default if None)
        target: Target session - 'fg' or 'spawn' (legacy mode)
        event: Bus event template to produce on fire (generalized mode)

    Returns:
        New reminder dict ready to append to reminders list
    """
    reminder_id = str(uuid.uuid4())[:8]
    now_utc = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')

    reminder = {
        "id": reminder_id,
        "title": title,
        "schedule": {
            "type": schedule_type,
            "value": schedule_value,
        },
        "next_fire": schedule_value if schedule_type == "once" else None,
        "created_at": now_utc,
        "last_fired": None,
        "fired_count": 0,
        "retry_count": 0,
        "last_error": None
    }

    if event:
        # Generalized mode: validate and store event template
        validate_event_template(event)
        reminder["event"] = event
    else:
        # Legacy mode: validate target and store contact
        if target not in ("fg", "spawn"):
            raise ValueError(f"Invalid target: {target}. Must be 'fg' or 'spawn'")
        if not contact:
            raise ValueError("Legacy reminders must have a contact")
        reminder["contact"] = contact
        reminder["target"] = target

    if tz_name:
        reminder["schedule"]["timezone"] = tz_name

    # Compute next_fire for cron
    if schedule_type == "cron":
        # Use provided timezone or will use default when firing
        fire_tz = tz_name or get_system_timezone()
        reminder["next_fire"] = next_cron_fire(schedule_value, fire_tz)

    return reminder


def get_reminder_timezone(reminder: Dict[str, Any], config: Dict[str, Any]) -> str:
    """Get the effective timezone for a reminder."""
    return reminder.get("schedule", {}).get("timezone") or config.get("default_timezone", "America/New_York")


# CLI helper functions

def add_reminder_cli(title: str, contact: Optional[str] = None,
                     in_duration: Optional[str] = None,
                     at_time: Optional[str] = None, cron_pattern: Optional[str] = None,
                     tz_override: Optional[str] = None, target: str = "fg",
                     event_json: Optional[str] = None) -> Dict[str, Any]:
    """
    Add a reminder via CLI.

    One of in_duration, at_time, or cron_pattern must be provided.

    Two modes:
    - Legacy: contact + target (inject into chat session)
    - Generalized: event_json (produce any bus event)

    Returns the created reminder.
    """
    import json as _json

    with reminders_lock():
        data = load_reminders()
        config = data["config"]
        tz_name = tz_override or config["default_timezone"]

        if in_duration:
            # Relative time - compute absolute
            delta = parse_duration(in_duration)
            fire_time = datetime.now(timezone.utc) + delta
            schedule_value = fire_time.isoformat().replace('+00:00', 'Z')
            schedule_type = "once"
        elif at_time:
            # Absolute time in local timezone
            fire_time = parse_time_string(at_time, tz_name)
            schedule_value = fire_time.isoformat().replace('+00:00', 'Z')
            schedule_type = "once"
        elif cron_pattern:
            schedule_value = cron_pattern
            schedule_type = "cron"
        else:
            raise ValueError("Must specify --in, --at, or --cron")

        # Parse event JSON if provided
        event = None
        if event_json:
            try:
                event = _json.loads(event_json)
            except _json.JSONDecodeError as e:
                raise ValueError(f"Invalid event JSON: {e}")

        reminder = create_reminder(
            title=title,
            contact=contact,
            schedule_type=schedule_type,
            schedule_value=schedule_value,
            tz_name=tz_override,  # Only store if explicitly overridden
            target=target,
            event=event,
        )

        # For cron, compute next_fire with timezone
        if schedule_type == "cron" and cron_pattern:
            assert tz_name is not None  # guaranteed above
            reminder["next_fire"] = next_cron_fire(cron_pattern, tz_name)

        data["reminders"].append(reminder)
        save_reminders(data)

        return reminder


def list_reminders_cli(contact: Optional[str] = None,
                       show_failed: bool = False) -> List[Dict[str, Any]]:
    """List reminders, optionally filtered."""
    with reminders_lock():
        data = load_reminders()
        config = data["config"]
        reminders = data["reminders"]

        if contact:
            reminders = [r for r in reminders if r.get("contact") == contact]

        if show_failed:
            max_retries = config["max_retries"]
            reminders = [r for r in reminders if r["retry_count"] >= max_retries]

        # Add display_time for each reminder
        for r in reminders:
            tz = get_reminder_timezone(r, config)
            r["_display_time"] = format_for_display(r["next_fire"], tz)
            r["_timezone"] = tz

        return reminders


def cancel_reminders_cli(reminder_id: Optional[str] = None,
                         title: Optional[str] = None,
                         match: Optional[str] = None,
                         force: bool = False) -> List[Dict[str, Any]]:
    """
    Cancel reminder(s) by ID, exact title, or case-insensitive substring match.

    - reminder_id: exact id
    - title: exact title (raises ValueError if multiple match and not force)
    - match: case-insensitive substring of the FULL title; cancels all matches

    Returns the list of cancelled reminder records (empty if nothing matched).
    """
    with reminders_lock():
        data = load_reminders()

        if reminder_id:
            def is_target(r):
                return r["id"] == reminder_id
        elif match is not None:
            if not match.strip():
                raise ValueError("--match must be a non-empty string")
            needle = match.casefold()

            def is_target(r):
                return needle in (r.get("title") or "").casefold()
        elif title:
            matches = [r for r in data["reminders"] if r["title"] == title]
            if len(matches) > 1 and not force:
                raise ValueError(f"Multiple reminders match title '{title}'. Use --force or specify ID.")

            def is_target(r):
                return r["title"] == title
        else:
            raise ValueError("Must specify reminder ID, --title, or --match")

        cancelled = [r for r in data["reminders"] if is_target(r)]
        if cancelled:
            data["reminders"] = [r for r in data["reminders"] if not is_target(r)]
            save_reminders(data)

        return cancelled


def cancel_reminder_cli(reminder_id: Optional[str] = None,
                        title: Optional[str] = None,
                        force: bool = False,
                        match: Optional[str] = None) -> int:
    """
    Cancel reminder(s) by ID, title, or substring match.

    Returns number of reminders cancelled.
    """
    return len(cancel_reminders_cli(reminder_id=reminder_id, title=title,
                                    match=match, force=force))


def retry_reminder_cli(reminder_id: str) -> bool:
    """
    Reset retry count for a failed reminder.

    Returns True if reminder was found and reset.
    """
    with reminders_lock():
        data = load_reminders()

        for r in data["reminders"]:
            if r["id"] == reminder_id:
                r["retry_count"] = 0
                r["last_error"] = None
                save_reminders(data)
                return True

        return False


def preview_cron_cli(pattern: str, tz_name: Optional[str] = None, count: int = 5) -> List[str]:
    """
    Preview next N fire times for a cron pattern.

    Returns list of formatted local time strings.
    """
    if tz_name is None:
        with reminders_lock():
            data = load_reminders()
            tz_name = data["config"]["default_timezone"]

    tz = ZoneInfo(tz_name)
    now_local = datetime.now(tz)
    cron = croniter(pattern, now_local)

    times = []
    for _ in range(count):
        next_local = cron.get_next(datetime)
        if next_local.tzinfo is None:
            next_local = next_local.replace(tzinfo=tz)
        times.append(next_local.strftime("%Y-%m-%d %I:%M %p %Z"))

    return times
