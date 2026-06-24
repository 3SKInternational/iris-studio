#!/bin/bash
# auth_canary.sh — proactive health probe for the failure modes that silently
# break EVERY headless `claude --print` launchd job at once:
#
#   1. AUTH  (primary, high-confidence) — the Claude CLI OAuth token (Claude Max
#            login) goes stale and the CLI returns "401 Invalid authentication
#            credentials" (or runs out of credits). The fix is manual + human-only:
#            `claude login` (a.k.a. `/login` inside the TUI) on the Mini. Nothing
#            can auto-fix it. This probe runs the REAL `claude` binary, so it is a
#            direct, trustworthy test of the exact thing the scheduled jobs do.
#
#   2. VAULT (internal-disk proxy) — can a scheduled job READ the vault at all? This
#            reads a vault file (on the internal Data volume) from this launchd
#            job's own context. It catches Full Disk Access being broadly revoked
#            for scheduled jobs as it affects INTERNAL-disk reads. (Scope: macOS TCC
#            is per-responsible-process; a revocation hitting ONLY claude's exact
#            binary while this bash context still reads the internal vault would not
#            be caught here. Best-effort proxy.)
#
#   3. MOUNT (the AI_Workspace external volume — THE failure that went 16 HOURS
#            SILENT on 2026-06-23) — the entire iris_studio repo + EVERY scheduled
#            job's scripts (incl. notify.sh + its .env token) live on the external
#            AI_Workspace volume. A `brew upgrade claude-code` re-symlinks
#            /opt/homebrew/bin/claude to a new version-pinned Caskroom path, which
#            INVALIDATES that volume's Full-Disk-Access grant (FDA is keyed to the
#            binary's identity). The signature is EXACT and sneaky: `ls`/`stat`
#            still succeed, but file OPEN returns EPERM. So this probe must READ a
#            byte of a real file ON the mount, not merely stat the directory.
#
# ── WHY THIS SCRIPT NOW LIVES ON THE INTERNAL DISK ──────────────────────────
# The 2026-06-22→23 outage proved the original design fatally circular: the
# canary's script, its notify.sh, AND the Telegram token (.env) ALL lived on the
# AI_Workspace mount it was meant to watch. When FDA was revoked on that mount,
# launchd could not even read this script → it never ran → ZERO alerts for ~16h.
# Fix (2026-06-24): the EXECUTED copy of this script lives at
# ~/iris_studio/scripts/auth_canary.sh (internal disk) and the launchd job runs
# THAT copy, so the canary survives the exact outage it detects. The repo copy
# (scripts/auth_canary.sh) remains the version-controlled SOURCE OF TRUTH; deploy
# with:  cp scripts/auth_canary.sh ~/iris_studio/scripts/auth_canary.sh
#
# ── ALERT PATH (mount-independent fallback) ─────────────────────────────────
# alert() tries the canonical notify.sh first (unchanged behaviour when healthy).
# If that is unreachable — which is GUARANTEED during a MOUNT outage, since
# notify.sh + its .env token are on the dead mount — it falls back to a direct
# Telegram send using the bot token + chat id read from the login KEYCHAIN
# (the approved credential store; never the synced vault). Provision once:
#   security add-generic-password -a iris-telegram -s iris_telegram_bot_token -w "<TOKEN>"   -U
#   security add-generic-password -a iris-telegram -s iris_telegram_chat_id  -w "<CHAT_ID>" -U
# Rotate the same way (-U updates). The fallback is internal-disk + Keychain only,
# so it delivers even when AI_Workspace is EPERM'd.
#
# It does NOT attempt any fix (it can't) and it does NOT touch the retry queue — it
# is a pure detector in front of the shared run_claude_job.sh wrapper, which still
# owns per-routine failure handling + auto-retry.
#
# Alert discipline (no spam): per-dimension state is tracked across runs in a state
# file, so a Telegram message is sent only on a STATE TRANSITION (a dimension goes
# healthy→broken, or a newly-broken dimension escalates an existing outage), plus a
# throttled "still failing" re-ping (at most once per RE_ALERT_THROTTLE_SECS) and a
# single "recovered" when all broken dimensions clear. Recovery is gated on
# CONFIRMED ok — an inconclusive/transient probe NEVER clears a real outage (no
# false recovery) and never raises a false alarm. A steady-healthy run is silent.
#
# Usage:  auth_canary.sh            (normal launchd invocation)
# Env overrides (for self-tests):
#   NOTIFY_BIN   path to notify.sh   (default: canonical mount copy)
#   CLAUDE_BIN   path to claude      (default: /opt/homebrew/bin/claude)
#   STATE_FILE   path to state file  (default: ~/iris_studio/state/auth_canary.state)
#   VAULT_PROBE  internal vault file  | MOUNT_PROBE  AI_Workspace file
#
# Exit codes: 0 = healthy, 1 = a definite failure is active (auth/vault/mount),
#             2 = inconclusive this run (network/transient — NOT alerted).

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# notify.sh + its token live on the AI_Workspace mount; point at it by ABSOLUTE
# path (this script now runs from the internal disk, so $SCRIPT_DIR is internal).
# Primary channel when healthy; the Keychain fallback covers it being unreachable.
NOTIFY="${NOTIFY_BIN:-/Volumes/AI_Workspace/iris_studio/scripts/notify.sh}"
CLAUDE="${CLAUDE_BIN:-/opt/homebrew/bin/claude}"
VAULT="/Users/steve/Documents/3SK/outputs"
VAULT_PROBE="${VAULT_PROBE:-$VAULT/CLAUDE.md}"          # internal-disk read proves vault access
MOUNT_PROBE="${MOUNT_PROBE:-/Volumes/AI_Workspace/iris_studio/requirements.txt}"  # external-mount read proves AI_Workspace access
LOG="/Users/steve/iris_studio/logs/claude-code-auth-canary.log"
STATE_FILE="${STATE_FILE:-/Users/steve/iris_studio/state/auth_canary.state}"
LOCK_DIR="$(dirname "$STATE_FILE")/auth_canary.lock"

