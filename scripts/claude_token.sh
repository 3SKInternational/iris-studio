# claude_token.sh — sourced (not run) by every unattended claude launcher.
#
# Exports CLAUDE_CODE_OAUTH_TOKEN from the login Keychain so headless claude
# runs on the long-lived `claude setup-token` credential instead of the ~30-day
# interactive /login token, whose silent expiry took the fleet dark twice
# (8/20-8/22, 9/22-10/09; DQ-49).
#
# No Keychain item -> no token export, and claude falls back to the /login
# credential. A token already in the environment wins (tests, one-off runs).
# Never source this for com.iris.claude-remote: Remote Control rejects the
# long-lived token and needs the normal /login.
#
# Also exports, for alerts and logs (never the token itself):
#   IRIS_CLAUDE_AUTH_SOURCE  keychain | env | login
#   IRIS_CLAUDE_REAUTH       the fix for THAT credential. The env token takes
#                            precedence over /login, so once it is installed
#                            `claude auth login` cannot clear a 401.
#
# Install / rotate (once a year):
#   claude setup-token        # browser sign-in, prints sk-ant-oat01-...
#   security add-generic-password -U -a iris-claude -s iris_claude_oauth_token -w
#   (-w with no value prompts for the token, so it never lands in shell history)
#   launchctl kickstart -k gui/$(id -u)/com.iris.studio   # daemon reads it at launch

if [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
    IRIS_CLAUDE_AUTH_SOURCE="env"
else
    IRIS_CLAUDE_AUTH_SOURCE="login"
    _iris_tok="$(security find-generic-password -a iris-claude -s iris_claude_oauth_token -w 2>/dev/null)" || _iris_tok=""
    if [ -n "$_iris_tok" ]; then
        export CLAUDE_CODE_OAUTH_TOKEN="$_iris_tok"
        IRIS_CLAUDE_AUTH_SOURCE="keychain"
    fi
    unset _iris_tok
fi
if [ "$IRIS_CLAUDE_AUTH_SOURCE" = "login" ]; then
    IRIS_CLAUDE_REAUTH="run \`claude auth login\` in a Terminal on the Mini (NOT \`claude login\`, which the CLI reads as a prompt)"
else
    IRIS_CLAUDE_REAUTH="the long-lived setup-token (Keychain iris_claude_oauth_token) was rejected; \`claude auth login\` will NOT fix it. On the Mini run \`claude setup-token\`, then \`security add-generic-password -U -a iris-claude -s iris_claude_oauth_token -w\` (paste the token), then \`launchctl kickstart -k gui/\$(id -u)/com.iris.studio\`"
fi
export IRIS_CLAUDE_AUTH_SOURCE IRIS_CLAUDE_REAUTH
