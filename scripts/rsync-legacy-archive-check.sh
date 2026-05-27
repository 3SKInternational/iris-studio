#!/bin/bash
# rsync-legacy-archive-check.sh
#
# Runs daily after the Syncthing pairing on 2026-05-27. On or after the
# trigger date (2026-06-03), checks if Syncthing has been healthy for the
# preceding 24 hours, and if so, archives the legacy rsync scripts in
# `post-transfer-additions/` to `post-transfer-additions/ARCHIVED-rsync/`
# and disables itself (removes its own launchd plist).
#
# Pre-trigger date: no-op (exits silently).
# Post-trigger date + Syncthing unhealthy: no-op (writes a flag note to INBOX).
# Post-trigger date + Syncthing healthy: archive + self-disable.

set -euo pipefail

TRIGGER_DATE="2026-06-03"
TODAY=$(date +%Y-%m-%d)
VAULT="/Users/steve/Documents/3SK/outputs"
PTA="$VAULT/post-transfer-additions"
ARCHIVE_DIR="$PTA/ARCHIVED-rsync"
LOG="/Users/steve/iris_studio/logs/rsync-cleanup.log"
PLIST="$HOME/Library/LaunchAgents/com.iris.rsync-legacy-archive-check.plist"
INBOX="$VAULT/INBOX.md"

# --- pre-trigger date: silently exit ---
if [[ "$TODAY" < "$TRIGGER_DATE" ]]; then
    echo "$(date -Iseconds) pre-trigger ($TODAY < $TRIGGER_DATE); skip" >> "$LOG"
    exit 0
fi

# --- past trigger date: check Syncthing health on the Mini ---
API_KEY=$(grep -oE "<apikey>[^<]+" "$HOME/Library/Application Support/Syncthing/config.xml" 2>/dev/null | sed 's/<apikey>//' | head -1)

if [ -z "$API_KEY" ]; then
    echo "$(date -Iseconds) ERROR: cannot read Syncthing API key; abort" >> "$LOG"
    exit 1
fi

FOLDER_STATE=$(curl -s --max-time 5 -H "X-API-Key: $API_KEY" "http://100.118.108.65:8384/rest/db/status?folder=3sk-vault" 2>&1)
AIR_COMPLETION=$(curl -s --max-time 5 -H "X-API-Key: $API_KEY" "http://100.118.108.65:8384/rest/db/completion?folder=3sk-vault&device=6GUK7NW-KP726GA-65EDWYX-KXZ6EXE-GATFIR5-X6ZPFIU-4QQJ5IP-TBUW4QV" 2>&1)

# Parse: state should be idle, errors=0, Air completion=100
ERRORS=$(echo "$FOLDER_STATE" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('errors',-1))" 2>/dev/null || echo "-1")
STATE=$(echo "$FOLDER_STATE" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('state','unknown'))" 2>/dev/null || echo "unknown")
COMPLETION=$(echo "$AIR_COMPLETION" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('completion',0))" 2>/dev/null || echo "0")

HEALTHY=true
REASON=""

if [ "$ERRORS" != "0" ]; then HEALTHY=false; REASON="folder errors=$ERRORS"; fi
if [ "$STATE" != "idle" ]; then HEALTHY=false; REASON="${REASON:+$REASON; }state=$STATE"; fi
# Use bc for float comparison; require Air >= 99.5% (allow rounding)
if ! awk -v c="$COMPLETION" 'BEGIN { exit !(c >= 99.5) }'; then HEALTHY=false; REASON="${REASON:+$REASON; }air_completion=$COMPLETION"; fi

if ! $HEALTHY; then
    echo "$(date -Iseconds) past trigger but UNHEALTHY: $REASON; archive deferred" >> "$LOG"
    # Surface in INBOX (idempotent: only insert if not already there today)
    MARK="🔁 rsync-legacy archive deferred: $TODAY (Syncthing not healthy yet — $REASON)"
    if ! grep -qF "$MARK" "$INBOX" 2>/dev/null; then
        # Insert after the first "## 🧍 THIS WEEK" header
        awk -v line="- $MARK" '
            /^## 🧍 THIS WEEK/ && !done { print; print ""; print line; done=1; next }
            { print }
        ' "$INBOX" > "$INBOX.tmp" && mv "$INBOX.tmp" "$INBOX"
    fi
    exit 0
fi

# --- healthy + past trigger: archive ---
echo "$(date -Iseconds) HEALTHY past trigger; archiving rsync scripts" >> "$LOG"

mkdir -p "$ARCHIVE_DIR"
if ls "$PTA"/sync-*.sh 1>/dev/null 2>&1; then
    mv "$PTA"/sync-*.sh "$ARCHIVE_DIR/"
    echo "$(date -Iseconds) archived sync-*.sh to $ARCHIVE_DIR" >> "$LOG"
fi
if [ -f "$PTA/deploy-iris.sh" ]; then
    mv "$PTA/deploy-iris.sh" "$ARCHIVE_DIR/"
    echo "$(date -Iseconds) archived deploy-iris.sh to $ARCHIVE_DIR" >> "$LOG"
fi

# Write a note in the archive dir explaining what happened
cat > "$ARCHIVE_DIR/README.md" <<'EOF'
# Legacy rsync scripts — archived 2026-06-03+

These scripts were the pre-Syncthing manual sync mechanism between the Mac Mini
and Mac Air. They were superseded on 2026-05-27 when Syncthing was deployed
with continuous bidirectional sync of the `3SK Vault` folder.

The `com.iris.rsync-legacy-archive-check` launchd job watched the Syncthing
health for the week following the pairing. Once the system passed a 24-hr
clean state (folder idle, no errors, Air at 100% completion), this job
moved the scripts here and disabled itself.

Restoration: if Syncthing ever needs to be backed out, copy these scripts
back to `post-transfer-additions/` and run as before. The Air's TCC FDA
grant on `/usr/libexec/sshd-keygen-wrapper` would also need to be re-checked.
EOF

# Append to daily note + bridge file
DAILY="$VAULT/_Iris_Memory/Daily/$(date +%Y-%m-%d).md"
if [ -f "$DAILY" ]; then
    # Append under Claude Code intra-day log if section exists; otherwise tack to end
    HHMM=$(date +%H:%M)
    LINE="- $HHMM — rsync-legacy-archive routine fired: Syncthing healthy 24h+, archived sync-*.sh + deploy-iris.sh to post-transfer-additions/ARCHIVED-rsync/; self-disabled this launchd job."
    if grep -q "## 🤖 Claude Code intra-day log" "$DAILY"; then
        awk -v line="$LINE" '
            /^## 🤖 Claude Code intra-day log/ { print; in_section=1; next }
            in_section && /^##/ && !/^## 🤖 Claude Code/ { print line; print ""; in_section=0 }
            { print }
            END { if (in_section) print line }
        ' "$DAILY" > "$DAILY.tmp" && mv "$DAILY.tmp" "$DAILY"
    fi
fi

# Self-disable: bootout + remove plist
launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
rm -f "$PLIST"
echo "$(date -Iseconds) self-disabled: removed $PLIST" >> "$LOG"

exit 0
