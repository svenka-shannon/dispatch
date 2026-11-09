#!/usr/bin/env -S uv run --script
"""
Read recent transcript context from Claude session transcripts.

Usage:
    python read_transcript.py                          # Last 15 entries from previous session
    python read_transcript.py --session jane-doe  # Specific contact
    python read_transcript.py --limit 30               # More entries
    python read_transcript.py --current                # Read current session (not previous)
    python read_transcript.py --session X --current --since 24h --limit 400
                                                       # Everything from the last 24h, across
                                                       # all of the session's transcript files

--since accepts a relative window (90m, 24h, 7d, 2w, 30s) or an ISO-8601
timestamp (naive timestamps are treated as local time). With --since, the
whole file is streamed (not just the tail), only entries whose timestamp is
inside the window are kept, and every transcript file of the session modified
since the cutoff is read oldest-first (so a mid-day restart isn't missed).
--limit still caps output to the most recent N entries inside the window.
"""

import json
import argparse
import os
import re
import subprocess
from pathlib import Path
from collections import deque
from datetime import datetime, timedelta, timezone

CONTACTS_CLI = os.path.expanduser("~/.claude/skills/contacts/scripts/contacts")

# Cache for tier lookups
_tier_cache = {}


def session_to_contact_name(session_name):
    """Convert session name (jane-doe) to contact name (Jane Doe)."""
    return session_name.replace('-', ' ').title()


def lookup_tier(session_name):
    """Look up contact tier by session name. Results are cached."""
    if not session_name or not os.path.exists(CONTACTS_CLI):
        return None

    if session_name in _tier_cache:
        return _tier_cache[session_name]

    contact_name = session_to_contact_name(session_name)
    try:
        result = subprocess.run(
            [CONTACTS_CLI, "tier", contact_name],
            capture_output=True, text=True, timeout=5
        )
        tier = result.stdout.strip()
        if tier in ('admin', 'partner', 'family', 'favorite'):
            _tier_cache[session_name] = tier
            return tier
    except Exception:
        pass

    _tier_cache[session_name] = None
    return None


def project_dir_name(session_name, home=None):
    """Map a session (e.g. "imessage/_15555550100") to its ~/.claude/projects dir name.

    Claude Code derives the project dir from the session cwd
    (~/transcripts/<session>) by replacing EVERY non-alphanumeric char with
    '-' -- so '/', '_', '.', '+' all become '-'.
    """
    home = Path(home) if home else Path.home()
    return re.sub(r"[^A-Za-z0-9]", "-", str(home / "transcripts" / session_name))


def find_transcripts(session_name=None):
    """Find all transcripts, optionally filtered by session name."""
    base = Path.home() / ".claude/projects"

    if session_name:
        # Exact match on the Claude Code project dir for this session's cwd
        # (excludes sibling "-spawn-<id>" dirs).
        target = project_dir_name(session_name)
        dirs = [d for d in base.iterdir() if d.is_dir() and d.name == target]
    else:
        # Find all transcript directories
        dirs = [d for d in base.iterdir() if d.is_dir() and "transcripts" in d.name]

    if not dirs:
        return []

    # Collect all jsonl files from matching directories
    transcripts = []
    for d in dirs:
        transcripts.extend(d.glob("*.jsonl"))

    # Sort by modification time, newest first
    return sorted(transcripts, key=lambda p: p.stat().st_mtime, reverse=True)


def extract_sms_from_prompt(text):
    """Extract just the SMS content from an injection prompt."""
    # Look for the actual message between SMS markers
    if '---SMS FROM' in text and '---END SMS---' in text:
        start = text.find('---SMS FROM')
        # Find sender line and skip it
        msg_start = text.find('\n', start) + 1
        end = text.find('---END SMS---')
        if msg_start > 0 and end > msg_start:
            return text[msg_start:end].strip()
    return None


