#!/usr/bin/env python3
"""verdict_resolution_lint.py — flag superseded-candidate review verdicts that
carry no resolution stamp (from Pattern_reviewer_verdicts_need_resolution_stamps).

Read-only. The review-gate leg of ground-truthing, next to the launch-spine
linters (receipt_surface_id_lint / triad_sync_check / youtube_reality_check) and
manifest_spine_lint. This one closes a different drift: a REVISE/HOLD verdict
file is a fact about a MOMENT, but nothing on the file changes when the verdict
is superseded (a fix applied, Steve ships-as-is, or the video publishes). So a
downstream scanner (the quality dept brief -> CEO brief -> morning brief) reads
the file, not the bridge/daily where the superseding event was recorded, and
escalates a closed verdict as an open gate forever.

The incident it prevents (observed 2026-07-11): three superseded V11 REVISE
verdicts + a V12 image REVISE + a V5 VO REVISE were cited by the 04:25 org-briefs
quality dept as "gates blocking tomorrow's publish"; the 08:00 morning briefing
escalated them; Steve's mid-trip briefing ordered a day of phantom fix work on
decisions he'd already made. Root fix = the resolution-stamp convention (rule #1)
+ scanners treating REVISE-on-a-receipted-video as suspect (rule #2). This linter
is rule #2 mechanized: flag-don't-auto-stamp.

The rule (deterministic):
  For each per-video reviewer verdict file, FLAG when ALL hold:
    - its overall verdict is OPEN (REVISE / HOLD / HOLD-SPEND / DEFECTS /
      "SHIP WITH FIXES" — anything that is not a clean bare SHIP), AND
    - the video HAS an upload receipt (Production_Kits/Video_NN_youtube_upload.json)
      => the reviewed render already shipped, so the verdict is a
      supersession CANDIDATE, not live pending work, AND
    - the verdict file frontmatter carries NO `resolution:` key (rule #1's
      canonical closure field).
  A FLAG means: a human/agent must verify the superseding event and either stamp
  the file (`resolution:`) or, if it is genuinely still open on a re-bake, leave
  it. The linter NEVER stamps or closes anything (flag-don't-auto-stamp).

Guard against eating genuinely-open gates (the pattern's explicit caveat):
  - A REVISE/HOLD-SPEND on a video WITHOUT a receipt = the artifact has not
    shipped, so it is REAL pending work -> never flagged.
  - A clean SHIP, or a file already carrying `resolution:` -> never flagged.
  - A non-per-video review (no Video_NN in frontmatter or filename), a MOC/index
    file, an unparseable verdict -> skipped.

Usage:
  verdict_resolution_lint.py               # scan, quiet on clean
  verdict_resolution_lint.py --verbose     # print full report to stdout
  verdict_resolution_lint.py --report-only # always exit 0 (gentle pre-brief pass)
  verdict_resolution_lint.py --quiet-ok    # suppress the clean-run stdout line
  verdict_resolution_lint.py --selftest    # run the review-gate fixtures
Exit: 0 CLEAN, 1 on any FLAG (or a selftest failure), 2 on usage error.
"""
import argparse
import glob
import os
import re
import subprocess
import sys
from datetime import datetime, timezone, timedelta

VAULT = "/Users/steve/Documents/3SK/outputs"
FIN = os.path.join(VAULT, "BRANDS", "3SK_Finance")
# Every dir where a reviewer gate writes a per-video verdict file.
REVIEW_DIRS = [
    "Scripts/_REVIEW_PREP",
    "Scripts/_VO_Review_Prep",
    "Video_Descriptions/_REVIEW",
    "Video_Descriptions/reviews",
    "Packaging/_REVIEW",
    "Lead_Magnets/_REVIEW",
    "Raw_Assets/Image_Factory/_REVIEW",
    "Channel_Intelligence/Analytics/_REVIEW",
]
RECEIPT_GLOB = os.path.join(FIN, "Production_Kits", "*_youtube_upload.json")
REPORT = os.path.join(FIN, "Raw_Assets", "_verdict_resolution_report.md")
NOTIFY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "notify.sh")

