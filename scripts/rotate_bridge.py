#!/usr/bin/env python3
"""rotate_bridge.py — size-gated, atomic rotation of the Claude Code bridge file (E9).

Keeps the newest KEEP entries live in CLAUDE_CODE_HANDOFF.md and MOVES older
entries to CLAUDE_CODE_HANDOFF_Archive.md (append, never delete). Splits only on
top-level entry boundaries so an entry is never truncated mid-body.

The bridge is the brain's most-read file (loaded at every session boot) and lives
in a git-tracked but Syncthing-STOPPED vault, so data-loss safety is paramount:
  - moves WHOLE entries, never deletes;
  - byte-conservation invariant (preamble + moved + live == original) is asserted
    before any write — fail-closed, writes nothing on mismatch;
  - timestamped pre-rotation backup of the live bridge;
  - atomic temp-file + os.replace (no reader ever sees a truncated bridge);
  - archive is written BEFORE the bridge is shrunk, so the only possible failure
    mode duplicates entries (recoverable) rather than dropping them;
  - --dry-run prints the plan and writes nothing.

Owner: the efficiency-steward routine (Fri 03:30 ET) runs this every audit; it is
size-gated, so a clean week is a no-op. Safe to run by hand any time.
"""
import argparse
import datetime
import os
import re
import shutil
import sys
import tempfile

BRIDGE = "/Users/steve/Documents/3SK/outputs/_Iris_Memory/Sessions/CLAUDE_CODE_HANDOFF.md"
ARCHIVE = "/Users/steve/Documents/3SK/outputs/_Iris_Memory/Sessions/CLAUDE_CODE_HANDOFF_Archive.md"
CAP_BYTES = 150 * 1024  # E9 cap
KEEP = 12               # newest entries kept live

# A top-level entry begins with a markdown heading (## or ###) OR a bold
# "**Session <number>" line — the three formats the bridge has used over time.
# The bold rule REQUIRES a digit after "Session" so internal notes like
# "**Session metrics:**" or "**Session-numbering note:**" are NOT treated as
# entry boundaries. Internal subsections use bold "**Foo:**" labels, never "## ".
ENTRY_RE = re.compile(r"^(#{2,3}\s|\*\*Session\s+\d)")

# A fenced code block opens/closes on a line whose first non-space content is
# ``` or ~~~ (3+ of either). Inside a fence, a line like "## foo" is code, not a
# heading, so ENTRY_RE must NOT treat it as an entry boundary — otherwise an
# entry can be torn across the bridge/archive split, and byte-conservation
# (pure concatenation) cannot detect the tear.
FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")

ARCHIVE_FRONTMATTER = """---
type: bridge-file-archive
status: archive
purpose: Rotated entries from CLAUDE_CODE_HANDOFF.md (E9 rotation rule). Newest ~12 entries stay in the live bridge; everything older lands here, newest at bottom.
related:
  - "[[CLAUDE_CODE_HANDOFF]]"
tags:
  - session/bridge-archive
---

# Claude Code ↔ Cowork-Iris bridge — ARCHIVE
"""


def split_entries(text):
    """Return (preamble, entries).

    preamble = everything before the first top-level entry boundary
    (frontmatter + title + standing-state header). entries = list of strings,
    each spanning one boundary line through (not including) the next boundary.
    Concatenating preamble + "".join(entries) reproduces text byte-for-byte.
    """
    lines = text.splitlines(keepends=True)
    starts = []
    in_fence = False
    fence_marker = ""
    for i, ln in enumerate(lines):
        fm = FENCE_RE.match(ln)
        if fm:
            tok = fm.group(1)[0] * 3  # normalize to ``` or ~~~
            if not in_fence:
                in_fence = True
                fence_marker = tok
            elif tok == fence_marker:
                in_fence = False
                fence_marker = ""
            continue  # a fence line is never itself an entry boundary
        if not in_fence and ENTRY_RE.match(ln):
            starts.append(i)
    if not starts:
        return text, []
    preamble = "".join(lines[: starts[0]])
    entries = []
    for idx, s in enumerate(starts):
        e = starts[idx + 1] if idx + 1 < len(starts) else len(lines)
        entries.append("".join(lines[s:e]))
    return preamble, entries


