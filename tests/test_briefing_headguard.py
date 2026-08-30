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


class TestSplitCombined(unittest.TestCase):
    """The 'briefing never populated' class (7/20-8/10, ~25% of mornings).

    The model is told to emit a bare sentinel between the file half and the
    Telegram half. It frequently emits a '# PART 2 ...' markdown heading instead
    — mirroring the prompt's own scaffolding — and the old parser then fell back
    to the HEAD of the response, sending the FILE half cut at 1800 chars and
    dropping the actual brief entirely.
    """

    def test_canonical_sentinel(self):
        out = f"{GOOD}\n{hg.BRIEFING_SENTINEL}\nGood morning Steve. V14 ships today."
        file_md, brief = hg.split_combined(out)
        self.assertEqual(file_md, GOOD.strip())
        self.assertEqual(brief, "Good morning Steve. V14 ships today.")

    def test_part2_heading_substituted_for_sentinel(self):
        # Verbatim shape captured from the 2026-08-10 reproduction.
        out = (
            "# PART 1: DAILY_BRIEFING.md — 2026-08-10\n\n## Focus\n\nV14 is the lever.\n\n"
            "# PART 2: Telegram Morning Brief — 2026-08-10\n\nGood morning. V14 ships today."
        )
        file_md, brief = hg.split_combined(out)
        self.assertIn("## Focus", file_md)
        self.assertEqual(brief, "Good morning. V14 ships today.")
        # The regression that mattered: the brief is the TAIL, never the head.
        self.assertNotIn("## Focus", brief)

    def test_splits_on_first_boundary_only(self):
        out = f"file\n{hg.BRIEFING_SENTINEL}\nbrief mentioning\n## PART 2 recap\ntail"
        file_md, brief = hg.split_combined(out)
        self.assertEqual(file_md, "file")
        self.assertIn("tail", brief)

    def test_no_boundary_returns_none(self):
        # Must be None, NOT a guessed split — a guess is what shipped the wrong half.
        self.assertIsNone(hg.split_combined("I'll generate both outputs.\n" + GOOD))

    def test_empty_input_returns_none(self):
        self.assertIsNone(hg.split_combined(""))
        self.assertIsNone(hg.split_combined(None))

    def test_empty_part2_returns_empty_brief_not_none(self):
        # Boundary present but nothing after it — caller degrades, parser must not
        # conflate this with "no boundary at all".
        file_md, brief = hg.split_combined(f"{GOOD}\n{hg.BRIEFING_SENTINEL}\n")
        self.assertEqual(brief, "")
        self.assertTrue(file_md)

    def test_sentinel_inside_prose_is_not_a_boundary(self):
        # Only a line whose whole content is the marker counts; an inline mention
        # must not chop the file in half.
        out = f"{GOOD}\nThe daemon splits on {hg.BRIEFING_SENTINEL} each morning.\n"
        self.assertIsNone(hg.split_combined(out))

    def test_heading_before_sentinel_prefers_the_sentinel(self):
        # The dangerous order (review 2026-08-30): a legitimate '## Part 2:'
        # heading inside the PART 1 file body sits BEFORE the real sentinel. The
        # split must fall on the sentinel, not the body heading — otherwise the
        # sentinel line leaks into the top of the brief Steve reads.
        out = (
            "# Daily Briefing — 2026-08-30\n\n"
            "## Part 2: The Catch-Up Phase\n\nA real body section.\n\n"
            f"{hg.BRIEFING_SENTINEL}\n"
            "Good morning Steve."
        )
        file_md, brief = hg.split_combined(out)
        self.assertIn("## Part 2: The Catch-Up Phase", file_md)
        self.assertEqual(brief, "Good morning Steve.")
        self.assertNotIn(hg.BRIEFING_SENTINEL, brief)


if __name__ == "__main__":
    unittest.main(verbosity=2)