# Overall verdict line: COLUMN 0, optional markdown heading/bold, then VERDICT:.
# Column-0 anchored to match the frontmatter read — an INDENTED `verdict: X` is a
# nested YAML history entry (`prior_round_verdicts:`), never a file's own verdict.
# Under any-clean-wins an indented history `verdict: SHIP` in the body would EAT a
# live open verdict, a worse failure direction than the false flag it used to
# cause. Verified free: all 157 corpus verdict lines start at column 0.
_VERDICT_RE = re.compile(r'^(?:#+\s*|\*\*\s*)?VERDICT\s*:\s*(.+)$', re.IGNORECASE)
_VIDEO_RE = re.compile(r'Video[_\- ]?(\d{1,3})', re.IGNORECASE)
# Non-verdict files that live in the review dirs but are not per-video gates.
_SKIP_NAME_RE = re.compile(r'(_MOC|Policy_and_AgentDef|__)', re.IGNORECASE)


def _norm_video(raw):
    """A frontmatter `video:` value -> 'Video_04'. Accepts 'Video_11' / 'video 4'
    / bare '04'. None if no number. Used ONLY on the controlled frontmatter value,
    never on filenames (a filename carries dates + tempfile noise)."""
    if raw is None:
        return None
    m = _VIDEO_RE.search(raw) or re.search(r'(\d{1,3})', raw)
    if not m:
        return None
    return "Video_%02d" % int(m.group(1))


def _video_from_name(name):
    """Derive Video_NN from a filename, requiring the literal 'Video' token so a
    date or random suffix can never mint a bogus video number."""
    m = _VIDEO_RE.search(name)
    return "Video_%02d" % int(m.group(1)) if m else None


def _frontmatter(text):
    """Return (raw frontmatter block, body start offset).

    A leading '---' that is a horizontal RULE rather than a frontmatter fence
    would otherwise swallow real content: the block must contain at least one
    column-0 `key:` line to count. Returns ('', 0) when there is no
    frontmatter, so the caller scans the whole file."""
    if not text.startswith("---"):
        return "", 0
    end = text.find("\n---", 3)
    if end == -1:
        return "", 0
    fm = text[3:end]
    if not re.search(r'^[A-Za-z_][\w-]*\s*:', fm, re.MULTILINE):
        return "", 0           # '---' hr, not a fence
    nl = text.find("\n", end + 1)          # past the closing '---' line
    return fm, (nl + 1 if nl != -1 else len(text))


def classify_verdict(remainder):
    """Classify the LEADING verdict token after 'VERDICT:'. Reading the leading
    token (not substring-anywhere) means a clean 'SHIP (was REVISE)' verdict does
    NOT read as open. clean SHIP -> 'clean'; REVISE/HOLD*/DEFECTS/SHIP-WITH-FIXES
    -> 'open'; anything unparseable -> 'unknown' (skipped, never flagged)."""
    up = remainder.upper()
    if "SUPERSEDED" in up:          # inline "REVISE _(superseded ...)_" -> already closed
        return "clean"
    m = re.match(r'\s*\**\s*(SHIP[\s-]WITH[\s-]FIXES|SHIP|REVISE|HOLD-SPEND|HOLD|'
                 r'DEFECTS FOUND|DEFECTS|DEFECT)\b', up)
    if not m:
        return "unknown"
    return "clean" if m.group(1) == "SHIP" else "open"


def _series_index(name):
    """Order files WITHIN a (video, gate) series by the index parsed from the
    filename, NOT by mtime (this vault is bulk-copied / Drive-synced / rsync-
    mirrored, so mtimes do not track authoring order). A higher (v, pass/round)
    tuple is later by construction. No token -> (0, 0) = the base pass."""
    vnum = max((int(x) for x in re.findall(r'(?:^|[_-])v(\d+)', name, re.I)), default=0)
    pnum = max((int(x) for x in re.findall(r'(?:pass|round)\s*_?(\d+)', name, re.I)), default=0)
    return (vnum, pnum)


