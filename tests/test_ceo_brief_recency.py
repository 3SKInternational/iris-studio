#!/usr/bin/env python3
"""Tests for iris.py's CEO-brief recency read (_read_latest_ceo_brief +
_ceo_brief_age_label).

Stdlib unittest only (repo convention). Run:
    python3 tests/test_ceo_brief_recency.py
or under the suite:
    python3 -m unittest discover -s tests -v

WHY THIS EXISTS. On 2026-07-27 the org-briefs job moved from daily to weekly, so
the daemon stopped reading "today's" CEO brief and started reading the NEWEST one
plus its date. `precheck.sh --mutate iris.py` then reported SEVEN survivors on the
new lines — every branch and both literals could be wrong in production with the
whole suite green, because nothing tested the function at all. These tests kill
those seven. The stakes are not cosmetic: the age-label branch is what stops a
week-old brief's numbers being announced as current.

Imports the REAL iris.py rather than a copy — a test against a re-implementation
proves nothing. iris.py pulls in apscheduler/telegram/dotenv/claude_agent_sdk,
which are absent from the system interpreter precheck runs under, so those four
are stubbed in sys.modules first. Stubbing the daemon's I/O deps is safe here
because nothing under test touches them; if that ever stops being true the import
will fail loudly rather than silently test the wrong thing.
"""
import importlib
import importlib.util
import os
import sys
import tempfile
import types
import unittest
from datetime import date, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _stub(name: str, force: bool = False, **attrs) -> None:
    """Register a placeholder module (and its parents) in sys.modules.

    By default this DEFERS to a real module when one is importable: under
    `unittest discover` every suite shares one process, so an unconditional stub
    would shadow the real apscheduler/telegram for any later test that wants
    them. They are absent from the system interpreter precheck uses (so the stub
    is needed there) but present in .venv (so it must not be).

    `force=True` stubs regardless. Required for any module whose import has a
    SIDE EFFECT, where "defer to the real one" means "perform the side effect".
    `dotenv` is exactly that: deferring to it let iris.py's module-scope
    load_dotenv() run for real under .venv, pulling the live .env — including
    ELEVENLABS_API_KEY and OBSIDIAN_API_KEY — into the test process. Caught in
    review 2026-07-28. A test must not load production credentials to run.
    """
    if not force:
        try:
            importlib.import_module(name)
            return
        except Exception:
            pass
    parts = name.split(".")
    for i in range(1, len(parts) + 1):
        sub = ".".join(parts[:i])
        if sub not in sys.modules:
            sys.modules[sub] = types.ModuleType(sub)
    for k, v in attrs.items():
        setattr(sys.modules[name], k, v)


