#!/bin/bash
# run_job.sh — run a scheduled NON-claude infra job (a plain shell script or
# command), log it, and alert Steve's Telegram (via notify.sh) on failure AND
# on completion.
#
# Why this exists: the iris.py daemon and the claude-code routines already alert
# (crash handler + run_claude_job.sh). The remaining launchd jobs — db-backup,
# log-rotate, drive-sync, sync-to-air — were silent: a failed nightly backup or
# Drive sync left no signal anywhere. Per Steve's 2026-06-15 directive ("warn me
# through the daemon for any/all failures AND completions of the automation
# process"), this wrapper runs the job, recovers its real exit code, and pings
# Telegram either way. These jobs run daily/weekly, so a per-run ✅ is low-noise.
#
# Unlike run_claude_job.sh there is NO completion sentinel: these are ordinary
# scripts with meaningful exit codes (0 = ok, non-zero = failed), so the exit
# code alone is authoritative.
#
# Usage:  run_job.sh <job-name> <command> [args...]
#   <job-name>  short id — names the log file + appears in the alert
#   <command>…  the program to run and its arguments
#
# Env:
#   JOB_QUIET_OK=1   suppress the ✅ success ping for this job (failures still
#                    alert). A per-job noise knob — set it in the job's plist
#                    EnvironmentVariables if a daily ✅ becomes noise.
#
# Exit code is the job's own exit code (so launchd still sees failures).

set -uo pipefail

JOB="${1:?run_job: job name required}"
shift || true
if [ "$#" -eq 0 ]; then
    echo "run_job: command required after job name" >&2
    exit 64
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NOTIFY="$SCRIPT_DIR/notify.sh"
LOG="/Users/steve/iris_studio/logs/job-${JOB}.log"

alert() {
    # Best-effort — never let a notify failure mask the job's own exit code.
    "$NOTIFY" "$1" || true
}

mkdir -p "$(dirname "$LOG")" 2>/dev/null || true

TS_START="$(date '+%Y-%m-%d %H:%M %Z')"
echo "run_job: '${JOB}' starting at ${TS_START} — $*" >> "$LOG"

"$@" >> "$LOG" 2>&1
rc=$?

TS="$(date '+%Y-%m-%d %H:%M %Z')"

if [ "$rc" -ne 0 ]; then
    alert "🔴 launchd job '${JOB}' FAILED — exit code ${rc}.
Time: ${TS}
Log tail:
$(tail -n 4 "$LOG" 2>/dev/null || echo '(no log)')"
    echo "run_job: '${JOB}' FAILED (exit ${rc}) at ${TS}" >> "$LOG"
    exit "$rc"
fi

echo "run_job: '${JOB}' completed ok at ${TS}" >> "$LOG"
if [ "${JOB_QUIET_OK:-0}" != "1" ]; then
    alert "✅ launchd job '${JOB}' completed at $(date '+%H:%M %Z')."
fi
exit 0
