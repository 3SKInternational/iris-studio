#!/usr/bin/env python3
"""rotate_backlog.py — size-gated, atomic rotation of the Automation_Backlog
`**Scan history:**` block (A-64).

Keeps the newest KEEP scan-history entries live in `Automation_Backlog.md` and
MOVES older ones to `Automation_Backlog_ScanHistory_Archive.md` (append, never
delete). The scan-history block is the fortnightly automation-scan narrative; it
grows ~2 entries/week and had reached ~48 KB, which — stacked on the candidate
section — pushed a default agent read past its ~25k-token cap BEFORE it reached
the auto-build candidate rows. That truncation was half the root cause of the
A-60/61/63 invisibility miss (2026-08-03). Same unbounded-growth class the bridge
already solved with `rotate_bridge.py`, which this mirrors.

ONE deliberate divergence from rotate_bridge.py: the bridge appends newest at the
bottom, so it keeps the newest N by POSITION. This block does NOT keep a stable
order — the newest scan note sits at the TOP (prepended), older notes run roughly
chronologically below it, and a correction blockquote rides mid-block — so
keeping the newest N by file position would archive the newest entry. This keeps
the newest N by PARSED DATE (the `- YYYY-MM-DD` prefix each scan bullet carries)
and preserves each kept/moved entry in its original file order (no reordering).

Data-loss safety (the vault is Syncthing-STOPPED, git-untracked, Drive-synced):
  - moves WHOLE dated entries, never deletes; only the scan-history block is
    touched — candidate rows, the LIVE auto-build queue, and every heading are
    left byte-identical;
  - a fail-closed byte-conservation invariant (before + block + after == original,
    and preamble + all entries == block) is asserted before any write;
  - undated lines (a correction blockquote, stray prose) ride with the preceding
    dated entry — split_entries only ever begins an entry on a dated bullet, so
    every entry select() sees carries a date; a malformed date-less entry (which
    cannot arise from the real parse) sorts as oldest so it can never displace a
    genuinely-newest dated entry from the keep set;
  - timestamped pre-rotation backup;
  - atomic temp-file + os.replace (no reader sees a torn file);
  - archive is written BEFORE the backlog is shrunk, so the only failure mode
    duplicates entries (recoverable) rather than dropping them;
  - --dry-run prints the plan and writes nothing.

Owner: fold into the efficiency-steward routine (Fri 03:30 ET) next to
rotate_bridge.py. Size-gated, so a clean week is a no-op. Safe to run by hand.
"""
import argparse
import datetime
import os
import re
import shutil
import sys
import tempfile

BACKLOG = "/Users/steve/Documents/3SK/outputs/06_CEO/Automation_Backlog.md"
ARCHIVE = "/Users/steve/Documents/3SK/outputs/06_CEO/Automation_Backlog_ScanHistory_Archive.md"
CAP_BYTES = 150 * 1024  # gate on total file size (matches the bridge cap)  # mutequiv: soft tuning cap, overridable via --cap; ±1 on 150 or 1024 has no test-observable effect short of an impractical ~150KB fixture
KEEP = 8                # newest scan-history entries kept live

# The scan-history block runs from this header to the first candidate heading.
SCAN_HEADER = "**Scan history:**"
# The block ends at the first "### ... High priority" heading below the header
# (the candidate section). A unique, stable anchor — scan prose has no code
# fences, so no `### ` inside the block can be mistaken for it.
BLOCK_END_RE = re.compile(r"^###\s.*High priority", re.IGNORECASE)
# A scan-history entry begins with a dated bullet: "- YYYY-MM-DD".
ENTRY_RE = re.compile(r"^- (\d{4}-\d{2}-\d{2})\b")

