#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["claude-agent-sdk"]
# ///
"""Offline benchmark of the converse brain — no phone call, no audio.

Exercises the exact ClaudeSDKClient streaming path used by `call converse`
and measures TTFT (time to first token), time-to-first-sentence, and total.
Prints each StreamEvent type seen so we can confirm thinking is OFF and
partial-message streaming is ON.
"""
import asyncio
import re
import sys
import time
from pathlib import Path

HOME = Path.home()
from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions

SYSTEM = """You are Svenka, Eric Maynard's assistant, on a live phone call.
GOAL: confirm whether the restaurant has a table for two at seven thirty tonight.
Reply with ONLY the words to speak aloud, 1-2 short sentences, natural and warm.
Open with a short first sentence. If the goal is met, end with [HANGUP]."""

TURNS = [
    "Thanks for calling Luigi's, how can I help you?",
    "Sure, for how many people and what time?",
    "Yes, seven thirty works, we can seat you. See you then!",
]


def find_cut(buf, min_len=20):
    for m in re.finditer(r"[.!?…](?=\s|$)", buf):
        cand = buf[: m.end()]
        if len(cand.strip()) < min_len:
            continue
        if re.search(r"\b(?:Mr|Mrs|Ms|Dr|St|vs|etc)\.$", cand):
            continue
        return m.end()
    return None


async def main():
    opts = ClaudeAgentOptions(
        cli_path=HOME / ".local" / "bin" / "claude",
        model="claude-haiku-4-5-20251001",
        system_prompt=SYSTEM,
        allowed_tools=[],
        max_turns=1,
        max_thinking_tokens=0,
        include_partial_messages=True,
        setting_sources=[],
    )
    seen_event_types = set()
    async with ClaudeSDKClient(options=opts) as client:
        for i, turn in enumerate(TURNS):
            t0 = time.time()
            await client.query(turn)
            buf = ""
            first_tok = None
            first_sent = None
            sentences = []
            async for msg in client.receive_response():
                mtype = type(msg).__name__
                seen_event_types.add(mtype)
                if mtype != "StreamEvent":
                    continue
                ev = msg.event
                et = ev.get("type")
                if et == "content_block_delta" and ev.get("delta", {}).get("type") == "text_delta":
                    if first_tok is None:
                        first_tok = time.time() - t0
                    buf += ev["delta"]["text"]
                    while True:
                        cut = find_cut(buf)
                        if cut is None:
                            break
                        if first_sent is None:
                            first_sent = time.time() - t0
                        sentences.append(buf[:cut].strip())
                        buf = buf[cut:]
                elif et == "content_block_delta" and ev.get("delta", {}).get("type") == "thinking_delta":
                    seen_event_types.add("THINKING_DELTA")
            if buf.strip():
                sentences.append(buf.strip())
            total = time.time() - t0
            reply = " ".join(sentences)
            print(f"\n--- turn {i+1} ---")
            print(f"THEM: {turn}")
            print(f"ME:   {reply}")
            print(f"TTFT={first_tok:.2f}s  first_sentence={first_sent}  total={total:.2f}s"
                  if first_tok else f"NO TOKENS  total={total:.2f}s")
    print(f"\nevent types seen: {sorted(seen_event_types)}")
    print("THINKING PRESENT!" if "THINKING_DELTA" in seen_event_types else "thinking off ✓")


if __name__ == "__main__":
    asyncio.run(main())
