#!/usr/bin/env python3
"""Regression tests for the pipeline orchestrator's safety-critical core.

Stdlib unittest only (matches the orchestrator's stdlib-only constraint). Run:
    python3 tests/test_pipeline_orchestrator.py
or under the suite:
    python3 -m unittest discover -s tests -v

These tests lock in the ONE invariant that matters most — the no-autonomous-
spend / no-autonomous-human-action guarantee in select_next — plus the
deterministic status/promotion/orphan logic. They are pure-function tests:
nothing here touches the real vault, the lock dir, Telegram, or subprocess.
The module is imported from its on-disk path via importlib (no package layout
assumed), and STATE_DIR is monkeypatched onto a tmpdir only for discover_videos.
"""

import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

# --- Import the orchestrator module from its path (no package assumption) ----
_HERE = Path(__file__).resolve().parent
_MODULE_PATH = _HERE.parent / "scripts" / "pipeline_orchestrator.py"
_spec = importlib.util.spec_from_file_location("pipeline_orchestrator", _MODULE_PATH)
po = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(po)


def _stages(**overrides):
    """A fresh default stage map with optional per-stage field overrides.

    overrides: {stage_key: {field: value, ...}} merged onto default_stages().
    """
    stages = po.default_stages()
    for key, patch in overrides.items():
        stages[key].update(patch)
    return stages


def _mark_done(stages, *keys, completed_at=None):
    for k in keys:
        stages[k]["status"] = "done"
        stages[k]["completed_at"] = completed_at or po.now_iso()
    return stages


class TestSelectNextInvariant(unittest.TestCase):
    """select_next is the structural no-auto-action core: it must ONLY ever
    return a ready, non-gate, owner==orchestrator, in-RUN_TABLE, non-billed
    stage. Every test here defends that contract from a different angle."""

    def test_fresh_fleet_first_runnable_is_script(self):
        # Nothing done yet → the only ready non-gate stage is 1_script.
        self.assertEqual(po.select_next(_stages()), "1_script")

    def test_never_returns_a_gate_stage(self):
        # Make EVERY dep done so all gates are "ready" by deps — select_next must
        # still skip every gate/steve-owned stage and pick only orchestrator work.
        stages = _mark_done(_stages(), *po.STAGE_ORDER[:-1])
        # With all-but-last done, the last orchestrator stage (11_analyze) is the
        # only thing left; everything returned must be non-gate + orchestrator.
        nxt = po.select_next(stages)
        if nxt is not None:
            self.assertFalse(stages[nxt].get("gate"), f"{nxt} is a gate")
            self.assertEqual(stages[nxt].get("owner"), "orchestrator")
            self.assertIn(nxt, po.RUN_TABLE)
            self.assertNotEqual(po.RUN_TABLE[nxt]["kind"], "billed")

    def test_billed_stage_never_auto_selected_even_when_ready(self):
        # 5_images deps = [2_review]. Mark review done so 5_images is deps-ready.
        stages = _mark_done(_stages(), "1_script", "2_review")
        nxt = po.select_next(stages)
        self.assertNotEqual(nxt, "5_images",
                            "billed image stage must NEVER be auto-selected")

    def test_billed_excluded_by_kind_even_if_gate_flag_flipped(self):
        # Adversarial: corrupt the state so 5_images looks like orchestrator work
        # with gate=False. The kind=="billed" guard must STILL exclude it — the
        # billed check is independent of the gate/owner flags.
        stages = _mark_done(_stages(), "1_script", "2_review")
        stages["5_images"]["gate"] = False
        stages["5_images"]["owner"] = "orchestrator"
        nxt = po.select_next(stages)
        self.assertNotEqual(nxt, "5_images",
                            "kind==billed must exclude 5_images regardless of gate/owner")
        # It falls through to the next genuine orchestrator stage instead.
        self.assertEqual(nxt, "7_packaging")

    def test_human_gate_excluded_even_if_in_run_table_absent(self):
        # 3_vo_expand is a steve gate not in RUN_TABLE; deps-ready must not select.
        stages = _mark_done(_stages(), "1_script", "2_review")
        nxt = po.select_next(stages)
        self.assertNotEqual(nxt, "3_vo_expand")
        self.assertNotEqual(nxt, "4_vo_record")

    def test_returns_none_when_only_gates_remain(self):
        # Mark all orchestrator stages whose deps are satisfiable done; what's left
        # is purely gates → select_next returns None (work parked on Steve).
        stages = _mark_done(_stages(), "1_script", "2_review", "7_packaging",
                            "9_description")
        nxt = po.select_next(stages)
        # remaining ready things should all be gates (None or a gate is acceptable,
        # but a gate must never be *returned* — so it must be None here).
        self.assertIsNone(nxt)

    def test_running_stage_is_not_reselected(self):
        # A stage mid-flight (status=running) is not "ready" → not selected.
        stages = _stages()
        stages["1_script"]["status"] = "running"
        self.assertIsNone(po.select_next(stages),
                          "a running stage must not be re-selected")


