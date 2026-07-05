#!/usr/bin/env python3
"""Sweep stale *.bak files out of the 3SK vault into 07_Archive/bak_sweep/,
preserving each file's relative path so provenance is obvious and a restore is
a single reverse-move. NON-DESTRUCTIVE (move, never delete) and DRY-RUN by
default — pass --apply to actually move.

Why an age threshold + exclusions: a .bak is often a live safety net for an
in-flight edit, and some .bak families have their own owner/lifecycle. This
sweep only touches backups that are clearly cold and unowned.

SKIPS (never swept):
  - 07_Archive/          already archived (and our own destination)
  - .obsidian/, .git/    tooling state
  - iris-studio-ebook/   the nightly book routine prunes its own dated snapshots
  - any */Backups/ dir   designated backup folders (e.g. 02_Finance/Backups)
  - files newer than --min-age-days (default 7) — fresh = probably in-flight

Usage:
  sweep_baks.py                      # dry-run: list what WOULD move
  sweep_baks.py --apply              # move them
  sweep_baks.py --min-age-days 14    # only sweep baks older than 14 days
  sweep_baks.py --selftest           # offline logic check
"""
from __future__ import annotations
import argparse
import re
import shutil
import sys
import time
from pathlib import Path

DEFAULT_VAULT = Path("/Users/steve/Documents/3SK/outputs")
ARCHIVE_SUBDIR = "07_Archive/bak_sweep"
# dir names anywhere in the path that make a file off-limits
SKIP_DIR_NAMES = {"07_Archive", ".obsidian", ".git", "iris-studio-ebook", "Backups"}
# a .bak marker: ".bak" at end, or ".bak-<tag>". Requiring end-or-dash avoids
# matching a mid-name ".bak." where a real extension follows (e.g. "foo.bak.md").
_BAK_RE = re.compile(r"\.bak($|-)", re.IGNORECASE)


def _age_days(p: Path, now: float) -> float:
    st = p.stat()
    # A cp -p / copy2 backup inherits the SOURCE mtime, so a backup made today of
    # a cold file would look old by mtime alone. ctime (inode-change time) is set
    # to NOW on create/copy and CANNOT be dragged backward by utime/cp -p (unlike
    # birthtime on APFS), so max(mtime, ctime) correctly reads a fresh snapshot as
    # fresh. Conservative side effect: a later chmod/move also bumps ctime, which
    # only ever DELAYS a sweep — the safe direction for an in-flight safety net.
    newest = max(st.st_mtime, st.st_ctime)
    return (now - newest) / 86400


def is_bak(name: str) -> bool:
    return bool(_BAK_RE.search(name))


def skipped_by_dir(rel: Path) -> bool:
    return any(part in SKIP_DIR_NAMES for part in rel.parts)


def find_sweepable(vault: Path, min_age_days: float, now: float) -> list[Path]:
    out: list[Path] = []
    for p in vault.rglob("*"):
        if not p.is_file() or not is_bak(p.name):
            continue
        rel = p.relative_to(vault)
        if skipped_by_dir(rel):
            continue
        if _age_days(p, now) < min_age_days:   # too fresh — likely an in-flight safety net
            continue
        out.append(p)
    return sorted(out)


def _free_dest(dest: Path) -> Path:
    """A destination path that does not exist — never clobber a prior sweep."""
    if not dest.exists():
        return dest
    i = 1
    while True:
        cand = dest.with_name(f"{dest.name}.dup{i}")
        if not cand.exists():
            return cand
        i += 1


