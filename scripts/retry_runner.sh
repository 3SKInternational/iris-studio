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
RUN_LOCK_NAME=".runner"   # → $RQ_BASE/locks/.runner.lock

mkdir -p "$RQ_BASE" "$(dirname "$LOG")" 2>/dev/null || true
# shellcheck source=retry_queue.sh
. "$SCRIPT_DIR/retry_queue.sh"

# Only one sweep at a time. If the previous sweep is still running (a recovered
# heavy routine can take up to its 8 h cap, twice per tick), skip this tick.
# Same lock as the per-job locks (A-105, after A-99): the holder's PID lives in
# the dir, so a live sweep is never stolen and a dead one is taken at once; the
# trap removes the lock only while it is still ours. Before A-105 the runner
# stole any lock untouched for 6 h (one 8 h replay never touched it) and its
# trap removed the lock even after it was stolen, so two sweeps could overlap.
if ! rq_acquire_lock "$RUN_LOCK_NAME"; then
    echo "$(date '+%F %T') retry_runner: a sweep is already running, skipping" >> "$LOG"
    exit 0
fi
RUN_LOCK="$_RQ_LOCK"
trap 'rq_release_lock' EXIT

# Nothing queued → silent no-op (the common case).
shopt -s nullglob
markers=( "$RQ_BASE"/*.retry )
shopt -u nullglob
if [ "${#markers[@]}" -eq 0 ]; then
    exit 0
fi

# Replay the markers whose last attempt is OLDEST first, and at most
# RQ_REPLAYS_PER_TICK per sweep. After an outage, a recovery un-pauses every
# paused claude marker at once (A-82); without the cap that is a dozen heavy
# routines back to back. Oldest-first means a job that keeps failing (its
# last_attempt moves forward each time) can't hold a slot ahead of the rest.
# ISO-8601 UTC stamps sort as text; a marker with no stamp sorts first.
# Trade: after a dozen are un-paused, a fresh failure can wait ~3 h behind them.
# A replay that exits at once (its job's lock is held) still uses a slot.
RQ_REPLAYS_PER_TICK="${RQ_REPLAYS_PER_TICK:-2}"
case "$RQ_REPLAYS_PER_TICK" in (*[!0-9]*|'') RQ_REPLAYS_PER_TICK=2 ;; esac
tab=$'\t'
sorted=()
while IFS= read -r line; do
    sorted+=( "${line#*"$tab"}" )
done < <(for m in "${markers[@]}"; do
    printf '%s\t%s\n' "$(grep -E '^last_attempt=' "$m" 2>/dev/null | head -1 | cut -d= -f2-)" "$m"
done | sort)
markers=( "${sorted[@]}" )
replayed=0

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

    if [ "$replayed" -ge "$RQ_REPLAYS_PER_TICK" ]; then
        echo "$(date '+%F %T') retry_runner: per-tick cap (${RQ_REPLAYS_PER_TICK}) reached — rest wait for the next sweep" >> "$LOG"
        break
    fi
    replayed=$(( replayed + 1 ))

    echo "$(date '+%F %T') retry_runner: replaying '${job}' → ${argv[*]}" >> "$LOG"
    IRIS_RETRY=1 "${argv[@]}" >> "$LOG" 2>&1
    rc=$?
    # Keep the runner lock's mtime fresh so the 24 h PID-reuse ceiling in
    # rq_acquire_lock counts from the last replay, not the start of the sweep.
    [ -n "$RUN_LOCK" ] && touch "$RUN_LOCK" 2>/dev/null || true
    echo "$(date '+%F %T') retry_runner: '${job}' replay exit ${rc} ($( [ -f "$marker" ] && echo 'still queued' || echo 'cleared'))" >> "$LOG"
done

exit 0
