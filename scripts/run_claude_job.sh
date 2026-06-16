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

"$CLAUDE" --print --dangerously-skip-permissions "$PROMPT" 2>&1 | tee -a "$LOG"
rc=${PIPESTATUS[0]}

if [ "$rc" -ne 0 ]; then
    fail "$rc" "exit code $rc"
fi

echo "run_claude_job: '${JOB}' completed ok at $(date '+%Y-%m-%d %H:%M %Z')" >> "$LOG"