class TestEffectiveStatus(unittest.TestCase):
    def test_done_is_terminal(self):
        stages = _stages()
        stages["1_script"]["status"] = "done"
        self.assertEqual(po.effective_status("1_script", stages), "done")

    def test_ready_when_deps_done(self):
        stages = _mark_done(_stages(), "1_script")
        # 2_review deps=[1_script] now done → ready
        self.assertEqual(po.effective_status("2_review", stages), "ready")

    def test_blocked_when_deps_unmet(self):
        stages = _stages()
        self.assertEqual(po.effective_status("2_review", stages), "blocked")

    def test_running_and_needs_steve_preserved(self):
        stages = _stages()
        stages["1_script"]["status"] = "running"
        stages["5_images"]["status"] = "needs-steve"
        self.assertEqual(po.effective_status("1_script", stages), "running")
        self.assertEqual(po.effective_status("5_images", stages), "needs-steve")

    def test_first_stage_with_no_deps_is_ready(self):
        self.assertEqual(po.effective_status("1_script", _stages()), "ready")


class TestPromoteGateExits(unittest.TestCase):
    def test_no_artifact_convention_never_promotes(self):
        # 3_vo_expand has no GATE_ARTIFACTS entry → stays needs-steve.
        stages = _stages()
        stages["3_vo_expand"]["status"] = "needs-steve"
        stages["3_vo_expand"]["park_reason"] = "gate"
        promoted = po.promote_gate_exits(stages, 1)
        self.assertEqual(promoted, [])
        self.assertEqual(stages["3_vo_expand"]["status"], "needs-steve")

    def test_failed_parked_never_promotes(self):
        # park_reason != "gate" (e.g. a failure) must never auto-promote, even if
        # it were an artifact gate.
        stages = _stages()
        stages["4_vo_record"]["status"] = "needs-steve"
        stages["4_vo_record"]["park_reason"] = "failed"
        promoted = po.promote_gate_exits(stages, 1)
        self.assertEqual(promoted, [])
        self.assertEqual(stages["4_vo_record"]["status"], "needs-steve")

    def test_billed_gate_never_promoted_here(self):
        stages = _stages()
        stages["5_images"]["status"] = "needs-steve"
        stages["5_images"]["park_reason"] = "gate"
        promoted = po.promote_gate_exits(stages, 1)
        self.assertNotIn("5_images", promoted)
        self.assertEqual(stages["5_images"]["status"], "needs-steve")

    def test_artifact_gate_promotes_when_file_present_and_fresh(self):
        # 4_vo_record IS an artifact gate (dir of .mp3). Build a tmp vault with a
        # non-empty mp3 fresher than the dep, monkeypatch WORKSPACE_DIR, and assert
        # promotion. Then restore.
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            rel = po.GATE_ARTIFACTS["4_vo_record"]["path_tmpl"].replace("NN", po.nn(1))
            vo_dir = tmp / rel
            vo_dir.mkdir(parents=True, exist_ok=True)
            (vo_dir / "take1.mp3").write_bytes(b"audio-bytes")
            old_ws = po.WORKSPACE_DIR
            try:
                po.WORKSPACE_DIR = tmp
                stages = _stages()
                # dep 3_vo_expand done well in the past so the artifact is "fresh"
                stages["3_vo_expand"]["status"] = "done"
                stages["3_vo_expand"]["completed_at"] = "2020-01-01T00:00:00Z"
                stages["4_vo_record"]["status"] = "needs-steve"
                stages["4_vo_record"]["park_reason"] = "gate"
                promoted = po.promote_gate_exits(stages, 1)
                self.assertIn("4_vo_record", promoted)
                self.assertEqual(stages["4_vo_record"]["status"], "done")
            finally:
                po.WORKSPACE_DIR = old_ws


