#!/bin/bash
# retry_queue.sh — shared helper sourced by run_claude_job.sh and run_job.sh to
# implement "if an automation can't run, retry it every 30 minutes until it can."
#
# Why this exists (Steve's 2026-06-18 directive): a scheduled launchd job that
# FAILED — stale `claude login`, exhausted Max credits, a transient MCP/network
# hiccup, the AI_Workspace volume momentarily busy — used to just alert Telegram
# and then wait until its NEXT scheduled fire (up to a day, or a week for the
# weekly jobs). So a 04:10 stale-auth failure meant the routine simply didn't run
# that day. This adds a self-healing retry layer:
#
#   1. When a wrapper hits a "could not run" failure it drops a marker file in the
#      retry queue recording the EXACT argv to replay (rq_record_failure).
#   2. A separate launchd job (com.iris.claude-code-retry) sweeps the queue every
#      30 minutes (retry_runner.sh) and re-runs each marked job through the SAME
#      wrapper with IRIS_RETRY=1 set.
#   3. The moment a replay succeeds, the wrapper clears the marker
#      (rq_clear_on_success) and Telegram gets a one-time "RECOVERED" ping.
#
# Alerts are throttled on the retry path so a permanently-broken job pings once on
# the first failure (the wrapper's own red alert), once when the first retry also
# fails, then only every ~3h — never every 30 minutes.
#
# Everything here lives on LOCAL disk (~/iris_studio/retry), NOT the synced vault
# and NOT the AI_Workspace volume, so the queue survives a volume hiccup. (If
# AI_Workspace itself is unmounted the wrappers can't launch at all — that is a
# whole-machine outage outside this layer's scope.)
#
# Portability: targets macOS /bin/bash 3.2 — no `mapfile`, no `flock` CLI, no
# associative arrays. Locks are atomic mkdir; argv is round-tripped through
# NUL-joined openssl base64. Designed to be sourced under `set -uo pipefail`
# WITHOUT `set -e` (the wrappers deliberately avoid -e), so every expansion is
# guarded and no function relies on -e for control flow.

# ---- locations -------------------------------------------------------------
RQ_BASE="${RQ_BASE:-$HOME/iris_studio/retry}"   # overridable for self-tests
_RQ_LOCK=""                                       # set by rq_acquire_lock
# Give-up ceiling for the every-30-min cadence. After this many failures (~24h at
# 30-min spacing) we PAUSE the high-frequency sweep so a permanently-broken job
# can't ping Telegram forever. The job is NOT abandoned — it still runs on its
# normal daily/weekly schedule and recovers on its own once fixed.
RQ_MAX_ATTEMPTS="${RQ_MAX_ATTEMPTS:-48}"

_rq_queue_dir() { printf '%s' "$RQ_BASE"; }
_rq_lock_dir()  { printf '%s' "$RQ_BASE/locks"; }
_rq_marker()    { printf '%s/%s.retry' "$RQ_BASE" "$1"; }

_rq_now_epoch() { date +%s; }
_rq_now_iso()   { date -u +%Y-%m-%dT%H:%M:%SZ; }
_rq_now_et()    { TZ=America/New_York date '+%Y-%m-%d %H:%M %Z'; }

# Notify Steve's Telegram, best-effort. Resolves notify.sh next to this file.
_rq_alert() {
    local nd
    nd="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/notify.sh"
    [ -x "$nd" ] && "$nd" "$1" >/dev/null 2>&1 || true
}

# Human "Hh Mm" from a second count.
_rq_human_dur() {
    local s="${1:-0}" h m
    h=$(( s / 3600 )); m=$(( (s % 3600) / 60 ))
    if [ "$h" -gt 0 ]; then printf '%dh %dm' "$h" "$m"
    elif [ "$m" -gt 0 ]; then printf '%dm' "$m"
    else printf '%ds' "$s"; fi
}

# Read one key from a marker file (value is everything after the first '=').
_rq_get() {
    local file="$1" key="$2"
    [ -f "$file" ] || return 0
    grep -E "^${key}=" "$file" 2>/dev/null | head -1 | cut -d= -f2-
}

