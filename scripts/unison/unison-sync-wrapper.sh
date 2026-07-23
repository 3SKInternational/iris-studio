#!/bin/bash
# unison-sync-wrapper.sh — launchd entrypoint on the AIR for bidirectional sync.
# Runs `unison 3sk-vault` non-interactively, classifies the result, logs it, and
# (if a bot token is available) pings Telegram on conflict/failure ONLY.
#
# Exit-code contract (passes launchd a non-zero only on a real fault):
#   unison 0 -> success, fully synced
#   unison 1 -> some files skipped (conflicts) — needs manual resolution -> NOTIFY
#   unison 2 -> non-fatal errors -> NOTIFY
#   unison 3 -> fatal error -> NOTIFY
# The Mini being unreachable (Air on a foreign network / Mini asleep) is NOT a
# fault — it exits 0 and stays quiet, exactly like the one-way rsync job does.

set -uo pipefail

PROFILE="3sk-vault"
PROFILE_FILE="${HOME}/.unison/${PROFILE}.prf"
MINI_IP="100.118.108.65"
MINI_USER="steve"
SSH_KEY="${HOME}/.ssh/id_ed25519_iris_mini_sync"
SSH_OPTS="-i ${SSH_KEY} -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=15 -o ServerAliveInterval=15 -o ServerAliveCountMax=4"
LOG="${HOME}/iris_studio/logs/unison-sync.log"
# Data-loss tripwire: hold sync if the Air vault has shrunk below this fraction
# of its last-synced file count (catches a partial local loss BEFORE it can
# propagate Air->Mini as deletions — the exact 5/27 vector).
FLOOR_PCT=90
COUNT_STATE="${HOME}/.unison/${PROFILE}.filecount"

mkdir -p "$(dirname "${LOG}")"
log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" | tee -a "${LOG}"; }

# Optional Telegram notify — only if a token+chat are present in the env file.
# Never hard-fail on a missing token; sync reliability must not depend on it.
notify() {
    local msg="$1"
    local envf="${HOME}/.iris_notify.env"
    [ -f "${envf}" ] || { log "notify skipped (no ${envf}): ${msg}"; return 0; }
    # shellcheck disable=SC1090
    . "${envf}" 2>/dev/null || return 0
    [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ] || return 0
    curl -s --max-time 15 \
        -d "chat_id=${TELEGRAM_CHAT_ID}" \
        --data-urlencode "text=${msg}" \
        "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" >/dev/null 2>&1 || true
}

# Derive the local Air vault root from the profile's first non-ssh `root =` line
# (DRY — whatever the bootstrap confirmed). Returns empty if unparseable.
air_root_from_profile() {
    [ -f "${PROFILE_FILE}" ] || return 0
    grep -E '^[[:space:]]*root[[:space:]]*=' "${PROFILE_FILE}" \
        | grep -v 'ssh://' \
        | head -1 | sed -E 's/^[[:space:]]*root[[:space:]]*=[[:space:]]*//' \
        | sed -E 's/[[:space:]]*$//'
}
# Count real vault files, pruning the same junk the profile ignores, so an
# Empty-Trash / .DS_Store purge can't false-trip the shrink guard.
count_files() {
    find "$1" -type f \
        -not -path '*/.git/*' -not -path '*/.trash/*' \
        -not -name '.DS_Store' -not -name '._*' -not -name '*.tmp' \
        2>/dev/null | wc -l | tr -d ' '
}

# --- locate unison (Homebrew arm64 / intel / PATH) ---------------------------
UNISON=""
for cand in "${HOME}/bin/unison" /opt/homebrew/bin/unison /usr/local/bin/unison "$(command -v unison 2>/dev/null)"; do
    if [ -n "${cand}" ] && [ -x "${cand}" ]; then UNISON="${cand}"; break; fi
done
if [ -z "${UNISON}" ]; then
    log "ERROR: unison not found on PATH — install via 'brew install unison'. exit 0 (no fault loop)"
    notify "🔴 Air↔Mini sync: unison not installed on the Air. Run: brew install unison"
    exit 0
