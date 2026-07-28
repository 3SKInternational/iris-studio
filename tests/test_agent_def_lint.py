#!/usr/bin/env python3
"""Tests for scripts/agent_def_lint.py — the agent-def size/frontmatter/roster gate.

Stdlib unittest only (repo convention). Run:
    python3 tests/test_agent_def_lint.py
or under the suite:
    python3 -m unittest discover -s tests -v

WHY THIS EXISTS. The lint's own --selftest lives inside the script, and precheck.sh
only discovers tests/test_*.py — so without this file none of it ran at the gate.
The roster collector is the part that most needs pinning: its first version read
only the `agent`/`members` keys and missed `lead`/`staff`, which made it report 11
false roster errors on a chart that was actually correct. A lint that cries wolf
gets ignored, which is worse than no lint.
"""
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "agent_def_lint", REPO / "scripts" / "agent_def_lint.py")
adl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(adl)


class TestFrontmatter(unittest.TestCase):
    def test_value_may_contain_colons(self):
        """Agent descriptions are full of colons ('Distinct from x: ...'); the
        flat parser must split on the FIRST colon only."""
        fm = adl.parse_frontmatter(
            "---\nname: x\ndescription: Uses this: and that\nmodel: sonnet\n"
            "tools: Read\n---\n\nbody\n")
        self.assertEqual(fm["description"], "Uses this: and that")

    def test_missing_block_is_none(self):
        self.assertIsNone(adl.parse_frontmatter("no frontmatter here\n"))

    def test_blank_and_comment_lines_are_skipped(self):
        """Real agent frontmatter contains blank separators and `#` comments;
        neither may become a parsed key."""
        fm = adl.parse_frontmatter(
            "---\nname: x\n\n# a comment: with a colon\n   # indented comment\n"
            "model: sonnet\n---\n\nbody\n")
        self.assertEqual(set(fm), {"name", "model"},
                         f"blank/comment lines leaked into the keys: {fm}")

    def test_comment_line_is_not_parsed_as_a_key(self):
        """Pins the `or` in the skip guard: with `and`, a comment line is only
        skipped when it is ALSO blank — i.e. never — so '#a' becomes a key."""
        fm = adl.parse_frontmatter("---\nname: x\n#fake: value\n---\n\nbody\n")
        self.assertNotIn("#fake", fm)
        self.assertIsNone(fm.get("#fake"))


class TestFrontmatterErrors(unittest.TestCase):
    def _lint_one(self, stem, fm):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        d = Path(td.name)
        (d / f"{stem}.md").write_text(f"---\n{fm}\n---\n\nbody\n")
        return adl.lint(d, None, max_kb=20.0)[0]

    def test_absent_name_field_reports_the_missing_field(self):
        """Distinct from a name/stem MISMATCH. Without the `declared is None`
        branch this falls through to the mismatch comparison and reports
        'name: None != stem' — a confusing message for a different defect."""
        errors = self._lint_one("noname", "description: d\nmodel: sonnet\ntools: Read")
        self.assertTrue(any("has no `name:` field" in e for e in errors), errors)
        self.assertFalse(any("!=" in e for e in errors),
                         "a missing name must not be reported as a mismatch")

    def test_reported_size_matches_the_real_file_size(self):
        """The KB figure drives the compaction decision, so the divisor matters."""
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        d = Path(td.name)
        body = "x" * 30000
        p = d / "fat.md"
        p.write_text(f"---\nname: fat\ndescription: d\nmodel: sonnet\ntools: Read\n---\n\n{body}")
        _, warnings, _ = adl.lint(d, None, max_kb=20.0)
        expected = f"{p.stat().st_size / 1024:.1f}KB"
        self.assertTrue(any(expected in w for w in warnings),
                        f"expected {expected} in {warnings}")


