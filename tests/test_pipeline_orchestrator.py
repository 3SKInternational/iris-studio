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
import shutil
import subprocess
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
# NOTE: the module-level `mut` (scripts/mutate.py) import that used to live here
# was dropped (round 16, 2026-07-27) — TestMutationCrashGuard now runs entirely
# against a private tmp copy (see its docstring), so nothing in this file reads
# the production mutate.py module directly any more.


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
        # 12_leadmagnet (added 2026-07-06, deps 2_review+7_packaging — both marked
        # done here) is one such satisfiable orchestrator stage; omitting it left it
        # legitimately ready, so select_next correctly returned it (not a gate bug).
        stages = _mark_done(_stages(), "1_script", "2_review", "7_packaging",
                            "9_description", "12_leadmagnet")
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
        promoted, _ = po.promote_gate_exits(stages, 1)
        self.assertEqual(promoted, [])
        self.assertEqual(stages["3_vo_expand"]["status"], "needs-steve")

    def test_failed_parked_never_promotes(self):
        # park_reason != "gate" (e.g. a failure) must never auto-promote, even if
        # it were an artifact gate.
        stages = _stages()
        stages["4_vo_record"]["status"] = "needs-steve"
        stages["4_vo_record"]["park_reason"] = "failed"
        promoted, _ = po.promote_gate_exits(stages, 1)
        self.assertEqual(promoted, [])
        self.assertEqual(stages["4_vo_record"]["status"], "needs-steve")

    def test_billed_gate_never_promoted_here(self):
        stages = _stages()
        stages["5_images"]["status"] = "needs-steve"
        stages["5_images"]["park_reason"] = "gate"
        promoted, _ = po.promote_gate_exits(stages, 1)
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
                promoted, _ = po.promote_gate_exits(stages, 1)
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
            def _raise(_sf, force=False):
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


class TestInfraFailureClassification(unittest.TestCase):
    """Round-2 HIGH fix: INFRA_FAILURE_MARKERS were too broad — substrings like
    'no such file or directory' / 'permission denied' / 'operation not permitted'
    also matched GENUINE task failures, so a real failure retried forever and
    never parked. These lock the markers down so that regression can't sneak back."""

    GENUINE_TASK_FAILURES = (
        "Error: No such file or directory: '/task/input.md'",
        "PermissionError: [Errno 1] Operation not permitted",
        "Permission denied (publickey).",
        "EPERM: operation not permitted, open '/x'",
        "Traceback ... FileNotFoundError: [Errno 2] No such file or directory",
    )
    REAL_INFRA = (
        "claude: error reading ~/.claude/session-env (EPERM)",
        "fork: Resource temporarily unavailable",
        "OSError: [Errno 12] Cannot allocate memory",
        "bash: claude: command not found",
        "OSError: [Errno 24] Too many open files",
    )

    def test_genuine_task_failures_are_not_infra(self):
        for msg in self.GENUINE_TASK_FAILURES:
            self.assertFalse(po._is_infra_failure(msg),
                             f"must NOT be infra (genuine task failure): {msg!r}")

    def test_real_host_failures_are_infra(self):
        for msg in self.REAL_INFRA:
            self.assertTrue(po._is_infra_failure(msg),
                            f"must be infra (host/toolchain): {msg!r}")


class TestOnFailureRetryBounding(unittest.TestCase):
    """Round-2 HIGH fix: infra skips must be bounded by MAX_INFRA (so a host
    outage that never clears parks+surfaces instead of looping invisibly), while
    a genuine task failure still parks as 'failed' at MAX_FAILS and never touches
    infra_count."""

    def test_infra_parks_at_max_infra(self):
        s = po._stage("orchestrator", False, [])
        for i in range(1, po.MAX_INFRA + 1):
            po._on_failure(s, "broken session-env")
            if i < po.MAX_INFRA:
                self.assertEqual(s["status"], "ready")
                self.assertIsNone(s.get("park_reason"))
        self.assertEqual(s["status"], "needs-steve")
        self.assertEqual(s["park_reason"], "infra")
        self.assertEqual(s["infra_count"], po.MAX_INFRA)
        self.assertEqual(s["fail_count"], 0, "infra must never burn a task-failure retry")

    def test_genuine_failure_parks_at_max_fails_without_touching_infra(self):
        s = po._stage("orchestrator", False, [])
        for _ in range(po.MAX_FAILS):
            po._on_failure(s, "agent task returned No such file or directory: '/x'")
        self.assertEqual(s["status"], "needs-steve")
        self.assertEqual(s["park_reason"], "failed")
        self.assertEqual(s["fail_count"], po.MAX_FAILS)
        self.assertEqual(s["infra_count"], 0)

    def test_success_resets_both_counters(self):
        s = po._stage("orchestrator", False, [])
        s["fail_count"], s["infra_count"] = 2, 3
        po._on_success(s, "1_script", 1, "done")
        self.assertEqual(s["fail_count"], 0)
        self.assertEqual(s["infra_count"], 0)


