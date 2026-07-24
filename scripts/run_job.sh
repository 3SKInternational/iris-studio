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
# Exit codes THE JOB MAY RETURN — 75 is a reserved cross-job contract:
#   0            success. Clears any retry marker (pings RECOVERED if it had been
#                failing) and emits the routine ✅ unless JOB_QUIET_OK=1.
#   75           EX_TEMPFAIL — "my TARGET was unavailable, so I did NOTHING."
#                Not success, not a fault. Silent: no ping, any pending retry
#                marker is DROPPED, and launchd is handed 0. Use it for a job
#                whose target is legitimately absent sometimes — e.g. sync-to-air
#                when the Air laptop is asleep. Do NOT use a bare `exit 0` for
#                that: it is indistinguishable from real success, so it reports a
#                completion that never happened and falsely clears real failures.
#   other ≠ 0    genuine failure. Red alert + retry marker, code passed to launchd.
#
# Otherwise the job's own exit code is returned verbatim, so launchd still sees
# failures. (The FDA/EPERM branch below is a second internal 0-returning skip.)

set -uo pipefail

# Capture the full invocation BEFORE we shift off the job name — the retry queue
# replays this verbatim if the job fails.
ORIG_ARGV=("$0" "$@")

JOB="${1:?run_job: job name required}"
shift || true
if [ "$#" -eq 0 ]; then
    echo "run_job: command required after job name" >&2
    exit 64
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NOTIFY="$SCRIPT_DIR/notify.sh"
LOG="/Users/steve/iris_studio/logs/job-${JOB}.log"

# shellcheck source=/dev/null
. "$SCRIPT_DIR/retry_queue.sh"

alert() {
    # Best-effort — never let a notify failure mask the job's own exit code.
    "$NOTIFY" "$1" || true
}

mkdir -p "$(dirname "$LOG")" 2>/dev/null || true

# Serialize the job against itself so a 30-min retry replay can't overlap its own
# scheduled fire. If another instance holds the lock, exit cleanly and leave the
# retry marker for the holder to resolve. Fail-open if the lock can't be taken.
if ! rq_acquire_lock "$JOB"; then
    echo "run_job: '${JOB}' already running (lock held) — skipping this invocation" >> "$LOG"
    exit 0
fi
trap 'rq_release_lock' EXIT

TS_START="$(date '+%Y-%m-%d %H:%M %Z')"
echo "run_job: '${JOB}' starting at ${TS_START} — $*" >> "$LOG"

# /Volumes mount guard. This wrapper + the retry queue live on the EXTERNAL
# AI_Workspace volume, and many jobs (pipeline sweeps, drive-sync) operate on
# workspace paths. If the volume detaches, its mountpoint can linger as an empty
# dir — so a bare `[ -d ]` is not enough. Confirm a sentinel file inside the repo
# is present; if not, don't run the job against a phantom tree. Enqueue the 30-min
# retry so it re-runs once the volume returns. (No cd — safe if already detached.)
WORKSPACE_ROOT="${SCRIPT_DIR%/*}"
if [ ! -f "$WORKSPACE_ROOT/iris.py" ]; then
    TSW="$(date '+%Y-%m-%d %H:%M %Z')"
    echo "run_job: '${JOB}' SKIPPED — AI_Workspace volume unavailable (sentinel $WORKSPACE_ROOT/iris.py missing) at ${TSW}" >> "$LOG"
    if [ "${IRIS_RETRY:-0}" != "1" ]; then
        alert "🔴 launchd job '${JOB}' could not run — AI_Workspace volume unavailable (not mounted).
Time: ${TSW}
The 30-min auto-retry will re-run it once the volume is back."
    fi
    rq_record_failure "$JOB" "infra" "AI_Workspace volume unavailable (not mounted)" -- "${ORIG_ARGV[@]}"
    exit 1
fi

# Byte offset of LOG before this run, so the EPERM check below greps ONLY this
# invocation's output — not stale lines from an earlier run still in the file. The
# log is a fixed accumulating per-job file; a `tail -n 12` match could otherwise
# misread a prior run's pyvenv line and wrongly swallow (and drop the marker of) a
# distinct later failure that printed only a line or two.
LOG_OFF_BEFORE="$(wc -c < "$LOG" 2>/dev/null || echo 0)"
LOG_OFF_BEFORE="${LOG_OFF_BEFORE//[^0-9]/}"; LOG_OFF_BEFORE="${LOG_OFF_BEFORE:-0}"

"$@" >> "$LOG" 2>&1
rc=$?

