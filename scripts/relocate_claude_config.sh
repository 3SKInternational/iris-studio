#!/usr/bin/env bash
# relocate_claude_config.sh — move ~/.claude off the X9 external volume to internal disk.
#
# WHY (DQ-15 / nightly-EPERM root cause):
#   ~/.claude is a SYMLINK to /Volumes/AI_Workspace/claude_config (the X9 external
#   volume). launchd jobs fire right after the Mac wakes — before the external
#   volume is mounted + TCC-granted — so `mkdir ~/.claude/session-env/<uuid>` fails
#   with EPERM and every Bash tool call in that scheduled `claude --print` run dies.
#   Relocating the config dir onto the internal disk removes the external-volume
#   dependency permanently. Auth lives in ~/.claude.json (a REAL internal file, NOT
#   inside this dir) and is never touched.
#
# WHAT it does: rsync-copies the X9 config to an internal staging dir, then atomically
#   replaces the ~/.claude symlink with that real internal directory. The old X9 copy
#   is LEFT IN PLACE as a rollback safety net (delete it manually once you've confirmed
#   a few interactive sessions + one scheduled job run cleanly).
#
# SAFETY: refuses to run while any `claude` process is alive (a live session writes to
#   this dir; swapping under it would split-brain its state). Run from a PLAIN Terminal
#   with all Claude Code / Cowork sessions quit.
#
# ROLLBACK (if anything misbehaves after the swap):
#   rm -rf ~/.claude && ln -s /Volumes/AI_Workspace/claude_config ~/.claude
#   (restores the original symlink; the X9 copy is untouched.)
#
# Idempotent: re-running after a completed relocation detects the real dir and exits 0.

set -euo pipefail

LINK="$HOME/.claude"
SRC="/Volumes/AI_Workspace/claude_config"   # current X9 symlink target
STAGE="$HOME/.claude_relocate_stage"         # internal staging dir (becomes ~/.claude)

log()  { printf '%s\n' "$*"; }
fail() { printf '✋ %s\n' "$*" >&2; exit 1; }

# Recovery guard: if we're interrupted in the tiny window after the symlink is
# removed but before the internal dir is moved into place, ~/.claude is missing.
# No data is lost (the copy is in $STAGE, the original in $SRC) — but tell the user
# exactly how to recover rather than leaving them staring at a missing dir.
SWAP_STARTED=0
on_exit() {
  local rc=$?
  if (( rc != 0 && SWAP_STARTED == 1 )) && [[ ! -e "$LINK" ]]; then
    {
      printf '\n⚠ INTERRUPTED MID-SWAP: ~/.claude is currently MISSING (no data lost).\n'
      printf '   Your relocated copy is intact at : %s\n' "$STAGE"
      printf '   The original X9 config is intact : %s\n' "$SRC"
      printf '   Finish the move (internal disk)  : mv "%s" "%s"\n' "$STAGE" "$LINK"
      printf '   Or roll back to the X9 symlink   : ln -s "%s" "%s"\n' "$SRC" "$LINK"
    } >&2
  fi
}
trap on_exit EXIT

# 0) Already relocated? (real dir, not a symlink) → nothing to do.
if [[ -d "$LINK" && ! -L "$LINK" ]]; then
  mp=$(df "$LINK" | awk 'NR==2{print $NF}')
  log "✓ ~/.claude is already a real directory on: $mp — already relocated, nothing to do."
  exit 0
fi

# 1) Must currently be the expected symlink.
[[ -L "$LINK" ]] || fail "~/.claude is neither a symlink nor a real dir — unexpected state. Inspect manually."
cur="$(readlink "$LINK")"
[[ "$cur" == "$SRC" ]] || fail "~/.claude points to '$cur', not '$SRC'. Aborting (manual check needed)."

# 2) Source (X9) must be mounted to copy from it.
[[ -d "$SRC" ]] || fail "source '$SRC' not present — is the X9 volume mounted? Aborting."

# 3) Refuse if a claude session is active (avoid corrupting live state). Two
#    INDEPENDENT signals so a session launched under a non-`claude` basename
#    (e.g. `node .../cli.js`) can't slip past:
#      (a) process-name match, and
#      (b) ANY open file handle under the config dir — name-agnostic, the strongest
#          signal that something is actively using it.
if pgrep -x claude >/dev/null 2>&1; then
  fail "a 'claude' process is running. Quit ALL Claude Code / Cowork sessions, then re-run from Terminal."
fi
if command -v lsof >/dev/null 2>&1; then
  open_handles="$(lsof +D "$SRC" 2>/dev/null || true)"
  if [[ -n "$open_handles" ]]; then
    fail "open file handles found under $SRC — a Claude session is still using the config dir. Quit ALL sessions (incl. the com.iris.claude-remote daemon) and re-run."
  fi
