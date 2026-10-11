#!/usr/bin/env python3
"""spend-ok runs the billed image batch so that it can finish, and never unowned.

THE GAP (2026-10-10). Flagship batches run 52-123 min. The orchestrator ran
generate_images under subprocess.run with a 3600 s timeout (most batches killed
part way), and the daemon's 3600 s /pipeline cap SIGTERMed only the orchestrator:
the batch kept billing with no owner, stage 5 kept the dead orchestrator's pid,
and the batch died of BrokenPipe on its first stderr line.

Now cmd_spend_ok runs the batch through _run_image_batch: own session, output to
a log file, its pid and start token in 5_images. These tests drive the REAL
orchestrator code against a stub batch (an executable that writes one PNG per
manifest shot at a set pace, printing to stderr as it goes). Nothing here can
bill: generate_images.py is never run, only the stub, through po.PYTHON.

Timings are scaled: the stub batch outlives the scaled daemon cap by the same
margin a 120-min batch outlives the real 3600 s one.

Run: python3 tests/test_spend_ok_batch.py
"""
import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
MODULE = REPO / "scripts" / "pipeline_orchestrator.py"
VIDEO = 97
SHOTS = [f"Video_{VIDEO}_Shot_{i:02d}" for i in range(1, 7)]

_spec = importlib.util.spec_from_file_location("po_batch_under_test", MODULE)
po = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(po)

STUB = f"""#!{sys.executable}
import json, os, sys, time
manifest, out = sys.argv[2], sys.argv[4]
dt = float(os.environ.get("STUB_DT", "0.2"))
os.makedirs(out, exist_ok=True)
for img in json.load(open(manifest))["images"]:
    time.sleep(dt)
    p = os.path.join(out, img["name"] + ".png")
    if not os.path.exists(p):
        with open(p, "wb") as fh:
            fh.write(b"\\x89PNG stub")
    print("rendered", img["name"], file=sys.stderr, flush=True)
"""

# Runs the real cmd_spend_ok in its own process, as the daemon does, so the test
# can SIGTERM it mid-batch the way iris.py's cap does.
DRIVER = """
import importlib.util, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("po", sys.argv[1])
po = importlib.util.module_from_spec(spec); spec.loader.exec_module(po)
exec(open(sys.argv[2]).read())  # the shared setup below
po.install_agent_reaper()
with po.StateFile(int(sys.argv[3])) as sf:
    sf.load()
    sys.exit(po.cmd_spend_ok(sf, force=True))
"""

SETUP = """
tmp = Path({tmp!r})
po.WORKSPACE_DIR = tmp / "vault"
po.STATE_DIR = po.WORKSPACE_DIR / po.VAULT_REL / "Production_Kits"
po.LOCK_DIR = tmp / "locks"
po.AGENT_LOG_DIR = tmp / "logs"
po.PYTHON = {stub!r}
po.notify = lambda *a, **k: None
po._run_manifest_spine_lint = lambda *a, **k: None
po.thumbnail_brief_paths = lambda *a, **k: ["stub-brief"]
"""


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.tmp)]))
        self.stub = self.tmp / "stub_batch"
        self.stub.write_text(STUB)
        self.stub.chmod(0o755)
        self.setup_py = self.tmp / "setup.py"
        self.setup_py.write_text(SETUP.format(tmp=str(self.tmp), stub=str(self.stub)))
        exec(self.setup_py.read_text(), {"po": po, "Path": Path})
        man = po.vault_abs(po.canonical_manifest_rel(VIDEO))
        man.parent.mkdir(parents=True)
        man.write_text(json.dumps({"images": [
            {"name": n, "prompt": "stub", "use_references": False} for n in SHOTS]}))
        po.STATE_DIR.mkdir(parents=True)
        stages = po.default_stages()
        stages["2_review"]["status"] = "done"
        self.state = po.STATE_DIR / f"Video_{VIDEO}_pipeline.json"
        self.state.write_text(json.dumps({"video": VIDEO, "stages": stages}))
        self.hd = po._hd_dir(VIDEO)

    def stage(self):
        return json.loads(self.state.read_text())["stages"]["5_images"]

    def wait_for(self, pred, secs=20.0):
        end = time.time() + secs
        while time.time() < end:
            if pred():
                return True
            time.sleep(0.05)
        return False

    def reconcile(self):
        with po.StateFile(VIDEO) as sf:
            sf.load()
            reset = po.reconcile_orphans(sf.data["stages"], VIDEO)
            sf.save()
        return reset


