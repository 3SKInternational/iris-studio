"""The V8/V9 park regression (2026-07-26).

A CLI usage/session-limit notice goes to STDOUT and exits 1, leaving stderr EMPTY.
Every agent-dispatch site used to build its error from stderr alone, so the notice
was invisible, matched no INFRA marker, and was billed as a genuine task failure —
three of those park the video. V8/V9's 11_analyze parked exactly that way (their
last strike lines up with the 07:24 EDT canary limit). V12/V13 also parked but not
purely on quota: V13's first strike was a real analyze-reviewer REVISE.

These tests drive the REAL functions (the dispatch sites via a patched
po._run_agent, then _on_failure), not a local re-implementation of the error
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


def _stub_dispatch(stdout="", stderr="", returncode=1):
    """Patch the ONE seam every agent dispatch goes through.

    Must patch po._run_agent, NOT subprocess.run: the dispatch sites moved to
    _run_agent (which uses Popen to stream to disk), so a subprocess.run patch
    silently stops intercepting and the suite spawns EIGHT REAL agent dispatches.
    That actually happened — it hung the run and burned live processes.
    """
    saved = (po._run_agent, pathlib.Path.exists, subprocess.Popen, subprocess.run)
    po._run_agent = lambda *a, **k: _FakeProc(returncode, stdout, stderr)
    pathlib.Path.exists = lambda self: True  # the agent-definition presence check

    real_popen, real_run = subprocess.Popen, subprocess.run

    def _guard(real):
        """Trap CLI dispatches only. _proc_start_token and notify.sh legitimately
        shell out through subprocess.run; blanket-trapping them would fail the suite
        with "dispatch escaped" pointing at entirely the wrong thing."""
        def wrapper(cmd=None, *a, **k):
            argv0 = cmd[0] if isinstance(cmd, (list, tuple)) and cmd else cmd
            if argv0 == po.CLAUDE_CLI_PATH:
                raise AssertionError(
                    "dispatch escaped the _run_agent stub — would spawn a REAL agent")
            return real(cmd, *a, **k)
        return wrapper

    # Trap the raw spawn primitives as an INVARIANT of being stubbed, not as a
    # separate test. As a separate test it ran AFTER the test that hangs, so it
    # never fired: the escaping dispatch spawned a real process first. Here it
    # protects every stubbed test regardless of execution order.
    subprocess.Popen = _guard(real_popen)
    subprocess.run = _guard(real_run)
    return saved


def _unstub(saved):
    po._run_agent, pathlib.Path.exists, subprocess.Popen, subprocess.run = saved


def _dispatch_with(stdout="", stderr="", returncode=1):
    """Run the real _dispatch_stage_agent against a stubbed CLI invocation."""
    saved = _stub_dispatch(stdout, stderr, returncode)
    try:
        return po._dispatch_stage_agent("11_analyze", 8)
    finally:
        _unstub(saved)


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
    saved = _stub_dispatch(stdout=QUOTA_NOTICES[0])
    try:
        for name, call in DISPATCH_SITES:
            detail = call()[-1]
            assert po.QUOTA_SENTINEL in detail, f"{name} lost the quota sentinel: {detail!r}"
    finally:
        _unstub(saved)




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


def test_placeholder_title_is_never_injected_as_a_topic():
    """cmd_init defaults title to f"Video {nn}" when --title is omitted. Injecting
    that would order the agent to write a flagship script about the literal string
    "Video 15" AND forbid re-deriving it — worse than the missing-topic bug it was
    meant to fix, on the normal path for any video created without --title."""
    import json
    import tempfile
    real_dir = po.STATE_DIR
    with tempfile.TemporaryDirectory() as td:
        po.STATE_DIR = pathlib.Path(td)
        # Exactly what cmd_init writes when --title is omitted.
        (po.STATE_DIR / "Video_15_pipeline.json").write_text(
            json.dumps({"video": 15, "title": "Video 15", "stages": {}}))
        try:
            assert po._stage_title(15) == "", "placeholder must read as absent"
            prompt = po._stage_prompt("1_script", 15)
            assert '"Video 15"' not in prompt, f"placeholder leaked into prompt: {prompt!r}"
            assert "locked topic" not in prompt, \
                "must fall back to letting the agent find the brief"
            # ...and a REAL title in the same slot must still be injected.
            (po.STATE_DIR / "Video_15_pipeline.json").write_text(
                json.dumps({"video": 15, "title": "Every Level of Something", "stages": {}}))
            assert "Every Level of Something" in po._stage_prompt("1_script", 15)
        finally:
            po.STATE_DIR = real_dir


def test_real_title_is_injected():
    """A real title must reach the prompt — the whole point of the change.

    Uses a TEMP state dir, never the live vault: _stage_title is deliberately
    defensive and returns "" on any read error, so asserting against a real
    Production_Kits file turns a missing/unreadable state file (they do get
    deleted, and the FDA-revocation failure mode EPERMs that whole path) into a
    silent false RED with a misleading message.
    """
    import json
    import tempfile
    real_dir = po.STATE_DIR
    with tempfile.TemporaryDirectory() as td:
        po.STATE_DIR = pathlib.Path(td)
        (po.STATE_DIR / "Video_14_pipeline.json").write_text(
            json.dumps({"video": 14, "title": "POV: Your Life If You Started At 18",
                        "stages": {}}))
        try:
            prompt = po._stage_prompt("1_script", 14)
            assert "POV: Your Life If You Started At 18" in prompt
            assert "do not re-derive" in prompt
        finally:
            po.STATE_DIR = real_dir


def test_reaper_kills_registered_agent_groups():
    """A dying orchestrator must take its detached agents with it. start_new_session
    removes them from launchd's job process group, so launchd's own cleanup no
    longer reaches them — without this, `launchctl kickstart -k` mid-stage leaves an
    agent writing to the vault while the next sweep dispatches a second one."""
    import os
    import time
    proc = subprocess.Popen(["/bin/sh", "-c", "sleep 30"], start_new_session=True)
    pgid = os.getpgid(proc.pid)
    po._LIVE_AGENT_PGIDS.add(pgid)
    try:
        po._reap_live_agents()
        time.sleep(0.3)
        assert proc.poll() is not None, "reaper left the agent alive"
        assert not po._LIVE_AGENT_PGIDS, "reaper did not clear its registry"
    finally:
        try:
            os.killpg(pgid, 9)
        except OSError:
            pass
        proc.wait()


def test_log_labels_do_not_collide_between_roles():
    """The same agent runs twice per video (draft then fixer; Pass A then Pass B).
    Without a role the second run overwrites the first — destroying the pre-spend
    money-gate log and the stage-1 draft log, the exact evidence this exists for."""
    cmd = po._build_agent_cmd("scriptwriter", "Draft the production script for Video_14 ...")
    produce, fix = po._agent_log_label(cmd, "produce"), po._agent_log_label(cmd, "fix")
    assert produce != fix, f"draft and fixer logs collide: {produce}"
    img = po._build_agent_cmd("image-reviewer", "Review Video_14 images ...")
    assert po._agent_log_label(img, "prompts") != po._agent_log_label(img, "renders")


def _with_temp_log_dir(fn):
    """Run fn() against a throwaway AGENT_LOG_DIR."""
    import tempfile
    real = po.AGENT_LOG_DIR
    with tempfile.TemporaryDirectory() as td:
        po.AGENT_LOG_DIR = pathlib.Path(td)
        try:
            return fn()
        finally:
            po.AGENT_LOG_DIR = real


def test_run_agent_keeps_streams_separate():
    """_run_agent is stubbed out by every other test, so its own contract was
    unguarded — merging the two streams (which would break _agent_failure_detail's
    trust boundary: general markers scan stderr, quota markers scan stdout) passed
    the entire suite."""
    p = _with_temp_log_dir(lambda: po._run_agent(
        ["/bin/sh", "-c", "echo OUT; echo ERR 1>&2; exit 3"], 30, "sep"))
    assert p.returncode == 3
    assert p.stdout.strip() == "OUT", f"stdout polluted: {p.stdout!r}"
    assert p.stderr.strip() == "ERR", f"stderr polluted: {p.stderr!r}"


def test_run_agent_preserves_partial_output_on_timeout():
    """The entire justification for streaming to disk: evidence must survive a
    timeout. In-memory buffering threw it away, which is why V14's missing-topic
    defect took three failed dispatches to spot."""
    def run():
        try:
            po._run_agent(["/bin/sh", "-c", "echo PARTIAL; sleep 30"], 1, "to")
            raise AssertionError("expected TimeoutExpired")
        except subprocess.TimeoutExpired:
            return (po.AGENT_LOG_DIR / "to.out.log").read_text()
    assert "PARTIAL" in _with_temp_log_dir(run)


def test_run_agent_reaps_grandchildren_on_timeout():
    """The CLI spawns children that inherit the log fds. Killing only the direct
    child leaves them writing into the next attempt's log."""
    import time

    def run():
        try:
            po._run_agent(
                ["/bin/sh", "-c", "(sleep 43 &) ; sleep 43"], 1, "gc")
            raise AssertionError("expected TimeoutExpired")
        except subprocess.TimeoutExpired:
            time.sleep(0.3)
            out = subprocess.run(["/bin/ps", "-ax", "-o", "command"],
                                 capture_output=True, text=True).stdout
            return [l for l in out.splitlines() if l.strip() == "sleep 43"]
    assert _with_temp_log_dir(run) == [], "grandchild survived the timeout kill"