def classify_path(path):
    """Read one verdict file -> (video, index, kind, line_no, snippet).
    kind in {'closed', 'open', 'clean', 'none'}:
      closed = carries a resolution: stamp (rule #1) -> a closure at this index
      clean  = a clean SHIP verdict          -> a closure at this index
      open   = an unstamped REVISE/HOLD verdict
      none   = non-per-video / no verdict line / unreadable  (caller skips)
    line_no/snippet are set only for 'open'."""
    name = os.path.basename(path)
    if _SKIP_NAME_RE.search(name):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except (OSError, UnicodeDecodeError):
        return None
    fm, body_start = _frontmatter(text)
    resolved = bool(re.search(r'^\s*resolution\s*:', fm, re.MULTILINE))
    mv = re.search(r'^\s*video\s*:\s*(.+)$', fm, re.MULTILINE)
    fm_video = mv.group(1).strip().strip('"').strip("'") if mv else None
    video = _norm_video(fm_video) or _video_from_name(name)
    if not video:
        return None
    idx = _series_index(name)
    if resolved:
        return (video, idx, "closed", 0, "")
    # Collect EVERY verdict signal in the file, then: any clean one concludes it.
    #
    # Signals are (1) the column-0 frontmatter `verdict:` key and (2) each body
    # `VERDICT:` line. Two false-positive classes this closes (both live
    # 2026-07-26, both flagged a SHIP'd file as an open gate):
    #  (a) frontmatter history — `prior_round_verdicts:` lists superseded rounds
    #      as INDENTED `verdict: REVISE` entries. Column-0 anchoring skips those
    #      while still reading a real fm verdict (V13: flagged at its round-1
    #      entry though its body verdict was SHIP).
    #  (b) retained-history sections — "# Prior pass retained — PASS 3, VERDICT:
    #      REVISE" appended BELOW the current verdict, whose own text says
    #      "-> RESOLVED pass 4" (V09 image review).
    #
    # WHY any-clean-wins rather than first-verdict-wins: the gates use TWO live
    # orderings and position does NOT encode recency. Newest-at-top: the image
    # gates. Newest-at-BOTTOM (current verdict appended last): the VO-review,
    # lead-magnet, packaging, and description-search-rewrite gates — e.g.
    # `Video_09_VO_Review.md` opens REVISE (pass 1) and closes SHIP (pass 2).
    # A positional rule reads the stale verdict in whichever convention it
    # guesses wrong, so it cannot be the discriminator.
    #
    # ponytail: this makes within-file behavior identical to the series contract
    # run_check already documents ("any SHIP concludes the series"), instead of
    # a second, conflicting rule. Same accepted ceiling, now in one place: a
    # genuine LATER re-bake REVISE after an earlier SHIP in the same file is not
    # flagged. On an already-shipped video that is future-re-bake work, not a
    # publish-blocking gate — and this linter exists to stop closed verdicts
    # being resurrected as open gates, so it errs toward silence, not phantoms.
    # Fixtures 11d/11e pin both orderings; 11c pins this ceiling deliberately.
    signals = []
    mfv = re.search(r'^verdict\s*:\s*(.+)$', fm, re.MULTILINE)   # column-0 ONLY
    if mfv:
        # Cite the real line/text: the frontmatter gate (lead-magnet) writes
        # fm-only verdicts, so an fm-only OPEN one is a reachable finding and a
        # report row with line 0 and a blank snippet is uncitable.
        # +1, not +2: _frontmatter returns text[3:end], and text[3] is the
        # NEWLINE terminating the opening '---'. That leading \n already pays
        # for the fence line, so adding another double-counts (verified against
        # Video_02_Packaging_Review.md, whose fm verdict is line 7 — a +2 cited
        # line 8, i.e. its `tags:` line, with the correct snippet beside it).
        signals.append((fm[:mfv.start()].count("\n") + 1,
                        mfv.group(0).strip()[:120],
                        classify_verdict(mfv.group(1))))
    for i, line in enumerate(text[body_start:].splitlines(),
                             text.count("\n", 0, body_start) + 1):
        m = _VERDICT_RE.match(line)
        if m:
            signals.append((i, line.strip()[:120], classify_verdict(m.group(1))))
    if any(s == "clean" for _, _, s in signals):
        return (video, idx, "clean", 0, "")
    for i, snippet, s in signals:
        if s == "open":
            return (video, idx, "open", i, snippet)
    return (video, idx, "none", 0, "")