class TestDaemonPath(_Base):
    """iris.py SIGTERMs the orchestrator at its cap while the batch runs."""

    def test_batch_survives_the_cap_and_ends_done(self):
        env = {**os.environ, "STUB_DT": "0.4"}          # ~2.4 s batch
        driver = self.tmp / "driver.py"
        driver.write_text(DRIVER)
        orch = subprocess.Popen(
            [sys.executable, str(driver), str(MODULE), str(self.setup_py), str(VIDEO)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.addCleanup(lambda: orch.poll() is None and (orch.kill(), orch.wait()))
        self.assertTrue(self.wait_for(
            lambda: self.stage().get("pid") not in (None, orch.pid)),
            "the batch's own pid must be recorded in 5_images before the wait")
        batch_pid = self.stage()["pid"]
        self.addCleanup(lambda: po._pid_alive(batch_pid) and os.kill(batch_pid, 9))
        self.assertEqual(self.stage()["status"], "running")
        self.assertEqual(self.stage()["pid_start_token"], po._proc_start_token(batch_pid))

        orch.terminate()                                  # the daemon's cap
        orch.communicate(timeout=10)                      # the daemon closes its pipes
        self.assertEqual(orch.returncode, -signal.SIGTERM)
        self.assertTrue(po._pid_alive(batch_pid), "the batch must outlive its launcher")
        self.assertLess(len(list(self.hd.glob("*.png"))), len(SHOTS),
                        "fixture too fast: the cap must land mid-batch")

        # While it runs, nobody may start a second one or call it orphaned.
        with self.assertRaises(po.LiveRunError) as cm:
            self.reconcile()
        self.assertIn(f"kill {batch_pid}", str(cm.exception))
        self.assertNotIn("--force-reset", str(cm.exception), "force-reset refuses a live batch")

        self.assertTrue(self.wait_for(lambda: not po._pid_alive(batch_pid)))
        self.assertEqual(sorted(p.stem for p in self.hd.glob("*.png")), SHOTS,
                         "every shot rendered: no BrokenPipe after the parent died")
        log = (po.AGENT_LOG_DIR / f"Video_{VIDEO}_generate_images.log").read_text()
        self.assertIn(f"rendered {SHOTS[-1]}", log)

        self.assertEqual(self.reconcile(), ["5_images"])
        st = self.stage()
        self.assertEqual(st["status"], "done")
        self.assertIsNone(st["pid"])

    def test_batch_survives_a_kill_of_the_whole_process_group(self):
        """launchd tears down com.iris.studio (kickstart -k) by killing its process
        group, and Ctrl-C hits a CLI spend-ok's group. The batch has its own session
        (start_new_session), so neither reaches it."""
        env = {**os.environ, "STUB_DT": "0.4"}
        driver = self.tmp / "driver.py"
        driver.write_text(DRIVER)
        orch = subprocess.Popen(
            [sys.executable, str(driver), str(MODULE), str(self.setup_py), str(VIDEO)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
            start_new_session=True)                      # its own group, as under launchd
        self.addCleanup(lambda: orch.poll() is None and (orch.kill(), orch.wait()))
        self.assertTrue(self.wait_for(
            lambda: self.stage().get("pid") not in (None, orch.pid)))
        batch_pid = self.stage()["pid"]
        self.addCleanup(lambda: po._pid_alive(batch_pid) and os.kill(batch_pid, 9))
        os.killpg(orch.pid, signal.SIGKILL)
        orch.wait(timeout=10)
        time.sleep(0.2)
        self.assertTrue(po._pid_alive(batch_pid), "a group kill must not reach the batch")
        self.assertTrue(self.wait_for(lambda: not po._pid_alive(batch_pid)))
        self.assertEqual(self.reconcile(), ["5_images"])
        self.assertEqual(self.stage()["status"], "done")

    def test_batch_that_dies_part_way_goes_ready(self):
        # A batch gone with shots missing is resumable, not done.
        stages = json.loads(self.state.read_text())
        dead = subprocess.Popen(["true"]); dead.wait()
        stages["stages"]["5_images"].update(status="running", pid=dead.pid,
                                            started_at=po.now_iso())
        self.state.write_text(json.dumps(stages))
        self.hd.mkdir(parents=True)
        (self.hd / f"{SHOTS[0]}.png").write_bytes(b"x")
        self.assertEqual(self.reconcile(), ["5_images"])
        st = self.stage()
        self.assertEqual(st["status"], "ready")
        self.assertIn(f"{len(SHOTS) - 1} shot(s) unrendered", st["note"])

    def test_entries_generate_images_skips_do_not_block_done(self):
        # generate_images skips entries without a prompt and unsafe names, and a
        # non-dict entry aborts its batch, so none of them ever gets a PNG; closing
        # the batch must not wait for them.
        man = po.vault_abs(po.canonical_manifest_rel(VIDEO))
        doc = json.loads(man.read_text())
        doc["images"] += ["junk", {"name": "No_Prompt"}, {"name": ".hidden", "prompt": "p"},
                          {"name": "a/b", "prompt": "p"}, {"name": "", "prompt": "p"}]
        man.write_text(json.dumps(doc))
        self.hd.mkdir(parents=True)
        for n in SHOTS:
            (self.hd / f"{n}.png").write_bytes(b"x")
        self.assertEqual(po._batch_pngs_missing(VIDEO, json.loads(
            self.state.read_text())["stages"]), 0)

    def _close(self, pngs):
        sent = []
        stages = json.loads(self.state.read_text())["stages"]
        stages["5_images"].update(status="running", pid=None)
        self.hd.mkdir(parents=True)
        for n in SHOTS[:pngs]:
            (self.hd / f"{n}.png").write_bytes(b"x")
        with mock.patch.object(po, "notify", lambda m, force=False: sent.append((m, force))), \
                mock.patch.object(po, "SUPPRESS_NOTIFY", True):
            po._close_batch(stages["5_images"], stages, VIDEO, "pid-dead")
        return stages["5_images"], sent

    def test_close_pings_through_a_sweep_when_done(self):
        # The sweep sets SUPPRESS_NOTIFY; after the daemon cap this ping is the only
        # word Steve gets that the batch finished, so it must be forced.
        st, sent = self._close(len(SHOTS))
        self.assertEqual(st["status"], "done")
        self.assertEqual(len(sent), 1, sent)
        self.assertTrue(sent[0][1], "the close ping must be force=True")
        self.assertIn("GENERATED", sent[0][0])

    def test_close_pings_through_a_sweep_when_ready(self):
        st, sent = self._close(2)
        self.assertEqual(st["status"], "ready")
        self.assertEqual(len(sent), 1, sent)
        self.assertTrue(sent[0][1], "the close ping must be force=True")
        self.assertIn(f"{len(SHOTS) - 2} shot(s) unrendered", sent[0][0])
        self.assertIn(f"/pipeline {VIDEO} spend-ok", sent[0][0])


class TestSpendWatch(unittest.TestCase):
    """spend_watch.sh must not call a detached batch's clean finish a crash: after
    the daemon cap the batch exits while stage 5 still says 'running' until the
    next sweep closes it (review M1, 2026-10-10)."""

    def test_running_with_no_process_waits_for_the_sweep(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(tmp)]))
        state = tmp / "state.json"
        state.write_text(json.dumps({"stages": {"5_images": {"status": "running"}}}))
        script = (REPO / "scripts" / "spend_watch.sh").read_text()
        for old, new in [
                ('STATE="/Users/steve/Documents/3SK/outputs/BRANDS/3SK_Finance/'
                 'Production_Kits/Video_${NN}_pipeline.json"', f'STATE="{state}"'),
                ("POLL=30 ", "POLL=1 "), ("GRACE=120 ", "GRACE=10 ")]:
            self.assertIn(old, script)
            script = script.replace(old, new)
        watch = tmp / "spend_watch.sh"
        watch.write_text(script)
        alerts = tmp / "alerts"
        (tmp / "notify.sh").write_text(f'#!/bin/sh\necho "$1" >> {alerts}\n')
        (tmp / "notify.sh").chmod(0o755)
        # A process the watcher's pattern matches, gone after ~2 s.
        fake = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(2)",
                                 f"image_factory/generate_images.py x Video_{VIDEO}_HD"])
        self.addCleanup(lambda: fake.poll() is None and (fake.kill(), fake.wait()))
        w = subprocess.Popen(["bash", str(watch), str(VIDEO)],
                             env={**os.environ, "HOME": str(tmp)})
        self.addCleanup(lambda: w.poll() is None and (w.kill(), w.wait()))
        fake.wait(timeout=10)
        time.sleep(3)                                    # watcher sees it gone, still 'running'
        self.assertIsNone(w.poll(), "must keep watching, not report a crash")
        state.write_text(json.dumps({"stages": {"5_images": {"status": "done"}}}))
        self.assertEqual(w.wait(timeout=10), 0)
        self.assertFalse(alerts.exists(), alerts.exists() and alerts.read_text())


