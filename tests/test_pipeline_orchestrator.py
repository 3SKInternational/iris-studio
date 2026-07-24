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
                 po.subprocess.run, po.run_prompt_review_loop)

        def _noop_reconcile(_stages, _video):
            return None

        # manifest_abs must .exists() → return an obviously-present path.
        existing = _MODULE_PATH  # this test file's sibling: a real file

        def _fake_vault_abs(rel):
            return existing

        po.reconcile_orphans = _noop_reconcile
        po.vault_abs = _fake_vault_abs
        po.notify = lambda *a, **k: None

        def restore():
            (po.reconcile_orphans, po.vault_abs, po.notify,
             po.subprocess.run, po.run_prompt_review_loop) = saved
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
                 po.subprocess.run, po.run_prompt_review_loop)
        po.reconcile_orphans = lambda _s, _v: None
        po.vault_abs = lambda rel: manifest_path
        po.notify = lambda *a, **k: None
        spy = {"gen": False}

        def _spy_run(*a, **k):
            spy["gen"] = True
            return _Proc(0, stdout="generated")
        po.subprocess.run = _spy_run
        # If the guard lets us through, SHIP so we reach the (stubbed) spend.
        po.run_prompt_review_loop = lambda video, manifest_rel: ("SHIP", "rel", "ok")

        def restore():
            (po.reconcile_orphans, po.vault_abs, po.notify,
             po.subprocess.run, po.run_prompt_review_loop) = saved
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

    def test_unreadable_manifest_fails_open_to_llm_gate(self):
        # A malformed-JSON manifest must NOT crash the guard; it fails OPEN so the
        # LLM gate (here stubbed SHIP) and downstream checks still run.
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix="_hd.json", delete=False, encoding="utf-8")
        tmp.write("{ this is not json ")
        tmp.close()
        manifest_path = Path(tmp.name)
        saved = (po.reconcile_orphans, po.vault_abs, po.notify,
                 po.subprocess.run, po.run_prompt_review_loop)
        po.reconcile_orphans = lambda _s, _v: None
        po.vault_abs = lambda rel: manifest_path
        po.notify = lambda *a, **k: None
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
             po.subprocess.run, po.run_prompt_review_loop) = saved
            try:
                manifest_path.unlink()
            except OSError:
                pass


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

    def test_unavailable_fails_open_and_builds(self):
        saved = (po.run_image_review, po.subprocess.run, po.notify)
        try:
            called = {"build": False}

            def _spy_run(*a, **k):
                called["build"] = True
                return _Proc(0, stdout="assembled")
            po.subprocess.run = _spy_run
            po.notify = lambda *a, **k: None
            po.run_image_review = lambda mode, video, manifest_rel=None: (
                "UNAVAILABLE", "rel", "reviewer down")
            ok, _msg = po.run_script_stage("6_assemble", 3)
            self.assertTrue(ok, "fail-open must still assemble")
            self.assertTrue(called["build"])
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
