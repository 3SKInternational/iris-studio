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

alert() {
    # Best-effort — never let a notify failure mask the job's own exit code.
    "$NOTIFY" "$1" || true
}

fail() {
    local rc="$1" reason="$2"
    alert "🔴 launchd job '${JOB}' FAILED — ${reason}
Time: $(date '+%Y-%m-%d %H:%M %Z')
Log tail:
$(tail -n 4 "$LOG" 2>/dev/null || echo '(no log)')"
    exit "$rc"
}

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
trap 'rm -f "$RUN_OUT"' EXIT

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
        REASON="$(printf '%s' "$LAST_LINE" | sed 's/^ROUTINE_INCOMPLETE:*[[:space:]]*//')"
        alert "⚠️ launchd job '${JOB}' ran but is NOT fully done.
Time: ${TS}
Reason: ${REASON:-(none given)}"
        echo "run_claude_job: '${JOB}' INCOMPLETE at ${TS}" >> "$LOG"
        ;;
    ROUTINE_COMPLETE*)
        alert "✅ launchd job '${JOB}' completed at $(date '+%H:%M %Z')."
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
            alert "🔴 launchd job '${JOB}' FAILED — Claude CLI auth/credits rejected.
FIX: run \`claude login\` in a Terminal on the Mini, then re-run the job. (A 401 = stale OAuth token; re-auth clears it. If re-auth doesn't help, you may be genuinely out of Max credits — wait for the window reset.)
This blocks EVERY overnight claude job until cleared.
Time: ${TS}
Detail: ${DETAIL}"
            echo "run_claude_job: '${JOB}' AUTH/CREDIT FAILURE at ${TS} — needs \`claude login\` on the Mini" >> "$LOG"
            exit 1
        fi
        alert "⚠️ launchd job '${JOB}' finished (exit 0) but emitted no completion signal — may not be fully done. Check the log."
        echo "run_claude_job: '${JOB}' completed WITHOUT sentinel at ${TS}" >> "$LOG"
        ;;
esac