class TestStalenessFromLastProgress(unittest.TestCase):
    """Round-2 HIGH fix: staleness is measured from last_progress_at (forward
    progress only), not updated_at (which bumps on every save incl. infra-skip
    and gate-park). So an infra-retry loop still goes stale, and a pre-existing
    state file lacking the field still classifies via the fallback."""

    def _line(self, **top):
        data = {"video": 1, "title": "T", "stages": po.default_stages()}
        data.update(top)
        return po._video_summary_line(data)[1]

    def test_old_progress_fresh_update_is_stale(self):
        import datetime as _dt
        old_iso = (_dt.datetime.now(_dt.timezone.utc)
                   - _dt.timedelta(days=po.STALE_DAYS + 2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        line = self._line(last_progress_at=old_iso, updated_at=po.now_iso(),
                          created_at=old_iso)
        self.assertIn("no progress", line,
                      "stale must be measured from last_progress_at, not updated_at")

    def test_fresh_progress_not_stale(self):
        line = self._line(last_progress_at=po.now_iso(), updated_at=po.now_iso(),
                          created_at=po.now_iso())
        self.assertNotIn("no progress", line)

    def test_missing_field_falls_back_without_crashing(self):
        # A pre-existing file (no last_progress_at, no per-stage infra_count) must
        # classify cleanly via the updated_at→created_at fallback.
        line = self._line(updated_at=po.now_iso(), created_at=po.now_iso())
        self.assertIsInstance(line, str)
        self.assertIn("V1", line)


class _Proc:
    """Minimal stand-in for subprocess.CompletedProcess."""
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _FakeSF:
    def __init__(self, stages, video=3):
        self.data = {"stages": stages, "video": video}
        self.saved = 0

    def save(self):
        self.saved += 1


class TestImageVerdictParse(unittest.TestCase):
    """_parse_image_verdict: the FILE → verdict extraction the gates rely on."""

    def test_each_verdict_token(self):
        for tok in ("SHIP", "SHIP WITH FIXES", "HOLD-SPEND", "REVISE"):
            self.assertEqual(po._parse_image_verdict(f"VERDICT: {tok}\n"), tok)

    def test_case_and_decoration_insensitive(self):
        self.assertEqual(po._parse_image_verdict("**VERDICT:** ship with fixes"),
                         "SHIP WITH FIXES")
        self.assertEqual(po._parse_image_verdict("Verdict - Hold-Spend"), "HOLD-SPEND")

    def test_most_severe_wins_when_multiple_present(self):
        # A file that quotes both a SHIP example and the real HOLD-SPEND verdict
        # must resolve to the blocking one.
        txt = "Example verdict line: SHIP.\n\nVERDICT: HOLD-SPEND — fix prompts.\n"
        self.assertEqual(po._parse_image_verdict(txt), "HOLD-SPEND")

    def test_rubric_echo_on_verdict_line_resolves_to_most_blocking(self):
        # The money-leak the parser MUST defend: a single VERDICT line that echoes
        # all four rubric choices must NOT parse as bare SHIP (leftmost match) —
        # it must fail safe to the most-blocking token present.
        self.assertEqual(
            po._parse_image_verdict("VERDICT: SHIP / SHIP WITH FIXES / HOLD-SPEND / REVISE"),
            "HOLD-SPEND")

    def test_two_tokens_after_one_verdict_label_picks_blocking(self):
        self.assertEqual(
            po._parse_image_verdict("VERDICT: SHIP, then on reflection HOLD-SPEND"),
            "HOLD-SPEND")
        self.assertEqual(
            po._parse_image_verdict("VERDICT: SHIP then REVISE"), "REVISE")

    def test_ship_with_fixes_not_downgraded_to_ship(self):
        # The long phrase must be tokenized whole, never as a bare SHIP.
        self.assertEqual(po._parse_image_verdict("VERDICT: SHIP WITH FIXES"),
                         "SHIP WITH FIXES")

    def test_token_only_counts_on_a_verdict_line(self):
        # A token buried in prose/shot findings with no 'verdict' on the line is
        # ignored, so a per-shot "REVISE this crop" note can't flip a SHIP file.
        txt = ("Shot 03 finding: REVISE this crop later.\n"
               "Overall VERDICT: SHIP\n")
        self.assertEqual(po._parse_image_verdict(txt), "SHIP")

    def test_no_verdict_line_is_none(self):
        self.assertIsNone(po._parse_image_verdict("no verdict here"))
        self.assertIsNone(po._parse_image_verdict(""))
        # 'verdict' present but no recognizable token → None (→ UNAVAILABLE path).
        self.assertIsNone(po._parse_image_verdict("VERDICT: pending review"))


class TestRunImageReviewMissingAgent(unittest.TestCase):
    def test_missing_agent_file_is_unavailable_not_crash(self):
        old = po.AGENTS_DIR
        try:
            with tempfile.TemporaryDirectory() as d:
                po.AGENTS_DIR = Path(d)  # no image-reviewer.md inside
                verdict, vrel, detail = po.run_image_review("prompts", 3)
            self.assertEqual(verdict, "UNAVAILABLE")
            self.assertIn("Video_03_Image_Review.md", vrel)
            self.assertIn("not found", detail)
        finally:
            po.AGENTS_DIR = old


class TestSpendOkImageGate(unittest.TestCase):
    """Pass A: the pre-spend PROMPTS gate inside cmd_spend_ok."""

    def _ready_stages(self):
        # 5_images deps == ['2_review']; mark it done so deps_done is True.
        return _mark_done(_stages(), "2_review")

    def _patch_common(self):
        """Neutralize side effects unrelated to the gate; return restore fn.

        Pass A now enters the gate through run_prompt_review_loop (the closed
        review→fix→re-review loop), so that — not run_image_review — is what the
        gate tests stub. Leaving the loop real would fire the prompt-fixer
        subprocess on a HOLD-SPEND verdict; the loop's own behaviour is covered
        by TestPromptReviewLoop instead."""
        saved = (po.reconcile_orphans, po.vault_abs, po.notify,
                 po.subprocess.run, po.run_prompt_review_loop,
                 po._run_manifest_spine_lint)

        def _noop_reconcile(_stages, _video):
            return None

        # manifest_abs must .exists() → return an obviously-present path.
        existing = _MODULE_PATH  # this test file's sibling: a real file

        def _fake_vault_abs(rel):
            return existing

        po.reconcile_orphans = _noop_reconcile
        po.vault_abs = _fake_vault_abs
        po.notify = lambda *a, **k: None
        # Neutralize the A-46 spine lint (fail-open) — the manifest here is a .py
        # source file, which the real lint would structurally FAIL and block on,
        # making every review-gate assertion below vacuous. The lint's own wire
        # behaviour is covered by TestSpendOkSpineLintGuard.
        po._run_manifest_spine_lint = lambda _v, _m: None

        def restore():
            (po.reconcile_orphans, po.vault_abs, po.notify,
             po.subprocess.run, po.run_prompt_review_loop,
             po._run_manifest_spine_lint) = saved
        return restore

    def test_hold_spend_refuses_spend(self):
        restore = self._patch_common()
        try:
            called = {"gen": False}

            def _spy_run(*a, **k):
                called["gen"] = True
                return _Proc(0)
            po.subprocess.run = _spy_run
            po.run_prompt_review_loop = lambda video, manifest_rel: (
                "HOLD-SPEND", "rel", "off-model")
            sf = _FakeSF(self._ready_stages())
            rc = po.cmd_spend_ok(sf)
            self.assertEqual(rc, 2)
            self.assertFalse(called["gen"], "must NOT shell generate_images on HOLD-SPEND")
        finally:
            restore()

    def test_revise_refuses_spend(self):
        # The allow-list guard: a REVISE (or any non-SHIP, non-UNAVAILABLE) verdict
        # reaching Pass A must fail CLOSED. A deny-list `else: spend` leaked REVISE
        # straight to a billed spend — this pins that regression shut.
        restore = self._patch_common()
        try:
            called = {"gen": False}

            def _spy_run(*a, **k):
                called["gen"] = True
                return _Proc(0)
            po.subprocess.run = _spy_run
            po.run_prompt_review_loop = lambda video, manifest_rel: (
                "REVISE", "rel", "off-model render")
            sf = _FakeSF(self._ready_stages())
            rc = po.cmd_spend_ok(sf)
            self.assertEqual(rc, 2)
            self.assertFalse(called["gen"], "REVISE must NOT shell generate_images")
        finally:
            restore()

    def test_unexpected_verdict_fails_closed(self):
        # Defense in depth: a token the gate doesn't recognize must block, not spend.
        restore = self._patch_common()
        try:
            called = {"gen": False}

            def _spy_run(*a, **k):
                called["gen"] = True
                return _Proc(0)
            po.subprocess.run = _spy_run
            po.run_prompt_review_loop = lambda video, manifest_rel: (
                "MAYBE", "rel", "garbage verdict")
            sf = _FakeSF(self._ready_stages())
            rc = po.cmd_spend_ok(sf)
            self.assertEqual(rc, 2)
            self.assertFalse(called["gen"], "an unrecognized verdict must fail closed")
        finally:
            restore()

    def test_ship_with_fixes_refuses_spend(self):
        # BINARY gate (Steve, 2026-06-20): "SHIP WITH FIXES" is RETIRED as a
        # spendable verdict — it once billed an off-model V3 image batch. Only a
        # clean SHIP advances; SHIP WITH FIXES now falls to the else-branch and
        # fails CLOSED. This pins that retirement shut.
        restore = self._patch_common()
        try:
            called = {"gen": False}

            def _spy_run(*a, **k):
                called["gen"] = True
                return _Proc(0, stdout="generated")
            po.subprocess.run = _spy_run
            po.run_prompt_review_loop = lambda video, manifest_rel: (
                "SHIP WITH FIXES", "rel", "low-cost vocab fixes")
            sf = _FakeSF(self._ready_stages())
            rc = po.cmd_spend_ok(sf)
            self.assertEqual(rc, 2)
            self.assertFalse(called["gen"], "SHIP WITH FIXES must NOT spend (binary gate)")
        finally:
            restore()

    def test_unavailable_fails_closed_and_refuses_spend(self):
        # Fail-CLOSED hardening (2026-06-22): a reviewer that could not RUN
        # (timeout/outage/unparseable → UNAVAILABLE) is NOT an approval. Spending
        # past it would bill real money on an unreviewed batch exactly when the
        # safety check is blind. Must refuse and return 2; the human's spend-ok is
        # honored by RETRYING on recovery or an explicit --force override.
        restore = self._patch_common()
        try:
            called = {"gen": False}

            def _spy_run(*a, **k):
                called["gen"] = True
                return _Proc(0, stdout="generated")
            po.subprocess.run = _spy_run
            po.run_prompt_review_loop = lambda video, manifest_rel: (
                "UNAVAILABLE", "rel", "reviewer down")
            sf = _FakeSF(self._ready_stages())
            rc = po.cmd_spend_ok(sf)
            self.assertEqual(rc, 2)
            self.assertFalse(called["gen"], "UNAVAILABLE must fail CLOSED — no billed spend")
        finally:
            restore()

    def test_force_skips_gate_entirely(self):
        restore = self._patch_common()
        try:
            called = {"gen": False, "review": False}

            def _spy_run(*a, **k):
                called["gen"] = True
                return _Proc(0, stdout="generated")
            po.subprocess.run = _spy_run

            def _no_review(video, manifest_rel):
                called["review"] = True
                return ("HOLD-SPEND", "rel", "should be skipped")
            po.run_prompt_review_loop = _no_review
            sf = _FakeSF(self._ready_stages())
            rc = po.cmd_spend_ok(sf, force=True)
            self.assertEqual(rc, 0)
            self.assertFalse(called["review"], "--force must NOT run the review gate")
            self.assertTrue(called["gen"], "--force still spends")
        finally:
            restore()


class TestSpendOkThumbnailGuard(unittest.TestCase):
    """Deterministic pre-spend thumbnail-presence guard inside cmd_spend_ok.

    The thumbnail ART renders in the stage-5 billed batch via Video_NN_Thumbnail_A/_B
    entries. A batch with ZERO such entries bills the scene shots but produces no
    backplate for stage 8 to burn (the V2/V3 bland-thumbnail bug). The guard fails
    CLOSED on zero, WARNS on an incomplete A/B pair, and passes a full pair through."""

    def _ready_stages(self):
        return _mark_done(_stages(), "2_review")

    def _patch(self, manifest_obj):
        """Patch cmd_spend_ok's side effects, pointing vault_abs at a REAL temp JSON
        manifest with the given shape. Returns (restore, manifest_path, spy)."""
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix="_hd.json", delete=False, encoding="utf-8")
        json.dump(manifest_obj, tmp)
        tmp.close()
        manifest_path = Path(tmp.name)

        saved = (po.reconcile_orphans, po.vault_abs, po.notify,
                 po.subprocess.run, po.run_prompt_review_loop,
                 po._run_manifest_spine_lint)
        po.reconcile_orphans = lambda _s, _v: None
        po.vault_abs = lambda rel: manifest_path
        po.notify = lambda *a, **k: None
        # Neutralize the A-46 spine lint (fail-open) — these tests pin the
        # thumbnail guard only; the lint wire has its own test class.
        po._run_manifest_spine_lint = lambda _v, _m: None
        spy = {"gen": False}

        def _spy_run(*a, **k):
            spy["gen"] = True
            return _Proc(0, stdout="generated")
        po.subprocess.run = _spy_run
        # If the guard lets us through, SHIP so we reach the (stubbed) spend.
        po.run_prompt_review_loop = lambda video, manifest_rel: ("SHIP", "rel", "ok")

        def restore():
            (po.reconcile_orphans, po.vault_abs, po.notify,
             po.subprocess.run, po.run_prompt_review_loop,
             po._run_manifest_spine_lint) = saved
            try:
                manifest_path.unlink()
            except OSError:
                pass
        return restore, manifest_path, spy

    def test_zero_thumbnail_entries_blocks_spend(self):
        # The V2/V3 bug: a manifest with only scene shots, no thumbnail backplate.
        manifest = {"images": [
            {"name": "Video_03_Shot_01", "use_references": True, "prompt": "Three waves."},
            {"name": "Video_03_Shot_02", "use_references": True, "prompt": "Three points."},
        ]}
        restore, _path, spy = self._patch(manifest)
        try:
            rc = po.cmd_spend_ok(_FakeSF(self._ready_stages()))
            self.assertEqual(rc, 2)
            self.assertFalse(spy["gen"], "no thumbnail entries must fail CLOSED — no spend")
        finally:
            restore()

    def test_full_ab_pair_passes_guard(self):
        # Canonical: both A and B present → guard passes → SHIP → billed spend runs.
        manifest = {"images": [
            {"name": "Video_03_Shot_01", "use_references": True, "prompt": "Three waves."},
            {"name": "Video_03_Thumbnail_A", "use_references": True, "prompt": "Split."},
            {"name": "Video_03_Thumbnail_B", "use_references": True, "prompt": "Closeup."},
        ]}
        restore, _path, spy = self._patch(manifest)
        try:
            rc = po.cmd_spend_ok(_FakeSF(self._ready_stages()))
            self.assertEqual(rc, 0)
            self.assertTrue(spy["gen"], "a full A/B pair must NOT block the billed spend")
        finally:
            restore()

    def test_partial_pair_warns_but_proceeds(self):
        # V4 case: only _A present. One backplate is usable — WARN, don't block.
        manifest = {"images": [
            {"name": "Video_03_Shot_01", "use_references": True, "prompt": "Three waves."},
            {"name": "Video_03_Thumbnail_A", "use_references": True, "prompt": "Split."},
        ]}
        restore, _path, spy = self._patch(manifest)
        try:
            rc = po.cmd_spend_ok(_FakeSF(self._ready_stages()))
            self.assertEqual(rc, 0)
            self.assertTrue(spy["gen"], "an incomplete A/B pair warns but still spends")
        finally:
            restore()

    def test_unreadable_manifest_still_fails_open_past_thumbnail_guard(self):
        # A malformed-JSON manifest must NOT crash the suppressor/thumbnail guards;
        # they fail OPEN. (Since A-46 the spine lint deterministically BLOCKS broken
        # JSON further down — covered by TestSpendOkSpineLintGuard — so it is
        # stubbed open here to keep this test about THESE two guards.)
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix="_hd.json", delete=False, encoding="utf-8")
        tmp.write("{ this is not json ")
        tmp.close()
        manifest_path = Path(tmp.name)
        saved = (po.reconcile_orphans, po.vault_abs, po.notify,
                 po.subprocess.run, po.run_prompt_review_loop,
                 po._run_manifest_spine_lint)
        po.reconcile_orphans = lambda _s, _v: None
        po.vault_abs = lambda rel: manifest_path
        po.notify = lambda *a, **k: None
        po._run_manifest_spine_lint = lambda _v, _m: None
        spy = {"gen": False}

        def _spy_run(*a, **k):
            spy["gen"] = True
            return _Proc(0, stdout="generated")
        po.subprocess.run = _spy_run
        po.run_prompt_review_loop = lambda video, manifest_rel: ("SHIP", "rel", "ok")
        try:
            rc = po.cmd_spend_ok(_FakeSF(self._ready_stages()))
            self.assertEqual(rc, 0)
            self.assertTrue(spy["gen"], "unreadable manifest must fail OPEN, not block")
        finally:
            (po.reconcile_orphans, po.vault_abs, po.notify,
             po.subprocess.run, po.run_prompt_review_loop,
             po._run_manifest_spine_lint) = saved
            try:
                manifest_path.unlink()
            except OSError:
                pass