class TestReconcileOrphans(unittest.TestCase):
    def test_dead_pid_running_resets_to_ready(self):
        stages = _stages()
        stages["1_script"]["status"] = "running"
        stages["1_script"]["pid"] = 999999999  # almost certainly not a live pid
        stages["1_script"]["pid_start_token"] = "Thu Jan  1 00:00:00 2020"
        stages["1_script"]["started_at"] = po.now_iso()
        reset = po.reconcile_orphans(stages, 1)
        self.assertIn("1_script", reset)
        self.assertEqual(stages["1_script"]["status"], "ready")
        self.assertIsNone(stages["1_script"]["pid"])

    def test_genuinely_live_run_raises_liverunerror(self):
        # Our OWN pid is alive and the start-token matches → genuinely live.
        # reconcile_orphans now raises LiveRunError (NOT die()/SystemExit) so the
        # fleet loop can tell a benign in-flight run apart from a corrupt state
        # file. The per-video CLI wrapper converts it back to die() — see below.
        stages = _stages()
        my_pid = os.getpid()
        stages["1_script"]["status"] = "running"
        stages["1_script"]["pid"] = my_pid
        stages["1_script"]["pid_start_token"] = po._proc_start_token(my_pid)
        # started_at = now so it isn't timed-out.
        stages["1_script"]["started_at"] = po.now_iso()
        with self.assertRaises(po.LiveRunError):
            po.reconcile_orphans(stages, 1)

    def test_cli_advance_still_surfaces_live_run_as_exit_1(self):
        # The CLI contract is preserved: cmd_advance catches LiveRunError and
        # die()s (SystemExit, exit 1) so a genuinely-live run is a hard CLI error,
        # exactly as before the fleet refactor. advance_once is stubbed to raise.
        old = po.advance_once
        try:
            def _raise(_sf):
                raise po.LiveRunError("stage 1_script already running")
            po.advance_once = _raise
            with self.assertRaises(SystemExit) as cm:
                po.cmd_advance(object())  # sf unused before advance_once raises
            self.assertEqual(cm.exception.code, 1)
        finally:
            po.advance_once = old

    def test_cli_spend_ok_surfaces_live_run_as_exit_1(self):
        # Symmetry with cmd_advance on the BILLED path: a genuinely-live run is
        # converted to die() (exit 1) by the guard at the TOP of cmd_spend_ok,
        # before any _mark_running / save / generate_images spend. Stub
        # reconcile_orphans to raise so we never touch real state or money.
        class _SF:
            data = {"stages": po.default_stages(), "video": 1}
        old = po.reconcile_orphans
        try:
            def _raise(_stages, _video):
                raise po.LiveRunError("stage 5_images already running")
            po.reconcile_orphans = _raise
            with self.assertRaises(SystemExit) as cm:
                po.cmd_spend_ok(_SF())
            self.assertEqual(cm.exception.code, 1)
        finally:
            po.reconcile_orphans = old

    def test_timed_out_running_resets_even_if_pid_alive(self):
        stages = _stages()
        my_pid = os.getpid()
        stages["1_script"]["status"] = "running"
        stages["1_script"]["pid"] = my_pid
        stages["1_script"]["pid_start_token"] = po._proc_start_token(my_pid)
        # started_at far in the past → exceeds the 1_script timeout (1200s).
        stages["1_script"]["started_at"] = "2020-01-01T00:00:00Z"
        reset = po.reconcile_orphans(stages, 1)
        self.assertIn("1_script", reset)
        self.assertEqual(stages["1_script"]["status"], "ready")


