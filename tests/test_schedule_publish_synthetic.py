#!/usr/bin/env python3
"""schedule_publish.py's altered/synthetic-media disclosure must default ON.

This file previously had NO test suite of any kind, and it is one of only two
paths that write video status. videos.update DELETES any status property the
request omits, and neither schedule-mode nor unschedule-mode can recover
containsSyntheticMedia from `live` (videos.list never returns it to the owner) —
so while the declaration was opt-in via --synthetic, both modes silently stripped
the AI disclosure from the video. Receipts for Video_08/09/10/12 carry this
script's own publish_at format, so that path has run against 4 live videos.

Run: python3 tests/test_schedule_publish_synthetic.py   (exit 0 = pass)
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# schedule_publish.py does `from youtube_client import ...` — a SIBLING import that
# only resolves when scripts/ is on the path. Add it rather than stubbing the module:
# youtube_client is internal, and per precheck.sh's own rule a missing INTERNAL
# import is a FAILURE to surface, not something to paper over.
sys.path.insert(0, str(REPO / "scripts"))

_spec = importlib.util.spec_from_file_location(
    "schedule_publish_under_test", REPO / "scripts" / "schedule_publish.py")
sp = importlib.util.module_from_spec(_spec)
sys.modules["schedule_publish_under_test"] = sp
_spec.loader.exec_module(sp)


class TestWantsSynthetic(unittest.TestCase):

    def test_defaults_to_declaring(self):
        """THE regression. Omitting the flag must still DECLARE — the old opt-in
        meant an ordinary scheduling run deleted the disclosure."""
        self.assertTrue(sp.wants_synthetic(["Video_09=2026-10-01T13:00:00Z"]))
        self.assertTrue(sp.wants_synthetic(["Video_09=2026-10-01T13:00:00Z", "--commit"]))
        self.assertTrue(sp.wants_synthetic([]))

    def test_explicit_opt_out_is_respected(self):
        self.assertFalse(sp.wants_synthetic(["Video_09", "--no-synthetic"]))
        self.assertFalse(sp.wants_synthetic(["--no-synthetic"]))

    def test_opt_out_is_found_anywhere_in_argv(self):
        """Kills the argv-slice mutant: reading from argv[2:] would miss a flag in
        first position and silently declare against an explicit opt-out."""
        self.assertFalse(sp.wants_synthetic(["--no-synthetic", "Video_09", "--commit"]))
        self.assertFalse(sp.wants_synthetic(["Video_09", "--commit", "--no-synthetic"]))

    def test_script_path_in_argv0_does_not_confuse_it(self):
        """The call site passes the WHOLE sys.argv (no slice — a slice was a
        mutable site the tests could not reach). argv[0] is a path, so it must
        never be mistaken for a flag, in either direction."""
        self.assertTrue(sp.wants_synthetic(["scripts/schedule_publish.py", "Video_09"]))
        self.assertFalse(sp.wants_synthetic(
            ["scripts/schedule_publish.py", "Video_09", "--no-synthetic"]))
        self.assertFalse(sp.wants_synthetic(
            ["scripts/schedule_publish.py", "--no-synthetic", "Video_09"]))

    def test_legacy_synthetic_flag_still_declares(self):
        """--synthetic was the old opt-in; scripts and habits still pass it."""
        self.assertTrue(sp.wants_synthetic(["Video_09", "--synthetic"]))

    def test_unrelated_flags_do_not_opt_out(self):
        self.assertTrue(sp.wants_synthetic(["Video_09", "--commit", "--dry-run"]))
        self.assertTrue(sp.wants_synthetic(["--no-synthetics"]))  # near-miss, not the flag


if __name__ == "__main__":
    unittest.main(verbosity=1)