class TestSpendOkThumbnailBriefGuard(unittest.TestCase):
    """thumbnail-coordinator provenance guard inside cmd_spend_ok (2026-07-27).

    Thumbnail ART bills inside the stage-5 batch by design, so "did the coordinator
    actually run?" must be answered before money moves. It never was: 7 of the first
    14 videos (V05-07, V09-11, V14) billed thumbnail art with no brief, and V14's
    miss surfaced only because Steve asked after the fact.

    These pin the WIRING, not the helper (tests/test_thumbnail_brief_guard.py pins
    the file-matching). Both halves are needed: a mutation pass found that with the
    helper fully covered, `if False:` at the call site — the gate simply never
    firing — still left a green suite."""

    def _ready_stages(self):
        return _mark_done(_stages(), "2_review")

    def _patch(self, briefs):
        """cmd_spend_ok with a full A/B thumbnail pair, and `thumbnail_brief_paths`
        stubbed to `briefs` ([] = none found, None = could not check, list = found).
        Returns (restore, spy)."""
        manifest = {"images": [
            {"name": "Video_03_Shot_01", "use_references": True, "prompt": "Three waves."},
            {"name": "Video_03_Thumbnail_A", "use_references": True, "prompt": "Split."},
            {"name": "Video_03_Thumbnail_B", "use_references": True, "prompt": "Closeup."},
        ]}
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix="_hd.json", delete=False, encoding="utf-8")
        json.dump(manifest, tmp)
        tmp.close()
        manifest_path = Path(tmp.name)

        saved = (po.reconcile_orphans, po.vault_abs, po.notify, po.subprocess.run,
                 po.run_prompt_review_loop, po._run_manifest_spine_lint,
                 po.thumbnail_brief_paths)
        po.reconcile_orphans = lambda _s, _v: None
        po.vault_abs = lambda rel: manifest_path
        po.notify = lambda *a, **k: None
        po._run_manifest_spine_lint = lambda _v, _m: None
        # Record the video argument, don't discard it. A stub of the form
        # `lambda _v: briefs` let `thumbnail_brief_paths(video + 1)` survive
        # mutation testing — which in the real corpus would false-PASS V07 by
        # gating on V08's brief, the exact miss this gate exists to catch.
        spy = {"gen": False, "asked": []}
        po.thumbnail_brief_paths = lambda v: (spy["asked"].append(v), briefs)[1]

        def _spy_run(*a, **k):
            spy["gen"] = True
            return _Proc(0, stdout="generated")
        po.subprocess.run = _spy_run
        po.run_prompt_review_loop = lambda video, manifest_rel: ("SHIP", "rel", "ok")

        def restore():
            (po.reconcile_orphans, po.vault_abs, po.notify, po.subprocess.run,
             po.run_prompt_review_loop, po._run_manifest_spine_lint,
             po.thumbnail_brief_paths) = saved
            try:
                manifest_path.unlink()
            except OSError:
                pass
        return restore, spy

    def test_no_brief_blocks_spend(self):
        # The V14 case: thumbnail art in the batch, coordinator never dispatched.
        restore, spy = self._patch([])
        try:
            rc = po.cmd_spend_ok(_FakeSF(self._ready_stages()))
            self.assertEqual(rc, 2)
            self.assertFalse(spy["gen"], "un-briefed thumbnail art must not bill")
            # Pin the ARGUMENT too: `lambda _v: briefs` let a mutation to
            # thumbnail_brief_paths(video + 1) survive, which would gate V07 on
            # V08's brief — a false PASS of exactly the class this gate catches.
            self.assertEqual(spy["asked"], [3])
        finally:
            restore()

    def test_brief_present_does_not_block(self):
        # The other direction matters as much: a false BLOCK on the 7 videos that
        # DID get a brief teaches the next operator to reach for --force.
        restore, spy = self._patch(["BRANDS/3SK_Finance/Thumbnails/Video_03_Thumbnail_Brief.md"])
        try:
            rc = po.cmd_spend_ok(_FakeSF(self._ready_stages()))
            self.assertEqual(rc, 0)
            self.assertTrue(spy["gen"], "a briefed batch must spend normally")
            # Pin the ARGUMENT too: `lambda _v: briefs` let a mutation to
            # thumbnail_brief_paths(video + 1) survive, which would gate V07 on
            # V08's brief — a false PASS of exactly the class this gate catches.
            self.assertEqual(spy["asked"], [3])
        finally:
            restore()

    def test_unreadable_brief_dirs_fail_open(self):
        # None == "could not check". Every sibling guard here fails OPEN on its own
        # infra failure; conflating that with "no brief" is what broke 5 unrelated
        # tests whose fixtures stub vault_abs to a file.
        restore, spy = self._patch(None)
        try:
            rc = po.cmd_spend_ok(_FakeSF(self._ready_stages()))
            self.assertEqual(rc, 0)
            self.assertTrue(spy["gen"], "a guard that cannot run must not block a spend")
            # Pin the ARGUMENT too: `lambda _v: briefs` let a mutation to
            # thumbnail_brief_paths(video + 1) survive, which would gate V07 on
            # V08's brief — a false PASS of exactly the class this gate catches.
            self.assertEqual(spy["asked"], [3])
        finally:
            restore()


