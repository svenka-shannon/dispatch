#!/bin/bash
# Offline barge-in test: give converse a goal that makes it talk a while, then
# have the "caller" interrupt mid-reply. Assert converse logs a barge-in and
# stops. No phone call.
set -e
CALL="$HOME/.claude/skills/phone-calls/scripts/call"
DIR=/tmp/phone-call
mkdir -p "$DIR"
rm -f "$DIR/bargein-transcript.txt"
"$CALL" devices --setup >/dev/null 2>&1 || true

(
  sleep 4
  # short line — consumed by answer-detection so converse enters the main loop
  "$CALL" say "Hello?" --device "BlackHole 16ch"
  sleep 2
  # question that makes Svenka give a long, detailed reply
  "$CALL" say "Hi there, can you tell me everything about the reservation you want, with all the details?" --device "BlackHole 16ch"
  # 2.5s in, she's mid-reply and speaking — interrupt her hard
  sleep 2.5
  "$CALL" say "Wait, wait, stop, hold on, let me interrupt you there for a second." --device "BlackHole 16ch"
) &
SPEAKER=$!

"$CALL" converse \
  --goal "Explain in a few sentences that you're calling on behalf of Eric to book a large dinner reservation, giving lots of detail about the party size and timing preferences." \
  --answer-timeout 20 --turn-timeout 12 --max-minutes 2 \
  --transcript "$DIR/bargein-transcript.txt" > "$DIR/bargein-run.log" 2>&1 || true

kill $SPEAKER 2>/dev/null || true
"$CALL" devices --restore >/dev/null 2>&1 || true
echo "=== bargein done ==="
if grep -q "barge-in:" "$DIR/bargein-run.log"; then
  echo "PASS: barge-in fired"
else
  echo "FAIL: no barge-in detected"
fi
grep -E 'barge-in|interrupted|THEM:|ME:|SYS' "$DIR/bargein-run.log" || true
