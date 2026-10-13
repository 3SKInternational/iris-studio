#!/usr/bin/env python3
"""Cloud system prompt size + API-error handling in iris.py (2026-10-13).

Stdlib unittest only (repo convention). Run:
    python3 tests/test_cloud_prompt_cap.py

WHY THIS EXISTS. From ~09-13 to 10-12 every 08:00 morning brief came back
"Prompt is too long" (HTTP 400). load_system_prompt_cloud loaded each vault file
whole: Vault_MOC had grown to 230 KB, and _read_bridge_latest_entry looked for
"## [" headers the bridge stopped using, so it loaded the whole 250 KB bridge.
query_cloud then salvaged the 18-char error as reply text, and the brief logged
"sent". These tests pin the per-section cap, the bridge header match, and that
an API rejection raises instead of becoming Iris's reply.

Imports the REAL iris.py with the stub pattern from test_generate_briefing_combined
(dotenv force-stubbed so no production credentials load).
"""
import asyncio
import importlib
import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _stub(name: str, force: bool = False, **attrs) -> None:
    """Register a placeholder module unless a real one is importable (see
    test_ceo_brief_recency for the full rationale). force=True stubs regardless —
    required for dotenv, whose import runs load_dotenv() at iris module scope."""
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

    spec = importlib.util.spec_from_file_location("iris_under_test", REPO / "iris.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


iris = _load_iris()

# The model window is 200K tokens. At ~3.5 chars/token, 450K chars is ~130K
# tokens, leaving room for tool definitions and the reply.
PROMPT_CEILING_CHARS = 450_000


class TestBridgeLatestEntry(unittest.TestCase):
    def test_returns_only_the_last_session_entry(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "bridge.md"
            p.write_text("# Bridge\n\nintro\n\n## Session — one\nold\n\n"
                         "## Session — two\nnewest\n### Work completed\nx\n")
            out = iris._read_bridge_latest_entry(p)
        self.assertEqual(out, "## Session — two\nnewest\n### Work completed\nx")


class TestSystemPromptCap(unittest.TestCase):
    FILES = ("BLUEPRINT_FILE", "CONTEXT_FILE", "VAULT_MOC_FILE", "INBOX_FILE",
             "DAILY_BRIEFING_FILE", "BUILD_QUEUE_FILE", "BRIDGE_FILE")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = {n: getattr(iris, n) for n in self.FILES}
        self.saved_ceo = iris._read_latest_ceo_brief
        self.saved_recent = iris._read_recent_markdown_files
        big = "x" * 1_000_000
        for n in self.FILES:
            p = Path(self.tmp.name) / f"{n}.md"
            p.write_text(("## Session — only\n" if n == "BRIDGE_FILE" else "") + big)
            setattr(iris, n, p)
        iris._read_latest_ceo_brief = lambda *a, **k: (big, "2026-10-13")
        iris._read_recent_markdown_files = lambda *a, **k: ""

    def tearDown(self):
        for n, v in self.saved.items():
            setattr(iris, n, v)
        iris._read_latest_ceo_brief = self.saved_ceo
        iris._read_recent_markdown_files = self.saved_recent
        self.tmp.cleanup()

    def test_eight_one_megabyte_files_stay_under_the_ceiling(self):
        prompt = iris.load_system_prompt_cloud([])
        self.assertLess(len(prompt), PROMPT_CEILING_CHARS)
        self.assertEqual(prompt.count("truncated:"), 8)

    def test_short_section_is_untouched(self):
        self.assertEqual(iris._cap_section("abc", "x"), "abc")
        exact = "y" * iris.CONTEXT_SECTION_MAX_CHARS
        self.assertEqual(iris._cap_section(exact, "x"), exact)


class _Msg:
    def __init__(self, **kw):
        self.__dict__.update(kw)


AssistantMessage = type("AssistantMessage", (_Msg,), {})
ResultMessage = type("ResultMessage", (_Msg,), {})


class TestQueryCloudApiError(unittest.TestCase):
    """The 10-12 failure shape: the CLI streams the error as text, sends an
    is_error ResultMessage carrying api_error_status=400, then raises."""

    def setUp(self):
        self.saved = (iris.query, iris.ClaudeAgentOptions, iris.load_system_prompt_cloud)
        iris.ClaudeAgentOptions = lambda **k: k
        iris.load_system_prompt_cloud = lambda h: "system"

    def tearDown(self):
        iris.query, iris.ClaudeAgentOptions, iris.load_system_prompt_cloud = self.saved

    def _run(self, status, then_raise):
        async def fake_query(prompt, options):
            yield AssistantMessage(content=[_Msg(text="Prompt is too long")])
            yield ResultMessage(is_error=True, api_error_status=status,
                                result="Prompt is too long")
            if then_raise:
                raise Exception("Claude Code returned an error result: success")
        iris.query = fake_query
        return asyncio.run(iris.query_cloud("hi", history=[]))

    def test_api_error_then_raise_is_not_returned_as_text(self):
        with self.assertRaises(RuntimeError) as cm:
            self._run(400, then_raise=True)
        self.assertIn("400", str(cm.exception))
        self.assertIn("Prompt is too long", str(cm.exception))

    def test_api_error_without_raise_still_raises(self):
        with self.assertRaises(RuntimeError):
            self._run(400, then_raise=False)

    def test_normal_reply_is_returned(self):
        async def fake_query(prompt, options):
            yield AssistantMessage(content=[_Msg(text="Morning, Steve.")])
            yield ResultMessage(is_error=False, duration_ms=5, model_usage={})
        iris.query = fake_query
        self.assertEqual(asyncio.run(iris.query_cloud("hi", history=[])), "Morning, Steve.")

    def test_completed_turn_without_api_status_is_still_salvaged(self):
        # the pre-existing salvage path (CLI error after a real reply) must stay
        self.assertEqual(self._run(None, then_raise=True), "Prompt is too long")


if __name__ == "__main__":
    unittest.main(verbosity=2)
