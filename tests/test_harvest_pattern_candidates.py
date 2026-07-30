#!/usr/bin/env python3
"""Gate coverage for scripts/harvest_pattern_candidates.py.

This file IS the coverage. The script deliberately has no `--selftest`: a
mutation inside one is unkillable (weakening a check still returns 0), so 31
survivors permanently BLOCKED the gate while duplicating everything here. The
sibling lesson still holds — a selftest is never what precheck runs.

These are the properties that must go red.

The load-bearing ones, in order of what they protect:
  * a marker line is NEVER silently dropped (the whole point of the script)
  * the queue never harvests its own output (self-poisoning)
  * a human's promoted/rejected status is never reset by a later run
  * a corrupt state file REFUSES rather than starting empty (which would
    re-open every rejected candidate and lose every carried one)
"""
import json
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import harvest_pattern_candidates as H  # noqa: E402


def _vault(tmp: Path) -> Path:
    (tmp / H.PATTERNS_SUBDIR).mkdir(parents=True)
    (tmp / "06_CEO").mkdir()
    return tmp


class TestParseLine(unittest.TestCase):
    def test_wellformed(self):
        f = H.parse_line("PATTERN CANDIDATE: guards must not re-parse — "
                         "evidence: adaptation.py r4", "a.md", 7)
        self.assertEqual(f.kind, "ok")
        self.assertEqual(f.rule, "guards must not re-parse")
        self.assertEqual(f.evidence, "adaptation.py r4")
        self.assertEqual(f.ref, "a.md:7")

    def test_no_marker_returns_none(self):
        self.assertIsNone(H.parse_line("ordinary prose about patterns", "a.md", 1))

    def test_marker_without_evidence_is_reported_not_dropped(self):
        """The core safety property. An agent that wrote a slightly-off marker
        still did the hard part; dropping it is the failure being fixed."""
        f = H.parse_line("PATTERN CANDIDATE: a rule but no evidence key", "a.md", 2)
        self.assertIsNotNone(f)
        self.assertEqual(f.kind, "malformed")

    def test_empty_fields_are_malformed(self):
        self.assertEqual(H.parse_line("PATTERN CANDIDATE: evidence:", "a.md", 1).kind,
                         "malformed")
        self.assertEqual(H.parse_line("PATTERN CANDIDATE:  — evidence: x", "a.md", 1).kind,
                         "malformed")

    def test_separator_variants_all_parse(self):
        """The separator is cosmetic; `evidence:` is the anchor. Being one
        unicode dash away from dropping a real lesson is not acceptable."""
        for sep in ("—", "–", "-", "", "  --  "):
            with self.subTest(sep=sep):
                f = H.parse_line(f"PATTERN CANDIDATE: r {sep} evidence: e", "a.md", 1)
                self.assertEqual(f.kind, "ok")
                self.assertEqual(f.rule, "r")
                self.assertEqual(f.evidence, "e")

    def test_evidence_keyword_case_insensitive(self):
        self.assertEqual(
            H.parse_line("PATTERN CANDIDATE: r — Evidence: e", "a.md", 1).kind, "ok")

    def test_template_placeholder_not_queued(self):
        """The instruction block is quoted verbatim in 19 agent defs, the bridge
        file and design docs. Harvesting it seeds the queue with its own docs."""
        f = H.parse_line("PATTERN CANDIDATE: <one-sentence rule> — evidence: "
                         "<file / incident / number>", "bridge.md", 3)
        self.assertEqual(f.kind, "placeholder")

    def test_placeholder_in_evidence_only(self):
        f = H.parse_line("PATTERN CANDIDATE: a real rule — evidence: <ref>", "b.md", 1)
        self.assertEqual(f.kind, "placeholder")

    def test_prose_mention_is_not_malformed(self):
        """The marker will be named in docs, bridge entries and daily notes
        forever. Classing those as malformed fills the LOUD section with
        documentation noise until nobody reads it — found on the first live
        dry-run, where both malformed hits were ordinary prose."""
        f = H.parse_line("added `PATTERN CANDIDATE:` capture to 19 agent defs",
                         "daily.md", 5)
        self.assertEqual(f.kind, "mention")

    def test_mention_is_reported_not_dropped(self):
        f = H.parse_line("we discussed PATTERN CANDIDATE: lines today", "x.md", 1)
        self.assertIsNotNone(f)

    def test_line_owning_prefixes_still_emit(self):
        """Over-refusal check against how agents actually write: a bullet,
        blockquote, bold run or heading must not suppress a real candidate."""
        for pre in ("", "  ", "- ", "* ", "> ", "**", "#### "):
            with self.subTest(prefix=pre):
                f = H.parse_line(f"{pre}PATTERN CANDIDATE: r — evidence: e", "a.md", 1)
                self.assertEqual(f.kind, "ok")


class TestDedupe(unittest.TestCase):
    def test_normalization_collapses_noise(self):
        self.assertEqual(H.rule_hash("Guards  must NOT re-parse."),
                         H.rule_hash("guards must not re-parse"))

    def test_distinct_rules_distinct_hashes(self):
        self.assertNotEqual(H.rule_hash("guards must not re-parse"),
                            H.rule_hash("force-stub side-effecting modules"))

    def test_normalization_does_not_merge_near_misses(self):
        """Crude on purpose: merging two different lessons loses one."""
        self.assertNotEqual(H.rule_hash("stub dotenv in tests"),
                            H.rule_hash("never stub dotenv in tests"))


