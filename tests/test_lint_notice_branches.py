#!/usr/bin/env python3
"""Pins the A-23 lint-notice branches and publish_video's schedule notice.

WHY THESE EXIST. Both were fixes whose conditionals SURVIVED mutation while
inline in a main()/_finish_dispatch body — nothing in the suite reached them, so
either branch could have been inverted in production and no test would have said
a word. Both were extracted into pure functions purely so they could be pinned.

What they guard:
  * iris.py — agent_output_lint now sets should_alert=True on its OWN crash, so a
    linter that COULD NOT RUN must be reported differently from one that ran and
    found nothing. Without the distinction a crash logs "0 banned-vocab
    occurrence(s)", which reads as a clean result for output that was never
    checked. The A2 auto-pass gate keys off should_alert, so this is the human-
    visible half of the same defect.
  * publish_video.py — the schedule notice is the ONLY warning a human gets
    before a scheduled release is preserved or dropped.

Run: python3 tests/test_lint_notice_branches.py   (exit 0 = pass)
"""
from __future__ import annotations

import asyncio
import importlib
import importlib.util
import os
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _stub(name: str, force: bool = False, **attrs) -> None:
    """Install a stub module. `force=True` replaces an installed real one.

    Forcing matters for dotenv: its import IS the side effect (it reads .env and
    populates os.environ with live credentials), so "only stub it if missing"
    silently loads real secrets whenever the package happens to be installed.
    See feedback_force_stub_side_effect_modules.
    """
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
    """Import the REAL iris.py with its heavy optional deps stubbed.

    Same loader as tests/test_pipeline_subprocess_timeout.py, which in turn
    follows tests/test_ceo_brief_recency.py — reused verbatim rather than
    re-derived, because a hand-rolled stub set is exactly the kind of check that
    silently stops exercising the real module.
    """
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

    spec = importlib.util.spec_from_file_location("iris_under_test_lint", ROOT / "iris.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_publish_video():
    spec = importlib.util.spec_from_file_location(
        "publish_video_under_test", ROOT / "scripts" / "publish_video.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- iris.py: lint_log_records -------------------------------------------------

def test_lint_alert_crash_is_a_warning(iris):
    recs = iris.lint_log_records({"should_alert": True, "error": "read failed: boom"},
                                 "d1", "scriptwriter")
    assert len(recs) == 1, recs
    level, detail = recs[0]
    assert level == "warning", (level, detail)
    assert "read failed: boom" in detail
    assert "NOT lint-clean" in detail
    # the level string must name a real logger method — the caller does getattr(logger, level)
    import logging
    assert callable(getattr(logging.getLogger("x"), level, None)), level


def test_lint_alert_findings_are_info_with_the_count(iris):
    recs = iris.lint_log_records(
        {"should_alert": True, "banned": ["cinematic", "sweeping"], "report_path": "/x_lint.md"},
        "d2", "scriptwriter")
    assert len(recs) == 1, recs
    level, detail = recs[0]
    assert level == "info", (level, detail)
    assert "2 banned-vocab occurrence(s)" in detail
    assert "/x_lint.md" in detail
    import logging
    assert callable(getattr(logging.getLogger("x"), level, None)), level


def test_lint_alert_crash_never_reports_a_zero_count(iris):
    """THE regression. A crash has no `banned` key, so the non-error branch would
    render "0 banned-vocab occurrence(s)" — a checked-and-clean claim about output
    nothing ever checked."""
    recs = iris.lint_log_records({"should_alert": True, "error": "X9 unmounted"}, "d3", "a")
    assert "0 banned-vocab" not in recs[0][1], recs


def test_a23_lint_exception_in_executor_path_is_not_clean(iris):
    """ROUND-2 REGRESSION (2026-07-28 review, MEDIUM-5). THE THIRD ARM: an
    exception raised by the run_in_executor call itself (not by
    agent_output_lint's own internals, which are separately hardened) used to
    leave lint_result at its None default. The A2 gate computes
    `lint_clean = not (lint_result and lint_result.get("should_alert"))`, which
    is True on None -- an unlinted deliverable auto-passed. _run_a23_lint must
    return a dict with should_alert=True, matching agent_output_lint's own
    error-arm shape, so the EXISTING lint_log_records/lint_telegram_suffix
    "err = lint_result.get('error')" branch (tested above) already renders it
    correctly."""
    class _BoomLint:
        @staticmethod
        def lint_and_report(path):
            raise RuntimeError("cannot schedule new futures after shutdown")

    old = iris._agent_output_lint
    iris._agent_output_lint = _BoomLint
    try:
        result = asyncio.run(iris._run_a23_lint("/tmp/x_deliverable.md", "d9", "scriptwriter"))
    finally:
        iris._agent_output_lint = old

    assert result is not None, "lint_result must never be left None on an executor exception"
    assert result.get("should_alert") is True, result
    assert "cannot schedule new futures" in result.get("error", ""), result
    # The A2 gate's own formula, exercised directly against the real return value —
    # this is the line that actually decides whether an unlinted deliverable
    # auto-passes.
    lint_clean = not (result and result.get("should_alert"))
    assert lint_clean is False, "an executor-path exception must NOT read as lint-clean"


def test_a23_lint_success_path_still_returns_the_real_result(iris):
    """Control case: _run_a23_lint must still pass through a normal (non-exception)
    lint_and_report() result unchanged, not just the exception arm."""
    class _CleanLint:
        @staticmethod
        def lint_and_report(path):
            return {"path": str(path), "banned": [], "should_alert": False, "ok": True}

    old = iris._agent_output_lint
    iris._agent_output_lint = _CleanLint
    try:
        result = asyncio.run(iris._run_a23_lint("/tmp/x_deliverable.md", "d10", "scriptwriter"))
    finally:
        iris._agent_output_lint = old

    assert result == {"path": "/tmp/x_deliverable.md", "banned": [],
                      "should_alert": False, "ok": True}, result


def test_lint_alert_silent_when_not_alerting(iris):
    """Silence is an EMPTY LIST, not a sentinel — the caller loops over this, so an
    empty result is what makes the no-op case branch-free at the call site."""
    assert iris.lint_log_records({"should_alert": False}) == []
    assert iris.lint_log_records(None) == []
    assert iris.lint_log_records({}) == []


# --- iris.py: lint_telegram_suffix --------------------------------------------

def test_telegram_suffix_crash_says_unchecked(iris):
    out = iris.lint_telegram_suffix({"should_alert": True, "error": "read failed: boom"},
                                    lambda p: p)
    assert "COULD NOT RUN" in out and "unchecked, not clean" in out, out
    assert "0 banned-vocab" not in out, out


def test_telegram_suffix_findings_use_vault_rel(iris):
    seen = []

    def vault_rel(p):
        seen.append(p)
        return "REL/" + str(p)

    out = iris.lint_telegram_suffix(
        {"should_alert": True, "banned": ["x"], "report_path": "/abs/x_lint.md"}, vault_rel)
    assert seen == ["/abs/x_lint.md"], seen
    assert "REL//abs/x_lint.md" in out, out
    assert "1 banned-vocab occurrence(s)" in out, out


def test_telegram_suffix_handles_missing_report_path(iris):
    out = iris.lint_telegram_suffix({"should_alert": True, "banned": []}, lambda p: p)
    assert "(in-memory)" in out, out


def test_telegram_suffix_empty_when_not_alerting(iris):
    assert iris.lint_telegram_suffix({"should_alert": False}, lambda p: p) == ""
    assert iris.lint_telegram_suffix(None, lambda p: p) == ""


# --- publish_video.py: schedule_notice ----------------------------------------

SCHEDULED = {"privacyStatus": "private", "publishAt": "2026-10-01T13:00:00Z"}


def test_notice_announces_preservation(pv):
    st = {"publishAt": "2026-10-01T13:00:00Z"}
    out = pv.schedule_notice(st, SCHEDULED, publish_at=None, clear_schedule=False)
    assert len(out) == 1, out
    assert "PRESERVING" in out[0] and "2026-10-01T13:00:00Z" in out[0], out
    assert "--clear-schedule" in out[0], "must tell the operator how to opt out"


def test_notice_announces_clearing(pv):
    out = pv.schedule_notice({}, SCHEDULED, publish_at=None, clear_schedule=True)
    assert len(out) == 1, out
    assert "CLEARING" in out[0] and "2026-10-01T13:00:00Z" in out[0], out


def test_notice_silent_on_an_explicit_new_schedule(pv):
    """--publish-at is the operator stating the schedule outright; the plan block
    already prints it, so this line would be noise."""
    st = {"publishAt": "2026-11-05T09:00:00+00:00"}
    assert pv.schedule_notice(st, SCHEDULED, publish_at="2026-11-05T09:00:00Z",
                              clear_schedule=False) == []


def test_notice_silent_when_there_was_never_a_schedule(pv):
    live = {"privacyStatus": "private"}
    assert pv.schedule_notice({}, live, publish_at=None, clear_schedule=False) == []
    assert pv.schedule_notice({}, live, publish_at=None, clear_schedule=True) == []


def test_notice_silent_when_both_publish_at_and_clear_schedule(pv):
    """HAND-WRITTEN because mutation is structurally blind to the line it guards.

    The round-3 M1 fix was adding `and not publish_at` to the CLEARING branch:
    with BOTH --publish-at and --clear-schedule the run SETS a new schedule, so
    announcing "CLEARING existing publishAt <old>" told the operator the opposite
    of what happened.

    Round-4 review proved that fix shipped UNGUARDED — reverting the conjunct left
    all 43 suites and every mutant green, with the gate printing CLEARED. mutate.py
    has `flip-bool` (rewrites the whole operator) and `if-test->True/False`
    (replaces the whole test), but NO drop-a-conjunct operator, so ADDING a term to
    an existing `and` chain is invisible to both halves of the gate. Only a fixture
    can hold this line. Revert `and not publish_at` and this case goes red."""
    st = pv.build_status("private", SCHEDULED, "2030-05-05T09:00:00Z",
                         clear_schedule=True)
    assert st["publishAt"] == "2030-05-05T09:00:00+00:00", \
        "the new schedule must still be sent when both flags are passed"
    assert pv.schedule_notice(st, SCHEDULED, "2030-05-05T09:00:00Z",
                              clear_schedule=True) == [], \
        "must NOT announce CLEARING on a run that installs a new schedule"


if __name__ == "__main__":
    iris = _load_iris()
    pv = _load_publish_video()
    n = 0
    for name, fn in sorted(globals().items()):
        if not (name.startswith("test_") and callable(fn)):
            continue
        arg = fn.__code__.co_varnames[0]
        if arg == "iris":
            if iris is None:
                continue
            fn(iris)
        else:
            fn(pv)
        n += 1
    print(f"test_lint_notice_branches: {n}/{n} pass")
