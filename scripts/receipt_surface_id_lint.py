#!/usr/bin/env python3
"""receipt_surface_id_lint.py — surface-vs-receipt video-ID linter (A-45).

Read-only. The third leg of launch-spine ground-truthing, next to
triad_sync_check.py (A-37, surface-vs-surface topic drift) and
youtube_reality_check.py (A-40, receipt-vs-LIVE-YouTube). This one closes the
middle: does what the vault SAYS in its human-facing launch-spine surfaces match
the canonical video IDs its OWN machine-written receipts record.

The defect it catches (hand-caught by Cowork 2026-07-08 + re-caught 07-09):
a video's YouTube id changes (a delete + re-upload mints a NEW id; a scheduled
video minting an id) and the prose surfaces keep asserting the STALE id — e.g.
V4 was `ClJVIUtwsVE`, deleted from the channel (receipt now records it as
`deleted_video_id`, `video_id: null`), yet Runway/Index rows still read
"published `ClJVIUtwsVE`".

Signals (per id occurrence in a surface):
  🔴 FLAG — a receipt's DEAD id (its `deleted_video_id`) is cited in a surface
            WITHOUT a death/reconciliation marker in its local context, i.e. the
            surface asserts a dead id as if it were live. Drives exit 1 + Telegram.
  🟡 WARN — an id-SHAPED token (11 base64url chars, mixed-case) that matches NO
            receipt id (neither a live `video_id` nor a `deleted_video_id`) and is
            not sitting in a death/recon context — a surface cites an id the
            receipts don't know (stale ghost id, or a receipt that never updated).
            Report-only, never pages. This is the "note surface ids with no
            receipt" half of A-45.

A DEAD-id mention in death/recon prose ("was published X, DELETED", "old X DEAD",
"X shelved") is CORRECT bookkeeping and is silently OK — the window check below
distinguishes "asserts X as live" (FLAG) from "records X as dead" (fine).

Surfaces scanned (all three launch-spine surfaces — unlike triad_sync, the
Integrity Map is NOT a truth surface for IDs; it cites ids in prose too and
carried the same 07-08 reconciliation residue):
  - BRANDS/3SK_Finance/Video_Topic_Integrity_Map.md
  - BRANDS/3SK_Finance/Launch_Runway.md
  - BRANDS/3SK_Finance/Lead_Magnets/_Lead_Magnet_Index.md

ponytail: no per-slot association. A finding reports file:line + the id + a
snippet, not "this is V5's row cites V9's id". The unknown-id WARN already
surfaces a stale/ghost id (V5's `122wyNCEOXo` vs receipt `J3yZK0Ik64s`) without
needing to parse which row it sits in; the cross-wire "live id of the WRONG slot"
case has no documented instance and would need fragile row parsing. Add slot
association only if a real cross-wire defect appears.

Usage:
  receipt_surface_id_lint.py               # scan, quiet on clean/warn
  receipt_surface_id_lint.py --verbose     # print the full report to stdout
  receipt_surface_id_lint.py --report-only # always exit 0 (gentle pre-brief pass)
  receipt_surface_id_lint.py --quiet-ok    # suppress the clean-run stdout line
  receipt_surface_id_lint.py --selftest    # run the review-gate fixtures
Exit: 0 on CLEAN / WARN-only, 1 on any FLAG (or a selftest failure).
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
RECEIPT_GLOB = os.path.join(FIN, "Production_Kits", "Video_*_youtube_upload.json")
INDEX = os.path.join(FIN, "Lead_Magnets", "_Lead_Magnet_Index.md")
RUNWAY = os.path.join(FIN, "Launch_Runway.md")
IMAP = os.path.join(FIN, "Video_Topic_Integrity_Map.md")
REPORT = os.path.join(FIN, "Raw_Assets", "_receipt_surface_id_report.md")
NOTIFY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "notify.sh")

SURFACES = [("Video_Topic_Integrity_Map", IMAP),
            ("Launch_Runway", RUNWAY),
            ("_Lead_Magnet_Index", INDEX)]

# A YouTube id is exactly 11 base64url chars. Bound it so an 11-char SUBSTRING of
# a longer token (a filename fragment "Generationa" of "Generational") is NOT a
# match — only free-standing 11-char runs.
ID_RE = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{11}(?![A-Za-z0-9_-])")

# Death / reconciliation markers. If one appears in a DEAD id's local window the
# mention is correct bookkeeping (records it as dead), not a stale live-assertion.
# Also silences the unknown-id WARN for historical ghost ids kept in dead prose
# (e.g. "old FJljsipxTkA DEAD"). Substring match, case-folded.
DEATH_MARKERS = (
    "delet", "dead", "removed", " gone", "nulled", "defunct", "404",
    "shelved", "retired", "reconcil", "stale", "was published", "formerly",
    "former ", "supersed", "replaced", "re-upload", "reupload", " old ",
    "earlier ", "no longer", "not live",
)
# FLAG (dead-id-as-live) reconciles at LINE granularity — reconciliation is
# written per table-row / per sentence, and a char window bleeds across lines
# (a row's id would inherit the next line's "RECONCILIATION" blockquote).
# WARN (unknown ghost id) uses a TIGHT window so a ghost id on the dense
# status-board line (which also carries far-off "DELETED"/"DEAD" for OTHER
# videos) still surfaces.
WARN_WINDOW = 30  # chars of context each side of an unknown-id occurrence


def load_receipts(paths):
    """Return (live, dead): {id -> video_label}. live = current video_id,
    dead = deleted_video_id. A receipt with video_id null (deleted) contributes
    only to dead."""
    live, dead = {}, {}
    for p in sorted(paths):
        try:
            with open(p, encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            continue  # unreadable/garbage receipt is skipped, not fatal
        label = d.get("video") or os.path.basename(p)
        vid = d.get("video_id")
        dvid = d.get("deleted_video_id")
        if isinstance(vid, str) and vid.strip():
            live[vid.strip()] = label
        if isinstance(dvid, str) and dvid.strip():
            dead[dvid.strip()] = label
    return live, dead


def _is_id_shaped(tok):
    """A real youtube id is 11 random base64url chars. The NOISE that survives
    the bounded 11-char regex is two classes, both cleanly excluded by requiring
    a digit AND mixed case:
      - Title-case words / hyphen-compounds ("Millionaire", "Help-Budget",
        "archetype-C") — mixed case but NO digit.
      - lowercase filename stems / domains ("video_04_hd", "3sk-finance") — have
        a digit but NO uppercase.
    A base64url id effectively always has both (the ghost ids we must catch —
    122wyNCEOXo, n8l84_UBLUc — do).

    ponytail: heuristic ceiling — a real id with no digit (~12% of ids) or all
    one case is missed by the WARN branch. Acceptable: the FLAG channel uses
    EXACT known-dead-id matching (no heuristic), so a missed WARN is at most one
    un-noted ghost id, never a wrong page. Tighten to a charset/entropy test only
    if a no-digit ghost id ever slips."""
    return (any(c.isdigit() for c in tok)
            and any(c.islower() for c in tok)
            and any(c.isupper() for c in tok))


def _line_of(text, start):
    lo = text.rfind("\n", 0, start) + 1
    hi = text.find("\n", start)
    if hi == -1:
        hi = len(text)
    return text[lo:hi]


def _death_line(text, start):
    # ponytail: line-granularity ceiling — a genuine dead-id-as-live citation is
    # suppressed if its physical line carries an UNRELATED marker substring (a
    # "404" page-fix note, "delet" inside "deleted-scenes cut"). Chosen over a
    # char window on purpose: a window bleeds a table row's id into the next
    # line's reconciliation blockquote. No such collision exists on disk today;
    # if one appears, tighten to "marker within N words of the id".
    return any(m in _line_of(text, start).lower() for m in DEATH_MARKERS)


def _death_window(text, start, end):
    w = text[max(0, start - WARN_WINDOW):end + WARN_WINDOW].lower()
    return any(m in w for m in DEATH_MARKERS)


def _snippet(text, start):
    s = _line_of(text, start).strip()
    return (s[:140] + " …") if len(s) > 140 else s


def scan_surface(text, live, dead):
    """Return list of (signal, id, line, snippet) for one surface's text."""
    findings = []
    for m in ID_RE.finditer(text):
        tok, start = m.group(0), m.start()
        line = text.count("\n", 0, start) + 1
        if tok in dead:
            # Dead id -> FLAG only if its LINE asserts it as live (no death /
            # reconciliation marker on the row/sentence).
            if not _death_line(text, start):
                findings.append(("FLAG", tok, line, _snippet(text, start)))
        elif tok in live:
            continue  # correct current id — clean
        elif _is_id_shaped(tok) and not _death_window(text, start, m.end()):
            # id-shaped token matching no receipt, not sitting in dead prose.
            findings.append(("WARN", tok, line, _snippet(text, start)))
    return findings


