#!/bin/bash
# Full-dress rehearsal: run the converse loop against Amtrak's "Julie" IVR.
# Robot-on-robot — proves full duplex conversation on a real call, no human.
TARGET="+18008727245"
CALL="$HOME/.claude/skills/phone-calls/scripts/call"
LOG=/tmp/phone-call/converse-ivr.log
mkdir -p /tmp/phone-call
{
  echo "=== converse-vs-IVR rehearsal $(date) target=$TARGET ==="
  "$CALL" start "$TARGET" --dialer phone-app || { echo "START FAILED"; exit 1; }
  "$CALL" converse \
    --goal "You are talking to Amtrak's automated phone agent (Julie) purely as a live audio test. When it speaks, respond naturally to its prompts (e.g. say 'train status' if asked what you want). After two exchanges, say thank you and goodbye, then [HANGUP]. Do NOT book or reserve anything." \
    --answer-timeout 45 --turn-timeout 30 --max-minutes 3 \
    --transcript /tmp/phone-call/converse-ivr-transcript.txt
  "$CALL" end
  echo "=== rehearsal complete ==="
} >>"$LOG" 2>&1
