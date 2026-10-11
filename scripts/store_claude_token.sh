#!/bin/bash
# store_claude_token.sh — create a long-lived setup-token and store it in
# Keychain for scripts/claude_token.sh (DQ-49), with no copy-paste.
#
# Copy-paste failed twice on 2026-10-09: the CLI prints the token wrapped over
# two indented lines, and only one line reached the Keychain. This runs
# `claude setup-token` under `script` (it needs a real terminal for the browser
# sign-in), pulls candidate tokens out of the captured output (the token
# rejoined across 1-4 wrapped lines), and stores the first candidate that
# actually authenticates. A candidate that fails the sign-in test is never
# stored, so a parsing mistake costs a retry, never a broken fleet.
#
# Usage (in a Terminal on the Mini):  bash scripts/store_claude_token.sh
# Self-check of the extraction only:  bash scripts/store_claude_token.sh --selftest

set -uo pipefail
CLAUDE="${CLAUDE_BIN:-/opt/homebrew/bin/claude}"

# Print candidate tokens from a captured setup-token transcript, shortest
# first. Ink's output is drawing commands, not text: cursor-forward codes stand
# for spaces, and lines are separated by CR plus cursor moves rather than LF.
# So: strip every escape, then join the
# whitespace-separated pieces after sk-ant-oat01- one at a time. No end marker:
# the caller's auth test decides which candidate is the token.
candidates() {
    perl -0777 -ne 's/\e\][^\a\e]*(?:\a|\e\\)//g; s/\e\[[0-9;?]*[ -\/]*[@-~]//g; s/\e[()][A-Za-z0-9]//g;
        /(sk-ant-oat01-.*)/s or exit;
        my @p = split /\s+/, $1; my $t = "";
        for my $i (0 .. 3) {
            last if $i > $#p || $p[$i] !~ /\A[A-Za-z0-9_-]+\z/;
            $t .= $p[$i];
            print "$t\n" if $t =~ /\Ask-ant-oat01-[A-Za-z0-9_-]{60,}\z/;
        }' "$1"
}

# Print the first candidate that authenticates. Exit status is the auth
# proof. The list comes in on fd 3 and claude gets </dev/null: `claude -p`
# reads stdin when it isn't a terminal, and would otherwise swallow the rest
# of the list after the first try (and could send it to the API as prompt).
pick_token() {
    local c
    while IFS= read -r c <&3; do
        if (cd /tmp && CLAUDE_CODE_OAUTH_TOKEN="$c" "$CLAUDE" -p 'Reply with exactly: ok' </dev/null >/dev/null 2>&1); then
            printf '%s\n' "$c"; return 0
        fi
    done 3< <(candidates "$1")
    return 1
}

if [ "${1:-}" = "--selftest" ]; then
    f="$(mktemp)"; trap 'rm -f "$f"' EXIT
    good="sk-ant-oat01-$(printf 'A%.0s' {1..60})_b-$(printf 'C%.0s' {1..30})"
    # Real Ink shape: color codes INSIDE the wrapped token, lines joined by CR +
    # cursor-down (no LF), and a space drawn as cursor-forward in the next line.
    printf ' \e[1mYour OAuth token (valid for 1 year):\e[0m\r\e[1B\r\e[1B \e[38;2;255;193;7m%s\e[39m\r\e[1B \e[38;2;255;193;7m%s\e[39m\r\e[1B\r\e[1B \e[2mStore this\e[1Ctoken securely.\e[22m\r\n' \
        "${good:0:70}" "${good:70}" > "$f"
    candidates "$f" | grep -qx "$good" || { echo "FAIL: wrapped token not among candidates"; exit 1; }
    printf ' Your OAuth token:\n sk-ant-oat01-short\n Store this token\n' > "$f"
    [ -z "$(candidates "$f")" ] || { echo "FAIL: offered a truncated token"; exit 1; }
    printf 'Login failed\n' > "$f"
    [ -z "$(candidates "$f")" ] || { echo "FAIL: invented a token"; exit 1; }
    # The loop must reach the full token even though shorter candidates come
    # first, with a fake claude that reads stdin the way the real one does.
    # Split at 80 so the first half alone is a candidate and is tried first.
    printf ' \e[38;2;255;193;7m%s\e[39m\r\e[1B \e[38;2;255;193;7m%s\e[39m\r\e[1B\r\e[1B Store this token\r\n' \
        "${good:0:80}" "${good:80}" > "$f"
    fake="$(mktemp)"; trap 'rm -f "$f" "$fake"' EXIT
    printf '#!/bin/bash\ncat >/dev/null\n[ "$CLAUDE_CODE_OAUTH_TOKEN" = "%s" ]\n' "$good" > "$fake"; chmod +x "$fake"
    [ "$(CLAUDE="$fake" pick_token "$f")" = "$good" ] || { echo "FAIL: loop never reached the full token"; exit 1; }
    echo "selftest ok"; exit 0
fi

log="$(mktemp)"; chmod 600 "$log"; trap 'rm -f "$log"' EXIT
script -q "$log" "$CLAUDE" setup-token
printf '\e[3J'; clear   # drop the printed token from scrollback

tok="$(pick_token "$log")"

if [ -z "$tok" ]; then
    echo "No working token found among $(candidates "$log" | wc -l | tr -d ' ') candidate(s). Nothing was stored."
    echo "Try again with the Terminal window wide enough for the token to fit on one line."
    # Shape of the capture for debugging: escape codes shown, letters and
    # digits collapsed to <alnum>. No token content.
    perl -0777 -ne 'printf "capture: %d bytes, sk-ant-oat01- x%d\n", length, scalar(() = /sk-ant-oat01-/g);
        if (/sk-ant-oat01-(.*)/s) { my $m=substr($1,0,700);
        $m =~ s/[A-Za-z0-9]+/<alnum>/g; $m =~ s/([^\x21-\x7e])/sprintf("\\x%02x",ord $1)/ge;
        print "shape: $m\n" }' "$log"
    exit 1
fi

# Through security's stdin, so the token never appears in argv (ps) or history.
printf 'add-generic-password -U -a iris-claude -s iris_claude_oauth_token -w %s\n' "$tok" | security -i \
    || { echo "Keychain write failed. Nothing changed."; exit 1; }
unset tok c
echo "Token works and is stored in Keychain. Restarting the daemon so it picks it up."
launchctl kickstart -k "gui/$(id -u)/com.iris.studio"
echo "Done."
