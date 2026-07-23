"""A-51 / DQ-39 — the review gate must honour a `resolution:` close.

Root cause these pin: run_stage_review_gate is RE-ENTRANT — it re-derives its
verdict from scratch every hourly sweep. Before this fix it could not see a
the `resolution:` stamp, so a stage that had been fixed, re-reviewed clean and
closed got re-opened on the next sweep: producer re-dispatched, reviewer
re-dispatched, stage parked back at needs-steve. Two opus calls per sweep, per
stage, forever, on already-approved work.

The dangerous direction is a FALSE PROMOTE — advancing work nothing approved —
so most of these assert a stamp is NOT honoured. Three of them pin bugs that were
live in earlier drafts of this fix and were caught at review:
  * a valueless `resolution:` promoted (`\\s*` matched the newline before `---`)
  * a ``` fenced EXAMPLE of the convention in the review BODY promoted a REVISE
  * the stamp was permanently sticky — no freshness check at all
"""
import importlib.util
import os
import tempfile
import time
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_MODULE_PATH = _HERE.parent / "scripts" / "pipeline_orchestrator.py"
_spec = importlib.util.spec_from_file_location("pipeline_orchestrator", _MODULE_PATH)
po = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(po)

STAGE = "7_packaging"          # a real resolution-stamping review gate
VIDEO = 99                     # a video number with no real files on disk
GOOD = "---\nresolution: closed-fix-applied-2026-07-20\n---\n\nVERDICT: SHIP\n"