def format_entry(entry, out):
    """Append display lines for one transcript entry to `out`.

    Appends in place (rather than returning a list) so that, exactly as
    before, lines appended before a mid-entry exception are kept.
    """
    entry_type = entry.get('type', 'unknown')
    timestamp = entry.get('timestamp', '')[:19]
    msg = entry.get('message', {})
    content = msg.get('content', [])

    if entry_type == 'user':
        if isinstance(content, str) and content.strip():
            # Check if it's an SMS injection
            sms_text = extract_sms_from_prompt(content)
            if sms_text:
                out.append(f"[{timestamp}] USER_SMS: {sms_text}")
            elif 'SESSION START' not in content and 'FIRST' not in content:
                # Not an injection prompt - real user input
                out.append(f"[{timestamp}] USER: {content}")
        elif isinstance(content, list):
            for c in content:
                if isinstance(c, dict):
                    if c.get('type') == 'tool_result':
                        result = str(c.get('content', ''))
                        # Skip noisy results - only keep meaningful ones
                        if ('SENT|' not in result and
                            len(result) > 20 and
                            'Exit code' not in result and
                            '===' not in result):
                            out.append(f"[{timestamp}] RESULT: {result}")

    elif entry_type == 'assistant':
        if isinstance(content, list):
            for c in content[:2]:  # First 2 items only
                if isinstance(c, dict):
                    if c.get('type') == 'text':
                        text = c.get('text', '').strip()
                        # Skip generic phrases
                        if text and not text.startswith(('Let me', "I'll", 'Good', 'Got it')):
                            out.append(f"[{timestamp}] ASSISTANT: {text}")
                    elif c.get('type') == 'tool_use':
                        name = c.get('name', '')
                        inp = c.get('input', {})
                        # Extract key info - be very concise
                        if name == 'Bash':
                            cmd = inp.get('command', '')
                            # Extract just the meaningful part of commands
                            if 'send-sms' in cmd:
                                out.append(f"[{timestamp}] -> Sent SMS")
                            elif cmd.startswith('#'):
                                # It's a comment-prefixed command, extract comment
                                comment = cmd.split('\n')[0]
                                out.append(f"[{timestamp}] -> {comment}")
                            else:
                                out.append(f"[{timestamp}] -> Bash: {cmd}")
                        elif name in ('Read', 'Edit', 'Write'):
                            path = inp.get('file_path', '').split('/')[-1]
                            out.append(f"[{timestamp}] -> {name}: {path}")


def extract_context(transcript_path, limit=15):
    """Extract useful context from the tail of a transcript."""
    with open(transcript_path, 'r') as f:
        last_lines = deque(f, maxlen=limit * 3)  # Read more to filter

    entries = []
    for line in last_lines:
        try:
            format_entry(json.loads(line.strip()), entries)
        except Exception:
            pass

    return entries[-limit:]  # Return only the requested limit


_RELATIVE_RE = re.compile(r"^\s*(\d+)\s*([smhdw])\s*$", re.IGNORECASE)
_UNIT_SECONDS = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400, 'w': 604800}


def parse_since(value, now=None):
    """Parse --since into an aware UTC datetime cutoff.

    Accepts relative windows ("90m", "24h", "7d", "2w", "30s") or an ISO-8601
    timestamp. Naive ISO timestamps are interpreted as local time.
    Raises ValueError on anything else.
    """
    now = now or datetime.now(timezone.utc)
    m = _RELATIVE_RE.match(value)
    if m:
        return now - timedelta(seconds=int(m.group(1)) * _UNIT_SECONDS[m.group(2).lower()])
    text = value.strip()
    if text.endswith(('Z', 'z')):
        text = text[:-1] + '+00:00'
    dt = datetime.fromisoformat(text)  # raises ValueError if invalid
    if dt.tzinfo is None:
        dt = dt.astimezone()  # local time -> aware
    return dt.astimezone(timezone.utc)


