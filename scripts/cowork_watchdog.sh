#!/bin/bash
# cowork_watchdog.sh — prevention layer for Cowork's app-open-only scheduled jobs.
#
# Why this exists: Cowork-Iris's overnight jobs (nightly value pulse ~03:05 ET,
# vault gardener ~04:10 ET) only fire while the Cowork desktop app (Claude.app)
# is open on the Mini. When it's closed, those fires queue silently and the
# artifacts go stale — witnessed misses on 6/7, 6/8, 6/14. The pre-brief Pass 16
# DETECTS this, but only at 05:00, after the fact. This watchdog runs AT each
# Cowork window and, if the app is not running, fires an immediate Telegram
# alert via notify.sh — so a closed app surfaces in seconds, not hours later.
#
# Read-only: it checks process liveness and sends an alert. It never launches,
# closes, or restarts anything (we can't relaunch a GUI app from launchd
# reliably, and force-launching could fight Steve). The alert tells Steve to
# reopen Cowork himself.
#
# Usage:  cowork_watchdog.sh [window-label]
#   With no arg the window is derived from the current hour (so one launchd job
#   with two fire times — 03:05 + 04:10 ET — labels each alert correctly):
#     hour 03 -> "nightly-pulse (~03:05 ET)"
#     hour 04 -> "vault-gardener (~04:10 ET)"
#     other   -> "off-window manual check"
#   An explicit arg overrides the derivation (handy for manual testing).
#
# Exit 0 always (a watchdog that fails launchd is its own silent failure); any
# real problem goes to Telegram, not to launchd's exit code.

set -uo pipefail

if [ "$#" -ge 1 ] && [ -n "$1" ]; then
    WINDOW="$1"
else
    case "$(date '+%H')" in
        03) WINDOW="nightly-pulse (~03:05 ET)" ;;
        04) WINDOW="vault-gardener (~04:10 ET)" ;;
        *)  WINDOW="off-window manual check" ;;
    esac
fi
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NOTIFY="$SCRIPT_DIR/notify.sh"
LOG="/Users/steve/iris_studio/logs/cowork-watchdog.log"
TS="$(date '+%Y-%m-%d %H:%M %Z')"

# Dedupe: the app being closed overnight is a NORMAL state (Steve's asleep), and
# this job fires twice (03:05 + 04:10). Without dedupe a closed app would push
# two Telegram alerts every night forever → alert fatigue → the channel gets
# ignored the night it matters. So we alert at most ONCE per calendar day, on
# the first closed detection, via a per-day marker. The 04:10 fire stays silent
# if 03:05 already alerted. Detecting the app OPEN clears the day's marker, so a
# genuine close→reopen→close within one night re-alerts as a fresh transition.
STATE_DIR="/Users/steve/iris_studio/state"
mkdir -p "$STATE_DIR" 2>/dev/null || true
MARKER="$STATE_DIR/.cowork-closed-$(date +%Y%m%d)"
# Self-clean markers older than a week so the state dir doesn't accumulate.
find "$STATE_DIR" -name '.cowork-closed-*' -mtime +7 -delete 2>/dev/null || true

# Match the Cowork desktop app's MAIN process. It runs as
# .../Claude.app/Contents/MacOS/Claude (translocated or from /Applications) —
# anchor on that exact tail so we don't match the claude-code CLI, the
# remote-control daemon, or a "Claude Helper" renderer/gpu subprocess (those can
# outlive the main app and would mask a closed app).
#
# NB: macOS `pgrep -f` silently fails to match patterns containing the literal
# "Claude.app", so we scan `ps` command lines with an anchored grep instead —
# verified reliable on this Mac (excludes the 7 Helper subprocesses).
#
# Capture `ps` into a var and grep a here-string rather than piping: under
# `set -o pipefail`, `grep -q` exits on first match and SIGPIPEs the still-
# writing `ps`, which (racily) makes the pipeline report failure even on a
# match — an intermittent FALSE "app closed" alert. The here-string has no
# upstream process to kill, so the result is deterministic.
PROCS="$(ps -axww -o command= 2>/dev/null)"
if grep -qE '/Claude\.app/Contents/MacOS/Claude$' <<<"$PROCS"; then
    echo "$TS  [$WINDOW] Cowork app is OPEN — ok" >> "$LOG"
    rm -f "$MARKER"   # reset the day's dedupe so a later close re-alerts
    exit 0
fi

# App is closed. Alert only if we haven't already alerted today.
if [ -f "$MARKER" ]; then
    echo "$TS  [$WINDOW] Cowork app CLOSED — already alerted today, staying silent" >> "$LOG"
    exit 0
fi
: > "$MARKER"
echo "$TS  [$WINDOW] Cowork app is CLOSED — alerting Steve (first today)" >> "$LOG"
"$NOTIFY" "⚠️ Cowork app is CLOSED on the Mini ($WINDOW window, ${TS}).
Its scheduled job will NOT fire while the app is closed — reopen Claude/Cowork on the Mini and leave it open. (launchd-driven jobs are unaffected.)" || true

exit 0
