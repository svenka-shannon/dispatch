#!/bin/bash
# Offline full-loop test of `call converse` — NO phone call.
# converse listens on BlackHole 16ch and speaks on BlackHole 2ch.
# We play the "other party" lines INTO BlackHole 16ch so converse hears them
# (16ch loops output->input). Proves the whole brain+TTS+playback pipeline
# and surfaces hangs, without dialing anyone.
set -e
CALL="$HOME/.claude/skills/phone-calls/scripts/call"
DIR=/tmp/phone-call
mkdir -p "$DIR"
rm -f "$DIR/loopback-transcript.txt"

# make sure audio is routed (converse assumes devices exist; routing also
# maxes volume so playback is audible to the recorder)
"$CALL" devices --setup >/dev/null 2>&1 || true

# background: the "restaurant" speaks 3 lines into BlackHole 16ch on a timer
# Gaps must exceed converse's reply-speak time (~7-9s) or lines land while
# Svenka is still talking and get missed. 13s spacing keeps turns clean.
(
  sleep 4
  "$CALL" say "Thanks for calling Luigi's, how can I help you?" --device "BlackHole 16ch"
  sleep 13
  "$CALL" say "Sure, for how many people and what time?" --device "BlackHole 16ch"
  sleep 13
  "$CALL" say "Yes, seven thirty works, we can seat you. See you then!" --device "BlackHole 16ch"
) &
SPEAKER=$!

# converse drives the call side; short timeouts so it can't hang the test
"$CALL" converse \
  --goal "Confirm whether the restaurant has a table for two at seven thirty tonight. When confirmed, thank them and [HANGUP]." \
  --answer-timeout 20 --turn-timeout 12 --max-minutes 2 \
  --transcript "$DIR/loopback-transcript.txt" || true

kill $SPEAKER 2>/dev/null || true
"$CALL" devices --restore >/dev/null 2>&1 || true
echo "=== loopback done ==="
echo "--- transcript ---"
cat "$DIR/loopback-transcript.txt" 2>/dev/null || echo "(no transcript)"
