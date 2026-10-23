#!/bin/bash
# notify.sh — push a one-off alert to Steve's Telegram via the Iris bot.
#
# This is the canonical alert channel for the whole iris_studio operation
# (Steve's standing directive 2026-06-15: "warn me through the daemon for any
# and all errors, failures or alerts — keep me updated on all dealings there").
#
# It is DECOUPLED from the running iris.py daemon on purpose: it talks straight
# to the Telegram Bot API, so it still delivers even when the daemon is down,
# mid-restart, or is itself the thing that failed. Any launchd job, cron, script,
# or Claude Code session can call it.
#
# Usage:
#   ./notify.sh "message text"
#   echo "message text" | ./notify.sh
#   ./notify.sh "🔴 nightly job failed: <detail>"
#   ./notify.sh --photo /path/to/image.png "optional caption"   # send an image
#
# In --photo mode the image is uploaded inline (Telegram sendPhoto) with the
# remaining args as an optional caption — used e.g. to push a finished contact
# sheet so a billed render batch can be eyeballed on the phone, not just on the
# Mac. The default (no --photo) text path is unchanged.
#
# Reads TELEGRAM_BOT_TOKEN + IRIS_TELEGRAM_USER_IDS (first id = chat target) from
# the gitignored .env next to iris.py. No secrets live in this file.
#
# Repeat check (opt-in, A-96): NOTIFY_ONCE=<key> ./notify.sh "..." skips the
# send when the same key was delivered in the last 7 days. The key is recorded
# only after Telegram returns ok:true, so a failed send is never suppressed.
# State: ~/iris_studio/state/notify_once.tsv (epoch<TAB>key). Unset = unchanged.
# Content pushes only — never set it on a failure or job-health alert.
#
# Exit codes: 0 = delivered (Telegram ok:true), or skipped because NOTIFY_ONCE's
#             key was already delivered in the last 7 days,
#             1 = config/usage error, 2 = Telegram API rejected the send.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/../.env"

if [ ! -f "$ENV_FILE" ]; then
    echo "notify.sh ERROR: .env not found at $ENV_FILE" >&2
    exit 1
fi

# Strip surrounding single/double quotes and whitespace — .env values may be quoted.
unquote() { sed -e 's/^[[:space:]]*["'\'']\{0,1\}//' -e 's/["'\'']\{0,1\}[[:space:]]*$//'; }

TOKEN=$(grep -E '^TELEGRAM_BOT_TOKEN=' "$ENV_FILE" | head -1 | cut -d= -f2- | unquote)
CHAT_ID=$(grep -E '^IRIS_TELEGRAM_USER_IDS=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr ',' '\n' | head -1 | unquote | tr -d '[:space:]')

if [ -z "${TOKEN:-}" ] || [ -z "${CHAT_ID:-}" ]; then
    echo "notify.sh ERROR: TELEGRAM_BOT_TOKEN or IRIS_TELEGRAM_USER_IDS missing in .env" >&2
    exit 1
fi

# --- NOTIFY_ONCE repeat check (see header) ----------------------------------
ONCE_KEY=$(printf '%s' "${NOTIFY_ONCE:-}" | tr -d '\t\n\r')
ONCE_FILE="$HOME/iris_studio/state/notify_once.tsv"
ONCE_CUTOFF=$(( $(date +%s) - 7*86400 ))
if [ -n "$ONCE_KEY" ] && [ -f "$ONCE_FILE" ] && \
   ONCE_KEY="$ONCE_KEY" awk -F'\t' -v c="$ONCE_CUTOFF" '$2==ENVIRON["ONCE_KEY"] && $1>=c {f=1} END {exit !f}' "$ONCE_FILE"; then
    echo "notify.sh: skipped, key '$ONCE_KEY' already delivered in the last 7 days"
    exit 0
fi
# Called only after ok:true. Drops entries older than 7 days so the file stays small.
record_once() {
    [ -n "$ONCE_KEY" ] || return 0
    mkdir -p "$(dirname "$ONCE_FILE")"
    { [ -f "$ONCE_FILE" ] && awk -F'\t' -v c="$ONCE_CUTOFF" '$1>=c' "$ONCE_FILE"
      printf '%s\t%s\n' "$(date +%s)" "$ONCE_KEY"; } > "$ONCE_FILE.tmp" && mv "$ONCE_FILE.tmp" "$ONCE_FILE"
}

# --- Photo mode: notify.sh --photo /path/to.png "optional caption" -----------
if [ "${1:-}" = "--photo" ]; then
    PHOTO="${2:-}"
    if [ -z "$PHOTO" ]; then
        echo "notify.sh ERROR: --photo needs a file path" >&2
        exit 1
    fi
    if [ ! -f "$PHOTO" ]; then
        echo "notify.sh ERROR: --photo file not found: $PHOTO" >&2
        exit 1
    fi
    shift 2
    CAPTION="$*"   # may be empty — Telegram allows a caption-less photo
    # Multipart upload (sendPhoto). Larger --max-time than text since we're
    # uploading a few MB. `|| RESP=""` keeps a curl-level failure on the error path.
    # --form-string for text fields so a caption starting with '@' or '<' is sent
    # literally, not interpreted by curl as a file reference; -F only for the upload.
    RESP=$(curl -s --max-time 60 \
        -X POST "https://api.telegram.org/bot${TOKEN}/sendPhoto" \
        --form-string "chat_id=${CHAT_ID}" \
        -F "photo=@${PHOTO}" \
        --form-string "caption=${CAPTION}") || RESP=""

    if printf '%s' "$RESP" | grep -q '"ok":true'; then
        echo "notify.sh: photo delivered to chat ${CHAT_ID}"
        record_once || echo "notify.sh WARN: delivered, but could not record NOTIFY_ONCE key" >&2
        exit 0
    else
        echo "notify.sh ERROR: Telegram API rejected the photo send: $RESP" >&2
        exit 2
    fi
fi

# Message from args, or stdin if no args given.
if [ "$#" -gt 0 ]; then
    MSG="$*"
else
    MSG="$(cat)"
fi

if [ -z "${MSG//[[:space:]]/}" ]; then
    echo "notify.sh ERROR: empty message" >&2
    exit 1
fi

# `|| RESP=""` so a curl-level failure (DNS, no network, --max-time timeout)
# falls through to the explicit error branch below instead of aborting the
# script with curl's exit code under `set -e`.
RESP=$(curl -s --max-time 20 \
    -X POST "https://api.telegram.org/bot${TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${CHAT_ID}" \
    --data-urlencode "text=${MSG}" \
    --data-urlencode "disable_web_page_preview=true") || RESP=""

if printf '%s' "$RESP" | grep -q '"ok":true'; then
    echo "notify.sh: delivered to chat ${CHAT_ID}"
    record_once || echo "notify.sh WARN: delivered, but could not record NOTIFY_ONCE key" >&2
    exit 0
else
    echo "notify.sh ERROR: Telegram API rejected the send: $RESP" >&2
    exit 2
fi
