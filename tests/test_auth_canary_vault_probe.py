"""Tests for auth_canary.sh's vault/mount readability probes.

Regression guard for the 2026-09-05 whole-vault false-green: iCloud evicted the
vault to dataless stubs and on-read materialization deadlocked (EDEADLK), which
`head` reports as "head: Error reading <file>" — a string the old EPERM-only grep
(`Operation not permitted|EPERM|Permission denied`) did not match, so the canary
reported vault=ok while the entire vault was unreadable.

The fix classifies the probe on WHETHER THE READ SUCCEEDED (any non-empty stderr
=> bad) instead of enumerating known-bad error strings. We can't create a real
dataless file on demand, so we use a directory as the probe target: `head -c1 <dir>`
prints a non-EPERM read error to stderr (e.g. "Error reading <dir>" or "Is a
directory", depending on platform) — the same non-EPERM class the OLD grep missed.
So this test FAILS against the old code and PASSES against the fix.
"""
import os
import subprocess
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CANARY = os.path.join(REPO, "scripts", "auth_canary.sh")


def _write_exec(path, body):
    with open(path, "w") as f:
        f.write(body)
    os.chmod(path, 0o755)


def _run(vault_probe, mount_probe):
    """Run auth_canary.sh with auth stubbed OK and notify captured.
    Returns (exit_code, log_text, notify_text)."""
    tmp = tempfile.mkdtemp()
    # Fake claude: prints the auth sentinel so auth_state=ok deterministically.
    claude = os.path.join(tmp, "fake_claude")
    _write_exec(claude, "#!/bin/bash\necho CANARY_OK\n")
    # Fake notify: appends its argument (the alert body) to notify.log.
    notify_log = os.path.join(tmp, "notify.log")
    notify = os.path.join(tmp, "fake_notify")
    _write_exec(notify, '#!/bin/bash\nprintf "%s\\n" "$1" >> "$NOTIFY_LOG"\n')

    env = dict(os.environ)
    env.update({
        "CLAUDE_BIN": claude,
        "NOTIFY_BIN": notify,
        "NOTIFY_LOG": notify_log,
        "STATE_FILE": os.path.join(tmp, "canary.state"),
        "LOG_FILE": os.path.join(tmp, "canary.log"),
        "VAULT_PROBE": vault_probe,
        "MOUNT_PROBE": mount_probe,
    })
    rc = subprocess.run(["/bin/bash", CANARY], env=env,
                        capture_output=True, text=True).returncode
    def _slurp(p):
        if not os.path.exists(p):
            return ""
        with open(p) as fh:
            return fh.read()
    return rc, _slurp(env["LOG_FILE"]), _slurp(notify_log)


class TestCanaryProbes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.good = os.path.join(self.tmp, "readable.md")
        with open(self.good, "w") as f:
            f.write("# real content\n")
        self.gooddir = self.tmp  # a directory: head -c1 -> "Is a directory" on stderr

    def test_healthy_readable_probes_are_ok_and_silent(self):
        rc, log, notify = _run(self.good, self.good)
        self.assertEqual(rc, 0, log)
        self.assertIn("healthy (auth=ok vault=ok mount=ok)", log)
        self.assertEqual(notify.strip(), "", "no alert on a healthy run")

    def test_vault_read_error_non_eperm_is_detected(self):
        # The core regression: a read error whose text is NOT an EPERM string
        # (a directory probe -> "Error reading"/"Is a directory", same non-EPERM
        # class as the dataless deadlock) must be classified bad. The old
        # EPERM-only grep would MISS this.
        rc, log, notify = _run(self.gooddir, self.good)
        self.assertEqual(rc, 1, log)
        self.assertIn("vault", log)
        self.assertIn("ALERT", log)
        self.assertNotEqual(notify.strip(), "", "alert must fire for unreadable vault")

    def test_eperm_vault_file_still_detected(self):
        # Regression: the originally-covered EPERM case must still be bad.
        noperm = os.path.join(self.tmp, "noperm.md")
        with open(noperm, "w") as f:
            f.write("x")
        os.chmod(noperm, 0o000)
        try:
            rc, log, notify = _run(noperm, self.good)
        finally:
            os.chmod(noperm, 0o644)
        # root (uid 0) can read 000 files, so the probe would legitimately pass;
        # only assert the failure classification when not root.
        if os.geteuid() != 0:
            self.assertEqual(rc, 1, log)
            self.assertIn("vault", log)

    def test_mount_read_error_is_detected(self):
        # The symmetric fix on the mount probe.
        rc, log, notify = _run(self.good, self.gooddir)
        self.assertEqual(rc, 1, log)
        self.assertIn("mount", log)
        self.assertNotEqual(notify.strip(), "", "alert must fire for unreadable mount")


if __name__ == "__main__":
    unittest.main()
