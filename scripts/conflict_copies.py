#!/usr/bin/env python3
"""List recent ` 2.*` / ` 3.*` ... sync-conflict copies in the vault (A-89 detector).

READ-ONLY. For each `<name> N.<ext>` file (N = 2..9) modified in the last --days days
(default 7), outside 07_Archive/, report how it compares to its canonical file
`<name>.<ext>`:
  identical      same bytes
  copy-older     differs, copy's mtime is older than the canonical's
  copy-newer     differs, copy's mtime is newer or equal (the case a mover must
                 never discard; the mover stays unbuilt until this never shows up)
  no-canonical   the canonical file does not exist
  dataless       one side is an iCloud dataless stub, so bytes were not read
                 (reading it would force a download, or deadlock: DQ-52)
  unreadable     reading failed
  unreadable-dir a directory could not be listed, so copies in it are unseen

Run weekly by routines/hygiene.prompt. Always exits 0.

Usage: conflict_copies.py [--vault PATH] [--days N]
"""
from __future__ import annotations
import argparse
import filecmp
import os
import re
import time
from pathlib import Path

DEFAULT_VAULT = Path("/Users/steve/Documents/3SK/outputs")
SKIP_DIRS = {"07_Archive", ".git", ".trash"}
COPY_RE = re.compile(r"^(.+) [2-9](\.[^/]+)$")   # "Foo 2.md", "Foo 3.md", "x.md.2026-10-20 2.bak"
SF_DATALESS = 0x40000000                      # macOS st_flags bit


def canonical_name(name: str) -> str | None:
    m = COPY_RE.match(name)
    return m.group(1) + m.group(2) if m else None


def classify(copy: Path, canon: Path) -> str:
    if not canon.is_file():
        return "no-canonical"
    try:
        a, b = copy.stat(), canon.stat()
        if (a.st_flags | b.st_flags) & SF_DATALESS:
            return "dataless"
        if a.st_size == b.st_size and filecmp.cmp(copy, canon, shallow=False):
            return "identical"
    except OSError:
        return "unreadable"
    return "copy-newer" if a.st_mtime >= b.st_mtime else "copy-older"


def scan(vault: Path, days: float, now: float | None = None) -> list[tuple[str, str]]:
    cutoff = (now if now is not None else time.time()) - days * 86400
    out = []

    def unreadable_dir(err: OSError) -> None:
        out.append(("unreadable-dir", os.path.relpath(err.filename, vault)))

    for root, dirs, files in os.walk(vault, onerror=unreadable_dir):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            canon = canonical_name(f)
            if canon is None:
                continue
            p = Path(root) / f
            try:
                if p.stat().st_mtime < cutoff:
                    continue
            except OSError:
                continue
            out.append((classify(p, Path(root) / canon), str(p.relative_to(vault))))
    return sorted(out, key=lambda r: r[1])


def main() -> None:
    ap = argparse.ArgumentParser(description="List recent ' 2.*' conflict copies (read-only).")
    ap.add_argument("--vault", type=Path, default=DEFAULT_VAULT)
    ap.add_argument("--days", type=float, default=7)
    args = ap.parse_args()
    rows = scan(args.vault, args.days)
    for status, rel in rows:
        print(f"{status:<13} {rel}")
    counts = {}
    for status, _ in rows:
        counts[status] = counts.get(status, 0) + 1
    summary = ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "none"
    print(f"{len(rows)} conflict cop{'y' if len(rows) == 1 else 'ies'} in the last {args.days:g} days ({summary})")


if __name__ == "__main__":  # mutequiv: forcing True only re-runs a read-only scan at import; no test observes it
    main()