class TestLooksOrphanedReadOnly(unittest.TestCase):
    """_looks_orphaned must mirror reconcile's verdict WITHOUT mutating."""

    def test_dead_pid_looks_orphaned_no_mutation(self):
        s = po._stage("orchestrator", False, [])
        s["status"] = "running"
        s["pid"] = 999999999
        s["pid_start_token"] = "Thu Jan  1 00:00:00 2020"
        s["started_at"] = po.now_iso()
        before = json.dumps(s, sort_keys=True)
        self.assertTrue(po._looks_orphaned(s, "1_script"))
        self.assertEqual(json.dumps(s, sort_keys=True), before,
                         "_looks_orphaned must not mutate the stage")

    def test_live_run_not_orphaned(self):
        s = po._stage("orchestrator", False, [])
        s["status"] = "running"
        s["pid"] = os.getpid()
        s["pid_start_token"] = po._proc_start_token(os.getpid())
        s["started_at"] = po.now_iso()
        self.assertFalse(po._looks_orphaned(s, "1_script"))


class TestSmallHelpers(unittest.TestCase):
    def test_nn_zero_pads(self):
        self.assertEqual(po.nn(1), "01")
        self.assertEqual(po.nn(9), "09")
        self.assertEqual(po.nn(12), "12")

    def test_parse_iso_roundtrip_and_none(self):
        self.assertIsNone(po.parse_iso(None))
        self.assertIsNone(po.parse_iso(""))
        self.assertIsNone(po.parse_iso("not-a-date"))
        t = po.parse_iso("2026-06-18T00:00:00Z")
        self.assertIsInstance(t, float)
        self.assertGreater(t, 0)

    def test_discover_videos_sorted_and_filtered(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            for name in ("Video_03_pipeline.json", "Video_01_pipeline.json",
                         "Video_10_pipeline.json", "Video_02_pipeline.json.tmp",
                         "not_a_video.json", "Video_AB_pipeline.json"):
                (tmp / name).write_text("{}")
            old = po.STATE_DIR
            try:
                po.STATE_DIR = tmp
                self.assertEqual(po.discover_videos(), [1, 3, 10])
            finally:
                po.STATE_DIR = old

    def test_discover_videos_missing_dir_is_empty(self):
        old = po.STATE_DIR
        try:
            po.STATE_DIR = Path("/nonexistent/path/that/should/not/exist/xyz")
            self.assertEqual(po.discover_videos(), [])
        finally:
            po.STATE_DIR = old


class TestQuietIdleNewsMapping(unittest.TestCase):
    """The --quiet-idle silence-break predicate. The whole point: a clean
    gate-parked / idle hourly tick stays silent, but anything Steve must hear
    about — a stage running, a stage failing, OR an infra skip that wedges the
    host — breaks silence. The infra_skip case is the skeptical-code-reviewer
    HIGH fix: a stale `claude login` must never hide behind --quiet-idle."""

    def test_news_outcomes_break_silence(self):
        for outcome in ("ran_done", "ran_failed", "infra_skip"):
            self.assertTrue(po._outcome_is_news(outcome),
                            f"{outcome} must break --quiet-idle silence")

    def test_steady_state_outcomes_stay_silent(self):
        for outcome in ("parked_gate", "idle"):
            self.assertFalse(po._outcome_is_news(outcome),
                             f"{outcome} should stay silent under --quiet-idle")

    def test_infra_skip_is_explicitly_news(self):
        # Pin the HIGH fix directly so a future refactor can't silently drop it.
        self.assertIn("infra_skip", po.NEWS_OUTCOMES)


class _FakeRes:
    """Stand-in for AdvanceResult with only the field cmd_advance_all reads."""
    def __init__(self, outcome, stage=None):
        self.outcome = outcome
        self.stage = stage


class TestAdvanceAllQuietIdle(unittest.TestCase):
    """End-to-end: drive cmd_advance_all over a tmp STATE_DIR with advance_once
    stubbed, and assert the digest is suppressed ONLY on a silent (idle) tick
    under --quiet-idle, and always sent otherwise."""

    def _seed(self, state_dir, video=1):
        data = {"video": video, "title": "T", "stages": po.default_stages(),
                "created_at": po.now_iso(), "updated_at": po.now_iso()}
        (state_dir / f"Video_{po.nn(video)}_pipeline.json").write_text(
            json.dumps(data, indent=2))

    def _run(self, outcome, quiet_idle, stage="1_script"):
        sent = []
        # The spy mirrors the real notify gate (drop force=False lines while
        # SUPPRESS_NOTIFY is set) so a stray per-stage notify leaking during the
        # fleet drain would be caught, not silently swallowed by the stub.
        def _spy(msg, force=False):
            if force or not po.SUPPRESS_NOTIFY:
                sent.append(msg)
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td) / "state"
            ld = Path(td) / "locks"
            sd.mkdir()
            self._seed(sd)
            old = (po.STATE_DIR, po.LOCK_DIR, po.advance_once, po.notify)
            try:
                po.STATE_DIR = sd
                po.LOCK_DIR = ld
                po.advance_once = lambda sf: _FakeRes(outcome, stage)
                po.notify = _spy
                rc = po.cmd_advance_all(quiet_idle=quiet_idle)
            finally:
                (po.STATE_DIR, po.LOCK_DIR, po.advance_once, po.notify) = old
        return rc, sent

    def test_idle_tick_suppressed_under_quiet_idle(self):
        rc, sent = self._run("idle", quiet_idle=True)
        self.assertEqual(rc, 0)
        self.assertEqual(sent, [], "idle + --quiet-idle must send NO digest")

    def test_idle_tick_sends_when_not_quiet(self):
        rc, sent = self._run("idle", quiet_idle=False)
        self.assertEqual(rc, 0)
        self.assertEqual(len(sent), 1, "without --quiet-idle the digest always sends")

    def test_infra_skip_breaks_silence_even_under_quiet_idle(self):
        # The HIGH fix, end-to-end: a wedged host must still ping Steve.
        rc, sent = self._run("infra_skip", quiet_idle=True)
        self.assertEqual(rc, 0)
        self.assertEqual(len(sent), 1,
                         "infra_skip must send the digest even under --quiet-idle")

    def test_ran_done_breaks_silence_under_quiet_idle(self):
        # Pass a real stage key so this drives the genuine ran_done path
        # (ran.append(stage) → _video_summary_line joins it), not the generic
        # exception fallback that a stage=None would trip.
        rc, sent = self._run("ran_done", quiet_idle=True, stage="1_script")
        self.assertEqual(len(sent), 1)


