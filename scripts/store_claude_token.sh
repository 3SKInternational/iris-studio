#!/bin/bash
# store_claude_token.sh — create a long-lived setup-token and store it in
# Keychain for scripts/claude_token.sh (DQ-49), with no copy-paste.
#
# Copy-paste failed twice on 2026-10-09: the CLI prints the token wrapped over
# two indented lines, and only one line reached the Keychain. This runs
# `claude setup-token` under `script` (it needs a real terminal for the browser
# sign-in), pulls the token out of the captured output, joins the wrapped
# lines, stores it, and proves it works with one headless call.
#
# Usage (in a Terminal on the Mini):  bash scripts/store_claude_token.sh
# Self-check of the extraction only:  bash scripts/store_claude_token.sh --selftest

set -uo pipefail
CLAUDE="${CLAUDE_BIN:-/opt/homebrew/bin/claude}"

# Print the token found in a captured setup-token transcript: strip ANSI
# escapes (cursor-forward first becomes the spaces it stands for, because Ink
# draws some spaces that way), take everything from sk-ant-oat01- up to
# "Store this token", and
# drop all whitespace (the wrap). Prints nothing when there is no token.
extract() {
    perl -0777 -ne 's/\e\[(\d*)C/" " x ($1 || 1)/ge;
        s/\e\[[0-9;?]*[ -\/]*[@-~]//g; s/\e[()][A-Za-z0-9]//g;
        if (/(sk-ant-oat01-.*?)Store\s+this\s+token/s) { ($t=$1) =~ s/\s//g;
        print $t if $t =~ /\Ask-ant-oat01-[A-Za-z0-9_-]{60,}\z/ }' "$1"
}

if [ "${1:-}" = "--selftest" ]; then
    f="$(mktemp)"; trap 'rm -f "$f"' EXIT
    good="sk-ant-oat01-$(printf 'A%.0s' {1..60})_b-$(printf 'C%.0s' {1..30})"
    # Ink re-applies color on every wrapped line, so put codes INSIDE the token.
    printf ' \e[1mYour OAuth token (valid for 1 year):\e[0m\r\n\r\n \e[38;2;255;193;7m%s\e[39m\r\n \e[38;2;255;193;7m%s\e[39m\r\n\r\n \e[2mStore this\e[1Ctoken securely.\r\n' \
        "${good:0:70}" "${good:70}" > "$f"
    [ "$(extract "$f")" = "$good" ] || { echo "FAIL: wrapped token not rejoined"; exit 1; }
    printf ' Your OAuth token:\n sk-ant-oat01-short\n Store this token\n' > "$f"
    [ -z "$(extract "$f")" ] || { echo "FAIL: accepted a truncated token"; exit 1; }
    printf 'Login failed\n' > "$f"
    [ -z "$(extract "$f")" ] || { echo "FAIL: invented a token"; exit 1; }
    echo "selftest ok"; exit 0
fi

log="$(mktemp)"; chmod 600 "$log"; trap 'rm -f "$log"' EXIT
script -q "$log" "$CLAUDE" setup-token
printf '\e[3J'; clear   # drop the printed token from scrollback
tok="$(extract "$log")"   # the EXIT trap deletes $log
if [ -z "$tok" ]; then
    echo "No complete token found in the setup-token output. Nothing was stored."
    # Shape of the capture for debugging: markers found, and every character
    # between them with letters/digits counted, not shown. No token content.
    perl -0777 -ne 'printf "capture: %d bytes, sk-ant-oat01- x%d, \"Store this token\" x%d\n",
        length, scalar(() = /sk-ant-oat01-/g), scalar(() = /Store this token/g);
        if (/sk-ant-oat01-(.*?)(Store this token|\z)/s) { my $m=$1; my $n=($m =~ tr/A-Za-z0-9//);
        $m =~ s/[A-Za-z0-9]+/<alnum>/g; $m =~ s/([^\x21-\x7e])/sprintf("\\x%02x",ord $1)/ge;
        print "alnum chars after marker: $n\nshape: ", substr($m,0,600), "\n" }
        else { my $m=substr($_,-500); $m =~ s/[A-Za-z0-9]+/<alnum>/g;
        $m =~ s/([^\x21-\x7e])/sprintf("\\x%02x",ord $1)/ge; print "tail shape: $m\n" }' "$log"
    exit 1
fi

# Prove the token before touching Keychain, so a bad capture never replaces
# whatever is stored. Exit status is the auth proof, not the reply text.
if ! (cd /tmp && CLAUDE_CODE_OAUTH_TOKEN="$tok" "$CLAUDE" -p 'Reply with exactly: ok' >/dev/null 2>&1); then
    echo "The new token did not authenticate. Nothing was stored."
    exit 1
fi

# Through security's stdin, so the token never appears in argv (ps) or history.
printf 'add-generic-password -U -a iris-claude -s iris_claude_oauth_token -w %s\n' "$tok" | security -i \
    || { echo "Keychain write failed. Nothing changed."; exit 1; }
unset tok
echo "Token works and is stored in Keychain. Restarting the daemon so it picks it up."
launchctl kickstart -k "gui/$(id -u)/com.iris.studio"
echo "Done."