class TestSizeBaselines(unittest.TestCase):
    """Over-cap defs with a reviewed baseline are silent; growth past it warns.

    The point of the baseline is that five permanently-warning defs train everyone
    to ignore the gate. But going silent must NOT mean going blind: growth past the
    audited size has to still fire, or the mechanism just deletes the signal.
    """

    def _def(self, d: Path, name: str, kb: float):
        """Write `name` sized to approximately `kb` kilobytes."""
        head = "---\nname: %s\ndescription: d\nmodel: sonnet\ntools: Read\n---\n\n" % name
        p = d / f"{name}.md"
        p.write_text(head + "x" * max(0, int(kb * 1024) - len(head)))
        return p

    def _lint(self, entries, baselines):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        d = Path(td.name)
        for name, kb in entries:
            self._def(d, name, kb)
        orig = adl.REVIEWED_BASELINES_KB
        adl.REVIEWED_BASELINES_KB = baselines
        self.addCleanup(lambda: setattr(adl, "REVIEWED_BASELINES_KB", orig))
        return adl.lint(d, None, max_kb=20.0)

    def test_unreviewed_oversize_still_warns(self):
        """The original signal must survive: a def nobody audited still shouts."""
        _, warnings, baselined = self._lint([("newbie", 40.0)], {})
        self.assertTrue(any("SIZE" in w and "newbie.md" in w for w in warnings), warnings)
        self.assertEqual(baselined, [])

    def test_baselined_def_at_its_baseline_is_silent(self):
        _, warnings, baselined = self._lint([("big", 40.0)], {"big.md": 40.0})
        self.assertEqual(warnings, [])
        self.assertEqual([n for n, _, _ in baselined], ["big.md"])

    def test_growth_past_the_grace_warns(self):
        """Literal sizes, NOT values derived from GROWTH_GRACE_KB — a fixture that
        moves with the constant cannot detect the constant changing."""
        _, warnings, baselined = self._lint([("big", 43.0)], {"big.md": 40.0})
        self.assertTrue(any("GROWTH" in w and "big.md" in w for w in warnings), warnings)
        self.assertEqual(baselined, [], "a grown def is a warning, not a silent carry")

    def test_growth_within_the_grace_stays_silent(self):
        _, warnings, _ = self._lint([("big", 41.0)], {"big.md": 40.0})
        self.assertEqual(warnings, [], "a 1KB drift must not nag")

    def test_the_grace_is_two_kb(self):
        """Pins the constant itself, in both directions, so a silent widening of
        the grace (which would suppress real growth) fails here."""
        self.assertEqual(adl.GROWTH_GRACE_KB, 2.0)

    def test_shrinking_below_the_baseline_is_silent_not_negative_growth(self):
        """A def that got SMALLER than its baseline is a win, never a warning."""
        _, warnings, baselined = self._lint([("big", 25.0)], {"big.md": 40.0})
        self.assertEqual(warnings, [])
        self.assertEqual([n for n, _, _ in baselined], ["big.md"])

    def test_baselined_def_under_the_cap_is_not_reported_at_all(self):
        """Below --max-kb nothing applies — not a warning, not a carry."""
        _, warnings, baselined = self._lint([("small", 5.0)], {"small.md": 40.0})
        self.assertEqual((warnings, baselined), ([], []))

    def test_real_baselines_cover_exactly_the_known_oversize_defs(self):
        """Guards against a baseline entry outliving the def it excused."""
        self.assertEqual(
            set(adl.REVIEWED_BASELINES_KB),
            {"scriptwriter.md", "scene-image-prompt-generator.md",
             "thumbnail-coordinator.md", "image-reviewer.md", "youtube-researcher.md"})


class TestRosterCollector(unittest.TestCase):
    """load_org_stems must read every key the chart names agents under."""

    def _chart(self, obj):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        p = Path(td.name) / "org.json"
        p.write_text(json.dumps(obj))
        return adl.load_org_stems(p)

    def test_collects_all_four_key_shapes(self):
        """Regression guard: an earlier collector read only agent/members and
        reported every department lead + CEO staffer as missing."""
        stems = self._chart({
            "ceo": {"agent": "chief-executive", "staff": ["chief-of-staff"]},
            "departments": [{"key": "m", "lead": "chief", "members": ["scriptwriter"]}],
        })
        self.assertEqual(
            stems, {"chief-executive", "chief-of-staff", "chief", "scriptwriter"})

    def test_list_key_holding_a_non_list_is_ignored(self):
        """Pins the `and isinstance(v, list)` guard. With `or`, a `members` that
        is a STRING gets .update()'d character-by-character, so the roster fills
        with single letters and every real agent reads as absent-from-chart."""
        stems = self._chart({"departments": [{"lead": "chief", "members": "oops"}]})
        self.assertEqual(stems, {"chief"},
                         f"a non-list members value leaked characters: {stems}")

    def test_ignores_non_agent_string_fields(self):
        """title/domain/key are prose, not agent stems."""
        stems = self._chart({
            "departments": [
                {"key": "quality", "title": "CQO", "domain": "Review Gates",
                 "lead": "chief", "members": []}]})
        self.assertEqual(stems, {"chief"})


