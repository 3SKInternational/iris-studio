#!/usr/bin/env bash
# pulse_audit.sh — Cowork "Nightly Value Pulse" miss-rate auditor.
#
# WHY: The nightly pulse runs on Cowork's APP-OPEN scheduler (not launchd, because
# launchd can't drive the Claude.app GUI). We're measuring how often it MISSES over
# a window before deciding whether to rebuild it as a launchd Claude Code routine.
#
# GROUND TRUTH: a pulse that fired leaves a digest at
#   _Iris_Memory/Sessions/YYYY-MM-DD_<HHMM>_Nightly_Value_Pulse.md
# Absence of that file for a date = a MISS. (The daily-note section is unreliable —
# false negatives — so we key off the digest, not the note.)
#
# WHY IT MISSED: cross-reference the watchdog log
#   ~/iris_studio/logs/cowork-watchdog.log
# which records OPEN/CLOSED of the Claude.app at the ~03:05 / ~04:10 windows. A miss
# with a CLOSED window = app-was-closed (expected). A miss with an OPEN window =
# app-was-open-but-pulse-didn't-run (a real reliability problem, the interesting case).
#
# Usage:
#   pulse_audit.sh [--days N] [--notify] [--quiet-ok]
#     --days N     window size in days, ending today (default 7)
#     --notify     send the summary to Steve's Telegram via notify.sh
#     --quiet-ok   with --notify, only send if miss-rate > 0 (for scheduled runs)
#
# Exit: always 0 (a health check must never wedge a scheduled job).

set -uo pipefail

# --- config ---------------------------------------------------------------
VAULT="/Users/steve/Documents/3SK/outputs"
SESSIONS_DIR="$VAULT/_Iris_Memory/Sessions"
WATCHDOG_LOG="/Users/steve/iris_studio/logs/cowork-watchdog.log"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NOTIFY="$SCRIPT_DIR/notify.sh"

DAYS=7
DO_NOTIFY=0
QUIET_OK=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --days)
      # NOTE: never use `shift 2` here. On bash 3.2 (macOS system bash), `shift 2`
      # with only one positional left fails and shifts NOTHING, re-entering this
      # branch forever — an infinite loop that would wedge the scheduled job.
      if [[ -n "${2:-}" && "${2}" != --* ]]; then
        DAYS="$2"; shift; shift
      else
        echo "pulse_audit: --days needs a value, using 7" >&2; DAYS=7; shift
      fi ;;
    --days=*)   DAYS="${1#*=}"; shift ;;
    --notify)   DO_NOTIFY=1; shift ;;
    --quiet-ok) QUIET_OK=1; shift ;;
    *) echo "pulse_audit: unknown arg: $1" >&2; shift ;;
  esac
done

# validate DAYS is a positive integer
if ! [[ "$DAYS" =~ ^[0-9]+$ ]] || [[ "$DAYS" -lt 1 ]]; then
  echo "pulse_audit: --days must be a positive integer (got '$DAYS'), using 7" >&2
  DAYS=7
fi

# --- portable 'date N days ago' (BSD/macOS first, GNU fallback) ------------
date_n_days_ago() {
  local n="$1"
  if date -v-1d +%Y-%m-%d >/dev/null 2>&1; then
    date -v-"${n}"d +%Y-%m-%d            # BSD/macOS
  else
    date -d "-${n} days" +%Y-%m-%d       # GNU/Linux
  fi
}

# --- scan the window ------------------------------------------------------
FIRED=0
MISSED=0
MISS_DATES=()
TABLE=""

for ((i = DAYS - 1; i >= 0; i--)); do
  d="$(date_n_days_ago "$i")"

  # digest present? (any time-prefix variant for that date)
  digest=""
  if compgen -G "$SESSIONS_DIR/${d}_*Nightly_Value_Pulse.md" >/dev/null 2>&1; then
    digest="$(compgen -G "$SESSIONS_DIR/${d}_*Nightly_Value_Pulse.md" | head -1)"
  fi

  # watchdog state for the PULSE window specifically (the ~03:05 nightly-pulse line).
  # Must anchor to "nightly-pulse" — each date also has a ~04:10 vault-gardener line,
  # and if we matched the whole date the gardener window could mislabel a pulse-window
  # miss (app CLOSED at 03:05 but OPEN at 04:10) as a false reliability issue.
  wd="?"
  if [[ -f "$WATCHDOG_LOG" ]]; then
    if grep -q "^${d} .*nightly-pulse.*OPEN" "$WATCHDOG_LOG" 2>/dev/null; then
      wd="app-OPEN"
    elif grep -q "^${d} .*nightly-pulse.*CLOSED" "$WATCHDOG_LOG" 2>/dev/null; then
      wd="app-CLOSED"
    fi
  fi

  if [[ -n "$digest" ]]; then
    FIRED=$((FIRED + 1))
    TABLE+="  $d   FIRED    $(basename "$digest")"$'\n'
  else
    MISSED=$((MISSED + 1))
    MISS_DATES+=("$d")
    # annotate why
    case "$wd" in
      app-CLOSED) why="app was CLOSED (expected miss)" ;;
      app-OPEN)   why="app was OPEN — pulse did not run (RELIABILITY ISSUE)" ;;
      *)          why="no watchdog data for this date" ;;
    esac
    TABLE+="  $d   MISSED   ${why}"$'\n'
  fi
done

# --- miss rate ------------------------------------------------------------
if [[ "$DAYS" -gt 0 ]]; then
  RATE=$(awk "BEGIN { printf \"%.0f\", ($MISSED/$DAYS)*100 }")
else
  RATE=0
fi

START="$(date_n_days_ago $((DAYS - 1)))"
END="$(date_n_days_ago 0)"

# --- report ---------------------------------------------------------------
REPORT="Cowork Nightly Value Pulse — audit ${START} → ${END} (${DAYS}d)
Fired: ${FIRED}/${DAYS}   Missed: ${MISSED}/${DAYS}   Miss-rate: ${RATE}%
"
if [[ "$MISSED" -gt 0 ]]; then
  REPORT+="Misses: ${MISS_DATES[*]}"$'\n'
fi
REPORT+=$'\n'"$TABLE"

echo "$REPORT"

# --- optional Telegram push ----------------------------------------------
if [[ "$DO_NOTIFY" -eq 1 ]]; then
  if [[ "$QUIET_OK" -eq 1 && "$MISSED" -eq 0 ]]; then
    : # clean week, stay silent on scheduled runs
  elif [[ -x "$NOTIFY" ]]; then
    msg="📊 Pulse audit ${START}→${END}: fired ${FIRED}/${DAYS}, miss-rate ${RATE}%."
    if [[ "$MISSED" -gt 0 ]]; then
      msg+=$'\n'"Missed: ${MISS_DATES[*]}"
    fi
    "$NOTIFY" "$msg" >/dev/null 2>&1 || echo "pulse_audit: notify failed" >&2
  else
    echo "pulse_audit: notify.sh not executable at $NOTIFY" >&2
  fi
fi

exit 0
