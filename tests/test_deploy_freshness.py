#!/usr/bin/env python3
"""Tests for scripts/deploy_freshness.py (A-79) — daemon deploy-freshness check.

Stdlib unittest only (repo convention). Run:
    python3 tests/test_deploy_freshness.py
or under the suite:
    python3 -m unittest discover -s tests

The load-bearing logic is the pure `evaluate()` verdict and the `process_start`
whitespace-tolerant lstart parse; both are exercised without touching launchctl,
ps, notify, or the live daemon.
"""
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import deploy_freshness as df  # noqa: E402

START = datetime(2026, 9, 2, 2, 3, 56)


class TestEvaluate(unittest.TestCase):
    def test_ok_when_mtime_before_start(self):
        # iris.py edited a day BEFORE the daemon booted → running current code.
        v, _ = df.evaluate(START - timedelta(days=1), START, pid=1465)
        self.assertEqual(v, "OK")

    def test_ok_at_grace_boundary(self):
        # mtime exactly at start+grace is still OK (a restart moments after edit).
        v, _ = df.evaluate(START + df.GRACE, START, pid=1465)
        self.assertEqual(v, "OK")

    def test_drift_when_mtime_after_start_plus_grace(self):
        # The 20-day incident shape: file edited long after the process started.
        v, msg = df.evaluate(START + timedelta(days=20), START, pid=801)
        self.assertEqual(v, "DRIFT")
        self.assertIn("STALE", msg)
        self.assertIn("20d", msg)  # _fmt_delta rendered the lateness

    def test_drift_just_past_grace(self):
        v, _ = df.evaluate(START + df.GRACE + timedelta(seconds=1), START, pid=801)
        self.assertEqual(v, "DRIFT")

    def test_grace_magnitude_pinned(self):
        # Pin GRACE's actual size (not just df.GRACE-relative): 90s inside it is
        # OK, 3min past start is DRIFT. Fails if the 2-minute grace drifts to 3+.
        self.assertEqual(df.evaluate(START + timedelta(seconds=90), START, pid=1)[0], "OK")
        self.assertEqual(df.evaluate(START + timedelta(minutes=3), START, pid=1)[0], "DRIFT")

    def test_unknown_when_daemon_down(self):
        v, msg = df.evaluate(START, START, pid=None)
        self.assertEqual(v, "UNKNOWN")
        self.assertIn("not running", msg)

    def test_unknown_when_start_unreadable(self):
        # Daemon has a PID but ps gave no start-time — must NOT read as OK.
        v, _ = df.evaluate(START, None, pid=1465)
        self.assertEqual(v, "UNKNOWN")

    def test_unknown_when_mtime_unreadable(self):
        v, _ = df.evaluate(None, START, pid=1465)
        self.assertEqual(v, "UNKNOWN")


class TestProcessStart(unittest.TestCase):
    def _run(self, stdout: str):
        with mock.patch.object(df.subprocess, "run",
                               return_value=mock.Mock(stdout=stdout)):
            return df.process_start(1465)

    def test_parses_space_padded_day(self):
        # ps emits a space-padded day: two spaces before a single-digit date.
        got = self._run("Wed Sep  2 02:03:56 2026\n")
        self.assertEqual(got, START)

    def test_parses_two_digit_day(self):
        got = self._run("Sat Aug 30 03:23:56 2026\n")
        self.assertEqual(got, datetime(2026, 8, 30, 3, 23, 56))

    def test_empty_output_is_none(self):
        # Dead PID → ps prints nothing → None, not a crash.
        self.assertIsNone(self._run("\n"))

    def test_garbage_output_is_none(self):
        self.assertIsNone(self._run("not a date"))


