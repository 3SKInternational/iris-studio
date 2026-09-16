"""Tests for scripts/nightly_silence_check.py (A-80 nightly silent-run tripwire).

unittest, NOT pytest: the repo's precheck runner executes each tests/test_*.py
as a plain script (no pytest on the box), so a pytest-fixture file would run zero
tests and read as a false-green — the class precheck exists to catch. No real IO:
git and the ledger/daily/bridge text are passed in or patched.

The load-bearing cases are the record-collision ones — the whole point of the
tripwire is that it must NOT read another job's narration of "nightly-eng silent"
as a nightly-eng record, and must NOT read the "Nightly book-update" header as
one either. If those two pass, the eight-night incident would have fired on night
one.
"""
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import nightly_silence_check as ns  # noqa: E402

SLOT = datetime(2026, 9, 14, 3, 0, 0)


def _ledger(*rows):
    """rows: (session, ts, tool, cwd, summary) -> JSONL text."""
    import json
    return "\n".join(json.dumps(
        {"session": s, "ts": t, "tool": tool, "cwd": cwd, "summary": summ})
        for (s, t, tool, cwd, summ) in rows)


# --- parse_sessions / _is_productive --------------------------------------

class ParseSessions(unittest.TestCase):
    def test_groups_and_spans(self):
        text = _ledger(
            ("A", "2026-09-14T03:00:05", "Bash", ns.REPO, "git status"),
            ("A", "2026-09-14T03:11:00", "Bash", ns.REPO, "precheck"),
            ("B", "2026-09-14T02:30:00", "Edit", str(ns.VAULT), "write book"),
        )
        by = {s.sid: s for s in ns.parse_sessions(text)}
        self.assertEqual(by["A"].n, 2)
        self.assertEqual(by["A"].start, datetime(2026, 9, 14, 3, 0, 5))
        self.assertEqual(by["A"].end, datetime(2026, 9, 14, 3, 11, 0))
        self.assertFalse(by["A"].has_productive)   # two read-only Bash calls
        self.assertTrue(by["B"].has_productive)     # an Edit

    def test_malformed_line_skipped_not_fatal(self):
        text = "not json\n" + _ledger(
            ("A", "2026-09-14T03:00:00", "Bash", ns.REPO, "x")) + "\n{oops"
        self.assertEqual(len(ns.parse_sessions(text)), 1)

    def test_event_missing_session_or_ts_is_skipped_not_crash(self):
        # A valid-JSON event with no "session" key would KeyError if the guard
        # were flipped to `and`; here it must be skipped, leaving one good session.
        import json
        text = (json.dumps({"ts": "2026-09-14T03:00:00", "tool": "Bash"}) + "\n"
                + json.dumps({"session": "X", "tool": "Bash"}) + "\n"  # no ts
                + _ledger(("A", "2026-09-14T03:00:00", "Bash", ns.REPO, "x")))
        sids = {s.sid for s in ns.parse_sessions(text)}
        self.assertEqual(sids, {"A"})

    def test_mcp_write_counts_as_productive(self):
        text = _ledger(("A", "2026-09-14T03:00:00",
                        "mcp__filesystem-vault__write_file", ns.REPO, "w"))
        self.assertTrue(ns.parse_sessions(text)[0].has_productive)

    def test_mcp_read_tool_is_not_productive(self):
        # An mcp READ (get_note) must NOT count — else a session that only read
        # via MCP would look like it produced work. Pins the `and any(...)` branch.
        text = _ledger(("A", "2026-09-14T03:00:00",
                        "mcp__obsidian__obsidian_get_note", ns.REPO, "r"))
        self.assertFalse(ns.parse_sessions(text)[0].has_productive)

    def test_agent_counts_as_productive(self):
        text = _ledger(("A", "2026-09-14T03:00:00", "Agent", ns.REPO, "review"))
        self.assertTrue(ns.parse_sessions(text)[0].has_productive)


# --- bound_session --------------------------------------------------------

class BoundSession(unittest.TestCase):
    def _sess(self, sid, hh, mm, cwd=ns.REPO):
        return ns.Session(sid, datetime(2026, 9, 14, hh, mm), datetime(2026, 9, 14, hh, mm),
                          cwd, 1, False, "x")

    def test_none_when_no_session_in_window(self):
        # book-update at 02:30 is 30 min before the 03:00 slot → outside BEFORE(10).
        self.assertIsNone(ns.bound_session([self._sess("b", 2, 30)], SLOT))

    def test_binds_late_fire_within_after(self):
        s = ns.bound_session([self._sess("n", 3, 40)], SLOT)  # 40 min late, within 45
        self.assertEqual(s.sid, "n")

    def test_nearest_slot_wins_over_adjacent(self):
        # A 03:31 session is 31 min from the 03:00 slot; a 03:02 session is 2 min.
        got = ns.bound_session([self._sess("far", 3, 31), self._sess("near", 3, 2)], SLOT)
        self.assertEqual(got.sid, "near")

    def test_non_cc_cwd_never_binds(self):
        self.assertIsNone(ns.bound_session([self._sess("x", 3, 1, cwd="/tmp/other")], SLOT))