ARCHIVE_FRONTMATTER = """---
type: automation-backlog-archive
status: archive
purpose: Rotated `Scan history:` entries from Automation_Backlog.md (A-64 rotation rule). Newest ~8 scan notes stay in the live backlog; everything older lands here, newest rotation at the bottom.
related:
  - "[[Automation_Backlog]]"
tags:
  - automation/backlog-archive
---

# Automation Backlog — Scan-history ARCHIVE
"""


def find_scan_block(text):
    """Return (before, block, after) with before + block + after == text.

    block spans the `**Scan history:**` header line through (not including) the
    first "High priority" candidate heading below it. Returns None if either
    anchor is missing (fail-closed — the caller writes nothing).
    """
    lines = text.splitlines(keepends=True)
    header_idx = None
    for i, ln in enumerate(lines):
        if ln.strip() == SCAN_HEADER:
            header_idx = i
            break
    if header_idx is None:
        return None
    end_idx = None
    for i in range(header_idx + 1, len(lines)):
        if BLOCK_END_RE.match(lines[i]):
            end_idx = i
            break
    if end_idx is None:
        return None
    before = "".join(lines[:header_idx])
    block = "".join(lines[header_idx:end_idx])
    after = "".join(lines[end_idx:])
    return before, block, after


def split_entries(block):
    """Return (preamble, entries).

    preamble = the `**Scan history:**` header + any lines before the first dated
    bullet. entries = list of strings, each a dated bullet through (not
    including) the next dated bullet, so an undated line (blank / blockquote
    correction) rides with the entry above it. preamble + "".join(entries)
    reproduces block byte-for-byte.
    """
    lines = block.splitlines(keepends=True)
    starts = [i for i, ln in enumerate(lines) if ENTRY_RE.match(ln)]
    if not starts:
        return block, []
    preamble = "".join(lines[: starts[0]])
    entries = []
    for idx, s in enumerate(starts):
        e = starts[idx + 1] if idx + 1 < len(starts) else len(lines)
        entries.append("".join(lines[s:e]))
    return preamble, entries


def entry_date(entry):
    """The `YYYY-MM-DD` a dated entry opens with, or None if unparseable."""
    m = ENTRY_RE.match(entry)
    return m.group(1) if m else None


def select(entries, keep):
    """Partition entries into (kept, moved), preserving original order in each.

    Keeps the `keep` newest by (date, original-index). Real entries always carry
    a date (split_entries begins each on a dated bullet); a malformed date-less
    entry sorts as oldest ("0000-00-00") so it can never bump a genuinely-newest
    dated entry out of the keep set.
    """
    indexed = list(enumerate(entries))

    def rank(pair):
        i, e = pair
        return (entry_date(e) or "0000-00-00", i)

    keep_idx = {i for i, _ in sorted(indexed, key=rank, reverse=True)[:keep]}
    kept = [e for i, e in indexed if i in keep_idx]
    moved = [e for i, e in indexed if i not in keep_idx]
    return kept, moved


def _atomic_write(path, content):
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp_rotate_bl_", suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        try:
            mode = os.stat(path).st_mode & 0o777
        except FileNotFoundError:
            mode = 0o644
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):  # mutequiv: best-effort cleanup on a write failure; only reachable via fault injection, out of unit-test scope
            os.unlink(tmp)
        raise


def _b(s):
    return len(s.encode("utf-8"))


