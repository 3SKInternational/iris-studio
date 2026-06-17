#!/bin/bash
set -euo pipefail

VAULT="/Users/steve/Documents/3SK/outputs"
REMOTE="gdrive:3SK_International"
EXCLUDES="/Volumes/AI_Workspace/iris_studio/scripts/drive-sync.excludes"
LOG="/Users/steve/iris_studio/logs/drive-sync.log"
NOTIFY="/Volumes/AI_Workspace/iris_studio/scripts/notify.sh"
RCLONE="/opt/homebrew/bin/rclone"

# --- Delete guardrail (post-2026-05-27 data-loss scare) ---------------------
# `rclone sync` MIRRORS: it deletes anything on Drive that's missing locally.
# So a corrupted/unmounted/half-deleted local vault would PROPAGATE that loss to
# the Drive backup on the next run. To prevent that, we DRY-RUN first, count how
# many deletes the real sync would make, and ABORT (with a Telegram alert) before
# touching Drive if the count exceeds MAX_DELETE — the cloud copy stays intact and
# Steve investigates. Tune via env: DRIVE_SYNC_MAX_DELETE=<n>. Legitimate large
# cleanups: re-run once with DRIVE_SYNC_ALLOW_DELETES=1 to bypass the count gate.
MAX_DELETE="${DRIVE_SYNC_MAX_DELETE:-60}"
# A known anchor file that MUST exist in a healthy vault. Its absence means the
# vault dir is empty / unmounted / wrong — exactly the state we must NOT sync from.
ANCHOR="$VAULT/CLAUDE.md"

echo "=== drive-sync start $(date -Iseconds) ==="

alert() {
    # Best-effort Telegram alert; never let a notify failure mask our own exit.
    "$NOTIFY" "$1" 2>/dev/null || true
}

# Build the sync command once so the dry-run and the real run can't drift.
sync_cmd() {
    "$RCLONE" sync "$VAULT" "$REMOTE" \
        --exclude-from "$EXCLUDES" \
        --transfers 4 \
        --checkers 8 \
        --fast-list \
        --stats 30s \
        --stats-one-line \
        --log-level INFO \
        "$@"
}

if ! "$RCLONE" listremotes 2>/dev/null | grep -q "^gdrive:"; then
    echo "ERROR: rclone gdrive: remote not configured. Run on the Mac Mini Terminal: rclone config"
    alert "🔴 drive-sync ABORTED — rclone gdrive: remote not configured (run \`rclone config\` on the Mini)."
    echo "Aborting sync."
    exit 1
fi

# Guard 0: source sanity. If the vault dir or its anchor file is missing, the
# local copy is in a bad state — refuse to sync (would mirror an empty/broken
# tree onto Drive and delete the backup).
if [ ! -d "$VAULT" ] || [ ! -f "$ANCHOR" ]; then
    echo "ERROR: vault sanity check failed — missing dir or anchor ($ANCHOR)."
    alert "🔴 drive-sync ABORTED — vault sanity check failed (missing $VAULT or anchor CLAUDE.md). Local vault may be unmounted/corrupt; Drive backup left untouched. Investigate before re-running."
    exit 1
fi

# Guard 1: dry-run delete-count gate (skippable for intentional big cleanups).
if [ "${DRIVE_SYNC_ALLOW_DELETES:-0}" = "1" ]; then
    echo "drive-sync: DRIVE_SYNC_ALLOW_DELETES=1 set — bypassing delete-count gate (intentional cleanup)."
    alert "⚠️ drive-sync: delete-count gate BYPASSED this run (DRIVE_SYNC_ALLOW_DELETES=1)."
else
    DRYRUN_OUT="$(mktemp -t drive-sync-dryrun.XXXXXX)"
    trap 'rm -f "$DRYRUN_OUT"' EXIT
    # --dry-run logs each would-be deletion as: "<path>: Skipped delete as --dry-run is set".
    # rc!=0 here means rclone itself failed to even plan the sync (auth/network/etc).
    if ! sync_cmd --dry-run >"$DRYRUN_OUT" 2>&1; then
        echo "ERROR: drive-sync dry-run failed:"
        cat "$DRYRUN_OUT"
        alert "🔴 drive-sync ABORTED — dry-run failed (rclone could not plan the sync). Tail:
$(tail -n 4 "$DRYRUN_OUT")"
        exit 1
    fi
    PLANNED_DELETES="$(grep -c "Skipped delete as --dry-run is set" "$DRYRUN_OUT" || true)"
    PLANNED_DELETES="${PLANNED_DELETES:-0}"
    echo "drive-sync: dry-run plans $PLANNED_DELETES delete(s) on Drive (threshold $MAX_DELETE)."
    if [ "$PLANNED_DELETES" -gt "$MAX_DELETE" ]; then
        alert "🔴 drive-sync ABORTED — would delete $PLANNED_DELETES file(s) on Drive (> $MAX_DELETE threshold). This looks like a mass local deletion; Drive backup left untouched.
If intentional: re-run with DRIVE_SYNC_ALLOW_DELETES=1. Else investigate the local vault first."
        echo "Aborting sync — delete count $PLANNED_DELETES exceeds MAX_DELETE $MAX_DELETE."
        exit 1
    fi
fi

# Real sync. --max-delete is a hard backstop in case the live delta is larger
# than the dry-run predicted (e.g. churn between the two passes). Wrap it so a
# backstop trip (or any rclone failure) ALWAYS alerts Telegram rather than dying
# silently under `set -e` — the canonical alert channel must see every failure.
rc=0
sync_cmd --max-delete "$MAX_DELETE" || rc=$?
if [ "$rc" -ne 0 ]; then
    alert "🔴 drive-sync FAILED — rclone sync exited $rc (possible --max-delete $MAX_DELETE backstop trip, or auth/network error). Drive may be partially synced; check $LOG."
    echo "ERROR: drive-sync real sync failed with rc=$rc."
    exit "$rc"
fi

echo "=== drive-sync done $(date -Iseconds) ==="