class TestSpendOkSpineLintGuard(unittest.TestCase):
    """A-46: the deterministic pre-spend manifest spine lint inside cmd_spend_ok.

    Runs the REAL lint module (seeded into po._MSL with its REPORT redirected to a
    tempfile so no test ever writes the vault report), wired through the real
    cmd_spend_ok. FAIL blocks the billed spend; WARN/CLEAN proceed; a lint that
    cannot run fails OPEN; --force downgrades a FAIL to a warning and spends."""

    def _ready_stages(self):
        return _mark_done(_stages(), "2_review")

    def _patch(self, manifest_obj, malformed=False):
        """Like the thumbnail guard's _patch, but keeps the REAL spine lint,
        seeded with a REPORT-redirected module copy."""
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix="_hd.json", delete=False, encoding="utf-8")
        if malformed:
            tmp.write("{ this is not json ")
        else:
            json.dump(manifest_obj, tmp)
        tmp.close()
        manifest_path = Path(tmp.name)
        report_tmp = tempfile.NamedTemporaryFile(suffix="_spine_report.md", delete=False)
        report_tmp.close()

        # Seed a REPORT-redirected lint module into the orchestrator's cache.
        _spec = importlib.util.spec_from_file_location(
            "manifest_spine_lint_test", _HERE.parent / "scripts" / "manifest_spine_lint.py")
        msl = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(msl)
        msl.REPORT = report_tmp.name

        saved = (po.reconcile_orphans, po.vault_abs, po.notify,
                 po.subprocess.run, po.run_prompt_review_loop, po._MSL)
        po.reconcile_orphans = lambda _s, _v: None
        po.vault_abs = lambda rel: manifest_path
        po.notify = lambda *a, **k: None
        po._MSL = msl
        spy = {"gen": False}

        def _spy_run(*a, **k):
            spy["gen"] = True
            return _Proc(0, stdout="generated")
        po.subprocess.run = _spy_run
        po.run_prompt_review_loop = lambda video, manifest_rel: ("SHIP", "rel", "ok")

        def restore():
            (po.reconcile_orphans, po.vault_abs, po.notify,
             po.subprocess.run, po.run_prompt_review_loop, po._MSL) = saved
            for p in (manifest_path, Path(report_tmp.name)):
                try:
                    p.unlink()
                except OSError:
                    pass
        return restore, manifest_path, spy

    # Both thumbnails present so the thumbnail guard passes and the spine lint is
    # the deciding guard in every case below.
    def _base_images(self):
        return [
            {"name": "Video_03_Thumbnail_A", "use_references": True,
             "prompt": "Split. Keep the top band clear."},
            {"name": "Video_03_Thumbnail_B", "use_references": True,
             "prompt": "Closeup. Keep the top band clear."},
        ]

    def test_duplicate_names_block_spend(self):
        # The V01 money leak: 6 dup-named shots double-billed. Structural FAIL.
        manifest = {"images": self._base_images() + [
            {"name": "Video_03_Shot_01", "use_references": True, "prompt": "Three waves."},
            {"name": "Video_03_Shot_01", "use_references": True, "prompt": "Three points."},
        ]}
        restore, _path, spy = self._patch(manifest)
        try:
            rc = po.cmd_spend_ok(_FakeSF(self._ready_stages()))
            self.assertEqual(rc, 2)
            self.assertFalse(spy["gen"], "dup image names must fail CLOSED — no spend")
        finally:
            restore()

    def test_malformed_json_blocks_spend(self):
        # Since A-46 an unparseable manifest is a deterministic structural FAIL —
        # it cannot render anyway; block BEFORE state churn, with a clear message.
        restore, _path, spy = self._patch(None, malformed=True)
        try:
            rc = po.cmd_spend_ok(_FakeSF(self._ready_stages()))
            self.assertEqual(rc, 2)
            self.assertFalse(spy["gen"], "malformed manifest JSON must block the spend")
        finally:
            restore()

    def test_warn_only_manifest_spends(self):
        # A genuine WARN (Thumbnail_A lacks a reserved-title-zone phrase → the
        # thumb-title-zone advisory) must not block the billed spend.
        manifest = {"images": [
            {"name": "Video_03_Thumbnail_A", "use_references": True,
             "prompt": "Split."},   # no reserved-zone phrase → WARN
            {"name": "Video_03_Thumbnail_B", "use_references": True,
             "prompt": "Closeup. Keep the top band clear."},
            {"name": "Video_03_Shot_01", "use_references": True, "prompt": "Three waves."},
        ]}
        restore, _path, spy = self._patch(manifest)
        try:
            # Fixture sanity: this manifest must actually produce a WARN verdict,
            # not CLEAN — otherwise this test isn't covering the advisory branch.
            res = po._run_manifest_spine_lint(3, _path)
            self.assertIsNotNone(res)
            self.assertEqual(res[1], "WARN", "fixture must genuinely WARN")
            rc = po.cmd_spend_ok(_FakeSF(self._ready_stages()))
            self.assertEqual(rc, 0)
            self.assertTrue(spy["gen"], "a WARN-only lint must not block the spend")
        finally:
            restore()

    def test_missing_script_fails_open(self):
        # Lint rc 2 (script not found) is a lint-side missing file, not a manifest
        # defect: warn + proceed. Selective vault_abs: manifest resolves, script
        # rel resolves to a nonexistent path.
        manifest = {"images": self._base_images()}
        restore, mpath, spy = self._patch(manifest)
        po.vault_abs = lambda rel: (Path("/nonexistent/script.md")
                                    if "Scripts/" in str(rel) else mpath)
        try:
            rc = po.cmd_spend_ok(_FakeSF(self._ready_stages()))
            self.assertEqual(rc, 0)
            self.assertTrue(spy["gen"], "a missing script must fail OPEN (rc 2), not block")
        finally:
            restore()

    def test_fixer_rewritten_manifest_is_relinted_before_spend(self):
        # THE deciding-pass pin (A-46 review HIGH finding): the review loop's
        # fixer rewrites the billed manifest IN PLACE. A manifest that linted
        # clean pre-loop but was rewritten into a FAIL state (dup names) before
        # the loop returned SHIP must STILL be blocked — the last lint must see
        # the bytes that would actually bill.
        clean = {"images": self._base_images() + [
            {"name": "Video_03_Shot_01", "use_references": True, "prompt": "Three waves."},
        ]}
        broken = {"images": self._base_images() + [
            {"name": "Video_03_Shot_01", "use_references": True, "prompt": "Three waves."},
            {"name": "Video_03_Shot_01", "use_references": True, "prompt": "Three points."},
        ]}
        restore, mpath, spy = self._patch(clean)

        def _rewriting_loop(video, manifest_rel):
            mpath.write_text(json.dumps(broken))  # the in-place fixer rewrite
            return ("SHIP", "rel", "ok")
        po.run_prompt_review_loop = _rewriting_loop
        try:
            rc = po.cmd_spend_ok(_FakeSF(self._ready_stages()))
            self.assertEqual(rc, 2)
            self.assertFalse(
                spy["gen"],
                "a fixer-rewritten FAIL manifest must be re-linted and blocked")
        finally:
            restore()

    def test_lint_unavailable_fails_open(self):
        # Guard-infra failure is not a finding: if the lint cannot load, spend
        # proceeds (the LLM review gate still stands).
        manifest = {"images": self._base_images()}
        restore, _path, spy = self._patch(manifest)
        saved_loader = po._load_spine_lint
        po._MSL = None

        def _boom():
            raise RuntimeError("lint import broken")
        po._load_spine_lint = _boom
        try:
            rc = po.cmd_spend_ok(_FakeSF(self._ready_stages()))
            self.assertEqual(rc, 0)
            self.assertTrue(spy["gen"], "a lint that cannot run must fail OPEN")
        finally:
            po._load_spine_lint = saved_loader
            restore()

    def test_force_downgrades_fail_to_warning_and_spends(self):
        # Steve override: --force never blocks on the lint, but it still runs it
        # and shouts, so an override can't silently bill a dup-named batch.
        manifest = {"images": self._base_images() + [
            {"name": "Video_03_Shot_01", "use_references": True, "prompt": "Three waves."},
            {"name": "Video_03_Shot_01", "use_references": True, "prompt": "Three points."},
        ]}
        restore, _path, spy = self._patch(manifest)
        try:
            rc = po.cmd_spend_ok(_FakeSF(self._ready_stages()), force=True)
            self.assertEqual(rc, 0)
            self.assertTrue(spy["gen"], "--force must still spend past a lint FAIL")
        finally:
            restore()