def test_sigterm_is_not_swallowed_by_an_except_systemexit():
    """The reaper must NOT terminate via sys.exit(). SystemExit unwinds through
    Python, and cmd_advance_all's per-video `except SystemExit` (written for die())
    catches it — so a SIGTERMed sweep swallowed its own termination, advanced to the
    next video, dispatched a FRESH agent, then got SIGKILLed by launchd ~20s later.
    SIGKILL bypasses the reaper, so that agent survived detached: the exact hole the
    reaper exists to close. It also sent a false "state file corrupt" alert and
    exited 0. Re-signalling with SIG_DFL restored cannot be intercepted."""
    import signal as _signal
    import textwrap
    scripts = str(pathlib.Path(__file__).resolve().parent.parent / "scripts")
    prog = textwrap.dedent(f"""
        import os, signal, sys
        sys.path.insert(0, {scripts!r})
        import pipeline_orchestrator as po
        po.install_agent_reaper()
        try:
            os.kill(os.getpid(), signal.SIGTERM)   # what launchd sends
        except SystemExit:
            print("SWALLOWED"); sys.exit(0)        # cmd_advance_all's handler
        print("CONTINUED"); sys.exit(0)
    """)
    p = subprocess.run([sys.executable, "-c", prog], capture_output=True, text=True)
    assert p.returncode == -_signal.SIGTERM, f"rc={p.returncode} stdout={p.stdout!r}"
    assert "SWALLOWED" not in p.stdout and "CONTINUED" not in p.stdout, p.stdout


