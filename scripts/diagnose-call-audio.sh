#!/bin/bash
# Diagnose where far-end call audio is going. Calls the USNO time line,
# dumps Phone's menus mid-call (looking for an in-app audio device picker),
# and records RAW audio (no VAD) from BlackHole 16ch to measure levels.
TARGET="+12027621401"
CALL="$HOME/.claude/skills/phone-calls/scripts/call"
AX="$HOME/.claude/skills/axctl/scripts/axctl"
LOG=/tmp/phone-call/diagnose.log
mkdir -p /tmp/phone-call
{
  echo "=== call audio diagnosis $(date) ==="
  "$CALL" start "$TARGET" --dialer phone-app || { echo "START FAILED"; exit 1; }
  sleep 8   # let the IVR answer and start talking
  echo "--- Phone menu bar (in-call), looking for audio device menu ---"
  "$AX" tree Phone --all-attributes 2>&1 | grep -iE "AXMenu(BarItem|Item).*(AXTitle|AXDescription)='[^']*'" | grep -oE "AXTitle='[^']*'|AXDescription='[^']*'" | sort -u
  echo "--- raw 12s capture from BlackHole 16ch (no VAD) ---"
  "$CALL" listen --max 12 --no-transcribe --raw /tmp/phone-call/raw-capture.wav --json
  echo "--- also raw 6s from BlackHole 2ch (is far-end audio landing there?) ---"
  "$CALL" listen --max 6 --no-transcribe --raw /tmp/phone-call/raw-capture-2ch.wav --device "BlackHole 2ch" --json
  "$CALL" end
  echo "=== diagnosis complete ==="
} >>"$LOG" 2>&1
