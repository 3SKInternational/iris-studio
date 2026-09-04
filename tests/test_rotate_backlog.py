#!/usr/bin/env python3
"""Tests for rotate_backlog.py — the Automation_Backlog scan-history rotation (A-64).

Run: python3 -m unittest tests.test_rotate_backlog
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import rotate_backlog as rb  # noqa: E402


# A backlog fixture whose scan block is NOT in file-position date order: the
# newest note (08-31) is prepended at the top, older notes run below, and a
# correction blockquote rides mid-block — exactly the real file's shape.
FIXTURE = """---
frontmatter
---

## Candidates

<!-- LIVE_AUTOBUILD_QUEUE:BEGIN -->
queue rows here — must never be touched
<!-- LIVE_AUTOBUILD_QUEUE:END -->

**Scan history:**
- 2026-08-31 (Mon) — newest, prepended at the top.
- 2026-05-27 — initial sweep.
- 2026-05-28 (Thu) — first fire.
- 2026-06-01 (Mon) — second fire.
- 2026-06-04 (Thu) — third fire.

- 2026-06-08 (Mon) — fourth fire, note the blank line above.
- 2026-08-20 (Thu) — a fire with a correction below it.

> ⚠️ CAUSALITY CORRECTION — this undated blockquote rides with 08-20.

- 2026-08-24 (Mon) — last bullet in the block.


### 🔥 High priority