def parse_entry_timestamp(value):
    """Parse a transcript entry timestamp to an aware UTC datetime, or None."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith(('Z', 'z')):
        text = text[:-1] + '+00:00'
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)  # transcript timestamps are UTC
    return dt.astimezone(timezone.utc)


def transcripts_since(transcripts, cutoff):
    """Transcript files modified at/after cutoff, oldest first."""
    cutoff_ts = cutoff.timestamp()
    selected = [t for t in transcripts if t.stat().st_mtime >= cutoff_ts]
    return sorted(selected, key=lambda p: p.stat().st_mtime)


def extract_context_since(transcript_paths, cutoff, limit=15):
    """Stream whole transcripts, keeping entries with timestamp >= cutoff.

    Returns (entries, total): the most recent `limit` display lines inside
    the window, and the total number of display lines inside the window.
    """
    kept = deque(maxlen=limit) if limit and limit > 0 else deque()
    total = 0
    for path in transcript_paths:
        with open(path, 'r') as f:
            for line in f:
                try:
                    entry = json.loads(line.strip())
                    ts = parse_entry_timestamp(entry.get('timestamp'))
                    if ts is None or ts < cutoff:
                        continue
                    out = []
                    try:
                        format_entry(entry, out)
                    finally:
                        total += len(out)
                        kept.extend(out)
                except Exception:
                    pass
    return list(kept), total


def format_cutoff(cutoff):
    return cutoff.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def print_file_header(transcript, label):
    print(f"# {label}: {transcript.name[:20]}...")
    print(f"# Size: {transcript.stat().st_size / 1024:.1f} KB")
    print(f"# Modified: {datetime.fromtimestamp(transcript.stat().st_mtime).strftime('%Y-%m-%d %H:%M:%S')}")


def run_since(transcripts, cutoff, limit):
    """--since mode: read every session transcript touched inside the window."""
    selected = transcripts_since(transcripts, cutoff)
    since_iso = format_cutoff(cutoff)
    print(f"# Transcripts in window: {len(selected)} (oldest first)")
    for t in selected:
        print_file_header(t, "Transcript")
    print()

    entries, total = extract_context_since(selected, cutoff, limit)
    if not entries:
        print(f"(No entries since {since_iso})")
        return

    print(f"# Window: since {since_iso} ({total} entries)")
    if len(entries) < total:
        print(f"# Showing most recent {len(entries)} of {total} (--limit {limit})")
    print()
    for entry in entries:
        print(entry)


def main():
    parser = argparse.ArgumentParser(description="Read recent transcript context")
    parser.add_argument("--session", help="Filter by session name (e.g., jane-doe)")
    parser.add_argument("--limit", type=int, default=15, help="Max entries to return")
    parser.add_argument("--current", action="store_true", help="Read current session instead of previous")
    parser.add_argument("--since", help="Only entries inside this window: 90m, 24h, 7d, 2w, or ISO-8601. "
                                        "Streams whole files and includes every session transcript "
                                        "modified since the cutoff, oldest first.")
    args = parser.parse_args()

    cutoff = None
    if args.since:
        try:
            cutoff = parse_since(args.since)
        except ValueError:
            parser.error(f"invalid --since value: {args.since!r} (use e.g. 90m, 24h, 7d, or ISO-8601)")

    transcripts = find_transcripts(args.session)

    if not transcripts:
        print("No transcripts found")
        return

    # Look up contact info if session provided
    if args.session:
        contact_name = session_to_contact_name(args.session)
        tier = lookup_tier(args.session)
        if tier:
            print(f"# Contact: {contact_name} | Tier: {tier}")
        else:
            print(f"# Session: {args.session}")
        print()

    if cutoff is not None:
        run_since(transcripts, cutoff, args.limit)
        return

    # By default, read the PREVIOUS transcript (second most recent)
    # This gives context from before the current session started
    if args.current or len(transcripts) < 2:
        transcript = transcripts[0]
        print(f"# Current transcript: {transcript.name[:20]}...")
    else:
        # Find the second most recent that's not a background session
        for t in transcripts[1:]:
            # Check if it's a real session (not just a few lines)
            if t.stat().st_size > 10000:  # At least 10KB
                transcript = t
                print(f"# Previous transcript: {transcript.name[:20]}...")
                break
        else:
            transcript = transcripts[1] if len(transcripts) > 1 else transcripts[0]
            print(f"# Previous transcript: {transcript.name[:20]}...")

    print(f"# Size: {transcript.stat().st_size / 1024:.1f} KB")
    print(f"# Modified: {datetime.fromtimestamp(transcript.stat().st_mtime).strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    entries = extract_context(transcript, args.limit)

    if not entries:
        print("(No relevant entries found)")
        return

    for entry in entries:
        print(entry)


if __name__ == "__main__":
    main()