AUTH_TIMEOUT=60                          # seconds to allow the claude probe
RE_ALERT_THROTTLE_SECS=$((6 * 3600))     # while broken, re-ping at most every 6h
TS="$(date '+%Y-%m-%d %H:%M %Z')"
NOW_EPOCH="$(date +%s)"

mkdir -p "$(dirname "$STATE_FILE")" "$(dirname "$LOG")" 2>/dev/null

log() { echo "$TS auth_canary: $*" >> "$LOG"; }

# Mount-independent last-resort alert path: bot token + chat id from the login
# Keychain (provisioned out-of-band; see header). No dependency on the mount or
# the .env, so it delivers even when AI_Workspace is EPERM'd.
_keychain_telegram() {
    local msg="$1" token chat
    token="$(security find-generic-password -a iris-telegram -s iris_telegram_bot_token -w 2>/dev/null)" || return 1
    chat="$(security find-generic-password -a iris-telegram -s iris_telegram_chat_id  -w 2>/dev/null)" || return 1
    [ -n "$token" ] && [ -n "$chat" ] || return 1
    curl -fsS --max-time 20 "https://api.telegram.org/bot${token}/sendMessage" \
        --data-urlencode "chat_id=${chat}" \
        --data-urlencode "text=${msg}" >/dev/null 2>&1
}
alert() {
    # Try the canonical channel first. ANY failure (unreachable script, EPERM on
    # exec, notify.sh's own .env read denied during a mount outage) → fall back.
    if "$NOTIFY" "$1" >/dev/null 2>&1; then
        return 0
    fi
    if _keychain_telegram "$1"; then
        log "alert delivered via Keychain fallback (notify.sh unreachable)"
    else
        log "WARN: BOTH notify.sh AND Keychain fallback failed — alert not delivered"
    fi
}
write_state() {
    printf '%s|%s|%s\n' "$1" "$2" "$3" > "$STATE_FILE" \
        || log "WARN: could not write state file $STATE_FILE (a silent re-alert loop is possible)"
}
# Space-delimited set membership: _has "<set>" "<member>".
_has() { case " $1 " in *" $2 "*) return 0 ;; *) return 1 ;; esac ; }

