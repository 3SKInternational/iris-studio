"""rotate-iris-log.sh (A-118): trims the claude-remote logs in place.

Runs the REAL script with LOGDIR pointed at a temp dir. Files are real-sized
(the threshold is 50 MiB) so the boundary itself is tested.
"""
import os
import pathlib
import subprocess
import tempfile
import unittest

# RIL_UNDER_TEST points at a mutant copy for hand mutation runs.
SCRIPT = pathlib.Path(os.environ.get("RIL_UNDER_TEST") or
                      pathlib.Path(__file__).resolve().parent.parent / "scripts" / "rotate-iris-log.sh")
MAX = 50 * 1024 * 1024
URL_RE = r"https://claude\.ai/code\?environment=[a-zA-Z0-9_]+"
SPIN = b"Reconnecting to the remote-control relay... retrying\n"  # 53 bytes


REMOTE_URL = SCRIPT.resolve().parent / "remote-url.sh"


def last_url(path):
    """What the real remote-url.sh prints for this log ('' when it finds none)."""
    r = subprocess.run(["/bin/bash", str(REMOTE_URL)], env=dict(os.environ, REMOTE_LOG=str(path)),
                       capture_output=True, text=True, timeout=30)
    assert r.returncode in (0, 1), r.stderr  # 127: no remote-url.sh beside SCRIPT
    return r.stdout.strip()


class TestRotate(unittest.TestCase):
    def setUp(self):
        self.d = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.d)])
        self.remote = self.d / "claude-remote.log"
        self.stdout = self.d / "claude-remote.stdout.log"

    def run_script(self):
        r = subprocess.run(["/bin/bash", str(SCRIPT)], env=dict(os.environ, LOGDIR=str(self.d)),
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def big(self, path, head=b"", tail=b"", size=MAX + 1):
        """head + spinner filler + tail, exactly `size` bytes."""
        fill = size - len(head) - len(tail)
        with open(path, "wb") as f:
            f.write(head)
            f.write(SPIN * (fill // len(SPIN)))
            f.write(b"x" * (fill % len(SPIN)))
            f.write(tail)

    def test_url_far_from_end_survives_trim(self):
        old = b"https://claude.ai/code?environment=env_OLD1\n"
        new = b"Open: https://claude.ai/code?environment=env_NEW2 in a browser\n"
        self.big(self.remote, head=old + SPIN * 10 + new)
        ino = self.remote.stat().st_ino
        self.run_script()
        self.assertEqual(self.remote.stat().st_ino, ino)
        self.assertLess(self.remote.stat().st_size, 1024 * 1024)
        self.assertLessEqual(len(self.remote.read_bytes().splitlines()), 5001)
        self.assertEqual(last_url(self.remote), "https://claude.ai/code?environment=env_NEW2")
        self.assertEqual(self.remote.read_bytes().count(b"environment="), 1)  # only the newest
        self.assertEqual(sorted(p.name for p in self.d.iterdir()), ["claude-remote.log"])

    def test_url_inside_last_lines_not_duplicated(self):
        url = b"https://claude.ai/code?environment=env_NEAR3"
        self.big(self.remote, tail=b"Open " + url + b"\n" + SPIN * 28)
        self.run_script()
        body = self.remote.read_bytes()
        self.assertEqual(body.count(url), 1)
        self.assertEqual(len(body.splitlines()), 5000)
        self.assertEqual(last_url(self.remote), url.decode())

    def test_nul_near_top_does_not_hide_url(self):
        url = b"https://claude.ai/code?environment=env_NUL4"
        self.big(self.remote, head=b"\x00garbage\n" + url + b"\n")
        self.run_script()
        body = self.remote.read_bytes()
        self.assertEqual(body.count(b"environment="), 1)
        self.assertTrue(body.endswith(url + b"\n"), body[-120:])

    def test_remote_url_reads_past_a_nul(self):
        # BSD grep without -a prints "Binary file matches" instead of the URL (A-121)
        self.remote.write_bytes(b"https://claude.ai/code?environment=env_OLD5\n\x00garbage\n"
                                b"Open https://claude.ai/code?environment=env_NUL5\n")
        self.assertEqual(last_url(self.remote), "https://claude.ai/code?environment=env_NUL5")

    def test_remote_url_says_when_no_url(self):
        # set -e used to end it silently on grep's no-match exit
        self.remote.write_bytes(SPIN)
        r = subprocess.run(["/bin/bash", str(REMOTE_URL)], env=dict(os.environ, REMOTE_LOG=str(self.remote)),
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 1)
        self.assertIn("No URL found", r.stderr)

    def test_no_url_anywhere(self):
        self.big(self.remote)
        self.run_script()
        self.assertEqual(len(self.remote.read_bytes().splitlines()), 5000)
        self.assertEqual(last_url(self.remote), "")

    def test_big_stdout_emptied_in_place(self):
        self.big(self.stdout)
        ino = self.stdout.stat().st_ino
        self.run_script()
        self.assertEqual(self.stdout.stat().st_size, 0)
        self.assertEqual(self.stdout.stat().st_ino, ino)

    def test_at_threshold_left_alone(self):
        self.big(self.remote, size=MAX)
        self.big(self.stdout, size=MAX)
        self.run_script()
        self.assertEqual(self.remote.stat().st_size, MAX)
        self.assertEqual(self.stdout.stat().st_size, MAX)

    def test_missing_or_empty_err_log_still_trims_remote(self):
        for err in (None, b""):
            if err is not None:
                (self.d / "iris.err.log").write_bytes(err)
            self.big(self.remote)
            self.big(self.stdout)
            self.run_script()
            self.assertEqual(len(self.remote.read_bytes().splitlines()), 5000)
            self.assertEqual(self.stdout.stat().st_size, 0)
            self.assertEqual(list(self.d.glob("iris.err.log.*.gz")), [])

    def test_err_log_still_rotated(self):
        err = self.d / "iris.err.log"
        err.write_text("boom\n")
        out = self.run_script()
        self.assertEqual(err.stat().st_size, 0)
        gz = list(self.d.glob("iris.err.log.*.gz"))
        self.assertEqual(len(gz), 1)
        self.assertIn("rotated iris.err.log", out)
        self.assertFalse(self.remote.exists())


if __name__ == "__main__":
    unittest.main()
