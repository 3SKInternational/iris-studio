#!/usr/bin/env python3
"""triad_sync_check.py — launch-spine drift linter (A-37 / DQ-27 follow-on).

Read-only. Cross-checks the two CONSUMER launch-spine surfaces against the
canonical topic-per-slot truth (encoded in CANON below, authored from
Video_Topic_Integrity_Map.md) and raises two signals per active production slot:

  🔴 FLAG  — an active slot's row in a consumer surface carries a distinctive
             keyword from a SHELVED/retired topic family that is NOT that slot's
             canonical topic (the exact wrong-magnet-at-publish defect that hit
             6/23 Debt→V6, 6/24 Tier-B, 6/25 Roth→V4). Drives exit 1 + Telegram.
  🟡 WARN  — a consumer row is missing the slot's canonical keyword. Usually just
             phrasing drift; report-only, never pages.

Surfaces scanned (the consumers that caused every documented defect):
  - BRANDS/3SK_Finance/Lead_Magnets/_Lead_Magnet_Index.md   (main magnet table)
  - BRANDS/3SK_Finance/Launch_Runway.md                     (Tier A + Tier B)

The Integrity Map is the TRUTH surface (its rows legitimately carry "Old Roth
SHELVED" reconciliation prose, so it is NOT scanned — CANON is its encoding).

ponytail: the 6/27 scope-widen to master-notes + companion-strategy is DEFERRED,
not built — shelved master files coexist on disk by slot number (Video_04 Roth +
Video_04 First-Gen both present) and those surfaces carry no keyword table or
per-slot row contract, so a naive scan there false-positives and fails the
green-on-first-run gate. Upgrade path: add an "active-master" resolver keyed off
the Integrity Map before extending SURFACES. See the pickup's SCOPE WIDENED note.

Usage:
  triad_sync_check.py                 # scan live surfaces, quiet on clean/warn
  triad_sync_check.py --verbose       # print the full report to stdout
  triad_sync_check.py --report-only   # always exit 0 (gentle pre-brief pass)
  triad_sync_check.py --quiet-ok      # suppress the clean-run stdout line
  triad_sync_check.py --selftest      # run the review-gate fixtures
Exit: 0 on CLEAN / WARN-only, 1 on any FLAG (or a selftest failure).
"""
import argparse
import os
import re
import subprocess
import sys
from datetime import datetime, timezone, timedelta

# --- paths -------------------------------------------------------------------
VAULT = "/Users/steve/Documents/3SK/outputs"
FIN = os.path.join(VAULT, "BRANDS", "3SK_Finance")
INDEX = os.path.join(FIN, "Lead_Magnets", "_Lead_Magnet_Index.md")
RUNWAY = os.path.join(FIN, "Launch_Runway.md")
REPORT = os.path.join(FIN, "Raw_Assets", "_triad_sync_report.md")
NOTIFY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "notify.sh")

SURFACES = [("_Lead_Magnet_Index", INDEX), ("Launch_Runway", RUNWAY)]
ACTIVE_SLOTS = range(1, 14)  # V1..V13; V14 was retired in the 6/20 relock.

# --- keyword tables (CANON = truth encoding; SHELVED = tripwire) -------------
CANON = {
    1:  ["save 50", "50%", "saver"],
    2:  ["net worth"],
    3:  ["first millionaire", "millionaire in your family"],
    4:  ["first in your family", "first-gen", "first gen"],
    5:  ["investor", "every level of investor"],
    6:  ["family sees you", "how your family sees"],
    7:  ["build wealth in silence", "in silence", "silent wealth"],
    8:  ["your house", "house at every level"],
    9:  ["never tell"],
    10: ["how people treat", "people treat you"],
    11: ["your job", "job at every level"],
    12: ["quietly", "quietly became a millionaire"],
    13: ["every level of saver", "saver"],
}
SHELVED = {
    # "roth" is a bare token because the shelved Roth magnet leaks as glued
    # camelCase filenames (Video_04_15Min_Roth_Setup) that underscore/space
    # normalization can't split; no active slot is a Roth topic, so it's safe.
    "roth $200 / 15-min roth": ["roth", "$200/month", "$200/mo", "15-min roth", "invest $200"],
    "debt payoff":             ["debt payoff", "debt-payoff", "payoff order"],
    "$500k windfall":          ["$500k", "500k decision"],
    "credit score":            ["credit score", "credit-score"],
    "passive income":          ["passive income"],
    "401k match":              ["match math", "401(k) match", "401k match"],
    "rent vs buy":             ["rent vs buy", "rent-vs-buy"],
    "4-generation map":        ["4-generation", "4 generation", "generation map"],
    "fi number":               ["fi number", "fi-number"],
}
# SHELVED families a specific slot is CANONICALLY allowed to reference (so they
# don't FLAG). V13 "Every Level of Saver" legitimately offers the FI-Number
# Calculator as a reuse candidate per all three surfaces.
SLOT_ALLOW = {13: {"fi number"}}
# Reconciliation-aside trigger words (word-boundary matched) — a PARENTHETICAL
# clause containing one is stripped before the SHELVED tripwire, so
# "(NOT the shelved Roth topic)" can't false-FLAG. Only parentheticals are
# stripped; em-dash topic separators are the norm in these files and are kept.
RECON_TRIGGERS = ("shelved", "retired", "not", "old")


