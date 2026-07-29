#!/usr/bin/env python3
"""Stale-pipeline-state lint — catch pipeline stages whose status no longer
matches reality, BEFORE they turn into recurring "needs Steve" noise.

Sibling of pipeline_stage_truth_lint / verdict_resolution_lint / youtube_reality_check
in the read-only house-linter family. Surface-only: it never edits state.

WHY THIS EXISTS (2026-07-25). Steve's hourly pipeline-sweep Telegram was reporting
that Videos 1-12 "need Steve" when every one of them was already published. Audit
found 26 pre-publish stages still sitting at needs-steve/blocked across 6 PUBLISHED
videos — plus one stage stuck at 'running' with no process behind it. The sweep
(`pipeline_orchestrator.py --advance-all`) iterates every video and has no
published-skip, so each stale stage regenerated the same false alert every hour,
forever. Alert fatigue is the cheap cost; the expensive one is that a real
"needs Steve" gets lost in a wall of fake ones.

THE TWO STALE CLASSES IT DETECTS

 1. PUBLISHED-BUT-OPEN — a video with a live upload receipt whose PRE-publish
    stage is not `done`. The receipt is proof the stage completed: you cannot
    publish a video whose images/VO/assembly never finished. Any such status is
    stale by definition. (11_analyze is deliberately EXEMPT — it is a genuine
    POST-publish stage and may legitimately be pending or running.)

 2. ORPHANED-RUNNING — a stage marked `running` with no live process owning it.
    A killed, crashed, or timed-out run leaves this behind, and because the
    orchestrator treats `running` as "someone else owns this", the stage is
    stuck forever and silently blocks everything downstream. Detected by PID
    (dead, or recycled to another process) via the orchestrator's own
    _pid_alive/_proc_start_token, so this linter's verdict always agrees with
    _looks_orphaned. Age is only a fallback when the pid signals are unreadable,
    and its margin is set against the REAL max stage timeout (3600s, 5_images).

Exit 0 = clean, exit 1 = stale state found (Telegram-pinged by the caller), exit 75
(EX_TEMPFAIL) = zero-scan (the target vault path was unavailable — see the guard
below; NOT a clean result, distinguishable from exit 0/1). This script is not
currently invoked through run_job.sh (see the CONSUMER note on the zero-scan guard
below) — the 75/EX_TEMPFAIL convention is inherited only as a shared exit-code
vocabulary with the scripts that ARE, so a human reading either doesn't have to
learn two meanings for the same number.
Read-only; the operator decides the fix. `--selftest` runs hermetic fixtures.

Usage:
  python3 scripts/pipeline_stale_state_lint.py            # lint, human output
  python3 scripts/pipeline_stale_state_lint.py --json     # machine-readable
  python3 scripts/pipeline_stale_state_lint.py --selftest # hermetic self-check
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_VAULT = "~/Documents/3SK/outputs/BRANDS/3SK_Finance"
KITS_SUBDIR = "Production_Kits"

# Stages that MUST be done once a video has a live upload receipt.
#
# TWO DELIBERATE EXEMPTIONS — both verified against pipeline_orchestrator, not
# assumed:
#   11_analyze    — deps ["10_publish"], i.e. genuinely POST-publish work. A
#                   pending/running analyze on a shipped video is real work.
#   12_leadmagnet — deliberately NOT a dep of 10_publish (orchestrator:356-365:
#                   "a REVISE-park on the magnet must never wedge publish").
#                   Only the draft+review is automatable; the PDF render → Drive
#                   host → URL fill is manual and out-of-pipeline. So a video
#                   can legally publish with the magnet parked at needs-steve,
#                   and flagging it would emit a false hourly ping forever.
PRE_PUBLISH_STAGES = (
    "1_script", "2_review", "3_vo_expand", "4_vo_record", "5_images",
    "6_assemble", "7_packaging", "8_thumbnail", "9_description", "10_publish",
)

# Age fallback for orphan detection, used ONLY when the pid signals are
# unreadable. The real max per-stage timeout is 3600s (5_images — a full billed
# render legitimately holds `running` for up to an hour); 2h is a 2x margin over
# that. NOT 720s — that is 11_analyze's timeout, not the maximum.
STALE_RUNNING_HOURS = 2.0

DONE = "done"


def vault() -> Path:
    return Path(os.path.expanduser(os.environ.get("SK_VAULT", DEFAULT_VAULT))).resolve()


def is_published(kits: Path, label: str) -> bool:
    """A live upload receipt with a NON-EMPTY video_id => the video shipped.

    Mirrors pipeline_stage_truth_lint.is_published exactly — two linters in the
    same family must not disagree on "is this published". A receipt can exist
    with `video_id: null` (Video_04 on disk right now: status
    "deleted_from_channel"), and a mere `.is_file()` test would call that
    published — so a re-cut of a deleted video, whose --force-init resets every
    stage to `blocked`, would emit a false finding per stage.
    """
    try:
        d = json.loads((kits / f"{label}_youtube_upload.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    vid = d.get("video_id")
    return isinstance(vid, str) and bool(vid.strip())


def _stages(doc: dict):
    """The stage map, or None if the doc is structurally corrupt.

    Tolerates both {'stages': {...}} and a flat top-level map (all 10 live files
    use the former). A `stages` key present but NOT a dict is corruption, not a
    flat doc — returning None makes the caller report it instead of silently
    iterating top-level scalars and reading CLEAN.
    """
    if "stages" in doc:
        return doc["stages"] if isinstance(doc["stages"], dict) else None
    return doc


def _age_hours(value, now: datetime) -> float | None:
    """Hours since an ISO-8601 timestamp, or None if unparseable/absent.

    Returns None (rather than 0) on a bad value so a malformed timestamp is
    reported as unknown-age instead of silently reading as 'just started' —
    which would hide a genuine orphan.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip().replace("Z", "+00:00")
    try:
        ts = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (now - ts).total_seconds() / 3600.0