# --- Single-instance lock ---------------------------------------------------
# RunAtLoad + the 3h interval (and manual kickstarts) can coincide; without a lock
# two overlapping instances could both observe the same transition and double-send
# the alert. mkdir is atomic, so it is a clean mutex. Fail-OPEN is NOT wanted here
# (a duplicate run is only cosmetic spam) — if the lock is held, just skip.
# Break a lock orphaned by a hard kill / panic / power-loss mid-run — the EXIT
# trap can't fire then, and the lockdir lives on the internal disk so it survives
# reboot. A legitimate run holds the lock <~90s (AUTH_TIMEOUT caps the only slow
# step at 60s), so a lock older than 10 min is certainly dead. Clearing it stops an
# orphaned lock from silently darking the canary forever (the very silent-failure
# class this canary exists to prevent).
if [ -d "$LOCK_DIR" ] && [ -n "$(find "$LOCK_DIR" -maxdepth 0 -mmin +10 2>/dev/null)" ]; then
    rmdir "$LOCK_DIR" 2>/dev/null && log "cleared a stale lock (orphaned >10m by a killed run)."
fi
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    log "another instance holds the lock — skipping this invocation."
    exit 0
fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null' EXIT

# --- Self-heal deployment drift ---------------------------------------------
# The EXECUTED copy lives on the internal disk; the repo copy on the mount is the
# version-controlled source of truth. If the mount is readable and the source has
# genuinely changed, atomically refresh THIS deployed copy so an "edited the repo,
# forgot to redeploy" can't leave a STALE canary running (the exact divergence the
# relocation was meant to retire). `mv` swaps a new inode in, so overwriting never
# corrupts the currently-running script; the new code is picked up next run.
# Skipped on a read error (the outage itself) — the last-good deployed copy keeps
# running, which is precisely what we want.
REPO_SRC="/Volumes/AI_Workspace/iris_studio/scripts/auth_canary.sh"
SELF="${BASH_SOURCE[0]}"
if [ "$SELF" != "$REPO_SRC" ] && [ -r "$REPO_SRC" ]; then
    cmp -s "$REPO_SRC" "$SELF" 2>/dev/null; _drift=$?   # 0=identical, 1=differ, >1=read error
    if [ "$_drift" -eq 1 ]; then
        if cp "$REPO_SRC" "$SELF.tmp.$$" 2>/dev/null && chmod +x "$SELF.tmp.$$" 2>/dev/null && mv -f "$SELF.tmp.$$" "$SELF" 2>/dev/null; then
            log "self-heal: refreshed deployed copy from repo source (drift detected); next run uses it."
        else
            rm -f "$SELF.tmp.$$" 2>/dev/null
            log "WARN: self-heal copy from repo failed; running the existing deployed copy."
        fi
    fi
fi

# --- State file format: STATUS|LAST_ALERT_EPOCH|REASON ----------------------
# STATUS = ok|bad. LAST_ALERT_EPOCH = epoch of the last Telegram ping about the
# current outage (0 if none). REASON = '+'-joined set of currently-broken
# dimensions (auth / vault / mount), e.g. "auth", "mount", "auth+mount".
prev_status="ok"; prev_alert_epoch=0; prev_reason=""
if [ -f "$STATE_FILE" ]; then
    IFS='|' read -r prev_status prev_alert_epoch prev_reason < "$STATE_FILE" || true
    prev_status="${prev_status:-ok}"
    case "$prev_alert_epoch" in (*[!0-9]*|'') prev_alert_epoch=0 ;; esac
fi
prev_bad=""
[ "$prev_status" = "bad" ] && prev_bad="$(printf '%s' "$prev_reason" | tr '+' ' ')"