def parse_slot_rows(text):
    """Return {slot_int: full_row_text} for integer-first pipe-table rows.

    Stops permanently at the first heading whose text contains 'shelved'
    (case-insensitive) — this excludes the Index's Shelved-explainer pool AND
    the Email-gated companions table (both sit after that heading and both
    legitimately list old slot->old-magnet pairs). Runway has no such heading,
    so its Tier-A and Tier-B tables (the only integer-first pipe rows in it)
    are both read. Blockquotes / prose / the one-line status board are excluded
    because they are not integer-first pipe rows.
    """
    rows = {}
    row_re = re.compile(r"^\|\s*(\d+)\s*\|(.*)$")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            low = stripped.lower()
            # Stop at legacy/companion tables that list old slot->old-magnet
            # pairs by design (Shelved-explainer pool AND Email-gated companions).
            # Match the "Email-gated companions" heading specifically, NOT the
            # live "Magnets (... email-gated opt-in)" heading that carries the
            # real per-slot table.
            if "shelved" in low or "email-gated companion" in low:
                break
        m = row_re.match(line)
        if not m:
            continue
        slot = int(m.group(1))
        if slot in ACTIVE_SLOTS:
            rows[slot] = m.group(2)  # keep the row body; last write wins per slot
    return rows


_RECON_RE = re.compile(r"\b(%s)\b" % "|".join(RECON_TRIGGERS))


def normalize(text):
    """Lowercase and flatten magnet-filename separators to spaces.

    The wrong-magnet payload is usually the magnet FILENAME in backticks
    (`Video_06_Debt_Payoff_Order.pdf`), so underscores and backticks must
    collapse to spaces before matching or every filename-form leak evades the
    space-form SHELVED keywords.
    """
    return text.lower().replace("_", " ").replace("`", " ")


def strip_recon_clauses(scan):
    """Blank out PARENTHETICAL asides that are reconciliation/self-reference
    notes (contain a word-boundary trigger). Em-dash segments are the normal
    topic separator in these files and are deliberately kept — a shelved leak
    in an em-dash segment should still FLAG. `scan` is already normalized.
    """
    return re.sub(r"\([^()]*\)",
                  lambda m: " " if _RECON_RE.search(m.group(0)) else m.group(0),
                  scan)


def check_slot(slot, row):
    """Return list of (signal, family, detail) for one slot's consumer row."""
    findings = []
    canon_kw = [normalize(k) for k in CANON.get(slot, [])]
    allowed = SLOT_ALLOW.get(slot, set())
    row_norm = normalize(row)
    # 🔴 FLAG: shelved keyword leaked into an active slot row (not its own canon,
    # not a slot-allowed reuse).
    scan = strip_recon_clauses(row_norm)
    for family, kws in SHELVED.items():
        if family in allowed:
            continue
        for kw in kws:
            kwn = normalize(kw)
            if kwn in scan and kwn not in canon_kw:
                findings.append(("FLAG", family, "shelved '%s' keyword '%s'" % (family, kw)))
                break  # one hit per family is enough
    # 🟡 WARN: slot's canonical keyword absent from the row.
    if canon_kw and not any(k in row_norm for k in canon_kw):
        findings.append(("WARN", "canon-absent", "no canonical keyword %s in row" % canon_kw))
    return findings


def run_check(surfaces):
    """Scan surfaces; return (findings, n_slots, n_flags, n_warns)."""
    findings = []  # (slot, surface_label, signal, family, detail)
    slots_seen = set()
    for label, path in surfaces:
        with open(path, encoding="utf-8") as fh:
            rows = parse_slot_rows(fh.read())
        for slot, row in rows.items():
            slots_seen.add(slot)
            for signal, family, detail in check_slot(slot, row):
                findings.append((slot, label, signal, family, detail))
    n_flags = sum(1 for f in findings if f[2] == "FLAG")
    n_warns = sum(1 for f in findings if f[2] == "WARN")
    return findings, len(slots_seen), n_flags, n_warns


