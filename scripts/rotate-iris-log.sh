#!/bin/bash
set -euo pipefail

LOGDIR="/Users/steve/iris_studio/logs"
LOG="$LOGDIR/iris.err.log"
TS="$(date +%Y%m%d_%H%M%S)"
KEEP=4

if [ ! -f "$LOG" ]; then
    exit 0
fi

if [ ! -s "$LOG" ]; then
    exit 0
fi

/usr/bin/gzip -c "$LOG" > "$LOG.$TS.gz"

: > "$LOG"

ls -1t "$LOGDIR"/iris.err.log.*.gz 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r OLD; do
    /bin/rm -f "$OLD"
done

echo "$(date -Iseconds) rotated iris.err.log -> iris.err.log.$TS.gz (kept last $KEEP)"
