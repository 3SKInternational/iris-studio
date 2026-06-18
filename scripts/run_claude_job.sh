#!/bin/bash
# run_claude_job.sh — run a scheduled Claude Code routine, log it, and alert
# Steve's Telegram (via notify.sh) if it fails.
#
# Why this exists: every claude-code launchd job used to be
#   cd vault && claude --print "<prompt>" 2>&1 | tee -a LOG
# The pipe to tee swallowed claude's exit code, so a failed overnight routine
# left no signal anywhere — it just silently didn't happen. This wrapper runs
# the same command, recovers the real exit code via PIPESTATUS, and pings
# Telegram on any non-zero exit (or a missing prompt file). Per Steve's
# 2026-06-15 directive: warn me through the daemon for any/all failures.
#
# Usage:  run_claude_job.sh <job-name> <prompt-file>
#   <job-name>    short id — names the log file + appears in the alert
#   <prompt-file> path to the routine .prompt whose contents are the prompt
#
# Exit code is the routine's own exit code (so launchd still sees failures).

set -uo pipefail

JOB="${1:?run_claude_job: job name required}"
PROMPT_FILE="${2:?run_claude_job: prompt file required}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NOTIFY="$SCRIPT_DIR/notify.sh"
LOG="/Users/steve/iris_studio/logs/claude-code-${JOB}.log"
VAULT="/Users/steve/Documents/3SK/outputs"
CLAUDE="${CLAUDE_BIN:-/opt/homebrew/bin/claude}"  # CLAUDE_BIN overridable for self-tests

# The exact command to replay if this run fails — captured before anything can
# mutate the positional args. The retry queue (sourced below) re-runs this verbatim.
ORIG_ARGV=("$0" "$@")
# shellcheck source=/dev/null
. "$SCRIPT_DIR/retry_queue.sh"

alert() {
    # Best-effort — never let a notify failure mask the job's own exit code.
    "$NOTIFY" "$1" || true
}

fail() {
    local rc="$1" reason="$2"
    # On the ORIGINAL scheduled fire, send the red failure alert as before. On a
    # retry attempt (IRIS_RETRY=1) stay silent here — rq_record_failure owns the
    # throttled "still failing" pings so we don't spam Telegram every 30 minutes.
    if [ "${IRIS_RETRY:-0}" != "1" ]; then
        alert "🔴 launchd job '${JOB}' FAILED — ${reason}
Time: $(date '+%Y-%m-%d %H:%M %Z')
Log tail:
$(tail -n 4 "$LOG" 2>/dev/null || echo '(no log)')"
    fi
    # Enqueue (or bump) a retry marker so the 30-min sweep keeps trying until it runs.
    rq_record_failure "$JOB" "claude" "$reason" -- "${ORIG_ARGV[@]}"
    exit "$rc"
}

# Serialize this job against itself: if its own scheduled fire and a retry-sweep
# replay coincide (plausible during a multi-hour auth outage), only one runs. If
# another instance holds the lock, exit cleanly WITHOUT touching the retry marker
# (the holder will clear or re-stamp it). Fail-open: if the lock can't be taken
# for any infrastructural reason, proceed anyway — a missed run is worse.
if ! rq_acquire_lock "$JOB"; then
    echo "run_claude_job: '${JOB}' already running (lock held) — skipping this invocation" >> "$LOG"
    exit 0
fi
trap 'rm -f "${RUN_OUT:-}"; rq_release_lock' EXIT

if [ ! -f "$PROMPT_FILE" ]; then
    echo "run_claude_job: prompt file missing: $PROMPT_FILE" | tee -a "$LOG"
    fail 1 "prompt file missing: $PROMPT_FILE"
fi

if [ ! -d "$VAULT" ]; then
    echo "run_claude_job: vault dir missing: $VAULT" | tee -a "$LOG"
    fail 1 "vault dir missing: $VAULT"
fi

PROMPT="$(cat "$PROMPT_FILE")"
cd "$VAULT"

# Capture THIS run's output in isolation (RUN_OUT) while still appending to the
# cumulative LOG, so we can read just this run's last line for the completion
# sentinel each routine prompt is instructed to emit.
RUN_OUT="$(mktemp -t "claude-job-${JOB}.XXXXXX")"
trap 'rm -f "$RUN_OUT"; rq_release_lock' EXIT

"$CLAUDE" --print --dangerously-skip-permissions "$PROMPT" 2>&1 | tee -a "$LOG" | tee "$RUN_OUT" >/dev/null
rc=${PIPESTATUS[0]}

# 1) Process died / non-zero exit → hard failure.
if [ "$rc" -ne 0 ]; then
    fail "$rc" "exit code $rc (process died / non-zero)"
fi