class TestRosterTier(unittest.TestCase):
    """The ROSTER errors, exercised through lint() with a real chart.

    These exist because a review on 2026-07-28 proved both roster loops could be
    DELETED with all tests green: every other test calls lint() with org_chart
    None or --no-roster, and TestRosterCollector only covers load_org_stems in
    isolation. The tier that efficiency-steward routes to a bridge entry as a
    defect was therefore pinned nowhere a mutant could be seen.
    """

    def _fixture(self, defs, chart_obj):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        d = root / "agents"
        d.mkdir()
        for stem in defs:
            (d / f"{stem}.md").write_text(
                f"---\nname: {stem}\ndescription: d\nmodel: sonnet\ntools: Read\n---\n\nbody\n")
        chart = root / "org.json"
        chart.write_text(json.dumps(chart_obj))
        return d, chart

    def test_charted_agent_with_no_def_on_disk_errors(self):
        d, chart = self._fixture(["alpha"], {"ceo": {"agent": "alpha", "staff": ["ghost"]}})
        errors, _, _ = adl.lint(d, chart, max_kb=20.0)
        self.assertTrue(any("ghost" in e and "does not exist" in e for e in errors),
                        f"expected a missing-def ROSTER error, got {errors}")

    def test_def_on_disk_absent_from_chart_errors(self):
        d, chart = self._fixture(["alpha", "orphan"], {"ceo": {"agent": "alpha"}})
        errors, _, _ = adl.lint(d, chart, max_kb=20.0)
        self.assertTrue(any("orphan" in e and "absent from org_chart" in e for e in errors),
                        f"expected an absent-from-chart ROSTER error, got {errors}")

    def test_matching_roster_produces_no_error(self):
        d, chart = self._fixture(["alpha", "beta"],
                                 {"ceo": {"agent": "alpha", "staff": ["beta"]}})
        errors, _, _ = adl.lint(d, chart, max_kb=20.0)
        self.assertEqual(errors, [])

    def test_non_fleet_tooling_agent_is_not_flagged(self):
        d, chart = self._fixture(["alpha", "echo"], {"ceo": {"agent": "alpha"}})
        errors, _, _ = adl.lint(d, chart, max_kb=20.0)
        self.assertEqual(errors, [], "echo is in NON_FLEET and must be exempt")

    def test_missing_org_chart_errors_instead_of_raising(self):
        """The default chart lives on /Volumes/AI_Workspace, whose unavailability
        is a recurring failure here (the FDA/cask incident). The steward must get
        a routed ROSTER error, not a FileNotFoundError traceback."""
        d, _ = self._fixture(["alpha"], {"ceo": {"agent": "alpha"}})
        errors, _, _ = adl.lint(d, Path("/nonexistent/definitely/not/here.json"), max_kb=20.0)
        self.assertTrue(any("org chart not found" in e for e in errors),
                        f"expected a ROSTER not-found error, got {errors}")

    def test_empty_agents_dir_is_an_error_not_a_clean_pass(self):
        """An empty dir reporting 'clean' is how a dead gate goes unnoticed —
        the exact failure the clean-banner test claims to guard."""
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        empty = Path(td.name)
        errors, warnings, _ = adl.lint(empty, None, max_kb=20.0)
        self.assertTrue(any("no agent defs found" in e for e in errors))
        self.assertEqual(warnings, [])


class TestLintTiers(unittest.TestCase):
    """Size is a WARNING; broken frontmatter is an ERROR. Conflating them would
    make the 5 known-oversized defs block every efficiency-steward run."""

    def _dir_with(self, stem, fm, body="body\n"):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        d = Path(td.name)
        (d / f"{stem}.md").write_text(f"---\n{fm}\n---\n\n{body}")
        return d

    def test_oversize_warns_and_does_not_error(self):
        d = self._dir_with("fat", "name: fat\ndescription: d\nmodel: sonnet\n"
                                  "tools: Read", "x" * 40000)
        errors, warnings, _ = adl.lint(d, None, max_kb=20.0)
        self.assertEqual(errors, [])
        self.assertTrue(any("fat.md" in w for w in warnings))

    def test_name_filename_mismatch_is_an_error(self):
        """A `name:` that doesn't match the stem makes dispatch-by-name fail
        silently — the loudest possible failure is the right one."""
        d = self._dir_with("real", "name: WRONG\ndescription: d\nmodel: sonnet\n"
                                   "tools: Read")
        errors, _, _ = adl.lint(d, None, max_kb=20.0)
        self.assertTrue(any("real.md" in e and "!=" in e for e in errors))

    def test_clean_small_def_is_silent(self):
        d = self._dir_with("ok", "name: ok\ndescription: d\nmodel: sonnet\ntools: Read")
        errors, warnings, _ = adl.lint(d, None, max_kb=20.0)
        self.assertEqual((errors, warnings), ([], []))