# --- Probe 1: AUTH (real claude binary, isolated from the vault) ------------
# Ask the CLI to echo a fixed sentinel, run from /tmp so a vault problem can't
# contaminate the auth result. A real auth/credit failure ALWAYS prints the CLI's
# error string → "bad". Sentinel present → "ok" (a full authenticated round-trip).
# Anything else (timeout, network blip, the transient "low max file descriptors"
# startup abort) → "inconclusive", deliberately NOT alerted. The perl alarm is a
# portable timeout (macOS has no `timeout`); on timeout the child is SIGALRM-killed
# with empty output → inconclusive (never misclassified as bad).
AUTH_OUT="$(cd /tmp && perl -e 'my $t=shift; alarm $t; exec @ARGV or die "exec failed\n"' \
    "$AUTH_TIMEOUT" "$CLAUDE" --print --dangerously-skip-permissions \
    'Reply with exactly this token and nothing else: CANARY_OK' 2>&1)"

auth_state="inconclusive"
if printf '%s' "$AUTH_OUT" | grep -qiE 'Failed to authenticate|API Error: 40[0-9]|Invalid authentication|out of usage credits|[Cc]redit balance is too low'; then
    auth_state="bad"
elif printf '%s' "$AUTH_OUT" | grep -q 'CANARY_OK'; then
    auth_state="ok"
fi

# --- Probe 2: VAULT readability (internal Data volume) ----------------------
# Read one byte of a known vault file. A denial ("Operation not permitted"/EPERM/
# "Permission denied") → "bad". A merely-absent probe file → "inconclusive".
vault_state="ok"
if [ -e "$VAULT_PROBE" ]; then
    VAULT_OUT="$(head -c1 "$VAULT_PROBE" 2>&1 >/dev/null)"
    if printf '%s' "$VAULT_OUT" | grep -qiE 'Operation not permitted|EPERM|Permission denied'; then
        vault_state="bad"
    fi
else
    vault_state="inconclusive"
fi

# --- Probe 3: AI_WORKSPACE MOUNT readability (external volume) ---------------
# READ a byte of a real file on the mount (not just stat the dir) — the 2026-06-23
# FDA-revocation signature is `ls`/`stat` OK but file OPEN → EPERM. Denial → "bad".
# A truly-absent probe file (genuine unmount, distinct condition) → "inconclusive",
# not a manufactured failure.
mount_state="ok"
if [ -e "$MOUNT_PROBE" ]; then
    MOUNT_OUT="$(head -c1 "$MOUNT_PROBE" 2>&1 >/dev/null)"
    if printf '%s' "$MOUNT_OUT" | grep -qiE 'Operation not permitted|EPERM|Permission denied'; then
        mount_state="bad"
    fi
else
    mount_state="inconclusive"
fi

# --- Resolve per-dimension health into an effective-bad set -----------------
cur_ok=""; cur_bad=""
[ "$auth_state"  = "ok"  ] && cur_ok="$cur_ok auth"
[ "$vault_state" = "ok"  ] && cur_ok="$cur_ok vault"
[ "$mount_state" = "ok"  ] && cur_ok="$cur_ok mount"
[ "$auth_state"  = "bad" ] && cur_bad="$cur_bad auth"
[ "$vault_state" = "bad" ] && cur_bad="$cur_bad vault"
[ "$mount_state" = "bad" ] && cur_bad="$cur_bad mount"

# effective_bad = previously-bad dims NOT yet confirmed-ok again, plus newly-bad
# dims. A dimension that probed "inconclusive" this run is neither confirmed-ok nor
# newly-bad, so a prior bad state for it PERSISTS (no false recovery) and a prior ok
# state stays ok (no false alarm). This decouples recovery per-dimension.
effective_bad=""
for d in $prev_bad; do
    _has "$cur_ok" "$d" || effective_bad="$effective_bad $d"
done
for d in $cur_bad; do
    _has "$effective_bad" "$d" || effective_bad="$effective_bad $d"
done
effective_bad="$(printf '%s' "$effective_bad" | tr ' ' '\n' | awk 'NF' | sort -u | tr '\n' ' ' | sed 's/ *$//;s/^ *//')"

