#!/usr/bin/env python3
"""Tests for scripts/conflict_copies.py (A-89 detector). Stdlib unittest. Run:
    python3 tests/test_conflict_copies.py
"""
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("conflict_copies", REPO / "scripts" / "conflict_copies.py")
cc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cc)


class _Flagged:
    """A real stat result with st_flags replaced."""
    def __init__(self, st, flags):
        self._st, self.st_flags = st, flags

    def __getattr__(self, name):
        return getattr(self._st, name)


class TestConflictCopies(unittest.TestCase):
    def setUp(self):
        self.v = Path(tempfile.mkdtemp())
        self.now = time.time()

    def tearDown(self):
        shutil.rmtree(self.v)

    def put(self, rel, text, age_days=0.0):
        p = self.v / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        t = self.now - age_days * 86400
        os.utime(p, (t, t))

    def scan(self, days=7):
        return dict((rel, s) for s, rel in cc.scan(self.v, days, now=self.now))

    def test_canonical_name(self):
        self.assertEqual(cc.canonical_name("Foo 2.md"), "Foo.md")
        self.assertEqual(cc.canonical_name("x.md.2026-10-20 2.bak"), "x.md.2026-10-20.bak")
        self.assertEqual(cc.canonical_name("Decision_Queue 2.md"), "Decision_Queue.md")
        self.assertIsNone(cc.canonical_name("Video 2 notes.md"))
        self.assertIsNone(cc.canonical_name("Foo2.md"))
        self.assertIsNone(cc.canonical_name("Foo 12.md"))
        self.assertEqual(cc.canonical_name("Decision_Queue 3.md"), "Decision_Queue.md")
        self.assertEqual(cc.canonical_name("Foo 9.md"), "Foo.md")
        self.assertIsNone(cc.canonical_name("Foo 1.md"))
        self.assertIsNone(cc.canonical_name("Foo 2"))
        self.assertIsNone(cc.canonical_name("Foo.md"))

    def test_statuses(self):
        self.put("a.md", "same", 1)
        self.put("a 2.md", "same", 1)
        self.put("b.md", "new canon", 1)
        self.put("b 2.md", "old copy", 2)
        self.put("c.md", "old canon", 2)
        self.put("c 2.md", "new copy", 1)
        self.put("sub/d 2.md", "orphan", 1)
        self.assertEqual(self.scan(), {
            "a 2.md": "identical",
            "b 2.md": "copy-older",
            "c 2.md": "copy-newer",
            "sub/d 2.md": "no-canonical",
        })

    def test_same_size_different_bytes_not_identical(self):
        self.put("e.md", "aaaa", 2)
        self.put("e 2.md", "bbbb", 1)
        self.assertEqual(self.scan()["e 2.md"], "copy-newer")

    def test_age_window_and_archive_skipped(self):
        self.put("old 2.md", "x", 7 + 3 / 86400)
        self.put("edge 2.md", "x", 7 - 3 / 86400)
        self.put("07_Archive/f 2.md", "x", 0)
        self.put(".git/g 2.md", "x", 0)
        self.put(".trash/t 2.md", "x", 0)
        self.assertEqual(set(self.scan()), {"edge 2.md"})

    def test_same_mtime_different_bytes_is_not_safe(self):
        self.put("s.md", "canon", 1)
        self.put("s 2.md", "COPY!", 1)
        self.assertEqual(self.scan()["s 2.md"], "copy-newer")

    def test_unreadable_dir_reported(self):
        self.put("lock/z 2.md", "x", 0)
        (self.v / "lock").chmod(0)
        try:
            self.assertEqual(self.scan(), {"lock": "unreadable-dir"})
        finally:
            (self.v / "lock").chmod(0o755)

    def _with_flags(self, flags):
        self.put("h.md", "same", 1)
        self.put("h 2.md", "same", 1)
        real = Path.stat

        def fake(p, *a, **k):
            st = real(p, *a, **k)
            return _Flagged(st, flags) if p.name == "h.md" else st
        with mock.patch.object(Path, "stat", fake):
            return cc.classify(self.v / "h 2.md", self.v / "h.md")

    def test_dataless_canonical_not_read(self):
        with mock.patch.object(cc.filecmp, "cmp", side_effect=AssertionError("read a stub")):
            self.assertEqual(self._with_flags(cc.SF_DATALESS), "dataless")

    def test_other_flag_bits_are_not_dataless(self):
        self.assertEqual(self._with_flags(0x1), "identical")

    def test_cli_summary_and_exit_zero(self):
        self.put("a.md", "same")
        self.put("a 2.md", "same")
        r = subprocess.run([sys.executable, str(REPO / "scripts" / "conflict_copies.py"),
                            "--vault", str(self.v)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
        self.assertIn("identical     a 2.md", r.stdout)
        self.assertIn("1 conflict copy in the last 7 days (identical 1)", r.stdout)

    def test_cli_none(self):
        r = subprocess.run([sys.executable, str(REPO / "scripts" / "conflict_copies.py"),
                            "--vault", str(self.v)], capture_output=True, text=True)
        self.assertIn("0 conflict copies in the last 7 days (none)", r.stdout)


if __name__ == "__main__":
    unittest.main()
