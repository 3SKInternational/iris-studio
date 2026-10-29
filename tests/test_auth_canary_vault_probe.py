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


def _run(vault_probe, mount_probe, claude_out="CANARY_OK", tmp=None, extra_env=None):
    """Run auth_canary.sh with auth stubbed OK and notify captured.
    Returns (exit_code, log_text, notify_text)."""
    tmp = tmp or tempfile.mkdtemp()
    # Fake claude: prints the auth sentinel so auth_state=ok deterministically.
    # The reply goes through a file so quotes ("You've") reach the canary intact.
    claude = os.path.join(tmp, "fake_claude")
    with open(os.path.join(tmp, "claude_out.txt"), "w") as f:
        f.write(claude_out + "\n")
    _write_exec(claude, '#!/bin/bash\ncat "%s"\n' % os.path.join(tmp, "claude_out.txt"))
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
    env.update(extra_env or {})
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

    def test_subscription_refusal_pages_then_recovers(self):
        # 2026-10-26/27: this reply was logged INCONCLUSIVE seven times, no page.
        refusal = ("Your organization has disabled Claude subscription access for Claude Code"
                   " · Use an Anthropic API key instead, or ask your admin to enable access")
        tmp = tempfile.mkdtemp()
        rc, log, notify = _run(self.good, self.good, refusal, tmp)
        self.assertEqual(rc, 1, log)
        self.assertIn("FAILED (auth)", notify)
        self.assertIn("account/plan refusal", notify)
        rc, log, notify = _run(self.good, self.good, "CANARY_OK", tmp)
        self.assertEqual(rc, 0, log)
        self.assertIn("RECOVERED", notify)

    def test_token_401_alert_has_no_refusal_line(self):
        rc, log, notify = _run(self.good, self.good, "API Error: 401 Invalid authentication credentials")
        self.assertEqual(rc, 1, log)
        self.assertIn("FAILED (auth)", notify)
        self.assertNotIn("account/plan refusal", notify)
        self.assertIn("A 401 = expired OAuth token", notify)

    def test_401_realert_after_one_unknown_keeps_relogin_steps(self):
        tmp = tempfile.mkdtemp()
        state = os.path.join(tmp, "canary.state")
        _run(self.good, self.good, "API Error: 401", tmp)
        with open(state) as f:
            st, _, rest = f.read().split("|", 2)
        with open(state, "w") as f:
            f.write("%s|1|%s" % (st, rest))  # alert epoch 1: throttle passed
        open(os.path.join(tmp, "notify.log"), "w").close()
        rc, log, notify = _run(self.good, self.good, self.UNKNOWN, tmp)
        self.assertEqual(rc, 1, log)
        self.assertIn("STILL FAILING", notify)
        self.assertIn("A 401 = expired OAuth token", notify)
        self.assertNotIn("auth probes, with no known answer", notify)

    # --- A-108: page on a run of unrecognised auth replies -----------------
    UNKNOWN = "Some new refusal wording the regex has never seen"

    def _seq(self, outs):
        # One tmp dir for the whole run: state carries over and notify text
        # in each result is cumulative.
        tmp = tempfile.mkdtemp()
        return [_run(self.good, self.good, o, tmp) for o in outs]

    def test_unknown_reply_pages_on_third_run_then_recovers(self):
        r = self._seq([self.UNKNOWN] * 3 + ["CANARY_OK"])
        for rc, log, notify in r[:2]:
            self.assertEqual(rc, 2, log)
            self.assertEqual(notify.strip(), "", "silent below the streak")
        self.assertIn("unknown_streak=2/3", r[1][1])
        rc, log, notify = r[2]
        self.assertEqual(rc, 1, log)
        self.assertIn("FAILED (auth)", notify)
        self.assertIn("3 auth probes, with no known answer", notify)
        self.assertIn(self.UNKNOWN, notify)
        self.assertNotIn("A 401 = expired OAuth token", notify)
        self.assertNotIn("account/plan refusal", notify)
        self.assertIn("RECOVERED", r[3][2])

    def test_realerts_past_the_threshold_stay_unknown(self):
        # Runs 4 and 5 (each after the 6 h throttle) must keep the unknown
        # bullet and its count, never fall back to the re-login steps.
        tmp = tempfile.mkdtemp()
        state = os.path.join(tmp, "canary.state")
        for _ in range(3):
            _run(self.good, self.good, self.UNKNOWN, tmp)
        for n in (4, 5):
            with open(state) as f:
                st, _, rest = f.read().split("|", 2)
            with open(state, "w") as f:
                f.write("%s|1|%s" % (st, rest))  # alert epoch 1: throttle passed
            open(os.path.join(tmp, "notify.log"), "w").close()
            rc, log, notify = _run(self.good, self.good, self.UNKNOWN, tmp)
            self.assertEqual(rc, 1, log)
            self.assertIn("STILL FAILING", notify)
            self.assertIn("%d auth probes, with no known answer" % n, notify)
            self.assertNotIn("A 401 = expired OAuth token", notify)

    def test_session_limit_spanning_two_runs_never_pages(self):
        r = self._seq(["You've hit your session limit · resets 6am"] * 2 + ["CANARY_OK"])
        self.assertTrue(all(n.strip() == "" for _, _, n in r), r)
        self.assertIn("auth_out=You've hit your session limit · resets 6am", r[1][1])

    def test_ok_resets_the_streak(self):
        r = self._seq([self.UNKNOWN, self.UNKNOWN, "CANARY_OK", self.UNKNOWN, self.UNKNOWN])
        self.assertTrue(all(n.strip() == "" for _, _, n in r), r)

    def test_known_failure_resets_the_streak(self):
        # The two unknowns before the 401 must not carry over: the next unknown
        # starts a fresh count (4th state field), with no OK run in between.
        tmp = tempfile.mkdtemp()
        for o in [self.UNKNOWN, self.UNKNOWN, "API Error: 401", self.UNKNOWN]:
            _run(self.good, self.good, o, tmp)
        with open(os.path.join(tmp, "canary.state")) as f:
            self.assertTrue(f.read().endswith("|1\n"))

    def test_empty_run_during_streak_outage_realerts_as_unknown(self):
        tmp = tempfile.mkdtemp()
        with open(os.path.join(tmp, "canary.state"), "w") as f:
            f.write("bad|1|auth|3\n")  # alert epoch 1: the throttle has passed
        rc, log, notify = _run(self.good, self.good, "", tmp)
        self.assertEqual(rc, 1, log)
        self.assertIn("STILL FAILING", notify)
        self.assertIn("(no output this run)", notify)
        self.assertNotIn("A 401 = expired OAuth token", notify)

    def test_leading_zero_and_overlong_streak_fields(self):
        for field, want_rc in (("08", 1), ("99999999999999999999", 2)):
            tmp = tempfile.mkdtemp()
            with open(os.path.join(tmp, "canary.state"), "w") as f:
                f.write("ok|0||%s\n" % field)
            rc, log, _ = _run(self.good, self.good, self.UNKNOWN, tmp)
            self.assertEqual(rc, want_rc, (field, log))

    def test_empty_output_neither_counts_nor_resets(self):
        r = self._seq(["", "", "", self.UNKNOWN, "", self.UNKNOWN, self.UNKNOWN])
        self.assertTrue(all(n.strip() == "" for _, _, n in r[:6]), r)
        self.assertEqual(r[6][0], 1, r[6][1])
        self.assertIn("3 auth probes, with no known answer", r[6][2])

    def test_pre_a108_three_field_state_reads_as_zero(self):
        tmp = tempfile.mkdtemp()
        with open(os.path.join(tmp, "canary.state"), "w") as f:
            f.write("ok|0|\n")
        rc, log, notify = _run(self.good, self.good, self.UNKNOWN, tmp)
        self.assertEqual(rc, 2, log)
        self.assertIn("unknown_streak=1/3", log)
        with open(os.path.join(tmp, "canary.state")) as f:
            self.assertEqual(f.read(), "ok|0||1\n")

    def test_whitespace_only_output_does_not_count(self):
        r = self._seq(["   "] * 3)
        self.assertTrue(all(rc == 2 and n.strip() == "" for rc, _, n in r), r)
        self.assertIn("unknown_streak=0/3", r[2][1])

    def test_corrupt_streak_field_reads_as_zero(self):
        tmp = tempfile.mkdtemp()
        with open(os.path.join(tmp, "canary.state"), "w") as f:
            f.write("ok|0||9x\n")
        rc, log, notify = _run(self.good, self.good, self.UNKNOWN, tmp)
        self.assertEqual(rc, 2, log)
        self.assertIn("unknown_streak=1/3", log)

    def test_leading_zero_alert_epoch_still_realerts(self):
        tmp = tempfile.mkdtemp()
        with open(os.path.join(tmp, "canary.state"), "w") as f:
            f.write("bad|08|auth|0\n")
        rc, log, notify = _run(self.good, self.good, "API Error: 401", tmp)
        self.assertEqual(rc, 1, log)
        self.assertIn("STILL FAILING", notify)

    def test_quoted_reply_cut_is_valid_utf8_under_c_locale(self):
        # Byte 160 falls inside the 2-byte "·"; a byte-counting cut splits it.
        lead = "Your organization has disabled Claude subscription access"
        out = lead + "x" * (159 - len(lead)) + "· tail"
        tmp = tempfile.mkdtemp()
        rc, log, _ = _run(self.good, self.good, out, tmp, {"LC_ALL": "C", "LANG": "C"})
        self.assertEqual(rc, 1, log)
        with open(os.path.join(tmp, "notify.log"), "rb") as f:
            raw = f.read()
        raw.decode("utf-8")  # raises on a split character
        self.assertIn(b"x" * 20 + b'" This', raw)

    def test_unknown_reply_quote_is_one_line_valid_utf8_under_c_locale(self):
        # The "Latest:" quote on the streak page goes through _snip too.
        out = "line one\n" + "y" * (159 - len("line one ")) + "· resets 6am\nline three"
        tmp = tempfile.mkdtemp()
        for _ in range(3):
            rc, log, _ = _run(self.good, self.good, out, tmp, {"LC_ALL": "C", "LANG": "C"})
        self.assertEqual(rc, 1, log)
        with open(os.path.join(tmp, "notify.log"), "rb") as f:
            raw = f.read()
        text = raw.decode("utf-8")  # raises on a split character
        latest = text.split('Latest: "', 1)[1].split('"', 1)[0]
        self.assertEqual(latest, "line one " + "y" * 150)

    def test_unknown_reply_quote_keeps_160_chars(self):
        r = self._seq(["z" * 200] * 3)
        self.assertIn('Latest: "%s"' % ("z" * 160), r[2][2])

    def test_recovery_keeps_a_mid_streak_count(self):
        # Mount recovers while auth has 1 unknown: the count must carry on.
        tmp = tempfile.mkdtemp()
        with open(os.path.join(tmp, "canary.state"), "w") as f:
            f.write("bad|1|mount|1\n")
        rc, log, notify = _run(self.good, self.good, self.UNKNOWN, tmp)
        self.assertEqual(rc, 0, log)
        self.assertIn("RECOVERED", notify)
        with open(os.path.join(tmp, "canary.state")) as f:
            self.assertEqual(f.read(), "ok|0||2\n")

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
