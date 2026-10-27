"""prompt_time_check.py (A-101): a routine prompt's line-1 time vs its plist."""
import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = Path(os.environ.get("PTC_UNDER_TEST") or REPO / "scripts" / "prompt_time_check.py")


class TestPromptTimeCheck(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.agents = Path(self.tmp.name) / "agents"
        self.routines = Path(self.tmp.name) / "routines"
        self.agents.mkdir()
        self.routines.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def job(self, name, line1, sched, interval=False):
        (self.routines / f"{name}.prompt").write_text(line1 + "\nbody 09:99 later\n")
        d = {"Label": f"com.iris.claude-code-{name}",
             "ProgramArguments": ["/bin/bash", "-lc",
                                  f"/x/scripts/run_claude_job.sh {name} /x/routines/{name}.prompt"]}
        if interval:
            d["StartInterval"] = 3600
        else:
            d["StartCalendarInterval"] = sched
        with open(self.agents / f"com.iris.claude-code-{name}.plist", "wb") as f:
            plistlib.dump(d, f)

    def run_check(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--agents", str(self.agents),
                            "--routines", str(self.routines)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_12h_match_is_clean(self):
        self.job("a", "Weekly at 4:30 AM ET.", {"Hour": 4, "Minute": 30, "Weekday": 0})
        self.job("b", "Runs (Mon 2:15 PM ET).", {"Hour": 14, "Minute": 15})
        self.job("c", "Daily (01:00 ET).", {"Hour": 1, "Minute": 0})
        self.job("d", "Monthly at 12:05 AM.", {"Hour": 0, "Minute": 5})
        out = self.run_check()
        self.assertNotIn("MISMATCH", out)
        self.assertIn("4 checked, 0 mismatch", out)

    def test_stale_time_is_flagged(self):
        self.job("shorts", "once a week (Friday, 6:45 AM ET).", [{"Hour": 2, "Minute": 0, "Weekday": 5}])
        out = self.run_check()
        self.assertIn("MISMATCH shorts.prompt: line 1 says 06:45;", out)
        self.assertIn("fires Fri 02:00", out)

    def test_24h_afternoon_time_without_am_pm(self):
        self.job("t", "Daily at 14:15 ET.", {"Hour": 14, "Minute": 15})
        self.assertIn("1 checked, 0 mismatch", self.run_check())

    def test_weekday_7_is_sunday_and_missing_keys_print_as_stars(self):
        self.job("w", "Weekly at 9:00 AM.", {"Hour": 4, "Minute": 30, "Weekday": 7})
        self.job("h", "Hourly at 9:00 AM.", {"Minute": 15})
        out = self.run_check()
        self.assertIn("fires Sun 04:30", out)
        self.assertIn("fires **:15", out)

    def test_missing_hour_or_minute_is_a_wildcard(self):
        self.job("m", "Every minute of 03:xx, e.g. 03:41.", {"Hour": 3})
        self.assertIn("1 checked, 0 mismatch", self.run_check())

    def test_pm_is_not_read_as_am(self):
        self.job("p", "Runs at 2:00 PM.", {"Hour": 2, "Minute": 0})
        self.assertIn("MISMATCH p.prompt", self.run_check())

    def test_any_entry_of_a_list_matches(self):
        self.job("scan", "Twice weekly at 03:30 AM ET.",
                 [{"Hour": 3, "Minute": 30, "Weekday": 1}, {"Hour": 3, "Minute": 30, "Weekday": 4}])
        self.assertIn("1 checked, 0 mismatch", self.run_check())

    def test_minute_only_entry_is_every_hour(self):
        self.job("hourly", "Every hour at :15 past, e.g. 07:15.", {"Minute": 15})
        self.assertIn("1 checked, 0 mismatch", self.run_check())

    def test_only_line_one_is_read(self):
        self.job("n", "No time here.", {"Hour": 3, "Minute": 0})
        out = self.run_check()
        self.assertIn("1 checked, 0 mismatch, 1 with no time", out)

    def test_interval_jobs_skipped_and_missing_prompts_listed(self):
        self.job("sweep", "Hourly at 9:00 AM.", None, interval=True)
        self.job("gone", "At 9:00 AM.", {"Hour": 3, "Minute": 0})
        (self.routines / "gone.prompt").unlink()
        out = self.run_check()
        self.assertIn("0 checked, 0 mismatch", out)
        self.assertIn("MISSING-PROMPT com.iris.claude-code-gone.plist -> gone.prompt", out)
        self.assertIn("1 missing prompt", out)

    def test_only_the_first_time_counts(self):
        # Moved to 06:00; line 1 still says 04:25 but also names the 06:00 brief.
        self.job("org", "Weekly at 04:25 AM ET, before Iris's 06:00 brief.", {"Hour": 6, "Minute": 0})
        self.assertIn("MISMATCH org.prompt: line 1 says 04:25;", self.run_check())

    def test_dotted_am_pm_and_word_boundary(self):
        self.job("d", "Runs at 2:15 p.m. daily.", {"Hour": 14, "Minute": 15})
        self.job("v", "Build v12:30 notes, then at 03:00 ET.", {"Hour": 3, "Minute": 0})
        self.job("a", "Runs 12:30 amid the midday lull.", {"Hour": 12, "Minute": 30})
        self.assertIn("3 checked, 0 mismatch", self.run_check())

    def test_unreadable_plist_is_reported(self):
        (self.agents / "com.iris.bad.plist").write_text("<plist><dict><key>x</key>")
        out = self.run_check()
        self.assertIn("UNREADABLE com.iris.bad.plist", out)
        self.assertIn("1 unreadable plist", out)


if __name__ == "__main__":
    unittest.main()
