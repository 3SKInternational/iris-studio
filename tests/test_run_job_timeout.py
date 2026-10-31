"""run_job.sh wall-clock cap (A-104).

Since A-99 a job lock is only stolen from a dead PID (or after 24 h), so a hung
run_job.sh job would make every later fire log "already running" for a day with
no alert. These tests run a COPY of the wrapper (beside a fake notify.sh, the
real retry_queue.sh and claude_token.sh, and an iris.py sentinel) with stub
jobs. Job name WRAPPER-SELFTEST is the convention the fleet-liveness check
already ignores.
"""
import os
import pathlib
import shutil
import signal
import subprocess
import tempfile
import time
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / "scripts"
# RJ_UNDER_TEST points at a mutant copy; never mutate the live script, because
# running launchd wrappers read it by byte offset.
WRAPPER = pathlib.Path(os.environ.get("RJ_UNDER_TEST") or SCRIPTS / "run_job.sh")
JOB = "WRAPPER-SELFTEST"
LOG = pathlib.Path.home() / "iris_studio" / "logs" / f"job-{JOB}.log"


class TestRunJobTimeout(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        ws = self.tmp / "ws"
        scripts = ws / "scripts"
        scripts.mkdir(parents=True)
        (ws / "iris.py").write_text("")  # the AI_Workspace mount sentinel
        shutil.copy(WRAPPER, scripts / "run_job.sh")
        for f in ("retry_queue.sh", "claude_token.sh"):
            shutil.copy(SCRIPTS / f, scripts / f)
        self.alerts = self.tmp / "alerts.txt"
        self.alerts.write_text("")
        notify = scripts / "notify.sh"
        notify.write_text(f'#!/bin/bash\nprintf "%s\\n" "$*" >> "{self.alerts}"\n')
        notify.chmod(0o755)
        self.wrapper = scripts / "run_job.sh"
        self.env = dict(os.environ, RQ_BASE=str(self.tmp / "rq"),
                        JOB_TIMEOUT_SEC="2", JOB_KILL_GRACE_SEC="1",
                        JOB_QUIET_OK="1", CLAUDE_CODE_OAUTH_TOKEN="selftest")
        self.env.pop("IRIS_RETRY", None)

    def run_with(self, body, **env):
        self.log_start = LOG.stat().st_size if LOG.exists() else 0
        t0 = time.time()
        r = subprocess.run(["/bin/bash", str(self.wrapper), JOB, "/bin/bash", "-c", body],
                           env=dict(self.env, **env), capture_output=True, text=True,
                           timeout=60)
        return r.returncode, time.time() - t0

    def new_log(self):
        with open(LOG, "rb") as f:
            f.seek(self.log_start)
            return f.read().decode(errors="replace")

    def marker(self):
        return (self.tmp / "rq" / f"{JOB}.retry").exists()

    def test_hang_that_ignores_term_is_killed(self):
        rc, took = self.run_with("trap '' TERM ALRM; sleep 120")
        self.assertEqual(rc, 124)
        self.assertLess(took, 20)
        self.assertIn("timed out after 2s (job hung, killed)", self.alerts.read_text())
        self.assertTrue(self.marker())
        self.assertIn(f"'{JOB}' TIMED OUT after 2s", self.new_log())

    def test_retry_run_timeout_records_but_does_not_alert(self):
        rc, _ = self.run_with("sleep 120", IRIS_RETRY="1")
        self.assertEqual(rc, 124)
        self.assertTrue(self.marker())
        self.assertNotIn("timed out", self.alerts.read_text())

    def test_exits_early_when_term_works(self):
        rc, took = self.run_with("sleep 120", JOB_KILL_GRACE_SEC="10")
        self.assertEqual(rc, 124)
        self.assertLess(took, 8)
        self.assertIn("TIMED OUT after 2s", self.new_log())

    def test_kill_waits_for_grace(self):
        rc, took = self.run_with("trap '' TERM; sleep 120", JOB_KILL_GRACE_SEC="4")
        self.assertEqual(rc, 124)
        self.assertGreaterEqual(took, 5.5)

    def test_children_get_term_first(self):
        got = self.tmp / "got"
        rc, _ = self.run_with(
            f"bash -c 'trap \"echo TERM > {got}; exit\" TERM; sleep 120 & wait' & wait")
        self.assertEqual(rc, 124)
        self.assertEqual(got.read_text().strip(), "TERM")

    def test_orphan_that_ignores_term_is_still_killed(self):
        # the job dies on TERM, its child does not and reparents, so it is only
        # reachable through the tree listed before TERM
        pidf = self.tmp / "pid"
        rc, took = self.run_with(f"bash -c \"trap '' TERM; echo \\$\\$ > {pidf}; sleep 120\" & wait")
        self.assertEqual(rc, 124)
        self.assertLess(took, 20)
        time.sleep(0.3)
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pidf.read_text()), 0)

    def test_grandchild_that_ignores_term_is_killed(self):
        # two levels below the job (perl -> drive-sync.sh -> rclone shape): only a
        # recursive tree walk reaches it
        pidf = self.tmp / "pidc"
        rc, _ = self.run_with(
            f"bash -c \"bash -c \\\"trap '' TERM; sleep 120 & echo \\\\\\$! > {pidf}; wait\\\"; :\"")
        self.assertEqual(rc, 124)
        time.sleep(0.3)
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pidf.read_text()), 0)

    def test_exec_failure_says_why(self):
        self.log_start = LOG.stat().st_size if LOG.exists() else 0
        r = subprocess.run(["/bin/bash", str(self.wrapper), JOB, "/nonexistent/x"],
                           env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 127)
        self.assertIn("cannot exec /nonexistent/x: No such file or directory", self.new_log())

    def test_single_argument_command_is_not_split(self):
        self.log_start = LOG.stat().st_size if LOG.exists() else 0
        r = subprocess.run(["/bin/bash", str(self.wrapper), JOB, "echo split me"],
                           env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 127)

    def test_killed_wrapper_group_takes_job_down(self):
        # launchd stops a job by killing its process group; the job must be in it
        pidf = self.tmp / "pid"
        w = subprocess.Popen(["/bin/bash", str(self.wrapper), JOB, "/bin/bash", "-c",
                              f"echo $$ > {pidf}; sleep 120"],
                             env=dict(self.env, JOB_TIMEOUT_SEC="60"),
                             start_new_session=True, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        for _ in range(100):
            if pidf.exists() and pidf.read_text().strip():
                break
            time.sleep(0.05)
        job = int(pidf.read_text())
        os.killpg(w.pid, signal.SIGKILL)
        w.wait()
        time.sleep(0.5)
        try:
            os.kill(job, 0)
            st = subprocess.run(["ps", "-o", "stat=", "-p", str(job)],
                                capture_output=True, text=True).stdout.strip()
            alive = bool(st) and not st.startswith("Z")
        except ProcessLookupError:
            alive = False
        if alive:
            os.kill(job, signal.SIGKILL)
        self.assertFalse(alive, "job outlived its killed wrapper group")

    def test_bad_values_fall_back(self):
        rc, _ = self.run_with("sleep 3", JOB_TIMEOUT_SEC="2h", JOB_KILL_GRACE_SEC="abc")
        self.assertEqual(rc, 0)
        log = self.new_log()
        self.assertIn("bad JOB_TIMEOUT_SEC '2h'", log)
        self.assertIn("bad JOB_KILL_GRACE_SEC 'abc'", log)

    def test_zero_timeout_falls_back(self):
        rc, _ = self.run_with("true", JOB_TIMEOUT_SEC="0")
        self.assertEqual(rc, 0)
        self.assertIn("bad JOB_TIMEOUT_SEC '0'", self.new_log())

    def test_leading_zero_values_are_decimal(self):
        # "010" and "020" are octal 8 and 16 to bash; they must mean 10 and 20 s.
        # ("08" can't catch a missing 10#: bash errors, keeps the string, and perl
        # reads it as 8 anyway.)
        rc, took = self.run_with("trap '' TERM; sleep 120",
                                 JOB_TIMEOUT_SEC="010", JOB_KILL_GRACE_SEC="020")
        self.assertEqual(rc, 124)
        self.assertGreaterEqual(took, 31)  # 10 + 20 + startup; octal 16 grace ends near 29
        self.assertIn("TIMED OUT after 10s", self.new_log())

    def test_job_exit_124_before_cap_is_not_a_timeout(self):
        rc, _ = self.run_with("exit 124")
        self.assertEqual(rc, 124)
        self.assertNotIn("TIMED OUT", self.new_log())
        self.assertNotIn("timed out", self.alerts.read_text())
        self.assertIn("exit code 124", self.alerts.read_text())

    def test_fast_job_is_not_touched(self):
        rc, _ = self.run_with('echo hel""lo')  # the log's start line shows hel""lo
        self.assertEqual(rc, 0)
        self.assertFalse(self.marker())
        self.assertIn("hello", self.new_log())

    def test_job_just_inside_cap_finishes(self):
        rc, _ = self.run_with("sleep 1")
        self.assertEqual(rc, 0)

    def test_nonzero_exit_code_kept(self):
        rc, _ = self.run_with("echo boom; exit 3")
        self.assertEqual(rc, 3)
        self.assertTrue(self.marker())

    def test_tempfail_75_still_silent(self):
        rc, _ = self.run_with("exit 75")
        self.assertEqual(rc, 0)
        self.assertEqual(self.alerts.read_text(), "")

    def test_signal_death_maps_to_128_plus(self):
        rc, _ = self.run_with("kill -KILL $$")
        self.assertEqual(rc, 137)


if __name__ == "__main__":
    unittest.main()
