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
#
# Reads TELEGRAM_BOT_TOKEN + IRIS_TELEGRAM_USER_IDS (first id = chat target) from
# the gitignored .env next to iris.py. No secrets live in this file.
#
# Exit codes: 0 = delivered (Telegram ok:true), 1 = config/usage error,
#             2 = Telegram API rejected the send.

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

RESP=$(curl -s --max-time 20 \
    -X POST "https://api.telegram.org/bot${TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${CHAT_ID}" \
    --data-urlencode "text=${MSG}" \
    --data-urlencode "disable_web_page_preview=true")

if printf '%s' "$RESP" | grep -q '"ok":true'; then
    echo "notify.sh: delivered to chat ${CHAT_ID}"
    exit 0
else
    echo "notify.sh ERROR: Telegram API rejected the send: $RESP" >&2
    exit 2
fi