def test_grandchild_cannot_contaminate_the_next_attempt_on_the_normal_path():
    """killpg must run in the finally on EVERY path, not just timeout. A grandchild
    that outlives the CLI keeps the inherited log fd; when the next attempt reuses
    the label, its stale bytes land in that attempt's stdout, where
    _agent_failure_detail scans them — forging an infra verdict on a genuine
    failure. Append mode alone removes the NUL hole, NOT the contamination."""
    quota = "You've hit your usage limit · resets 7:30am (America/New_York)"

    def run():
        po._run_agent(["/bin/sh", "-c", f'( sleep 3; echo "{quota}" ) & echo OK; exit 0'],
                      30, "lbl")
        return po._run_agent(["/bin/sh", "-c", "echo REAL_FAILURE; sleep 5; exit 1"],
                             30, "lbl")
    p2 = _with_temp_log_dir(run)
    assert quota not in p2.stdout, f"stale quota notice contaminated attempt 2: {p2.stdout!r}"
    assert not po._is_infra_failure(po._agent_failure_detail(p2)), "forged an infra verdict"


def test_log_label_sanitiser_strips_path_separators():
    """The label is safe by construction today (digits from a regex + an agent name
    from module constants), so this calls the sanitiser directly rather than routing
    through _build_agent_cmd, where it would be unreachable and guard nothing."""
    label = po._agent_log_label(["x", "--agent", "../../pwn", "--", "Video_01"], "produce")
    # ".." surviving is fine — without a separator it cannot traverse. The property
    # that matters is that the log stays inside AGENT_LOG_DIR.
    assert "/" not in label, label
    resolved = (po.AGENT_LOG_DIR / f"{label}.out.log").resolve()
    assert resolved.parent == po.AGENT_LOG_DIR.resolve(), resolved


def test_run_agent_falls_back_when_logging_is_impossible():
    """Logging must never be able to fail a stage."""
    import tempfile
    real = po.AGENT_LOG_DIR
    with tempfile.TemporaryDirectory() as td:
        blocker = pathlib.Path(td) / "not-a-dir"
        blocker.write_text("")           # a FILE where the log dir should be
        po.AGENT_LOG_DIR = blocker / "stages"
        try:
            p = po._run_agent(["/bin/sh", "-c", "echo OUT; echo ERR 1>&2"], 30, "fb")
            assert p.stdout.strip() == "OUT" and p.stderr.strip() == "ERR"
        finally:
            po.AGENT_LOG_DIR = real


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