def _load_iris():
    _stub("apscheduler.schedulers.asyncio", AsyncIOScheduler=object)
    _stub("apscheduler.triggers.cron", CronTrigger=object)
    _stub("apscheduler.triggers.interval", IntervalTrigger=object)
    # force=True: never defer to the real dotenv. Importing it is not neutral —
    # iris.py calls load_dotenv() at module scope, so the real module would read
    # the production .env into this process. Keep it a no-op.
    _stub("dotenv", force=True, load_dotenv=lambda *a, **k: None)
    _stub("telegram", Update=object)
    _stub("telegram.error", NetworkError=Exception, RetryAfter=Exception,
          TimedOut=Exception)
    _stub("telegram.ext", Application=object, ContextTypes=object,
          MessageHandler=object, filters=object)
    # claude_agent_sdk: the message/option names are only used as types here, but
    # `tool` is applied as @tool(...) at module scope, so it must be a decorator
    # FACTORY (call -> decorator -> unchanged function), not a bare sentinel.
    sdk = types.ModuleType("claude_agent_sdk")
    for attr in ("ClaudeSDKClient", "ClaudeAgentOptions", "AssistantMessage",
                 "TextBlock", "ResultMessage", "ToolUseBlock", "ToolResultBlock",
                 "UserMessage", "SystemMessage"):
        setattr(sdk, attr, type(attr, (), {}))
    sdk.tool = lambda *a, **k: (lambda fn: fn)
    sdk.query = lambda *a, **k: None
    sdk.create_sdk_mcp_server = lambda *a, **k: None
    sys.modules.setdefault("claude_agent_sdk", sdk)
    # iris.py reads this at module scope and hard-fails without it. A placeholder
    # keeps the test runnable on a machine with no daemon secrets. Never read a
    # real token here; setdefault leaves an already-exported env var alone. With
    # dotenv force-stubbed above, no .env is loaded, so this placeholder is what
    # iris.py actually sees.
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token-not-a-real-credential")

    spec = importlib.util.spec_from_file_location("iris_under_test", REPO / "iris.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


iris = _load_iris()


class TestLatestCeoBrief(unittest.TestCase):
    """_read_latest_ceo_brief: which file wins, and what date comes back."""

    # Injected rather than wall-clock: with a staleness ceiling in play, fixtures
    # dated against the real "today" would silently start returning ("", "") once
    # the calendar drifts past it — a suite that passes now and fails in a month.
    TODAY = "2026-07-28"

    def _with_briefs(self, names):
        """Point iris at a temp Org_Briefs dir containing `names`; return result."""
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        d = Path(td.name)
        for n in names:
            (d / n).write_text(f"body of {n}\n")
        orig = iris.ORG_BRIEFS_DIR
        iris.ORG_BRIEFS_DIR = d
        self.addCleanup(lambda: setattr(iris, "ORG_BRIEFS_DIR", orig))
        return iris._read_latest_ceo_brief(today_iso=self.TODAY)

    def test_missing_dir_returns_empty_pair(self):
        """A never-created briefs dir must degrade to ("", ""), not crash.

        There is deliberately no is_dir() branch in the function: glob returns []
        rather than raising for a missing path, so the empty-list guard covers
        this. This pins the CONTRACT (degrade, don't crash) independently of
        which guard implements it, so re-adding an is_dir() check or removing the
        empty guard both stay covered.
        """
        orig = iris.ORG_BRIEFS_DIR
        iris.ORG_BRIEFS_DIR = Path("/nonexistent/definitely/not/here")
        self.addCleanup(lambda: setattr(iris, "ORG_BRIEFS_DIR", orig))
        self.assertEqual(iris._read_latest_ceo_brief(today_iso=self.TODAY), ("", ""))

    def test_existing_dir_is_not_treated_as_missing(self):
        """A brief that exists must actually be read — the section must not blank
        while briefs sit on disk."""
        body, date = self._with_briefs(["2026-07-27_ceo.md"])
        self.assertTrue(body.strip(), "a present brief must be read, not skipped")
        self.assertEqual(date, "2026-07-27")

    def test_empty_dir_returns_empty_pair(self):
        """Kills `not briefs` -> False: with no briefs, briefs[-1] is IndexError."""
        self.assertEqual(self._with_briefs([]), ("", ""))

    def test_non_empty_dir_is_not_treated_as_empty(self):
        """Kills `not briefs` -> True (would blank the section unconditionally)."""
        body, date = self._with_briefs(["2026-07-20_ceo.md"])
        self.assertIn("2026-07-20_ceo.md", body)
        self.assertEqual(date, "2026-07-20")

    def test_picks_newest_not_second_newest(self):
        """Kills the briefs[-1] -> briefs[-2] index mutant.

        Written out of ISO order so a passing result cannot come from insertion
        order — only from the sort.
        """
        body, date = self._with_briefs(
            ["2026-07-13_ceo.md", "2026-07-27_ceo.md", "2026-07-20_ceo.md"])
        self.assertEqual(date, "2026-07-27")
        self.assertIn("2026-07-27_ceo.md", body)

    def test_date_slice_is_exactly_the_iso_date(self):
        """Kills the name[:10] -> name[:11] mutant, which would return
        '2026-07-27_' and never compare equal to an ISO date — silently making
        every brief render as stale."""
        _, date = self._with_briefs(["2026-07-27_ceo.md"])
        self.assertEqual(date, "2026-07-27")
        self.assertEqual(len(date), 10)

    def test_only_ceo_briefs_are_considered(self):
        """Department briefs share the directory; a newer _marketing.md must not
        be mistaken for the CEO brief."""
        _, date = self._with_briefs(
            ["2026-07-20_ceo.md", "2026-07-27_marketing.md", "2026-07-27_finance.md"])
        self.assertEqual(date, "2026-07-20")


class TestStalenessCeiling(unittest.TestCase):
    """Past CEO_BRIEF_MAX_AGE_DAYS the brief must disappear entirely.

    Without a ceiling, a permanently-dead org-briefs job keeps rendering old
    numbers forever and nothing announces that the producer stopped — a silent
    failure replacing the loud one the old today-only read gave for free.
    """

    def _with_brief(self, name, today):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        d = Path(td.name)
        (d / name).write_text("state of the company\n")
        orig = iris.ORG_BRIEFS_DIR
        iris.ORG_BRIEFS_DIR = d
        self.addCleanup(lambda: setattr(iris, "ORG_BRIEFS_DIR", orig))
        return iris._read_latest_ceo_brief(today_iso=today)

    def test_fresh_brief_is_served(self):
        body, dt = self._with_brief("2026-07-27_ceo.md", "2026-07-28")
        self.assertTrue(body.strip())
        self.assertEqual(dt, "2026-07-27")

    # Literal dates, NOT dates derived from CEO_BRIEF_MAX_AGE_DAYS. Deriving them
    # makes the fixture move with the constant, so the test cannot see the
    # constant change — mutation proved exactly that (15 -> 16 survived). These
    # pin the VALUE, in both directions.
    def test_the_ceiling_is_fifteen_days(self):
        self.assertEqual(iris.CEO_BRIEF_MAX_AGE_DAYS, 15,
                         "the ceiling is a deliberate two-missed-weekly-fires value; "
                         "changing it should be a conscious edit, not a drift")

    def test_brief_exactly_at_the_ceiling_is_still_served(self):
        """2026-07-01 + 15d = 2026-07-16 — the last day it may serve."""
        body, _ = self._with_brief("2026-07-01_ceo.md", "2026-07-16")
        self.assertTrue(body.strip(), "a brief exactly 15d old must still serve")

    def test_brief_one_day_past_the_ceiling_is_withheld(self):
        """2026-07-01 + 16d = 2026-07-17 — the first day it must vanish."""
        self.assertEqual(self._with_brief("2026-07-01_ceo.md", "2026-07-17"), ("", ""))

    def test_unparseable_date_reads_as_stale(self):
        self.assertTrue(iris._ceo_brief_is_stale("not-a-date", "2026-07-28"))

    def test_non_iso_filename_cannot_win_the_sort(self):
        """A stray 'draft_ceo.md' sorts after any ISO name; if the glob let it
        through it would win and produce a junk 'draft_ceo.' date label."""
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        d = Path(td.name)
        (d / "2026-07-27_ceo.md").write_text("real\n")
        (d / "draft_ceo.md").write_text("junk\n")
        orig = iris.ORG_BRIEFS_DIR
        iris.ORG_BRIEFS_DIR = d
        self.addCleanup(lambda: setattr(iris, "ORG_BRIEFS_DIR", orig))
        body, dt = iris._read_latest_ceo_brief(today_iso="2026-07-28")
        self.assertEqual(dt, "2026-07-27")
        self.assertIn("real", body)


class TestMorningBriefPromptStaysDateAware(unittest.TestCase):
    """The emission template must keep its date-awareness instruction.

    Round 1 of the 2026-07-28 review caught exactly this gap: the system-prompt
    HEADER was updated to carry the brief's date, but MORNING_BRIEFING_PROMPT —
    the instruction that actually renders the four strategic blocks — was not, so
    six days a week the brief would have led with undated stale numbers. Mutation
    is blind to string content, so only an assertion can guard the recurrence.
    """

    def test_state_of_company_block_demands_the_as_of_prefix(self):
        p = iris.MORNING_BRIEFING_PROMPT
        self.assertIn("As of", p,
                      "the STATE OF THE COMPANY block must require an 'As of <date>:' "
                      "prefix when the brief is not from today")
        self.assertIn("NOT today", p)

    def test_fallback_does_not_claim_the_job_runs_daily(self):
        """The old '(the org-briefs job did not run today)' wording became false
        under a weekly cadence — an absent section now means no servable brief."""
        p = iris.MORNING_BRIEFING_PROMPT
        self.assertNotIn("did not run today", p)
        self.assertIn("no current CEO brief", p)


class TestAgeLabel(unittest.TestCase):
    """_ceo_brief_age_label: the honesty branch."""

    def test_same_date_reads_as_today(self):
        self.assertEqual(
            iris._ceo_brief_age_label("2026-07-27", "2026-07-27"), "today's")

    def test_older_brief_is_labelled_with_its_date(self):
        """Kills the negate-compare mutant. If inverted, a six-day-old brief is
        announced as "today's" — the exact fabrication this pipeline forbids."""
        self.assertEqual(
            iris._ceo_brief_age_label("2026-07-20", "2026-07-27"), "dated 2026-07-20")

    def test_label_distinguishes_the_two_cases(self):
        """Both branches must not collapse to the same string."""
        self.assertNotEqual(
            iris._ceo_brief_age_label("2026-07-27", "2026-07-27"),
            iris._ceo_brief_age_label("2026-07-20", "2026-07-27"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
