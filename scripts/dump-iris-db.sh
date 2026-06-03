#!/bin/bash
set -euo pipefail

# A-6: daily iris.db SQLite dump.
# Defense-in-depth for conversation memory + dispatcher state + expense rows.
# Time Machine snapshots the live file; this gives a portable SQL form
# safe against SQLite-internal corruption and easy to restore without TM.

DB="/Volumes/AI_Workspace/iris_studio/iris.db"
BACKUP_DIR="/Volumes/AI_Workspace/iris_studio/backups"
DATE="$(date +%Y-%m-%d)"
OUT="$BACKUP_DIR/iris.db.$DATE.sql.gz"
SNAPSHOT="$(mktemp -t iris.db.snapshot.XXXXXX)"
trap 'rm -f "$SNAPSHOT"' EXIT

if [ ! -f "$DB" ]; then
    echo "$(date -Iseconds) ERROR: $DB missing" >&2
    exit 1
fi

mkdir -p "$BACKUP_DIR"

# Use SQLite's online backup API for a consistent snapshot. Safe against
# the live daemon's writes; .dump direct on the live DB can hit
# "database is locked" since iris.db runs in `delete` journal mode.
/usr/bin/sqlite3 "$DB" ".backup '$SNAPSHOT'"

# Dump the snapshot to portable SQL + gzip.
/usr/bin/sqlite3 "$SNAPSHOT" ".dump" | /usr/bin/gzip > "$OUT"

# Prune dumps older than 30 days.
/usr/bin/find "$BACKUP_DIR" -name 'iris.db.*.sql.gz' -mtime +30 -delete 2>/dev/null || true

BYTES="$(/usr/bin/stat -f %z "$OUT")"
KEPT="$(/bin/ls -1 "$BACKUP_DIR"/iris.db.*.sql.gz 2>/dev/null | wc -l | tr -d ' ')"
echo "$(date -Iseconds) dump iris.db -> $OUT ($BYTES bytes, $KEPT total kept)"