class TestCliPath(_Base):
    """pipeline_orchestrator.py --video N --spend-ok, in-process."""

    def _spend(self):
        with po.StateFile(VIDEO) as sf:
            sf.load()
            return po.cmd_spend_ok(sf, force=True)

    def test_timeout_covers_the_measured_batches(self):
        # Longest measured flagship batch: 123 min (V12, cost_ledger.jsonl).
        self.assertGreaterEqual(po.RUN_TABLE["5_images"]["timeout"], 123 * 60 * 1.5)

    def test_full_batch_ends_done(self):
        with mock.patch.dict(os.environ, {"STUB_DT": "0.05"}):
            self.assertEqual(self._spend(), 0)
        st = self.stage()
        self.assertEqual(st["status"], "done")
        self.assertIsNone(st["pid"])
        self.assertNotIn("exited", st["note"], "success must not read as a failure")
        self.assertEqual(len(list(self.hd.glob("*.png"))), len(SHOTS))

    def test_batch_past_its_timeout_is_stopped_and_resumable(self):
        with mock.patch.dict(os.environ, {"STUB_DT": "2"}), \
                mock.patch.dict(po.RUN_TABLE["5_images"], {"timeout": 1}):
            self.assertEqual(self._spend(), 2)
        st = self.stage()
        self.assertNotEqual(st["status"], "done")
        self.assertNotEqual(st["status"], "running")
        self.assertIn("timed out", st["note"])
        self.assertIsNone(st["pid"])