TS="$(date '+%Y-%m-%d %H:%M %Z')"

# Known infra condition, NOT a job bug: launchd cannot exec a venv interpreter that
# lives on the external AI_Workspace volume — a claude-code cask auto-upgrade silently
# revokes Full Disk Access from the launchd context, so Python aborts at startup with
# EPERM reading pyvenv.cfg (the interpreter never even ran the script). Stay fully
# silent: no red alert, no retry marker (so no escalating "still failing" pings). The
# job self-heals on its next scheduled fire once FDA is restored. Durable fix is
# relocating the repo to an internal disk. See memory: FDA breaks on claude cask upgrade.
# Match ONLY the unambiguous FDA signature (EPERM reading pyvenv.cfg) — a genuinely
# broken/corrupted venv fails differently and still surfaces its red alert.
if [ "$rc" -ne 0 ] && tail -c "+$((LOG_OFF_BEFORE + 1))" "$LOG" 2>/dev/null \
        | grep -qE 'Operation not permitted.*pyvenv\.cfg'; then
    # Retire any pending retry marker too. The EPERM skip is silent and exits 0
    # WITHOUT bumping attempts, so a marker left over from an earlier failure would
    # never clear and never hit the give-up cap — the 30-min sweep would replay this
    # guaranteed-skip forever (the storm the 6/27 patch missed). Drop it silently;
    # the job self-heals on its next scheduled fire once FDA returns.
    rq_drop_marker "$JOB"
    echo "run_job: '${JOB}' SKIPPED — venv interpreter not execable under launchd (FDA/EPERM at Python startup); silent, retries on next schedule at ${TS}" >> "$LOG"
    exit 0
fi

# EX_TEMPFAIL (75) = "my TARGET was unavailable, so I did nothing." Distinct from
# success (0) and from failure (other non-zero): no work happened, nothing is broken.
# A job signals this itself — e.g. sync-to-air when the Air laptop is asleep, which is
# the normal case here, not a fault.
#
# Why (2026-07-23): sync-to-air used a bare `exit 0` for the asleep-Air skip, which is
# indistinguishable from real success. That fired a "✅ completed" ping for a run that
# moved zero bytes, and — the actual bug — reached rq_clear_on_success, deleting a
# pending retry marker and sending a false "RECOVERED" for a failure never re-attempted.
# It fired live at 22:46 on 2026-07-23: a real rsync-255 drop was cleared by a next run
# that had merely skipped. With the routine ✅ silenced, failure/recovery is the ONLY
# channel left, so a false all-clear there is worse than the noise it replaced.
#
# Drop any pending marker rather than keeping it: a skip exits 0 without bumping
# `attempts`, so a retained marker would never reach the give-up ceiling and the 30-min
# sweep would replay a guaranteed-skip forever — the exact storm the FDA/EPERM branch
# above documents. Dropping is silent and safe: a still-broken job re-detects and
# re-alerts on its next fire (hourly here), so at most one cycle of memory is lost.
if [ "$rc" -eq 75 ]; then
    rq_drop_marker "$JOB"
    echo "run_job: '${JOB}' SKIPPED — target unavailable, nothing done (EX_TEMPFAIL 75); silent, retries on next schedule at ${TS}" >> "$LOG"
    exit 0
fi

if [ "$rc" -ne 0 ]; then
    # Red alert only on the original fire; retries are throttled by rq_record_failure.
    if [ "${IRIS_RETRY:-0}" != "1" ]; then
        alert "🔴 launchd job '${JOB}' FAILED — exit code ${rc}.
Time: ${TS}
Log tail:
$(tail -n 4 "$LOG" 2>/dev/null || echo '(no log)')"
    fi
    # Enqueue/bump a retry marker so the 30-min sweep keeps trying until it runs.
    rq_record_failure "$JOB" "infra" "exit code ${rc}" -- "${ORIG_ARGV[@]}"
    echo "run_job: '${JOB}' FAILED (exit ${rc}) at ${TS}" >> "$LOG"
    exit "$rc"
fi

# Success: clear any retry marker (emits the RECOVERED ping if it had been
# failing). On the retry path that recovery ping is the signal, so suppress the
# routine ✅ to avoid a duplicate.
rq_clear_on_success "$JOB"
echo "run_job: '${JOB}' completed ok at ${TS}" >> "$LOG"
if [ "${JOB_QUIET_OK:-0}" != "1" ] && [ "${IRIS_RETRY:-0}" != "1" ]; then
    alert "✅ launchd job '${JOB}' completed at $(date '+%H:%M %Z')."
fi
exit 0
