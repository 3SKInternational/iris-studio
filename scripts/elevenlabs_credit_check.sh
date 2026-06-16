#!/bin/bash
# elevenlabs_credit_check.sh — check ElevenLabs character balance and warn
# Steve's Telegram (via notify.sh) if it's running low.
#
# This is the pre-flight primitive for the (not-yet-built) VO pipeline: call it
# before a synthesis run so a near-empty quota alerts Steve instead of failing
# the run blind. Also safe to run standalone or on a schedule. Per Steve's
# 2026-06-15 directive: warn me through the daemon for any/all alerts.
#
# Requires the key in .env to have the `user_read` permission (enabled
# 2026-06-15). Reads ELEVENLABS_API_KEY from the gitignored .env next to iris.py.
#
# Usage:
#   ./elevenlabs_credit_check.sh [threshold_chars]
#     threshold_chars  warn if remaining < this (default 5000; env THRESHOLD also works)
#
# Exit: 0 = checked ok (whether or not it warned), 2 = API/parse error.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/../.env"
NOTIFY="$SCRIPT_DIR/notify.sh"
THRESHOLD="${1:-${THRESHOLD:-5000}}"

unquote() { sed -e 's/^[[:space:]]*["'\'']\{0,1\}//' -e 's/["'\'']\{0,1\}[[:space:]]*$//'; }

if [ ! -f "$ENV_FILE" ]; then
    echo "elevenlabs_credit_check ERROR: .env not found at $ENV_FILE" >&2
    exit 2
fi

KEY=$(grep -E '^ELEVENLABS_API_KEY=' "$ENV_FILE" | head -1 | cut -d= -f2- | unquote)
if [ -z "${KEY:-}" ]; then
    echo "elevenlabs_credit_check ERROR: ELEVENLABS_API_KEY missing in .env" >&2
    exit 2
fi

RESP=$(curl -s --max-time 20 https://api.elevenlabs.io/v1/user/subscription \
    -H "xi-api-key: $KEY")

# Parse with python3 (no jq dependency). Emits "tier used limit" or "ERR".
read -r TIER USED LIMIT < <(printf '%s' "$RESP" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
    print(d["tier"], int(d["character_count"]), int(d["character_limit"]))
except Exception:
    print("ERR 0 0")
')

# Guard: if python3 produced no/garbage output, USED/LIMIT may be empty/non-numeric.
# Without this, the math below would compute remaining=0 and fire a bogus LOW alert.
if ! [[ "${USED:-}" =~ ^[0-9]+$ && "${LIMIT:-}" =~ ^[0-9]+$ ]]; then
    TIER="ERR"
fi

if [ "$TIER" = "ERR" ]; then
    MSG="🔴 ElevenLabs credit check FAILED — could not read subscription. API said: $(printf '%s' "$RESP" | head -c 200)"
    echo "$MSG" >&2
    "$NOTIFY" "$MSG" || true
    exit 2
fi

REMAIN=$(( LIMIT - USED ))
echo "ElevenLabs: tier=$TIER used=$USED limit=$LIMIT remaining=$REMAIN threshold=$THRESHOLD"

if [ "$REMAIN" -lt "$THRESHOLD" ]; then
    "$NOTIFY" "🟡 ElevenLabs credits LOW — ${REMAIN} of ${LIMIT} chars left (tier: ${TIER}), below ${THRESHOLD} threshold. Top up or pause VO runs before they fail." || true
fi

exit 0