# --- cc_log_author_prefixes: the anti-collision heart ---------------------

class AuthorPrefixes(unittest.TestCase):
    # A real 9/14-shaped daily note: book-update + automation-scan lines, and the
    # automation-scan line NARRATES "nightly-eng builder silent" in its body.
    DAILY_SILENT = (
        "## 🤖 Claude Code intra-day log\n"
        "_Append-only by Claude Code. Format: HH:MM — action → outcome._\n"
        "- 02:39 — book-update: flagship shipped v0.63; p14 noop.\n"
        "- 03:00 — automation scan: 0 reconciled → nightly-eng builder silent 8 "
        "straight nights (last bridge entry 09-06), DQ-52 eviction.\n"
        "- 05:00 — pre-brief sweep (all 21 passes) → clean.\n"
        "\n## 📝 Cowork-Iris intra-day log\n"
        "- 09:00 — something about nightly-eng in Cowork's section too.\n"
    )
    DAILY_RECORDED = DAILY_SILENT.replace(
        "- 05:00 — pre-brief sweep",
        "- 03:05 — nightly-eng: shipped materialize_delta reconcile (commit 3dc062f)\n- 05:00 — pre-brief sweep")

    def test_author_prefixes_do_not_include_body_prose(self):
        prefixes = ns.cc_log_author_prefixes(self.DAILY_SILENT)
        # The nightly-eng mention lives in the automation-scan BODY, not an author
        # prefix → the token must be absent from the prefix list.
        self.assertNotIn("nightlyeng", " ".join(prefixes))
        self.assertIn("bookupdate", prefixes)
        self.assertIn("automationscan", prefixes)

    def test_records_when_nightly_eng_authors_a_line(self):
        prefixes = ns.cc_log_author_prefixes(self.DAILY_RECORDED)
        self.assertTrue(any("nightlyeng" in p for p in prefixes))

    def test_cowork_section_prose_excluded(self):
        # The "## 📝 Cowork-Iris" line mentions nightly-eng but is out of section.
        self.assertNotIn("nightlyeng",
                         " ".join(ns.cc_log_author_prefixes(self.DAILY_SILENT)))


# --- bridge_headers_for_date ----------------------------------------------

class BridgeHeaders(unittest.TestCase):
    BRIDGE = (
        "### 2026-09-14 (early AM) — Nightly book-update routine\n"
        "Body mentions nightly-eng was silent again.\n"
        "## Session — nightly-eng (2026-09-13 03:05 ET)\n"   # stale: wrong date
        "shipped something on the 13th.\n"
    )

    def test_nightly_book_update_header_is_not_a_nightly_eng_record(self):
        # "Nightly book-update" normalizes to ...nightlybookupdate..., which does
        # NOT contain "nightlyeng" → correctly not a record.
        headers = ns.bridge_headers_for_date(self.BRIDGE, "2026-09-14")
        self.assertFalse(any(ns.RECORD_TOKEN in h for h in headers))

    def test_stale_dated_nightly_eng_header_excluded(self):
        # The real nightly-eng header carries 2026-09-13, so it must not count for
        # the 2026-09-14 slot.
        headers = ns.bridge_headers_for_date(self.BRIDGE, "2026-09-14")
        self.assertNotIn("sessionnightlyeng20260913035et", headers)

    def test_same_date_nightly_eng_header_counts(self):
        b = "## Session — nightly-eng (2026-09-14 03:05 ET)\n"
        headers = ns.bridge_headers_for_date(b, "2026-09-14")
        self.assertTrue(any(ns.RECORD_TOKEN in h for h in headers))


# --- record_exists --------------------------------------------------------

class RecordExists(unittest.TestCase):
    def test_git_commit_short_circuits(self):
        self.assertTrue(ns.record_exists("", "", "2026-09-14", committed=True))

    def test_silent_night_has_no_record(self):
        self.assertFalse(ns.record_exists(
            AuthorPrefixes.DAILY_SILENT, BridgeHeaders.BRIDGE, "2026-09-14",
            committed=False))

    def test_daily_author_line_is_a_record(self):
        self.assertTrue(ns.record_exists(
            AuthorPrefixes.DAILY_RECORDED, "", "2026-09-14", committed=False))

    def test_bridge_header_is_a_record(self):
        self.assertTrue(ns.record_exists(
            "", "## Session — nightly-eng (2026-09-14 ...)\n", "2026-09-14",
            committed=False))


