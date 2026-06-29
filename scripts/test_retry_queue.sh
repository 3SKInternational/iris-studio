#!/bin/bash
# test_retry_queue.sh — self-checks for the retry-queue helpers, focused on the
# rq_drop_marker fix (the immortal-marker bug: an EPERM-skip exit-0 path that never
# cleared a pre-existing retry marker → the 30-min sweep replayed a guaranteed-skip
# forever, never bumping attempts so never hitting the give-up cap).
#
# Run:  bash scripts/test_retry_queue.sh   (exit 0 = all pass)

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Isolate all marker state in a throwaway dir; never touch the real queue.
RQ_BASE="$(mktemp -d)"; export RQ_BASE
trap 'rm -rf "$RQ_BASE"' EXIT

# Stub notify so a test can never page Steve's Telegram.
notify() { :; }

# shellcheck source=/dev/null
. "$SCRIPT_DIR/retry_queue.sh"

fail() { echo "FAIL: $1"; exit 1; }
marker() { printf '%s/%s.retry' "$RQ_BASE" "$1"; }

# 1. drop is a silent no-op when no marker exists (must not error, must not create one).
rq_drop_marker "ghost"
[ ! -f "$(marker ghost)" ] || fail "rq_drop_marker created a marker out of nothing"

# 2. drop removes an existing marker.
rq_record_failure "capsweep" "infra" "exit code 1" -- /bin/echo hi
[ -f "$(marker capsweep)" ] || fail "rq_record_failure did not create a marker"
rq_drop_marker "capsweep"
[ ! -f "$(marker capsweep)" ] || fail "rq_drop_marker left the marker in place"

# 3. drop is silent — no RECOVERED ping (unlike rq_clear_on_success). We assert it
#    does NOT call _rq_alert by overriding _rq_alert to a tripwire.
rq_record_failure "capsweep2" "infra" "exit code 1" -- /bin/echo hi
_rq_alert() { fail "rq_drop_marker emitted an alert (should be silent)"; }
rq_drop_marker "capsweep2"
[ ! -f "$(marker capsweep2)" ] || fail "rq_drop_marker (silent path) left the marker"

echo "OK: all retry-queue drop-marker checks pass"
