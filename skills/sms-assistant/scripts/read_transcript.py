#!/usr/bin/env -S uv run --script
"""
Read recent transcript context from Claude session transcripts.

Usage:
    python read_transcript.py                          # Last 15 entries from previous session
    python read_transcript.py --session jane-doe  # Specific contact
    python read_transcript.py --limit 30               # More entries
    python read_transcript.py --current                # Read current session (not previous)
"""

import json
import argparse
import os
import re
import subprocess
from pathlib import Path
from collections import deque
from datetime import datetime

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


def extract_context(transcript_path, limit=15):
    """Extract useful context from the tail of a transcript."""
    with open(transcript_path, 'r') as f:
        last_lines = deque(f, maxlen=limit * 3)  # Read more to filter

    entries = []
    for line in last_lines:
        try:
            entry = json.loads(line.strip())
            entry_type = entry.get('type', 'unknown')
            timestamp = entry.get('timestamp', '')[:19]
            msg = entry.get('message', {})
            content = msg.get('content', [])

            if entry_type == 'user':
                if isinstance(content, str) and content.strip():
                    # Check if it's an SMS injection
                    sms_text = extract_sms_from_prompt(content)
                    if sms_text:
                        entries.append(f"[{timestamp}] USER_SMS: {sms_text}")
                    elif 'SESSION START' not in content and 'FIRST' not in content:
                        # Not an injection prompt - real user input
                        entries.append(f"[{timestamp}] USER: {content}")
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
                                    entries.append(f"[{timestamp}] RESULT: {result}")

            elif entry_type == 'assistant':
                if isinstance(content, list):
                    for c in content[:2]:  # First 2 items only
                        if isinstance(c, dict):
                            if c.get('type') == 'text':
                                text = c.get('text', '').strip()
                                # Skip generic phrases
                                if text and not text.startswith(('Let me', "I'll", 'Good', 'Got it')):
                                    entries.append(f"[{timestamp}] ASSISTANT: {text}")
                            elif c.get('type') == 'tool_use':
                                name = c.get('name', '')
                                inp = c.get('input', {})
                                # Extract key info - be very concise
                                if name == 'Bash':
                                    cmd = inp.get('command', '')
                                    # Extract just the meaningful part of commands
                                    if 'send-sms' in cmd:
                                        entries.append(f"[{timestamp}] -> Sent SMS")
                                    elif cmd.startswith('#'):
                                        # It's a comment-prefixed command, extract comment
                                        comment = cmd.split('\n')[0]
                                        entries.append(f"[{timestamp}] -> {comment}")
                                    else:
                                        entries.append(f"[{timestamp}] -> Bash: {cmd}")
                                elif name in ('Read', 'Edit', 'Write'):
                                    path = inp.get('file_path', '').split('/')[-1]
                                    entries.append(f"[{timestamp}] -> {name}: {path}")
        except Exception:
            pass

    return entries[-limit:]  # Return only the requested limit


def main():
    parser = argparse.ArgumentParser(description="Read recent transcript context")
    parser.add_argument("--session", help="Filter by session name (e.g., jane-doe)")
    parser.add_argument("--limit", type=int, default=15, help="Max entries to return")
    parser.add_argument("--current", action="store_true", help="Read current session instead of previous")
    args = parser.parse_args()

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