def main():
    ap = argparse.ArgumentParser(description="Size-gated atomic rotation of the Automation_Backlog scan-history block (A-64).")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, write nothing")
    ap.add_argument("--force", action="store_true", help="rotate even if under the size cap")
    ap.add_argument("--keep", type=int, default=KEEP, help=f"newest scan entries to keep live (default {KEEP})")
    ap.add_argument("--cap", type=int, default=CAP_BYTES, help=f"total-file size cap in bytes (default {CAP_BYTES})")
    ap.add_argument("--backlog", default=BACKLOG)
    ap.add_argument("--archive", default=ARCHIVE)
    args = ap.parse_args()

    if args.keep < 1:
        print("rotate_backlog: --keep must be >= 1")
        return 1
    if not os.path.exists(args.backlog):
        print(f"rotate_backlog: backlog not found: {args.backlog}")
        return 1

    with open(args.backlog, encoding="utf-8") as f:
        text = f.read()
    size = _b(text)

    if size <= args.cap and not args.force:
        print(f"rotate_backlog: {size} B <= cap {args.cap} B — no rotation needed (no-op).")
        return 0

    found = find_scan_block(text)
    if found is None:
        print("rotate_backlog: ABORT — could not locate the scan-history block (header or High-priority anchor missing). Wrote nothing.")
        return 2
    before, block, after = found
    preamble, entries = split_entries(block)

    # Fail-closed: the split must reproduce the file exactly, or refuse to write.
    # mutequiv: defense-in-depth assert; find_scan_block/split_entries are pure
    # keepends slicing so reconstruction is exact — this guard never fires on any
    # reachable input, so its condition and rc-2 cannot be killed by a fixture.
    if before + preamble + "".join(entries) + after != text:
        print("rotate_backlog: ABORT — byte-conservation check failed (rebuilt != original). Wrote nothing.")
        return 2  # mutequiv: unreachable — the guard above never fires, so this rc can't be exercised

    kept, moved = select(entries, args.keep)
    if not moved:
        print(
            f"rotate_backlog: {len(entries)} scan entries <= keep {args.keep} — nothing to move (no-op). "
            f"File is {size} B; if over cap, the candidate section (not scan history) is the cause."
        )
        return 0

    new_text = before + preamble + "".join(kept) + after
    moved_text = "".join(moved)
    today = datetime.date.today().isoformat()

    if os.path.exists(args.archive):
        with open(args.archive, encoding="utf-8") as f:
            arch = f.read()
    else:
        arch = ARCHIVE_FRONTMATTER
    if not arch.endswith("\n"):
        arch += "\n"
    new_arch = arch + f"\n## Rotated {today} — {len(moved)} entries\n\n" + moved_text

    # Archive must contain every moved byte verbatim.
    # mutequiv: new_arch is `arch + header + moved_text`, so moved_text is always
    # a substring — this fail-closed guard never fires on reachable input, so its
    # condition and rc-2 cannot be killed by a fixture.
    if moved_text not in new_arch:
        print("rotate_backlog: ABORT — moved entries not fully present in new archive. Wrote nothing.")
        return 2  # mutequiv: unreachable — the guard above never fires, so this rc can't be exercised

    oldest_moved = entry_date(moved[0]) or "(undated)"
    newest_moved = entry_date(moved[-1]) or "(undated)"

    if args.dry_run:
        print("rotate_backlog DRY-RUN (no files written):")
        print(f"  scan entries : {len(entries)}  -> keep {len(kept)} live, move {len(moved)} to archive")
        print(f"  backlog bytes: {size} -> {_b(new_text)}")
        print(f"  archive bytes: {_b(arch)} -> {_b(new_arch)}")
        print("  conservation : OK (before + preamble + entries + after == original)")
        print(f"  moving dates : {oldest_moved} .. {newest_moved} (original order)")
        return 0

    stamp = datetime.datetime.now().strftime("%Y-%m-%dT%H%M%S")
    bak = f"{args.backlog}.bak-pre-rotate-{stamp}"
    shutil.copy2(args.backlog, bak)

    # Archive first (additive) so a mid-failure duplicates rather than drops.
    _atomic_write(args.archive, new_arch)
    _atomic_write(args.backlog, new_text)

    print(
        f"rotate_backlog: moved {len(moved)} scan entries to archive; "
        f"backlog {size} B -> {_b(new_text)} B; kept {len(kept)} newest; backup {bak}"
    )
    return 0


if __name__ == "__main__":  # mutequiv: standard entrypoint guard, not reachable under unittest import
    sys.exit(main())
