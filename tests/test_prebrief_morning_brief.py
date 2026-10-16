"""A-87: the morning-brief fire check reads the daemon's receipt line."""
import gzip
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import prebrief_fire_check as pf  # noqa: E402

EXP = pf.Expected("morning_briefing", datetime(2026, 10, 15, 8, 0), "morning_brief")
NOW = datetime(2026, 10, 16, 5, 0)
FIRING = "2026-10-15 08:00:00,004 - iris - INFO - Morning briefing job firing for chat_id=1\n"


def sent(n, ts="2026-10-15 08:01:19,943"):
    return f"{ts} - iris - INFO - Morning briefing sent ({n} chars)\n"


class MorningBrief(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        for target, val in (("LOGS_DIR", self.dir), ("IRIS_LOG", self.dir / "iris.err.log")):
            p = mock.patch.object(pf, target, val)
            p.start()
            self.addCleanup(p.stop)

    def check(self, live="", rotated=None):
        (self.dir / "iris.err.log").write_text(live)
        if rotated is not None:
            with gzip.open(self.dir / "iris.err.log.20261018_031005.gz", "wt") as f:
                f.write(rotated)
        return pf._check_morning_brief(EXP, NOW)

    def test_healthy_brief_passes(self):
        ok, msg = self.check(FIRING + sent(2300))
        self.assertTrue(ok, msg)
        self.assertIn("2300", msg)

    def test_threshold_is_inclusive_at_500(self):
        self.assertTrue(self.check(sent(500))[0])
        self.assertFalse(self.check(sent(499))[0])

    def test_154_char_stub_flags(self):
        ok, msg = self.check(FIRING + sent(154))
        self.assertFalse(ok)
        self.assertIn("154", msg)

    def test_missing_line_flags(self):
        ok, msg = self.check(FIRING)
        self.assertFalse(ok)
        self.assertIn("did not fire", msg)

    def test_missing_log_flags_not_crashes(self):
        ok, _ = pf._check_morning_brief(EXP, NOW)
        self.assertFalse(ok)

    def test_failed_line_flags_even_with_long_send(self):
        ok, msg = self.check(
            "2026-10-15 08:00:40,000 - iris - ERROR - Morning briefing failed: boom\n"
            + sent(2300))
        self.assertFalse(ok)
        self.assertIn("boom", msg)

    def test_no_part2_boundary_flags(self):
        ok, msg = self.check(
            "2026-10-15 08:00:40,000 - iris - ERROR - Combined briefing has no "
            "PART 2 boundary (neither ...); no brief could be extracted\n"
            + sent(2300))
        self.assertFalse(ok)
        self.assertIn("PART 2", msg)

    def test_lines_outside_window_ignored(self):
        # A healthy send the day before and a later manual run don't count.
        ok, _ = self.check(sent(2300, "2026-10-14 08:01:00,000")
                           + sent(2300, "2026-10-15 10:30:00,000"))
        self.assertFalse(ok)

    def test_window_edges_inclusive(self):
        # hi = 08:00 + 2h grace + 4 min run slack (late start, slow generation)
        self.assertTrue(self.check(sent(2300, "2026-10-15 06:00:00,000"))[0])
        self.assertTrue(self.check(sent(2300, "2026-10-15 10:04:00,999"))[0])
        self.assertFalse(self.check(sent(2300, "2026-10-15 10:04:01,000"))[0])
        self.assertFalse(self.check(sent(2300, "2026-10-15 05:59:59,999"))[0])

    def test_telegram_text_cannot_fake_a_pass(self):
        # iris.py echoes inbound/outbound Telegram text into the same log.
        for echo in (
            "2026-10-15 09:12:00,000 - iris - INFO - From @x: Morning briefing sent (2361 chars)\n",
            "2026-10-15 09:12:00,000 - iris - INFO - To @steve: Morning briefing sent (2361 chars)\n",
            "2026-10-15 09:12:00,000 - iris - WARNING - BLOCKED unauthorized message "
            "text='Morning briefing sent (2361 chars)'\n",
            "Morning briefing sent (2361 chars)\n",
            "2026-10-15 09:12:00,000 - httpx - INFO - Morning briefing sent (2361 chars)\n",
        ):
            ok, _ = self.check(sent(62) + echo)
            self.assertFalse(ok, echo)

    def test_telegram_text_cannot_raise_a_false_alarm(self):
        for echo in (
            "2026-10-15 09:12:00,000 - iris - INFO - From @steve: why no PART 2 boundary?\n",
            "2026-10-15 09:12:00,000 - iris - INFO - To @steve: The Morning briefing failed on 9/22\n",
            "2026-10-15 09:12:00,000 - iris - ERROR - Handler error: Morning briefing failed: x\n",
        ):
            ok, msg = self.check(sent(2485) + echo)
            self.assertTrue(ok, f"{echo!r} -> {msg}")

    def test_pasted_full_log_line_cannot_fake_a_pass(self):
        # The exact r1 payload: a whole older receipt line pasted into Telegram.
        ok, msg = self.check(sent(62) + "2026-10-15 08:30:00,000 - iris - INFO - From @steve: "
                             + sent(2361, "2026-10-13 08:01:12,811"))
        self.assertFalse(ok, msg)

    def test_pasted_full_error_line_cannot_raise_a_false_alarm(self):
        ok, msg = self.check(
            sent(2485) + "2026-10-15 08:30:00,000 - iris - INFO - From @steve: "
            "2026-09-22 08:03:00,000 - iris - ERROR - Morning briefing failed: x\n")
        self.assertTrue(ok, msg)

    def test_sent_line_must_end_after_count(self):
        ok, _ = self.check(sent(2300).rstrip("\n") + " (quoted)\n")
        self.assertFalse(ok)

    def test_truncated_gz_is_skipped_not_crashed(self):
        (self.dir / "iris.err.log.20261018_031005.gz").write_bytes(
            gzip.compress(sent(2300).encode())[:-12])
        (self.dir / "iris.err.log").write_text(sent(2300))
        ok, msg = pf._check_morning_brief(EXP, NOW)
        self.assertTrue(ok, msg)

    def test_rotated_then_live_order_latest_wins(self):
        # The gz holds the earlier line, the live log the later one.
        ok, _ = self.check(sent(154, "2026-10-15 09:00:00,000"),
                           rotated=sent(2300, "2026-10-15 08:01:00,000"))
        self.assertFalse(ok)
        ok, _ = self.check(sent(2300, "2026-10-15 09:00:00,000"),
                           rotated=sent(154, "2026-10-15 08:01:00,000"))
        self.assertTrue(ok)

    def test_latest_send_in_window_wins(self):
        self.assertFalse(self.check(sent(2300, "2026-10-15 08:00:30,000") + sent(154))[0])

    def test_reads_newest_rotated_gz(self):
        # Sunday 03:10 rotation moves Saturday's 08:00 lines into the .gz.
        (self.dir / "iris.err.log.20261011_031005.gz").write_bytes(
            gzip.compress(b""))
        ok, msg = self.check("", rotated=FIRING + sent(2300))
        self.assertTrue(ok, msg)

    def test_older_rotated_gz_not_read(self):
        with gzip.open(self.dir / "iris.err.log.20261011_031005.gz", "wt") as f:
            f.write(sent(2300))
        ok, _ = self.check("", rotated="")
        self.assertFalse(ok)

    def test_error_detail_keeps_dashes_and_is_capped(self):
        ok, msg = self.check(
            "2026-10-15 08:00:40,000 - iris - ERROR - Morning briefing failed: a - b\n")
        self.assertFalse(ok)
        self.assertTrue(msg.endswith("Morning briefing failed: a - b"), msg)
        _, msg = self.check(
            "2026-10-15 08:00:40,000 - iris - ERROR - Morning briefing failed: "
            + "x" * 300 + "\n")
        self.assertEqual(len(msg) - len("daemon logged a brief error: "), 160)


class Selftest(unittest.TestCase):
    def _run(self, fails):
        subs = ("_selftest_youtube_coverage", "_selftest_dispatch_coverage",
                "_selftest_interval_liveness")
        with mock.patch("builtins.print") as out:
            ctx = [mock.patch.object(pf, n, return_value=f) for n, f in zip(subs, fails)]
            for c in ctx:
                c.start()
            try:
                rc = pf._selftest()
            finally:
                for c in ctx:
                    c.stop()
        return rc, out.call_args[0][0]

    def test_clean_selftest_exits_zero(self):
        self.assertEqual(self._run((0, 0, 0)), (0, "SELFTEST OK"))

    def test_failing_selftest_exits_one_and_counts(self):
        self.assertEqual(self._run((1, 0, 2)), (1, "SELFTEST FAILED (3)"))


if __name__ == "__main__":
    unittest.main()
