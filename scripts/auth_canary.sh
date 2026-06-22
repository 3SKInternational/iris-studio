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
#   2. VAULT (secondary proxy) — can a scheduled job READ the vault at all? This
#            reads a vault file (on the internal Data volume) from this launchd
#            job's own context. It catches Full Disk Access being broadly revoked
#            for scheduled jobs (which a `brew upgrade claude-code` CAN cause, since
#            FDA is keyed to the version-pinned Caskroom path) — i.e. the vault
#            becomes unreadable from a background job. HONEST SCOPE: (a) macOS TCC is
#            per-responsible-process, so a revocation that hits ONLY claude's exact
#            binary path while this bash context still reads the vault would NOT be
#            caught here; and (b) the probe file lives on the internal Data volume,
#            so an UNMOUNT of a separate volume (e.g. AI_Workspace/X9) is NOT what
#            this detects. This is a best-effort proxy, not a claude-binary-specific
#            TCC assertion. The AUTH probe is the primary signal; this is a guard
#            against the broader "background jobs can't read the vault" class.
#
# Why this exists: before this canary, both modes were detected only REACTIVELY —
# a real scheduled routine had to fire and fail, which (a) could be hours after the
# token expired on a quiet day and (b) surfaced as confusing "agent failed to fire"
# alerts rather than a clean "re-auth needed" signal. (Root-caused live 2026-06-22:
# a token expired ~06:00 and the only symptom was competitor-tripwire +
# sponsor-tracker red alerts.) This job runs every few hours and emits ONE
# purpose-built Telegram alert the moment either mode breaks, and a single
# "recovered" ping when it clears.
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
#   NOTIFY_BIN   path to notify.sh  (default: alongside this script)
#   CLAUDE_BIN   path to claude     (default: /opt/homebrew/bin/claude)
#   STATE_FILE   path to state file (default: ~/iris_studio/state/auth_canary.state)
#
# Exit codes: 0 = healthy, 1 = a definite failure is active (auth and/or vault),
#             2 = inconclusive this run (network/transient — NOT alerted).

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NOTIFY="${NOTIFY_BIN:-$SCRIPT_DIR/notify.sh}"
CLAUDE="${CLAUDE_BIN:-/opt/homebrew/bin/claude}"
VAULT="/Users/steve/Documents/3SK/outputs"
VAULT_PROBE="${VAULT_PROBE:-$VAULT/CLAUDE.md}"   # a file whose read proves vault access (overridable for self-tests)
LOG="/Users/steve/iris_studio/logs/claude-code-auth-canary.log"
STATE_FILE="${STATE_FILE:-/Users/steve/iris_studio/state/auth_canary.state}"
LOCK_DIR="$(dirname "$STATE_FILE")/auth_canary.lock"

AUTH_TIMEOUT=60                          # seconds to allow the claude probe
RE_ALERT_THROTTLE_SECS=$((6 * 3600))     # while broken, re-ping at most every 6h
TS="$(date '+%Y-%m-%d %H:%M %Z')"
NOW_EPOCH="$(date +%s)"

mkdir -p "$(dirname "$STATE_FILE")" "$(dirname "$LOG")" 2>/dev/null

log() { echo "$TS auth_canary: $*" >> "$LOG"; }
alert() { "$NOTIFY" "$1" || log "WARN: notify failed (alert not delivered)"; }
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
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    log "another instance holds the lock — skipping this invocation."
    exit 0
fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null' EXIT

# --- State file format: STATUS|LAST_ALERT_EPOCH|REASON ----------------------
# STATUS = ok|bad. LAST_ALERT_EPOCH = epoch of the last Telegram ping about the
# current outage (0 if none). REASON = '+'-joined set of currently-broken
# dimensions (auth / vault), e.g. "auth", "vault", "auth+vault".
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

# --- Probe 2: VAULT readability from this job's context ---------------------
# Read one byte of a known vault file. A denial ("Operation not permitted"/EPERM/
# "Permission denied") → "bad". A merely-absent probe file (volume unmounted /
# mid-sync) is a DIFFERENT, separately-visible condition → "inconclusive", not a
# manufactured failure. (Scope caveat in the header: this tests THIS job's access,
# a proxy — not claude's exact-binary TCC grant.)
vault_state="ok"
if [ -e "$VAULT_PROBE" ]; then
    VAULT_OUT="$(head -c1 "$VAULT_PROBE" 2>&1 >/dev/null)"
    if printf '%s' "$VAULT_OUT" | grep -qiE 'Operation not permitted|EPERM|Permission denied'; then
        vault_state="bad"
    fi