class TestOwnership(_Base):
    """A live batch is never left behind a stage that says it is not running."""

    def _live_batch(self, started_at):
        self.hd.mkdir(parents=True)
        proc = subprocess.Popen([str(self.stub), "x", str(po.vault_abs(
            po.canonical_manifest_rel(VIDEO))), "--output", str(self.hd)],
            env={**os.environ, "STUB_DT": "5"}, start_new_session=True,
            stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: proc.poll() is None and (proc.kill(), proc.wait()))
        self.assertTrue(self.wait_for(lambda: po._proc_start_token(proc.pid)))
        doc = json.loads(self.state.read_text())
        doc["stages"]["5_images"].update(
            status="running", pid=proc.pid, started_at=started_at,
            pid_start_token=po._proc_start_token(proc.pid))
        self.state.write_text(json.dumps(doc))
        return proc

    def test_reconcile_stops_a_batch_past_its_timeout(self):
        proc = self._live_batch("2020-01-01T00:00:00Z")
        self.assertEqual(self.reconcile(), ["5_images"])
        self.assertEqual(proc.wait(timeout=10), -signal.SIGTERM)
        self.assertEqual(self.stage()["status"], "ready")

    def test_reconcile_leaves_a_recycled_pid_alone(self):
        # Same pid, different start token: someone else's process. Never signal it.
        proc = self._live_batch("2020-01-01T00:00:00Z")
        doc = json.loads(self.state.read_text())
        doc["stages"]["5_images"]["pid_start_token"] = "Thu Jan  1 00:00:00 1970"
        self.state.write_text(json.dumps(doc))
        self.reconcile()
        self.assertIsNone(proc.poll(), "a recycled pid must not be signalled")
        self.assertEqual(self.stage()["status"], "ready")

    def test_force_reset_refuses_a_live_batch(self):
        proc = self._live_batch("2020-01-01T00:00:00Z")   # past timeout, still live
        with po.StateFile(VIDEO) as sf:
            sf.load()
            self.assertEqual(po.cmd_force_reset(sf), 2)
        self.assertEqual(self.stage()["status"], "running")
        self.assertIsNone(proc.poll(), "force-reset must never kill")

    def _force_reset(self):
        with po.StateFile(VIDEO) as sf:
            sf.load()
            return po.cmd_force_reset(sf)

    def _set(self, key, **fields):
        doc = json.loads(self.state.read_text())
        doc["stages"][key].update(**fields)
        self.state.write_text(json.dumps(doc))

    def test_force_reset_closes_a_recycled_batch(self):
        proc = self._live_batch(po.now_iso())
        self._set("5_images", pid_start_token="Thu Jan  1 00:00:00 1970")
        self.assertEqual(self._force_reset(), 0)
        self.assertEqual(self.stage()["status"], "ready")
        self.assertIsNone(proc.poll(), "a recycled pid must not be signalled")

    def test_force_reset_marks_a_finished_batch_done(self):
        dead = subprocess.Popen(["true"]); dead.wait()
        self._set("5_images", status="running", pid=dead.pid, started_at=po.now_iso())
        self.hd.mkdir(parents=True)
        for n in SHOTS:
            (self.hd / f"{n}.png").write_bytes(b"x")
        self.assertEqual(self._force_reset(), 0)
        self.assertEqual(self.stage()["status"], "done")

    def test_force_reset_of_another_stage_is_unchanged(self):
        dead = subprocess.Popen(["true"]); dead.wait()
        self._set("7_packaging", status="running", pid=dead.pid, started_at=po.now_iso())
        self.assertEqual(self._force_reset(), 0)
        st = json.loads(self.state.read_text())["stages"]["7_packaging"]
        self.assertEqual(st["status"], "ready")
        self.assertTrue(st["note"].startswith("manual --force-reset"), st["note"])
        self.assertNotIn("image batch", st["note"])

    def test_looks_orphaned_agrees_with_reconcile_on_a_live_batch(self):
        self._live_batch(po.now_iso())
        self.assertFalse(po._looks_orphaned(self.stage(), "5_images"))


if __name__ == "__main__":
    unittest.main(verbosity=1)
