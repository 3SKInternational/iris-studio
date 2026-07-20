#!/usr/bin/env python3
"""pipeline_stage_truth_lint.py — pipeline-stage-status vs review-reality linter.

Read-only. The pipeline-state leg of the house ground-truthing family, next to
verdict_resolution_lint.py (are superseded verdicts stamped?),
receipt_surface_id_lint.py / triad_sync_check.py / youtube_reality_check.py
(launch-spine ids) and manifest_spine_lint.py (pre-spend figure trace). This one
closes a distinct gap: does a video's Production_Kits/Video_NN_pipeline.json
stage STATUS agree with the review REALITY already recorded on disk.

The defect it catches (the recurring "asserted-closed-vs-actually-open" /
phantom-work class — 2026-07-11 incident, re-seen 07-12/07-14/07-17/07-19):
a producer stage (7_packaging, 9_description) runs through the orchestrator's
produce-then-review gate, which RE-DISPATCHES the producer + RE-RUNS the reviewer
every sweep and only advances on a fresh SHIP. When a human has already taken the
artifact to a clean SHIP and stamped the review file `resolution: closed-*`, the
gate cannot see that stamp — it re-produces, times out or re-REVISEs, and parks
the stage at needs-steve. So the stage sits `needs-steve` FOREVER while its review
on disk says CLOSED-SHIP. That exact state stranded V13's packaging + description
hours before its publish slot, and is the root of every phantom-work re-flag.

Signal (per in-flight video × stage):
  🔴 STALE-OPEN — a stage whose status is NOT `done` while its mapped review file
                  carries a `resolution:` stamp (a deliberate human "this gate is
                  CLOSED"). The stage's work is done; the json is stale. Drives
                  exit 1 + Telegram. The reconcile action: verify + set the stage
                  `status: done` in the pipeline json.

Scoping — FLAGs are raised only for IN-FLIGHT videos (no live upload receipt).
A published video's Production_Kits/Video_NN_youtube_upload.json records a live
`video_id`; its pipeline json is then historical (the video shipped, the stale
early-stage statuses are moot), so it is skipped — the phantom-work harm only
exists while a video is mid-pipeline and its status is being read as live truth.

Why the resolution STAMP, not artifact freshness: an mtime "artifact older than
its deps → stale" test false-positives here (V13's 07-19 VO-line-only script edit
post-dates the packaging artifact without invalidating it). The `resolution:`
stamp is an explicit, deliberate human close that SUPERSEDES any mtime ordering —
keying on it gives a zero-false-positive signal. (A review carrying only
`resolution-prior-revise:` — an intermediate note, not a close — is correctly NOT
matched: the regex anchors `^resolution:` exactly.)

Usage:
  pipeline_stage_truth_lint.py               # scan, quiet on clean
  pipeline_stage_truth_lint.py --verbose     # print the full report to stdout
  pipeline_stage_truth_lint.py --report-only # always exit 0 (gentle pre-brief pass)
  pipeline_stage_truth_lint.py --quiet-ok    # suppress the clean-run stdout line
  pipeline_stage_truth_lint.py --selftest    # run the review-gate fixtures
Exit: 0 on CLEAN, 1 on any STALE-OPEN flag (or a selftest failure).
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone, timedelta

# --- paths -------------------------------------------------------------------
VAULT = "/Users/steve/Documents/3SK/outputs"
FIN = os.path.join(VAULT, "BRANDS", "3SK_Finance")
PIPELINE_GLOB = os.path.join(FIN, "Production_Kits", "Video_*_pipeline.json")
RECEIPT_TMPL = os.path.join(FIN, "Production_Kits", "Video_{nn}_youtube_upload.json")
REPORT = os.path.join(FIN, "Raw_Assets", "_pipeline_stage_truth_report.md")
NOTIFY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "notify.sh")

# Stage -> review-file glob (relative to FIN). "{V}" -> zero-padded "Video_NN".
# Only stages with a reviewer that stamps `resolution:` on close are mapped; an
# unmapped stage (or a glob that matches nothing) simply never flags — a wrong
# glob is a false-NEGATIVE (safe), never a false page. Extend as new
# resolution-stamping review gates are added.
STAGE_REVIEW_GLOB = {
    "1_script":      "Scripts/_REVIEW_PREP/{V}_Script_Review*.md",
    "3_vo_expand":   "Scripts/_VO_Review_Prep/{V}_VO_Review*.md",
    "7_packaging":   "Packaging/_REVIEW/*{V}*.md",
    "9_description": "Video_Descriptions/_REVIEW/{V}_Description_Review*.md",
}

# A deliberate human "this gate is closed" marker in review frontmatter. Anchored
# so `resolution-prior-revise:` (an intermediate note) is NOT matched.
RESOLUTION_RE = re.compile(r"^resolution:\s*\S", re.MULTILINE)


def nn(video):
    return "%02d" % int(video)


def video_num(pipeline_path):
    m = re.search(r"Video_(\d+)_pipeline\.json$", os.path.basename(pipeline_path))
    return int(m.group(1)) if m else None


def is_published(video):
    """A live upload receipt (non-null video_id) => the video shipped; its
    pipeline json is historical and out of scope for phantom-work flags."""
    p = RECEIPT_TMPL.format(nn=nn(video))
    try:
        with open(p, encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return False
    vid = d.get("video_id")
    return isinstance(vid, str) and bool(vid.strip())


def resolution_stamp(review_glob, video):
    """Return the `resolution:` value from the first matching review file that
    carries one, else None. Reads only the frontmatter head (cheap, and the stamp
    always lives in frontmatter)."""
    pattern = os.path.join(FIN, review_glob.format(V="Video_" + nn(video)))
    for path in sorted(glob.glob(pattern)):
        try:
            with open(path, encoding="utf-8") as fh:
                head = fh.read(4000)
        except OSError:
            continue
        m = RESOLUTION_RE.search(head)
        if m:
            # Return (rel-path, the resolution line's value) for the report.
            line = head[m.start():].splitlines()[0]
            val = line.split(":", 1)[1].strip().strip('"').strip()
            return os.path.relpath(path, VAULT), (val[:80] or "(stamped)")
    return None


def scan_pipeline(pipeline_path):
    """Return list of (video, stage, status, review_rel, resolution) stale-open
    findings for one in-flight pipeline file. Empty for published / clean."""
    video = video_num(pipeline_path)
    if video is None:
        return []
    if is_published(video):
        return []  # historical json — out of scope
    try:
        with open(pipeline_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []  # unreadable pipeline is skipped, not fatal
    stages = data.get("stages", {})
    findings = []
    for stage, glob_tmpl in STAGE_REVIEW_GLOB.items():
        s = stages.get(stage)
        if not isinstance(s, dict):
            continue
        if s.get("status") == "done":
            continue  # correct state — review closed AND stage advanced
        stamp = resolution_stamp(glob_tmpl, video)
        if stamp:
            review_rel, val = stamp
            findings.append((video, stage, s.get("status"), review_rel, val))
    return findings


def run_check(pipeline_paths):
    findings, n_inflight, n_published = [], 0, 0
    for p in sorted(pipeline_paths):
        video = video_num(p)
        if video is not None and is_published(video):
            n_published += 1
            continue
        n_inflight += 1
        findings.extend(scan_pipeline(p))
    return findings, n_inflight, n_published


def _now_et():
    # ET is UTC-4 (EDT) in the summer launch window; report stamp only.
    return (datetime.now(timezone.utc) - timedelta(hours=4)).strftime("%Y-%m-%d %H:%M ET")


def write_report(findings, n_inflight, n_published):
    verdict = "STALE-OPEN" if findings else "CLEAN"
    lines = [
        "# Pipeline stage-status vs review-reality report",
        "",
        "_Generated %s by `scripts/pipeline_stage_truth_lint.py` (read-only)._" % _now_et(),
        "",
        "**Verdict: %s** — %d in-flight pipeline(s) scanned, %d published skipped; "
        "%d stale-open stage(s)." % (verdict, n_inflight, n_published, len(findings)),
        "",
        "| video | stage | json status | resolution-stamped review | resolution |",
        "| --- | --- | --- | --- | --- |",
    ]
    if findings:
        for video, stage, status, review_rel, val in sorted(findings):
            val = val.replace("|", "\\|")
            lines.append("| Video_%s | %s | 🔴 %s | `%s` | %s |"
                         % (nn(video), stage, status, review_rel, val))
        lines += ["",
                  "**Reconcile:** for each row, confirm the review is genuinely a "
                  "closed SHIP, then set that stage `status: done` in "
                  "`Production_Kits/Video_NN_pipeline.json` (back it up first). The "
                  "orchestrator's produce-then-review gate re-runs the producer every "
                  "sweep and cannot see a human `resolution:` stamp, so it parks the "
                  "stage — this list is the manual bridge until that gate honors the stamp."]
    else:
        lines.append("| — | — | ✅ | — | every in-flight stage agrees with its review |")
    lines.append("")
    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    with open(REPORT, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return verdict


def summary_line(verdict, findings):
    if verdict == "STALE-OPEN":
        v, stage, status, _, _ = findings[0]
        extra = (" (+%d more)" % (len(findings) - 1)) if len(findings) > 1 else ""
        return ("pipeline_stage_truth_lint: 🔴 STALE-OPEN — Video_%s %s is '%s' but its "
                "review is resolution-stamped%s" % (nn(v), stage, status, extra))
    return "pipeline_stage_truth_lint: CLEAN"


def notify(msg):
    if not os.access(NOTIFY, os.X_OK):
        return
    try:
        subprocess.run([NOTIFY, msg], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=20)
    except Exception:
        pass  # best-effort; never block the check on a notify failure


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Pipeline stage-status vs review-reality linter (read-only).")
    ap.add_argument("--verbose", action="store_true", help="print full report to stdout")
    ap.add_argument("--report-only", action="store_true", help="always exit 0")
    ap.add_argument("--quiet-ok", action="store_true", help="suppress the clean-run stdout line")
    ap.add_argument("--selftest", action="store_true", help="run review-gate fixtures")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    findings, n_inflight, n_published = run_check(glob.glob(PIPELINE_GLOB))
    verdict = write_report(findings, n_inflight, n_published)
    line = summary_line(verdict, findings)

    if verdict == "STALE-OPEN":
        print(line)
        notify(line + " — see BRANDS/3SK_Finance/Raw_Assets/_pipeline_stage_truth_report.md")
    elif not args.quiet_ok:
        print(line)
    if args.verbose:
        with open(REPORT, encoding="utf-8") as fh:
            print(fh.read())

    if args.report_only:
        return 0
    return 1 if findings else 0


# --- selftest fixtures -------------------------------------------------------
# The selftest builds a throwaway vault tree and points the module's paths at it,
# so it exercises the real run_check end-to-end (pipeline json + receipt +
# review-file glob + resolution-stamp regex + published-scoping) hermetically.
def selftest():
    import tempfile
    global FIN, PIPELINE_GLOB, RECEIPT_TMPL, REPORT
    fails = []
    root = tempfile.mkdtemp(prefix="pstl_")
    FIN_bak, PG_bak, RT_bak, RP_bak = FIN, PIPELINE_GLOB, RECEIPT_TMPL, REPORT
    FIN = root
    PIPELINE_GLOB = os.path.join(root, "Production_Kits", "Video_*_pipeline.json")
    RECEIPT_TMPL = os.path.join(root, "Production_Kits", "Video_{nn}_youtube_upload.json")
    REPORT = os.path.join(root, "Raw_Assets", "_pipeline_stage_truth_report.md")

    def w(rel, text):
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)

    def pipeline(nn_, **stage_status):
        stages = {k: {"status": v} for k, v in stage_status.items()}
        w("Production_Kits/Video_%s_pipeline.json" % nn_,
          json.dumps({"video": int(nn_), "stages": stages}))

    def review(rel, resolution=None):
        fm = "---\ntitle: r\n"
        if resolution is not None:
            fm += "resolution: %s\n" % resolution
        fm += "---\nVERDICT: SHIP\n"
        w(rel, fm)

    def receipt(nn_, video_id):
        w("Production_Kits/Video_%s_youtube_upload.json" % nn_,
          json.dumps({"video": "Video_%s" % nn_, "video_id": video_id}))

    def count(name, exp):
        f, _, _ = run_check(glob.glob(PIPELINE_GLOB))
        if len(f) != exp:
            fails.append("%s: expected %d finding(s), got %d %r" % (name, exp, len(f), f))

    try:
        # 1. THE incident: packaging + description parked needs-steve, both reviews
        #    resolution-stamped, video in-flight (no receipt) -> 2 STALE-OPEN.
        pipeline("13", **{"7_packaging": "needs-steve", "9_description": "needs-steve",
                          "1_script": "done", "3_vo_expand": "done"})
        review("Packaging/_REVIEW/Packaging_Video_13_Review.md", "closed-fix-applied-2026-07-18")
        review("Video_Descriptions/_REVIEW/Video_13_Description_Review.md", "closed-fix-applied-2026-07-16")
        count("1 incident (pkg+desc stale-open)", 2)

        # 2. same stages, now correctly `done` -> 0 (the reconciled state).
        pipeline("13", **{"7_packaging": "done", "9_description": "done"})
        count("2 reconciled-done", 0)

        # 3. stage parked but review has NO resolution stamp (genuinely open REVISE)
        #    -> 0 (must not flag an in-progress gate).
        pipeline("13", **{"7_packaging": "needs-steve"})
        os.remove(os.path.join(root, "Video_Descriptions/_REVIEW/Video_13_Description_Review.md"))
        w("Packaging/_REVIEW/Packaging_Video_13_Review.md", "---\ntitle: r\n---\nVERDICT: REVISE\n")
        count("3 open-no-stamp", 0)

        # 4. published-scoping: same stale-open state but a LIVE receipt exists
        #    -> 0 (historical json, out of scope).
        review("Packaging/_REVIEW/Packaging_Video_13_Review.md", "closed-fix-applied-2026-07-18")
        receipt("13", "abcDEF12345")
        count("4 published-skipped", 0)
        os.remove(os.path.join(root, "Production_Kits/Video_13_youtube_upload.json"))

        # 5. `resolution-prior-revise:` alone (intermediate note, not a close) must
        #    NOT match -> 0.
        pipeline("13", **{"9_description": "needs-steve"})
        os.remove(os.path.join(root, "Packaging/_REVIEW/Packaging_Video_13_Review.md"))
        w("Video_Descriptions/_REVIEW/Video_13_Description_Review.md",
          "---\ntitle: r\nresolution-prior-revise: some earlier note\n---\nVERDICT: REVISE\n")
        count("5 prior-revise-only", 0)

        # 6. a `done` stage WITH a stamped review is the correct state -> 0.
        pipeline("13", **{"9_description": "done"})
        w("Video_Descriptions/_REVIEW/Video_13_Description_Review.md",
          "---\nresolution: closed\n---\nSHIP\n")
        count("6 done-and-stamped", 0)
    finally:
        FIN, PIPELINE_GLOB, RECEIPT_TMPL, REPORT = FIN_bak, PG_bak, RT_bak, RP_bak
        import shutil
        shutil.rmtree(root, ignore_errors=True)

    if fails:
        print("pipeline_stage_truth_lint --selftest: FAIL")
        for f in fails:
            print("  ✗ " + f)
        return 1
    print("pipeline_stage_truth_lint --selftest: PASS (6/6 fixtures)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