class TestAssembleImageGate(unittest.TestCase):
    """Pass B: the pre-assemble RENDERS gate inside run_script_stage."""

    def test_revise_refuses_assembly(self):
        saved = (po.run_image_review, po.subprocess.run, po.notify)
        try:
            called = {"build": False}

            def _spy_run(*a, **k):
                called["build"] = True
                return _Proc(0)
            po.subprocess.run = _spy_run
            po.notify = lambda *a, **k: None
            po.run_image_review = lambda mode, video, manifest_rel=None: (
                "REVISE", "rel", "off-model render")
            ok, msg = po.run_script_stage("6_assemble", 3)
            self.assertFalse(ok)
            self.assertIn("REVISE", msg)
            self.assertFalse(called["build"], "must NOT build the cut on REVISE")
        finally:
            (po.run_image_review, po.subprocess.run, po.notify) = saved

    def test_force_overrides_revise_and_builds_without_reviewing(self):
        """Steve's explicit CLI override (`--advance --force`): the assemble gate
        skips the RENDERS review entirely and assembles renders accepted as-is
        (e.g. V14's closed-by-steve-decision). run_image_review must NOT be called
        (a re-dispatch would re-bill an agent to reprint the same REVISE on
        unchanged renders), and build_video.py MUST run. A _Boom that raises on any
        review call proves the skip is real, not a happened-to-pass SHIP."""
        saved = (po.run_image_review, po.subprocess.run, po.notify,
                 po._image_review_verdict_rel)
        try:
            called = {"build": False}

            def _spy_run(*a, **k):
                called["build"] = True
                return _Proc(0, stdout="assembled")

            def _boom(*a, **k):
                raise AssertionError("run_image_review must NOT be called under --force")
            po.subprocess.run = _spy_run
            po.notify = lambda *a, **k: None
            po.run_image_review = _boom
            po._image_review_verdict_rel = lambda video: "rel"
            ok, msg = po.run_script_stage("6_assemble", 3, force=True)
            self.assertTrue(ok, f"--force must assemble accepted-as-is renders: {msg!r}")
            self.assertTrue(called["build"], "--force must reach build_video.py")
        finally:
            (po.run_image_review, po.subprocess.run, po.notify,
             po._image_review_verdict_rel) = saved

    def test_advance_all_never_forces_the_assemble_gate(self):
        """The hourly fleet sweep MUST stay fail-closed: autonomy can never cross a
        REVISE-parked assemble stage. Two structural invariants pin that: (1)
        advance_once defaults force=False, and (2) cmd_advance_all (the sweep core)
        calls advance_once WITHOUT ever passing force. If either regresses — the
        default flips or someone threads force into the sweep — the override becomes
        auto-reachable and unreviewed renders could ship. Driving the full sweep
        here is heavy (real StateFile/discover_videos); assert the invariants
        directly so the guard can't be defeated by a behavioural test that happens
        to pass."""
        import inspect
        self.assertIs(inspect.signature(po.advance_once).parameters["force"].default,
                      False, "advance_once must default force=False")
        src = inspect.getsource(po.cmd_advance_all)
        self.assertIn("advance_once(sf)", src,
                      "the sweep must call advance_once WITHOUT force")
        # Precise: the advance_once CALL must not carry a force arg. (notify(...,
        # force=True) elsewhere in the fn is an unrelated callee — don't match it.)
        self.assertNotIn("advance_once(sf, force", src,
                          "cmd_advance_all must never thread force into advance_once")

    def test_unavailable_fails_closed_and_does_not_build(self):
        """SUPERSEDED 2026-07-26 (was test_unavailable_fails_open_and_builds).

        The original asserted "fail-open must still assemble" and was written
        2026-06-19 — BEFORE the binary-gate directive (c53c77e, 06-20) and before
        Steve hardened the PROMPTS gate to fail CLOSED on UNAVAILABLE (25ba76a,
        06-22: "A review that did not run is not an approval"). That audit left the
        RENDERS gate fail-open, which reads as an inconsistency rather than a
        deliberate carve-out: the justification was "assembly is free", but the real
        cost is that unreviewed renders flow onward to thumbnail, description and
        publish. Now fails closed and retries as INFRA (bounded by MAX_INFRA), so a
        transient reviewer outage self-heals instead of shipping unvetted work.
        """
        saved = (po.run_image_review, po.subprocess.run, po.notify)
        try:
            called = {"build": False}

            def _spy_run(*a, **k):
                called["build"] = True
                return _Proc(0, stdout="assembled")
            po.subprocess.run = _spy_run
            po.notify = lambda *a, **k: None
            # A long detail exercises the detail[:120] truncation bound (the
            # documented ≤300 two-opposite-end-truncation invariant): the head must
            # cut at exactly 120 chars so the cause survives both display windows.
            long_detail = "Q" * 200
            po.run_image_review = lambda mode, video, manifest_rel=None: (
                "UNAVAILABLE", "rel", long_detail)
            ok, msg = po.run_script_stage("6_assemble", 3)
            self.assertFalse(ok, "must NOT assemble unreviewed renders")
            self.assertFalse(called["build"], "build_video.py must never be reached")
            self.assertTrue(po._is_infra_failure(msg),
                            f"a reviewer outage must retry, not burn a strike: {msg!r}")
            # Exactly 120 detail chars then the literal " — refusing" — 121 chars
            # (an off-by-one on the bound) would push a Q between them and this fails.
            self.assertIn("Q" * 120 + " — refusing", msg)
            self.assertNotIn("Q" * 121, msg)
        finally:
            (po.run_image_review, po.subprocess.run, po.notify) = saved

    def test_success_folds_stderr_warning_into_the_state_note(self):
        """MEDIUM-1 (round 18, 2026-07-28). build_video.py's dropped-shot warning
        prints to stderr, and this function used to return only stdout on the
        rc==0 path — so the warning never reached _on_success's `s["note"]`,
        the one place a human/scanner actually reads it later. Pin that it does
        now, on the SAME build_video.py:~300 shape (a real skipped-name line)."""
        saved = (po.run_image_review, po.subprocess.run, po.notify)
        try:
            po.subprocess.run = lambda *a, **k: _Proc(
                0, stdout="assembled",
                stderr="warning: video_04_hd.json: 18 shot-shaped name(s) did "
                       "not parse and were DROPPED — Video_04_Shot_04a2.")
            po.notify = lambda *a, **k: None
            po.run_image_review = lambda mode, video, manifest_rel=None: (
                "SHIP", "rel", "clean")
            ok, msg = po.run_script_stage("6_assemble", 3)
            self.assertTrue(ok)
            self.assertIn("STDERR: warning:", msg)
            self.assertIn("DROPPED", msg)
        finally:
            (po.run_image_review, po.subprocess.run, po.notify) = saved

    def test_success_stdout_and_stderr_bounds_in_the_fold(self):
        """HIGH-2 (round 20) / MEDIUM-A,B (round 22). Retires the round-18
        fixture that pinned out[-300:]+"\\nSTDERR: "+warn[-300:] as correct —
        that shape passes run_script_stage but is destroyed by _on_success's
        OWN [-300:], which strips the FRONT of the combined string, i.e.
        exactly where build_video's warning puts its cause. The fix bounds
        the fold itself to out[-50:]+"\\nSTDERR: "+warn[:230] (<=300 total, so
        _on_success's cut is a guaranteed no-op): warn is head-sliced because
        the cause sits at the FRONT of build_video's warning, but `out` is
        ALSO tail-sliced (not head-sliced) because it is already a tail slice
        of stdout ([-300:] applied earlier in this function) whose one
        meaningful line — the `done. Draft video -> ...` artifact path — is
        printed LAST.

        The round-22 fixture regressed this: it used a <=300-char stdout, so
        the earlier [-300:] was a no-op and out[:50] vs out[-50:] merely
        picked different characters of the SAME untouched string — it never
        exercised the two-step truncation the real bug depends on, and its
        assertion pinned the out[:50] (wrong-direction) value, so a correct
        out[-50:] fix read as a test failure. Fixed here: stdout_in is >>300
        chars so the first [-300:] actually truncates, and the marker sits at
        stdout_in's true last character — the only place a tail-slice-of-a-
        tail-slice can find it."""
        saved = (po.run_image_review, po.subprocess.run, po.notify)
        try:
            # 1050 chars total, clearly longer than the 1157 [-300:] window,
            # so this fixture exercises out's OWN prior truncation. 'Q' is
            # the true last character of stdout — it must survive both the
            # [-300:] and the [-50:] slice.
            stdout_in = "a" * 1000 + "b" * 49 + "Q"
            stderr_in = "c" * 229 + "Z" + "c" * 100   # 'Z' is the 230th char (last kept)
            po.subprocess.run = lambda *a, **k: _Proc(0, stdout=stdout_in, stderr=stderr_in)
            po.notify = lambda *a, **k: None
            po.run_image_review = lambda mode, video, manifest_rel=None: (
                "SHIP", "rel", "clean")
            ok, msg = po.run_script_stage("6_assemble", 3)
            self.assertTrue(ok)
            self.assertEqual(msg, "b" * 49 + "Q" + "\nSTDERR: " + "c" * 229 + "Z",
                              "stdout must be TAIL-sliced (out[-50:]), not "
                              "head-sliced — the artifact path build_video "
                              "prints last must survive")
            self.assertLessEqual(len(msg), 300,
                                  "combined must fit so _on_success's own cut is a no-op")
        finally:
            (po.run_image_review, po.subprocess.run, po.notify) = saved

    def test_success_stderr_cause_survives_on_success_truncation(self):
        """HIGH-2 (round 20). The round-18 fold survived run_script_stage's
        return but not the SECOND truncation in _on_success (`s["note"] =
        (out or "")[-300:]`) — that cut is from the TAIL, and build_video's
        dropped-shot warning puts its cause (the count, filename, "DROPPED")
        at the FRONT, so the old tail-sliced fold lost the cause and kept only
        a bare list of shot names. Runs the composed string through the REAL
        _on_success (not just run_script_stage's return) with the verbatim
        476-char V04 warning and asserts the cause is what a scanner reading
        the state-file note would actually see."""
        saved = (po.run_image_review, po.subprocess.run, po.notify)
        try:
            warn = (
                "warning: video_04_hd.json: 18 shot-shaped name(s) did not parse "
                "and were DROPPED — Video_04_Shot_04a2, Video_04_Shot_04d2, "
                "Video_04_Shot_05a2, Video_04_Shot_06a2, Video_04_Shot_06c2, "
                "Video_04_Shot_07a2, Video_04_Shot_07a3, Video_04_Shot_07c2, "
                "Video_04_Shot_08a2, Video_04_Shot_08a3, Video_04_Shot_08a4, "
                "Video_04_Shot_08b2, Video_04_Shot_08c2, Video_04_Shot_08c3, "
                "Video_04_Shot_08d2, Video_04_Shot_08e2, Video_04_Shot_09a2, "
                "Video_04_Shot_11b2. Rename to Shot_NNa or Shot_NNa_N."
            )
            self.assertEqual(len(warn), 476, "fixture drifted from the real V04 warning")
            po.subprocess.run = lambda *a, **k: _Proc(0, stdout="assembled", stderr=warn)
            po.notify = lambda *a, **k: None
            po.run_image_review = lambda mode, video, manifest_rel=None: (
                "SHIP", "rel", "clean")
            ok, msg = po.run_script_stage("6_assemble", 3)
            self.assertTrue(ok)

            saved_art = po._producer_artifact_ok
            po._producer_artifact_ok = lambda key, video: (True, "")
            try:
                s: dict = {}
                done, _ = po._on_success(s, "6_assemble", 3, msg)
            finally:
                po._producer_artifact_ok = saved_art
            self.assertTrue(done)
            note = s["note"]
            self.assertIn("DROPPED", note, f"cause dropped by the second truncation: {note!r}")
            self.assertIn("video_04_hd", note, f"filename dropped: {note!r}")
            self.assertIn("18", note, f"count dropped: {note!r}")
        finally:
            (po.run_image_review, po.subprocess.run, po.notify) = saved

    def test_success_stdout_alone_truncates_at_exactly_300_chars(self):
        """Round-20 mutation-gate finding: `out = stdout.strip()[-300:]` (the
        FIRST slice, line ~1157) survived int+1 '300'->'301' -- nothing pinned
        it standalone. The stderr-present path bounds its OWN combined string
        to <=300 regardless (out[:50]+"\\nSTDERR: "+warn[:230]), so that path
        can never observe this slice's exact boundary; only the no-stderr path,
        where `out` is returned unchanged, can."""
        saved = (po.run_image_review, po.subprocess.run, po.notify)
        try:
            stdout_301 = "Q" + "a" * 300   # marker at position -301
            po.subprocess.run = lambda *a, **k: _Proc(0, stdout=stdout_301)
            po.notify = lambda *a, **k: None
            po.run_image_review = lambda mode, video, manifest_rel=None: (
                "SHIP", "rel", "clean")
            ok, msg = po.run_script_stage("6_assemble", 3)
            self.assertTrue(ok)
            self.assertNotIn("Q", msg, "stdout kept 301+ chars, not 300")
            self.assertEqual(msg, "a" * 300)
        finally:
            (po.run_image_review, po.subprocess.run, po.notify) = saved

    def test_success_with_no_stderr_has_no_stderr_marker(self):
        """The flip side of the fixture above: a clean run (no stderr) must NOT
        grow a 'STDERR:' marker out of nowhere — pins that the fold-in is
        conditional on `warn`, not unconditional."""
        saved = (po.run_image_review, po.subprocess.run, po.notify)
        try:
            po.subprocess.run = lambda *a, **k: _Proc(0, stdout="assembled")
            po.notify = lambda *a, **k: None
            po.run_image_review = lambda mode, video, manifest_rel=None: (
                "SHIP", "rel", "clean")
            ok, msg = po.run_script_stage("6_assemble", 3)
            self.assertTrue(ok)
            self.assertNotIn("STDERR:", msg)
            self.assertEqual(msg, "assembled")
        finally:
            (po.run_image_review, po.subprocess.run, po.notify) = saved