def run_check(review_files, receipts):
    """review_files: list of paths. receipts: list of receipt paths.
    Returns (findings, n_receipted, n_flags). A finding is
    (relpath, video, line_no, snippet).

    Grouping: a (video, gate-dir) SERIES is CONCLUDED if any file in it records a
    clean SHIP or carries a resolution: stamp — the video shipped, so a gate that
    ever passed is historically satisfied for that render and its earlier REVISE
    passes are just the pre-SHIP iterations. We flag only a series that is
    ALL-OPEN / never-SHIP'd / unstamped, on a RECEIPTED (shipped) video: that is
    a render that shipped ship-as-is while its gate only ever recorded REVISE/HOLD
    — the exact thing a scanner will resurrect until it is stamped. An unreceipted
    video is never flagged (its render has not shipped, so its REVISE is
    genuinely-open pending work).

    ponytail: 'any SHIP concludes the series' rather than 'latest pass wins'. The
    files use inconsistent numbering (_v3 vs _Pass5 vs _Round3 vs _v2_Pass3) that
    cannot be reliably totally-ordered, and mtime is meaningless in this
    bulk-copied / Drive-synced vault. The ceiling: a genuine LATER re-bake REVISE
    after an earlier SHIP is not flagged — acceptable, because on an
    already-shipped video that is future-re-bake work, not a publish-blocking gate
    (the incident class). Tighten to real ordering only if a later-REVISE case
    ever slips through."""
    receipted = set()
    for r in receipts:
        v = _norm_video(os.path.basename(r))
        if v:
            receipted.add(v)

    # Bucket every file by (video, gate-dir) with its parsed series index + kind.
    groups = {}   # (video, dir) -> list of (index, kind, path, line, snip)
    for path in review_files:
        info = classify_path(path)
        if info is None:
            continue
        video, idx, kind, line, snip = info
        groups.setdefault((video, os.path.dirname(path)), []).append(
            (idx, kind, path, line, snip))

    findings = []
    for (video, _d), items in groups.items():
        if video not in receipted:
            continue                       # unshipped -> whole series genuinely open
        if any(kind in ("clean", "closed") for _idx, kind, *_ in items):
            continue                       # gate ever SHIP'd or was stamped -> concluded
        opens = [(idx, path, line, snip) for idx, kind, path, line, snip in items
                 if kind == "open"]
        if not opens:
            continue
        opens.sort(reverse=True)           # cite the highest-index open as representative
        _idx, path, line, snip = opens[0]
        findings.append((os.path.relpath(path, FIN), video, line, snip))
    findings.sort(key=lambda f: (f[1], f[0]))
    return findings, len(receipted), len(findings)


def _collect_review_files():
    files = []
    for d in REVIEW_DIRS:
        files.extend(glob.glob(os.path.join(FIN, d, "*.md")))
    return files


def _now_et():
    # ET is UTC-4 (EDT) in the summer window; report stamp only.
    return (datetime.now(timezone.utc) - timedelta(hours=4)).strftime("%Y-%m-%d %H:%M ET")


