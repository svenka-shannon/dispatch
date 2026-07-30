#!/bin/bash
# Click the macOS "Click to Call" notification banner's accept button.
# Must run under the daemon (launchd context) where AX is granted.
AX="$HOME/.claude/skills/axctl/scripts/axctl"
LOG=/tmp/phone-call/nc-click.log
mkdir -p /tmp/phone-call
{
  echo "=== $(date) ==="
  echo "--- buttons in Notification Center ---"
  "$AX" search "Notification Center" --role AXButton 2>&1
  echo "--- trying clicks ---"
  for title in "Call" "Answer" "Click to Call" "Accept"; do
    if "$AX" click "Notification Center" --title "$title" 2>&1; then
      echo "clicked: $title"
      exit 0
    fi
  done
  echo "--- title clicks failed; trying AXPress on all banner buttons ---"
  "$AX" tree "Notification Center" --refs --list-actions 2>&1
} >>"$LOG" 2>&1