class TestCli(unittest.TestCase):
    """The exit code IS the integration contract: efficiency-steward routes on
    it, so an always-0 exit would silently stop reporting broken agent defs — a
    lint that never barks. Driven as a subprocess because that is how the
    routine invokes it."""

    SCRIPT = REPO / "scripts" / "agent_def_lint.py"

    def _run(self, *args):
        return subprocess.run([sys.executable, str(self.SCRIPT), *args],
                              capture_output=True, text=True)

    def _agents_dir(self, **defs):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        d = Path(td.name)
        for stem, (fm, body) in defs.items():
            (d / f"{stem}.md").write_text(f"---\n{fm}\n---\n\n{body}")
        return d

    GOOD = ("name: {stem}\ndescription: d\nmodel: sonnet\ntools: Read", "body\n")

    def test_clean_fleet_exits_zero_and_says_so(self):
        """Exit code AND the banner: a silent zero-exit is indistinguishable from
        a lint that scanned nothing, which is how a dead gate goes unnoticed."""
        d = self._agents_dir(alpha=(self.GOOD[0].format(stem="alpha"), "body\n"))
        r = self._run("--agents-dir", str(d), "--no-roster")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("clean", r.stdout)
        self.assertNotIn("error(s)", r.stdout)

    def test_problem_run_prints_a_count_summary(self):
        """The non-clean branch must report how much it found."""
        d = self._agents_dir(bad=("nope\n", "body\n"))
        r = self._run("--agents-dir", str(d), "--no-roster")
        self.assertIn("error(s)", r.stdout)
        self.assertNotIn("clean —", r.stdout)

    def test_broken_frontmatter_exits_one(self):
        d = self._agents_dir(broken=("name: WRONG\ndescription: d\nmodel: sonnet\n"
                                     "tools: Read", "body\n"))
        r = self._run("--agents-dir", str(d), "--no-roster")
        self.assertEqual(r.returncode, 1, "a broken def must fail the gate")
        self.assertIn("FRONT", r.stdout)

    def test_oversize_alone_still_exits_zero(self):
        """Size is advisory. If it errored, the 5 known-oversized defs would
        block every efficiency-steward run and the gate would get disabled."""
        d = self._agents_dir(fat=(self.GOOD[0].format(stem="fat"), "x" * 40000))
        r = self._run("--agents-dir", str(d), "--no-roster")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("SIZE", r.stdout)

    def test_quiet_suppresses_the_clean_banner_but_not_problems(self):
        clean = self._agents_dir(alpha=(self.GOOD[0].format(stem="alpha"), "body\n"))
        r = self._run("--agents-dir", str(clean), "--no-roster", "--quiet")
        self.assertEqual(r.stdout.strip(), "", "quiet must be silent when clean")
        broken = self._agents_dir(bad=("nope\n", "body\n"))
        r2 = self._run("--agents-dir", str(broken), "--no-roster", "--quiet")
        self.assertIn("FRONT", r2.stdout, "quiet must still report defects")

    # These drive the CLI against the REAL REVIEWED_BASELINES_KB by naming the
    # fixture after a baselined def — so they exercise the actual wiring rather
    # than an injected dict, and would catch the constant being emptied.
    _BASELINED = "scriptwriter.md"          # real baseline 53.6KB, grace 2.0KB

    def _sized(self, kb):
        """A dir holding one def named after a baselined agent, sized to `kb`."""
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        d = Path(td.name)
        stem = self._BASELINED[:-3]
        head = f"---\nname: {stem}\ndescription: d\nmodel: sonnet\ntools: Read\n---\n\n"
        (d / self._BASELINED).write_text(head + "x" * (int(kb * 1024) - len(head)))
        return d

    def test_baselined_summary_is_shown_on_a_normal_run(self):
        """Silent must not mean hidden. If the carried defs stop printing, nobody
        can see what is being carried without reading the source — which is how a
        baseline quietly becomes a permanent excuse."""
        r = self._run("--agents-dir", str(self._sized(53.6)), "--no-roster")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("reviewed baseline", r.stdout)
        self.assertIn(self._BASELINED, r.stdout)

    def test_baselined_summary_is_suppressed_under_quiet(self):
        """--quiet is the cron path: problems only, never routine carry noise."""
        r = self._run("--agents-dir", str(self._sized(53.6)), "--no-roster", "--quiet")
        self.assertEqual(r.stdout.strip(), "",
                         "quiet must be silent for a baselined, non-growing def")

    def test_growth_is_reported_even_under_quiet(self):
        """The one thing --quiet must never swallow."""
        r = self._run("--agents-dir", str(self._sized(60.0)), "--no-roster", "--quiet")
        self.assertIn("GROWTH", r.stdout)

    def test_missing_agents_dir_is_a_usage_error(self):
        r = self._run("--agents-dir", "/nonexistent/definitely/not/here", "--no-roster")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("agents dir not found", r.stderr)

    def test_selftest_flag_runs_and_passes(self):
        r = self._run("--selftest")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("selftest: PASS", r.stdout)


class TestSelftest(unittest.TestCase):
    def test_module_selftest_passes(self):
        self.assertEqual(adl._selftest(), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
