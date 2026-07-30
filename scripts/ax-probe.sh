#!/bin/bash
# ax-probe — verify the daemon's context has Accessibility (AX) permission and
# can see web content in Chrome's AX tree. Used to validate the chrome-heal
# AX-reload path. Writes results to /tmp/ax-probe-result.txt.
OUT=/tmp/ax-probe-result.txt
{
  echo "=== ax-probe $(date '+%Y-%m-%d %H:%M:%S') ==="
  echo "--- whoami/context ---"
  echo "pid=$$ ppid=$PPID"
  ps -o command= -p $PPID 2>/dev/null | head -1
  echo "--- AXIsProcessTrusted ---"
  cd "$HOME/dispatch" && uv run python - <<'EOF'
from ApplicationServices import AXIsProcessTrusted, AXUIElementCreateApplication, AXUIElementSetAttributeValue, AXUIElementCopyAttributeValue
import subprocess
print("AXIsProcessTrusted:", AXIsProcessTrusted())
try:
    pid = int(subprocess.check_output(["pgrep", "-x", "Google Chrome"]).split()[0])
    app = AXUIElementCreateApplication(pid)
    err = AXUIElementSetAttributeValue(app, "AXManualAccessibility", True)
    print("AXManualAccessibility set err:", err, "(0 = success)")
    err, wins = AXUIElementCopyAttributeValue(app, "AXWindows", None)
    print("AXWindows read err:", err, "| count:", len(wins) if wins else wins)
except Exception as e:
    print("chrome AX probe failed:", e)
EOF
} > "$OUT" 2>&1
