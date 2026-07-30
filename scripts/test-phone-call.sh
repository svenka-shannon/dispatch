#!/bin/bash
# Live end-to-end test of the phone-calls skill from a daemon session.
# Dials, then runs the real-time converse loop (greeting, echo test, goodbye).
TARGET="${1:-+15182587876}"
CALL="$HOME/.claude/skills/phone-calls/scripts/call"
LOG=/tmp/phone-call/test-call.log
mkdir -p /tmp/phone-call
{
  echo "=== live converse test $(date) target=$TARGET ==="
  "$CALL" start "$TARGET" --dialer phone-app || { echo "START FAILED"; exit 1; }
  "$CALL" converse \
    --goal "This is a live test call with Eric, your owner, who knows exactly what this is. Chat briefly: confirm he can hear you, answer anything he asks, repeat back something he says to prove you can hear him. Keep it light and short; when he is satisfied or says goodbye, end with [HANGUP]." \
    --greeting "Hi Eric, it's Svenka. I can hear you now, and this time I'm running on the fast conversation loop. Say something and I'll actually respond." \
    --answer-timeout 30 --turn-timeout 20 --max-minutes 4 \
    --transcript /tmp/phone-call/live-transcript.txt
  "$CALL" end
  echo "=== test complete ==="
} >>"$LOG" 2>&1
