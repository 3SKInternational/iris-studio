"""run_claude_job.sh wall-clock cap (2026-10-25).

The 2026-10-18 security-audit CLI hung for 7 days: it ignored ALRM and TERM,
the wrapper waited forever, and nothing alerted. These tests run the REAL
wrapper with a stub CLI and check that a hang is killed and fails loudly while
normal exits keep their codes. Job name WRAPPER-SELFTEST is the convention the
fleet-liveness check already ignores.
"""
import os
import pathlib
import signal
import subprocess
import tempfile
import time
import unittest

# RCJ_UNDER_TEST points at a copy for mutation runs; never mutate the live script,
# because running launchd wrappers read it by byte offset.
WRAPPER = pathlib.Path(os.environ.get("RCJ_UNDER_TEST") or
                       pathlib.Path(__file__).resolve().parent.parent / "scripts" / "run_claude_job.sh")
JOB = "WRAPPER-SELFTEST"
LOG = pathlib.Path.home() / "iris_studio" / "logs" / f"claude-code-{JOB}.log"


class TestClaudeJobTimeout(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.alerts = self.tmp / "alerts.txt"
        notify = self.tmp / "notify.sh"
        notify.write_text(f'#!/bin/bash\nprintf "%s\\n" "$*" >> "{self.alerts}"\n')
        notify.chmod(0o755)
        self.prompt = self.tmp / "p.prompt"
        self.prompt.write_text("selftest")
        self.env = dict(os.environ, NOTIFY_BIN=str(notify), RQ_BASE=str(self.tmp / "rq"),
                        CLAUDE_JOB_TIMEOUT_SEC="2", CLAUDE_JOB_KILL_GRACE_SEC="1",
                        JOB_QUIET_OK="1")
        self.env.pop("IRIS_RETRY", None)

    def run_with(self, body):
        stub = self.tmp / "claude"
        stub.write_text("#!/bin/bash\n" + body + "\n")
        stub.chmod(0o755)
        env = dict(self.env, CLAUDE_BIN=str(stub))
        self.log_start = LOG.stat().st_size if LOG.exists() else 0
        t0 = time.time()
        r = subprocess.run(["/bin/bash", str(WRAPPER), JOB, str(self.prompt)],
                           env=env, capture_output=True, text=True, timeout=60)
        return r.returncode, time.time() - t0

    def new_log(self):
        """Only what this run appended to the shared selftest log."""
        with open(LOG, "rb") as f:
            f.seek(self.log_start)
            return f.read().decode(errors="replace")

    def marker(self):
        return (self.tmp / "rq" / f"{JOB}.retry").exists()

    def test_hang_that_ignores_term_is_killed(self):
        rc, took = self.run_with("trap '' TERM ALRM; sleep 120")
        self.assertEqual(rc, 124)
        self.assertLess(took, 20)
        self.assertIn("timed out after 2s", self.alerts.read_text())
        self.assertTrue(self.marker())
        self.assertIn(f"'{JOB}' TIMED OUT after 2s", self.new_log())

    def test_exits_early_when_term_works(self):
        # the stub dies on TERM, so the wrapper must not sit out the 10 s grace
        self.env["CLAUDE_JOB_KILL_GRACE_SEC"] = "10"
        rc, took = self.run_with("sleep 120")
        self.assertEqual(rc, 124)
        self.assertLess(took, 8)

    def test_kill_waits_for_grace(self):
        self.env["CLAUDE_JOB_KILL_GRACE_SEC"] = "4"
        rc, took = self.run_with("trap '' TERM; sleep 120")
        self.assertEqual(rc, 124)
        self.assertGreaterEqual(took, 5.5)

    def test_children_get_term_first(self):
        # MCP servers deserve a clean shutdown, not just the KILL
        got = self.tmp / "got"
        rc, _ = self.run_with(
            f"bash -c 'trap \"echo TERM > {got}; exit\" TERM; sleep 120 & wait' & wait")
        self.assertEqual(rc, 124)
        self.assertEqual(got.read_text().strip(), "TERM")

    def test_orphan_that_ignores_term_is_still_killed(self):
        # the CLI dies on TERM, its child does not and reparents to launchd, so
        # it is only reachable through the tree listed before TERM
        rc, took = self.run_with("bash -c \"trap '' TERM; sleep 120\" & wait")
        self.assertEqual(rc, 124)
        self.assertLess(took, 20)

    def test_killed_wrapper_group_takes_cli_down(self):
        # launchd stops a job by killing its process group; the CLI must be in it
        pidf = self.tmp / "pid"
        stub = self.tmp / "claude"
        stub.write_text(f"#!/bin/bash\necho $$ > {pidf}\nsleep 120\n")
        stub.chmod(0o755)
        env = dict(self.env, CLAUDE_BIN=str(stub), CLAUDE_JOB_TIMEOUT_SEC="60")
        w = subprocess.Popen(["/bin/bash", str(WRAPPER), JOB, str(self.prompt)], env=env,
                             start_new_session=True, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        for _ in range(100):
            if pidf.exists() and pidf.read_text().strip():
                break
            time.sleep(0.05)
        cli = int(pidf.read_text())
        os.killpg(w.pid, signal.SIGKILL)
        w.wait()
        time.sleep(0.5)
        try:
            os.kill(cli, 0)
            alive = subprocess.run(["ps", "-o", "stat=", "-p", str(cli)],
                                   capture_output=True, text=True).stdout.strip()
            alive = bool(alive) and not alive.startswith("Z")
        except ProcessLookupError:
            alive = False
        if alive:
            os.kill(cli, signal.SIGKILL)
        self.assertFalse(alive, "CLI outlived its killed wrapper group")

    def test_bad_timeout_value_falls_back(self):
        self.env["CLAUDE_JOB_TIMEOUT_SEC"] = "2h"
        self.env["CLAUDE_JOB_KILL_GRACE_SEC"] = "abc"
        rc, _ = self.run_with("sleep 3; echo ROUTINE_COMPLETE")
        self.assertEqual(rc, 0)
        log = self.new_log()
        self.assertIn("bad CLAUDE_JOB_TIMEOUT_SEC '2h'", log)
        self.assertIn("bad CLAUDE_JOB_KILL_GRACE_SEC 'abc'", log)

    # --- A-110: background-wait ceiling reaches the CLI ---------------------
    def bg_wait_seen(self, **env):
        seen = self.tmp / "bgwait"
        self.env.pop("CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS", None)
        self.env.update(env)
        rc, _ = self.run_with(
            f'printf "%s" "${{CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS-unset}}" > {seen}; echo ROUTINE_COMPLETE')
        self.assertEqual(rc, 0)
        return seen.read_text()

    def test_bg_wait_default_is_45_min(self):
        self.assertEqual(self.bg_wait_seen(), "2700000")

    def test_bg_wait_plist_override_kept(self):
        self.assertEqual(self.bg_wait_seen(CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS="0"), "0")

    def test_bg_wait_bad_value_falls_back(self):
        self.assertEqual(self.bg_wait_seen(CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS="45m"), "2700000")
        self.assertIn("bad CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS '45m'", self.new_log())

    def test_cli_exit_124_before_cap_is_not_a_timeout(self):
        rc, _ = self.run_with("exit 124")
        self.assertEqual(rc, 124)
        self.assertNotIn("TIMED OUT", self.new_log())
        self.assertNotIn("timed out", self.alerts.read_text())

    def test_fast_job_is_not_touched(self):
        rc, _ = self.run_with("echo ROUTINE_COMPLETE")
        self.assertEqual(rc, 0)
        self.assertFalse(self.marker())

    def test_job_just_inside_cap_finishes(self):
        rc, _ = self.run_with("sleep 1; echo ROUTINE_COMPLETE")
        self.assertEqual(rc, 0)

    def test_nonzero_exit_code_kept(self):
        rc, _ = self.run_with("echo boom; exit 3")
        self.assertEqual(rc, 3)
        self.assertTrue(self.marker())

    def test_signal_death_maps_to_128_plus(self):
        rc, _ = self.run_with("kill -KILL $$")
        self.assertEqual(rc, 137)


if __name__ == "__main__":
    unittest.main()