# --- committed_since: the bounded window ----------------------------------

class CommittedSince(unittest.TestCase):
    def _args_for(self, stdout=""):
        seen = {}

        def fake_run(cmd, **kw):
            seen["cmd"] = cmd
            return mock.Mock(returncode=0, stdout=stdout)

        with mock.patch.object(ns.subprocess, "run", fake_run):
            hit = ns.committed_since(SLOT)
        return hit, seen["cmd"]

    def test_passes_both_since_and_until_bounds(self):
        # An unbounded --since would let a LATER night's commit vouch for a silent
        # one (the bug validation caught). Both bounds must be present, and --until
        # must be slot + RECORD_WINDOW.
        _, cmd = self._args_for(stdout="")
        joined = " ".join(cmd)
        self.assertIn(f"--since={SLOT:%Y-%m-%dT%H:%M:%S}", cmd)
        until = SLOT + ns.RECORD_WINDOW
        self.assertIn(f"--until={until:%Y-%m-%dT%H:%M:%S}", cmd)

    def test_nonempty_output_is_a_commit(self):
        hit, _ = self._args_for(stdout="abc123\n")
        self.assertTrue(hit)

    def test_empty_output_is_no_commit(self):
        hit, _ = self._args_for(stdout="")
        self.assertFalse(hit)

    def test_git_failure_is_false_not_crash(self):
        with mock.patch.object(ns.subprocess, "run",
                               side_effect=OSError("git missing")):
            self.assertFalse(ns.committed_since(SLOT))


# --- evaluate -------------------------------------------------------------

class Evaluate(unittest.TestCase):
    def _sess(self, n, prod):
        return ns.Session("abcd1234", SLOT, SLOT + timedelta(minutes=11), ns.REPO,
                          n, prod, "Check iris_studio git state")

    def test_no_session_is_ok(self):
        v, _ = ns.evaluate(None, recorded=False)
        self.assertEqual(v, "OK")

    def test_recorded_is_ok(self):
        v, _ = ns.evaluate(self._sess(46, True), recorded=True)
        self.assertEqual(v, "OK")

    def test_abort_when_few_calls_and_no_write(self):
        v, msg = ns.evaluate(self._sess(6, False), recorded=False)
        self.assertEqual(v, "ABORT")
        self.assertIn("abcd1234", msg)
        self.assertIn("nightly-eng", msg)
        self.assertIn("died early", msg)  # detail phrase must match the ABORT verdict

    def test_silent_when_worked_but_no_record(self):
        # 46 calls with a write but still no durable record → SILENT, not ABORT.
        v, msg = ns.evaluate(self._sess(46, True), recorded=False)
        self.assertEqual(v, "SILENT")
        self.assertIn("shipped nothing", msg)  # detail phrase must match SILENT

    def test_many_calls_no_write_is_still_silent(self):
        # Over the abort threshold even without a write → ran, didn't die early.
        v, _ = ns.evaluate(self._sess(20, False), recorded=False)
        self.assertEqual(v, "SILENT")


# --- nightly_slot: read the schedule from the plist, never hand-typed ------

class NightlySlot(unittest.TestCase):
    def _with_plist(self, plist):
        import prebrief_fire_check as pf
        seen = {}

        def fake_last(now, hour, minute):
            seen["hour"], seen["minute"] = hour, minute
            return datetime(2026, 9, 16, hour, minute)
        ctx = [mock.patch.object(pf, "_load_plist", return_value=plist),
               mock.patch.object(pf, "_last_fire_daily", fake_last)]
        for c in ctx:
            c.start()
            self.addCleanup(c.stop)
        return seen

    def test_reads_hour_and_minute_from_plist(self):
        seen = self._with_plist({"StartCalendarInterval": {"Hour": 3, "Minute": 0}})
        self.assertIsNotNone(ns.nightly_slot(datetime(2026, 9, 16, 5, 0)))
        self.assertEqual((seen["hour"], seen["minute"]), (3, 0))

    def test_minute_defaults_to_zero_when_absent(self):
        seen = self._with_plist({"StartCalendarInterval": {"Hour": 3}})
        ns.nightly_slot(datetime(2026, 9, 16, 5, 0))
        self.assertEqual(seen["minute"], 0)  # not 1

    def test_list_sci_uses_first_entry(self):
        seen = self._with_plist({"StartCalendarInterval": [
            {"Hour": 3, "Minute": 0}, {"Hour": 9, "Minute": 30}]})
        ns.nightly_slot(datetime(2026, 9, 16, 5, 0))
        self.assertEqual(seen["hour"], 3)  # [0], not [1]

    def test_none_when_no_hour(self):
        self._with_plist({"StartCalendarInterval": {"Minute": 30}})
        self.assertIsNone(ns.nightly_slot(datetime(2026, 9, 16, 5, 0)))

    def test_none_when_plist_unreadable(self):
        self._with_plist(None)
        self.assertIsNone(ns.nightly_slot(datetime(2026, 9, 16, 5, 0)))


