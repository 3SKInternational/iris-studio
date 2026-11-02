#!/bin/bash
set -euo pipefail

LOGDIR="${LOGDIR:-/Users/steve/iris_studio/logs}"  # overridable for tests
LOG="$LOGDIR/iris.err.log"
TS="$(date +%Y%m%d_%H%M%S)"
KEEP=4

if [ -s "$LOG" ]; then
    /usr/bin/gzip -c "$LOG" > "$LOG.$TS.gz"

    : > "$LOG"

    ls -1t "$LOGDIR"/iris.err.log.*.gz 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r OLD; do
        /bin/rm -f "$OLD"
    done

    echo "$(date -Iseconds) rotated iris.err.log -> iris.err.log.$TS.gz (kept last $KEEP)"
fi

# claude-remote logs (A-118): nothing rotated them and both reached 379 MB of
# "Reconnecting" spinner lines. Both writers (tee -a, launchd stdout) hold the files
# open in append mode, so cut them in place (same inode); never mv a live log.
REMOTE_MAX=$((50 * 1024 * 1024))
R="$LOGDIR/claude-remote.log"
if [ -f "$R" ] && [ "$(stat -f %z "$R")" -gt "$REMOTE_MAX" ]; then
    # remote-url.sh greps this file for the last remote-control URL, which is printed
    # only on a reconnect and can sit more than 5,000 lines back. Keep it. -a: one NUL
    # near the top makes BSD grep print "Binary file matches" instead of the URL.
    URL="$(grep -aoE 'https://claude\.ai/code\?environment=[a-zA-Z0-9_]+' "$R" | tail -1 || true)"
    tail -n 5000 "$R" > "$R.tmp"
    cat "$R.tmp" > "$R"
    rm -f "$R.tmp"
    if [ -n "$URL" ] && ! grep -qF "$URL" "$R"; then
        echo "$URL" >> "$R"
    fi
    echo "$(date -Iseconds) trimmed claude-remote.log to its last 5000 lines"
fi
S="$LOGDIR/claude-remote.stdout.log"  # a tee copy of the same output; nothing reads it
if [ -f "$S" ] && [ "$(stat -f %z "$S")" -gt "$REMOTE_MAX" ]; then
    : > "$S"
    echo "$(date -Iseconds) emptied claude-remote.stdout.log"
fi
