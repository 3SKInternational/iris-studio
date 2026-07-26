#!/usr/bin/env python3
"""Tests for scripts/briefing_headguard.py — the DAILY_BRIEFING head-guard.

Stdlib unittest only (matches the repo convention). Run:
    python3 tests/test_briefing_headguard.py
or under the suite:
    python3 -m unittest discover -s tests -v

Locks the two behaviours the 2026-07-23/24 leak class needs: preamble above the
header is stripped (Task 1), and a headerless / preamble-first file is refused
(so the daemon preserves yesterday's file instead of clobbering it) + surfaced
by the writer-agnostic head check (Task 2).
"""
import importlib.util
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "briefing_headguard",
    Path(__file__).resolve().parent.parent / "scripts" / "briefing_headguard.py",
)
hg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hg)

GOOD = "# Daily Briefing — Thursday, 2026-07-23\n\nSome status text.\n"


class TestStrip(unittest.TestCase):
    def test_header_only_unchanged(self):
        self.assertEqual(hg.strip_briefing_preamble(GOOD), GOOD)

    def test_blob_preamble_above_header_stripped(self):
        # 2026-07-23 shape: an <analysis> blob prepended, header on its own line.
        leaked = "<analysis>chatter about the day</analysis>\n\n" + GOOD
        self.assertEqual(hg.strip_briefing_preamble(leaked), GOOD)

    def test_fused_one_line_preamble_stripped(self):
        # 2026-07-24 shape: conversational lead-in fused onto the header line.
        leaked = (
            "I'll generate both outputs. First, let me check Gmail and Calendar "
            "to inform the briefing." + GOOD
        )
        self.assertEqual(
            hg.strip_briefing_preamble(leaked), GOOD
        )  # slices at the fused '# Daily Briefing'

    def test_no_header_returns_none(self):
        self.assertIsNone(
            hg.strip_briefing_preamble("Sorry, I could not build the briefing.")
        )

    def test_empty_returns_none(self):
        self.assertIsNone(hg.strip_briefing_preamble(""))

    def test_prefers_line_start_over_earlier_prose_mention(self):
        # A prose mention with no newline before a LATER real line-start header:
        # the line-start header wins, prose mention is kept as body (not cut).
        content = "note: writing the # Daily Briefing now\n" + GOOD
        self.assertEqual(hg.strip_briefing_preamble(content), GOOD)


class TestHasValidHead(unittest.TestCase):
    def test_valid(self):
        self.assertTrue(hg.has_valid_head(GOOD))

    def test_leading_blank_lines_ok(self):
        self.assertTrue(hg.has_valid_head("\n\n" + GOOD))

    def test_preamble_first_line_is_corrupt(self):
        self.assertFalse(hg.has_valid_head("I'll generate both outputs.\n" + GOOD))

    def test_empty_is_corrupt(self):
        self.assertFalse(hg.has_valid_head(""))
        self.assertFalse(hg.has_valid_head("   \n\n"))


class TestCli(unittest.TestCase):
    def _write(self, text):
        import tempfile

        p = Path(tempfile.mkstemp(suffix=".md")[1])
        p.write_text(text, encoding="utf-8")
        return p

    def test_cli_pass(self):
        p = self._write(GOOD)
        try:
            self.assertEqual(hg._cli(["--check", str(p)]), 0)
        finally:
            p.unlink()

    def test_cli_corrupt(self):
        p = self._write("I'll generate both outputs.\n" + GOOD)
        try:
            self.assertEqual(hg._cli(["--check", str(p)]), 1)
        finally:
            p.unlink()

    def test_cli_missing_file(self):
        self.assertEqual(hg._cli(["--check", "/no/such/file.md"]), 1)

    def test_cli_usage(self):
        self.assertEqual(hg._cli(["bogus"]), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
