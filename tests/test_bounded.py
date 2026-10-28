"""bounded.sh (A-100): a hard deadline for audit probes that hang on iCloud.

On 10-25 the security audit's `fileproviderctl dump` and an `ls` of CloudDocs
hung past 120 s and ignored perl alarm; only kill -9 stopped them. These tests
run the real script with stub commands that ignore TERM and keep stdout open.
"""
import os
import pathlib
import signal
import subprocess
import tempfile
import time
import unittest

SCRIPT = pathlib.Path(os.environ.get("BOUNDED_UNDER_TEST") or
                      pathlib.Path(__file__).resolve().parent.parent / "scripts" / "bounded.sh")


def run(*args, timeout=30):
    t0 = time.time()
    # capture_output waits for EOF on the pipe, so a leftover process holding
    # our stdout shows up as a slow run (or a TimeoutExpired), not a pass.
    r = subprocess.run(["/bin/bash", str(SCRIPT), *args],
                       capture_output=True, text=True, timeout=timeout)
    return r, time.time() - t0


class TestBounded(unittest.TestCase):
    def test_fast_command_passes_output_and_code(self):
        r, took = run("5", "/bin/sh", "-c", "echo hi; echo err >&2; exit 3")
        self.assertEqual(r.returncode, 3)
        self.assertIn("hi", r.stdout)
        self.assertIn("err", r.stdout)
        self.assertNotIn("TIMEOUT", r.stdout)
        self.assertLess(took, 3)

    def test_zero_exit(self):
        r, _ = run("5", "/usr/bin/true")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")

    def test_hang_ignoring_term_is_killed(self):
        # exec keeps the pid and the ignored TERM, like a probe stuck in the kernel.
        r, took = run("2", "/bin/bash", "-c", "trap '' TERM ALRM; echo partial; echo P=$$; exec sleep 60")
        self.assertEqual(r.returncode, 124)
        probe = int(next(l for l in r.stdout.splitlines() if l.startswith("P="))[2:])
        time.sleep(0.3)
        with self.assertRaises(ProcessLookupError, msg="probe ignoring TERM left running"):
            os.kill(probe, 0)
        self.assertIn("partial", r.stdout)
        self.assertIn("TIMEOUT /bin/bash -c", r.stdout)
        # Never before the deadline (SECONDS is whole-second; -ge fired early).
        self.assertGreaterEqual(took, 2.0)
        self.assertLess(took, 6)

    def test_deadline_never_early(self):
        # SECONDS ticks on whole seconds from a random start fraction, so one
        # run catches an early deadline only by luck; five runs catch it reliably.
        for _ in range(5):
            r, took = run("1", "/bin/sleep", "30")
            self.assertEqual(r.returncode, 124)
            self.assertGreaterEqual(took, 1.0)

    def test_timeout_line_starts_its_own_line(self):
        for probe in ("printf partial; exec sleep 30", "echo partial; exec sleep 30"):
            r, _ = run("1", "/bin/sh", "-c", probe)
            self.assertEqual(r.returncode, 124)
            self.assertEqual(r.stdout.splitlines()[:2], ["partial", "TIMEOUT /bin/sh -c " + probe], probe)

    def test_grandchild_holding_stdout_does_not_hold_caller(self):
        # The child leaves a background sleep with stdout open, then exits.
        r, took = run("5", "/bin/bash", "-c", "sleep 30 & echo done")
        self.assertEqual(r.returncode, 0)
        self.assertIn("done", r.stdout)
        self.assertLess(took, 4)

    def test_hung_child_and_its_children_are_all_killed(self):
        r, took = run("2", "/bin/bash", "-c",
                      "trap '' TERM; sleep 60 & echo A=$!; sleep 60 & echo B=$!; echo C=$$; wait")
        self.assertEqual(r.returncode, 124)
        self.assertLess(took, 6)
        pids = [int(l.split("=")[1]) for l in r.stdout.splitlines() if l[:2] in ("A=", "B=", "C=")]
        self.assertEqual(len(pids), 3, r.stdout)
        time.sleep(0.5)
        for pid in pids:
            with self.assertRaises(ProcessLookupError, msg=f"pid {pid} left running"):
                os.kill(pid, 0)

    def test_term_on_wrapper_kills_the_probe(self):
        pidfile = pathlib.Path(tempfile.mkdtemp()) / "pid"
        p = subprocess.Popen(["/bin/bash", str(SCRIPT), "60", "/bin/bash", "-c",
                              f"trap '' TERM; sleep 60 & echo $! > {pidfile}.child; echo $$ > {pidfile}; exec sleep 60"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            if pidfile.exists() and pidfile.read_text().strip():
                break
            time.sleep(0.1)
        pids = [int(pidfile.read_text()), int(pathlib.Path(f"{pidfile}.child").read_text())]
        p.send_signal(signal.SIGTERM)
        self.assertEqual(p.wait(timeout=5), 143)
        time.sleep(0.3)
        for pid in pids:
            with self.assertRaises(ProcessLookupError, msg=f"pid {pid} left running"):
                os.kill(pid, 0)

    def test_fast_exit_leaves_no_wait_for_deadline(self):
        # A long deadline must not delay a fast command.
        r, took = run("60", "/bin/echo", "x")
        self.assertEqual(r.returncode, 0)
        self.assertLess(took, 3)

    def test_stdin_not_inherited(self):
        # Our stdin stays open; a probe that inherited it would block until the deadline.
        p = subprocess.Popen(["/bin/bash", str(SCRIPT), "10", "/bin/cat"],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        try:
            self.assertEqual(p.wait(timeout=4), 0)
        finally:
            p.kill()
            p.stdin.close()
            p.stdout.close()

    def test_bad_usage(self):
        for args in ([], ["5"], ["x", "/usr/bin/true"], ["", "/usr/bin/true"], ["-1", "/usr/bin/true"],
                     ["0", "/usr/bin/true"], ["00", "/usr/bin/true"]):
            r, _ = run(*args)
            self.assertEqual(r.returncode, 2, args)

    def test_leading_zero_is_decimal(self):
        r, _ = run("08", "/bin/echo", "x")
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "x\n", ""))

    def test_temp_file_removed(self):
        tmp = tempfile.mkdtemp()
        env = dict(os.environ, TMPDIR=tmp)
        subprocess.run(["/bin/bash", str(SCRIPT), "5", "/bin/echo", "x"], env=env,
                       capture_output=True, timeout=10)
        subprocess.run(["/bin/bash", str(SCRIPT), "1", "/bin/sleep", "30"], env=env,
                       capture_output=True, timeout=10)
        self.assertEqual(os.listdir(tmp), [])


if __name__ == "__main__":
    unittest.main()