class TestCanonicalManifest(unittest.TestCase):
    """canonical_manifest_rel: the ONE billed manifest the reviewer audits, the
    fixer edits, and generate_images bills must all agree on."""

    def test_default_is_image_factory_hd_batch(self):
        rel = po.canonical_manifest_rel(7)
        self.assertEqual(
            rel,
            f"{po.VAULT_REL}/Raw_Assets/Image_Factory/manifests/video_07_hd.json")

    def test_none_stages_uses_default(self):
        self.assertTrue(po.canonical_manifest_rel(3, None).endswith(
            "manifests/video_03_hd.json"))

    def test_state_override_wins(self):
        stages = po.default_stages()
        stages["5_images"]["scene_manifest"] = "custom/path/v9.json"
        self.assertEqual(po.canonical_manifest_rel(9, stages), "custom/path/v9.json")

    def test_missing_override_key_falls_back(self):
        # A stage map with no 'scene_manifest' field must not crash.
        self.assertTrue(po.canonical_manifest_rel(1, po.default_stages()).endswith(
            "video_01_hd.json"))


class TestPromptReviewLoop(unittest.TestCase):
    """run_prompt_review_loop: the CLOSED review→fix→re-review feedback loop at
    the PROMPTS gate (image analogue of scriptwriter↔script-reviewer). Stubs
    run_image_review with a scripted verdict sequence + run_prompt_fixer."""

    def _patch(self, verdicts, fixer=None):
        """verdicts: list consumed one per run_image_review call. fixer: stub for
        run_prompt_fixer (defaults to a successful no-op). Returns (restore, calls)
        where calls records review/fix invocation counts."""
        saved = (po.run_image_review, po.run_prompt_fixer)
        calls = {"review": 0, "fix": 0}
        seq = list(verdicts)

        def _review(mode, video, manifest_rel=None):
            calls["review"] += 1
            v = seq[min(calls["review"] - 1, len(seq) - 1)]
            return v, f"rel{calls['review']}", f"detail {v}"

        def _default_fixer(video, verdict_rel, manifest_rel):
            calls["fix"] += 1
            return True, "fixed"

        def _wrapped_fixer(video, verdict_rel, manifest_rel):
            calls["fix"] += 1
            return fixer(video, verdict_rel, manifest_rel)

        po.run_image_review = _review
        po.run_prompt_fixer = _wrapped_fixer if fixer else _default_fixer

        def restore():
            (po.run_image_review, po.run_prompt_fixer) = saved
        return restore, calls

    def test_clean_first_pass_no_fix(self):
        restore, calls = self._patch(["SHIP"])
        try:
            verdict, vrel, detail = po.run_prompt_review_loop(3, "m.json")
            self.assertEqual(verdict, "SHIP")
            self.assertEqual(calls["review"], 1)
            self.assertEqual(calls["fix"], 0, "a clean first pass must not invoke the fixer")
        finally:
            restore()

    def test_ship_with_fixes_drives_fix_loop(self):
        # BINARY gate (Steve, 2026-06-20): "SHIP WITH FIXES" is RETIRED — it is no
        # longer a clean SHIP, so the loop now treats it as blocking and drives the
        # fixer. A stub that keeps returning it must exhaust the fix budget (like a
        # persistent HOLD-SPEND) and surface the blocking verdict for the human gate.
        restore, calls = self._patch(["SHIP WITH FIXES"])
        try:
            verdict, _vrel, _detail = po.run_prompt_review_loop(3, "m.json")
            self.assertEqual(verdict, "SHIP WITH FIXES")
            self.assertEqual(calls["fix"], po.IMAGE_REVIEW_MAX_FIX_ATTEMPTS)
            self.assertEqual(calls["review"], po.IMAGE_REVIEW_MAX_FIX_ATTEMPTS + 1)
        finally:
            restore()

    def test_hold_then_ship_fixes_once_and_clears(self):
        # HOLD-SPEND → fix → re-review SHIP: exactly one fix, two reviews, clears.
        restore, calls = self._patch(["HOLD-SPEND", "SHIP"])
        try:
            verdict, _vrel, detail = po.run_prompt_review_loop(3, "m.json")
            self.assertEqual(verdict, "SHIP")
            self.assertEqual(calls["review"], 2)
            self.assertEqual(calls["fix"], 1)
            self.assertIn("auto-fix attempt", detail)
        finally:
            restore()

    def test_persistent_hold_stops_at_max_attempts(self):
        # Always HOLD-SPEND: loop must stop at IMAGE_REVIEW_MAX_FIX_ATTEMPTS fixes
        # (not loop forever) and return the blocking verdict for the human gate.
        restore, calls = self._patch(["HOLD-SPEND"])
        try:
            verdict, _vrel, _detail = po.run_prompt_review_loop(3, "m.json")
            self.assertEqual(verdict, "HOLD-SPEND")
            self.assertEqual(calls["fix"], po.IMAGE_REVIEW_MAX_FIX_ATTEMPTS)
            # one initial review + one re-review per fix attempt
            self.assertEqual(calls["review"], po.IMAGE_REVIEW_MAX_FIX_ATTEMPTS + 1)
        finally:
            restore()

    def test_fixer_cannot_run_stops_loop(self):
        # If the fixer itself can't run, the loop must STOP (not re-review) and
        # surface the blocking verdict — never silently proceed toward spend.
        restore, calls = self._patch(
            ["HOLD-SPEND"], fixer=lambda v, vr, m: (False, "agent missing"))
        try:
            verdict, _vrel, detail = po.run_prompt_review_loop(3, "m.json")
            self.assertEqual(verdict, "HOLD-SPEND")
            self.assertEqual(calls["fix"], 1)
            self.assertEqual(calls["review"], 1, "must not re-review after a failed fix")
            self.assertIn("could not run", detail)
        finally:
            restore()

    def test_unavailable_ends_loop_immediately(self):
        restore, calls = self._patch(["UNAVAILABLE"])
        try:
            verdict, _vrel, _detail = po.run_prompt_review_loop(3, "m.json")
            self.assertEqual(verdict, "UNAVAILABLE")
            self.assertEqual(calls["fix"], 0)
            self.assertEqual(calls["review"], 1)
        finally:
            restore()


