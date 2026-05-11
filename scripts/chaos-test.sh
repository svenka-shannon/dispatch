#!/bin/bash
# ────────────────────────────────────────────────────────────────────────────
# chaos-test.sh — run the chaos / resilience smoke suite (Phase 5 of
#                 plans/self-healing-resilience.md)
#
# ⚠️  THIS DELIBERATELY BREAKS LIVE COMPONENTS ON *THIS* MACHINE:
#       - kills Chrome's native_host.py (simulates an evicted MV3 service worker)
#       - with --yes-i-mean-it: ALSO quits Chrome.app, kills the signal-cli
#         daemon, and pokes the bus consumer
#     …then asserts the self-healing loops recover within the §3 SLOs.
#
# ONLY run this when:
#   1. the daemon was restarted *after* the Phase 1/2 resilience changes
#      (`claude-assistant restart`) — otherwise the new loops aren't live and
#      the live tests will (correctly) time out;
#   2. chrome-control is in a known-good state (`chrome reload-extension`);
#   3. NO chat session is mid-task — these kill Chrome / signal-cli / the
#      consumer out from under whatever's using them;
#   4. you can babysit it (a full destructive run takes several minutes and
#      may need a manual chrome://extensions reload at the end).
#
# Usage:
#   scripts/chaos-test.sh                       # mocked/structural tests only (safe)
#   scripts/chaos-test.sh --live                # + the moderately-disruptive live
#                                               #   tests (native_host kill, etc.)
#   scripts/chaos-test.sh --yes-i-mean-it       # + the DESTRUCTIVE tests
#                                               #   (quit Chrome, kill signal-cli,
#                                               #    poke the bus consumer)
#   scripts/chaos-test.sh --live -k native_host # pass a pytest -k filter through
#
# Env it sets:
#   CLAUDE_LIVE_TESTS=1            (for --live / --yes-i-mean-it)
#   CLAUDE_CHAOS_DESTRUCTIVE=1     (for --yes-i-mean-it only)
# ────────────────────────────────────────────────────────────────────────────
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

MODE="mocked"          # mocked | live | destructive
PYTEST_EXTRA=()        # extra args (e.g. -k filter) passed through to pytest

while [[ $# -gt 0 ]]; do
  case "$1" in
    --live)            MODE="live"; shift ;;
    --yes-i-mean-it)   MODE="destructive"; shift ;;
    --mocked|--safe)   MODE="mocked"; shift ;;
    -h|--help)
      sed -n '2,40p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *)                 PYTEST_EXTRA+=("$1"); shift ;;
  esac
done

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
red()  { printf '\033[1;31m%s\033[0m\n' "$*"; }
yel()  { printf '\033[1;33m%s\033[0m\n' "$*"; }

echo
red  "════════════════════════════════════════════════════════════════════"
red  "  CHAOS / RESILIENCE TEST SUITE  —  plans/self-healing-resilience.md"
red  "════════════════════════════════════════════════════════════════════"
case "$MODE" in
  mocked)
    bold "Mode: MOCKED ONLY (safe — nothing on this machine is touched)."
    ;;
  live)
    yel  "Mode: LIVE — this will KILL Chrome's native_host.py on THIS machine"
    yel  "      (simulates an evicted service worker) and assert chrome-control"
    yel  "      self-heals. Moderately disruptive; does NOT quit Chrome itself."
    ;;
  destructive)
    red  "Mode: DESTRUCTIVE — this will, on THIS machine:"
    red  "        • kill Chrome's native_host.py"
    red  "        • QUIT Google Chrome.app (then relaunch it)"
    red  "        • KILL the signal-cli daemon (then expect it to restart)"
    red  "        • inspect/poke the bus consumer registry"
    red  "      It may end with the chrome-control extension needing a MANUAL"
    red  "      reload at chrome://extensions/. Only run this with NO chat"
    red  "      session mid-task and someone watching it."
    ;;
esac
echo

if [[ "$MODE" != "mocked" ]]; then
  if [[ -t 0 ]]; then
    printf "Type 'chaos' to proceed: "
    read -r ans
    if [[ "$ans" != "chaos" ]]; then
      echo "Aborted."
      exit 1
    fi
  else
    echo "Non-interactive shell; pass --yes-i-mean-it explicitly was already required for destructive mode."
    if [[ "$MODE" == "destructive" ]]; then
      : # --yes-i-mean-it was the gate; proceed
    else
      echo "Refusing to run live tests non-interactively without a TTY confirmation."
      exit 1
    fi
  fi
fi

# Build the env.
EXPORTS=()
if [[ "$MODE" == "live" || "$MODE" == "destructive" ]]; then
  export CLAUDE_LIVE_TESTS=1
  EXPORTS+=("CLAUDE_LIVE_TESTS=1")
fi
if [[ "$MODE" == "destructive" ]]; then
  export CLAUDE_CHAOS_DESTRUCTIVE=1
  EXPORTS+=("CLAUDE_CHAOS_DESTRUCTIVE=1")
fi
[[ ${#EXPORTS[@]} -gt 0 ]] && bold "env: ${EXPORTS[*]}"

bold "Running: uv run --group dev pytest tests/chaos -v -m chaos ${PYTEST_EXTRA[*]:-}"
echo
exec uv run --group dev pytest tests/chaos -v -m chaos ${PYTEST_EXTRA[@]+"${PYTEST_EXTRA[@]}"}