# 2) Exit 0 — classify how it finished from the sentinel on the last non-empty line.
#    ROUTINE_COMPLETE → fully done; ROUTINE_INCOMPLETE: <reason> → partial;
#    neither → ran but completion unconfirmed (treat as not-fully-done).
# Normalize first: strip CR (CRLF output) and ANSI color codes, then trim
# surrounding whitespace, so the sentinel match is exact and the reason is clean.
# NOTE: this script must NOT use `set -e` — the grep below returns 1 on no-match
# (empty output), which under -e would silently abort and skip the alert.
LAST_LINE="$(grep -vE '^[[:space:]]*$' "$RUN_OUT" | tail -1 \
    | tr -d '\r' \
    | sed -e $'s/\033\\[[0-9;]*m//g' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
TS="$(date '+%Y-%m-%d %H:%M %Z')"

# Anchor the sentinel to the START of the (normalized) last line so prose that
# merely mentions the token (e.g. "I will not print ROUTINE_COMPLETE") can't
# trigger a false ✅. INCOMPLETE is checked first because it is a prefix of
# COMPLETE's substring — ordering is load-bearing.
case "$LAST_LINE" in
    ROUTINE_INCOMPLETE*)
        # The agent DID run (exit 0) — the "couldn't run" condition is resolved,
        # so clear any retry marker (recovery ping if it had been failing). The
        # ⚠️ below still tells Steve it wasn't fully done.
        rq_clear_on_success "$JOB"
        REASON="$(printf '%s' "$LAST_LINE" | sed 's/^ROUTINE_INCOMPLETE:*[[:space:]]*//')"
        alert "⚠️ launchd job '${JOB}' ran but is NOT fully done.
Time: ${TS}
Reason: ${REASON:-(none given)}"
        echo "run_claude_job: '${JOB}' INCOMPLETE at ${TS}" >> "$LOG"
        ;;
    ROUTINE_COMPLETE*)
        # Clear any retry marker; if this was a recovery, rq_clear_on_success
        # emits the RECOVERED ping, so suppress the duplicate routine ✅ on the
        # retry path.
        rq_clear_on_success "$JOB"
        if [ "${IRIS_RETRY:-0}" != "1" ]; then
            alert "✅ launchd job '${JOB}' completed at $(date '+%H:%M %Z')."
        fi
        echo "run_claude_job: '${JOB}' completed ok at ${TS}" >> "$LOG"
        ;;
    *)
        # No sentinel. Before the generic warning, disambiguate the one cause
        # that's both common and silently misrouted here: a Claude CLI auth /
        # credit failure. When the CLI can't authenticate it prints its error
        # (e.g. "Failed to authenticate. API Error: 401 ..." / "out of usage
        # credits") and exits *0* — the agent never runs, so there is never a
        # sentinel, which is *why* this branch is the only place it can land.
        # Gating on "no sentinel" (rather than scanning all output) means a
        # routine that merely WROTE about credits/auth — e.g. the book chapter on
        # the two-quota model — can't false-alarm: it still emits its sentinel
        # above and never reaches here. This wrapper is the shared choke point
        # for every claude --print launchd job, so this one guard hardens the
        # whole suite. (Root-caused 2026-06-17 after book-update + nightly
        # silently died on a stale CLI login for days.)
        if grep -qiE 'Failed to authenticate|API Error: 40[0-9]|out of usage credits' "$RUN_OUT"; then
            DETAIL="$(grep -iE 'Failed to authenticate|API Error: 40[0-9]|out of usage credits|Invalid authentication' "$RUN_OUT" | head -2 | tr '\n' ' ' | sed -e $'s/\033\\[[0-9;]*m//g' -e 's/[[:space:]]\\+/ /g' -e 's/[[:space:]]*$//')"
            # This is THE canonical "couldn't run" case — enqueue it for the 30-min
            # sweep so the moment Steve re-auths (or the credit window resets) the
            # routine runs itself instead of waiting for tomorrow. Red alert only on
            # the original fire; retries stay throttled.
            if [ "${IRIS_RETRY:-0}" != "1" ]; then
                alert "🔴 launchd job '${JOB}' FAILED — Claude CLI auth/credits rejected.
FIX: run \`claude login\` in a Terminal on the Mini. (A 401 = stale OAuth token; re-auth clears it. If re-auth doesn't help, you may be genuinely out of Max credits — wait for the window reset.) Once cleared, the auto-retry will run this job within 30 min — no need to re-run it by hand.
This blocks EVERY overnight claude job until cleared.
Time: ${TS}
Detail: ${DETAIL}"
            fi
            rq_record_failure "$JOB" "claude" "Claude CLI auth/credits rejected" -- "${ORIG_ARGV[@]}"
            echo "run_claude_job: '${JOB}' AUTH/CREDIT FAILURE at ${TS} — needs \`claude login\` on the Mini" >> "$LOG"
            exit 1
        fi
        # Ran (exit 0) but no sentinel — the agent executed, so the "couldn't run"
        # condition is resolved: clear any retry marker, then warn as before.
        rq_clear_on_success "$JOB"
        alert "⚠️ launchd job '${JOB}' finished (exit 0) but emitted no completion signal — may not be fully done. Check the log."
        echo "run_claude_job: '${JOB}' completed WITHOUT sentinel at ${TS}" >> "$LOG"
        ;;
esac
