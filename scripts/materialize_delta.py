#!/usr/bin/env python3
"""Pre-materialize the changed delta before an rclone backup read (DQ-52).

The vault lives under ~/Documents, which iCloud "Optimize Mac Storage" evicts:
file contents become APFS `dataless` stubs, and a read of a stub fails EDEADLK.
rclone's dry-run only compares size+modtime (never reads bytes), so it passes
green — then the real sync reads the changed delta, deadlocks on an evicted
file, and the whole offsite backup wedges (had to be hand-recovered two nights
running until Steve flips the toggle).

This helper reads the rclone --dry-run log drive-sync already produced, finds
the files the real sync would upload, and for each one that is currently
unreadable (an evicted stub) runs `brctl download` to rehydrate it — bounded by
free disk so we never mass-download the whole evicted store onto a near-full
volume. Best-effort: anything it can't fix is left for the real sync to hit and
alert on, exactly as before. Always exits 0 — the real sync stays the source of
truth for success/failure.

Usage: materialize_delta.py <rclone-dryrun-log> <vault-root>

ponytail: best-effort read-side self-heal. The real fix is Steve's iCloud
"Optimize Mac Storage" OFF toggle (DQ-52); this only shrinks the recurring
manual-recovery tax until then.
"""
import os
import re
import shutil
import subprocess
import sys

BRCTL = os.environ.get("BRCTL", "/usr/bin/brctl")
# Leave this much headroom on the volume; refuse to materialize past it so we
# can't fill a near-full disk (DQ-52: 16 GiB free on a 93%-full volume; a single
# evicted 700 MB video in the delta must not be allowed to snowball).
DEFAULT_MARGIN = 3 * 1024 ** 3  # mutequiv: safety-headroom knob, no exact-value contract (any few-GiB margin works)

# rclone dry-run line, e.g.:
#   2026/09/05 03:45:40 NOTICE: 06_CEO/Automation_Backlog.md: Skipped copy as --dry-run is set (size 228.540Ki)
_LINE = re.compile(
    r"NOTICE:\s+(?P<rel>.+?): Skipped copy as --dry-run is set"
    r"(?: \(size (?P<num>[\d.]+)\s*(?P<unit>[KMGTP]i|B)?\))?"
)
_UNIT = {None: 1, "B": 1, "Ki": 1024, "Mi": 1024 ** 2,
         "Gi": 1024 ** 3, "Ti": 1024 ** 4, "Pi": 1024 ** 5}


def parse_dryrun(text):
    """[(relpath, size_bytes)] for every file the real sync would upload."""
    out = []
    for m in _LINE.finditer(text):
        num, unit = m.group("num"), m.group("unit")
        size = int(float(num) * _UNIT[unit]) if num is not None else 0
        out.append((m.group("rel"), size))
    return out


def readable(path):
    """True if the first byte reads without error. An evicted stub raises
    OSError (EDEADLK) here instead of auto-downloading — that IS the wedge."""
    try:
        with open(path, "rb") as f:
            f.read(1)  # mutequiv: read(1) vs read(2) identical — any byte-touch trips the dataless deadlock
        return True
    except OSError:
        return False


def disk_free(path):
    return shutil.disk_usage(path).free


def run_brctl(path):
    """Rehydrate one file. Best-effort; brctl failure is handled by the caller
    re-probing readability, not by trusting the return code."""
    subprocess.run([BRCTL, "download", path],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   timeout=120, check=False)  # mutequiv: 120s brctl timeout is a tunable knob, no exact-value contract


def materialize(entries, vault, margin=DEFAULT_MARGIN):
    """Rehydrate the evicted files in `entries`, smallest first, within budget.
    Returns a counts dict. Pure of argv/exit so it's unit-testable."""
    evicted = []
    for rel, size in entries:
        p = os.path.join(vault, rel)
        if os.path.exists(p) and not readable(p):
            evicted.append((size, rel, p))
    evicted.sort()  # smallest first: clear the most files per byte of disk

    budget = max(0, disk_free(vault) - margin)  # mutequiv: floor 0 vs 1 — a 0-byte file materializes either way, negative budget skips either way
    counts = {"checked": len(entries), "evicted": len(evicted),
              "materialized": 0, "skipped_too_big": 0, "failed": 0}
    for size, rel, p in evicted:
        if size > budget:
            counts["skipped_too_big"] += 1
            print(f"materialize_delta: SKIP (disk budget) {rel} ({size} B)")
            continue
        run_brctl(p)
        if readable(p):
            counts["materialized"] += 1
            budget -= size
            print(f"materialize_delta: materialized {rel}")
        else:
            counts["failed"] += 1
            print(f"materialize_delta: STILL UNREADABLE after brctl {rel}")
    return counts


def main(argv):
    if len(argv) != 3:
        print(__doc__.strip().splitlines()[0], file=sys.stderr)  # mutequiv: which usage line prints is cosmetic; the arg-error path's contract (return 0) is tested
        print("usage: materialize_delta.py <rclone-dryrun-log> <vault-root>",
              file=sys.stderr)
        return 0  # never block the sync on our own arg error
    dryrun_log, vault = argv[1], argv[2]
    # If we can't even read the always-warm anchor, this interpreter has no
    # vault read access (FDA/TCC under launchd — see DQ-42). Don't pretend to
    # help: no-op and let the real sync run exactly as before.
    anchor = os.path.join(vault, "CLAUDE.md")
    if os.path.exists(anchor) and not readable(anchor):
        print("materialize_delta: vault anchor unreadable (no read access) — skipping")
        return 0
    try:
        with open(dryrun_log, "r", errors="replace") as f:
            entries = parse_dryrun(f.read())
    except OSError as e:
        print(f"materialize_delta: could not read dry-run log ({e}) — skipping")
        return 0
    if not entries:
        print("materialize_delta: no would-copy files in dry-run — nothing to do")
        return 0
    c = materialize(entries, vault)
    if c["evicted"] == 0:  # mutequiv: picks which cosmetic summary line prints; return value + side effects identical on both arms
        print(f"materialize_delta: {c['checked']} delta file(s), none evicted")
    else:
        print("materialize_delta: summary "
              f"materialized={c['materialized']} "
              f"skipped_too_big={c['skipped_too_big']} failed={c['failed']} "
              f"of {c['evicted']} evicted ({c['checked']} checked)")
    return 0


if __name__ == "__main__":  # mutequiv: standard entrypoint guard, not exercised under module import
    sys.exit(main(sys.argv))
