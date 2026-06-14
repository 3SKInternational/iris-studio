#!/bin/bash
# A-28 — Token-efficiency steward (E11) zero-token pre-pass.
#
# Collects machine + repo + daemon state into a single markdown report
# that the steward prompt reads. The point of the shell pass is that
# *everything mechanical* happens BEFORE the model wakes up, so the model
# spends its budget on judgment (tier-fit / blueprint authoring), not on
# 20 grep/launchctl/sqlite invocations.
#
# Surface-only. Never modifies state. Safe to run unconditionally.
# Always exits 0 — a partial metrics file is still useful to the steward.

set -u
LC_ALL=C
export LC_ALL

VAULT="/Users/steve/Documents/3SK/outputs"
REPO="/Volumes/AI_Workspace/iris_studio"
LOGS="/Users/steve/iris_studio/logs"
DB="$REPO/iris.db"

TODAY="$(/bin/date +%Y-%m-%d)"
NOW_ET="$(/bin/date '+%Y-%m-%d %H:%M %Z')"
OUT="${1:-/tmp/efficiency-metrics-$TODAY.md}"

# Section helpers — every section starts with a `## ` heading so the
# prompt can navigate the file deterministically. Errors get inlined as
# bullets, never aborts the script (per surface-only contract).
write() { printf '%s\n' "$@" >>"$OUT"; }
section() { printf '\n## %s\n\n' "$1" >>"$OUT"; }
sub() { printf '\n### %s\n\n' "$1" >>"$OUT"; }
fence_begin() { printf '```\n' >>"$OUT"; }
fence_end() { printf '```\n' >>"$OUT"; }
safe() {
  # Run a command; capture stdout+stderr; truncate at MAX_LINES so a
  # runaway log can't blow up the metrics file.
  local max="${MAX_LINES:-100}"
  "$@" 2>&1 | /usr/bin/head -n "$max" >>"$OUT" || true
}

: >"$OUT"
printf '%s\n' "---" >>"$OUT"
printf 'date: %s\n' "$TODAY" >>"$OUT"
printf 'type: efficiency-metrics\n' >>"$OUT"
printf 'generated_at: %s\n' "$NOW_ET" >>"$OUT"
printf 'generator: efficiency-metrics.sh (A-28 E11 pre-pass)\n' >>"$OUT"
printf '%s\n' "---" >>"$OUT"
printf '\n# Efficiency metrics %s\n' "$TODAY" >>"$OUT"
write "_Zero-token shell pre-pass for the token-efficiency steward (Fri 03:30 ET). Always-fresh snapshot; the steward prompt audits this against the Token_Efficiency_Ledger._"

# ----------------------------------------------------------------------
section "1 · LaunchD job state (iris / claude-code only)"
write "_Registered iris jobs + their last-exit status. Middle column = last exit code; non-zero = the most-recent invocation failed (necessary-but-not-sufficient signal; pair with log mtimes for the authoritative view). The unfiltered list is ~150 macOS-system jobs; intentionally omitted to keep this file lean — the steward audits iris cadences, not Apple's._"
fence_begin
MAX_LINES=40 safe sh -c '/bin/launchctl list | /usr/bin/awk "NR==1 || /com\\.iris\\./"'
fence_end

# ----------------------------------------------------------------------
section "2 · Log sizes (last 24h of growth)"
write "_Log size + mtime of every routine + daemon log. A log that hasn't been touched in >7 days for a daily/weekly cadence is a cold-cadence signal; a log >50 MB without recent rotation is a Pass-5 concern; per-job stderr.log presence is the silent-failure surface._"
fence_begin
if [ -d "$LOGS" ]; then
  MAX_LINES=80 safe /bin/ls -lhT "$LOGS"
else
  write "(logs dir missing: $LOGS)"
fi
fence_end

