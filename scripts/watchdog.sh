#!/usr/bin/env bash
# watchdog.sh — generic DETACHED completion watchdog for ANY long-running process.
#
# Pings Steve's Telegram (via notify.sh) when the watched process finishes —
# success OR failure — INDEPENDENT of whatever launched it. This is the reusable
# primitive version of spend_watch.sh: spend_watch watches a billed image spend;
# this watches an arbitrary command or PID.
#
# WHY THIS EXISTS (root-caused 2026-06-20): the SCHEDULED routine suite is already
# covered — every launchd Claude routine routes through run_claude_job.sh, and
# non-claude jobs through run_job.sh, both of which alert Telegram on ✅/⚠️/🔴.
# But an AD-HOC long job — a manually kickstarted routine watched from a chat
# session, a one-off backfill, a detached spend — had no DURABLE watcher. An
# in-session `until grep …; do sleep; done` poll loop dies the moment the session
# ends or compacts (the loop was killed; the job kept running and finished unseen).
# Wrap the job in this instead and the completion ping is guaranteed.
#
# TWO MODES
#   watchdog.sh --label NAME [--log PATH] [--quiet-ok] -- CMD [ARGS...]
#       Runs CMD as a child, captures combined output to LOG (a mktemp if omitted),
#       and pings ✅ on rc=0 (unless --quiet-ok) / 🔴 with a log tail on rc!=0.
#       The watchdog's own exit code IS the command's exit code.
#
#   watchdog.sh --label NAME --pid PID [--timeout SEC]
#       Watches an ALREADY-running PID until it exits, then pings. A non-child's
#       exit code is not retrievable, so this reports "ended" (not pass/fail);
#       pair with --quiet-ok if that process emits its own success notice and you
#       only want an alert when the watchdog itself times out. Liveness is checked
#       with kill -0 AND ps -p, so a cross-uid (e.g. root/sudo) process is seen as
#       alive rather than mis-read as gone. CAVEAT: PID watching is inherently
#       racy — the OS can recycle a PID onto an unrelated process; watch a PID you
#       just spawned, promptly.
#
# LAUNCH IT DETACHED so the same kill that takes the job down does NOT take the
# watcher down. macOS has no `setsid` — use nohup + disown (same as spend_watch):
#   nohup scripts/watchdog.sh --label backfill --log /tmp/bf.log -- python3 backfill.py \
#         >/dev/null 2>&1 & disown
#   nohup scripts/watchdog.sh --label spend3 --pid 54321 >/dev/null 2>&1 & disown
#
# Launch it from a FOREGROUND shell call that returns immediately (the nohup+&
# backgrounds it). Do NOT launch it via a harness "run in background" task — that
# task is tracked and gets killed on session/turn boundaries, which is the exact
# failure this script exists to avoid.
#
# notify.sh is best-effort: a notify failure never changes the watchdog's own exit
# code. NOTIFY_BIN overrides the notifier (used by --self-test).
#
# Exit codes: 0 = watched ok (rc 0 / pid ended cleanly)
#             N = watched command's non-zero rc (alerted)
#             2 = --pid timeout, process still alive (alerted)
#             64 = usage error

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NOTIFY="${NOTIFY_BIN:-$SCRIPT_DIR/notify.sh}"   # NOTIFY_BIN overridable for self-tests
POLL_INTERVAL="${WATCHDOG_POLL_INTERVAL:-10}"   # seconds between --pid liveness checks
# Guard the env knob: a non-integer or 0 would busy-spin the --pid loop. Fall back.
case "$POLL_INTERVAL" in (*[!0-9]*|''|0) POLL_INTERVAL=10;; esac

usage() {
    echo "usage: watchdog.sh --label NAME [--log PATH] [--quiet-ok] -- CMD [ARGS...]" >&2
    echo "       watchdog.sh --label NAME --pid PID [--timeout SEC]" >&2
    echo "       watchdog.sh --self-test | --help" >&2
    exit 64
}

show_help() { sed -n '2,60p' "$0"; exit 0; }

alert() {
    # Best-effort — never let a notify failure mask the watched job's exit code.
    "$NOTIFY" "$1" >/dev/null 2>&1 || true
}

ts() { date '+%Y-%m-%d %H:%M %Z'; }

# True if PID exists, regardless of ownership. kill -0 fails with EPERM for a
# cross-uid process (which would falsely read as "gone"), so confirm existence
# with ps -p on the kill -0 miss. ps -p alone is authoritative on macOS but forks;
# the kill -0 fast path keeps the common same-uid poll cheap.
pid_alive() {
    kill -0 "$1" 2>/dev/null && return 0
    ps -p "$1" >/dev/null 2>&1
}