fi

# 4) Internal free-space check (need ~2x config size as headroom).
need_kb=$(du -sk "$SRC" | awk '{print $1}')
free_kb=$(df -k "$HOME" | awk 'NR==2{print $4}')
if (( free_kb < need_kb * 2 )); then
  fail "not enough internal free space (want ~$(( need_kb/1024 ))M x2, have $(( free_kb/1024 ))M)."
fi

log "→ source : $SRC ($(( need_kb/1024 ))M)"
log "→ copying to internal staging: $STAGE …"
rsync -aH --delete "$SRC"/ "$STAGE"/

# 5) Atomic-ish swap: drop the symlink, promote the internal dir into its place.
log "→ swapping the symlink for the internal directory…"
SWAP_STARTED=1        # arms the EXIT trap's mid-swap recovery message
rm "$LINK"            # removes the SYMLINK only — never its X9 target
mv "$STAGE" "$LINK"   # the internal copy becomes ~/.claude (now a real directory)
SWAP_STARTED=0        # swap completed; disarm

# 6) Verify.
[[ -L "$LINK" ]] && fail "~/.claude is STILL a symlink — swap failed. Rollback: ln -s $SRC ~/.claude"
[[ -d "$LINK" ]] || fail "~/.claude is not a directory — swap failed. Rollback: ln -s $SRC ~/.claude"
mp=$(df "$LINK" | awk 'NR==2{print $NF}')
# Pass if it's anywhere on the internal disk (macOS reports '/System/Volumes/Data'
# for $HOME, not '/'); warn only if it's somehow still on the X9 external volume.
case "$mp" in
  /Volumes/AI_Workspace*) log "  ⚠ ~/.claude still resolves to the X9 volume ($mp) — unexpected, investigate." ;;
esac

# 7) Restore the remote-control daemon the SAFETY check (step 3) made you quit.
#    com.iris.claude-remote holds open file handles under ~/.claude, so this script
#    can only run with it down — but nothing was bringing it back, leaving Remote
#    Control offline (and the playpen RELAY tile red) until someone noticed. Re-load
#    it here so the relocation self-heals. Best-effort: never fail the (already
#    successful) relocation just because the daemon couldn't be re-loaded.
REMOTE_PLIST="$HOME/Library/LaunchAgents/com.iris.claude-remote.plist"
REMOTE_TGT="gui/$(id -u)/com.iris.claude-remote"
# `launchctl print <target>` is the canonical "is this service loaded?" probe — no
# pipe, so no SIGPIPE race (unlike `launchctl list | grep`, which can misreport when
# grep exits before list finishes writing).
if [[ -f "$REMOTE_PLIST" ]]; then
  if launchctl print "$REMOTE_TGT" >/dev/null 2>&1; then
    log "→ restarting com.iris.claude-remote for the new config path…"
    launchctl kickstart -k "$REMOTE_TGT" 2>/dev/null || true
  else
    log "→ re-loading com.iris.claude-remote (step 3 had you quit it)…"
    launchctl bootstrap "gui/$(id -u)" "$REMOTE_PLIST" 2>/dev/null || true
  fi
  if launchctl print "$REMOTE_TGT" >/dev/null 2>&1; then
    log "✓ com.iris.claude-remote re-loaded."
  else
    log "  ⚠ com.iris.claude-remote did NOT come back — load it manually:"
    log "      launchctl bootstrap gui/\$(id -u) '$REMOTE_PLIST'"
  fi
fi

log ""
log "✓ ~/.claude is now a real directory on: $mp"
log "✓ settings.json : $([[ -f "$LINK/settings.json" ]] && echo present || echo MISSING)"
log "✓ agents        : $(ls "$LINK/agents" 2>/dev/null | wc -l | tr -d ' ') file(s)"
log "✓ sessions dir  : $([[ -d "$LINK/sessions" ]] && echo present || echo MISSING)"
log "✓ auth file     : $([[ -f "$HOME/.claude.json" ]] && echo '~/.claude.json present (untouched)' || echo 'MISSING — check!')"
log ""
log "Old X9 copy kept at: $SRC"
log "  → delete it only AFTER a few interactive sessions + one scheduled job run cleanly:"
log "      rm -rf '$SRC'"
log "Rollback if needed:  rm -rf ~/.claude && ln -s '$SRC' ~/.claude"
log "Done. Auth was never touched; CLAUDE_CONFIG_DIR stays unset (Claude defaults to ~/.claude)."
