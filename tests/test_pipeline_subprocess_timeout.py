#!/usr/bin/env python3
"""Tests for iris.py's _run_pipeline_subprocess (the shared /pipeline runner).

THE DEFECT (two-pass codebase audit, lane A, 2026-07-28). `_run_pipeline_command`
and `_run_pipeline_fleet_command` both called a bare `await proc.communicate()`
with NO timeout, while `_run_dispatch` (the model-dispatch runner in the same
file) already has the correct wait_for -> kill() -> bounded re-drain -> reap
pattern for the identical hazard. The orchestrator holds a BLOCKING
flock(LOCK_EX) for its entire run, and the hourly pipeline-sweep plist runs
`--advance-all`, so `/pipeline N status` during a sweep could block Telegram
forever with no message, and each impatient retry leaked a permanently blocked
process + asyncio task.

Fix: both runners now go through one shared `_run_pipeline_subprocess` helper
with a bounded timeout, mirroring `_run_dispatch`'s pattern.

Imports the REAL iris.py (module-stubbing pattern from
tests/test_ceo_brief_recency.py — see that file for why each stub exists).

Run: python3 tests/test_pipeline_subprocess_timeout.py
"""
import asyncio
import importlib
import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _stub(name: str, force: bool = False, **attrs) -> None:
    if not force:
        try:
            importlib.import_module(name)
            return
        except Exception:
            pass
    parts = name.split(".")
    for i in range(1, len(parts) + 1):
        sub = ".".join(parts[:i])
        if sub not in sys.modules:
            sys.modules[sub] = types.ModuleType(sub)
    for k, v in attrs.items():
        setattr(sys.modules[name], k, v)