def write_report(findings, n_receipted, n_flags):
    verdict = "FLAG" if n_flags else "CLEAN"
    lines = [
        "# Verdict-resolution report",
        "",
        "_Generated %s by `scripts/verdict_resolution_lint.py` (read-only)._" % _now_et(),
        "",
        "**Verdict: %s** — %d receipted videos; %d unstamped open verdict(s) on a "
        "shipped render." % (verdict, n_receipted, n_flags),
        "",
        "Each FLAG = a REVISE/HOLD verdict on a video that already has an upload "
        "receipt but carries no `resolution:` stamp. Verify the superseding event "
        "(receipt / bridge / daily) and stamp the file, or confirm it is genuinely "
        "open on a re-bake. This linter never stamps or closes anything.",
        "",
        "| verdict file | video | line | open verdict |",
        "| --- | --- | --- | --- |",
    ]
    if findings:
        for rel, video, line_no, snip in findings:
            snip = snip.replace("|", "\\|")
            lines.append("| %s | %s | %d | %s |" % (rel, video, line_no, snip))
    else:
        lines.append("| — | — | — | ✅ every open verdict is either unshipped or stamped |")
    lines.append("")
    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    with open(REPORT, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return verdict


def summary_line(verdict, n_flags, findings):
    if verdict == "FLAG":
        f = findings[0]
        extra = " (+%d more)" % (n_flags - 1) if n_flags > 1 else ""
        return ("verdict_resolution_lint: 🔴 FLAG — %s (%s) unstamped open verdict%s"
                % (f[0], f[1], extra))
    return "verdict_resolution_lint: CLEAN (0 unstamped open verdicts on shipped renders)"


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
        description="Flag unstamped superseded-candidate review verdicts (read-only).")
    ap.add_argument("--verbose", action="store_true", help="print full report to stdout")
    ap.add_argument("--report-only", action="store_true", help="always exit 0")
    ap.add_argument("--quiet-ok", action="store_true", help="suppress the clean-run stdout line")
    ap.add_argument("--selftest", action="store_true", help="run review-gate fixtures")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    receipts = glob.glob(RECEIPT_GLOB)
    findings, n_receipted, n_flags = run_check(_collect_review_files(), receipts)
    verdict = write_report(findings, n_receipted, n_flags)
    line = summary_line(verdict, n_flags, findings)

    if verdict == "FLAG":
        print(line)
        notify(line + " — see BRANDS/3SK_Finance/Raw_Assets/_verdict_resolution_report.md")
    elif not args.quiet_ok:
        print(line)
    if args.verbose:
        with open(REPORT, encoding="utf-8") as fh:
            print(fh.read())

    if args.report_only:
        return 0
    return 1 if n_flags else 0


# --- selftest fixtures -------------------------------------------------------
# Fixture filenames must be DETERMINISTIC. mkstemp draws its random component
# from [a-z0-9_]; the prefixes here end in '_', so a draw starting with '_' (or
# containing '__') matched _SKIP_NAME_RE, classify_path returned None, and the
# fixture's file was silently skipped -> 0 flags -> a bogus FAIL on whichever
# fixture drew badly. Measured ~2.6%/file x ~19 files = ~50% of runs failed on a
# random innocent fixture, in BOTH this version and the prior one. A flaky
# verification instrument reports noise, and under retry_runner.sh it would page
# Steve every 30 min about a phantom. One temp DIR, counter-suffixed names.
_TMP_DIR = None
_TMP_N = 0


