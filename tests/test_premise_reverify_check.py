#!/usr/bin/env python3
"""Tests for scripts/premise_reverify_check.py (A-81). Stdlib unittest only."""
import datetime
import io
import os
import runpy
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

# Point state at a throwaway path BEFORE import: a mutant that runs main() at
# import time must never touch the live ledger.
_TMP = tempfile.mkdtemp()
unittest.addModuleCleanup(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["PREMISE_REVERIFY_STATE"] = str(Path(_TMP) / "import_state.tsv")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
try:
    import premise_reverify_check as prc  # noqa: E402
except SystemExit as e:  # the __main__ guard ran on import
    raise AssertionError(f"importing premise_reverify_check ran main() (exit {e.code})")
import stale_on_you  # noqa: E402

TODAY = datetime.date(2026, 10, 11)


def row(dq_id, deadline, days):
    return {"id": dq_id, "deadline": deadline, "days": days, "text": "", "class": "", "source": "DQ"}


# The live 2026-10-11 trigger rows, verbatim deadline + days cells.
LIVE = [
    row("DQ-8", "trigger: **first ~100–250 subscribers OR weekly-publishing cadence** (was: re-surface 2026-09-01 / first-video-filmed)", "101 (added 6/11; trigger reset 6/20)"),
    row("DQ-16", "trigger: EIN + Impact.com tracked URLs live", "94 (added 6/18)"),
    row("DQ-25", "trigger: Steve's call (no hard deadline)", "85 (added 6/27)"),
    row("DQ-26", "trigger: revisit at ≥15 published videos (no calendar deadline)", "84 (added 6/28)"),
    row("DQ-29", "2026-07-04 (soft)", "81 (added 7/01)"),
]


class TestDueRows(unittest.TestCase):
    def test_live_rows_flag_only_dq16_with_real_age(self):
        # 6/18 → 10/11 is 115 days; the frozen cell says 94.
        self.assertEqual(prc.due_rows(LIVE, TODAY), [("DQ-16", 115)])

    def test_age_boundary_from_cell(self):
        self.assertEqual(prc.due_rows([row("DQ-1", "trigger: vendor ships API", "30")], TODAY), [])
        self.assertEqual(prc.due_rows([row("DQ-1", "trigger: vendor ships API", "31")], TODAY), [("DQ-1", 31)])

    def test_age_from_added_date_when_cell_frozen(self):
        self.assertEqual(prc.due_rows([row("DQ-1", "trigger: vendor ships API", "0 (added 9/10)")], TODAY), [("DQ-1", 31)])
        self.assertEqual(prc.due_rows([row("DQ-1", "trigger: vendor ships API", "0 (added 9/11)")], TODAY), [])

    def test_unparseable_cell_uses_added_date(self):
        self.assertEqual(prc.due_rows([row("DQ-1", "trigger: bank approves", "? (added 6/18)")], TODAY), [("DQ-1", 115)])

    def test_no_trigger_field_skipped(self):
        self.assertEqual(prc.due_rows([row("DQ-1", "vendor ships API", "99")], TODAY), [])
        self.assertEqual(prc.due_rows([row("DQ-1", "2026-09-05 (trigger-gated on vendor)", "99")], TODAY), [])

    def test_trigger_label_forms(self):
        for cell in ["**trigger:** bank approves account", "**Trigger**: bank approves", "Trigger - bank approves",
                     "soft, or trigger: bank approves", "Trigger — bank approves"]:
            with self.subTest(cell=cell):
                self.assertEqual(prc.due_rows([row("DQ-1", cell, "40")], TODAY), [("DQ-1", 40)])

    def test_internal_triggers_skipped(self):
        for trig in ["V1 Session C done", "2026-12-01", "soft — 2026-12-01", "first 100 subs",
                     "V14 final cut lands", "weekly cadence", "no hard deadline",
                     "15 published videos"]:
            with self.subTest(trig=trig):
                self.assertEqual(prc.due_rows([row("DQ-1", f"trigger: {trig}", "90")], TODAY), [])

    def test_asides_and_v_numbers_do_not_silence_external(self):
        for trig in ["EIN + Impact.com tracked URLs live (was: 2026-07-15)",
                     "EIN + Impact.com tracked URLs live (re-verified 2026-10-12)",
                     "Mercury approves EIN before V2 publishes",
                     "Amazon Associates approval (needs 3 sales in 180d of V1)"]:
            with self.subTest(trig=trig):
                self.assertEqual(prc.due_rows([row("DQ-1", f"trigger: {trig}", "90")], TODAY), [("DQ-1", 90)])


class TestAgeDays(unittest.TestCase):
    def test_december_row_read_in_january(self):
        self.assertEqual(prc.age_days("0 (added 12/20)", datetime.date(2027, 1, 10)), 21)

    def test_near_future_typo_is_not_a_year_old(self):
        self.assertEqual(prc.age_days("0 (added 10/12)", TODAY), 0)
        self.assertEqual(prc.age_days("0 (added 11/10)", TODAY), 0)
        self.assertEqual(prc.age_days("0 (added 11/11)", TODAY), 334)

    def test_leap_day_rewound_into_non_leap_year_falls_back_to_cell(self):
        self.assertEqual(prc.age_days("5 (added 2/29)", datetime.date(2028, 1, 10)), 5)

    def test_bad_added_date_falls_back_to_cell(self):
        self.assertEqual(prc.age_days("12 (added 2/30)", TODAY), 12)

    def test_cell_larger_than_added_wins(self):
        self.assertEqual(prc.age_days("200 (added 10/01)", TODAY), 200)


class TestThrottle(unittest.TestCase):
    def test_first_time_emits_and_records(self):
        emit, seen = prc.throttle([("DQ-16", 94)], {}, TODAY)
        self.assertEqual(emit, [("DQ-16", 94)])
        self.assertEqual(seen, {"DQ-16": "2026-10-11"})

    def test_within_week_suppressed(self):
        emit, seen = prc.throttle([("DQ-16", 94)], {"DQ-16": "2026-10-05"}, TODAY)
        self.assertEqual(emit, [])
        self.assertEqual(seen, {"DQ-16": "2026-10-05"})

    def test_after_week_emits_again(self):
        emit, _ = prc.throttle([("DQ-16", 94)], {"DQ-16": "2026-10-04"}, TODAY)
        self.assertEqual(emit, [("DQ-16", 94)])

    def test_corrupt_date_emits(self):
        emit, _ = prc.throttle([("DQ-16", 94)], {"DQ-16": "garbage"}, TODAY)
        self.assertEqual(emit, [("DQ-16", 94)])


class TestMain(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state = Path(self.dir.name) / "sub" / "seen.tsv"
        self.dq = Path(self.dir.name) / "Decision_Queue.md"
        self.dq.write_text("x", encoding="utf-8")

    def tearDown(self):
        self.dir.cleanup()

    def run_main(self, rows, argv=(), env=None):
        out, err = io.StringIO(), io.StringIO()
        env = {"SK_MUTATION_RUN": ""} if env is None else env
        patch_rows = (mock.patch.object(stale_on_you, "parse_decision_queue", side_effect=rows)
                      if isinstance(rows, Exception) else
                      mock.patch.object(stale_on_you, "parse_decision_queue", return_value=rows))
        with patch_rows, mock.patch.object(stale_on_you, "DECISION_QUEUE", self.dq), \
             mock.patch.object(prc, "STATE_FILE", self.state), mock.patch.dict(os.environ, env), \
             redirect_stdout(out), redirect_stderr(err):
            code = prc.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_flag_then_throttled_clean(self):
        code, out, _ = self.run_main(LIVE)
        self.assertEqual(code, 1)
        self.assertRegex(out, r"^DQ-16 premise re-verify due \(\d+\+ days\) — re-read the trigger's source, not the row\.$")
        self.assertNotIn("DQ-8", out)
        self.assertEqual(prc.load_seen(self.state), {"DQ-16": datetime.date.today().isoformat()})
        code, out, _ = self.run_main(LIVE)
        self.assertEqual((code, out.strip()), (0, "premise_reverify_check: CLEAN"))

    def test_dry_run_prints_but_writes_no_state(self):
        code, out, _ = self.run_main(LIVE, argv=["--dry-run"])
        self.assertEqual(code, 1)
        self.assertIn("DQ-16", out)
        self.assertFalse(self.state.exists())

    def test_mutation_env_writes_no_state(self):
        code, _, _ = self.run_main(LIVE, env={"SK_MUTATION_RUN": "1"})
        self.assertEqual(code, 1)
        self.assertFalse(self.state.exists())

    def test_clean_writes_no_state(self):
        code, out, _ = self.run_main(LIVE[:1])
        self.assertEqual((code, out.strip()), (0, "premise_reverify_check: CLEAN"))
        self.assertFalse(self.state.exists())

    def test_missing_queue_skips_75(self):
        self.dq.unlink()
        code, out, err = self.run_main(LIVE)
        self.assertEqual((code, out), (75, ""))
        self.assertIn("premise_reverify_check: SKIP", err)

    def test_read_error_skips_75(self):
        code, out, err = self.run_main(OSError(11, "Resource deadlock avoided"))
        self.assertEqual((code, out), (75, ""))
        self.assertIn("SKIP — Decision_Queue unreadable: OSError", err)

    def test_load_seen_skips_bad_lines(self):
        self.state.parent.mkdir(parents=True)
        self.state.write_text("DQ-16\t2026-10-01\njunk\n", encoding="utf-8")
        self.assertEqual(prc.load_seen(self.state), {"DQ-16": "2026-10-01"})

    def test_entrypoint_passes_argv(self):
        # Run the file as __main__ (guard + argv slice) with --dry-run: must exit 1
        # with the DQ-16 line and write no state. The env var, not the module
        # attribute, steers the fresh module's state path.
        state = Path(self.dir.name) / "entry.tsv"
        script = str(Path(prc.__file__))
        out = io.StringIO()
        with mock.patch.object(stale_on_you, "parse_decision_queue", return_value=LIVE), \
             mock.patch.object(stale_on_you, "DECISION_QUEUE", self.dq), \
             mock.patch.dict(os.environ, {"SK_MUTATION_RUN": "", "PREMISE_REVERIFY_STATE": str(state)}), \
             mock.patch.object(sys, "argv", [script, "--dry-run"]), redirect_stdout(out), \
             self.assertRaises(SystemExit) as cm:
            runpy.run_path(script, run_name="__main__")
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("DQ-16 premise re-verify due", out.getvalue())
        self.assertFalse(state.exists())

    def test_missing_ledger_is_empty_and_quiet(self):
        self.assertEqual(prc.load_seen(self.state), {})
        _, _, err = self.run_main(LIVE, argv=["--dry-run"])
        self.assertEqual(err, "")

    def test_corrupt_ledger_still_flags(self):
        self.state.parent.mkdir(parents=True)
        self.state.write_bytes(b"\xff\xfe")
        code, out, err = self.run_main(LIVE, argv=["--dry-run"])
        self.assertEqual(code, 1)
        self.assertIn("DQ-16 premise re-verify due", out)
        self.assertIn("state ledger unreadable", err)

    def test_unwritable_ledger_still_flags(self):
        self.state = Path("/dev/null/x/s.tsv")
        code, out, err = self.run_main(LIVE)
        self.assertEqual(code, 1)
        self.assertIn("DQ-16 premise re-verify due", out)
        self.assertIn("state ledger write failed", err)

    def test_state_path_read_from_env_at_import(self):
        self.assertEqual(prc.STATE_FILE, Path(os.environ["PREMISE_REVERIFY_STATE"]))


if __name__ == "__main__":
    unittest.main()
