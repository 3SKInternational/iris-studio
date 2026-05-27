#!/bin/bash
# remote-url.sh — print the current "Iris Code" Claude Code Remote Control session URL.
#
# Background: the `com.iris.claude-remote` launchd job runs
# `claude remote-control --name "Iris Code"` always-on and logs to
# `/Users/steve/iris_studio/logs/claude-remote.log`. Each time the process
# restarts (after a crash, reboot, or auto-restart by launchd), the environment
# ID in the URL rotates — so the URL printed at the previous run becomes stale.
#
# This script greps the log for the most recently printed URL and prints it.
# Run from any terminal on the Mac Mini:
#
#   ~/iris_studio/scripts/remote-url.sh
#   (or: /Volumes/AI_Workspace/iris_studio/scripts/remote-url.sh)
#
# Pipe into pbcopy for clipboard:
#   /Volumes/AI_Workspace/iris_studio/scripts/remote-url.sh | pbcopy
#
# Then paste the URL into Claude.ai/code in a browser, OR open the Claude mobile
# app's Code tab and pick the "Iris Code" environment (the app auto-discovers it
# from the URL once you've added the environment once).
#
# If the URL doesn't return, check:
#   1. Is the launchd job running? `launchctl list | grep claude-remote`
#   2. Does the log exist + have content? `ls -la /Users/steve/iris_studio/logs/claude-remote.log`
#   3. Kickstart it: `launchctl kickstart -k gui/$(id -u)/com.iris.claude-remote`

set -euo pipefail

LOG="/Users/steve/iris_studio/logs/claude-remote.log"

if [ ! -f "$LOG" ]; then
    echo "ERROR: Claude Code Remote Control log not found at $LOG" >&2
    echo "Is the com.iris.claude-remote launchd job running? Check:" >&2
    echo "  launchctl list | grep claude-remote" >&2
    exit 1
fi

URL=$(grep -oE "https://claude\.ai/code\?environment=[a-zA-Z0-9_]+" "$LOG" | tail -1)

if [ -z "$URL" ]; then
    echo "ERROR: No URL found in $LOG (yet?). The launchd job may still be starting." >&2
    echo "Try again in a few seconds, or tail the log:" >&2
    echo "  tail -f $LOG" >&2
    exit 1
fi

echo "$URL"