def _orphan_reason(stage: dict, key: str, now: datetime) -> str | None:
    """Why this `running` stage has nobody home, or None if it looks live.

    PID FIRST, age only as fallback. `_mark_running` always writes `pid` and
    `pid_start_token` alongside `started_at`, and the orchestrator's own
    `_looks_orphaned` keys on those — so keying on age alone would (a) miss a
    dead-pid orphan that is only minutes old, and (b) disagree with the
    orchestrator's verdict, which defeats the point of a linter. Delegates to
    the orchestrator's helpers when importable so the two can never drift.
    """
    pid = stage.get("pid")
    try:
        here = str(Path(__file__).resolve().parent)
        if here not in sys.path:
            sys.path.insert(0, here)
        from pipeline_orchestrator import _pid_alive, _proc_start_token  # type: ignore
    except Exception as e:
        # NEVER degrade silently: without the pid helpers this falls back to the
        # 2h age rule, re-opening a blind window in which a dead-pid orphan reads
        # CLEAN. Warn loudly so a broken/renamed orchestrator is visible instead
        # of the linter reporting healthy while half-blind.
        print(f"pipeline_stale_state_lint: WARNING — pipeline_orchestrator "
              f"unimportable ({e.__class__.__name__}: {e}); orphan detection has "
              f"fallen back to the {STALE_RUNNING_HOURS:.0f}h AGE rule and can "
              f"miss a fresh dead-pid orphan.", file=sys.stderr)
        _pid_alive = _proc_start_token = None   # fall through to age

    if _pid_alive is not None and isinstance(pid, int):
        if not _pid_alive(pid):
            return (f"pid {pid} is not alive — the run that owned this stage is "
                    f"gone (killed/crashed), so it is stuck forever and silently "
                    f"blocks everything downstream")
        cur = _proc_start_token(pid)
        rec = stage.get("pid_start_token")
        if cur is not None and rec is not None and cur != rec:
            return (f"pid {pid} was RECYCLED (a different process now holds that "
                    f"pid) — the original run is gone")
        return None      # pid alive and not recycled => genuinely live

    # Fallback: no usable pid signal. Use age against a 2x margin over the real
    # 3600s max stage timeout.
    age = _age_hours(stage.get("started_at"), now)
    if age is None:
        return ("no readable pid and no parseable start timestamp — liveness "
                "cannot be verified, so it cannot be confirmed live")
    if age > STALE_RUNNING_HOURS:
        return (f"no usable pid signal and it has been 'running' for {age:.1f}h — "
                f"beyond the {STALE_RUNNING_HOURS:.0f}h margin over the longest "
                f"stage timeout (3600s), so no process owns it")
    return None


def lint_doc(label: str, doc: dict, published: bool, now: datetime) -> list[dict]:
    """Findings for one video's state doc. Pure — no disk, no clock of its own."""
    out: list[dict] = []
    st = _stages(doc)
    if st is None:
        return [{"video": label, "stage": "-", "status": "-", "kind": "unreadable",
                 "detail": (f"{label} has a 'stages' key that is not a dict — the "
                            f"state file is structurally corrupt. Reported rather "
                            f"than silently skipped.")}]

    for key, stage in st.items():
        if not isinstance(stage, dict):
            continue
        status = stage.get("status")

        # Class 1 — published, but a pre-publish stage isn't done.
        if published and key in PRE_PUBLISH_STAGES and status != DONE:
            out.append({
                "video": label, "stage": key, "status": status,
                "kind": "published-but-open",
                "detail": (f"{label} has a live upload receipt but {key} is "
                           f"'{status}' — publishing proves this stage completed, "
                           f"so the status is stale. It regenerates a false "
                           f"'needs Steve' every sweep, and effective_status can "
                           f"recompute it to 'ready' and RE-DISPATCH the stage's "
                           f"agent on an already-published video (token burn)."),
            })

        # Class 2 — running with nobody home. pid first, age only as fallback.
        elif status == "running":
            reason = _orphan_reason(stage, key, now)
            if reason:
                out.append({
                    "video": label, "stage": key, "status": status,
                    "kind": "orphaned-running",
                    "detail": f"{label} {key} is stuck 'running': {reason}.",
                })
    return out


