"""Tests for prebrief_fire_check.py A-35 auto-derive. Pure-logic (no real fires).

Run: python scripts/test_prebrief_fire_check.py  (exit 0 = all pass)
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "pfc", Path(__file__).with_name("prebrief_fire_check.py")
)
m = importlib.util.module_from_spec(_spec)
sys.modules["pfc"] = m  # @dataclass needs the module registered before exec
_spec.loader.exec_module(m)


def test_weekday_conversion():
    # launchd Sun=0..Sat=6  ->  python Mon=0..Sun=6
    assert m._launchd_wd_to_py(0) == 6, "Sun"   # launchd Sun -> py Sun(6)
    assert m._launchd_wd_to_py(1) == 0, "Mon"
    assert m._launchd_wd_to_py(3) == 2, "Wed"
    assert m._launchd_wd_to_py(4) == 3, "Thu"
    assert m._launchd_wd_to_py(6) == 5, "Sat"
    print("weekday conversion: pass")


def test_calendar_entry_derivation():
    lp = Path("/tmp/x.log")
    # A Wednesday-2:45 weekly entry, evaluated on a Wed at 06:00 -> in-window fire.
    wed = datetime(2026, 7, 1, 6, 0)  # 2026-07-01 is a Wednesday
    assert wed.weekday() == 2
    e = m._expected_from_calendar_entry(
        wed, {"Hour": 2, "Minute": 45, "Weekday": 3}, "topic-scout", lp
    )
    assert e and e.expected_at.hour == 2 and e.expected_at.minute == 45, e
    assert e.expected_at.weekday() == 2, e  # Wednesday

    # Same entry evaluated on a Thursday -> last Wed fire is >24h back -> None.
    thu = datetime(2026, 7, 2, 6, 0)
    assert m._expected_from_calendar_entry(
        thu, {"Hour": 2, "Minute": 45, "Weekday": 3}, "topic-scout", lp
    ) is None

    # Daily entry (no Weekday/Day) fires today.
    daily = m._expected_from_calendar_entry(
        wed, {"Hour": 3, "Minute": 0}, "nightly", lp
    )
    assert daily and daily.expected_at.hour == 3, daily

    # Hourly / wildcard-hour entry (no Hour) -> not modeled -> None.
    assert m._expected_from_calendar_entry(wed, {"Minute": 0}, "x", lp) is None
    print("calendar entry derivation: pass")


def test_log_path_derivation():
    LOGS = m.LOGS_DIR
    # run_claude_job.sh, -lc single-string form (nightly, topic-scout shape)
    d1 = {"ProgramArguments": ["/bin/bash", "-lc",
          "/x/scripts/run_claude_job.sh topic-scout /x/routines/topic-scout.prompt"]}
    assert m._derive_log_path(d1, "claude-code-topic-scout") == LOGS / "claude-code-topic-scout.log"
    # run_job.sh, split-element form (caption-sweep, pipeline-heartbeat shape)
    d2 = {"ProgramArguments": ["/bin/bash", "/x/scripts/run_job.sh",
          "pipeline-heartbeat", "/usr/bin/python3", "/x/scripts/po.py", "--supervise"]}
    assert m._derive_log_path(d2, "claude-code-pipeline-heartbeat") == LOGS / "job-pipeline-heartbeat.log"
    # neither wrapper → StandardOutPath fallback
    d3 = {"ProgramArguments": ["/x/bin/foo"], "StandardOutPath": "/tmp/foo.out"}
    assert m._derive_log_path(d3, "foo") == Path("/tmp/foo.out")
    print("log path derivation: pass")


def test_collect_runs_live():
    # Smoke: against the real ~/Library/LaunchAgents (or empty if absent), the
    # auto-derive must not crash and must return well-formed structures. On this
    # Mac it should find real claude-code plists and classify interval jobs as
    # not-checked (auth-canary, comment-sweep, pipeline-sweep, retry).
    now = datetime.now()
    expected, not_checked = m.collect_launchd_expected(now)
    assert isinstance(expected, list) and isinstance(not_checked, list)
    for e in expected:
        assert e.check == "launchd_log" and e.log_path is not None, e
        assert e.name not in (lbl.replace("com.iris.", "") for lbl in m._AUTODERIVE_SKIP), e
    # not_checked entries are (name, reason) tuples.
    for n, r in not_checked:
        assert isinstance(n, str) and isinstance(r, str)
    print(f"live collect: {len(expected)} derived, {len(not_checked)} not-checked — pass")

    # Full main() must run clean (surface-only, returns 0) and not raise.
    rc = m.main(["--skip-pre-brief-self"])
    assert rc == 0, rc
    print("main() end-to-end: pass")


if __name__ == "__main__":
    test_weekday_conversion()
    test_calendar_entry_derivation()
    test_log_path_derivation()
    test_collect_runs_live()
    print("ALL PASS")