def _load_iris():
    _stub("apscheduler.schedulers.asyncio", AsyncIOScheduler=object)
    _stub("apscheduler.triggers.cron", CronTrigger=object)
    _stub("apscheduler.triggers.interval", IntervalTrigger=object)
    _stub("dotenv", force=True, load_dotenv=lambda *a, **k: None)
    _stub("telegram", Update=object)
    _stub("telegram.error", NetworkError=Exception, RetryAfter=Exception,
          TimedOut=Exception)
    _stub("telegram.ext", Application=object, ContextTypes=object,
          MessageHandler=object, filters=object)
    sdk = types.ModuleType("claude_agent_sdk")
    for attr in ("ClaudeSDKClient", "ClaudeAgentOptions", "AssistantMessage",
                 "TextBlock", "ResultMessage", "ToolUseBlock", "ToolResultBlock",
                 "UserMessage", "SystemMessage"):
        setattr(sdk, attr, type(attr, (), {}))
    sdk.tool = lambda *a, **k: (lambda fn: fn)
    sdk.query = lambda *a, **k: None
    sdk.create_sdk_mcp_server = lambda *a, **k: None
    sys.modules.setdefault("claude_agent_sdk", sdk)
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token-not-a-real-credential")

    spec = importlib.util.spec_from_file_location("iris_under_test_pipeline", REPO / "iris.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


iris = _load_iris()


class _CapturingTelegram:
    """Stand-in for iris._send_telegram: records every (chat_id, text) call."""

    def __init__(self):
        self.calls = []

    async def __call__(self, chat_id, text):
        self.calls.append((chat_id, text))
        return True


class TestTimeoutKillsAndReports(unittest.TestCase):
    def setUp(self):
        self._orig_send = iris._send_telegram
        self._orig_timeout = iris.PIPELINE_SUBPROCESS_TIMEOUT
        self.tg = _CapturingTelegram()
        iris._send_telegram = self.tg
        # Shrink the real 3600s bound to something a test can actually wait out.
        iris.PIPELINE_SUBPROCESS_TIMEOUT = 0.2

    def tearDown(self):
        iris._send_telegram = self._orig_send
        iris.PIPELINE_SUBPROCESS_TIMEOUT = self._orig_timeout

    def test_hung_subprocess_is_killed_and_reported_not_left_hanging(self):
        """A subprocess that outlives the timeout must be killed and the call
        must RETURN — not block forever. `asyncio.wait_for` around the whole
        test bounds it: if the fix regresses to a bare communicate(), this test
        itself times out instead of silently passing."""
        cmd = [sys.executable, "-c", "import time; time.sleep(30)"]

        async def run():
            await asyncio.wait_for(
                iris._run_pipeline_subprocess(cmd, 12345, "test-label"), timeout=10,
            )

        asyncio.run(run())
        self.assertEqual(len(self.tg.calls), 1, self.tg.calls)
        chat_id, text = self.tg.calls[0]
        self.assertEqual(chat_id, 12345)
        self.assertIn("timed out", text)
        self.assertIn("test-label", text)

    def test_fast_subprocess_completes_normally_no_false_timeout(self):
        """A subprocess well under the timeout must relay its real output and
        must NOT print a timeout message — pins the other direction, so a
        mutant that always reports timed_out=True is also caught."""
        iris.PIPELINE_SUBPROCESS_TIMEOUT = 30  # plenty for `print`
        cmd = [sys.executable, "-c", "print('hello-from-subprocess')"]

        async def run():
            await iris._run_pipeline_subprocess(cmd, 999, "test-label")

        asyncio.run(run())
        self.assertEqual(len(self.tg.calls), 1, self.tg.calls)
        chat_id, text = self.tg.calls[0]
        self.assertEqual(chat_id, 999)
        self.assertIn("hello-from-subprocess", text)
        self.assertNotIn("timed out", text)


class TestPinnedLiterals(unittest.TestCase):
    """Pin the exact constants (not just their sign/direction) so a future edit
    can't silently drift them. Read via the production symbol, not re-derived."""

    def test_default_subprocess_timeout_is_3600(self):
        self.assertEqual(iris.PIPELINE_SUBPROCESS_TIMEOUT, 3600)

    def test_redrain_timeout_is_5(self):
        self.assertEqual(iris.PIPELINE_REDRAIN_TIMEOUT, 5)

    def test_stdout_trunc_is_3500(self):
        self.assertEqual(iris.PIPELINE_STDOUT_TRUNC, 3500)

    def test_rc_msg_trunc_is_3000(self):
        self.assertEqual(iris.PIPELINE_RC_MSG_TRUNC, 3000)


class TestOutputRelayBranches(unittest.TestCase):
    """The three post-completion branches (clean rc=0 output / rc!=0 or stderr
    present / truly nothing) and their exact truncation cutoffs."""

    def setUp(self):
        self._orig_send = iris._send_telegram
        self._orig_timeout = iris.PIPELINE_SUBPROCESS_TIMEOUT
        self.tg = _CapturingTelegram()
        iris._send_telegram = self.tg
        iris.PIPELINE_SUBPROCESS_TIMEOUT = 30

    def tearDown(self):
        iris._send_telegram = self._orig_send
        iris.PIPELINE_SUBPROCESS_TIMEOUT = self._orig_timeout

    def _run(self, cmd, chat_id=1):
        async def go():
            await iris._run_pipeline_subprocess(cmd, chat_id, "test-label")
        asyncio.run(go())
        self.assertEqual(len(self.tg.calls), 1, self.tg.calls)
        return self.tg.calls[0][1]

    def test_rc0_with_empty_stdout_does_not_take_the_stdout_branch(self):
        """rc=0 but NOTHING printed must fall through to the 'no output' else
        branch, not the bare out[:N] branch (which would silently send an empty
        Telegram message). Kills the :3360 if-test->True / int 0->1 / negate-
        compare mutants, all of which force this branch open on empty output."""
        cmd = [sys.executable, "-c", "pass"]
        text = self._run(cmd)
        self.assertIn("finished (rc=0, no output)", text)

    def test_nonzero_rc_with_stderr_only_takes_the_rc_branch(self):
        """rc!=0 with stderr (no stdout) must take the '(rc=N):' branch via the
        `err` side of `out or err` — kills the :3362 flip-bool 'or'->'and' and
        if-test->False mutants, which would wrongly fall through to 'no output'
        here since stdout is empty."""
        cmd = [sys.executable, "-c",
              "import sys; sys.stderr.write('boom'); sys.exit(3)"]
        text = self._run(cmd)
        self.assertIn("(rc=3):", text)
        self.assertIn("boom", text)

    def test_nonzero_rc_WITH_stdout_still_reports_the_rc(self):
        """rc!=0 but the command DID print to stdout must still take the '(rc=N):'
        branch — a failed run is never relayed as a bare success.

        Round-5 review found this case missing: the suite pinned the `or`->`and`
        flip, both truncations, the empty-output else and the stderr-only arm, but
        never rc!=0 WITH stdout. Dropping the `proc.returncode == 0` test entirely
        (`if proc.returncode == 0 and out:` -> `if out:`) passed every existing
        case, and under that mutation a failed orchestrator run that printed to
        stdout reaches Telegram as a plain success with no rc marker at all — the
        operator reads a failure as a completion."""
        cmd = [sys.executable, "-c",
              "import sys; sys.stdout.write('partial progress'); sys.exit(3)"]
        text = self._run(cmd)
        self.assertIn("(rc=3):", text,
                      "a non-zero rc must be reported even when stdout is non-empty")
        self.assertIn("partial progress", text)

    def test_nonzero_rc_with_nothing_at_all_takes_the_else_branch(self):
        """rc!=0, no stdout, no stderr -> the plain 'finished, no output'
        message. Kills the :3362 if-test->True mutant, which would wrongly
        route this into the '(rc=N):\\n' branch with an empty body."""
        cmd = [sys.executable, "-c", "import sys; sys.exit(7)"]
        text = self._run(cmd)
        self.assertEqual(text, "test-label finished (rc=7, no output).")

    def test_stdout_truncated_at_exactly_3500_chars(self):
        """Kills the :3361 int 3500->3501 mutant."""
        cmd = [sys.executable, "-c",
              "import sys; sys.stdout.write('A' * 5000)"]
        text = self._run(cmd)
        self.assertEqual(text, "A" * 3500)

    def test_rc_message_truncated_at_exactly_3000_chars(self):
        """Kills the :3364 int 3000->3001 mutant."""
        cmd = [sys.executable, "-c",
              "import sys; sys.stderr.write('E' * 5000); sys.exit(9)"]
        text = self._run(cmd)
        self.assertEqual(text, "test-label (rc=9):\n" + "E" * 3000)


class TestRunnersShareTheHelper(unittest.TestCase):
    """The per-video and fleet runners must both route through
    _run_pipeline_subprocess with the correct cmd + label — pins the collapse
    (no duplicated bare-communicate() logic left in either caller)."""

    def setUp(self):
        self._orig = iris._run_pipeline_subprocess
        self.captured = []

        async def fake(cmd, chat_id, label):
            self.captured.append((cmd, chat_id, label))

        iris._run_pipeline_subprocess = fake

    def tearDown(self):
        iris._run_pipeline_subprocess = self._orig

    def test_per_video_command_routes_through_shared_helper(self):
        asyncio.run(iris._run_pipeline_command(14, "status", 555))
        self.assertEqual(len(self.captured), 1)
        cmd, chat_id, label = self.captured[0]
        self.assertIn("--status", cmd)
        self.assertIn("14", cmd)
        self.assertEqual(chat_id, 555)
        self.assertEqual(label, "/pipeline 14 status")

    def test_fleet_command_routes_through_shared_helper(self):
        asyncio.run(iris._run_pipeline_fleet_command("advance", 555))
        self.assertEqual(len(self.captured), 1)
        cmd, chat_id, label = self.captured[0]
        self.assertIn("--advance-all", cmd)
        self.assertNotIn("--video", cmd)
        self.assertEqual(label, "/pipeline all advance")


if __name__ == "__main__":
    unittest.main()