class TestStatusReadOnly(unittest.TestCase):
    """--status must never mutate or save state (skeptical-code-reviewer ask).
    Drive cmd_status against a real on-disk state file with an orphaned running
    stage and assert the bytes on disk are unchanged."""

    def test_status_does_not_mutate_disk(self):
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td) / "state"
            ld = Path(td) / "locks"
            sd.mkdir()
            stages = po.default_stages()
            # An orphaned running stage — the old cmd_status would reconcile+save.
            stages["1_script"]["status"] = "running"
            stages["1_script"]["pid"] = 999999999
            stages["1_script"]["pid_start_token"] = "Thu Jan  1 00:00:00 2020"
            stages["1_script"]["started_at"] = "2020-01-01T00:00:00Z"
            data = {"video": 1, "title": "T", "stages": stages,
                    "created_at": po.now_iso(), "updated_at": po.now_iso()}
            path = sd / "Video_01_pipeline.json"
            path.write_text(json.dumps(data, indent=2))
            before = path.read_bytes()
            old = (po.STATE_DIR, po.LOCK_DIR)
            try:
                po.STATE_DIR = sd
                po.LOCK_DIR = ld
                sf = po.StateFile(1)
                with sf:
                    sf.load()
                    rc = po.cmd_status(sf)
            finally:
                (po.STATE_DIR, po.LOCK_DIR) = old
            self.assertEqual(rc, 0)
            self.assertEqual(path.read_bytes(), before,
                             "--status must not write to the state file")


if __name__ == "__main__":
    unittest.main(verbosity=2)