# ---- per-job lock (atomic mkdir) ------------------------------------------
# Serializes a job against itself so a 30-min retry sweep can't run concurrently
# with that job's own daily/weekly fire (the auth-recovery scenario makes that
# overlap genuinely possible). Returns 0 = lock held by US (proceed), 1 = held by
# a live run (caller should exit 0 quietly). FAIL-OPEN: if the lock machinery
# itself is unusable we return 0 and proceed — a missed run is worse than a rare
# double-run.
rq_acquire_lock() {
    local job="$1" ld lock
    ld="$(_rq_lock_dir)"
    mkdir -p "$ld" 2>/dev/null || { _RQ_LOCK=""; return 0; }
    lock="$ld/${job}.lock"
    # Steal a stale lock (>180 min) — a previous run that was killed mid-flight
    # (panic, SIGKILL) could leave the dir behind; don't wedge the job forever.
    if [ -d "$lock" ] && [ -n "$(find "$lock" -maxdepth 0 -mmin +180 2>/dev/null)" ]; then
        rm -rf "$lock" 2>/dev/null || true
    fi
    if mkdir "$lock" 2>/dev/null; then
        _RQ_LOCK="$lock"
        return 0
    fi
    _RQ_LOCK=""
    return 1
}

rq_release_lock() {
    [ -n "${_RQ_LOCK:-}" ] && rmdir "$_RQ_LOCK" 2>/dev/null
    _RQ_LOCK=""
    return 0
}