class TestCoverageHint(unittest.TestCase):
    TITLES = ["Pattern_guards_must_not_reparse", "Pattern_liveness_is_not_success"]

    def test_fires_on_real_overlap(self):
        self.assertEqual(
            H.coverage_hint("guards must not reparse untrusted yaml", self.TITLES),
            "Pattern_guards_must_not_reparse")

    def test_silent_on_weak_overlap(self):
        self.assertIsNone(H.coverage_hint("expand vo to full runtime", self.TITLES))

    def test_hint_never_filters(self):
        """A hint that could drop a candidate would lose lessons permanently."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            v = _vault(Path(td))
            (v / H.PATTERNS_SUBDIR / "Pattern_guards_must_not_reparse.md").write_text("x")
            (v / "06_CEO" / "b.md").write_text(
                "PATTERN CANDIDATE: guards must not reparse yaml — evidence: x.py:1\n")
            H.run(v, 30, False, time.time())
            q = (v / H.PATTERNS_SUBDIR / H.QUEUE_NAME).read_text()
            self.assertIn("guards must not reparse yaml", q)
            self.assertIn("Possible existing coverage", q)


class TestScan(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._td = tempfile.TemporaryDirectory()
        self.v = _vault(Path(self._td.name))

    def tearDown(self):
        self._td.cleanup()

    def test_skips_archive_and_tooling_dirs(self):
        (self.v / "07_Archive").mkdir()
        (self.v / "07_Archive" / "o.md").write_text(
            "PATTERN CANDIDATE: archived — evidence: x\n")
        (self.v / "06_CEO" / "b.md").write_text(
            "PATTERN CANDIDATE: live — evidence: x\n")
        found, _ = H.scan(self.v, 30, time.time())
        rules = {f.rule for f in found if f.kind == "ok"}
        self.assertIn("live", rules)
        self.assertNotIn("archived", rules)

    def test_mtime_window_bounds_the_scan(self):
        import os
        p = self.v / "06_CEO" / "old.md"
        p.write_text("PATTERN CANDIDATE: stale — evidence: x\n")
        past = time.time() - 90 * 86400
        os.utime(p, (past, past))
        found, _ = H.scan(self.v, 30, time.time())
        self.assertEqual([f for f in found if f.kind == "ok"], [])

    def test_ignores_non_markdown(self):
        (self.v / "06_CEO" / "x.txt").write_text(
            "PATTERN CANDIDATE: from txt — evidence: x\n")
        found, _ = H.scan(self.v, 30, time.time())
        self.assertEqual([f for f in found if f.kind == "ok"], [])

    def test_does_not_harvest_its_own_output(self):
        """Self-poisoning guard: the queue file quotes every candidate it lists,
        so a scan that included it would re-queue everything forever."""
        (self.v / "06_CEO" / "b.md").write_text(
            "PATTERN CANDIDATE: real rule — evidence: x\n")
        now = time.time()
        H.run(self.v, 30, False, now)
        H.run(self.v, 30, False, now)
        state = json.loads((self.v / H.PATTERNS_SUBDIR / H.STATE_NAME).read_text())
        self.assertEqual(len(state), 1)


class TestRun(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._td = tempfile.TemporaryDirectory()
        self.v = _vault(Path(self._td.name))
        self.q = self.v / H.PATTERNS_SUBDIR / H.QUEUE_NAME
        self.s = self.v / H.PATTERNS_SUBDIR / H.STATE_NAME

    def tearDown(self):
        self._td.cleanup()

    def _write(self, name, body):
        (self.v / "06_CEO" / name).write_text(body)

    def test_pending_candidate_exits_zero_and_writes_both_files(self):
        self._write("b.md", "PATTERN CANDIDATE: rule one — evidence: f.md:1\n")
        rc = H.run(self.v, 30, False, time.time())
        self.assertEqual(rc, 0)
        self.assertTrue(self.q.exists() and self.s.exists())
        self.assertIn("rule one", self.q.read_text())

    def test_empty_vault_exits_75(self):
        rc = H.run(self.v, 30, False, time.time())
        self.assertEqual(rc, 75)

    def test_malformed_alone_keeps_the_job_loud(self):
        """A broken emitter is a real finding; 75 would silence it."""
        self._write("b.md", "PATTERN CANDIDATE: no evidence key here\n")
        rc = H.run(self.v, 30, False, time.time())
        self.assertEqual(rc, 0)
        self.assertIn("Malformed markers", self.q.read_text())

    def test_placeholder_alone_is_a_noop(self):
        self._write("b.md", "PATTERN CANDIDATE: <rule> — evidence: <ref>\n")
        self.assertEqual(H.run(self.v, 30, False, time.time()), 75)

    def test_human_status_survives_rerun(self):
        self._write("b.md", "PATTERN CANDIDATE: rule one — evidence: f.md:1\n")
        now = time.time()
        H.run(self.v, 30, False, now)
        st = json.loads(self.s.read_text())
        h = next(iter(st))
        st[h]["status"] = "rejected"
        self.s.write_text(json.dumps(st))
        rc = H.run(self.v, 30, False, now)
        st2 = json.loads(self.s.read_text())
        self.assertEqual(st2[h]["status"], "rejected")
        self.assertEqual(rc, 75)
        self.assertNotIn("rule one", self.q.read_text().split("Malformed")[0])

    def test_candidate_outside_window_is_carried_not_lost(self):
        """Ageing out silently is the exact failure mode this replaces."""
        self._write("b.md", "PATTERN CANDIDATE: rule one — evidence: f.md:1\n")
        now = time.time()
        H.run(self.v, 30, False, now)
        H.run(self.v, 30, False, now + 400 * 86400)
        st = json.loads(self.s.read_text())
        self.assertEqual(len(st), 1)
        self.assertIn("rule one", self.q.read_text())

    def test_multiple_sources_merge_into_one_candidate(self):
        self._write("a.md", "PATTERN CANDIDATE: same rule — evidence: e1\n")
        self._write("b.md", "PATTERN CANDIDATE: Same  Rule. — evidence: e2\n")
        H.run(self.v, 30, False, time.time())
        st = json.loads(self.s.read_text())
        self.assertEqual(len(st), 1)
        entry = next(iter(st.values()))
        self.assertEqual(len(entry["sources"]), 2)
        self.assertEqual(len(entry["evidence"]), 2)

    def test_corrupt_state_refuses_rather_than_starting_empty(self):
        """Starting empty would re-open every rejected candidate and drop every
        carried one — a silent, unrecoverable data loss."""
        self.s.write_text("{not json")
        self._write("b.md", "PATTERN CANDIDATE: rule one — evidence: x\n")
        with self.assertRaises(SystemExit):
            H.run(self.v, 30, False, time.time())

    def test_missing_patterns_dir_is_an_error_not_a_noop(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(H.run(Path(td), 30, False, time.time()), 1)

    def test_dry_run_writes_nothing(self):
        self._write("b.md", "PATTERN CANDIDATE: rule one — evidence: x\n")
        H.run(self.v, 30, True, time.time())
        self.assertFalse(self.q.exists())
        self.assertFalse(self.s.exists())

    def test_dry_run_exit_codes_match_the_real_run(self):
        """Mutation survivor (line 362): the dry-run branch has its OWN exit
        path, and nothing asserted it — a dry-run could return anything."""
        self.assertEqual(H.run(self.v, 30, True, time.time()), 75)
        self._write("b.md", "PATTERN CANDIDATE: rule one — evidence: x\n")
        self.assertEqual(H.run(self.v, 30, True, time.time()), 0)

    def test_unreadable_file_marks_scan_incomplete(self):
        """An empty queue caused by a permission error must not read as healthy."""
        import os
        p = self.v / "06_CEO" / "locked.md"
        p.write_text("PATTERN CANDIDATE: hidden — evidence: x\n")
        os.chmod(p, 0o000)
        try:
            if os.access(p, os.R_OK):
                self.skipTest("running as root — chmod 000 is not enforced")
            _, unreadable = H.scan(self.v, 30, time.time())
            self.assertEqual(len(unreadable), 1)
            import contextlib
            import io
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                H.run(self.v, 30, False, time.time())
            self.assertIn("scan was INCOMPLETE", self.q.read_text())
            # Mutation survivor (line 369): the stdout warning could be deleted
            # OR fired unconditionally with nothing going red. The operator
            # reading job output is the one who notices an incomplete scan, so
            # the warning is load-bearing, not decoration.
            self.assertIn("WARNING scan incomplete", buf.getvalue())
        finally:
            os.chmod(p, 0o644)

    def test_clean_scan_emits_no_incomplete_warning(self):
        """The other half of the survivor: an always-on warning is as useless
        as an absent one."""
        import contextlib
        import io
        self._write("b.md", "PATTERN CANDIDATE: rule one — evidence: x\n")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            H.run(self.v, 30, False, time.time())
        self.assertNotIn("WARNING scan incomplete", buf.getvalue())


class TestMutationGaps(unittest.TestCase):
    """Each test here closes a specific surviving mutant from the whole-file
    mutation pass. Named by the behaviour that breaks, not by the mutant."""

    def setUp(self):
        import tempfile
        self._td = tempfile.TemporaryDirectory()
        self.v = _vault(Path(self._td.name))
        self.q = self.v / H.PATTERNS_SUBDIR / H.QUEUE_NAME

    def tearDown(self):
        self._td.cleanup()

    def test_reported_line_number_is_the_real_one(self):
        """Survivor (`start=1` -> `start=2`). The ref is how a human finds the
        source line; an off-by-one sends them to the wrong place silently."""
        (self.v / "06_CEO" / "b.md").write_text(
            "one\ntwo\nPATTERN CANDIDATE: on line three — evidence: x\n")
        found, _ = H.scan(self.v, 30, time.time())
        ok = [f for f in found if f.kind == "ok"]
        self.assertEqual(len(ok), 1)
        self.assertEqual(ok[0].line_no, 3)
        self.assertTrue(ok[0].ref.endswith(":3"))

    def test_queue_file_is_not_re_harvested(self):
        """Survivor (`p in own_files` -> False). The malformed section echoes
        each offending line VERBATIM, marker and all — so without the exclusion
        the queue re-ingests its own output every run."""
        (self.v / "06_CEO" / "bad.md").write_text(
            "PATTERN CANDIDATE: broken with no evidence key\n")
        now = time.time()
        H.run(self.v, 30, False, now)
        first = self.q.read_text()
        self.assertIn("PATTERN CANDIDATE: broken", first)   # verbatim echo
        H.run(self.v, 30, False, now)
        second = self.q.read_text()
        self.assertEqual(first.count("broken with no evidence key"),
                         second.count("broken with no evidence key"))
        # The load-bearing assertion. The count check above passes even with the
        # exclusion removed, because a re-ingested queue line lands mid-sentence
        # and is classed a `mention` — so it never touches that count. What the
        # guard actually prevents is the queue citing ITSELF as a source.
        # Matched as `name:` because every ref carries a line number; a bare
        # filename check would fail on the instructions paragraph, which names
        # both files legitimately.
        self.assertNotIn(f"{H.QUEUE_NAME}:", second)

    def test_repeat_sighting_does_not_duplicate_source_or_evidence(self):
        """Survivors (the `not in` guards -> True): re-running appends the same
        ref every time, growing a duplicate list forever."""
        (self.v / "06_CEO" / "b.md").write_text(
            "PATTERN CANDIDATE: same rule — evidence: e1\n")
        now = time.time()
        H.run(self.v, 30, False, now)
        H.run(self.v, 30, False, now)
        st = json.loads((self.v / H.PATTERNS_SUBDIR / H.STATE_NAME).read_text())
        entry = next(iter(st.values()))
        self.assertEqual(len(entry["sources"]), 1)
        self.assertEqual(len(entry["evidence"]), 1)

    def test_clean_run_omits_every_conditional_section(self):
        """Survivors (each section's `if` -> True). An always-on 'Malformed
        markers' or 'scan was INCOMPLETE' heading with nothing under it trains
        the reader to skip exactly the sections that are meant to be loud."""
        H.run(self.v, 30, False, time.time())
        text = self.q.read_text()
        self.assertIn("_No pending candidates._", text)
        # Asserted structurally, not by heading name. An earlier version listed
        # the headings as literals and silently decayed the moment one was
        # renamed ("Unreadable files" -> "Unreadable paths"), which let two
        # always-on-section mutants survive. A clean run has the H1 title and
        # NOTHING at H2 — that cannot rot when a heading is reworded.
        self.assertEqual([ln for ln in text.splitlines() if ln.startswith("## ")], [])
        self.assertNotIn("Possible existing coverage", text)

    def test_populated_run_omits_the_no_candidates_line(self):
        (self.v / "06_CEO" / "b.md").write_text(
            "PATTERN CANDIDATE: a rule — evidence: e\n")
        H.run(self.v, 30, False, time.time())
        self.assertNotIn("_No pending candidates._", self.q.read_text())

    def test_summary_counts_are_real(self):
        """Survivors (`n_new` / `n_bad`). The routine reports this line
        VERBATIM to Steve, so a wrong count is a wrong report."""
        import contextlib
        import io
        (self.v / "06_CEO" / "a.md").write_text(
            "PATTERN CANDIDATE: rule a — evidence: e\n"
            "PATTERN CANDIDATE: rule b — evidence: e\n"
            "PATTERN CANDIDATE: broken line\n")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            H.run(self.v, 30, False, time.time())
        out = buf.getvalue()
        self.assertIn("2 pending candidate(s)", out)
        self.assertIn("2 marker(s)", out)
        self.assertIn("1 malformed", out)

    def test_not_candidates_section_lists_only_non_candidates(self):
        """Survivors (the `placeholder` / `mention` comprehensions negated, and
        the section's `or` condition). A real candidate leaking into the
        'not a lesson' bucket is how a lesson gets quietly discarded."""
        # Separate files so each finding has a DISTINCT ref. The section lists
        # refs, not rule text, so a shared file makes the real candidate
        # indistinguishable from the noise and the assertion proves nothing.
        (self.v / "06_CEO" / "real.md").write_text(
            "PATTERN CANDIDATE: a genuine rule — evidence: e\n")
        (self.v / "06_CEO" / "tmpl.md").write_text(
            "PATTERN CANDIDATE: <template rule> — evidence: <ref>\n")
        (self.v / "06_CEO" / "prose.md").write_text(
            "we discussed PATTERN CANDIDATE: lines in review\n")
        H.run(self.v, 30, False, time.time())
        text = self.q.read_text()
        head, _, tail = text.partition("## Not candidates")
        self.assertTrue(tail, "section should be present")
        self.assertIn("tmpl.md", tail)
        self.assertIn("template placeholder", tail)
        self.assertIn("prose.md", tail)
        self.assertIn("prose mention", tail)
        # The real candidate belongs above the section and must never be
        # bucketed into it — that is how a lesson gets quietly discarded.
        self.assertIn("a genuine rule", head)
        self.assertNotIn("real.md", tail)
        self.assertEqual(tail.count("\n- `"), 2)

    def test_mention_alone_still_renders_the_section(self):
        """Survivor (`placeholders or mentions` -> `and`): with only mentions
        and no placeholders, an `and` silently drops the whole section."""
        (self.v / "06_CEO" / "a.md").write_text(
            "we discussed PATTERN CANDIDATE: lines in review\n")
        H.run(self.v, 30, False, time.time())
        self.assertIn("prose mention", self.q.read_text())

    def test_candidate_without_overlap_gets_no_coverage_line(self):
        """Survivor (`if hint:` -> True): an unconditional line renders the
        literal '[[None]]' as a wikilink into the knowledge graph."""
        (self.v / H.PATTERNS_SUBDIR / "Pattern_unrelated_topic.md").write_text("x")
        (self.v / "06_CEO" / "a.md").write_text(
            "PATTERN CANDIDATE: force stub side effecting imports — evidence: e\n")
        H.run(self.v, 30, False, time.time())
        text = self.q.read_text()
        self.assertIn("force stub side effecting imports", text)
        self.assertNotIn("Possible existing coverage", text)
        self.assertNotIn("None", text.split("## ")[-1])

    def test_coverage_hint_handles_a_wordless_rule(self):
        """Survivor (`not words` -> False)."""
        self.assertIsNone(H.coverage_hint("a b c 42", ["Pattern_x_y_z"]))

    def test_existing_pattern_titles_on_missing_dir(self):
        """Survivor (`not d.is_dir()` -> False)."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(H.existing_pattern_titles(Path(td)), [])

    def test_underscore_files_are_not_pattern_titles(self):
        """The MOC and the queue are not patterns; they must never be offered
        as 'possible existing coverage'."""
        d = self.v / H.PATTERNS_SUBDIR
        (d / "_Patterns_MOC.md").write_text("x")
        (d / "Pattern_real.md").write_text("x")
        self.assertEqual(H.existing_pattern_titles(self.v), ["Pattern_real"])


class TestMain(unittest.TestCase):
    """Mutation survivor: the argparse dispatch was untested, so a flag could
    have stopped reaching run() unnoticed."""

    def test_main_dispatches_dry_run(self):
        import contextlib
        import io
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            v = _vault(Path(td))
            (v / "06_CEO" / "b.md").write_text(
                "PATTERN CANDIDATE: from main — evidence: x\n")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(H.main(["--vault", str(v), "--dry-run"]), 0)
            self.assertFalse((v / H.PATTERNS_SUBDIR / H.QUEUE_NAME).exists())

    def test_vault_is_required_no_production_default(self):
        """HIGH-1. `--vault` defaulted to the real vault, so ANY harness that
        imported this module with the `__main__` guard disturbed — which is
        exactly what mutate.py does — ran main() on an empty argv, resolved the
        default, and rewrote Steve's live Drive-synced vault. It then exited 0
        with zero tests run, which the gate scored as a passing suite. A guard
        cannot fix that (the guard is what gets mutated); having no default can.
        argparse exits 2 on the missing argument and writes nothing."""
        import contextlib
        import io
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                H.main([])
        self.assertEqual(cm.exception.code, 2)

    def test_cli_exit_codes_as_the_routine_branches_on_them(self):
        """LOW-2 r5. Nothing in the gate ever ran the actual command
        `vault-gardener.prompt` step 10b invokes, so the four exit codes the
        routine branches on (75 quiet / 0 needs-human / 2 bad invocation /
        1 error) were only ever verified by hand. Runs the real subprocess."""
        import subprocess
        import tempfile
        script = Path(__file__).resolve().parents[1] / "scripts" / \
            "harvest_pattern_candidates.py"
        with tempfile.TemporaryDirectory() as td:
            v = _vault(Path(td))
            def run(*args):
                return subprocess.run([sys.executable, str(script), *args],
                                      capture_output=True, text=True).returncode
            self.assertEqual(run("--vault", str(v)), 75, "clean vault must be quiet")
            (v / "06_CEO" / "a.md").write_text(
                "PATTERN CANDIDATE: a real rule — evidence: e\n")
            self.assertEqual(run("--vault", str(v)), 0, "pending must be loud")
            self.assertEqual(run(), 2, "missing --vault must fail, not default")
            self.assertEqual(run("--vault", str(Path(td) / "nope")), 1,
                             "a vault with no Patterns dir is an error")

    def test_main_honours_vault_and_days(self):
        import contextlib
        import io
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            v = _vault(Path(td))
            (v / "06_CEO" / "b.md").write_text(
                "PATTERN CANDIDATE: from main — evidence: x\n")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = H.main(["--vault", str(v), "--days", "30"])
            self.assertEqual(rc, 0)
            self.assertIn("from main",
                          (v / H.PATTERNS_SUBDIR / H.QUEUE_NAME).read_text())


class TestReviewFixes(unittest.TestCase):
    """Regressions for the 2026-07-29 review BLOCK. Each names the finding it
    closes. Two of these (H3, H4) were proved blind by the reviewer applying the
    fix as a mutation and watching the suite stay green — the tests that looked
    like they covered these did not."""

    def setUp(self):
        import tempfile
        self._td = tempfile.TemporaryDirectory()
        self.v = _vault(Path(self._td.name))
        self.q = self.v / H.PATTERNS_SUBDIR / H.QUEUE_NAME
        self.s = self.v / H.PATTERNS_SUBDIR / H.STATE_NAME

    def tearDown(self):
        self._td.cleanup()

    # --- H1: marker detection must not be case- or decoration-brittle --------

    def test_case_and_decoration_variants_are_never_dropped(self):
        """H1. `MARKER not in line` was case-SENSITIVE, so every one of these
        returned None — silently dropped, falsifying the docstring's central
        claim. Bold-with-the-colon-outside is the likeliest drift, because all
        19 defs show the line in backticks and LLMs bold their labels."""
        for line in (
            "Pattern candidate: a real rule — evidence: f.md:1",
            "Pattern Candidate: a real rule — evidence: f.md:1",
            "**PATTERN CANDIDATE**: a real rule — evidence: f.md:1",
            "PATTERN  CANDIDATE: a real rule — evidence: f.md:1",
            "`PATTERN CANDIDATE:` a real rule — evidence: f.md:1",
            "1. PATTERN CANDIDATE: a real rule — evidence: f.md:1",
            "1) PATTERN CANDIDATE: a real rule — evidence: f.md:1",
        ):
            with self.subTest(line=line):
                f = H.parse_line(line, "a.md", 1)
                self.assertIsNotNone(f, "silently dropped")
                self.assertEqual(f.kind, "ok")
                self.assertEqual(f.rule, "a real rule")
                self.assertEqual(f.evidence, "f.md:1")

    def test_colonless_paraphrase_surfaces_as_a_mention(self):
        """C2's fingerprint: the live 2026-07-29 loss was this exact shape, and it
        must surface rather than vanish.

        Asserts `mention` EXACTLY, not `in ("malformed", "mention")`. The loose
        assertion was the seventh pass-for-the-wrong-reason test in this suite:
        named `_is_loud_`, it accepted the quiet bucket, and this input really is
        a mention (the leading "Emitted 1" defeats _PREFIX, so the marker does not
        own the line). It therefore never exercised the malformed branch it was
        named for — that branch is covered by
        test_line_owning_colonless_marker_is_malformed."""
        f = H.parse_line(
            "- Emitted 1 PATTERN CANDIDATE (add `provenance:` to notes)", "d.md", 5)
        self.assertEqual(f.kind, "mention")

    def test_two_markers_on_one_line_is_malformed_not_merged(self):
        f = H.parse_line("PATTERN CANDIDATE: rule a — evidence: e1 "
                         "PATTERN CANDIDATE: rule b — evidence: e2", "a.md", 1)
        self.assertEqual(f.kind, "malformed")

    # --- H2: only a WHOLE-field placeholder is template text ----------------

    def test_real_rules_containing_angle_brackets_are_queued(self):
        """H2. `<[^>]{2,}>` searched anywhere, so these real lessons were filed
        under a heading that says 'nothing here is a lesson' — and that section
        did not even echo the rule text. Angle brackets are this repo's slot
        convention, so they appear inside exactly the rules worth keeping."""
        for rule in (
            "always run precheck.sh --mutate <changed_file.py> first",
            "strip <script> before rendering user html",
        ):
            with self.subTest(rule=rule):
                f = H.parse_line(f"PATTERN CANDIDATE: {rule} — evidence: r3",
                                 "a.md", 1)
                self.assertEqual(f.kind, "ok")
                self.assertEqual(f.rule, rule)

    def test_angle_brackets_in_evidence_do_not_disqualify(self):
        f = H.parse_line("PATTERN CANDIDATE: verify sender domain — "
                         "evidence: mail from <a@b.com>", "a.md", 1)
        self.assertEqual(f.kind, "ok")

    def test_not_candidates_section_echoes_the_line(self):
        """H2. A real lesson misfiled here must still be readable."""
        (self.v / "06_CEO" / "a.md").write_text(
            "PATTERN CANDIDATE: <one-sentence rule> — evidence: <ref>\n")
        H.run(self.v, 30, False, time.time())
        self.assertIn("<one-sentence rule>", self.q.read_text())

    # --- H3: an INCOMPLETE scan must not exit 75 ----------------------------

    def test_unreadable_file_exits_zero_not_seventyfive(self):
        """H3. `unreadable` was absent from the exit expression, so a permission
        error returned 75 = 'healthy, stay quiet' — and the routine then told
        Steve 'no pending candidates'. The pre-existing test asserted the queue
        text and the stdout warning but never the exit code, so it read as
        covering this and did not."""
        import contextlib
        import io
        import os
        p = self.v / "06_CEO" / "locked.md"
        p.write_text("PATTERN CANDIDATE: hidden lesson — evidence: x\n")
        os.chmod(p, 0o000)
        try:
            if os.access(p, os.R_OK):
                self.skipTest("running as root — chmod 000 not enforced")
            with contextlib.redirect_stdout(io.StringIO()):
                rc = H.run(self.v, 30, False, time.time())
            self.assertEqual(rc, 0)
        finally:
            os.chmod(p, 0o644)

    def test_unreadable_directory_is_surfaced(self):
        """M1. Path.rglob swallows a scandir PermissionError, so an unreadable
        DIRECTORY holding a candidate produced findings=[] unreadable=[] and
        read as perfectly healthy."""
        import os
        d = self.v / "06_CEO" / "locked_dir"
        d.mkdir()
        (d / "x.md").write_text("PATTERN CANDIDATE: buried — evidence: x\n")
        os.chmod(d, 0o000)
        try:
            if os.access(d, os.R_OK):
                self.skipTest("running as root — chmod 000 not enforced")
            _, unreadable = H.scan(self.v, 30, time.time())
            self.assertTrue(unreadable, "unreadable dir was swallowed")
        finally:
            os.chmod(d, 0o755)

    # --- H4: non-dict state must refuse ------------------------------------

    def test_valid_json_of_wrong_shape_refuses(self):
        """H4. `return data if isinstance(data, dict) else {}` was the exact
        silent reset its own comment forbids, on the one file a human must
        hand-edit. The old test only covered INVALID json."""
        for payload in ("[]", "null", '"promoted"', '[{"a":1}]', "42"):
            with self.subTest(payload=payload):
                self.s.write_text(payload)
                with self.assertRaises(SystemExit):
                    H.load_state(self.s)

    # --- M3: a hand-edit typo must not brick the run -----------------------

    def test_unusable_state_rows_are_skipped_and_reported(self):
        """M3. A bare string raised AttributeError and a missing `rule` raised
        KeyError — loud, so nothing was lost, but it bricked the harvest behind
        a traceback on a file humans are required to edit."""
        self.s.write_text(json.dumps({
            "aaa": "just a string",
            "bbb": {"status": "pending"},
            "ccc": {"rule": "a good one", "status": "pending",
                    "first_seen": "2026-07-01", "sources": [], "evidence": []},
        }))
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = H.run(self.v, 30, False, time.time())
        text = self.q.read_text()
        self.assertEqual(rc, 0)
        self.assertIn("a good one", text)
        self.assertIn("Unusable state entries", text)
        self.assertIn("`aaa`", text)
        self.assertIn("`bbb`", text)
        self.assertIn("unusable state", buf.getvalue())

    def test_bad_rows_only_still_exits_zero(self):
        """MEDIUM-1. The `bad_state` term in `needs_human` had no fixture: the
        existing test's state also carried a GOOD pending row, so `n_pending`
        alone forced rc=0 and removing `bad_state` from the expression stayed
        green. Live shape: a state file with only unusable rows and nothing
        pending exited 75 = 'healthy, stay quiet'."""
        import contextlib
        import io
        self.s.write_text(json.dumps({"aaa": "just a string"}))
        with contextlib.redirect_stdout(io.StringIO()):
            rc = H.run(self.v, 30, False, time.time())
        self.assertEqual(rc, 0)

    def test_wrong_typed_state_fields_do_not_crash_the_run(self):
        """MEDIUM-2. Validating `rule` alone closed the instance, not the class:
        five realistic hand-edit typos still bricked the run with an uncaught
        traceback — the exact outcome partition_state exists to prevent."""
        import contextlib
        import io
        for label, row in (
            ("sources int", {"rule": "r", "status": "pending", "sources": 5}),
            ("sources str", {"rule": "r", "status": "pending", "sources": "a"}),
            ("evidence int", {"rule": "r", "status": "pending", "evidence": 7}),
        ):
            with self.subTest(case=label):
                self.s.write_text(json.dumps({"aaa": row}))
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = H.run(self.v, 30, False, time.time())   # must not raise
                self.assertEqual(rc, 0)
                self.assertIn("must be a list", self.q.read_text())

    def test_null_first_seen_with_two_rows_does_not_crash(self):
        """MEDIUM-2. The sort key raised TypeError comparing str to None — and
        only once a SECOND row existed, so a single-row vault hid it."""
        import contextlib
        import io
        self.s.write_text(json.dumps({
            "aaa": {"rule": "one", "status": "pending", "first_seen": None},
            "bbb": {"rule": "two", "status": "pending", "first_seen": "2026-07-01"},
        }))
        with contextlib.redirect_stdout(io.StringIO()):
            rc = H.run(self.v, 30, False, time.time())
        self.assertEqual(rc, 0)
        self.assertIn("one", self.q.read_text())

    def test_unusable_row_survives_a_second_run(self):
        """CRITICAL-1 r4. Rows routed to `bad` were absent from the rebuilt state,
        so the NEXT write deleted them — while the queue printed 'skipped, not
        deleted — fix the row and they return.' The gardener runs daily and
        rewrites the queue, so the one report naming the row expired after a
        single morning: rule text AND the human's decision, gone with no trace.
        Both prior fixtures called run() once and neither read the state back, so
        preserving and deleting were equally green. This runs TWICE and reads the
        persisted state — the only way to see it."""
        import contextlib
        import io
        self.s.write_text(json.dumps({"aaa": {
            "rule": "a hard-won lesson", "status": "rejected", "sources": 5}}))
        now = time.time()
        with contextlib.redirect_stdout(io.StringIO()):
            H.run(self.v, 30, False, now)
        st1 = json.loads(self.s.read_text())
        self.assertIn("aaa", st1, "unusable row deleted on the FIRST write")
        self.assertEqual(st1["aaa"]["status"], "rejected", "human decision lost")
        # NOTE the vault deliberately contains no emitting .md — the COLLISION
        # half (the live emission re-creating the quarantined key) is covered by
        # test_quarantined_row_is_not_resurrected_by_a_live_emission. This test's
        # earlier version had no emitting file either, which is exactly why it
        # read broader than it was and missed the collision bug entirely.
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = H.run(self.v, 30, False, now)
        st2 = json.loads(self.s.read_text())
        self.assertIn("aaa", st2, "unusable row deleted on the SECOND write")
        self.assertEqual(st2["aaa"]["rule"], "a hard-won lesson")
        # and it must still be REPORTED on run 2, not silently carried
        self.assertEqual(rc, 0)
        self.assertIn("Unusable state entries", self.q.read_text())

    def test_quarantined_row_is_not_resurrected_by_a_live_emission(self):
        """HIGH-1 r5. Preserving the bad row was not enough. Its key is absent
        from `good_state`, so merge_state sees the STILL-LIVE emission as brand
        new and re-creates the same key as a fresh `pending` row, which won the
        merge — silently resetting the human's decision and bumping first_seen,
        with no report on the next run.

        Not a corner: an emission stays in the window for 30 days after its
        file's mtime, so every row created in the workflow's first month is in
        this path. All four rows in the live state file were.

        Mutation cannot cover this — there is no drop-a-key or reorder-a-dict
        operator, so swapping the merge order leaves every test green. The
        fixture must exist."""
        import contextlib
        import io
        rule = "a lesson still being emitted"
        (self.v / "06_CEO" / "live.md").write_text(
            f"PATTERN CANDIDATE: {rule} — evidence: e\n")
        now = time.time()
        with contextlib.redirect_stdout(io.StringIO()):
            H.run(self.v, 30, False, now)          # run 1 creates it
        st = json.loads(self.s.read_text())
        key = next(iter(st))
        # the human judges it, and fatfingers the casing at the same time
        st[key]["status"] = "Rejected"
        self.s.write_text(json.dumps(st))
        buf = io.StringIO()
        # Run 2 is TWO DAYS LATER on purpose. With both runs sharing one `now`,
        # `today` is identical and the first_seen assertion below is vacuous — it
        # holds in the resurrected case too, so it could never fail. Advancing the
        # clock is what makes the re-aging check real.
        with contextlib.redirect_stdout(buf):
            rc = H.run(self.v, 30, False, now + 2 * 86400)
        st2 = json.loads(self.s.read_text())
        self.assertEqual(st2[key]["status"], "Rejected",
                         "live emission resurrected the row and reset the decision")
        self.assertEqual(st2[key].get("first_seen"), st[key].get("first_seen"),
                         "first_seen was bumped, so the row re-aged")
        # still named every run until the human fixes the typo
        self.assertEqual(rc, 0)
        self.assertIn("unknown status", self.q.read_text())
        # and it must NOT be sitting in the pending list
        self.assertNotIn(rule, self.q.read_text().split("## ⚠️")[0])

    def test_non_utf8_file_does_not_abort_the_harvest(self):
        """LOW-3 r6. `errors="replace"` is string content, so mutation is blind to
        it — flipping it to strict leaves all 88 tests green. It matters because
        UnicodeDecodeError is a ValueError, NOT an OSError, so it escapes the
        `except OSError` in scan() and one latin-1 .md anywhere in the vault would
        abort the entire harvest with a traceback instead of landing in the
        `unreadable` bucket."""
        # plain "-" separator: an em-dash is not encodable in latin-1, which is
        # what makes this a latin-1 file in the first place.
        (self.v / "06_CEO" / "latin1.md").write_bytes(
            "PATTERN CANDIDATE: caf\xe9 pricing rule - evidence: e\n".encode("latin-1"))
        (self.v / "06_CEO" / "clean.md").write_text(
            "PATTERN CANDIDATE: a clean lesson — evidence: e\n")
        found, unreadable = H.scan(self.v, 30, time.time())      # must not raise
        rules = {f.rule for f in found if f.kind == "ok"}
        self.assertIn("a clean lesson", rules, "one bad byte lost the whole scan")
        self.assertEqual(unreadable, [])
        self.assertEqual(len(rules), 2, "the latin-1 line should still parse")

    def test_valid_status_membership_is_pinned(self):
        """MEDIUM-1 r5. Adding a 4th value survives the suite green, and every
        value other than `pending` is one render_queue HIDES — so widening the
        allowlist by one word silently reinstates the r4 disappearance bug.
        HASH_WIDTH and MAX_REFS each got an independent literal for this reason;
        status did not."""
        self.assertEqual(sorted(H.VALID_STATUS),
                         ["pending", "promoted", "rejected"])

    def test_unknown_status_is_reported_not_silently_hidden(self):
        """HIGH-1 r4. `status` was the ONLY field a human is told to edit and the
        only one unvalidated — the three that WERE checked are machine-written.
        render_queue filters `== "pending"`, so a typo made the candidate vanish
        with rc=75 'healthy, stay quiet' and the routine reporting 'no pending
        candidates'."""
        import contextlib
        import io
        for bad in ("Pending", "pendign", "PENDING", "", None, True, 5):
            with self.subTest(status=bad):
                self.s.write_text(json.dumps({"aaa": {
                    "rule": "a real lesson", "status": bad,
                    "sources": [], "evidence": [], "first_seen": "2026-07-01"}}))
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = H.run(self.v, 30, False, time.time())
                self.assertEqual(rc, 0, f"status {bad!r} exited quiet")
                text = self.q.read_text()
                self.assertIn("Unusable state entries", text)
                self.assertIn("unknown status", text)
                # and the row itself must survive for the human to fix
                self.assertIn("aaa", json.loads(self.s.read_text()))

    def test_the_three_valid_statuses_are_accepted(self):
        """Over-refusal check: the validator must not reject legitimate values."""
        good, bad = H.partition_state({
            "a": {"rule": "r", "status": "pending"},
            "b": {"rule": "r", "status": "promoted"},
            "c": {"rule": "r", "status": "rejected"},
            "d": {"rule": "r"},                      # absent -> defaults pending
        })
        self.assertEqual(sorted(good), ["a", "b", "c", "d"])
        self.assertEqual(bad, [])
        # L1 r7: partition_state and render_queue each default a missing `status`
        # to "pending", and NOTHING pinned that the two agree. Flipping
        # render_queue's default to "rejected" makes a status-less row vanish with
        # rc=75 "healthy" and the routine reporting "no pending candidates" — the
        # r4 disappearance bug exactly — while all 86 tests stay green. Pins the
        # render side end-to-end.
        import contextlib
        import io
        self.s.write_text(json.dumps({"nostatus": {
            "rule": "a row with no status key", "sources": [], "evidence": [],
            "first_seen": "2026-07-01"}}))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = H.run(self.v, 30, False, time.time())
        self.assertEqual(rc, 0, "a status-less row must count as pending")
        self.assertIn("a row with no status key", self.q.read_text())

    def test_line_owning_colonless_marker_is_malformed(self):
        """MEDIUM-1 r4. Collapsing the malformed/mention branch to a bare
        `mention` left all 80 tests green — LOUD-on-a-colon-less-emission, the
        entire reason the loose tier exists, was unasserted. The test named
        `_is_loud_` accepted `mention` and its input actually classifies as
        `mention` (the leading "Emitted 1" defeats _PREFIX), so it never once
        exercised this branch."""
        f = H.parse_line("PATTERN CANDIDATE add provenance to notes", "d.md", 1)
        self.assertEqual(f.kind, "malformed")

    def test_mtime_window_boundary_is_the_real_number(self):
        """LOW-3 r4. The existing window test used a 90-day-old file against a
        30-day window, so any seconds-per-day constant from ~1 to 86400 passed."""
        import os
        p = self.v / "06_CEO" / "edge.md"
        p.write_text("PATTERN CANDIDATE: near the edge — evidence: x\n")
        now = time.time()
        for age_days, expected in ((29, 1), (31, 0)):
            with self.subTest(age_days=age_days):
                t = now - age_days * 86400
                os.utime(p, (t, t))
                found, _ = H.scan(self.v, 30, now)
                self.assertEqual(len([f for f in found if f.kind == "ok"]), expected)

    def test_hash_width_is_pinned_to_twelve(self):
        """HIGH-1 r3. The width is the PERSISTED STATE KEY, not a display detail.
        A `# mutequiv:` marker blessed 'any length in roughly 8..64' — and
        `[:12]` -> `[:16]` re-keys every row, so a rule Steve already REJECTED
        returns as pending and the state double-books. Measured, gate fully
        green. The literal is independent on purpose: changing the width must be
        a deliberate decision plus a state migration."""
        self.assertEqual(H.HASH_WIDTH, 12)
        self.assertEqual(len(H.rule_hash("any rule at all")), 12)

    def test_rejected_status_survives_across_runs(self):
        """The property HIGH-1 broke, asserted end-to-end rather than by width:
        a human decision must not resurrect, and the rule must not double-book."""
        import contextlib
        import io
        (self.v / "06_CEO" / "a.md").write_text(
            "PATTERN CANDIDATE: a lesson already judged — evidence: e\n")
        now = time.time()
        with contextlib.redirect_stdout(io.StringIO()):
            H.run(self.v, 30, False, now)
        st = json.loads(self.s.read_text())
        self.assertEqual(len(st), 1)
        key = next(iter(st))
        st[key]["status"] = "rejected"
        self.s.write_text(json.dumps(st))
        with contextlib.redirect_stdout(io.StringIO()):
            H.run(self.v, 30, False, now)
        st2 = json.loads(self.s.read_text())
        self.assertEqual(len(st2), 1, "state double-booked the same rule")
        self.assertEqual(st2[key]["status"], "rejected")
        self.assertNotIn("a lesson already judged",
                         self.q.read_text().split("## Not candidates")[0])

    def test_loose_tier_stays_case_insensitive(self):
        """MEDIUM-1 r3. Regex literals are mutation-BLIND, so the gate can never
        see this tier. The only fixture touching it fed an ALL-CAPS line, which
        passes identically under the old caps-only regex — the sixth
        pass-for-the-wrong-reason test. This one bites: making the loose tier
        case-sensitive again reintroduces the round-2 defect."""
        f = H.parse_line("Pattern Candidate - always X - evidence: y", "a.md", 1)
        self.assertIsNotNone(f, "title-case emission silently dropped")

    def test_plural_heading_is_not_a_finding(self):
        """MEDIUM-1 r3, other direction. Dropping `(?!s)` from the loose tier
        adds 7 permanent mentions over the live corpus — the routine's own
        heading EVERY day, plus this script's own filename three times."""
        self.assertIsNone(H.parse_line("### 🧪 Pattern candidates emitted",
                                       "daily.md", 80))
        self.assertIsNone(H.parse_line(
            "run scripts/harvest_pattern_candidates.py --vault x", "d.md", 1))

    def test_plural_with_colon_is_not_dropped(self):
        """The invariant said 'None only when the marker is absent', and
        `PATTERN CANDIDATES:` returned None. 0 live occurrences, so no lesson was
        lost — but a colon proves intent, so accepting plural costs no noise."""
        f = H.parse_line("PATTERN CANDIDATES: a real rule — evidence: e", "a.md", 1)
        self.assertEqual(f.kind, "ok")
        self.assertEqual(f.rule, "a real rule")

    def test_task_list_bullet_is_an_emission(self):
        """A checkbox bullet classed a real emission as prose and discarded the
        rule. Checklists are ordinary agent-report formatting."""
        for pre in ("* [ ] ", "- [x] ", "[ ] "):
            with self.subTest(prefix=pre):
                f = H.parse_line(f"{pre}PATTERN CANDIDATE: r — evidence: e",
                                 "a.md", 1)
                self.assertEqual(f.kind, "ok")

    def test_bad_element_types_render_instead_of_crashing(self):
        """MEDIUM-2 r3. Validating the CONTAINER type left the ELEMENTS open:
        `[5]`, `[None]`, `[{...}]` all raised TypeError in the join, so the
        'every field is type-checked' comment overstated the fix."""
        import contextlib
        import io
        for label, sources in (("int", [5]), ("none", [None]), ("dict", [{"a": 1}])):
            with self.subTest(case=label):
                self.s.write_text(json.dumps({"aaa": {
                    "rule": "r", "status": "pending", "sources": sources,
                    "evidence": [], "first_seen": "2026-07-01"}}))
                with contextlib.redirect_stdout(io.StringIO()):
                    rc = H.run(self.v, 30, False, time.time())   # must not raise
                self.assertEqual(rc, 0)

    def test_newline_in_rule_does_not_break_the_heading(self):
        import contextlib
        import io
        self.s.write_text(json.dumps({"aaa": {
            "rule": "line one\nline two", "status": "pending",
            "sources": [], "evidence": [], "first_seen": "2026-07-01"}}))
        with contextlib.redirect_stdout(io.StringIO()):
            H.run(self.v, 30, False, time.time())
        heads = [ln for ln in self.q.read_text().splitlines() if ln.startswith("## ")]
        self.assertEqual(len(heads), 1)
        self.assertIn("line one line two", heads[0])

    def test_rule_beginning_with_a_slot_is_still_a_lesson(self):
        """L4. `_PLACEHOLDER_RE` un-anchoring at the END left the suite green
        (`.match` already anchors the start), so nothing caught a rule that
        BEGINS with a slot being misfiled as template text."""
        f = H.parse_line("PATTERN CANDIDATE: <script> tags must be escaped "
                         "before render — evidence: sec audit", "a.md", 1)
        self.assertEqual(f.kind, "ok")

    def test_graphify_out_is_skipped(self):
        """L4. String content is mutation-blind, so dropping this from
        SKIP_DIR_NAMES left the suite green — and graphify-out already holds a
        verbatim copy of the queue."""
        d = self.v / "graphify-out"
        d.mkdir()
        (d / "GRAPH_REPORT.md").write_text(
            "PATTERN CANDIDATE: copied from the queue — evidence: x\n")
        found, _ = H.scan(self.v, 30, time.time())
        self.assertEqual([f for f in found if f.kind == "ok"], [])

    def test_clean_state_emits_no_unusable_warning(self):
        """The other direction: an always-on warning is as useless as an absent
        one, and both mutants survived until this existed."""
        import contextlib
        import io
        (self.v / "06_CEO" / "a.md").write_text(
            "PATTERN CANDIDATE: a rule — evidence: e\n")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            H.run(self.v, 30, False, time.time())
        self.assertNotIn("unusable state", buf.getvalue())
        self.assertNotIn("Unusable state entries", self.q.read_text())

    # --- M4: atomic write ---------------------------------------------------

    def test_atomic_write_preserves_old_content_on_failure(self):
        """HIGH-2. `test_state_write_leaves_no_temp_file` could not tell an
        atomic write from a plain one — a plain `write_text` also leaves no
        `.tmp`, and json.loads on a one-shot write always succeeds. Reducing
        `_write_atomic` to `path.write_text(...)` left the whole suite green, so
        M4 was uncovered. THE property is: a failed write must not destroy what
        was there. Forced by making os.replace raise."""
        import unittest.mock
        target = self.v / H.PATTERNS_SUBDIR / "atomic_probe.json"
        target.write_text('{"keep": "me"}')
        with unittest.mock.patch("harvest_pattern_candidates.os.replace",
                                 side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                H._write_atomic(target, '{"clobbered": true}')
        self.assertEqual(target.read_text(), '{"keep": "me"}')

    def test_state_write_leaves_no_temp_file(self):
        (self.v / "06_CEO" / "a.md").write_text(
            "PATTERN CANDIDATE: a rule — evidence: e\n")
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            H.run(self.v, 30, False, time.time())
        leftovers = list((self.v / H.PATTERNS_SUBDIR).glob("*.tmp"))
        self.assertEqual(leftovers, [])
        json.loads(self.s.read_text())      # valid JSON, fully written

    # --- Low: ref list is capped -------------------------------------------

    def test_source_refs_are_capped_at_ten(self):
        """The literal 10 is deliberate. `assertLessEqual(len(x), H.MAX_REFS)`
        compared the constant to itself, so raising MAX_REFS kept the test green
        — a tautology, and the last mutation survivor in the file. An
        independent literal makes a cap change a decision, not a silent drift."""
        findings = [H.Finding("ok", "f.md", i, rule="same rule", evidence=f"e{i}")
                    for i in range(1, 26)]
        st = H.merge_state({}, findings, "2026-07-29")
        entry = next(iter(st.values()))
        self.assertEqual(len(entry["sources"]), 10)
        self.assertEqual(len(entry["evidence"]), 10)
        # keeps the MOST RECENT sightings, not the oldest
        self.assertEqual(entry["sources"][-1], "f.md:25")

    def test_no_selftest_remains(self):
        """C1. `_selftest` was 31 of the 32 mutation survivors that BLOCKED the
        gate, every property duplicated by this suite. If it comes back, the
        gate goes permanently red again."""
        self.assertFalse(hasattr(H, "_selftest"))


if __name__ == "__main__":
    unittest.main()