class TestRunPromptFixerMissingAgent(unittest.TestCase):
    def test_missing_agent_file_does_not_run(self):
        old = po.AGENTS_DIR
        try:
            with tempfile.TemporaryDirectory() as d:
                po.AGENTS_DIR = Path(d)  # no scene-image-prompt-generator.md inside
                ran, detail = po.run_prompt_fixer(3, "vrel", "m.json")
            self.assertFalse(ran)
            self.assertIn("not found", detail)
        finally:
            po.AGENTS_DIR = old


class TestMutationCrashGuard(unittest.TestCase):
    """HIGH-2 (round 12 review): the mutation-crash guard build_video.py has
    must ALSO protect pipeline_orchestrator.py — the OTHER hourly launchd
    target (com.iris.claude-code-pipeline-sweep runs `--advance-all` on THIS
    file directly, not via build_video.py). A mutant left on disk by a
    SIGKILL/OOM/power-loss would otherwise execute silently on the next
    sweep, and this file owns cmd_spend_ok, the RENDERS gate and
    run_script_review_gate — strictly worse than the build_video.py case,
    which is at least spawned BY the (now also guarded) orchestrator.

    Invoked as a real subprocess, not in-process: main() calls
    install_agent_reaper() right after the guard, which installs process-wide
    SIGINT/SIGTERM handlers — that must land in a throwaway child process, not
    hijack the signal handlers of the test runner itself (see that function's
    own docstring: 'Called from main() ONLY — never at import... would hijack
    them from any process that merely imports this module (the daemon, the
    test suites).').

    ISOLATED TREE (round 16, 2026-07-27): a prior cut wrote directly to the
    REAL `.mutate.lock` / `pipeline_orchestrator.py.mutate.orig` in the repo
    root — the exact `if exists(): ... write()` TOCTOU shape mutate.py's own
    module docstring forbids on those primitives (a real mutate.py run racing
    this fixture gets its lock clobbered then unlinked, and its pristine
    sidecar deleted out from under it). setUp copies only the guard's
    dependency closure — this target plus scripts/mutate.py, same relative
    layout so each file's own __file__-derived paths resolve the same way
    they do in the real repo — into a private tmp_root per test. Nothing below
    ever opens the real LOCK or sidecar, so parallel test runs (or a real
    concurrent mutation pass) cannot collide with this fixture or each other.
    """

    def setUp(self):
        self.guard_root = Path(tempfile.mkdtemp(prefix="po_guard_tree_"))
        (self.guard_root / "scripts").mkdir()
        shutil.copy(_HERE.parent / "scripts" / "mutate.py",
                    self.guard_root / "scripts" / "mutate.py")
        self.guard_target = self.guard_root / "scripts" / "pipeline_orchestrator.py"
        shutil.copy(_MODULE_PATH, self.guard_target)
        self.guard_lock = self.guard_root / ".mutate.lock"
        self.guard_sidecar = self.guard_target.with_suffix(
            self.guard_target.suffix + ".mutate.orig")

    def tearDown(self):
        shutil.rmtree(self.guard_root, ignore_errors=True)

    def test_refuses_to_run_with_a_mutate_sidecar_present(self):
        # This is the CRASH (died-mid-flight) branch. guard_lock is a private
        # tmp path nothing else can be holding, so — unlike the old fixture,
        # which shared the real .mutate.lock with mutate.py's own outer pass
        # and had to skip when nested inside one — no live-lock caveat is
        # needed here; this scenario is unconditionally the crash branch.
        self.guard_sidecar.write_text(self.guard_target.read_text(encoding="utf-8"))
        env = {k: v for k, v in os.environ.items() if k != "SK_MUTATION_RUN"}
        r = subprocess.run([sys.executable, str(self.guard_target),
                            "--status", "--video", "999999"],
                           capture_output=True, text=True, timeout=30, env=env)
        out = r.stdout + r.stderr
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("mutate.orig", out)
        self.assertIn("Refusing to run", out)

    def test_stands_down_under_sk_mutation_run(self):
        # SK_MUTATION_RUN is how the harness tells the guard to stand down
        # DURING an actual mutation pass (the sidecar exists by design then).
        # If it didn't stand down, the guard would trivially "kill" every
        # mutant of itself by refusing to run at all — proving nothing.
        self.guard_sidecar.write_text(self.guard_target.read_text(encoding="utf-8"))
        env = dict(os.environ, SK_MUTATION_RUN="1")
        r = subprocess.run([sys.executable, str(self.guard_target),
                            "--advance-all", "--video", "1"],
                           capture_output=True, text=True, timeout=30, env=env)
        out = r.stdout + r.stderr
        # Must NOT die with the guard's own message. It should get past the
        # guard and fail on the very next real check instead (fleet
        # commands reject --video) — proving the guard stood down rather
        # than coincidentally refusing for some other reason.
        self.assertNotIn("mutate.orig", out)
        self.assertIn("do not pass --video", out)

    def test_refuses_with_active_wording_under_a_live_lock_not_crash_advice(self):
        # MEDIUM-1 (round 12, ported round 14): end-to-end proof the ported
        # live-lock branch actually fires in THIS file's guard, not just that
        # its source text resembles build_video.py's. Same sidecar+lock
        # scenario as test_derive_shots.py's build_video.py fixture. Writing
        # our own pid into guard_lock is safe: it is a tmp file this test
        # owns outright, not the shared production lock.
        self.guard_sidecar.write_text(self.guard_target.read_text(encoding="utf-8"))
        self.guard_lock.write_text(f"{os.getpid()} selftest\n")
        env = {k: v for k, v in os.environ.items() if k != "SK_MUTATION_RUN"}
        r = subprocess.run([sys.executable, str(self.guard_target),
                            "--status", "--video", "999999"],
                           capture_output=True, text=True, timeout=30, env=env)
        out = r.stdout + r.stderr
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("mutation-run-active", out)
        self.assertIn("ACTIVE", out)
        self.assertNotIn("died mid-flight", out)
        # HIGH-1 (round 20): must be EXACTLY 75 (EX_TEMPFAIL), not merely
        # nonzero. run_job.sh (which wraps the hourly --advance-all sweep)
        # special-cases 75 as "target unavailable, did nothing" -- silent, no
        # red alert, retry marker dropped. Any other nonzero code is a genuine
        # failure: red Telegram alert + a retry marker the 30-min replayer
        # picks up, for a condition that is expected and self-clearing.
        self.assertEqual(r.returncode, 75,
                          f"must be EX_TEMPFAIL so run_job.sh stays silent, got {r.returncode}")

    def test_active_branch_requires_both_holder_and_alive_not_or(self):
        # HIGH-2 mutation-gate finding (round 16, 2026-07-27): `holder or alive`
        # is indistinguishable from `holder and alive` in BOTH subprocess
        # scenarios above — the crash branch has both falsy (no lock file at
        # all), the live-lock branch has both truthy (a real running pid) — so
        # `scripts/pipeline_orchestrator.py --mutate` survived that flip-bool
        # until this pin was added. Exact same fixture shape as
        # test_derive_shots.py's build_video.py guard's equivalent check; that
        # file's copy of this guard already carries it, this one didn't.
        guard_src = _MODULE_PATH.read_text(encoding="utf-8")
        self.assertIn("if holder and alive:", guard_src)