def run_check(surface_paths, receipt_paths):
    live, dead = load_receipts(receipt_paths)
    findings = []  # (surface_label, signal, id, line, snippet)
    for label, path in surface_paths:
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            continue
        for signal, tok, line, snip in scan_surface(text, live, dead):
            findings.append((label, signal, tok, line, snip))
    n_flags = sum(1 for f in findings if f[1] == "FLAG")
    n_warns = sum(1 for f in findings if f[1] == "WARN")
    return findings, len(live), len(dead), n_flags, n_warns


def _now_et():
    # ET is UTC-4 (EDT) in the summer launch window; report stamp only.
    return (datetime.now(timezone.utc) - timedelta(hours=4)).strftime("%Y-%m-%d %H:%M ET")


def write_report(findings, n_live, n_dead, n_flags, n_warns):
    verdict = "FLAG" if n_flags else ("WARN-only" if n_warns else "CLEAN")
    lines = [
        "# Receipt-vs-surface video-ID report",
        "",
        "_Generated %s by `scripts/receipt_surface_id_lint.py` (read-only)._" % _now_et(),
        "",
        "**Verdict: %s** — %d live ids + %d dead ids known from receipts; "
        "%d flags, %d warns." % (verdict, n_live, n_dead, n_flags, n_warns),
        "",
        "| surface | line | signal | id | context |",
        "| --- | --- | --- | --- | --- |",
    ]
    if findings:
        for label, signal, tok, line, snip in sorted(
                findings, key=lambda f: (f[1] != "FLAG", f[0], f[3])):
            icon = "🔴 FLAG" if signal == "FLAG" else "🟡 WARN"
            snip = snip.replace("|", "\\|")
            lines.append("| %s | %d | %s | `%s` | %s |" % (label, line, icon, tok, snip))
    else:
        lines.append("| — | — | ✅ | — | every cited id matches a receipt |")
    lines.append("")
    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    with open(REPORT, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return verdict


def summary_line(verdict, n_flags, n_warns, findings):
    if verdict == "FLAG":
        f = next(x for x in findings if x[1] == "FLAG")
        return ("receipt_surface_id_lint: 🔴 FLAG — %s line %d asserts dead id %s"
                % (f[0], f[3], f[2]))
    return "receipt_surface_id_lint: %s (%d flags, %d warns)" % (verdict, n_flags, n_warns)


def notify(msg):
    if not os.access(NOTIFY, os.X_OK):
        return
    try:
        subprocess.run([NOTIFY, msg], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=20)
    except Exception:
        pass  # best-effort; never block the check on a notify failure


def main(argv=None):
    ap = argparse.ArgumentParser(description="Surface-vs-receipt video-ID linter (read-only).")
    ap.add_argument("--verbose", action="store_true", help="print full report to stdout")
    ap.add_argument("--report-only", action="store_true", help="always exit 0")
    ap.add_argument("--quiet-ok", action="store_true", help="suppress the clean-run stdout line")
    ap.add_argument("--selftest", action="store_true", help="run review-gate fixtures")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    receipts = glob.glob(RECEIPT_GLOB)
    findings, n_live, n_dead, n_flags, n_warns = run_check(SURFACES, receipts)
    verdict = write_report(findings, n_live, n_dead, n_flags, n_warns)
    line = summary_line(verdict, n_flags, n_warns, findings)

    if verdict == "FLAG":
        print(line)
        notify(line + " — see BRANDS/3SK_Finance/Raw_Assets/_receipt_surface_id_report.md")
    elif not args.quiet_ok:
        print(line)
    if args.verbose:
        with open(REPORT, encoding="utf-8") as fh:
            print(fh.read())

    if args.report_only:
        return 0
    return 1 if n_flags else 0


# --- selftest fixtures (the pickup's review-gate cases) ----------------------
def _tmp(text, suffix=".md"):
    import tempfile
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def _receipt(video, video_id=None, deleted_video_id=None):
    d = {"video": video, "video_id": video_id, "deleted_video_id": deleted_video_id}
    return _tmp(json.dumps(d), suffix=".json")


def selftest():
    fails = []
    LIVE = "DY2RVnuUb64"   # a real live id shape
    DEAD = "ClJVIUtwsVE"   # a real deleted id shape
    GHOST = "122wyNCEOXo"  # id-shaped, no receipt (the V5 case)
    rlive = _receipt("Video_09", video_id=LIVE)
    rdead = _receipt("Video_04", video_id=None, deleted_video_id=DEAD)
    receipts = [rlive, rdead]

    def check(name, surface_text, exp_flags, exp_warns):
        s = _tmp(surface_text)
        f, _, _, nf, nw = run_check([("t", s)], receipts)
        os.unlink(s)
        if nf != exp_flags:
            fails.append("%s: expected %d FLAG, got %d" % (name, exp_flags, nf))
        if nw != exp_warns:
            fails.append("%s: expected %d WARN, got %d" % (name, exp_warns, nw))

    # 1. clean: cites the correct live id -> 0/0
    check("1 clean", "| 9 | Generational Wealth | published `%s` |\n" % LIVE, 0, 0)

    # 2. stale-dead-id asserted as live (no death marker) -> 1 FLAG
    check("2 stale-dead-live", "| 4 | First In Your Family (PUBLISHED `%s`) |\n" % DEAD, 1, 0)

    # 3. reconciled dead id (death marker in context) -> 0 FLAG (false-positive guard)
    check("3 reconciled-dead", "V4 was published `%s`, DELETED from the channel.\n" % DEAD, 0, 0)

    # 4. no-receipt id-shaped token (the ghost id) -> 1 WARN, 0 FLAG
    check("4 no-receipt-ghost", "V5 published (public, %s)\n" % GHOST, 0, 1)

    # 5. no id cited at all -> 0/0
    check("5 no-id", "| 3 | Net Worth By Age — script done, magnet built, all clean |\n", 0, 0)

    # 6. lowercase-only noise (filename/domain, not an id) must NOT warn -> 0/0
    check("6 lowercase-noise",
          "manifest `video_04_hd` deployed to 3sk-finance via 3-archetype path\n", 0, 0)

    # 7. dense line: a dead id in death context + a ghost id in live context, same line.
    #    Per-occurrence window (not line-level) must OK the dead id AND warn the ghost.
    check("7 dense-mixed-line",
          "V4 DELETED (was `%s`)  V5 published(public, %s)\n" % (DEAD, GHOST), 0, 1)

    # 8. correct live id must never warn even though it is id-shaped -> 0/0
    check("8 live-not-warned", "chapters filled on the live video `%s` per the pack\n" % LIVE, 0, 0)

    # 9. an id-shaped token in dead prose (historical ghost) must NOT warn -> 0/0
    check("9 ghost-in-dead-prose", "re-upload after the bake — earlier `%s` DEAD\n" % GHOST, 0, 0)

    for r in receipts:
        os.unlink(r)

    if fails:
        print("receipt_surface_id_lint --selftest: FAIL")
        for f in fails:
            print("  ✗ " + f)
        return 1
    print("receipt_surface_id_lint --selftest: PASS (9/9 fixtures)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