# ----------------------------------------------------------------------
section "3 · Daemon cost + tier-split (last 7 days)"
write "_From iris.db \`daily_stats\` — per-day per-tier cost. Local tier rows = Tier-1/2 routing saves; cloud rows = Tier-3+ spend. A 7-day window with zero local rows = the router isn't routing (or no chatty traffic). Cost = USD._"
fence_begin
if [ -f "$DB" ]; then
  /usr/bin/sqlite3 -cmd ".timeout 5000" "$DB" \
    "SELECT date, tier, ROUND(SUM(cost_usd), 4) AS cost_usd, COUNT(*) AS rows
     FROM daily_stats
     WHERE date >= date('now', '-7 days')
     GROUP BY date, tier
     ORDER BY date DESC, tier;" 2>&1 | /usr/bin/head -n 40 >>"$OUT" || \
    write "(sqlite3 query failed)"
else
  write "(iris.db missing — X9 unmounted?)"
fi
fence_end
sub "Daemon run count (proxy for boot churn)"
fence_begin
MAX_LINES=10 safe sh -c '/bin/launchctl print "gui/$(/usr/bin/id -u)/com.iris.studio" 2>/dev/null | /usr/bin/grep -E "state|pid|runs"'
fence_end

# ----------------------------------------------------------------------
section "4 · Dispatch activity (last 7 days)"
write "_Per-agent dispatch counts by status — proxy for Tier-3 agent load. A status-skew (lots of \`timed_out\`/\`failed\`) is a tier-misfit signal; a quiet autonomous-cadence agent that should be firing weekly is a cadence-health signal (A-22 catches missed fires — this is the spend-side view). Autonomous vs manual is not stored as a column; agent_name pattern (\`project-manager\`, \`youtube-researcher\`, \`market-researcher\`, \`expense-categorizer\`) implies autonomous._"
fence_begin
if [ -f "$DB" ]; then
  /usr/bin/sqlite3 -cmd ".timeout 5000" "$DB" \
    "SELECT agent_name,
            COUNT(*) AS n,
            SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed,
            SUM(CASE WHEN status='timed_out' THEN 1 ELSE 0 END) AS timed_out,
            SUM(CASE WHEN status='failed'    THEN 1 ELSE 0 END) AS failed,
            SUM(CASE WHEN status='cancelled' THEN 1 ELSE 0 END) AS cancelled
     FROM dispatches
     WHERE started_epoch IS NOT NULL
       AND started_epoch >= strftime('%s', 'now', '-7 days')
     GROUP BY agent_name
     ORDER BY n DESC;" 2>&1 | /usr/bin/head -n 40 >>"$OUT" || \
    write "(sqlite3 dispatches query failed)"
else
  write "(iris.db missing — X9 unmounted?)"
fi
fence_end

# ----------------------------------------------------------------------
section "5 · Standing-context costs"
write "_Files loaded into the daemon prompt on EVERY Telegram message (INBOX/TODO) + the bridge file (read at the start of every Claude Code session). These are the standing token costs the ledger Rule 4 asks the steward to audit, not just active work. Caps (per ledger v0): INBOX ≤ 150 lines · TODO ≤ 80 lines · bridge ≤ 150 KB._"
fence_begin
for f in "INBOX.md" "TODO.md" "_Iris_Memory/Sessions/CLAUDE_CODE_HANDOFF.md"; do
  full="$VAULT/$f"
  if [ -f "$full" ]; then
    bytes="$(/usr/bin/stat -f %z "$full" 2>/dev/null || /bin/echo "?")"
    kb="$(/bin/echo "$bytes" | /usr/bin/awk '{printf "%.1f", $1/1024}')"
    lines="$(/usr/bin/wc -l <"$full" | /usr/bin/awk '{print $1}')"
    printf '%-50s  %7s lines  %7s KB  (%s bytes)\n' "$f" "$lines" "$kb" "$bytes" >>"$OUT"
  else
    printf '%-50s  (missing)\n' "$f" >>"$OUT"
  fi
done
fence_end

# ----------------------------------------------------------------------
section "6 · Repo state (auto-push hygiene per A1)"
write "_With A1 auto-push live (Redesign Night 1, 6/10), unpushed commits should be ZERO at all times after the next pre-brief catch-all. Non-zero here means a routine committed but the push silently failed — that's a Tier-1 reliability flag, not an efficiency one, but worth surfacing._"
fence_begin
cd "$REPO" 2>/dev/null && {
  printf 'branch:           %s\n' "$(/usr/bin/git rev-parse --abbrev-ref HEAD 2>/dev/null)" >>"$OUT"
  printf 'commits ahead:    %s\n' "$(/usr/bin/git rev-list --count origin/main..HEAD 2>/dev/null || /bin/echo '?')" >>"$OUT"
  printf 'dirty files:      %s\n' "$(/usr/bin/git status --porcelain 2>/dev/null | /usr/bin/wc -l | /usr/bin/awk '{print $1}')" >>"$OUT"
  printf 'last commit:      %s\n' "$(/usr/bin/git log -1 --pretty=format:'%h %ci %s' 2>/dev/null)" >>"$OUT"
} || write "(could not cd to repo: $REPO)"
fence_end