else
    vault_state="inconclusive"
fi

# --- Resolve per-dimension health into an effective-bad set -----------------
cur_ok=""; cur_bad=""
[ "$auth_state"  = "ok"  ] && cur_ok="$cur_ok auth"
[ "$vault_state" = "ok"  ] && cur_ok="$cur_ok vault"
[ "$auth_state"  = "bad" ] && cur_bad="$cur_bad auth"
[ "$vault_state" = "bad" ] && cur_bad="$cur_bad vault"

# effective_bad = previously-bad dims NOT yet confirmed-ok again, plus newly-bad
# dims. A dimension that probed "inconclusive" this run is neither confirmed-ok nor
# newly-bad, so a prior bad state for it PERSISTS (no false recovery) and a prior ok
# state stays ok (no false alarm). This decouples recovery per-dimension: an
# inconclusive vault probe can't block an auth recovery, and vice-versa.
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
    _has "$effective_bad" "vault" && body="${body}• VAULT: a scheduled job can't read the vault (EPERM/permission denied). Re-grant Full Disk Access to the background job's binary (System Settings ▸ Privacy & Security) — a \`brew upgrade claude-code\` can revoke it by moving to a new version-pinned path."$'\n'

    # Escalation = a dimension is broken now that was NOT in the prior outage.
    escalation="no"
    for d in $effective_bad; do _has "$prev_bad" "$d" || escalation="yes"; done

    if [ "$prev_status" != "bad" ] || [ "$escalation" = "yes" ]; then
        alert "🔴 Claude CLI health canary FAILED (${reason}). Headless launchd jobs are blocked.
${body}Time: ${TS}
The 30-min auto-retry will run the affected jobs automatically once this clears — no manual re-run needed."
        write_state "bad" "$NOW_EPOCH" "$reason"
        log "ALERT (${reason}) prev='${prev_reason:-ok}' escalation=$escalation auth=$auth_state vault=$vault_state"
    elif [ $((NOW_EPOCH - prev_alert_epoch)) -ge "$RE_ALERT_THROTTLE_SECS" ]; then
        alert "🔴 STILL FAILING — Claude CLI health canary (${reason}) remains broken since the last alert.
${body}Time: ${TS}"
        write_state "bad" "$NOW_EPOCH" "$reason"
        log "STILL bad (${reason}) — throttled re-alert sent. auth=$auth_state vault=$vault_state"
    else
        # Preserve the FIRST alert's epoch so the throttle measures from it.
        write_state "bad" "$prev_alert_epoch" "$reason"
        log "STILL bad (${reason}) — within throttle, silent. auth=$auth_state vault=$vault_state"
    fi
    exit 1
fi

# ---- Nothing is effectively bad ----
if [ "$prev_status" = "bad" ]; then
    # Every previously-broken dimension is now CONFIRMED ok → recovery.
    alert "✅ RECOVERED — Claude CLI health canary is green again (auth=$auth_state, vault=$vault_state) as of ${TS}. Headless jobs can run; the auto-retry sweep will drain anything that queued during the outage."
    write_state "ok" "0" ""
    log "RECOVERED from '${prev_reason}'. auth=$auth_state vault=$vault_state"
    exit 0
fi

# prev_status was ok and nothing is bad.
if [ "$auth_state" = "ok" ] && [ "$vault_state" = "ok" ]; then
    write_state "ok" "0" ""
    log "healthy (auth=ok vault=ok) — silent."
    exit 0
fi
# Healthy history, nothing bad, but a probe was inconclusive — a transient blip.
# Stay healthy + silent (a real failure prints its error → classified "bad").
write_state "ok" "0" ""
log "INCONCLUSIVE while healthy — auth=$auth_state vault=$vault_state (no alert). auth_out=$(printf '%s' "$AUTH_OUT" | tr '\n' ' ' | cut -c1-160)"
exit 2