**A-99 — a candidate row that must survive untouched.**
- detail line.
"""


def _write(tmp, name, content):
    p = os.path.join(tmp, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write(content)
    return p


class TestSplit(unittest.TestCase):
    def test_find_block_conserves_bytes(self):
        before, block, after = rb.find_scan_block(FIXTURE)
        self.assertEqual(before + block + after, FIXTURE)
        self.assertTrue(block.startswith("**Scan history:**"))
        self.assertIn("### 🔥 High priority", after)
        self.assertNotIn("High priority", block)

    def test_split_entries_conserves_and_counts(self):
        _, block, _ = rb.find_scan_block(FIXTURE)
        preamble, entries = rb.split_entries(block)
        self.assertEqual(preamble + "".join(entries), block)
        self.assertEqual(len(entries), 8)  # 8 dated bullets
        self.assertTrue(preamble.startswith("**Scan history:**"))

    def test_correction_rides_with_its_entry(self):
        _, block, _ = rb.find_scan_block(FIXTURE)
        _, entries = rb.split_entries(block)
        aug20 = [e for e in entries if e.startswith("- 2026-08-20")][0]
        self.assertIn("CAUSALITY CORRECTION", aug20)

    def test_missing_anchors_return_none(self):
        self.assertIsNone(rb.find_scan_block("no scan header here"))
        self.assertIsNone(rb.find_scan_block("**Scan history:**\n- 2026-01-01 x\n"))

    def test_high_priority_immediately_after_header(self):
        # Degenerate empty block: the end-anchor sits one line below the header.
        # The end-search must start at header+1 (not +2), or it misses it.
        txt = "pre\n**Scan history:**\n### 🔥 High priority\ncand\n"
        before, block, after = rb.find_scan_block(txt)
        self.assertEqual(before + block + after, txt)
        self.assertEqual(block, "**Scan history:**\n")
        self.assertTrue(after.startswith("### 🔥 High priority"))

    def test_split_entries_empty_block(self):
        block = "**Scan history:**\n\n"
        preamble, entries = rb.split_entries(block)
        self.assertEqual(entries, [])
        self.assertEqual(preamble, block)


class TestSelect(unittest.TestCase):
    def test_keeps_newest_by_date_not_position(self):
        _, block, _ = rb.find_scan_block(FIXTURE)
        _, entries = rb.split_entries(block)
        kept, moved = rb.select(entries, keep=3)
        kept_dates = {rb.entry_date(e) for e in kept}
        # The 3 newest dates, regardless of file position (08-31 is at the top).
        self.assertEqual(kept_dates, {"2026-08-31", "2026-08-24", "2026-08-20"})
        self.assertTrue(all(d < "2026-08-20" for d in (rb.entry_date(e) for e in moved)))

    def test_preserves_original_order(self):
        _, block, _ = rb.find_scan_block(FIXTURE)
        _, entries = rb.split_entries(block)
        kept, moved = rb.select(entries, keep=3)
        # 08-31 was first in the file, so it stays first among kept.
        self.assertEqual(rb.entry_date(kept[0]), "2026-08-31")
        # moved keeps its file order: 05-27 before 05-28.
        self.assertEqual(rb.entry_date(moved[0]), "2026-05-27")
        self.assertEqual(rb.entry_date(moved[1]), "2026-05-28")

    def test_undated_entry_never_displaces_real_newest(self):
        # A date-less entry can't arise from split_entries (it only begins an
        # entry on a dated bullet), but if one is forced in it must sort oldest
        # so the genuinely-newest dated entry still wins the keep slot.
        entries = ["- 2026-01-01 old\n", "- not-a-date stray\n", "- 2026-09-01 new\n"]
        kept, moved = rb.select(entries, keep=1)
        self.assertEqual([rb.entry_date(e) for e in kept], ["2026-09-01"])
        self.assertIn("- not-a-date stray\n", moved)


class TestMainRotation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.backlog = _write(self.tmp, "Automation_Backlog.md", FIXTURE)
        self.archive = os.path.join(self.tmp, "Automation_Backlog_ScanHistory_Archive.md")

    def _run(self, *extra):
        argv = sys.argv
        sys.argv = ["rotate_backlog.py", "--backlog", self.backlog,
                    "--archive", self.archive, *extra]
        try:
            return rb.main()
        finally:
            sys.argv = argv

    def test_force_rotation_keeps_candidates_and_conserves(self):
        rc = self._run("--force", "--keep", "3")
        self.assertEqual(rc, 0)
        new = open(self.backlog, encoding="utf-8").read()
        arch = open(self.archive, encoding="utf-8").read()
        # Candidate section + queue markers untouched.
        self.assertIn("A-99 — a candidate row that must survive untouched.", new)
        self.assertIn("LIVE_AUTOBUILD_QUEUE:BEGIN", new)
        self.assertIn("### 🔥 High priority", new)
        # Newest 3 stayed; older moved.
        self.assertIn("- 2026-08-31", new)
        self.assertIn("- 2026-08-24", new)
        self.assertNotIn("- 2026-05-27", new)
        self.assertIn("- 2026-05-27", arch)
        # The correction blockquote moved with 08-20? No — 08-20 is kept here.
        self.assertIn("CAUSALITY CORRECTION", new)
        # Every original dated bullet is in exactly one of the two files.
        for d in ("2026-08-31", "2026-05-27", "2026-05-28", "2026-06-01",
                  "2026-06-04", "2026-06-08", "2026-08-20", "2026-08-24"):
            here = (f"- {d}" in new) + (f"- {d}" in arch)
            self.assertEqual(here, 1, f"{d} should be in exactly one file")
        # Backup written.
        baks = [f for f in os.listdir(self.tmp) if ".bak-pre-rotate-" in f]
        self.assertEqual(len(baks), 1)
        # Fresh archive (frontmatter ends in \n): exactly one blank line before
        # the rotation header, never a doubled one.
        self.assertIn("ARCHIVE\n\n## Rotated", arch)
        self.assertNotIn("ARCHIVE\n\n\n## Rotated", arch)

    def test_moved_correction_rides_when_its_entry_moves(self):
        # keep=1 -> only 08-31 stays; 08-20 (and its correction) move.
        self._run("--force", "--keep", "1")
        arch = open(self.archive, encoding="utf-8").read()
        self.assertIn("CAUSALITY CORRECTION", arch)
        new = open(self.backlog, encoding="utf-8").read()
        self.assertNotIn("CAUSALITY CORRECTION", new)

    def test_under_cap_is_noop(self):
        rc = self._run()  # default cap 150 KB, fixture is tiny
        self.assertEqual(rc, 0)
        self.assertEqual(open(self.backlog, encoding="utf-8").read(), FIXTURE)
        self.assertFalse(os.path.exists(self.archive))

    def test_keep_ge_count_is_noop(self):
        rc = self._run("--force", "--keep", "50")
        self.assertEqual(rc, 0)
        self.assertEqual(open(self.backlog, encoding="utf-8").read(), FIXTURE)
        self.assertFalse(os.path.exists(self.archive))

    def test_dry_run_writes_nothing(self):
        rc = self._run("--force", "--keep", "3", "--dry-run")
        self.assertEqual(rc, 0)
        self.assertEqual(open(self.backlog, encoding="utf-8").read(), FIXTURE)
        self.assertFalse(os.path.exists(self.archive))

    def test_second_rotation_appends_to_archive(self):
        self._run("--force", "--keep", "3")
        first = open(self.archive, encoding="utf-8").read()
        # Re-run with keep=1 pushes two more (08-20 + 08-24's neighbours) across.
        self._run("--force", "--keep", "1")
        second = open(self.archive, encoding="utf-8").read()
        self.assertGreater(len(second), len(first))
        self.assertEqual(second.count("# Automation Backlog — Scan-history ARCHIVE"), 1)
        self.assertEqual(second.count("## Rotated "), 2)  # two rotation headers

    def test_abort_when_block_missing(self):
        _write(self.tmp, "Automation_Backlog.md", "no scan header at all\n")
        rc = self._run("--force")
        self.assertEqual(rc, 2)

    def test_keep_zero_rejected(self):
        self.assertEqual(self._run("--force", "--keep", "0"), 1)

    def test_missing_backlog_returns_1(self):
        argv = sys.argv
        sys.argv = ["rotate_backlog.py", "--backlog", os.path.join(self.tmp, "nope.md"),
                    "--archive", self.archive, "--force"]
        try:
            self.assertEqual(rb.main(), 1)
        finally:
            sys.argv = argv

    def test_under_cap_is_noop_even_with_movable_entries(self):
        # keep=3 WOULD move 5, but the file is under the default cap and there is
        # no --force, so it must no-op untouched — proves the size gate, not the
        # entry count, is what suppresses the rotation here.
        rc = self._run("--keep", "3")
        self.assertEqual(rc, 0)
        self.assertEqual(open(self.backlog, encoding="utf-8").read(), FIXTURE)
        self.assertFalse(os.path.exists(self.archive))

    def test_archive_new_file_mode_is_0644(self):
        self._run("--force", "--keep", "3")
        self.assertEqual(os.stat(self.archive).st_mode & 0o777, 0o644)

    def test_dry_run_reports_moved_date_span(self):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self._run("--force", "--keep", "3", "--dry-run")
        # keep=3 keeps 08-31/08-24/08-20; oldest..newest MOVED by file order.
        self.assertIn("moving dates : 2026-05-27 .. 2026-06-08", buf.getvalue())

    def test_default_keep_is_8(self):
        # A backlog with 11 dated scan entries + --force, no --keep, must keep 8.
        bullets = "".join(f"- 2026-{m:02d}-01 (x) — fire {m}.\n" for m in range(1, 12))
        big = ("**Scan history:**\n" + bullets + "\n### 🔥 High priority\n**A-1 row.**\n")
        _write(self.tmp, "Automation_Backlog.md", big)
        self._run("--force")  # default keep = 8
        new = open(self.backlog, encoding="utf-8").read()
        arch = open(self.archive, encoding="utf-8").read()
        self.assertEqual(new.count("- 2026-"), 8)   # newest 8 (May..Nov) kept
        self.assertEqual(arch.count("- 2026-"), 3)   # oldest 3 (Jan..Mar) moved
        self.assertIn("- 2026-01-01", arch)
        self.assertIn("- 2026-04-01", new)

    def test_appends_after_non_newline_terminated_archive(self):
        # An archive whose tail lacks a trailing newline must get exactly one
        # blank line before the rotation header — never glued, never doubled.
        with open(self.archive, "w", encoding="utf-8") as f:
            f.write("hand-edited tail no newline")
        self._run("--force", "--keep", "3")
        arch = open(self.archive, encoding="utf-8").read()
        self.assertIn("no newline\n\n## Rotated", arch)
        self.assertNotIn("no newline\n\n\n## Rotated", arch)


if __name__ == "__main__":
    unittest.main()