# ----------------------------------------------------------------------
section "7 · MCP config audit"
write "_Configured MCPs (Claude Code surface) — connection state + name. \`✗ Failed to connect\` rows = standing per-session cost with zero payoff (paying the schema-load tax for a broken connection). Compare to \`Token_Efficiency_Ledger\` Tier-2 column._"
fence_begin
MAX_LINES=50 safe /opt/homebrew/bin/claude mcp list
fence_end

# ----------------------------------------------------------------------
section "8 · Drive sync last status (Tier-1 reference exemplar)"
write "_The exemplar of Tier-1 (zero-token script) for offsite backup. Last-run signal lives in \`drive-sync.stdout.log\` (start/done markers) and \`launchctl list\` exit code. A failing tier-1 job is the worst kind of regression because the cheaper tier was supposed to be more reliable, not less._"
fence_begin
if [ -f "$LOGS/drive-sync.stdout.log" ]; then
  MAX_LINES=8 safe /usr/bin/tail -n 8 "$LOGS/drive-sync.stdout.log"
else
  write "(no drive-sync log yet)"
fi
fence_end

# ----------------------------------------------------------------------
section "9 · Vault file count + size (A-18 baseline trajectory)"
write "_Quick total — not a substitute for A-18's per-file diff, just a trend reference for the steward (a vault that doubled in a week IS itself an efficiency story)._"
fence_begin
if [ -d "$VAULT" ]; then
  count="$(/usr/bin/find "$VAULT" -type f \
    -not -path "*/.git/*" -not -path "*/.venv/*" -not -path "*/.obsidian/workspace*" \
    -not -path "*/__pycache__/*" -not -name "*.bak-*" -not -name ".DS_Store" 2>/dev/null | /usr/bin/wc -l | /usr/bin/awk '{print $1}')"
  size_kb="$(/usr/bin/du -sk "$VAULT" 2>/dev/null | /usr/bin/awk '{print $1}')"
  printf 'file count:   %s\n' "$count" >>"$OUT"
  printf 'total size:   %s KB (du -sk; not excludes-aware)\n' "$size_kb" >>"$OUT"
else
  write "(vault dir missing: $VAULT)"
fi
fence_end

# ----------------------------------------------------------------------
section "10 · Listening ports (Rule 1 enforcement)"
write "_Defense-in-depth on Hard Rule #1 (never expose the Mac to the public internet without explicit go-ahead). Filter keeps only listeners NOT bound to loopback — \`127.0.0.1\`, \`::1\`, \`[::1]\`, \`[::ffff:127.*]\` (IPv4-mapped IPv6 loopback). NOTE: \`*:port\` means \"bound to all interfaces\" (external-facing) — it is the OPPOSITE of loopback, so it must NOT be filtered out. Anything below should be reviewed against the iris allowlist (Telegram bot, playpen at 127.0.0.1:8080, claude-remote tunnel). Pre-brief Pass 7 enforces this daily; surfaced here so the steward can flag a Tier-4-class \"open to internet\" line in the ledger if a new non-loopback bind appears._"
fence_begin
MAX_LINES=20 safe sh -c "/usr/sbin/lsof -nP -iTCP -sTCP:LISTEN 2>/dev/null | /usr/bin/awk '\$9 !~ /^(127\\.0\\.0\\.1|::1):/ && \$9 !~ /^\\[::1\\]:/ && \$9 !~ /^\\[::ffff:127\\./' | /usr/bin/head -20"
fence_end

# ----------------------------------------------------------------------
write ""
write "_End of metrics. The steward prompt reads this file, audits the ledger, and writes blueprints/updates per the 5 standing rules._"

exit 0