class TestMutationRunActiveInfraMarker(unittest.TestCase):
    """MEDIUM-1 extra credit (round 12 review): build_video.py's lock-ACTIVE
    refusal text must classify as infra (left ready, retried — bounded by
    MAX_INFRA) rather than a task failure (consumes MAX_FAILS) — the whole
    point being that this refusal clears on its own in ~100s and should not
    park a video after 3 collisions with a healthy mutation pass."""

    def test_marker_recognised_as_infra(self):
        # This is the actual string build_video.py's guard prints (see
        # build_video.py's crash-guard block) run through run_script_stage's
        # exact wrapping (`build_video.py exited N: {stderr[-500:]}`).
        msg = ("build_video.py exited 1: error: build_video.py.mutate.orig exists "
               "and a mutation run is ACTIVE (pid 123) — this refusal is correct "
               "and temporary; do NOT cp/rm anything below, just re-run after it "
               "finishes. (mutation-run-active)")
        self.assertTrue(po._is_infra_failure(msg))

    def test_marker_is_a_real_member_of_the_tuple(self):
        # Guards against the marker being asserted only in the crafted string
        # above and silently dropping out of INFRA_FAILURE_MARKERS itself.
        self.assertIn("mutation-run-active", po.INFRA_FAILURE_MARKERS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