def _pipeline_files(vlt: Path) -> list[Path]:
    """The Video_*_pipeline.json files under Production_Kits/, sorted, or [] if
    the dir doesn't exist. Single source of truth for both collect() and
    main()'s zero-scan guard (round-2 audit finding, 2026-07-28): a guard that
    re-derives this glob instead of calling the production symbol is correct
    today and silently wrong the moment the pattern changes -- the recurring
    defect class the 2026-07-28 gate doc names firing five times in one chain."""
    kits = vlt / KITS_SUBDIR
    return sorted(kits.glob("Video_*_pipeline.json")) if kits.is_dir() else []


def collect(vlt: Path, now: datetime) -> list[dict]:
    kits = vlt / KITS_SUBDIR
    findings: list[dict] = []
    for p in _pipeline_files(vlt):
        label = p.name.replace("_pipeline.json", "")
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            findings.append({"video": label, "stage": "-", "status": "-",
                             "kind": "unreadable",
                             "detail": f"{p.name} could not be parsed: {e}"})
            continue
        published = is_published(kits, label)
        findings.extend(lint_doc(label, doc, published, now))
    return findings


def _selftest() -> int:
    now = datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc)
    fails = 0

    def check(name, doc, published, want_kinds):
        nonlocal fails
        got = sorted({f["kind"] for f in lint_doc("Video_TEST", doc, published, now)})
        ok = got == sorted(want_kinds)
        if not ok:
            fails += 1
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {got} (want {sorted(want_kinds)})")

    # --- Class 1: published-but-open ---------------------------------------
    check("published w/ open pre-publish stage",
          {"stages": {"5_images": {"status": "needs-steve"}}}, True,
          ["published-but-open"])
    check("in-flight w/ open stage (must NOT flag)",
          {"stages": {"5_images": {"status": "needs-steve"}}}, False, [])
    check("published, all done",
          {"stages": {k: {"status": "done"} for k in PRE_PUBLISH_STAGES}}, True, [])

    # --- The two EXEMPTIONS (regression pins for the 2026-07-25 review) -----
    check("published w/ 11_analyze open (post-publish, exempt)",
          {"stages": {"11_analyze": {"status": "blocked"}}}, True, [])
    # 12_leadmagnet is NOT a dep of 10_publish — a video may legally publish with
    # the magnet parked. Flagging it emitted a false hourly ping forever.
    check("published w/ 12_leadmagnet parked (exempt)",
          {"stages": {"12_leadmagnet": {"status": "needs-steve"}}}, True, [])

    # --- Class 2: orphaned-running (pid-first) ------------------------------
    live_pid = os.getpid()
    check("running w/ THIS live pid (must NOT flag)",
          {"stages": {"11_analyze": {"status": "running", "pid": live_pid,
                                     "started_at": "2026-07-25T11:50:00Z"}}}, True, [])
    # A dead pid is an orphan REGARDLESS of age — the age-only rule missed this.
    check("running w/ dead pid, only 10 min old (age rule would MISS)",
          {"stages": {"11_analyze": {"status": "running", "pid": 999999,
                                     "started_at": "2026-07-25T11:50:00Z"}}}, True,
          ["orphaned-running"])
    # Recycled-pid: a LIVE pid whose start-token no longer matches what was
    # recorded => a different process now holds that pid; the original run is gone.
    check("running w/ live pid but MISMATCHED start token (recycled)",
          {"stages": {"11_analyze": {"status": "running", "pid": live_pid,
                                     "pid_start_token": "Thu Jan  1 00:00:00 1970",
                                     "started_at": "2026-07-25T11:50:00Z"}}}, True,
          ["orphaned-running"])
    # Age-fallback LOWER bound: no pid signal, but recent => must stay silent.
    # Pins STALE_RUNNING_HOURS against being driven to 0.
    check("running w/ no pid, only 10 min old (must NOT flag)",
          {"stages": {"11_analyze": {"status": "running",
                                     "started_at": "2026-07-25T11:50:00Z"}}}, True, []),
    check("running w/ no pid + no timestamp (unverifiable)",
          {"stages": {"11_analyze": {"status": "running"}}}, True,
          ["orphaned-running"])
    check("running w/ no pid, 5h old (age fallback fires)",
          {"stages": {"11_analyze": {"status": "running",
                                     "started_at": "2026-07-25T07:00:00Z"}}}, True,
          ["orphaned-running"])

    # --- Structural ---------------------------------------------------------
    check("flat doc shape",
          {"5_images": {"status": "blocked"}}, True, ["published-but-open"])
    # A corrupt 'stages' must be REPORTED, not silently read as clean.
    check("corrupt stages (list, not dict)",
          {"video": 17, "stages": ["5_images"]}, True, ["unreadable"])

    # --- is_published: pin the video_id-non-empty contract -------------------
    # Previously UNPINNED — `is_published -> True` and `-> .is_file()` both left
    # the suite green, so the fix that stopped V04's `video_id: null` receipt
    # reading as published could be reverted silently. That regression already
    # happened once.
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        kits = Path(td)
        (kits / "Video_90_youtube_upload.json").write_text(
            json.dumps({"video_id": "abc123XYZ"}), encoding="utf-8")
        (kits / "Video_91_youtube_upload.json").write_text(
            json.dumps({"video_id": None, "status": "deleted_from_channel"}),
            encoding="utf-8")
        (kits / "Video_92_youtube_upload.json").write_text(
            json.dumps({"video_id": "   "}), encoding="utf-8")
        for lbl, want, why in (("Video_90", True,  "real video_id"),
                               ("Video_91", False, "video_id null (V04 shape)"),
                               ("Video_92", False, "video_id whitespace-only"),
                               ("Video_93", False, "no receipt at all")):
            got = is_published(kits, lbl)
            ok = got == want
            if not ok:
                fails += 1
            print(f"  [{'PASS' if ok else 'FAIL'}] is_published {why}: "
                  f"{got} (want {want})")

    print("SELFTEST OK" if not fails else f"SELFTEST FAILED ({fails})")
    return 1 if fails else 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--selftest", action="store_true", help="hermetic self-check")
    args = ap.parse_args(argv)

    if args.selftest:
        return _selftest()

    # ZERO-SCAN GUARD (2026-07-28 two-pass audit, lane E). collect() returns [] both when
    # it scanned every pipeline and found nothing stale AND when it scanned NOTHING — a
    # missing kits dir (line 235) or a glob that matches no files. Those are opposite
    # facts that produced identical output ("CLEAN", exit 0), so one renamed vault path
    # would have reported green forever. Exit 75 = EX_TEMPFAIL, distinguishable from a
    # clean 0 and silent-on-stderr so a transient unmount doesn't page.
    # CONSUMER (corrected 2026-07-28 round-2 review — this is NOT a run_job.sh job;
    # it is never registered as its own scheduled task): routines/pre-brief.prompt
    # §18 shells out to this script directly and reads the exit code itself. Its
    # decision table now has an explicit SKIP/75 row (was binary CLEAN/FLAG only,
    # which read a 75-with-empty-stdout as "note nothing" — the exact silent-green
    # this guard exists to kill). Uses _pipeline_files() (the same symbol
    # collect() calls below), not its own re-derived glob (round-2 audit
    # finding, 2026-07-28) -- a re-derived guard is a check that only knows
    # what the real scan scanned by COINCIDENCE, not by construction.
    _kits = vault() / KITS_SUBDIR
    _pipelines = _pipeline_files(vault())
    if not _pipelines:
        _why = ("kits dir missing" if not _kits.is_dir()
                else "kits dir present but no Video_*_pipeline.json matched")
        if args.json:
            print(json.dumps({"status": "skip", "reason": _why,
                              "kits_dir": str(_kits), "count": 0, "findings": []},
                             indent=2))
        else:
            print(f"pipeline_stale_state_lint: SKIP — scan target unavailable "
                  f"({_why}: {_kits}). Nothing scanned; this is NOT a clean result.",
                  file=sys.stderr)
        return 75

    findings = collect(vault(), datetime.now(timezone.utc))

    if args.json:
        print(json.dumps({"status": "flag" if findings else "ok",
                          "count": len(findings), "findings": findings}, indent=2))
        return 1 if findings else 0

    if not findings:
        # Scanned count in the line a human reads — a bare "CLEAN" can't be told apart
        # from a shrinking scan set. Modelled on triad_sync_check.py's summary_line.
        print(f"pipeline_stale_state_lint: CLEAN — no stale pipeline state "
              f"[scanned {len(_pipelines)} pipeline file(s)].")
        return 0

    print(f"pipeline_stale_state_lint: 🔴 FLAG — {len(findings)} stale stage(s)")
    for f in findings:
        print(f"  [{f['kind']}] {f['video']} {f['stage']} = {f['status']}")
        print(f"      {f['detail']}")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