def _now_et():
    # ET is UTC-4 (EDT) in the June/July launch window; report stamp only.
    return (datetime.now(timezone.utc) - timedelta(hours=4)).strftime("%Y-%m-%d %H:%M ET")


def write_report(findings, n_slots, n_flags, n_warns):
    verdict = "FLAG" if n_flags else ("WARN-only" if n_warns else "CLEAN")
    lines = [
        "# Launch-spine triad-sync report",
        "",
        "_Generated %s by `scripts/triad_sync_check.py` (read-only)._" % _now_et(),
        "",
        "**Verdict: %s** — %d slots, %d flags, %d warns." % (verdict, n_slots, n_flags, n_warns),
        "",
        "| slot | surface | signal | detail |",
        "| --- | --- | --- | --- |",
    ]
    if findings:
        for slot, label, signal, family, detail in sorted(findings, key=lambda f: (f[2] != "FLAG", f[0])):
            icon = "🔴 FLAG" if signal == "FLAG" else "🟡 WARN"
            lines.append("| V%d | %s | %s | %s |" % (slot, label, icon, detail))
    else:
        lines.append("| — | — | ✅ | all active slots in sync across consumer surfaces |")
    lines.append("")
    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    with open(REPORT, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return verdict


def summary_line(verdict, n_slots, n_flags, n_warns, findings):
    if verdict == "FLAG":
        first = next(f for f in findings if f[2] == "FLAG")
        return "triad_sync_check: 🔴 FLAG — V%d %s references %s" % (first[0], first[1], first[4])
    return "triad_sync_check: %s (%d slots, %d flags, %d warns)" % (verdict, n_slots, n_flags, n_warns)


def notify(msg):
    if not os.access(NOTIFY, os.X_OK):
        return
    try:
        subprocess.run([NOTIFY, msg], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=20)
    except Exception:
        pass  # best-effort; never block the check on a notify failure


def main(argv=None):
    ap = argparse.ArgumentParser(description="Launch-spine triad-sync linter (read-only).")
    ap.add_argument("--verbose", action="store_true", help="print full report to stdout")
    ap.add_argument("--report-only", action="store_true", help="always exit 0")
    ap.add_argument("--quiet-ok", action="store_true", help="suppress the clean-run stdout line")
    ap.add_argument("--selftest", action="store_true", help="run review-gate fixtures")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    findings, n_slots, n_flags, n_warns = run_check(SURFACES)
    verdict = write_report(findings, n_slots, n_flags, n_warns)
    line = summary_line(verdict, n_slots, n_flags, n_warns, findings)

    if verdict == "FLAG":
        print(line)
        notify(line + " — see BRANDS/3SK_Finance/Raw_Assets/_triad_sync_report.md")
    elif not args.quiet_ok:
        print(line)
    if args.verbose:
        with open(REPORT, encoding="utf-8") as fh:
            print(fh.read())

    if args.report_only:
        return 0
    return 1 if n_flags else 0


# --- selftest fixtures (the pickup's 5 binary-review-gate cases) -------------
def _tmp(text):
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".md")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def selftest():
    fails = []

    # 1. Clean-state: live surfaces must be CLEAN or WARN-only, zero FLAGs.
    _, _, live_flags, _ = run_check(SURFACES)
    if live_flags:
        fails.append("1 clean-state: expected 0 FLAGs on live surfaces, got %d" % live_flags)

    # 1b. Each live surface must actually parse its full active-slot set — guards
    #     the silent-empty-surface class (a break-too-early bug makes a surface
    #     contribute 0 rows, which a combined-scan clean check can't see).
    for label, path in SURFACES:
        with open(path, encoding="utf-8") as fh:
            got = set(parse_slot_rows(fh.read()))
        if not (set(ACTIVE_SLOTS) <= got):
            fails.append("1b non-empty-parse: %s parsed %s, expected V1..V13" % (label, sorted(got)))

    # 2. Injected defect in the REAL payload form: V6's magnet filename reverted
    #    to the shelved Debt artifact -> FLAG V6. (The prior prose-only fixture
    #    masked the underscore-filename false-negative.)
    bad = _tmp(
        "## Magnets\n"
        "| 6 | POV: How Your Family Sees You | `Video_06_Debt_Payoff_Order.pdf` | own | ok |\n"
        "### Shelved explainer pool\n"
        "| 6 | Debt Payoff Order | old | ok |\n"
    )
    f2, _, flags2, _ = run_check([("inj", bad)])
    os.unlink(bad)
    if not (flags2 == 1 and any(x[0] == 6 and x[2] == "FLAG" for x in f2)):
        fails.append("2 injected-defect (filename form): expected 1 FLAG on V6, got %d" % flags2)

    # 3. Shelved-section false-positive: rows under a Shelved heading must NOT flag.
    shelved_only = _tmp(
        "## Magnets\n"
        "| 6 | POV: How Your Family Sees You When You're First To Get Rich | own | ok |\n"
        "### Shelved explainer pool\n"
        "| 6 | Debt Payoff Order | old | ok |\n"
        "| 8 | Credit Score Action Card | old | ok |\n"
    )
    _, _, flags3, _ = run_check([("shelved", shelved_only)])
    os.unlink(shelved_only)
    if flags3:
        fails.append("3 shelved-section: expected 0 FLAGs, got %d" % flags3)

    # 4. Reconciliation-note false-positive: "NOT the shelved Roth topic" must NOT flag.
    recon = _tmp(
        "## Magnets\n"
        "| 4 | POV: Every Level of Wealth — First In Your Family "
        "(NOT the shelved Roth $200/mo topic) | Blueprint | ok |\n"
    )
    _, _, flags4, _ = run_check([("recon", recon)])
    os.unlink(recon)
    if flags4:
        fails.append("4 reconciliation-note: expected 0 FLAGs, got %d" % flags4)

    # 5. Two-table parse: Runway Tier A (V1-4) AND Tier B (V5-13) both read.
    two = _tmp(
        "### Tier A\n"
        "| 1 | Your Life If You Save 50% vs 10% | ok |\n"
        "| 4 | POV: Every Level of Wealth — First In Your Family | ok |\n"
        "### Tier B\n"
        "| 5 | Every Level of Investor | ok |\n"
        "| 13 | Every Level of Saver | ok |\n"
    )
    with open(two, encoding="utf-8") as fh:
        parsed = parse_slot_rows(fh.read())
    os.unlink(two)
    if not ({1, 4, 5, 13} <= set(parsed)):
        fails.append("5 two-table: expected slots 1,4,5,13 parsed, got %s" % sorted(parsed))

    # 6. camelCase Roth filename leak on active V4 -> FLAG (glued "15Min_Roth").
    roth = _tmp(
        "## Magnets\n"
        "| 4 | POV: Every Level of Wealth — First In Your Family | "
        "`Video_04_15Min_Roth_Setup_Checklist.pdf` | own | ok |\n"
    )
    _, _, flags6, _ = run_check([("roth", roth)])
    os.unlink(roth)
    if flags6 != 1:
        fails.append("6 camelCase-roth-filename: expected 1 FLAG on V4, got %d" % flags6)

    # 7. V13 FI-Number reuse is canonically allowed -> must NOT FLAG.
    fi = _tmp(
        "## Magnets\n"
        "| 13 | Every Level of Saver — reuse `Video_14_FI_Number_Calculator.xlsx` | A | ok |\n"
    )
    _, _, flags7, _ = run_check([("fi", fi)])
    os.unlink(fi)
    if flags7:
        fails.append("7 fi-number-allowed-on-13: expected 0 FLAGs, got %d" % flags7)

    # 8. Shelved leak in an em-dash segment (not a parenthetical) must still FLAG.
    emdash = _tmp(
        "## Magnets\n"
        "| 6 | POV: How Your Family Sees You — reused Debt Payoff Order magnet | own | ok |\n"
    )
    _, _, flags8, _ = run_check([("emdash", emdash)])
    os.unlink(emdash)
    if flags8 != 1:
        fails.append("8 em-dash-leak: expected 1 FLAG on V6, got %d" % flags8)

    # 9. Regression lock: an "email-gated opt-in" phrase in the MAIN table's own
    #    heading must NOT stop the parse (only "Email-gated companions" does).
    optin = _tmp(
        "## Magnets (ship Day 1 — email-gated opt-in)\n"
        "| 6 | POV: How Your Family Sees You | own | ok |\n"
        "## Email-gated companions\n"
        "| 6 | Debt Payoff drip | old | ok |\n"
    )
    with open(optin, encoding="utf-8") as fh:
        p9 = parse_slot_rows(fh.read())
    os.unlink(optin)
    if 6 not in p9:
        fails.append("9 email-gated-optin-heading: expected V6 parsed, got %s" % sorted(p9))

    if fails:
        print("triad_sync_check --selftest: FAIL")
        for f in fails:
            print("  ✗ " + f)
        return 1
    print("triad_sync_check --selftest: PASS (10/10 fixtures)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