class TestDaemonPid(unittest.TestCase):
    def test_extracts_pid(self):
        block = '\t"Label" = "com.iris.studio";\n\t"PID" = 1465;\n'
        with mock.patch.object(df.subprocess, "run",
                               return_value=mock.Mock(returncode=0, stdout=block)):
            self.assertEqual(df.daemon_pid(), 1465)

    def test_no_pid_line_is_none(self):
        # A loaded-but-not-running job lists no "PID" line.
        with mock.patch.object(df.subprocess, "run",
                               return_value=mock.Mock(returncode=0, stdout='\t"Label" = "x";\n')):
            self.assertIsNone(df.daemon_pid())

    def test_nonzero_exit_is_none(self):
        with mock.patch.object(df.subprocess, "run",
                               return_value=mock.Mock(returncode=1, stdout="")):
            self.assertIsNone(df.daemon_pid())

    def test_nonzero_exit_ignores_stale_pid(self):
        # returncode!=0 must win even when a stale "PID" line is still in stdout —
        # else a job that just crashed reads as running on its old pid.
        with mock.patch.object(df.subprocess, "run",
                               return_value=mock.Mock(returncode=1, stdout='\t"PID" = 999;\n')):
            self.assertIsNone(df.daemon_pid())


class TestFmtDelta(unittest.TestCase):
    def test_minutes_only(self):
        self.assertEqual(df._fmt_delta(timedelta(minutes=45)), "45m")

    def test_hours_and_minutes(self):
        self.assertEqual(df._fmt_delta(timedelta(hours=5, minutes=30)), "5h 30m")

    def test_days_and_hours(self):
        self.assertEqual(df._fmt_delta(timedelta(days=2, hours=3, minutes=59)), "2d 3h")

    def test_zero(self):
        self.assertEqual(df._fmt_delta(timedelta(0)), "0m")


class TestNotify(unittest.TestCase):
    def test_skips_when_notify_missing(self):
        with mock.patch.object(df, "NOTIFY", mock.Mock(exists=lambda: False)), \
             mock.patch.object(df.subprocess, "run") as run:
            df._notify("x")
        run.assert_not_called()

    def test_calls_notify_when_present(self):
        with mock.patch.object(df, "NOTIFY", mock.Mock(exists=lambda: True, __str__=lambda s: "/n")), \
             mock.patch.object(df.subprocess, "run") as run:
            df._notify("x")
        run.assert_called_once()


class TestMain(unittest.TestCase):
    def test_drift_pages_and_returns_zero(self):
        with mock.patch.object(df, "iris_mtime", return_value=START + timedelta(days=5)), \
             mock.patch.object(df, "daemon_pid", return_value=801), \
             mock.patch.object(df, "process_start", return_value=START), \
             mock.patch.object(df, "_notify") as notify:
            rc = df.main([])
        self.assertEqual(rc, 0)
        notify.assert_called_once()

    def test_ok_does_not_page(self):
        with mock.patch.object(df, "iris_mtime", return_value=START - timedelta(days=1)), \
             mock.patch.object(df, "daemon_pid", return_value=1465), \
             mock.patch.object(df, "process_start", return_value=START), \
             mock.patch.object(df, "_notify") as notify:
            rc = df.main([])
        self.assertEqual(rc, 0)
        notify.assert_not_called()

    def test_selftest_returns_zero_and_prints(self):
        # --selftest short-circuits to the smoke print and returns 0 without
        # touching the normal verdict/notify path.
        with mock.patch.object(df, "daemon_pid", return_value=1465), \
             mock.patch.object(df, "process_start", return_value=START), \
             mock.patch.object(df, "iris_mtime", return_value=START), \
             mock.patch.object(df, "_notify") as notify, \
             mock.patch("builtins.print") as pr:
            rc = df.main(["--selftest"])
        self.assertEqual(rc, 0)
        notify.assert_not_called()
        self.assertTrue(pr.call_args[0][0].startswith("selftest:"))

    def test_unknown_does_not_page(self):
        # Down daemon is surfaced to INBOX (stdout) but must not flap-page.
        with mock.patch.object(df, "iris_mtime", return_value=START), \
             mock.patch.object(df, "daemon_pid", return_value=None), \
             mock.patch.object(df, "_notify") as notify:
            rc = df.main([])
        self.assertEqual(rc, 0)
        notify.assert_not_called()


if __name__ == "__main__":
    unittest.main()
