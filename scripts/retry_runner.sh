#!/bin/bash
# retry_runner.sh — sweep the retry queue and replay every failed automation.
#
# Fired every 30 minutes by com.iris.claude-code-retry. For each marker that
# run_claude_job.sh / run_job.sh left in ~/iris_studio/retry, this decodes the
# saved argv and re-runs the job THROUGH ITS OWN WRAPPER with IRIS_RETRY=1. The
# wrapper does all the real work: it re-acquires the per-job lock (so this can't
# collide with the job's own scheduled fire), re-attempts the routine/command,
# and on success clears the marker + pings "RECOVERED" — or on another failure
# re-stamps the marker (throttled alerting) for the next sweep to pick up.
#
# This runner is deliberately dumb: it owns NO retry policy, only dispatch. When
# the queue is empty it is a near-instant no-op, so a fixed 30-min cadence is
# cheap. Replays run SEQUENTIALLY — if a stale-auth outage queued every overnight
# job, recovering them one at a time avoids a thundering-herd of heavy routines
# (and concurrent Claude credit burn) the moment auth is restored.
#
# Portability: macOS /bin/bash 3.2 — no mapfile; argv is read NUL-delimited.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RQ_BASE="${RQ_BASE:-$HOME/iris_studio/retry}"
LOG="${RETRY_RUNNER_LOG:-$HOME/iris_studio/logs/claude-code-retry.log}"
RUN_LOCK="$RQ_BASE/.runner.lock"

mkdir -p "$RQ_BASE" "$(dirname "$LOG")" 2>/dev/null || true

# Only one sweep at a time. If the previous sweep is somehow still running
# (a recovered heavy routine took >30 min), skip this tick — atomic mkdir lock.
# The sweep touches RUN_LOCK between jobs, so a long-but-LIVE recovery batch keeps
# its mtime fresh; only a genuinely dead lock (>6h untouched) gets stolen. This is
# well above the realistic worst case of replaying the whole job suite back-to-back
# after an auth outage, so a live batch is never mistaken for stale.
if [ -d "$RUN_LOCK" ] && [ -n "$(find "$RUN_LOCK" -maxdepth 0 -mmin +360 2>/dev/null)" ]; then
    rm -rf "$RUN_LOCK" 2>/dev/null || true   # steal a stale runner lock (>6h untouched)
fi
if ! mkdir "$RUN_LOCK" 2>/dev/null; then
    echo "$(date '+%F %T') retry_runner: a sweep is already running, skipping" >> "$LOG"
    exit 0
fi
trap 'rmdir "$RUN_LOCK" 2>/dev/null || true' EXIT

# Nothing queued → silent no-op (the common case).
shopt -s nullglob
markers=( "$RQ_BASE"/*.retry )
shopt -u nullglob
if [ "${#markers[@]}" -eq 0 ]; then
    exit 0
fi

echo "$(date '+%F %T') retry_runner: ${#markers[@]} job(s) queued for retry" >> "$LOG"

ALLOWED_WRAPPERS="run_claude_job.sh run_job.sh"

for marker in "${markers[@]}"; do
    [ -f "$marker" ] || continue
    job="$(grep -E '^job=' "$marker" 2>/dev/null | head -1 | cut -d= -f2-)"
    job="${job:-$(basename "$marker" .retry)}"
    cmd_b64="$(grep -E '^cmd_b64=' "$marker" 2>/dev/null | head -1 | cut -d= -f2-)"
    paused="$(grep -E '^paused=' "$marker" 2>/dev/null | head -1 | cut -d= -f2-)"

    # Gave up the 30-min cadence (too many failures) → don't replay or alert; the
    # job recovers via its normal schedule.
    if [ "${paused:-0}" = "1" ]; then
        continue
    fi

    if [ -z "${cmd_b64:-}" ]; then
        echo "$(date '+%F %T') retry_runner: '${job}' marker has no cmd_b64 — removing corrupt marker" >> "$LOG"
        rm -f "$marker" 2>/dev/null || true
        "$SCRIPT_DIR/notify.sh" "🔴 retry_runner: dropped a corrupt retry marker for '${job}' (no command recorded). It will run again on its normal schedule." >/dev/null 2>&1 || true
        continue
    fi

    # Decode NUL-joined argv (bash 3.2: no mapfile — read -d '' instead).
    argv=()
    while IFS= read -r -d '' field; do
        argv+=( "$field" )
    done < <(printf '%s' "$cmd_b64" | openssl base64 -d -A 2>/dev/null)

    if [ "${#argv[@]}" -lt 2 ]; then
        echo "$(date '+%F %T') retry_runner: '${job}' cmd_b64 undecodable (<2 argv) — removing corrupt marker" >> "$LOG"
        rm -f "$marker" 2>/dev/null || true
        "$SCRIPT_DIR/notify.sh" "🔴 retry_runner: dropped a corrupt retry marker for '${job}' (undecodable command). It will run again on its normal schedule." >/dev/null 2>&1 || true
        continue
    fi

    # Defense-in-depth: only ever replay our own known wrappers, by absolute path
    # inside THIS scripts dir. Markers are only ever written by those wrappers, so
    # this should always pass; it guarantees a corrupted/edited marker can never
    # turn the runner into an arbitrary-command executor.
    wrapper="${argv[0]}"
    base="$(basename "$wrapper")"
    case " $ALLOWED_WRAPPERS " in
        *" $base "*) : ;;
        *)
            echo "$(date '+%F %T') retry_runner: '${job}' wrapper '${base}' not allowlisted — skipping" >> "$LOG"
            continue
            ;;
    esac
    if [ "$wrapper" != "$SCRIPT_DIR/$base" ] || [ ! -x "$wrapper" ]; then
        echo "$(date '+%F %T') retry_runner: '${job}' wrapper path unexpected/missing ('${wrapper}') — skipping" >> "$LOG"
        continue
    fi

    # The job's own scheduled fire may have run and cleared this marker since we
    # listed the queue — don't redundantly replay a heavy routine.
    if [ ! -f "$marker" ]; then
        echo "$(date '+%F %T') retry_runner: '${job}' marker cleared before replay — skipping" >> "$LOG"
        continue
    fi

    echo "$(date '+%F %T') retry_runner: replaying '${job}' → ${argv[*]}" >> "$LOG"
    IRIS_RETRY=1 "${argv[@]}" >> "$LOG" 2>&1
    rc=$?
    # Keep the runner lock fresh so a long sequential recovery batch isn't mistaken
    # for a stale lock and stolen by the next 30-min tick.
    touch "$RUN_LOCK" 2>/dev/null || true
    echo "$(date '+%F %T') retry_runner: '${job}' replay exit ${rc} ($( [ -f "$marker" ] && echo 'still queued' || echo 'cleared'))" >> "$LOG"
done

exit 0