# --- Act --------------------------------------------------------------------
if [ -n "$effective_bad" ]; then
    # ---- BROKEN (one or more dimensions are/remain down) ----
    reason="$(printf '%s' "$effective_bad" | tr ' ' '+')"
    body=""
    _has "$effective_bad" "auth"  && body="${body}• AUTH: run \`claude login\` (or \`/login\` in the TUI) in a Terminal on the Mini — a 401 = stale OAuth token (or out of Max credits). Blocks EVERY headless claude job until cleared."$'\n'
    _has "$effective_bad" "vault" && body="${body}• VAULT (internal disk): a scheduled job can't read the vault (EPERM). Re-grant Full Disk Access to the background job's binary (System Settings ▸ Privacy & Security)."$'\n'
    _has "$effective_bad" "mount" && body="${body}• AI_WORKSPACE MOUNT: scheduled jobs can't READ /Volumes/AI_Workspace (EPERM) — the iris_studio repo + every job script live there, so the WHOLE fleet is down. Fix (~2 min): System Settings ▸ Privacy & Security ▸ Full Disk Access ▸ remove + re-add /opt/homebrew/bin/claude. A \`brew upgrade claude-code\` revokes it. Durable fix: relocate iris_studio to the internal disk (DQ-17)."$'\n'

    # Escalation = a dimension is broken now that was NOT in the prior outage.
    escalation="no"
    for d in $effective_bad; do _has "$prev_bad" "$d" || escalation="yes"; done

    if [ "$prev_status" != "bad" ] || [ "$escalation" = "yes" ]; then
        alert "🔴 Claude CLI health canary FAILED (${reason}). Headless launchd jobs are blocked.
${body}Time: ${TS}
The 30-min auto-retry will run the affected jobs automatically once this clears — no manual re-run needed."
        write_state "bad" "$NOW_EPOCH" "$reason"
        log "ALERT (${reason}) prev='${prev_reason:-ok}' escalation=$escalation auth=$auth_state vault=$vault_state mount=$mount_state"
    elif [ $((NOW_EPOCH - prev_alert_epoch)) -ge "$RE_ALERT_THROTTLE_SECS" ]; then
        alert "🔴 STILL FAILING — Claude CLI health canary (${reason}) remains broken since the last alert.
${body}Time: ${TS}"
        write_state "bad" "$NOW_EPOCH" "$reason"
        log "STILL bad (${reason}) — throttled re-alert sent. auth=$auth_state vault=$vault_state mount=$mount_state"
    else
        # Preserve the FIRST alert's epoch so the throttle measures from it.
        write_state "bad" "$prev_alert_epoch" "$reason"
        log "STILL bad (${reason}) — within throttle, silent. auth=$auth_state vault=$vault_state mount=$mount_state"
    fi
    exit 1
fi

# ---- Nothing is effectively bad ----
if [ "$prev_status" = "bad" ]; then
    # Every previously-broken dimension is now CONFIRMED ok → recovery.
    alert "✅ RECOVERED — Claude CLI health canary is green again (auth=$auth_state, vault=$vault_state, mount=$mount_state) as of ${TS}. Headless jobs can run; the auto-retry sweep will drain anything that queued during the outage."
    write_state "ok" "0" ""
    log "RECOVERED from '${prev_reason}'. auth=$auth_state vault=$vault_state mount=$mount_state"
    exit 0
fi

# prev_status was ok and nothing is bad.
if [ "$auth_state" = "ok" ] && [ "$vault_state" = "ok" ] && [ "$mount_state" = "ok" ]; then
    write_state "ok" "0" ""
    log "healthy (auth=ok vault=ok mount=ok) — silent."
    exit 0
fi
# Healthy history, nothing bad, but a probe was inconclusive — a transient blip.
# Stay healthy + silent (a real failure prints its error → classified "bad").
write_state "ok" "0" ""
log "INCONCLUSIVE while healthy — auth=$auth_state vault=$vault_state mount=$mount_state (no alert). auth_out=$(printf '%s' "$AUTH_OUT" | tr '\n' ' ' | cut -c1-160)"
exit 2
