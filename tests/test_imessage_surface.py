#!/usr/bin/env python3
"""Tests for iris.py's iMessage surface: _answer_imessage + _imessage_loop.

Stdlib unittest only (repo convention). Run:
    python3 tests/test_imessage_surface.py

Imports the REAL iris.py via the stub pattern of test_generate_briefing_combined.py
(dotenv force-stubbed so no live .env loads). The transport (imessage_bridge) and
the model (ask_iris) are stubbed; what's pinned is the daemon's routing:
commands refused, captures filed, files diverted to Telegram, failures alerted
once, and the cursor saved before any answer goes out.
"""
import asyncio
import importlib
import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
STEVE = "+15551234567"


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
    os.environ.setdefault("IRIS_TELEGRAM_USER_IDS", "777")
    spec = importlib.util.spec_from_file_location("iris_under_test", REPO / "iris.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


iris = _load_iris()


class Harness(unittest.TestCase):
    """Stubs every side effect _answer_imessage can reach and records calls."""

    def setUp(self):
        self.calls = []
        self.sent = []
        self.ask_result = ("model reply", "cloud")
        self.ask_exc = None
        self.send_exc = None

        async def ask_iris(text, chat_id, user_id):
            self.calls.append(("ask", text, chat_id, user_id))
            if self.ask_exc:
                raise self.ask_exc
            return self.ask_result

        def send(handle, text):
            if self.send_exc:
                raise self.send_exc
            self.sent.append((handle, text))

        async def save_brain(prefix, text, uid, uname):
            self.calls.append(("brain", prefix, uname))
            return self.brain_rel

        async def save_qc(prefix, text, uid, uname):
            self.calls.append(("qc", prefix, uname))
            return True

        async def route_qc(prefix, text, uid, uname):
            self.calls.append(("route", prefix))

        async def send_media(bot, chat, src, *a, **k):
            self.calls.append(("media", chat, src))

        self.brain_rel = "00 - INBOX/x.md"
        p = [
            mock.patch.object(iris, "ALLOWED_USER_IDS", {777}),
            mock.patch.object(iris, "ask_iris", ask_iris),
            mock.patch.object(iris.imessage_bridge, "send", send),
            mock.patch.object(iris, "save_brain_capture", save_brain),
            mock.patch.object(iris, "save_quick_capture", save_qc),
            mock.patch.object(iris, "route_quick_capture", route_qc),
            mock.patch.object(iris, "send_media_to_chat", send_media),
            mock.patch.object(iris, "_safe_vault_path", lambda s: "/safe/" + s),
            mock.patch.object(iris, "_alert_steve",
                              lambda t, wait=False: self.calls.append(("alert", t))),
        ]
        for x in p:
            x.start()
            self.addCleanup(x.stop)

    def answer(self, text):
        asyncio.run(iris._answer_imessage(STEVE, text))


class TestAnswer(Harness):
    def test_plain_text_goes_to_model_on_telegram_thread(self):
        self.answer("how's V14 doing?")
        self.assertEqual(self.calls, [("ask", "how's V14 doing?", "777", 777)])
        self.assertEqual(self.sent, [(STEVE, "model reply")])

    def test_commands_refused_without_model(self):
        for cmd in ("/pipeline 14 spend-ok", "!approve abc"):
            self.calls.clear()
            self.sent.clear()
            self.answer(cmd)
            self.assertEqual(self.calls, [])
            self.assertIn("Telegram", self.sent[0][1])

    def test_brain_capture_files_without_model(self):
        self.answer("BRAIN: a thought")
        self.assertEqual(self.calls, [("brain", "BRAIN:", f"imessage:{STEVE}")])
        self.assertIn("00 - INBOX/x.md", self.sent[0][1])

    def test_brain_capture_failure_says_not_filed(self):
        self.brain_rel = None
        self.answer("BRAIN: a thought")
        self.assertIn("Not filed", self.sent[0][1])

    def test_quick_capture_saved_and_routed_then_model(self):
        self.answer("DECISION: ship V14 Tuesday")
        self.assertEqual([c[0] for c in self.calls], ["qc", "route", "ask"])
        self.assertEqual(self.sent, [(STEVE, "model reply")])

    def test_cloud_send_file_goes_to_telegram(self):
        self.ask_result = ("here you go [[SEND_FILE: a/b.png]]", "cloud_fallback")
        self.answer("send me the thumbnail")
        self.assertIn(("media", 777, "/safe/a/b.png"), self.calls)
        self.assertNotIn("SEND_FILE", self.sent[0][1])
        self.assertIn("File sent to Telegram", self.sent[0][1])

    def test_local_tier_reply_not_parsed_for_files(self):
        self.ask_result = ("literal [[SEND_FILE: a/b.png]]", "local")
        self.answer("hi")
        self.assertEqual(self.sent, [(STEVE, "literal [[SEND_FILE: a/b.png]]")])
        self.assertFalse(any(c[0] == "media" for c in self.calls))

    def test_model_error_still_replies(self):
        self.ask_exc = RuntimeError("boom")
        self.answer("hi")
        self.assertEqual(self.sent, [(STEVE, "Iris hit an error: boom")])

    def test_empty_reply_sends_placeholder(self):
        self.ask_result = ("", "local")
        self.answer("hi")
        self.assertEqual(self.sent, [(STEVE, "(no reply)")])

    def test_send_failure_alerts_with_reply(self):
        self.send_exc = RuntimeError("not authorized")
        self.answer("hi")
        alerts = [c[1] for c in self.calls if c[0] == "alert"]
        self.assertEqual(len(alerts), 1)
        self.assertIn("not authorized", alerts[0])
        self.assertIn("model reply", alerts[0])


class TestAnswerReturns(Harness):
    def test_returns_what_was_sent(self):
        self.ask_result = ("hello steve", "local")
        self.assertEqual(asyncio.run(iris._answer_imessage(STEVE, "hi")), "hello steve")

    def test_returns_placeholder_when_that_was_sent(self):
        self.ask_result = ("", "local")
        self.assertEqual(asyncio.run(iris._answer_imessage(STEVE, "hi")), "(no reply)")
        self.assertEqual(self.sent, [(STEVE, "(no reply)")])


class TestAskLock(unittest.TestCase):
    def test_turns_never_overlap(self):
        live, peak = [0], [0]

        async def inner(prompt, chat_id, user_id):
            live[0] += 1
            peak[0] = max(peak[0], live[0])
            await asyncio.sleep(0.01)
            live[0] -= 1
            return prompt, "local"

        async def go():
            with mock.patch.object(iris, "_ask_iris_unlocked", inner), \
                 mock.patch.object(iris, "_ASK_LOCK", asyncio.Lock()):
                return await asyncio.gather(*(iris.ask_iris(str(i), "777", 777) for i in range(3)))

        self.assertEqual([r[0] for r in asyncio.run(go())], ["0", "1", "2"])
        self.assertEqual(peak[0], 1)


class TestStart(unittest.TestCase):
    def _start(self, handles, loop_exc=None):
        started, alerts = [], []

        async def fake_loop():
            started.append(True)
            if loop_exc:
                raise loop_exc

        async def go():
            with mock.patch.object(iris, "IMESSAGE_HANDLES", handles), \
                 mock.patch.object(iris, "_imessage_task", None), \
                 mock.patch.object(iris, "_imessage_loop", fake_loop), \
                 mock.patch.object(iris, "_alert_steve", lambda t, wait=False: alerts.append(t)):
                iris._start_imessage_surface()
                task = iris._imessage_task
                if task:
                    await asyncio.gather(task, return_exceptions=True)
                    await asyncio.sleep(0)  # let the done-callback run
                return task
        with self.assertLogs(iris.logger, "INFO") as logs:
            iris.logger.info("start")
            task = asyncio.run(go())
        return task, started, alerts, "\n".join(logs.output)

    def test_off_without_handles(self):
        task, started, alerts, _ = self._start(set())
        self.assertEqual((task, started, alerts), (None, [], []))

    def test_on_with_handles(self):
        task, started, alerts, log = self._start({STEVE, "me@icloud.com"})
        self.assertIsNotNone(task)
        self.assertEqual((started, alerts), ([True], []))
        self.assertNotIn("not +E.164", log)

    def test_bad_handle_format_warned(self):
        _, _, _, log = self._start({"5551234567"})
        self.assertIn("'5551234567' is not +E.164", log)

    def test_loop_death_alerts(self):
        _, _, alerts, _ = self._start({STEVE}, loop_exc=IsADirectoryError("cursor"))
        self.assertEqual(len(alerts), 1)
        self.assertIn("IsADirectoryError", alerts[0])

    def test_cancel_does_not_alert(self):
        alerts = []
        loop = asyncio.new_event_loop()
        try:
            fut = loop.create_future()
            fut.cancel()
            with mock.patch.object(iris, "_alert_steve", lambda t, wait=False: alerts.append(t)):
                iris._on_imessage_task_done(fut)
        finally:
            loop.close()
        self.assertEqual(alerts, [])


class StopHere(Exception):
    pass


class TestPostInitWiring(unittest.TestCase):
    """_post_init must call _start_imessage_surface once Telegram users exist,
    and must not when there are none. Deleting the bare call kills no mutant,
    so this fixture is the only thing that notices."""

    def _post_init(self, users):
        called = []

        async def noop():
            return None

        def boom(*a, **k):
            raise StopHere

        import tempfile
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(iris, "ALLOWED_USER_IDS", users), \
             mock.patch.object(iris, "OUTBOX_DIR", Path(d) / "outbox"), \
             mock.patch.object(iris, "_reconcile_orphaned_dispatches", noop), \
             mock.patch.object(iris, "_start_imessage_surface", lambda: called.append(True)), \
             mock.patch.object(iris, "AsyncIOScheduler", boom), \
             mock.patch.object(iris, "_telegram_bot", None), \
             mock.patch.object(iris, "_dispatch_semaphore", None):
            try:
                asyncio.run(iris._post_init(types.SimpleNamespace(bot=None)))
            except StopHere:
                pass
        return called

    def test_started_with_users(self):
        self.assertEqual(self._post_init({777}), [True])

    def test_not_started_without_users(self):
        self.assertEqual(self._post_init(set()), [])


class StopLoop(Exception):
    pass


class TestLoop(Harness):
    """Drives _imessage_loop for a fixed number of polls via a fake sleep."""

    def run_loop(self, polls, fetch_results, start_cursor=None, max_rowid=50,
                 save=None, replies=None):
        self.saved = []
        self.answered = []
        fetches = iter(fetch_results)
        n = {"sleep": 0}

        async def fake_sleep(_):
            n["sleep"] += 1
            if n["sleep"] > polls:
                raise StopLoop

        def fetch_new(db, after, allowed):
            self.calls.append(("fetch", after))
            r = next(fetches)
            if isinstance(r, Exception):
                raise r
            return r

        async def answer(handle, text):
            self.answered.append((handle, text, list(self.saved)))
            if text == "explode":
                raise RuntimeError("handler bug")
            return (replies or {}).get(text, "reply to " + text)

        p = [
            mock.patch.object(iris.asyncio, "sleep", fake_sleep),
            mock.patch.object(iris.imessage_bridge, "load_cursor", lambda p: start_cursor),
            mock.patch.object(iris.imessage_bridge, "max_rowid", lambda: max_rowid),
            mock.patch.object(iris.imessage_bridge, "fetch_new", fetch_new),
            mock.patch.object(iris.imessage_bridge, "save_cursor",
                              save or (lambda p, c: self.saved.append(c))),
            mock.patch.object(iris, "_answer_imessage", answer),
        ]
        for x in p:
            x.start()
            self.addCleanup(x.stop)
        with self.assertRaises(StopLoop):
            asyncio.run(iris._imessage_loop())

    def fetches(self):
        return [c for c in self.calls if c[0] == "fetch"]

    def test_first_run_starts_at_now_without_replay(self):
        self.run_loop(2, [(51, [(51, STEVE, "new")])], start_cursor=None, max_rowid=50)
        self.assertEqual(self.fetches(), [("fetch", 50)])
        self.assertEqual(self.saved, [50, 51, 51])
        self.assertEqual(self.answered, [(STEVE, "new", [50, 51])])

    def test_cursor_saved_per_message_before_its_answer(self):
        self.run_loop(1, [(12, [(9, STEVE, "a"), (11, STEVE, "b")])], start_cursor=5)
        self.assertEqual([(a[1], a[2]) for a in self.answered], [("a", [9]), ("b", [9, 11])])
        self.assertEqual(self.saved, [9, 11, 12])

    def test_echo_of_own_reply_skipped(self):
        self.run_loop(2, [(6, [(6, STEVE, "hi")]), (8, [(7, STEVE, "reply to hi"), (8, STEVE, "hi")])],
                      start_cursor=5)
        self.assertEqual([a[1] for a in self.answered], ["hi", "hi"])
        self.assertEqual(self.saved, [6, 6, 7, 8, 8])

    def test_echo_check_is_per_handle(self):
        other = "me@icloud.com"
        self.run_loop(2, [(6, [(6, STEVE, "hi")]), (7, [(7, other, "reply to hi")])],
                      start_cursor=5)
        self.assertEqual([(a[0], a[1]) for a in self.answered],
                         [(STEVE, "hi"), (other, "reply to hi")])

    def test_read_failure_alerts_once_per_outage_with_fda_hint(self):
        err = OSError("authorization denied")
        self.run_loop(4, [err, err, (7, [(7, STEVE, "back")]), err], start_cursor=5)
        alerts = [c[1] for c in self.calls if c[0] == "alert"]
        self.assertEqual(len(alerts), 2)
        self.assertIn("Full Disk Access", alerts[0])
        self.assertEqual(self.answered, [(STEVE, "back", [7])])

    def test_unable_to_open_also_gets_fda_hint(self):
        self.run_loop(1, [OSError("unable to open database file")], start_cursor=5)
        self.assertIn("Full Disk Access", [c[1] for c in self.calls if c[0] == "alert"][0])

    def test_schema_error_gets_different_hint(self):
        self.run_loop(1, [OSError("no such column: m.item_type")], start_cursor=5)
        alert = [c[1] for c in self.calls if c[0] == "alert"][0]
        self.assertNotIn("Full Disk Access", alert)
        self.assertIn("changed shape", alert)

    def test_handler_crash_does_not_kill_loop_or_skip_rest(self):
        self.run_loop(2, [(7, [(6, STEVE, "explode"), (7, STEVE, "next")]), (7, [])],
                      start_cursor=5)
        self.assertEqual([a[1] for a in self.answered], ["explode", "next"])
        self.assertEqual(self.fetches(), [("fetch", 5), ("fetch", 7)])

    def test_cursor_save_failure_still_answers(self):
        def bad_save(p, c):
            raise OSError("disk full")
        self.run_loop(1, [(6, [(6, STEVE, "hi")])], start_cursor=5, save=bad_save)
        self.assertEqual([a[1] for a in self.answered], ["hi"])
        self.assertFalse(any(c[0] == "alert" for c in self.calls))


if __name__ == "__main__":
    unittest.main()
