"""The V8/V9 park regression (2026-07-26).

A CLI usage/session-limit notice goes to STDOUT and exits 1, leaving stderr EMPTY.
Every agent-dispatch site used to build its error from stderr alone, so the notice
was invisible, matched no INFRA marker, and was billed as a genuine task failure —
three of those park the video. V8/V9's 11_analyze parked exactly that way (their
last strike lines up with the 07:24 EDT canary limit). V12/V13 also parked but not
purely on quota: V13's first strike was a real analyze-reviewer REVISE.

These tests drive the REAL functions (_dispatch_stage_agent via a patched
subprocess.run, then _on_failure), not a local re-implementation of the error
assembly — an earlier version of this file asserted against its own copy of the
string-building and therefore passed with the fix reverted, guarding nothing.
"""
import pathlib
import subprocess
import sys
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
import pipeline_orchestrator as po  # noqa: E402


# Literal CLI output, as captured by auth_canary.sh on 2026-07-24 and 2026-07-26.
QUOTA_NOTICES = [
    "You've hit your session limit · resets 11:20pm (America/New_York)",
    "You've hit your usage limit · resets 7:30am (America/New_York)",
]


class _FakeProc:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def _dispatch_with(stdout="", stderr="", returncode=1):
    """Run the real _dispatch_stage_agent against a stubbed CLI invocation."""
    real_run, real_exists = subprocess.run, pathlib.Path.exists
    subprocess.run = lambda *a, **k: _FakeProc(returncode, stdout, stderr)
    pathlib.Path.exists = lambda self: True  # the agent-definition presence check
    try:
        return po._dispatch_stage_agent("11_analyze", 8)
    finally:
        subprocess.run, pathlib.Path.exists = real_run, real_exists


def _fresh_stage():
    return {"status": "running", "fail_count": 0, "infra_count": 0,
            "park_reason": None, "note": None, "started_at": None,
            "completed_at": None, "pid": None, "pid_start_token": None}


def test_quota_notice_on_stdout_does_not_burn_a_retry():
    """The whole point: a quota hit must leave the stage retryable, fail_count 0."""
    for notice in QUOTA_NOTICES:
        ok, detail = _dispatch_with(stdout=notice, stderr="")
        assert not ok
        assert po._is_infra_failure(detail), f"must classify INFRA: {detail!r}"
        s = _fresh_stage()
        po._on_failure(s, detail)
        assert s["fail_count"] == 0, f"quota must not bill a strike: {s}"
        assert s["status"] == "ready", f"stage must stay retryable: {s}"


def test_genuine_task_failure_still_parks():
    """The narrow-marker discipline: a real failure must still consume retries."""
    s = _fresh_stage()
    for _ in range(3):
        ok, detail = _dispatch_with(stderr="FileNotFoundError: analytics export missing")
        assert not ok
        assert not po._is_infra_failure(detail)
        po._on_failure(s, detail)
    assert s["fail_count"] == 3 and s["status"] == "needs-steve", s


def test_agent_prose_cannot_forge_an_infra_marker():
    """Agent stdout is appended for observability but must NOT be scanned for the
    general markers — 'command not found' is ordinary agent transcript text."""
    ok, detail = _dispatch_with(
        stdout="I ran the export and got: bash: jq: command not found",
        stderr="agent reported a task failure")
    assert not ok
    assert not po._is_infra_failure(detail), f"prose must not read as infra: {detail!r}"


def test_marker_survives_truncation_of_a_huge_stdout():
    """Classification must not depend on the marker landing in the last 500 chars."""
    notice = QUOTA_NOTICES[0]
    ok, detail = _dispatch_with(stdout=notice + ("x" * 20000))
    assert not ok
    assert po._is_infra_failure(detail), "sentinel must survive stdout truncation"


def test_quota_sentinel_is_an_infra_marker():
    assert po.QUOTA_SENTINEL in po.INFRA_FAILURE_MARKERS


# Every function that shells `claude --print` via _build_agent_cmd. Round-1 of this
# fix patched only the producer and left seven siblings broken — "fix one call site,
# siblings stay broken" IS the incident, so each one is driven here. The detail
# string is the last element of every return tuple.
DISPATCH_SITES = [
    ("_dispatch_stage_agent", lambda: po._dispatch_stage_agent("11_analyze", 8)),
    ("run_image_review", lambda: po.run_image_review("renders", 8)),
    ("run_prompt_fixer", lambda: po.run_prompt_fixer(8, "v.md", "m.json")),
    ("run_script_review", lambda: po.run_script_review(8)),
    ("run_script_fixer", lambda: po.run_script_fixer(8, "v.md")),
    ("run_vo_review", lambda: po.run_vo_review(8, "kit.md")),
    ("run_stage_review", lambda: po.run_stage_review("11_analyze", 8)),
    ("run_stage_fixer", lambda: po.run_stage_fixer("11_analyze", 8, "v.md")),
]


def test_every_agent_dispatch_site_reports_quota():
    """A quota hit must be identifiable from EVERY dispatch site's error string."""
    notice = QUOTA_NOTICES[0]
    real_run, real_exists = subprocess.run, pathlib.Path.exists
    subprocess.run = lambda *a, **k: _FakeProc(1, notice, "")
    pathlib.Path.exists = lambda self: True
    try:
        for name, call in DISPATCH_SITES:
            detail = call()[-1]
            assert po.QUOTA_SENTINEL in detail, f"{name} lost the quota sentinel: {detail!r}"
    finally:
        subprocess.run, pathlib.Path.exists = real_run, real_exists


def test_sentinel_survives_a_long_stderr_with_empty_stdout():
    """Bound-then-prefix ordering: prefixing first and slicing [-500:] afterwards
    chops from the FRONT and destroys the sentinel."""
    notice = QUOTA_NOTICES[0]
    ok, detail = _dispatch_with(stdout="", stderr="noise line\n" * 200 + notice)
    assert not ok
    assert po._is_infra_failure(detail), f"sentinel eaten by truncation: {detail!r}"


def test_general_marker_scan_stays_bounded_to_a_tail():
    """An unbounded stderr scan would let a marker buried in a long stderr forge an
    infra verdict on what is really a genuine task failure."""
    ok, detail = _dispatch_with(
        stderr="bash: jq: command not found" + "\npadding" * 400,
        stdout="ok")
    assert not ok
    assert not po._is_infra_failure(detail), f"deep-stderr marker forged infra: {detail!r}"


def test_credit_exhaustion_is_not_infra():
    """Deliberately excluded: it does not self-heal, so it should park for Steve."""
    ok, detail = _dispatch_with(stdout="Credit balance is too low")
    assert not po._is_infra_failure(detail)


def test_script_stage_timeout_exceeds_measured_drafts():
    """V12 drafted in 730s, V13 in 840s; V14 blew through 1200s twice."""
    assert po.RUN_TABLE["1_script"]["timeout"] >= 840 * 2


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and isinstance(v, types.FunctionType)]
    for fn in fns:
        fn()
    print(f"ok — {len(fns)} tests: quota retries, real failures park, "
          f"prose cannot forge infra, sentinel survives truncation")