def _atomic_write(path, content):
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp_rotate_", suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        # mkstemp creates 0600; preserve the existing file's mode (or default
        # 0644 for a new file) so rotation never silently tightens permissions.
        try:
            mode = os.stat(path).st_mode & 0o777
        except FileNotFoundError:
            mode = 0o644
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _b(s):
    return len(s.encode("utf-8"))


def main():
    ap = argparse.ArgumentParser(description="Size-gated atomic rotation of the bridge file (E9).")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, write nothing")
    ap.add_argument("--force", action="store_true", help="rotate even if under the size cap")
    ap.add_argument("--keep", type=int, default=KEEP, help=f"newest entries to keep live (default {KEEP})")
    ap.add_argument("--cap", type=int, default=CAP_BYTES, help=f"size cap in bytes (default {CAP_BYTES})")
    ap.add_argument("--bridge", default=BRIDGE)
    ap.add_argument("--archive", default=ARCHIVE)
    args = ap.parse_args()

    if args.keep < 1:
        print("rotate_bridge: --keep must be >= 1")
        return 1
    if not os.path.exists(args.bridge):
        print(f"rotate_bridge: bridge not found: {args.bridge}")
        return 1

    with open(args.bridge, encoding="utf-8") as f:
        text = f.read()
    size = _b(text)

    if size <= args.cap and not args.force:
        print(f"rotate_bridge: {size} B <= cap {args.cap} B — no rotation needed (no-op).")
        return 0

    preamble, entries = split_entries(text)
    if len(entries) <= args.keep:
        print(
            f"rotate_bridge: {len(entries)} entries <= keep {args.keep} — nothing to move (no-op). "
            f"File is {size} B; if over cap, entry verbosity (not count) is the cause."
        )
        return 0

    move = entries[: -args.keep]
    live = entries[-args.keep:]

    # Fail-closed byte-conservation invariant: the rebuilt text must equal the
    # original exactly, or we refuse to write anything.
    if preamble + "".join(move) + "".join(live) != text:
        print("rotate_bridge: ABORT — byte-conservation check failed (rebuilt != original). Wrote nothing.")
        return 2

    new_bridge = preamble + "".join(live)
    today = datetime.date.today().isoformat()

    if os.path.exists(args.archive):
        with open(args.archive, encoding="utf-8") as f:
            arch = f.read()
    else:
        arch = ARCHIVE_FRONTMATTER
    if not arch.endswith("\n"):
        arch += "\n"
    moved_text = "".join(move)
    new_arch = arch + f"\n## Rotated {today} — {len(move)} entries\n\n" + moved_text

    # Archive must contain every moved byte verbatim.
    if moved_text not in new_arch:
        print("rotate_bridge: ABORT — moved entries not fully present in new archive. Wrote nothing.")
        return 2

    first_kept = live[0].splitlines()[0][:90] if live and live[0].strip() else "(blank)"
    last_moved = move[-1].splitlines()[0][:90] if move and move[-1].strip() else "(blank)"

    if args.dry_run:
        print("rotate_bridge DRY-RUN (no files written):")
        print(f"  entries total : {len(entries)}  -> keep {len(live)} live, move {len(move)} to archive")
        print(f"  bridge bytes  : {size} -> {_b(new_bridge)}")
        print(f"  archive bytes : {_b(arch)} -> {_b(new_arch)}")
        print("  conservation  : OK (preamble + moved + live == original)")
        print(f"  first KEPT    : {first_kept}")
        print(f"  last MOVED    : {last_moved}")
        return 0

    # Timestamped to the second so a same-day second run never overwrites an
    # earlier (pre-rotation) backup with an already-shrunk file.
    stamp = datetime.datetime.now().strftime("%Y-%m-%dT%H%M%S")
    bak = f"{args.bridge}.bak-pre-rotate-{stamp}"
    shutil.copy2(args.bridge, bak)

    # Archive first (additive) so a mid-failure duplicates rather than drops.
    _atomic_write(args.archive, new_arch)
    _atomic_write(args.bridge, new_bridge)

    print(
        f"rotate_bridge: moved {len(move)} entries to archive; "
        f"bridge {size} B -> {_b(new_bridge)} B; kept {len(live)} newest; backup {bak}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
