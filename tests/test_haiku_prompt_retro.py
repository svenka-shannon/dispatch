#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["claude-agent-sdk"]
# ///
"""
Retroactively test the updated haiku health check prompt against
known false-positive and true-positive sessions.

Usage:
    uv run tests/test_haiku_prompt_retro.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Add parent to path for health module
sys.path.insert(0, str(Path(__file__).parent.parent))
from assistant.health import extract_assistant_text, HAIKU_PROMPT, get_transcript_entries_since


# Kill times converted from lifecycle log (EDT/local) to UTC
# EDT = UTC-4, so add 4 hours to get UTC

# Known false positives from 2026-03-28 (sessions that were actively working when killed)
FALSE_POSITIVES = [
    {
        "label": "FP: Active editing + building - 'fragmented responses'",
        "session_cwd": os.path.expanduser("~/transcripts/dispatch-app/1aa4b634-5beb-464d-8087-664ae79f6285"),
        "session_id": "ace531bb-0215-43ed-90fb-5da0efed323f",
        # lifecycle: 2026-03-28 16:43:04 EDT = 20:43:04 UTC
        "kill_time": "2026-03-28T20:43:04+00:00",
        "expected": "HEALTHY",
    },
    {
        "label": "FP: Finishing task + sending summary - 'contradictory claims'",
        "session_cwd": os.path.expanduser("~/transcripts/dispatch-app/a27fdbe4-ba7f-4baa-a8c9-3c19b4061a58"),
        "session_id": "0143525e-2de6-4b4d-a1f8-6348b096c55d",
        # lifecycle: 2026-03-28 20:21:57 EDT = 00:21:57 UTC next day
        "kill_time": "2026-03-29T00:21:57+00:00",
        "expected": "HEALTHY",
    },
    {
        "label": "FP: Running bash commands mid-work - 'crashed mid-task'",
        "session_cwd": os.path.expanduser("~/transcripts/dispatch-app/80f6c894-183c-4909-938a-28a0ddb81f49"),
        "session_id": "1113d9d5-5d3a-4020-9bea-7dbd3886f96f",
        # lifecycle: 2026-03-28 21:11:33 EDT = 01:11:33 UTC next day
        "kill_time": "2026-03-29T01:11:33+00:00",
        "expected": "HEALTHY",
    },
]

# Debatable cases — we expect HEALTHY (session was making progress, just slowly)
DEBATABLE = [
    {
        "label": "DB: Score plateaued at 8.6/10 across iterations",
        "session_cwd": os.path.expanduser("~/transcripts/dispatch-app/a98f6286-adaf-4c21-bd25-33b75cc0c94e"),
        "session_id": "7d88dc27-51aa-40c1-9cf6-d2926b916813",
        # lifecycle: 2026-03-28 15:41:40 EDT = 19:41:40 UTC
        "kill_time": "2026-03-28T19:41:40+00:00",
        "expected": "HEALTHY",
    },
]

# Known true positives (sessions that were genuinely broken)
TRUE_POSITIVES = [
    {
        "label": "TP: Stuck in 'silently resuming' loop (imessage)",
        "session_cwd": os.path.expanduser("~/transcripts/imessage/bce4ce5386564704a6b757beb06d6fb0"),
        "session_id": "1fa8fbd8-3b7b-4dfa-b144-cc1b3339192b",
        # Session has "Silently resumed" repeating from 20:51 to 21:03 UTC
        "kill_time": "2026-03-28T21:04:00+00:00",
        "expected": "FATAL",
    },
]


async def run_case(case: dict) -> tuple[str, str, bool]:
    """Run one test case through the new prompt."""
    from claude_agent_sdk import (
        query as sdk_query,
        ClaudeAgentOptions,
        AssistantMessage,
        TextBlock,
    )

    kill_time = datetime.fromisoformat(case["kill_time"])
    since = kill_time - timedelta(minutes=5)

    entries = get_transcript_entries_since(
        session_cwd=case["session_cwd"],
        session_id=case["session_id"],
        since=since,
    )

    # Filter out entries AFTER the kill time (transcript may contain later sessions)
    filtered = []
    for entry in entries:
        ts_str = entry.get('timestamp', '')
        if ts_str:
            try:
                ts = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
                if ts <= kill_time:
                    filtered.append(entry)
            except ValueError:
                filtered.append(entry)
        else:
            filtered.append(entry)
    entries = filtered

    if not entries:
        return case["label"], "NO ENTRIES FOUND", False

    text = extract_assistant_text(entries)
    if not text or len(text.strip()) < 20:
        return case["label"], "TEXT TOO SHORT", False

    prompt = HAIKU_PROMPT.format(messages=text)

    options = ClaudeAgentOptions(
        cli_path=Path.home() / ".local" / "bin" / "claude",
        model="haiku",
        max_turns=1,
        permission_mode="bypassPermissions",
    )

    result_text = ""
    async for message in sdk_query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    result_text += block.text

    result = result_text.strip()
    verdict = "FATAL" if result.startswith("FATAL:") else "HEALTHY"
    passed = verdict == case["expected"]

    return case["label"], result, passed


async def main():
    all_cases = FALSE_POSITIVES + DEBATABLE + TRUE_POSITIVES
    print(f"Running {len(all_cases)} test cases against updated prompt...\n")

    passed = 0
    failed = 0

    for case in all_cases:
        print(f"Testing: {case['label']}")
        print(f"  Expected: {case['expected']}")

        label, result, ok = await run_case(case)
        status = "✅ PASS" if ok else "❌ FAIL"

        print(f"  Got:      {result[:100]}")
        print(f"  {status}")
        print()

        if ok:
            passed += 1
        else:
            failed += 1

    print(f"\n{'='*60}")
    print(f"Results: {passed}/{len(all_cases)} passed, {failed} failed")

    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