def sweep(vault: Path, min_age_days: float, apply: bool) -> int:
    now = time.time()
    files = find_sweepable(vault, min_age_days, now)
    dest_root = vault / ARCHIVE_SUBDIR
    if not files:
        print(f"sweep_baks: nothing to sweep (0 cold .bak files older than {min_age_days}d).")
        return 0
    print(f"sweep_baks: {len(files)} cold .bak file(s) "
          f"{'MOVING' if apply else 'would move'} → {ARCHIVE_SUBDIR}/")
    for p in files:
        rel = p.relative_to(vault)
        age_d = _age_days(p, now)
        if apply:
            dest = _free_dest(dest_root / rel)       # idempotent: never clobber a prior sweep
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(p), str(dest))
            print(f"  [{age_d:4.0f}d] {rel}" + ("" if dest.name == p.name else f"  → {dest.name}"))
        else:
            print(f"  [{age_d:4.0f}d] {rel}")
    if not apply:
        print("\n(dry-run — nothing moved. Re-run with --apply.)")
    return 0


def _selftest() -> int:
    import os
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        v = Path(td)
        # build a tree
        cases = {
            "a/foo.md.bak": True,                       # plain .bak, sweepable
            "a/bar.md.bak-pre-edit": True,              # -tag
            "a/baz.md.2026-06-01.bak": True,            # .date.bak
            "07_Archive/old.md.bak": False,             # in archive
            "iris-studio-ebook/book.md.2026-06-01.bak": False,  # owned lifecycle
            "02_Finance/Backups/x.csv.bak-1": False,    # designated Backups dir
            ".obsidian/w.json.bak": False,              # tooling
            "a/notabak.md": False,                      # not a bak
            "a/config.backup": False,                   # ".backup" is not ".bak"
            "a/mid.bak.md": False,                      # ".bak" mid-name, real ext follows
        }
        for rel in cases:
            f = v / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("x")
        # make everything old enough
        now0 = time.time()
        # Backdate MTIME (os.utime bumps ctime to now as a side effect) — this
        # mimics a cp -p backup: old mtime, recent creation. Age is measured with
        # an INJECTED now so the test doesn't depend on wall-clock aging.
        past = now0 - 30 * 86400
        for rel in cases:
            os.utime(v / rel, (past, past))
        future = now0 + 30 * 86400   # relative to future-now these read 30d old
        found = {str(p.relative_to(v)) for p in find_sweepable(v, 7, future)}
        expected = {rel for rel, want in cases.items() if want}
        ok = found == expected
        # ctime guard: relative to REAL now, these backdated-mtime files read
        # FRESH (ctime≈now), so NOTHING is swept — defeats the cp-p mtime trap.
        fresh_found = bool(find_sweepable(v, 7, now0))
        # collision loop: three generations of the same rel-path never clobber
        d = v / "dest.md.bak"
        n1 = _free_dest(d); n1.write_text("g1")
        n2 = _free_dest(d); n2.write_text("g2")
        n3 = _free_dest(d); n3.write_text("g3")
        no_clobber = len({n1, n2, n3}) == 3 and all(x.exists() for x in (n1, n2, n3))
        checks = {
            "selects exactly the sweepable set (injected future-now)": ok,
            "ctime guard: backdated-mtime files read fresh vs real-now": not fresh_found,
            "is_bak marker": is_bak("x.md.bak") and is_bak("x.bak-1")
                              and not is_bak("x.backup") and not is_bak("x.bak.md"),
            "collision loop never clobbers (3 gens)": no_clobber,
        }
        for name, passed in checks.items():
            print(f"  [{'ok ' if passed else 'FAIL'}] {name}")
        if not ok:
            print(f"    expected={sorted(expected)}\n    found={sorted(found)}")
        allok = all(checks.values())
        print("selftest:", "PASS" if allok else "FAIL")
        return 0 if allok else 1


def main() -> None:
    ap = argparse.ArgumentParser(description="Sweep stale .bak files to 07_Archive/bak_sweep/.")
    ap.add_argument("--vault", type=Path, default=DEFAULT_VAULT)
    ap.add_argument("--min-age-days", type=float, default=7.0,
                    help="only sweep baks older than this (default 7)")
    ap.add_argument("--apply", action="store_true", help="actually move (default: dry-run)")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(_selftest())
    if not a.vault.is_dir():
        ap.error(f"vault not found: {a.vault}")
    sys.exit(sweep(a.vault, a.min_age_days, a.apply))


if __name__ == "__main__":
    main()
