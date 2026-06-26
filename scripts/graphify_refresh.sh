#!/bin/bash
# graphify_refresh.sh — deterministic daily refresh of the 3SK vault knowledge graph.
#
# Refreshes the STRUCTURAL/code layer only (graphify update — no LLM, ~35s on the
# 1750-file vault). The full SEMANTIC/doc-content layer still needs `/graphify
# --update` in a Claude Code session (LLM-backed); this script never spends tokens,
# it only pings Steve when that semantic pass is pending (needs_update flag set by
# the graphify-watch job when docs change).
#
# ponytail: structural refresh is free + deterministic, so it runs daily. The
# token-costing semantic pass stays human-gated — flagged, not auto-run.
set -euo pipefail

VAULT="/Users/steve/Documents/3SK/outputs"
GRAPHIFY="/Users/steve/.local/bin/graphify"
NOTIFY="/Volumes/AI_Workspace/iris_studio/scripts/notify.sh"

# Check the doc-staleness flag BEFORE update — a structural rebuild clears it.
if [ -f "$VAULT/graphify-out/needs_update" ]; then
    "$NOTIFY" "📊 graphify: docs changed since the last semantic build — run /graphify --update in Claude Code for a full knowledge-graph refresh." || true
fi

if ! "$GRAPHIFY" update "$VAULT" >/dev/null 2>>/Users/steve/iris_studio/logs/graphify-refresh.err.log; then
    "$NOTIFY" "🔴 graphify daily refresh FAILED on $VAULT — knowledge graph may be stale. Check com.iris.graphify-refresh logs." || true
    exit 1
fi

echo "$(date '+%Y-%m-%d %H:%M') graphify structural refresh OK ($VAULT)"
