#!/bin/bash
set -euo pipefail

VAULT="/Users/steve/Documents/3SK/outputs"
REMOTE="gdrive:3SK_International"
EXCLUDES="/Volumes/AI_Workspace/iris_studio/scripts/drive-sync.excludes"
LOG="/Users/steve/iris_studio/logs/drive-sync.log"

echo "=== drive-sync start $(date -Iseconds) ==="

if ! /opt/homebrew/bin/rclone listremotes 2>/dev/null | grep -q "^gdrive:"; then
    echo "ERROR: rclone gdrive: remote not configured. Run on the Mac Mini Terminal: rclone config"
    echo "Aborting sync."
    exit 1
fi

/opt/homebrew/bin/rclone sync "$VAULT" "$REMOTE" \
    --exclude-from "$EXCLUDES" \
    --transfers 4 \
    --checkers 8 \
    --fast-list \
    --stats 30s \
    --stats-one-line \
    --log-level INFO

echo "=== drive-sync done $(date -Iseconds) ==="