# Resolve $LOG to a writable file. If --log was given but its dir can't be made or
# the file can't be opened (bad path, no perm, full disk), DON'T let that bash
# redirect failure abort the command and masquerade as a command failure — fall
# back to a mktemp log and tell Steve the requested path was unusable.
prepare_log() {
    if [ -z "$LOG" ]; then
        LOG="$(mktemp -t "watchdog-${LABEL//[^A-Za-z0-9_]/_}.XXXXXX")"
        return
    fi
    mkdir -p "$(dirname "$LOG")" 2>/dev/null || true
    # Probe inside a subshell whose stderr is pre-redirected, so the open-failure
    # message (emitted before an inline `2>/dev/null` would take effect) is swallowed.
    if ! ( : >>"$LOG" ) 2>/dev/null; then
        local bad="$LOG"
        LOG="$(mktemp -t "watchdog-${LABEL//[^A-Za-z0-9_]/_}.XXXXXX")"
        alert "⚠️ watchdog '${LABEL}': requested --log '${bad}' is not writable — logging to ${LOG} instead. The command still runs."
    fi
}

# ---- the watch itself (uses the parsed globals) -------------------------------
run_watch() {
    [ -n "$LABEL" ] || { echo "watchdog.sh: --label is required" >&2; usage; }

    # ---- PID mode -------------------------------------------------------------
    if [ -n "$PID" ]; then
        [ ${#CMD[@]} -eq 0 ] || { echo "watchdog.sh: --pid and -- CMD are mutually exclusive" >&2; usage; }
        case "$PID" in (*[!0-9]*|'') echo "watchdog.sh: --pid must be an integer" >&2; usage;; esac
        if [ -n "$TIMEOUT" ]; then
            case "$TIMEOUT" in (*[!0-9]*|'') echo "watchdog.sh: --timeout must be an integer (seconds)" >&2; usage;; esac
        fi

        if ! pid_alive "$PID"; then
            # Already gone before we started watching — say so, don't false-claim a watch.
            [ "$QUIET_OK" -eq 1 ] || alert "✅ watchdog '${LABEL}': pid ${PID} already not running at $(ts) (nothing to watch)."
            exit 0
        fi

        local waited=0
        while pid_alive "$PID"; do
            sleep "$POLL_INTERVAL"
            waited=$((waited + POLL_INTERVAL))
            if [ -n "$TIMEOUT" ] && [ "$waited" -ge "$TIMEOUT" ]; then
                if pid_alive "$PID"; then
                    alert "⏱️ watchdog '${LABEL}': pid ${PID} STILL running after ${TIMEOUT}s timeout at $(ts). Not killed — check it."
                    exit 2
                fi
                break
            fi
        done
        [ "$QUIET_OK" -eq 1 ] || alert "✅ watchdog '${LABEL}': pid ${PID} ended at $(ts)."
        exit 0
    fi

    # ---- CMD mode -------------------------------------------------------------
    [ ${#CMD[@]} -gt 0 ] || { echo "watchdog.sh: provide either --pid PID or -- CMD ..." >&2; usage; }
    prepare_log

    # Run the command as a child, combined output appended to LOG.
    "${CMD[@]}" >>"$LOG" 2>&1
    local rc=$?

    if [ "$rc" -eq 0 ]; then
        if [ "$QUIET_OK" -ne 1 ]; then
            alert "✅ watchdog '${LABEL}': finished ok (rc=0) at $(ts).
Log: ${LOG}"
        fi
        exit 0
    fi

    alert "🔴 watchdog '${LABEL}': FAILED (rc=${rc}) at $(ts).
Log: ${LOG}
Tail:
$(tail -n 4 "$LOG" 2>/dev/null || echo '(no log)')"
    exit "$rc"
}

# ---- self-test ----------------------------------------------------------------
# Exercises both notify paths with a stub notifier (NOTIFY_BIN), asserting the
# right ping fires and the watchdog's exit code mirrors the command. Run:
#   scripts/watchdog.sh --self-test
selftest() {
    local tmp stub out_ok out_fail out_quiet rc
    tmp="$(mktemp -d -t watchdog-selftest.XXXXXX)"
    stub="$tmp/stub_notify.sh"
    cat >"$stub" <<'STUB'
#!/usr/bin/env bash
# stub: record the alert text to the file named in WD_STUB_OUT
printf '%s' "$1" >>"${WD_STUB_OUT:?}"
STUB
    chmod +x "$stub"

    local fails=0
    # 1) rc=0 → ✅ ping, watchdog exits 0
    out_ok="$tmp/ok.txt"; : >"$out_ok"
    WD_STUB_OUT="$out_ok" NOTIFY_BIN="$stub" "$0" --label t_ok -- true; rc=$?
    if [ "$rc" -ne 0 ]; then echo "FAIL: ok-path exit $rc (want 0)"; fails=$((fails+1)); fi
    if ! grep -q '✅' "$out_ok"; then echo "FAIL: ok-path no ✅ ping"; fails=$((fails+1)); fi

    # 2) rc!=0 → 🔴 ping, watchdog mirrors the rc
    out_fail="$tmp/fail.txt"; : >"$out_fail"
    WD_STUB_OUT="$out_fail" NOTIFY_BIN="$stub" "$0" --label t_fail -- bash -c 'exit 7'; rc=$?
    if [ "$rc" -ne 7 ]; then echo "FAIL: fail-path exit $rc (want 7)"; fails=$((fails+1)); fi
    if ! grep -q '🔴' "$out_fail"; then echo "FAIL: fail-path no 🔴 ping"; fails=$((fails+1)); fi

    # 3) rc=0 + --quiet-ok → NO ping, exit 0
    out_quiet="$tmp/quiet.txt"; : >"$out_quiet"
    WD_STUB_OUT="$out_quiet" NOTIFY_BIN="$stub" "$0" --label t_quiet --quiet-ok -- true; rc=$?
    if [ "$rc" -ne 0 ]; then echo "FAIL: quiet-path exit $rc (want 0)"; fails=$((fails+1)); fi
    if [ -s "$out_quiet" ]; then echo "FAIL: quiet-path emitted a ping (want none)"; fails=$((fails+1)); fi

    # 4) missing --label → usage error (64)
    NOTIFY_BIN="$stub" "$0" -- true >/dev/null 2>&1; rc=$?
    if [ "$rc" -ne 64 ]; then echo "FAIL: missing-label exit $rc (want 64)"; fails=$((fails+1)); fi

    # 5) unwritable --log → command STILL runs, exit mirrors command (not a false rc=1).
    #    Use a path whose dirname is a regular file, so mkdir -p + open both fail.
    local blocker="$tmp/blocker"; : >"$blocker"
    local marker="$tmp/ran_marker"; rm -f "$marker"
    out_log="$tmp/log.txt"; : >"$out_log"
    WD_STUB_OUT="$out_log" NOTIFY_BIN="$stub" "$0" --label t_log --log "$blocker/x.log" -- touch "$marker"; rc=$?
    if [ "$rc" -ne 0 ]; then echo "FAIL: badlog-path exit $rc (want 0 — command should still run)"; fails=$((fails+1)); fi
    if [ ! -e "$marker" ]; then echo "FAIL: badlog-path command did NOT run (marker missing)"; fails=$((fails+1)); fi

    # 6) cross-uid liveness: pid 1 (root launchd) is alive but kill -0 gives EPERM.
    #    pid_alive must see it as alive → --timeout 1 yields ⏱️ exit 2, NOT a false
    #    "already not running". (Regression guard for the EPERM-as-dead bug.)
    out_p1="$tmp/p1.txt"; : >"$out_p1"
    WATCHDOG_POLL_INTERVAL=1 WD_STUB_OUT="$out_p1" NOTIFY_BIN="$stub" "$0" --label t_p1 --pid 1 --timeout 1; rc=$?
    if [ "$rc" -ne 2 ]; then echo "FAIL: cross-uid pid exit $rc (want 2 — pid 1 is alive)"; fails=$((fails+1)); fi
    if grep -q 'already not running' "$out_p1"; then echo "FAIL: cross-uid pid mis-read as dead"; fails=$((fails+1)); fi

    rm -rf "$tmp"
    if [ "$fails" -eq 0 ]; then echo "watchdog.sh self-test: ALL PASS"; return 0; fi
    echo "watchdog.sh self-test: ${fails} FAILURE(S)"; return 1
}

# ---- arg parse + dispatch (functions above are all defined by now) ------------
LABEL=""
LOG=""
QUIET_OK=0
PID=""
TIMEOUT=""
CMD=()

while [ $# -gt 0 ]; do
    case "$1" in
        --label)     LABEL="${2:-}"; shift 2 || usage ;;
        --log)       LOG="${2:-}"; shift 2 || usage ;;
        --pid)       PID="${2:-}"; shift 2 || usage ;;
        --timeout)   TIMEOUT="${2:-}"; shift 2 || usage ;;
        --quiet-ok)  QUIET_OK=1; shift ;;
        --self-test) selftest; exit $? ;;
        -h|--help)   show_help ;;
        --)          shift; CMD=("$@"); break ;;
        *)           echo "watchdog.sh: unexpected arg: $1" >&2; usage ;;
    esac
done

run_watch