class _VaultCase(unittest.TestCase):
    """Redirects vault-relative lookups into a scratch tree, so no test ever
    reads a real review file or depends on what happens to be on disk."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._orig = po.vault_abs
        po.vault_abs = lambda rel: (self.root / rel) if rel else None

    def tearDown(self):
        po.vault_abs = self._orig
        self._tmp.cleanup()

    def _write(self, body, suffix="", mtime=None):
        rel = po._stage_review_verdict_rel(STAGE, VIDEO)
        p = self.root / rel
        if suffix:
            p = p.with_name(p.stem + suffix + p.suffix)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
        if mtime is not None:
            os.utime(p, (mtime, mtime))
        return p


class TestStampIsHonoured(_VaultCase):
    def test_clean_close_is_detected(self):
        self._write(GOOD)
        closed, stamp = po._resolution_closed_stage(STAGE, VIDEO)
        self.assertTrue(closed)
        self.assertIn("closed-fix-applied", stamp)

    def test_versioned_pass_file_is_found(self):
        # Humans stamp the OPERATIVE pass (…_v3.md, …_Pass3.md), not always the
        # base filename. Reading only verdict_tmpl made the fix inert in practice.
        self._write("---\nreviewer: x\n---\n\nVERDICT: SHIP\n", mtime=time.time() - 60)
        self._write(GOOD, suffix="_v3")
        self.assertTrue(po._resolution_closed_stage(STAGE, VIDEO)[0])

    def test_single_file_close_is_honoured(self):
        # The shape CLAUDE.md actually documents: ONE review, VERDICT still REVISE,
        # closed in place by stamping its frontmatter. A SHIP-only veto left A-51
        # unfixed for exactly the workflow the convention prescribes.
        self._write("---\nresolution: closed-fix-applied-2026-07-16\n---\n\n"
                    "VERDICT: REVISE\n\n> ✅ RESOLUTION — fix applied, re-reviewed.\n")
        self.assertTrue(po._resolution_closed_stage(STAGE, VIDEO)[0])

    def test_the_real_vault_shape_is_honoured(self):
        # The live V13 convention: the BASE file records the original REVISE and
        # carries the human's stamp; the clean re-reviews land in newer _vN
        # siblings. Nothing is open, and a close exists -> honour it.
        self._write("---\nresolution: closed-fix-applied-2026-07-16\n---\n\n"
                    "VERDICT: REVISE\n\nThis file is the historical REVISE record.\n",
                    mtime=time.time() - 600)
        self._write("---\nreviewer: x\n---\n\nVERDICT: SHIP\n", suffix="_v8_2026-07-16")
        self.assertTrue(po._resolution_closed_stage(STAGE, VIDEO)[0])

    def test_quoted_stamp_value_is_unwrapped(self):
        self._write('---\nresolution: "closed-fix-applied-2026-07-16 — fixed"\n---\n\n'
                    'VERDICT: SHIP\n')
        closed, stamp = po._resolution_closed_stage(STAGE, VIDEO)
        self.assertTrue(closed)
        self.assertTrue(stamp.startswith("closed-fix-applied"))


class TestFalsePromoteGuards(_VaultCase):
    def test_no_review_file(self):
        self.assertEqual(po._resolution_closed_stage(STAGE, VIDEO), (False, ""))

    def test_unstamped_review(self):
        self._write("---\nreviewer: packaging-reviewer\n---\n\nVERDICT: REVISE\n")
        self.assertEqual(po._resolution_closed_stage(STAGE, VIDEO), (False, ""))

    def test_fenced_example_in_the_body_must_not_promote(self):
        # THE C1 REGRESSION. Reviewers quote the convention when telling a human
        # how to close a gate; scanning the whole file promoted a REVISE'd stage.
        self._write("---\nreviewer: packaging-reviewer\n---\n\nVERDICT: REVISE\n\n"
                    "Fix the title, then stamp per the convention:\n\n"
                    "```\nresolution: closed-fix-applied-YYYY-MM-DD\n```\n")
        self.assertEqual(po._resolution_closed_stage(STAGE, VIDEO), (False, ""))

    def test_prose_resolution_line_in_body_must_not_promote(self):
        self._write("---\nreviewer: x\n---\n\nVERDICT: REVISE\n"
                    "resolution: not yet - do not close\n")
        self.assertEqual(po._resolution_closed_stage(STAGE, VIDEO), (False, ""))

    def test_resolution_prior_revise_must_not_impersonate_a_close(self):
        self._write("---\nresolution-prior-revise: 2026-07-19 pass 2\n---\n\nREVISE\n")
        self.assertEqual(po._resolution_closed_stage(STAGE, VIDEO), (False, ""))

    def test_valueless_stamp(self):
        self._write("---\nresolution:\n---\n\nREVISE\n")
        self.assertEqual(po._resolution_closed_stage(STAGE, VIDEO), (False, ""))

    def test_empty_quoted_stamp(self):
        self._write('---\nresolution: ""\n---\n')
        self.assertEqual(po._resolution_closed_stage(STAGE, VIDEO), (False, ""))

    def test_overtaken_by_publish_must_not_promote(self):
        # The video shipped WITHOUT the fix. That records the gate stopped
        # mattering — it is not evidence the artifact passed review.
        self._write("---\nresolution: overtaken-by-publish-2026-07-08\n---\n")
        self.assertEqual(po._resolution_closed_stage(STAGE, VIDEO), (False, ""))

    def test_closed_by_steve_decision_must_not_promote(self):
        self._write("---\nresolution: closed-by-steve-decision-2026-07-08\n---\n")
        self.assertEqual(po._resolution_closed_stage(STAGE, VIDEO), (False, ""))

    def test_no_frontmatter_at_all(self):
        self._write("resolution: closed-fix-applied-2026-07-20\n\nSHIP\n")
        self.assertEqual(po._resolution_closed_stage(STAGE, VIDEO), (False, ""))

    def test_unknown_stage_key(self):
        self.assertEqual(po._resolution_closed_stage("not_a_stage", VIDEO), (False, ""))

    def test_directory_in_place_of_review_file(self):
        rel = po._stage_review_verdict_rel(STAGE, VIDEO)
        (self.root / rel).mkdir(parents=True, exist_ok=True)
        self.assertEqual(po._resolution_closed_stage(STAGE, VIDEO), (False, ""))

    def test_newer_open_revise_beats_an_older_close(self):
        # THE ROUND-2 REGRESSION. The scan used to walk past an unstamped REVISE
        # to any older stamped file, promoting a stage whose operative verdict on
        # disk was REVISE. A newer OPEN verdict must always win.
        self._write(GOOD, mtime=time.time() - 600)
        self._write("---\nreviewer: x\n---\n\nVERDICT: REVISE\n",
                    suffix="_v2_2026-07-22")
        self.assertEqual(po._resolution_closed_stage(STAGE, VIDEO), (False, ""))

    def test_stamp_bump_ordering_must_not_promote(self):
        # THE ROUND-4 REGRESSION, and it is the reviewer agents' own documented
        # behaviour: description-/image-/vo-reviewer.md instruct stamping the OLD
        # verdict file, which bumps that REVISE-bearing file's mtime ABOVE the
        # re-review that superseded it. So the newest candidate is a STAMPED
        # REVISE while a sibling re-review is still open. Present in live data:
        # Video_13_Description_Review.md (stamped REVISE) is 07:38 and its _v2
        # re-review is 07:37 — V13 escapes only because _v3.._v8 landed later.
        self._write("---\nreviewer: x\n---\n\nVERDICT: REVISE\n",
                    suffix="_v2", mtime=time.time() - 120)
        self._write("---\nresolution: closed-fix-applied-2026-07-20\n---\n\n"
                    "VERDICT: REVISE\n", mtime=time.time() - 60)
        self.assertEqual(po._resolution_closed_stage(STAGE, VIDEO), (False, ""))

    def test_stamp_bump_with_a_ship_sibling_also_blocks(self):
        # Safe direction: once a second review file exists, only SHIP-on-the-newest
        # proves nothing is still open. Costs one extra gate run.
        self._write("---\nreviewer: x\n---\n\nVERDICT: SHIP\n",
                    suffix="_v2", mtime=time.time() - 120)
        self._write("---\nresolution: closed-fix-applied-2026-07-20\n---\n\n"
                    "VERDICT: REVISE\n", mtime=time.time() - 60)
        self.assertEqual(po._resolution_closed_stage(STAGE, VIDEO), (False, ""))

    def test_unparseable_newest_review_blocks(self):
        # No verdict token at all -> None -> not SHIP -> block.
        self._write(GOOD, mtime=time.time() - 600)
        self._write("---\nreviewer: x\n---\n\nsome notes, no verdict line\n",
                    suffix="_v2")
        self.assertEqual(po._resolution_closed_stage(STAGE, VIDEO), (False, ""))

    def test_a_different_review_sharing_the_video_number_is_not_matched(self):
        # Video_Descriptions/_REVIEW/ really does hold both
        # Video_01_Description_Review.md and Video_01_Search_Rewrite_Review.md.
        rel = po._stage_review_verdict_rel(STAGE, VIDEO)
        other = (self.root / rel).parent / f"Video_{po.nn(VIDEO)}_Search_Rewrite_Review.md"
        other.parent.mkdir(parents=True, exist_ok=True)
        other.write_text(GOOD, encoding="utf-8")
        self.assertEqual(po._resolution_closed_stage(STAGE, VIDEO), (False, ""))


class TestFreshness(_VaultCase):
    """A stamp is evidence about the artifact that existed WHEN it was written."""

    def test_stamp_older_than_deps_is_ignored(self):
        old = time.time() - 86400
        self._write(GOOD, mtime=old)
        self.assertEqual(po._resolution_closed_stage(STAGE, VIDEO, newer_than=time.time()),
                         (False, ""))

    def test_stamp_newer_than_deps_is_honoured(self):
        self._write(GOOD)
        self.assertTrue(
            po._resolution_closed_stage(STAGE, VIDEO, newer_than=time.time() - 86400)[0])

    def test_none_threshold_means_no_freshness_constraint(self):
        self._write(GOOD, mtime=time.time() - 86400)
        self.assertTrue(po._resolution_closed_stage(STAGE, VIDEO, newer_than=None)[0])


class TestGateHonoursTheClose(unittest.TestCase):
    """End-to-end through run_stage_review_gate. The producer/reviewer dispatchers
    are stubbed (they shell out to claude), but _resolution_closed_stage,
    deps_freshness_threshold and the Step-0 branch all run for real."""

    def setUp(self):
        self.dispatched = []
        self._d, self._r = po._dispatch_stage_agent, po.run_stage_review_loop
        po._dispatch_stage_agent = lambda k, v: (
            self.dispatched.append(("producer", k)), (True, "ran"))[1]
        po.run_stage_review_loop = lambda k, v: (
            self.dispatched.append(("reviewer", k)), ("SHIP", "rel", "d"))[1]
        self._art = po._producer_artifact_ok
        po._producer_artifact_ok = lambda k, v: (True, "")
        self._closed = po._resolution_closed_stage
        self._tmp = tempfile.TemporaryDirectory()
        self._vault = po.vault_abs
        po.vault_abs = lambda rel: (Path(self._tmp.name) / rel) if rel else None

    def tearDown(self):
        po._dispatch_stage_agent, po.run_stage_review_loop = self._d, self._r
        po._producer_artifact_ok = self._art
        po._resolution_closed_stage = self._closed
        po.vault_abs = self._vault
        self._tmp.cleanup()

    def _stages_all_deps_done(self):
        stages = po.default_stages()
        for k in stages:
            stages[k]["status"] = "done"
            stages[k]["completed_at"] = po.now_iso()
        return stages

    def test_close_advances_without_dispatching_anything(self):
        po._resolution_closed_stage = lambda k, v, newer_than=None: (True, "closed-fix-applied-x")
        ok, msg = po.run_stage_review_gate(STAGE, VIDEO, self._stages_all_deps_done())
        self.assertTrue(ok)
        self.assertIn("resolution-closed", msg)
        self.assertEqual(self.dispatched, [], "a resolution close must re-dispatch NOTHING")

    def test_missing_artifact_falls_through_to_the_real_gate(self):
        po._resolution_closed_stage = lambda k, v, newer_than=None: (True, "closed-fix-applied-x")
        po._producer_artifact_ok = lambda k, v: (False, "declared artifact missing")
        ok, msg = po.run_stage_review_gate(STAGE, VIDEO, self._stages_all_deps_done())
        self.assertNotIn("resolution-closed", msg)
        # The load-bearing assertion: BOTH subprocesses ran, i.e. the real gate.
        self.assertEqual(self.dispatched, [("producer", STAGE), ("reviewer", STAGE)])

    def test_no_stages_means_no_promote(self):
        # A direct call with no stages has no freshness proof, so Step 0 is skipped
        # even when a close exists.
        po._resolution_closed_stage = lambda k, v, newer_than=None: (True, "closed-fix-applied-x")
        ok, msg = po.run_stage_review_gate(STAGE, VIDEO, None)
        self.assertNotIn("resolution-closed", msg)
        self.assertEqual(self.dispatched, [("producer", STAGE), ("reviewer", STAGE)])

    def test_incomplete_deps_block_the_promote(self):
        stages = self._stages_all_deps_done()
        for k, v in stages.items():
            if k != STAGE:
                v["completed_at"] = None      # upstream not really done
        po._resolution_closed_stage = lambda k, v, newer_than=None: (True, "closed-fix-applied-x")
        ok, msg = po.run_stage_review_gate(STAGE, VIDEO, stages)
        self.assertNotIn("resolution-closed", msg)
        self.assertEqual(self.dispatched, [("producer", STAGE), ("reviewer", STAGE)])

    def test_unclosed_stage_runs_the_full_gate_as_before(self):
        ok, msg = po.run_stage_review_gate(STAGE, VIDEO, self._stages_all_deps_done())
        self.assertTrue(ok)
        self.assertNotIn("resolution-closed", msg)
        self.assertEqual(self.dispatched, [("producer", STAGE), ("reviewer", STAGE)])


if __name__ == "__main__":
    unittest.main()