# --- main() smoke: OK on a shipped night, flag on a silent one ------------
# The record predicate itself is covered in RecordExists; here we only prove the
# wiring — slot lookup → session binding → evaluate → the printed line. File
# reads are stubbed to "" and committed_since is patched, so the OK-vs-flag turn
# is driven by (bound session?, committed?).

class Main(unittest.TestCase):
    def _run(self, *, sessions, committed):
        captured = {}
        fake_dt = mock.Mock(wraps=datetime)
        fake_dt.now.return_value = SLOT + timedelta(hours=2)  # a 05:00 pre-brief
        with mock.patch.object(ns, "datetime", fake_dt), \
             mock.patch.object(ns, "nightly_slot", return_value=SLOT), \
             mock.patch.object(ns, "committed_since", return_value=committed), \
             mock.patch.object(ns, "parse_sessions", return_value=sessions), \
             mock.patch.object(ns.Path, "read_text", return_value=""), \
             mock.patch("builtins.print", lambda m: captured.__setitem__("out", m)):
            self.assertEqual(ns.main([]), 0)
        return captured.get("out", "")

    def test_shipped_night_prints_ok(self):
        sess = ns.Session("nite1234", SLOT + timedelta(minutes=1),
                          SLOT + timedelta(minutes=40), ns.REPO, 46, True, "x")
        self.assertTrue(self._run(sessions=[sess], committed=True).startswith("OK"))

    def test_silent_night_prints_flag(self):
        sess = ns.Session("dead1234", SLOT + timedelta(minutes=1),
                          SLOT + timedelta(minutes=11), ns.REPO, 6, False,
                          "Check iris_studio git state")
        out = self._run(sessions=[sess], committed=False)
        self.assertTrue(out.startswith("ABORT") or out.startswith("SILENT"))
        self.assertIn("nightly-eng", out)

    def test_no_session_prints_ok(self):
        # A book-update-only night (02:30, outside the 03:00 window) → OK.
        book = ns.Session("book0000", datetime(2026, 9, 14, 2, 30),
                          datetime(2026, 9, 14, 2, 46), str(ns.VAULT), 84, True, "x")
        self.assertTrue(self._run(sessions=[book], committed=False).startswith("OK"))

    def test_returns_zero_and_ok_when_no_slot(self):
        # A monitor never blocks the routine: no schedulable slot → exit 0, OK.
        out = {}
        with mock.patch.object(ns, "nightly_slot", return_value=None), \
             mock.patch("builtins.print", lambda m: out.__setitem__("o", m)):
            self.assertEqual(ns.main([]), 0)
        self.assertTrue(out["o"].startswith("OK"))

    def test_loads_today_and_yesterday_ledgers(self):
        # The slot can be yesterday's 03:00 on an early-morning run, so both the
        # current and the PRIOR day's ledgers must be read — not day-2.
        seen = []
        fake_dt = mock.Mock(wraps=datetime)
        fake_dt.now.return_value = datetime(2026, 9, 16, 5, 0)

        def cap(self, *a, **k):
            seen.append(str(self))
            return ""
        with mock.patch.object(ns, "datetime", fake_dt), \
             mock.patch.object(ns, "nightly_slot", return_value=SLOT), \
             mock.patch.object(ns, "committed_since", return_value=False), \
             mock.patch.object(ns, "parse_sessions", return_value=[]), \
             mock.patch.object(ns.Path, "read_text", cap), \
             mock.patch("builtins.print", lambda m: None):
            ns.main([])
        dates = {p.rsplit("/", 1)[-1][:-6] for p in seen if p.endswith(".jsonl")}
        self.assertIn("2026-09-16", dates)   # today
        self.assertIn("2026-09-15", dates)   # yesterday (days=1), NOT 09-14
        self.assertNotIn("2026-09-14", dates)


if __name__ == "__main__":
    unittest.main()
