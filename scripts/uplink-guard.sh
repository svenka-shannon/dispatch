#!/bin/bash
# uplink-guard: backstop for the 2026-09-18 degraded-uplink failure mode.
#
# When the Wi-Fi uplink chokes sustained uploads >~1.5MB, sessions with large
# contexts fail every turn with ECONNRESET. The daemon's stuck-detector
# restarts WITH resume, which re-uploads the same giant context and can never
# succeed. The only working remedy is a --clean restart (fresh context; the
# session re-reads SMS history on start).
#
# This script fires from a cron reminder (task.requested, script mode). It
# clean-restarts a session only when its last TWO completed turns both errored
# — the signature that a resume-restart already failed — and at most once per
# cooldown window. Remove the reminder to disarm (claude-assistant remind list).
set -u

CHAT_ID="$("$HOME/dispatch/bin/identity" owner.phone)"
SESSION="imessage/${CHAT_ID/+/_}"
BUS_DB="$HOME/dispatch/state/bus.db"
STATE="$HOME/dispatch/state/uplink-guard.last"
LOG="$HOME/dispatch/logs/uplink-guard.log"
CLI="$HOME/dispatch/bin/claude-assistant"
COOLDOWN_S=1500  # 25 min

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') | $*" >> "$LOG"; }

errs=$(sqlite3 -readonly "$BUS_DB" \
  "SELECT COALESCE(SUM(json_extract(payload,'$.is_error')),0) FROM (
     SELECT payload FROM records
     WHERE topic='system' AND type='sdk.turn_complete' AND key='$SESSION'
     ORDER BY timestamp DESC LIMIT 2);") || { log "bus query failed"; exit 1; }

if [ "$errs" != "2" ]; then
  exit 0  # healthy or only one failure so far — let the daemon's own recovery try first
fi

# The error signal is stale until a fresh session completes its first turn.
# If a session was created after the newest completed turn, a recovery is
# already in flight — stand down and let it finish (or fail, which updates
# the turn events and re-arms us).
last_turn_ts=$(sqlite3 -readonly "$BUS_DB" \
  "SELECT COALESCE(MAX(timestamp),0) FROM records
   WHERE topic='system' AND type='sdk.turn_complete' AND key='$SESSION';")
last_created_ts=$(sqlite3 -readonly "$BUS_DB" \
  "SELECT COALESCE(MAX(timestamp),0) FROM records
   WHERE topic='sessions' AND type='session.created' AND key='$CHAT_ID';")
if [ "$last_created_ts" -gt "$last_turn_ts" ]; then
  log "wedged signature but a newer session is already in flight, skipping"
  exit 0
fi

now=$(date +%s)
last=$(cat "$STATE" 2>/dev/null || echo 0)
if [ $((now - last)) -lt "$COOLDOWN_S" ]; then
  log "wedged (2 consecutive error turns) but within cooldown, skipping"
  exit 0
fi

log "wedged: last 2 turns errored — clean-restarting $SESSION"
echo "$now" > "$STATE"
"$CLI" restart-session "$SESSION" --clean >> "$LOG" 2>&1
"$CLI" inject-prompt "$SESSION" --admin "AUTOMATED RECOVERY (uplink-guard): your predecessor session died because this Mac's uplink drops API requests with large contexts (>~1.5MB body). Keep your context small: delegate web research to Task subagents returning compact summaries, never pull large pages into your own context. Read recent SMS history, reply to anything unanswered with an honest status, and finish any outstanding commitment using the old transcript (read_transcript.py) rather than redoing research." >> "$LOG" 2>&1
log "clean restart + inject done"