def _tmp(text, suffix=".md", prefix="Video_09_"):
    global _TMP_DIR, _TMP_N
    import tempfile
    if _TMP_DIR is None:
        _TMP_DIR = tempfile.mkdtemp(prefix="vrl_selftest_")
    _TMP_N += 1
    path = os.path.join(_TMP_DIR, "%s%03d%s" % (prefix, _TMP_N, suffix))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def selftest():
    fails = []
    ran = [0]
    receipts = ["Video_04_youtube_upload.json", "Video_11_youtube_upload.json"]  # basenames suffice

    def check(name, filetext, prefix, exp_flags, exp_line=None):
        """exp_line: assert the FINDING'S CITED LINE, not just the flag count.
        Until this existed the harness compared counts only, so a wrong line
        number sailed through a green 24-fixture suite — a report that cites the
        wrong line is worse than one that cites none, because it reads as
        authoritative and contradicts its own snippet."""
        ran[0] += 1
        p = _tmp(filetext, prefix=prefix)
        try:
            f, _n, nf = run_check([p], receipts)
        finally:
            os.unlink(p)
        if nf != exp_flags:
            fails.append("%s: expected %d flag, got %d" % (name, exp_flags, nf))
        elif exp_line is not None:
            got = f[0][2] if f else None
            if got != exp_line:
                fails.append("%s: expected finding at line %s, got %s"
                             % (name, exp_line, got))

    FM = "---\ndate: 2026-07-06\ntype: vo-review\nstatus: ok\nvideo: %s\n%s---\n"

    # 1. open REVISE, receipted, NO resolution -> 1 FLAG (the incident class)
    check("1 open-receipted-unstamped",
          FM % ("Video_11", "") + "\nVERDICT: REVISE\n", "Video_11_", 1)

    # 2. same, but resolution: stamped -> 0 (rule #1 closure)
    check("2 stamped",
          FM % ("Video_11", "resolution: closed-overtaken-2026-07-12\n")
          + "\nVERDICT: REVISE _(superseded — see RESOLUTION above)_\n", "Video_11_", 0)

    # 3. open REVISE but video has NO receipt -> 0 (genuinely open, don't eat it)
    check("3 open-unreceipted",
          FM % ("Video_13", "") + "\nVERDICT: HOLD-SPEND\n", "Video_13_", 0)

    # 4. clean SHIP, receipted, unstamped -> 0 (nothing to close)
    check("4 clean-ship",
          FM % ("Video_11", "") + "\n**VERDICT: SHIP** — 49/49 PASS.\n", "Video_11_", 0)

    # 5. HOLD via markdown-heading verdict line, receipted, unstamped -> 1 FLAG
    check("5 heading-hold",
          FM % ("Video_04", "") + "\n## VERDICT: HOLD — do not assemble yet\n", "Video_04_", 1)

    # 6. video from FILENAME only (no frontmatter video:), receipted, open -> 1 FLAG
    check("6 filename-video",
          "---\ntype: image-review\nstatus: ok\n---\n**VERDICT: REVISE** — 2 shots\n",
          "Video_04_Image_Review", 1)

    # 7. inline superseded note (no frontmatter key) -> classified clean -> 0
    check("7 inline-superseded",
          FM % ("Video_11", "") + "\nVERDICT: REVISE _(superseded — fix applied)_\n",
          "Video_11_", 0)

    # 8. SHIP WITH FIXES on a receipted video, unstamped -> 1 FLAG (retired half-state)
    check("8 ship-with-fixes",
          FM % ("Video_04", "") + "\n**VERDICT: SHIP WITH FIXES**\n", "Video_04_", 1)

    # 9. MOC/index file must be skipped even if it looks open -> 0
    check("9 moc-skipped",
          "# reviews index\nVERDICT: REVISE somewhere in prose\n", "__REVIEW_MOC", 0)

    # 10. no verdict line at all -> 0
    check("10 no-verdict",
          FM % ("Video_11", "") + "\njust some review prose, no overall line\n", "Video_11_", 0)

    # 11. no video number derivable -> 0 (not a per-video gate)
    check("11 no-video",
          "---\ntype: review\n---\nVERDICT: REVISE\n", "Policy_disclosure_", 0)

    # --- false-positive classes observed live 2026-07-26 --------------------
    # 11a. frontmatter `prior_round_verdicts:` history must NOT be read as this
    #      file's verdict. Body says SHIP -> 0. (The live V13 case: flagged at
    #      its round-1 frontmatter entry while its real verdict was SHIP.)
    check("11a fm-prior-round-history",
          "---\ntype: analyze-review\nstatus: ok\nvideo: Video_11\nround: 3\n"
          "prior_round_verdicts:\n"
          "  - round: 1\n    verdict: REVISE\n    issues: 3\n"
          "  - round: 2\n    verdict: REVISE\n    issues: 1\n"
          "---\n\nVERDICT: SHIP\n", "Video_11_", 0)

    # 11b. retained-history section BELOW the current verdict must not flag.
    #      (The live V09 case: PASS 6 SHIP at top, "Prior pass retained ...
    #      VERDICT: REVISE" further down, whose own text says RESOLVED pass 4.)
    check("11b retained-history-below-ship",
          FM % ("Video_11", "") + "\n**VERDICT: SHIP**\n\n55/55 clean.\n\n"
          "# Prior pass retained — RENDERS pass 3, VERDICT: REVISE\n\n"
          "**VERDICT: REVISE** — Thumbnail_B re-failed. -> RESOLVED pass 4.\n",
          "Video_11_", 0)

    # 11c. the inverse must STILL flag: a genuinely open verdict at the top with
    #      an older SHIP retained below it. This pins the ACCEPTED CEILING of
    #      any-clean-wins: expected 0, i.e. deliberately NOT flagged. Identical
    #      to run_check's documented series ceiling #14 (a later re-bake REVISE
    #      after a SHIP is future work, not a publish gate) — the two rules now
    #      agree instead of contradicting. Change this only with the series
    #      contract, never on its own.
    check("11c ceiling: open-above-retained-ship not flagged",
          FM % ("Video_11", "") + "\n**VERDICT: REVISE** — 2 shots re-failed.\n\n"
          "# Prior pass retained — pass 2, VERDICT: SHIP\n\n**VERDICT: SHIP**\n",
          "Video_11_", 0)

    # 11d. NEWEST-AT-BOTTOM convention (the real `Video_09_VO_Review.md` shape:
    #      pass 1 REVISE at top, pass 2 SHIP appended at the bottom). A
    #      positional rule reads the stale REVISE here; any-clean-wins reads the
    #      real verdict -> 0. This is why the rule is not first-verdict-wins.
    check("11d newest-at-bottom-convention",
          FM % ("Video_11", "") + "\n**VERDICT: REVISE**\n\npass 1 findings...\n\n"
          "## Pass 2 — re-review after fixes\n\n**VERDICT: SHIP** — all fixes verified.\n",
          "Video_11_", 0)

    # (11e is a GROUP fixture — see below. A lone fm-only file can never flag on
    #  its own, so testing it single-file would be vacuous: the regression only
    #  shows up when that file has to CONCLUDE a series containing a REVISE.)

    # 11f. column-0 anchoring must not swallow a REAL open verdict: indented
    #      prior_round history (skipped) + a genuinely open body verdict -> 1.
    check("11f indented-history-plus-real-open",
          "---\ntype: analyze-review\nvideo: Video_11\nround: 2\n"
          "prior_round_verdicts:\n  - round: 1\n    verdict: SHIP\n---\n"
          "\nVERDICT: REVISE\n", "Video_11_", 1)

    # 11i. CITATION fixture — the frontmatter verdict's reported line number.
    #      Mirrors the real `Video_02_Packaging_Review.md` shape (fm verdict on
    #      line 7). Pins the off-by-one that a count-only harness could not see:
    #      _frontmatter returns text[3:end] whose leading \n is already the
    #      fence line's terminator, so the offset is +1, not +2.
    #        1 ---            5 status: ok
    #        2 date: 2026-07-06   6 tags:
    #        3 type: pkg-review   7 verdict: REVISE   <- cited
    #        4 video: Video_11    8 reviewer: x
    check("11i fm-verdict-citation-line",
          "---\ndate: 2026-07-06\ntype: pkg-review\nvideo: Video_11\nstatus: ok\n"
          "tags:\nverdict: REVISE\nreviewer: x\n---\n\nprose, no body verdict\n",
          "Video_11_", 1, exp_line=7)

    # 11g. a leading '---' HORIZONTAL RULE is not a frontmatter fence. Without
    #      the key-line guard in _frontmatter, everything to the closing '---'
    #      is skipped as if it were frontmatter and the verdict inside is lost.
    #      Pins a guard that was otherwise untested (mutation: deleting the
    #      guard left the whole suite green).
    check("11g hr-lead-is-not-frontmatter",
          "---\n\nsome prose above the verdict\n\n**VERDICT: REVISE**\n\n---\n",
          "Video_11_", 1)

    # 11h. an INDENTED body `verdict:` is nested YAML history, not a verdict.
    #      Under any-clean-wins an indented `verdict: SHIP` would otherwise EAT
    #      the live REVISE below it — a worse failure direction than the false
    #      flag the permissive regex used to cause. Must still flag.
    check("11h indented-body-verdict-is-not-a-signal",
          FM % ("Video_11", "") + "\nhistory:\n    verdict: SHIP\n\nVERDICT: REVISE\n",
          "Video_11_", 1)

    # --- multi-file (video, dir) grouping: the series-supersession contract ----
    # Each spec = (prefix, verdict_line). All share video Video_11 (receipted) and
    # land in the same tmp dir -> one (video, dir) series. Series order is the
    # filename index (Pass/Round N), NOT mtime.
    # spec = (prefix, fm_extra, verdict_line); fm_extra goes INTO the frontmatter.
    def check_group(name, specs, exp_flags):
        ran[0] += 1
        paths = [_tmp(FM % ("Video_11", fm) + "\n" + vl + "\n", prefix=pre)
                 for pre, fm, vl in specs]
        try:
            _f, _n, nf = run_check(paths, receipts)
        finally:
            for p in paths:
                os.unlink(p)
        if nf != exp_flags:
            fails.append("%s: expected %d flag, got %d" % (name, exp_flags, nf))

    # 12. intermediate Pass3 REVISE + a SHIP elsewhere in the series -> 0. This is
    #     the mtime-bug case (REVISE created LAST) AND the cross-scheme-numbering
    #     case: any SHIP in the group concludes it regardless of file order.
    check_group("12 ship-in-series", [
        ("Video_11_Script_Review_Pass5_", "", "**VERDICT: SHIP** — 49/49 PASS."),
        ("Video_11_Script_Review_Pass3_", "", "VERDICT: REVISE"),
    ], 0)

    # 13. mixed numbering schemes (_v3 REVISE + _Pass5 SHIP) -> 0 (any SHIP wins;
    #     do NOT try to order _v3 vs _Pass5).
    check_group("13 mixed-scheme-ship", [
        ("Video_11_Packaging_Review_v3_", "", "VERDICT: REVISE"),
        ("Video_11_Packaging_Review_Pass5_", "", "## VERDICT: SHIP"),
    ], 0)

    # 14. later REVISE after an earlier SHIP -> 0 (the accepted ceiling: a shipped
    #     video's future-re-bake REVISE is not a publish-blocking gate).
    check_group("14 later-revise-after-ship", [
        ("Video_11_VO_Review_Pass2_", "", "VERDICT: SHIP"),
        ("Video_11_VO_Review_Pass4_", "", "VERDICT: REVISE"),
    ], 0)

    # 15. no SHIP anywhere in the series (Round3 + Round6 both REVISE) -> 1
    #     (the render shipped ship-as-is with the gate only ever recording REVISE).
    check_group("15 no-ship-in-series", [
        ("Video_11_Script_Review_Round3_", "", "VERDICT: REVISE"),
        ("Video_11_Script_Review_Round6_", "", "## VERDICT: REVISE"),
    ], 1)

    # 11e. frontmatter-only verdict concluding a series (the real
    #      `Lead_Magnets/_REVIEW/Video_09_LeadMagnet_Review.md` shape: column-0
    #      `verdict: SHIP` in frontmatter, ZERO body VERDICT: lines). Paired
    #      with a REVISE sibling so the fm verdict has to do real work — if the
    #      frontmatter signal is dropped, that file classifies 'none', stops
    #      concluding the series, and the linter manufactures a false FLAG on a
    #      shipped video: the exact phantom class this script exists to prevent,
    #      re-created by its own fix. Must be 0.
    check_group("11e fm-only-verdict-concludes-series", [
        ("Video_11_LeadMagnet_Review_a_", "verdict: SHIP\n", ""),
        ("Video_11_LeadMagnet_Review_b_", "", "VERDICT: REVISE"),
    ], 0)

    # 16. all-REVISE series but ONE file carries a resolution stamp -> 0
    #     (a stamp anywhere concludes the gate).
    check_group("16 stamp-concludes", [
        ("Video_11_Script_Review_a_", "", "VERDICT: REVISE"),
        ("Video_11_Script_Review_b_",
         "resolution: closed-by-steve-decision-2026-07-12\n", "VERDICT: REVISE"),
    ], 0)

    if fails:
        print("verdict_resolution_lint selftest: FAIL")
        for f in fails:
            print("  - " + f)
        return 1
    # Counted, never hardcoded — a stale literal in a verification instrument is
    # the exact drift this script polices (it read "16/16" while running 19).
    print("verdict_resolution_lint selftest: OK (%d/%d)" % (ran[0], ran[0]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
