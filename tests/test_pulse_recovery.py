#!/usr/bin/env python3
"""Tests for scripts/pulse_recovery.py (A-78) — the Cowork pulse miss-recovery net.

Stdlib unittest only (repo convention). Run:
    python3 tests/test_pulse_recovery.py
or under the suite:
    python3 -m unittest discover -s tests

Pure functions are exercised against tmp dirs; no network, no notify, no live vault.
"""
import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import pulse_recovery as pr  # noqa: E402

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 8, 31, 10, 0, tzinfo=UTC)


class _Tmp(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.addCleanup(self._td.cleanup)


# --- detection ------------------------------------------------------------- #
class TestDetection(_Tmp):
    def test_digest_present_is_detected(self):
        (self.tmp / "2026-08-31_0030_Nightly_Value_Pulse.md").write_text("x")
        self.assertIsNotNone(pr.find_pulse_digest(self.tmp, "2026-08-31"))

    def test_digest_missing_returns_none(self):
        (self.tmp / "2026-08-30_0030_Nightly_Value_Pulse.md").write_text("x")  # yesterday
        self.assertIsNone(pr.find_pulse_digest(self.tmp, "2026-08-31"))

    def test_glob_matches_pass17_byte_for_byte(self):
        # a suffixed non-digest (e.g. a practice-run file) must NOT count as a digest —
        # recovery's glob must equal pulse_audit.sh's exactly, else the two observers
        # disagree and recovery silently no-ops on a night Pass 17 calls a miss.
        (self.tmp / "2026-08-31_0030_Nightly_Value_Pulse_Practice_Run.md").write_text("x")
        self.assertIsNone(pr.find_pulse_digest(self.tmp, "2026-08-31"))
        # the real digest shape still matches
        (self.tmp / "2026-08-31_0030_Nightly_Value_Pulse.md").write_text("x")
        self.assertIsNotNone(pr.find_pulse_digest(self.tmp, "2026-08-31"))


# --- publish runway -------------------------------------------------------- #
def _receipt(tmp, name, **fields):
    (tmp / f"{name}_youtube_upload.json").write_text(json.dumps(fields))


class TestParse(unittest.TestCase):
    def test_non_string_is_none_not_a_crash(self):
        # the `or` guard must reject a non-str (a numeric publish_at) before .strip()
        self.assertIsNone(pr._parse_utc(12345))
        self.assertIsNone(pr._parse_utc(None))

    def test_parses_z_suffix(self):
        d = pr._parse_utc("2026-08-31T10:00:00Z")
        self.assertEqual(d, dt.datetime(2026, 8, 31, 10, 0, tzinfo=UTC))


class TestRunway(_Tmp):
    def test_receipt_count_is_exact(self):
        _receipt(self.tmp, "Video_01", uploaded_at="2026-06-18T00:00:00+00:00")
        _receipt(self.tmp, "Video_02", uploaded_at="2026-06-25T00:00:00+00:00")
        self.assertEqual(pr.publish_runway(self.tmp, NOW)["n_receipts"], 2)

    def test_runway_zero_when_nothing_scheduled_ahead(self):
        _receipt(self.tmp, "Video_01", publish_at=None, uploaded_at="2026-06-18T16:56:08+00:00")
        r = pr.publish_runway(self.tmp, NOW)
        self.assertEqual(r["runway_days"], 0.0)
        self.assertEqual(r["n_scheduled"], 0)
        self.assertGreater(r["days_dark"], 70)  # dark since June

    def test_runway_counts_future_publish(self):
        _receipt(self.tmp, "Video_20", publish_at="2026-09-05T14:00:00+00:00")
        r = pr.publish_runway(self.tmp, NOW)
        self.assertEqual(r["n_scheduled"], 1)
        self.assertTrue(4.5 < r["runway_days"] < 5.5)  # ~5 days out

    def test_runway_ignores_deleted_receipts(self):
        _receipt(self.tmp, "Video_04", publish_at="2026-09-05T14:00:00+00:00",
                 status="deleted_from_channel")
        r = pr.publish_runway(self.tmp, NOW)
        self.assertEqual(r["n_scheduled"], 0)
        self.assertEqual(r["n_receipts"], 0)

    def test_runway_glob_is_pinned_to_video(self):
        # a Shorts receipt must NOT count toward runway
        (self.tmp / "Short_01_youtube_upload.json").write_text(
            json.dumps({"publish_at": "2026-09-05T14:00:00+00:00"}))
        r = pr.publish_runway(self.tmp, NOW)
        self.assertEqual(r["n_scheduled"], 0)
        self.assertEqual(r["n_receipts"], 0)

    def test_days_dark_uses_latest_channel_write(self):
        # a re-publish (last_published_at later than publish_at) sets the go-live age
        _receipt(self.tmp, "Video_02", publish_at="2026-06-18T00:00:00+00:00",
                 last_published_at="2026-08-30T00:00:00+00:00")
        r = pr.publish_runway(self.tmp, NOW)
        self.assertLess(r["days_dark"], 2)  # ~1 day, not ~74

    def test_future_last_published_is_not_a_golive(self):
        # a scheduled-ahead publish_at must not also count as a past go-live
        _receipt(self.tmp, "Video_20", publish_at="2026-09-05T14:00:00+00:00")
        r = pr.publish_runway(self.tmp, NOW)
        self.assertIsNone(r["days_dark"])

    def test_runway_rounds_to_one_decimal(self):
        # 5 d 6 h = 5.25 d -> round(,1)=5.2 (distinct from 5.25 at 2 dp: pins the precision)
        pub = (NOW + dt.timedelta(days=5, hours=6)).isoformat()
        _receipt(self.tmp, "Video_20", publish_at=pub)
        self.assertEqual(pr.publish_runway(self.tmp, NOW)["runway_days"], 5.2)

    def test_days_dark_rounds_to_one_decimal(self):
        # 8 d 10 h = 8.4166.. d -> round(,1)=8.4 (distinct from 8.42 at 2 dp)
        up = (NOW - dt.timedelta(days=8, hours=10)).isoformat()
        _receipt(self.tmp, "Video_01", uploaded_at=up)
        self.assertEqual(pr.publish_runway(self.tmp, NOW)["days_dark"], 8.4)


# --- deadline sweep -------------------------------------------------------- #
class TestDeadline(unittest.TestCase):
    def test_deadline_due_flags_past_and_today(self):
        rows = [
            {"id": "DQ-50", "deadline": "2026-09-05 (7d)"},        # future
            {"id": "DQ-49", "deadline": "2026-08-31 (post-trip)"},  # today
            {"id": "DQ-44", "deadline": "2026-08-06 (72h)"},        # past
            {"id": "DQ-8", "deadline": "trigger: first ~100 subscribers"},  # non-calendar
        ]
        self.assertEqual(pr.deadline_due_ids(rows, "2026-08-31"), ["DQ-49", "DQ-44"])

    def test_deadline_ignores_blank_and_trigger_rows(self):
        rows = [{"id": "DQ-1", "deadline": ""}, {"id": "DQ-2", "deadline": None}]
        self.assertEqual(pr.deadline_due_ids(rows, "2026-08-31"), [])


# --- idempotent INBOX write ------------------------------------------------ #
class TestInbox(_Tmp):
    def test_inbox_insert_then_replace_is_idempotent(self):
        inbox = self.tmp / "INBOX.md"
        inbox.write_text(f"# Inbox\n\n{pr.INBOX_ANCHOR}\n## TODAY\nbody\n")
        block1 = pr.build_stub("2026-08-31",
                               {"runway_days": 0.0, "n_scheduled": 0, "days_dark": 9.0, "n_receipts": 14},
                               ["DQ-49"], None)
        self.assertTrue(pr.upsert_inbox_block(inbox, block1))
        t0 = inbox.read_text()
        self.assertEqual(t0.count(pr.INBOX_BEGIN), 1)
        # block goes RIGHT AFTER the anchor, not appended at EOF
        self.assertIn(pr.INBOX_ANCHOR + "\n" + pr.INBOX_BEGIN, t0)
        # second, identical run: no change
        self.assertFalse(pr.upsert_inbox_block(inbox, block1))
        # a changed block replaces in place, still exactly one block
        block2 = pr.build_stub("2026-08-31",
                               {"runway_days": 5.0, "n_scheduled": 1, "days_dark": None, "n_receipts": 14},
                               [], None)
        self.assertTrue(pr.upsert_inbox_block(inbox, block2))
        t2 = inbox.read_text()
        self.assertEqual(t2.count(pr.INBOX_BEGIN), 1)
        self.assertIn("Runway 5.0 days", t2)
        self.assertIn("## TODAY", t2)  # anchor content preserved

    def test_inbox_never_forges_a_pulse_digest_name(self):
        inbox = self.tmp / "INBOX.md"
        inbox.write_text(f"# Inbox\n{pr.INBOX_ANCHOR}\n")
        block = pr.build_stub("2026-08-31",
                              {"runway_days": 0.0, "n_scheduled": 0, "days_dark": 9.0, "n_receipts": 14},
                              [], None)
        pr.upsert_inbox_block(inbox, block)
        self.assertNotIn("Nightly_Value_Pulse", inbox.read_text())


# --- idempotent daily marker ----------------------------------------------- #
class TestDaily(_Tmp):
    def test_daily_marker_added_once(self):
        daily = self.tmp / "2026-08-31.md"
        daily.write_text("# 2026-08-31\n")
        self.assertTrue(pr.append_daily_marker(daily, "05:30", "recovery ran"))
        self.assertFalse(pr.append_daily_marker(daily, "05:31", "recovery ran again"))
        self.assertEqual(daily.read_text().count(pr.DAILY_MARKER), 1)

    def test_daily_marker_creates_content_when_file_missing(self):
        daily = self.tmp / "2026-08-31.md"  # does not exist yet
        self.assertTrue(pr.append_daily_marker(daily, "05:30", "recovery ran"))
        self.assertIn(pr.DAILY_HEADING, daily.read_text())


# --- build_stub text branches --------------------------------------------- #
class TestStubText(unittest.TestCase):
    RW = {"runway_days": 0.0, "n_scheduled": 0, "days_dark": 9.0, "n_receipts": 14}
    RW_NODARK = {"runway_days": 0.0, "n_scheduled": 0, "days_dark": None, "n_receipts": 14}

    def test_days_dark_text_when_present(self):
        self.assertIn("9.0 days dark", pr.build_stub("2026-08-31", self.RW, [], None))

    def test_no_golive_text_when_dark_none(self):
        self.assertIn("no go-live on record", pr.build_stub("2026-08-31", self.RW_NODARK, [], None))

    def test_sweep_error_branch(self):
        out = pr.build_stub("2026-08-31", self.RW, ["DQ-1"], "deadline sweep unavailable (ImportError)")
        self.assertIn("deadline sweep unavailable", out)
        self.assertNotIn("Deadline-due rows", out)  # error wins over due list

    def test_due_ids_branch(self):
        out = pr.build_stub("2026-08-31", self.RW, ["DQ-49", "DQ-44"], None)
        self.assertIn("Deadline-due rows (today or past):** DQ-49, DQ-44", out)

    def test_no_due_branch(self):
        out = pr.build_stub("2026-08-31", self.RW, [], None)
        self.assertIn("no open row is due today or overdue", out)


# --- INBOX append path (no markers, no anchor) ----------------------------- #
class TestInboxAppend(_Tmp):
    def test_append_when_no_marker_or_anchor(self):
        inbox = self.tmp / "INBOX.md"
        inbox.write_text("# Inbox\nno markers here\n")
        block = pr.build_stub("2026-08-31", TestStubText.RW, [], None)
        self.assertTrue(pr.upsert_inbox_block(inbox, block))
        t = inbox.read_text()
        self.assertEqual(t.count(pr.INBOX_BEGIN), 1)
        self.assertTrue(t.rstrip().endswith(pr.INBOX_END))  # appended at end

    def test_half_marker_does_not_silently_noop(self):
        # a stray BEGIN with no END must NOT make the write a no-op (the `and` guard)
        inbox = self.tmp / "INBOX.md"
        inbox.write_text(f"# Inbox\n{pr.INBOX_ANCHOR}\n{pr.INBOX_BEGIN}\nstray, no end\n")
        block = pr.build_stub("2026-08-31", TestStubText.RW, [], None)
        self.assertTrue(pr.upsert_inbox_block(inbox, block))  # wrote something
        self.assertIn(pr.INBOX_END, inbox.read_text())        # a complete block now exists


# --- daily marker with heading already present ----------------------------- #
class TestDailyHeadingPresent(_Tmp):
    def test_marker_inserted_under_existing_heading(self):
        daily = self.tmp / "2026-08-31.md"
        daily.write_text(f"# Day\n\n{pr.DAILY_HEADING}\n- 02:00 — existing\n")
        self.assertTrue(pr.append_daily_marker(daily, "05:30", "recovery ran"))
        lines = daily.read_text().splitlines()
        i = lines.index(pr.DAILY_HEADING)
        self.assertIn(pr.DAILY_MARKER, lines[i + 1])  # new line right under the heading
        self.assertEqual(daily.read_text().count(pr.DAILY_HEADING), 1)  # heading not duplicated


# --- notify guard ---------------------------------------------------------- #
class TestNotify(_Tmp):
    def test_notify_skipped_when_script_missing(self):
        with mock.patch.object(pr, "NOTIFY", self.tmp / "nope.sh"), \
             mock.patch.object(pr.subprocess, "run") as run:
            pr._notify("x")
            run.assert_not_called()

    def test_notify_runs_when_script_present(self):
        script = self.tmp / "notify.sh"
        script.write_text("#!/bin/bash\n")
        with mock.patch.object(pr, "NOTIFY", script), \
             mock.patch.object(pr.subprocess, "run") as run:
            pr._notify("x")
            run.assert_called_once()


# --- main() / run_recovery() orchestration --------------------------------- #
def _ok_log(log_dir, job, when, extra=""):
    """Write a claude-code-<job>.log whose last success line is at `when` (local)."""
    (log_dir / f"claude-code-{job}.log").write_text(
        f"run_claude_job: '{job}' completed ok at {when:%Y-%m-%d %H:%M} EDT\n{extra}")


def _agents(dir_, *jobs):
    """A LaunchAgents dir holding plists for `jobs` (only those count as real runs)."""
    dir_.mkdir(exist_ok=True)
    for j in jobs:
        (dir_ / f"com.iris.claude-code-{j}.plist").write_text("x")
    return dir_


# --- fleet liveness -------------------------------------------------------- #
LOCAL_NOW = dt.datetime(2026, 10, 10, 5, 30)


class TestFleetLiveness(_Tmp):
    def setUp(self):
        super().setUp()
        self.logs = self.tmp / "logs"; self.logs.mkdir()
        self.agents = _agents(self.tmp / "agents", "nightly", "pre-brief")

    def last(self):
        return pr.last_claude_success(self.logs, LOCAL_NOW, self.agents)

    def test_newest_success_across_logs(self):
        _ok_log(self.logs, "nightly", dt.datetime(2026, 9, 20, 3, 0))
        _ok_log(self.logs, "pre-brief", dt.datetime(2026, 9, 21, 5, 3))
        self.assertEqual(self.last(), dt.datetime(2026, 9, 21, 5, 3))

    def test_newest_line_within_one_log(self):
        (self.logs / "claude-code-nightly.log").write_text(
            "run_claude_job: 'nightly' completed ok at 2026-09-21 03:05 EDT\n"
            "run_claude_job: 'nightly' completed ok at 2026-09-19 03:05 EDT\n")
        self.assertEqual(self.last(), dt.datetime(2026, 9, 21, 3, 5))

    def test_selftest_log_does_not_count(self):
        _ok_log(self.logs, "nightly", dt.datetime(2026, 9, 21, 3, 0))
        _ok_log(self.logs, "WRAPPER-SELFTEST", dt.datetime(2026, 10, 10, 5, 0))
        self.assertEqual(self.last(), dt.datetime(2026, 9, 21, 3, 0))

    def test_non_claude_logs_ignored(self):
        (self.logs / "job-pipeline-sweep.log").write_text(
            "run_claude_job: 'x' completed ok at 2026-10-10 05:00 EDT\n")
        self.assertIsNone(self.last())

    def test_failure_lines_are_not_success(self):
        (self.logs / "claude-code-nightly.log").write_text(
            "run_claude_job: 'nightly' AUTH/CREDIT FAILURE at 2026-10-10 03:00 EDT\n"
            "run_claude_job: 'nightly' completed WITHOUT sentinel at 2026-10-10 03:10 EDT\n"
            "run_claude_job: 'nightly' INCOMPLETE at 2026-10-10 03:20 EDT\n")
        self.assertIsNone(self.last())

    def test_job_without_plist_does_not_count(self):
        # hand tests of run_claude_job.sh (e2ejob, testjob2, ...) write real-looking lines
        _ok_log(self.logs, "nightly", dt.datetime(2026, 9, 21, 3, 0))
        _ok_log(self.logs, "e2ejob", dt.datetime(2026, 10, 10, 5, 0))
        self.assertEqual(self.last(), dt.datetime(2026, 9, 21, 3, 0))

    def test_future_dated_line_ignored(self):
        (self.logs / "claude-code-nightly.log").write_text(
            "run_claude_job: 'nightly' completed ok at 2026-09-21 03:00 EDT\n"
            "run_claude_job: 'nightly' completed ok at 2027-01-01 03:00 EDT\n")
        self.assertEqual(self.last(), dt.datetime(2026, 9, 21, 3, 0))

    def test_line_exactly_now_counts(self):
        _ok_log(self.logs, "nightly", LOCAL_NOW)
        self.assertEqual(self.last(), LOCAL_NOW)

    def test_impossible_date_skipped_not_fatal(self):
        (self.logs / "claude-code-nightly.log").write_text(
            "run_claude_job: 'nightly' completed ok at 2026-13-01 00:00 EDT\n"
            "run_claude_job: 'nightly' completed ok at 2026-09-21 03:00 EDT\n")
        self.assertEqual(self.last(), dt.datetime(2026, 9, 21, 3, 0))


    def test_dark_threshold_edges(self):
        self.assertIsNone(pr.fleet_dark_hours(LOCAL_NOW - dt.timedelta(hours=47), LOCAL_NOW))
        self.assertIsNone(pr.fleet_dark_hours(LOCAL_NOW - dt.timedelta(hours=48), LOCAL_NOW))
        h = pr.fleet_dark_hours(LOCAL_NOW - dt.timedelta(hours=49), LOCAL_NOW)
        self.assertAlmostEqual(h, 49.0)
        self.assertEqual(pr.fleet_dark_hours(None, LOCAL_NOW), float("inf"))

    def test_auth_failure_seen_in_recent_tail(self):
        _ok_log(self.tmp, "nightly", dt.datetime(2026, 9, 21, 3, 0),
                extra="Failed to authenticate. API Error: 401 OAuth access token has expired.\n")
        self.assertTrue(pr.auth_failure_seen(self.tmp, dt.datetime.now() - dt.timedelta(hours=24)))

    def test_auth_failure_ignored_in_old_file(self):
        _ok_log(self.tmp, "nightly", dt.datetime(2026, 9, 21, 3, 0), extra="API Error: 401\n")
        import os
        old = (dt.datetime.now() - dt.timedelta(days=3)).timestamp()
        os.utime(self.tmp / "claude-code-nightly.log", (old, old))
        self.assertFalse(pr.auth_failure_seen(self.tmp, dt.datetime.now() - dt.timedelta(hours=24)))

    def test_credit_error_counts_as_auth(self):
        _ok_log(self.tmp, "nightly", dt.datetime(2026, 9, 21, 3, 0),
                extra="Your account is out of usage credits.\n")
        self.assertTrue(pr.auth_failure_seen(self.tmp, dt.datetime.now() - dt.timedelta(hours=24)))

    def test_no_auth_failure(self):
        _ok_log(self.tmp, "nightly", dt.datetime(2026, 9, 21, 3, 0), extra="EPERM on /Volumes\n")
        self.assertFalse(pr.auth_failure_seen(self.tmp, dt.datetime.now() - dt.timedelta(hours=24)))

    def test_line_wording(self):
        last = dt.datetime(2026, 9, 21, 5, 33)
        auth = pr.fleet_dark_line(18 * 24.0, last, True)
        self.assertIn("FLEET DARK 18.0 days", auth)
        self.assertIn("since 2026-09-21 05:33", auth)
        self.assertIn("401", auth)
        self.assertIn("credit", auth)
        other = pr.fleet_dark_line(50.0, last, False)
        self.assertIn("FLEET DARK 2.1 days", other)
        self.assertIn("Full Disk Access", other)
        self.assertNotIn("401", other)
        self.assertIn("no scheduled Claude job has a completed run on record",
                      pr.fleet_dark_line(float("inf"), None, False))

    def test_ping_throttle(self):
        state = self.tmp / "s"
        now = dt.datetime(2026, 10, 10, 9, 30, tzinfo=UTC)
        self.assertTrue(pr.ping_due(state, now))                      # first dark morning
        pr.mark_pinged(state, now)
        self.assertFalse(pr.ping_due(state, now + dt.timedelta(hours=71)))
        self.assertTrue(pr.ping_due(state, now + dt.timedelta(hours=72)))
        state.write_text("garbage")
        self.assertTrue(pr.ping_due(state, now))                      # unreadable -> ping
        pr.clear_fleet_state(state)
        self.assertFalse(state.exists())
        pr.clear_fleet_state(state)                                   # absent is fine


class TestMain(_Tmp):
    def _wire(self, stack):
        """Point module globals at tmp paths; stub the pure computations + notify."""
        sess = self.tmp / "Sessions"; sess.mkdir()
        daily = self.tmp / "Daily"; daily.mkdir()
        inbox = self.tmp / "INBOX.md"
        inbox.write_text(f"# Inbox\n{pr.INBOX_ANCHOR}\n## TODAY\n")
        stack.enter_context(mock.patch.object(pr, "SESSIONS_DIR", sess))
        stack.enter_context(mock.patch.object(pr, "DAILY_DIR", daily))
        stack.enter_context(mock.patch.object(pr, "INBOX", inbox))
        stack.enter_context(mock.patch.object(
            pr, "publish_runway",
            return_value={"runway_days": 0.0, "n_scheduled": 0, "days_dark": 9.0, "n_receipts": 14}))
        # Hermetic fleet check: a Claude success one hour ago (alive), and a tmp
        # ping-state file, so these tests never read the real logs or state.
        logs = self.tmp / "logs"; logs.mkdir()
        self.fleet_state = self.tmp / "fleet_dark.state"
        _ok_log(logs, "nightly", dt.datetime.now() - dt.timedelta(hours=1))
        self.logs = logs
        stack.enter_context(mock.patch.object(pr, "CLAUDE_LOG_DIR", logs))
        stack.enter_context(mock.patch.object(pr, "FLEET_STATE", self.fleet_state))
        stack.enter_context(mock.patch.object(pr, "LAUNCH_AGENTS", _agents(self.tmp / "agents", "nightly")))
        # each test patches open_deadline_due itself (so a hermetic value is used,
        # never the live Decision_Queue).
        return sess, daily, inbox

    def test_digest_present_is_noop_exit_75(self):
        import contextlib
        with contextlib.ExitStack() as stack:
            sess, daily, inbox = self._wire(stack)
            (sess / "2026-08-31_0030_Nightly_Value_Pulse.md").write_text("x")
            notify = stack.enter_context(mock.patch.object(pr, "_notify"))
            rc = pr.main(["--today", "2026-08-31"])
            self.assertEqual(rc, pr.EXIT_NOOP)
            self.assertEqual(rc, 75)  # literal EX_TEMPFAIL — run_job.sh stays silent on this
            notify.assert_not_called()
            self.assertNotIn(pr.INBOX_BEGIN, inbox.read_text())  # no write on no-op

    def test_miss_writes_and_pings_once(self):
        import contextlib
        with contextlib.ExitStack() as stack:
            sess, daily, inbox = self._wire(stack)
            stack.enter_context(mock.patch.object(pr, "open_deadline_due", return_value=(["DQ-49"], None)))
            notify = stack.enter_context(mock.patch.object(pr, "_notify"))
            rc = pr.main(["--today", "2026-08-31"])   # no digest exists -> miss
            self.assertEqual(rc, 0)
            self.assertIn(pr.INBOX_BEGIN, inbox.read_text())
            self.assertIn(pr.DAILY_MARKER, (daily / "2026-08-31.md").read_text())
            notify.assert_called_once()
            # second, same-day run: idempotent, no second ping
            rc2 = pr.main(["--today", "2026-08-31"])
            self.assertEqual(rc2, 0)
            notify.assert_called_once()

    def test_force_miss_ignores_present_digest(self):
        import contextlib
        with contextlib.ExitStack() as stack:
            sess, daily, inbox = self._wire(stack)
            stack.enter_context(mock.patch.object(pr, "open_deadline_due", return_value=([], None)))
            (sess / "2026-08-31_0030_Nightly_Value_Pulse.md").write_text("x")  # digest present
            stack.enter_context(mock.patch.object(pr, "_notify"))
            rc = pr.main(["--today", "2026-08-31", "--force-miss"])
            self.assertEqual(rc, 0)
            self.assertIn(pr.INBOX_BEGIN, inbox.read_text())  # recovered despite the digest

    def test_dry_run_writes_nothing(self):
        import contextlib
        with contextlib.ExitStack() as stack:
            sess, daily, inbox = self._wire(stack)
            stack.enter_context(mock.patch.object(pr, "open_deadline_due", return_value=([], None)))
            notify = stack.enter_context(mock.patch.object(pr, "_notify"))
            rc = pr.main(["--today", "2026-08-31", "--force-miss", "--dry-run"])
            self.assertEqual(rc, 0)
            self.assertNotIn(pr.INBOX_BEGIN, inbox.read_text())
            self.assertFalse((daily / "2026-08-31.md").exists())
            notify.assert_not_called()

    def test_today_override_is_used_for_detection(self):
        # with the or->and mutant on `a.today or now`, detection would use now's date
        # and miss this digest; assert the override date drives the lookup.
        import contextlib
        with contextlib.ExitStack() as stack:
            sess, daily, inbox = self._wire(stack)
            (sess / "2026-01-15_0030_Nightly_Value_Pulse.md").write_text("x")
            stack.enter_context(mock.patch.object(pr, "_notify"))
            rc = pr.main(["--today", "2026-01-15"])
            self.assertEqual(rc, pr.EXIT_NOOP)


    def _go_dark(self, days=18, extra="Failed to authenticate. API Error: 401\n"):
        for f in self.logs.iterdir():
            f.unlink()
        _ok_log(self.logs, "nightly", dt.datetime.now() - dt.timedelta(days=days), extra=extra)

    def test_fleet_dark_overrides_present_digest_and_pings_once(self):
        import contextlib
        with contextlib.ExitStack() as stack:
            sess, daily, inbox = self._wire(stack)
            self._go_dark()
            (sess / "2026-08-31_0030_Nightly_Value_Pulse.md").write_text("x")  # pulse fired
            notify = stack.enter_context(mock.patch.object(pr, "_notify"))
            self.assertEqual(pr.main(["--today", "2026-08-31"]), 0)
            text = inbox.read_text()
            self.assertIn("FLEET DARK 18", text)
            self.assertIn("401", text)
            self.assertNotIn("Deadline", text)           # DQ list dropped in this state
            self.assertIn("FLEET DARK 18", (daily / "2026-08-31.md").read_text())
            notify.assert_called_once()
            self.assertIn("FLEET DARK", notify.call_args[0][0])
            # same morning re-run, and the next morning: inside 72h -> no second ping
            pr.main(["--today", "2026-08-31"])
            pr.main(["--today", "2026-09-01"])
            notify.assert_called_once()
            self.assertIn("FLEET DARK", (daily / "2026-09-01.md").read_text())

    def test_fleet_alive_clears_ping_state(self):
        import contextlib
        with contextlib.ExitStack() as stack:
            sess, daily, inbox = self._wire(stack)
            self.fleet_state.write_text("123\n")
            (sess / "2026-08-31_0030_Nightly_Value_Pulse.md").write_text("x")
            stack.enter_context(mock.patch.object(pr, "_notify"))
            self.assertEqual(pr.main(["--today", "2026-08-31"]), pr.EXIT_NOOP)
            self.assertFalse(self.fleet_state.exists())

    def test_fleet_dark_dry_run_writes_nothing(self):
        import contextlib
        with contextlib.ExitStack() as stack:
            sess, daily, inbox = self._wire(stack)
            self._go_dark()
            before = inbox.read_text()
            notify = stack.enter_context(mock.patch.object(pr, "_notify"))
            self.assertEqual(pr.main(["--today", "2026-08-31", "--dry-run"]), 0)
            self.assertEqual(inbox.read_text(), before)
            self.assertFalse((daily / "2026-08-31.md").exists())
            self.assertFalse(self.fleet_state.exists())
            notify.assert_not_called()

    def test_unreadable_vault_still_pings(self):
        import contextlib
        with contextlib.ExitStack() as stack:
            sess, daily, inbox = self._wire(stack)
            self._go_dark()
            stack.enter_context(mock.patch.object(pr, "INBOX", self.tmp / "missing" / "INBOX.md"))
            notify = stack.enter_context(mock.patch.object(pr, "_notify"))
            self.assertEqual(pr.main(["--today", "2026-08-31"]), 0)
            notify.assert_called_once()
            self.assertTrue(self.fleet_state.exists())

    def test_recovery_clears_stale_fleet_block(self):
        import contextlib
        with contextlib.ExitStack() as stack:
            sess, daily, inbox = self._wire(stack)
            self._go_dark()
            stack.enter_context(mock.patch.object(pr, "_notify"))
            pr.main(["--today", "2026-08-31"])
            self.assertIn("FLEET DARK", inbox.read_text())
            # next day: a fresh success and the pulse fired
            _ok_log(self.logs, "nightly", dt.datetime.now() - dt.timedelta(hours=1))
            (sess / "2026-09-01_0030_Nightly_Value_Pulse.md").write_text("x")
            self.assertEqual(pr.main(["--today", "2026-09-01"]), pr.EXIT_NOOP)
            text = inbox.read_text()
            self.assertNotIn("FLEET DARK", text)
            self.assertIn("✅ Fleet back", text)
            self.assertEqual(text.count(pr.INBOX_BEGIN), 1)

    def test_recovery_leaves_pulse_missed_block_alone(self):
        import contextlib
        with contextlib.ExitStack() as stack:
            sess, daily, inbox = self._wire(stack)
            stack.enter_context(mock.patch.object(pr, "open_deadline_due", return_value=([], None)))
            stack.enter_context(mock.patch.object(pr, "_notify"))
            pr.main(["--today", "2026-08-31"])                       # alive, pulse missed
            before = inbox.read_text()
            (sess / "2026-09-01_0030_Nightly_Value_Pulse.md").write_text("x")
            pr.main(["--today", "2026-09-01"])
            self.assertEqual(inbox.read_text(), before)

    def test_fleet_dark_heading_is_the_headline(self):
        import contextlib
        with contextlib.ExitStack() as stack:
            sess, daily, inbox = self._wire(stack)
            self._go_dark()
            stack.enter_context(mock.patch.object(pr, "_notify"))
            pr.main(["--today", "2026-08-31"])
            self.assertIn("\n## 🚨 FLEET DARK 18.0 days (2026-08-31)\n", inbox.read_text())

    def test_auth_error_older_than_24h_is_not_blamed(self):
        # A 401 in a log last written 24.5h ago is stale: the cause line must not
        # claim auth. Pins the 24h look-back window.
        import contextlib, os
        with contextlib.ExitStack() as stack:
            sess, daily, inbox = self._wire(stack)
            self._go_dark()
            old = (dt.datetime.now() - dt.timedelta(hours=24, minutes=30)).timestamp()
            os.utime(self.logs / "claude-code-nightly.log", (old, old))
            stack.enter_context(mock.patch.object(pr, "_notify"))
            pr.main(["--today", "2026-08-31"])
            self.assertNotIn("401", inbox.read_text())
            self.assertIn("Full Disk Access", inbox.read_text())

    def test_dry_run_while_alive_keeps_ping_state(self):
        import contextlib
        with contextlib.ExitStack() as stack:
            sess, daily, inbox = self._wire(stack)
            self.fleet_state.write_text("123\n")
            stack.enter_context(mock.patch.object(pr, "open_deadline_due", return_value=([], None)))
            stack.enter_context(mock.patch.object(pr, "_notify"))
            pr.main(["--today", "2026-08-31", "--dry-run"])
            self.assertTrue(self.fleet_state.exists())

    def test_fleet_dark_without_auth_error_names_other_causes(self):
        import contextlib
        with contextlib.ExitStack() as stack:
            sess, daily, inbox = self._wire(stack)
            self._go_dark(days=3, extra="")
            stack.enter_context(mock.patch.object(pr, "_notify"))
            pr.main(["--today", "2026-08-31"])
            self.assertIn("Full Disk Access", inbox.read_text())


if __name__ == "__main__":
    unittest.main()