fi

log "=== unison-sync run start (bidirectional, profile=${PROFILE}) ==="

# --- reachability precheck (Mini asleep/foreign-network = normal, skip clean) -
if ! ssh -n ${SSH_OPTS} "${MINI_USER}@${MINI_IP}" true 2>/dev/null; then
    log "Mini unreachable (${MINI_USER}@${MINI_IP}) — skipped (normal when away). exit 0"
    log "=== unison-sync run end (skipped) ==="
    exit 0
fi

# --- data-loss tripwire: hold if the Air vault shrank since last sync --------
# FAIL CLOSED: if we can't resolve a real Air root, the shrink guard would be
# blind — so HOLD, don't sync. (Never fall through to unison unguarded.)
AIR_ROOT="$(air_root_from_profile)"
if [ -z "${AIR_ROOT}" ] || [ ! -d "${AIR_ROOT}" ]; then
    log "🔴 HELD: could not resolve a valid Air vault root from ${PROFILE_FILE} (got '${AIR_ROOT}'). NOT syncing — shrink guard would be blind."
    notify "🔴 Air↔Mini sync HELD (fail-closed): can't resolve the Air vault root from ~/.unison/${PROFILE}.prf root1. Sync paused. Fix the profile root."
    log "=== unison-sync run end (held: unresolved root) ==="
    exit 0
fi

if [ -f "${COUNT_STATE}" ]; then
    CUR_COUNT="$(count_files "${AIR_ROOT}")"
    PREV_COUNT="$(tr -dc '0-9' < "${COUNT_STATE}")"
    # floor = prev * FLOOR_PCT / 100, integer math
    if [ -n "${PREV_COUNT}" ] && [ "${PREV_COUNT}" -gt 0 ] 2>/dev/null; then
        FLOOR=$(( PREV_COUNT * FLOOR_PCT / 100 ))
        if [ "${CUR_COUNT}" -lt "${FLOOR}" ]; then
            log "🔴 HELD: Air vault shrank ${PREV_COUNT} -> ${CUR_COUNT} files (floor ${FLOOR}). NOT syncing — refusing to propagate possible deletions to the Mini."
            notify "🔴 Air↔Mini sync HELD: Air vault dropped ${PREV_COUNT}→${CUR_COUNT} files. Sync paused to avoid deleting from the Mini. If intentional, on the Air run: rm ${COUNT_STATE}"
            log "=== unison-sync run end (held: shrink guard) ==="
            exit 0
        fi
    fi
fi

"${UNISON}" "${PROFILE}" >>"${LOG}" 2>&1
RC=$?

# After a non-fatal sync (rc 0/1), refresh the last-known count as the new
# baseline (incl. Mini-originated deletions applied this run). Never persist 0.
if [ "${RC}" -eq 0 ] || [ "${RC}" -eq 1 ]; then
    NEW_COUNT="$(count_files "${AIR_ROOT}")"
    [ "${NEW_COUNT}" -gt 0 ] 2>/dev/null && printf '%s\n' "${NEW_COUNT}" > "${COUNT_STATE}"
fi

case "${RC}" in
    0) log "Sync complete, no conflicts. exit 0"
       log "=== unison-sync run end (success) ==="
       exit 0 ;;
    1) log "Sync done but SOME FILES SKIPPED (conflicts). Manual resolution needed. unison rc=1"
       notify "⚠️ Air↔Mini sync: conflict(s) skipped — same file edited on both Macs. Resolve manually (see ~/iris_studio/logs/unison-sync.log)."
       log "=== unison-sync run end (conflicts) ==="
       exit 0 ;;
    *) log "ERROR: unison rc=${RC} (non-fatal/fatal). Sync may be incomplete."
       notify "🔴 Air↔Mini sync FAILED (unison rc=${RC}). Check ~/iris_studio/logs/unison-sync.log on the Air."
       log "=== unison-sync run end (error ${RC}) ==="
       exit "${RC}" ;;
esac
