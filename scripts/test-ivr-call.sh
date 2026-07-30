#!/bin/bash
# Autonomous inbound-audio test: call the USNO time announcement line
# (automated, answers instantly, speaks continuously) and check we can
# capture + transcribe it. No human involved.
TARGET="+18008727245"
CALL="$HOME/.claude/skills/phone-calls/scripts/call"
LOG=/tmp/phone-call/ivr-test.log
mkdir -p /tmp/phone-call
{
  echo "=== IVR capture test $(date) target=$TARGET ==="
  "$CALL" start "$TARGET" --dialer phone-app || { echo "START FAILED"; exit 1; }
  echo "--- flat 30s recording (rings + announcement) ---"
  "$CALL" listen --max 30 --json --raw /tmp/phone-call/ivr-capture.wav
  "$CALL" end
  echo "=== test complete ==="
} >>"$LOG" 2>&1
