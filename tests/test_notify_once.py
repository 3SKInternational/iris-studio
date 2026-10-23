#!/usr/bin/env python3
"""Tests for the NOTIFY_ONCE repeat check in scripts/notify.sh (A-96).

Runs a copy of notify.sh in a temp dir with a fake .env, a fake `curl` on PATH
and HOME pointed at the temp dir, so nothing reaches Telegram and the real
~/iris_studio/state is untouched. Stdlib unittest only. Run:
    python3 tests/test_notify_once.py
"""
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


class TestNotifyOnce(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "scripts").mkdir()
        shutil.copy(REPO / "scripts" / "notify.sh", self.tmp / "scripts" / "notify.sh")
        (self.tmp / ".env").write_text("TELEGRAM_BOT_TOKEN=fake\nIRIS_TELEGRAM_USER_IDS=123\n")
        bindir = self.tmp / "bin"
        bindir.mkdir()
        self.calls = self.tmp / "curl_calls"
        curl = bindir / "curl"
        # Fake curl: count calls; reply ok unless FAKE_FAIL is set.
        curl.write_text('#!/bin/bash\necho x >> "%s"\n'
                        '[ -n "${FAKE_FAIL:-}" ] && echo \'{"ok":false}\' || echo \'{"ok":true}\'\n'
                        % self.calls)
        curl.chmod(0o755)
        self.state = self.tmp / "iris_studio" / "state" / "notify_once.tsv"
        self.env = {"PATH": f"{bindir}:/usr/bin:/bin", "HOME": str(self.tmp)}

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_notify(self, key=None, fail=False, photo=False):
        env = dict(self.env)
        if key is not None:
            env["NOTIFY_ONCE"] = key
        if fail:
            env["FAKE_FAIL"] = "1"
        args = ["bash", str(self.tmp / "scripts" / "notify.sh")]
        if photo:
            img = self.tmp / "p.png"
            img.write_bytes(b"x")
            args += ["--photo", str(img)]
        return subprocess.run(args + ["hello"], env=env, capture_output=True, text=True)

    def sends(self):
        return len(self.calls.read_text().splitlines()) if self.calls.exists() else 0

    def test_same_key_suppressed(self):
        self.assertEqual(self.run_notify("k1").returncode, 0)
        r = self.run_notify("k1")
        self.assertEqual(r.returncode, 0)
        self.assertIn("skipped", r.stdout)
        self.assertEqual(self.sends(), 1)

    def test_new_key_sent(self):
        self.run_notify("k1")
        self.run_notify("k2")
        self.assertEqual(self.sends(), 2)

    def test_resend_after_seven_days(self):
        self.state.parent.mkdir(parents=True)
        old = int(time.time()) - 7 * 86400 - 60
        self.state.write_text(f"{old}\tk1\n")
        self.assertEqual(self.run_notify("k1").returncode, 0)
        self.assertEqual(self.sends(), 1)
        # The stale line is pruned, the fresh one kept.
        self.assertEqual(len(self.state.read_text().splitlines()), 1)

    def test_within_seven_days_still_suppressed(self):
        self.state.parent.mkdir(parents=True)
        self.state.write_text(f"{int(time.time()) - 7 * 86400 + 60}\tk1\n")
        self.run_notify("k1")
        self.assertEqual(self.sends(), 0)

    def test_unset_never_suppresses_or_records(self):
        self.run_notify()
        self.run_notify()
        self.assertEqual(self.sends(), 2)
        self.assertFalse(self.state.exists())

    def test_empty_key_is_unset(self):
        self.run_notify("")
        self.run_notify("")
        self.assertEqual(self.sends(), 2)

    def test_failed_send_leaves_no_record(self):
        self.assertEqual(self.run_notify("k1", fail=True).returncode, 2)
        self.assertFalse(self.state.exists())
        self.assertEqual(self.run_notify("k1").returncode, 0)
        self.assertEqual(self.sends(), 2)

    def test_photo_mode_records_and_suppresses(self):
        self.run_notify("p1", photo=True)
        self.run_notify("p1", photo=True)
        self.assertEqual(self.sends(), 1)

    def test_tab_in_key_does_not_break_the_state_file(self):
        self.run_notify("a\tb")
        self.run_notify("a\tb")
        self.assertEqual(self.sends(), 1)

    def test_backslash_in_key_matches(self):
        self.run_notify("a\\nb")
        self.run_notify("a\\nb")
        self.assertEqual(self.sends(), 1)

    def test_key_match_is_exact(self):
        self.run_notify("k10")
        self.run_notify("k1")
        self.assertEqual(self.sends(), 2)


if __name__ == "__main__":
    unittest.main()
