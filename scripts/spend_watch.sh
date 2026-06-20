#!/usr/bin/env bash
# spend_watch.sh VIDEO — independent watchdog for a billed pipeline image spend.
#
# WHY: `pipeline_orchestrator.py --video N --spend-ok` notifies Steve on a clean
# success (💸 GENERATED) and on an in-process failure (⚠️ spend FAILED). But if
# the process is KILLED from the outside (harness stop, SIGKILL, reboot, an agent
# turn ending) it dies BEFORE reaching either notify — silently. Worse, a kill
# during the Pass-A review window leaves stage 5 still 'needs-steve' with no
# 'running' marker, so even the hourly pipeline-sweep can't tell it ever started.
# This watchdog closes that gap: it watches the PROCESS (not just state), and if
# the spend disappears without stage 5 reaching 'done', it Telegram-pings Steve.
#
# It is meant to run DETACHED from whatever launched the spend, e.g.:
#   nohup scripts/spend_watch.sh 3 >/dev/null 2>&1 & disown
# so that the same kill which takes down the spend does NOT take down the watcher.
# (NOTE: macOS has no `setsid` — use `nohup ... & disown` to detach.)
#
# Silent on success (the orchestrator already notifies). Alerts ONLY on an
# abnormal end or a stuck run. Safe to launch any time: a startup grace window
# avoids a false alarm if the spend hasn't spawned yet, and it no-ops cleanly if
# the spend already finished.
#
# Exit codes: 0 = spend completed (stage 5 done) | 1 = abnormal end (alerted)
#             2 = watchdog timed out, spend still alive (alerted)
#             3 = spend never appeared within the grace window (quiet no-op)

set -uo pipefail

VIDEO="${1:?usage: spend_watch.sh VIDEO}"
case "$VIDEO" in (*[!0-9]*|'') echo "spend_watch.sh: VIDEO must be an integer" >&2; exit 64;; esac
NN=$(printf '%02d' "$VIDEO")

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NOTIFY="$SCRIPT_DIR/notify.sh"
STATE="/Users/steve/Documents/3SK/outputs/BRANDS/3SK_Finance/Production_Kits/Video_${NN}_pipeline.json"
LOG_DIR="${HOME:-/Users/steve}/iris_studio/logs"
LOG="$LOG_DIR/spend_watch_v${NN}.log"
mkdir -p "$LOG_DIR" 2>/dev/null || true

POLL=30           # seconds between checks
GRACE=120         # startup window to let the spend process appear
# Worst-case wall time: Pass-A review can loop IMAGE_REVIEW_MAX_FIX_ATTEMPTS(2)
# times, so up to 5 review passes × IMAGE_REVIEW_TIMEOUT(900) = 4500s, THEN the
# billed generation at 5_images timeout 3600s = 8100s. Add slack → 9000s.
MAX_WAIT=9000     # review(<=4500) + gen(3600) + slack, in seconds

# Match the orchestrator spend process for THIS video, or its generate_images
# child. The trailing " --spend-ok" anchors so "--video 3" never matches "30".
# The child generate_images.py is scoped to THIS video's output dir
# (…/Raw_Assets/Video_${NN}_HD) so a concurrent OTHER-video spend can't mask
# this video's death (or be mistaken for it).
SPEND_PAT="pipeline_orchestrator\.py --video ${VIDEO} --spend-ok"
GEN_PAT="image_factory/generate_images\.py .*Video_${NN}_HD"

log() { echo "$(date '+%Y-%m-%dT%H:%M:%S%z') v${NN} $*" >>"$LOG" 2>/dev/null || true; }

stage5_status() {
  python3 - "$STATE" <<'PY' 2>/dev/null
import json, sys
try:
    print(json.load(open(sys.argv[1]))["stages"]["5_images"].get("status", ""))
except Exception:
    print("")
PY
}

spend_alive() {
  pgrep -f "$SPEND_PAT" >/dev/null 2>&1 && return 0
  pgrep -f "$GEN_PAT" >/dev/null 2>&1 && return 0
  return 1
}

alert() { "$NOTIFY" "$1" >>"$LOG" 2>&1 || log "notify FAILED: $1"; }

# On a clean finish, build + open a contact sheet of the rendered batch so the
# billed run can be eyeballed at a glance (standing instruction, 2026-06-19).
# Best-effort and $0: never let a sheet failure change the watchdog's exit path.
make_sheet() {
  local sheet="$SCRIPT_DIR/contact_sheet.py"
  [ -f "$sheet" ] || { log "contact_sheet.py not found at $sheet — skipping sheet"; return 0; }
  log "building contact sheet for video ${VIDEO}"
  # Bound the whole build (no `timeout` binary on macOS — perl's alarm stands in)
  # so a stalled PNG decode on a slow/network path can't hang the watchdog.
  perl -e 'alarm shift; exec @ARGV' 120 python3 "$sheet" "$VIDEO" --open >>"$LOG" 2>&1 \
    || log "contact_sheet.py exited non-zero or timed out (non-fatal)"
}

log "watch start (poll=${POLL}s grace=${GRACE}s max=${MAX_WAIT}s)"

# --- Startup grace: wait for the spend to appear (or already be done) ---------
waited=0
while ! spend_alive; do
  st="$(stage5_status)"
  if [ "$st" = "done" ]; then
    log "stage5 already done at startup — nothing to watch"; exit 0
  fi
  if [ "$waited" -ge "$GRACE" ]; then
    log "no spend process within ${GRACE}s grace (stage5='${st}') — quiet no-op"; exit 3
  fi
  sleep "$POLL"; waited=$((waited + POLL))
done
log "spend process detected — entering watch loop"

# --- Main watch loop ----------------------------------------------------------
elapsed=0
while true; do
  st="$(stage5_status)"
  if [ "$st" = "done" ]; then
    log "stage5 reached done — spend completed (orchestrator already notified)"
    make_sheet; exit 0
  fi
  if ! spend_alive; then
    # Re-read state: the process may have written stage5=done and exited in the
    # window between the read above and this check — don't false-alarm on a clean
    # finish. Only a non-done status after the process is truly gone is abnormal.
    st="$(stage5_status)"
    if [ "$st" = "done" ]; then
      log "spend process exited with stage5=done — clean finish"
      make_sheet; exit 0
    fi
    log "spend process GONE with stage5='${st}' — abnormal end"
    alert "⚠️ Video ${VIDEO}: image spend STOPPED without completing (stage 5 = '${st}'). Killed/crashed mid-run — \$0 or a partial batch, nothing assembled. Re-authorize when ready: /pipeline ${VIDEO} spend-ok"
    exit 1
  fi
  if [ "$elapsed" -ge "$MAX_WAIT" ]; then
    log "watchdog timeout after ${MAX_WAIT}s, spend still alive (stage5='${st}')"
    alert "⚠️ Video ${VIDEO}: image spend watchdog hit ${MAX_WAIT}s and the process is still running (stage 5 = '${st}'). It may be stuck — check it: /pipeline ${VIDEO} status"
    exit 2
  fi
  sleep "$POLL"; elapsed=$((elapsed + POLL))
done