# ---- record a failure (enqueue / increment) -------------------------------
# Usage: rq_record_failure <job> <kind> <reason> -- <argv...>
#   <kind>  "claude" | "infra" (informational only)
#   <reason> short human cause (single line)
#   <argv>  the EXACT command to replay — typically "$0" "$@" of the wrapper.
# Creates or updates the marker, bumping the failure count. Emits a Telegram
# alert ONLY on the retry path (IRIS_RETRY=1) and only at throttled checkpoints;
# the ORIGINAL (scheduled) fire is expected to emit its own red alert via the
# wrapper, so we stay silent here for it to avoid a double-ping.
rq_record_failure() {
    local job="$1" kind="$2" reason="$3"
    shift 3
    [ "${1:-}" = "--" ] && shift   # tolerate the explicit argv delimiter
    local file dir attempts first_iso first_epoch cmd_b64 reason1
    dir="$(_rq_queue_dir)"
    mkdir -p "$dir" 2>/dev/null || { echo "rq_record_failure: cannot create $dir" >&2; return 0; }
    file="$(_rq_marker "$job")"

    # Preserve first-failure stamps across updates; start fresh otherwise.
    attempts="$(_rq_get "$file" attempts)";          attempts="${attempts:-0}"
    first_iso="$(_rq_get "$file" first_failed)";      first_iso="${first_iso:-$(_rq_now_iso)}"
    first_epoch="$(_rq_get "$file" first_failed_epoch)"; first_epoch="${first_epoch:-$(_rq_now_epoch)}"
    case "$attempts" in (*[!0-9]*|'') attempts=0 ;; esac
    attempts=$(( 10#$attempts + 1 ))   # 10# = force base-10 so 008/009 don't break

    # Cross the give-up ceiling → pause the 30-min cadence (the runner skips
    # paused markers). The normal schedule still re-attempts the job.
    local paused=0
    [ "$attempts" -ge "$RQ_MAX_ATTEMPTS" ] && paused=1

    # Replay argv → NUL-joined, base64 (-A = single line). openssl is present on
    # macOS and handles the embedded NULs that plain `base64`/word-splitting can't.
    cmd_b64="$(printf '%s\0' "$@" | openssl base64 -A 2>/dev/null)"
    reason1="$(printf '%s' "$reason" | tr '\n' ' ' | sed 's/[[:space:]]\{1,\}/ /g')"

    # Atomic write (temp + mv) so a concurrent reader never sees a half-file.
    local tmp; tmp="$(mktemp "${file}.XXXXXX" 2>/dev/null)" || tmp="${file}.tmp.$$"
    {
        printf 'job=%s\n' "$job"
        printf 'kind=%s\n' "$kind"
        printf 'first_failed=%s\n' "$first_iso"
        printf 'first_failed_epoch=%s\n' "$first_epoch"
        printf 'last_attempt=%s\n' "$(_rq_now_iso)"
        printf 'attempts=%s\n' "$attempts"
        printf 'paused=%s\n' "$paused"
        printf 'last_reason=%s\n' "$reason1"
        printf 'cmd_b64=%s\n' "$cmd_b64"
    } > "$tmp" 2>/dev/null && mv -f "$tmp" "$file" 2>/dev/null || { rm -f "$tmp" 2>/dev/null; }
    chmod 0644 "$file" 2>/dev/null || true

    # Throttled "still failing" alert — retry path only. The give-up notice fires
    # exactly once (at the crossing), because once paused the runner stops calling
    # this on the retry path.
    if [ "${IRIS_RETRY:-0}" = "1" ]; then
        local elapsed; elapsed=$(( $(_rq_now_epoch) - first_epoch ))
        if [ "$paused" -eq 1 ]; then
            _rq_alert "🛑 Job '${job}' has failed ${attempts}× over ~$(_rq_human_dur "$elapsed") — pausing the every-30-min auto-retry (this looks like a real problem, not a transient one).
It will STILL run on its normal schedule, so it can recover once fixed — but please take a look.
Last reason: ${reason1:-(none)}
Time: $(_rq_now_et)"
        elif [ "$attempts" -eq 2 ]; then
            _rq_alert "⚠️ Job '${job}' STILL failing after the first auto-retry.
Will keep retrying every 30 min until it succeeds.
Reason: ${reason1:-(none)}
Time: $(_rq_now_et)"
        elif [ $(( attempts % 6 )) -eq 0 ]; then
            _rq_alert "⚠️ Job '${job}' still failing — attempt ${attempts}, ~$(_rq_human_dur "$elapsed") of auto-retries.
Reason: ${reason1:-(none)}
Time: $(_rq_now_et)"
        fi
    fi
    return 0
}

# ---- how many consecutive failures are on record for a job --------------------
# Echoes a base-10 integer (0 when there is no marker). Lets a wrapper throttle its
# own scheduled-path alert by how long the job has already been failing, the way
# rq_record_failure throttles the retry path. Always succeeds; never errors.
rq_attempts() {
    local a
    a="$(_rq_get "$(_rq_marker "$1")" attempts)"
    case "${a:-0}" in (*[!0-9]*|'') a=0 ;; esac
    printf '%s' "$(( 10#${a:-0} ))"
}

# ---- silently retire a marker (a run we deliberately SKIPPED, not a recovery) ----
# Use on an exit-0 path that did NOT actually run the job because of a known,
# self-healing infra condition (e.g. FDA/EPERM can't exec the venv interpreter).
# Removes any pending retry marker WITHOUT a RECOVERED ping — the job didn't
# recover, we just don't want the 30-min sweep replaying a guaranteed-skip forever
# (which never bumps attempts, so it would never hit the give-up ceiling either).
# Matches the EPERM-path contract: "no retry marker — self-heal on next schedule."
# Silent no-op when no marker exists.
rq_drop_marker() {
    local job="$1" file
    file="$(_rq_marker "$job")"
    rm -f "$file" 2>/dev/null || true
    return 0
}

# ---- clear on a run that actually happened --------------------------------
# Call on ANY exit-0 / "it ran" path. If a retry marker existed (the job had been
# failing) this emits a one-time RECOVERED ping and removes the marker. On a
# normal healthy run with no marker it is a silent no-op.
rq_clear_on_success() {
    local job="$1" file attempts first_epoch elapsed reason
    file="$(_rq_marker "$job")"
    [ -f "$file" ] || return 0
    attempts="$(_rq_get "$file" attempts)"; attempts="${attempts:-0}"
    case "$attempts" in (*[!0-9]*|'') attempts=0 ;; esac
    attempts=$(( 10#$attempts ))   # base-10 normalize so -ge below can't hit an octal error
    first_epoch="$(_rq_get "$file" first_failed_epoch)"; first_epoch="${first_epoch:-$(_rq_now_epoch)}"
    reason="$(_rq_get "$file" last_reason)"
    rm -f "$file" 2>/dev/null || true
    if [ "$attempts" -ge 1 ]; then
        elapsed=$(( $(_rq_now_epoch) - first_epoch ))
        _rq_alert "✅ Job '${job}' RECOVERED — ran successfully after ${attempts} failed attempt(s) (~$(_rq_human_dur "$elapsed") of auto-retries).
Was failing: ${reason:-(unknown)}
Time: $(_rq_now_et)"
    fi
    return 0
}
