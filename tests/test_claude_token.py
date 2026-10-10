"""Tests for the long-lived setup-token loader (DQ-49).

scripts/claude_token.sh (sourced by run_claude_job.sh, run_job.sh, run_iris.sh)
and the inlined copy in auth_canary.sh must both: export CLAUDE_CODE_OAUTH_TOKEN
from Keychain when the item exists, do nothing when it doesn't (so claude falls
back to the /login credential), and never override a token already in the env.
A fake `security` on PATH stands in for the Keychain.
"""
import os
import subprocess
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HELPER = os.path.join(REPO, "scripts", "claude_token.sh")
CANARY = os.path.join(REPO, "scripts", "auth_canary.sh")


def _write_exec(path, body):
    with open(path, "w") as f:
        f.write(body)
    os.chmod(path, 0o755)


def _env(tmp, keychain_token, preset=None):
    """PATH with a fake `security`: prints keychain_token, or fails like a
    missing item when it is None. Only answers our exact account/service."""
    bindir = os.path.join(tmp, "bin")
    os.makedirs(bindir, exist_ok=True)
    if keychain_token is None:
        body = "#!/bin/bash\nexit 44\n"
    else:
        body = ('#!/bin/bash\n'
                'case "$*" in *"-a iris-claude -s iris_claude_oauth_token -w"*) '
                f'echo "{keychain_token}";; *) exit 44;; esac\n')
    _write_exec(os.path.join(bindir, "security"), body)
    env = dict(os.environ)
    env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    if preset is not None:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = preset
    env["PATH"] = bindir + os.pathsep + env["PATH"]
    return env


def _helper(keychain_token, preset=None):
    tmp = tempfile.mkdtemp()
    out = subprocess.run(
        ["/bin/bash", "-uc",
         f'. "{HELPER}"; echo "${{CLAUDE_CODE_OAUTH_TOKEN-<unset>}}"; '
         'echo "${_iris_tok-<clean>}"; echo "$IRIS_CLAUDE_AUTH_SOURCE"; '
         'echo "$IRIS_CLAUDE_REAUTH"'],
        env=_env(tmp, keychain_token, preset), capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    tok, leak, source, reauth = out.stdout.splitlines()
    return tok, leak, source, reauth


def _canary(keychain_token, preset=None, auth_fails=False):
    """Run the canary with a fake claude that records the token it was given.
    Returns (token_seen, alert_body)."""
    tmp = tempfile.mkdtemp()
    seen = os.path.join(tmp, "seen")
    claude = os.path.join(tmp, "fake_claude")
    reply = ("echo 'Failed to authenticate. API Error: 401 OAuth access token has expired.'"
             if auth_fails else "echo CANARY_OK")
    _write_exec(claude, '#!/bin/bash\necho "${CLAUDE_CODE_OAUTH_TOKEN-<unset>}" > "$SEEN"\n'
                        + reply + '\n')
    notify_log = os.path.join(tmp, "notify.log")
    notify = os.path.join(tmp, "fake_notify")
    _write_exec(notify, '#!/bin/bash\nprintf "%s\\n" "$1" >> "$NOTIFY_LOG"\n')
    probe = os.path.join(tmp, "probe")
    with open(probe, "w") as f:
        f.write("x")
    env = _env(tmp, keychain_token, preset)
    env.update({"CLAUDE_BIN": claude, "NOTIFY_BIN": notify, "SEEN": seen,
                "NOTIFY_LOG": notify_log,
                "STATE_FILE": os.path.join(tmp, "s"), "LOG_FILE": os.path.join(tmp, "l"),
                "VAULT_PROBE": probe, "MOUNT_PROBE": probe})
    subprocess.run(["/bin/bash", CANARY], env=env, capture_output=True, text=True)
    with open(seen) as f:
        token_seen = f.read().strip()
    body = open(notify_log).read() if os.path.exists(notify_log) else ""
    return token_seen, body


class TestHelper(unittest.TestCase):
    def test_exports_keychain_token(self):
        tok, leak, source, reauth = _helper("sk-ant-oat01-abc")
        self.assertEqual((tok, leak, source), ("sk-ant-oat01-abc", "<clean>", "keychain"))
        # A dead setup-token outranks /login, so the fix must name setup-token.
        self.assertIn("claude setup-token", reauth)
        self.assertIn("will NOT fix it", reauth)

    def test_noop_without_keychain_item(self):
        tok, leak, source, reauth = _helper(None)
        self.assertEqual((tok, leak, source), ("<unset>", "<clean>", "login"))
        self.assertIn("claude auth login", reauth)
        self.assertNotIn("setup-token", reauth)

    def test_env_token_wins(self):
        tok, _, source, reauth = _helper("sk-ant-oat01-abc", preset="from-env")
        self.assertEqual((tok, source), ("from-env", "env"))
        self.assertIn("claude setup-token", reauth)


class TestCanaryInlineCopy(unittest.TestCase):
    """The canary must probe the same credential the jobs use."""
    def test_exports_keychain_token(self):
        self.assertEqual(_canary("sk-ant-oat01-abc")[0], "sk-ant-oat01-abc")

    def test_noop_without_keychain_item(self):
        self.assertEqual(_canary(None)[0], "<unset>")

    def test_env_token_wins(self):
        self.assertEqual(_canary("sk-ant-oat01-abc", preset="from-env")[0], "from-env")

    def test_dead_setup_token_alert_names_setup_token(self):
        _, body = _canary("sk-ant-oat01-dead", auth_fails=True)
        self.assertIn("credential: keychain", body)
        self.assertIn("claude setup-token", body)

    def test_dead_login_alert_names_auth_login(self):
        _, body = _canary(None, auth_fails=True)
        self.assertIn("credential: login", body)
        self.assertIn("claude auth login", body)
        self.assertNotIn("setup-token", body)

    def test_inline_copy_matches_helper(self):
        """The canary inlines the helper's logic (it must not read the mount);
        a drift here would make the canary probe a different credential."""
        helper = open(HELPER).read()
        logic = helper[helper.index('if [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]'):]
        self.assertIn(logic, open(CANARY).read())


if __name__ == "__main__":
    unittest.main()
