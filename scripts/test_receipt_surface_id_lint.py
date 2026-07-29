#!/usr/bin/env python3
"""Tests for receipt_surface_id_lint.py's CLI-level guards.

THE GAP (2026-07-28 pre-review gate, mandatory per the two-pass audit follow-up).
This linter's own --selftest exercises scan_surface()/run_check() directly but
never main() — so the zero-scan guard (exit 75), the scanned-count "scope"
segment in summary_line, and the literal 75 itself carried ZERO suite coverage.
`scripts/precheck.sh --mutate` found exactly this: 9 survivors, all on the lines
the 2026-07-28 zero-scan fix touched (:251 scope conditional, :289 the guard
itself, :297 the 75 literal).

run_job.sh treats 75 and 76 completely differently (75 = silent skip + retry,
76 = 🔴 Telegram page), so pinning the LITERAL — not just "nonzero" — matters.

Run: python3 scripts/test_receipt_surface_id_lint.py   (exit 0 = pass)
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import tempfile
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "receipt_surface_id_lint", Path(__file__).with_name("receipt_surface_id_lint.py")
)
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)


def _patch_vault(fin_dir: Path):
    """Point every module-level path global at a hermetic temp FIN dir — same
    monkeypatch style pipeline_stage_truth_lint.py's own selftest() already uses
    for its sibling. Returns a restore function."""
    old = (m.FIN, m.RECEIPT_GLOB, m.INDEX, m.RUNWAY, m.IMAP, m.REPORT, m.SURFACES,
           m.NOTIFY)
    fin = str(fin_dir)
    # NOTIFY MUST be redirected too. notify() only skips when the target is not
    # executable (`os.access(NOTIFY, os.X_OK)`), and scripts/notify.sh IS
    # executable — so a FLAG fixture driven through main() sent a REAL Telegram
    # alert to Steve. It did: this suite fired
    #   "🔴 FLAG — _Lead_Magnet_Index line 1 asserts dead id ClJVIUtwsVE"
    # (line 1 of the tiny temp fixture, using the real V04 dead id) hundreds of
    # times on 2026-07-28, because the mutation gate runs every suite once PER
    # MUTANT. A test whose side effect escapes the test is not hermetic; point
    # this at a path inside the temp dir that will never exist.
    # See feedback_force_stub_side_effect_modules.
    m.NOTIFY = os.path.join(fin, "_no_notify_in_tests.sh")
    m.FIN = fin
    m.RECEIPT_GLOB = os.path.join(fin, "Production_Kits", "Video_*_youtube_upload.json")
    m.INDEX = os.path.join(fin, "Lead_Magnets", "_Lead_Magnet_Index.md")
    m.RUNWAY = os.path.join(fin, "Launch_Runway.md")
    m.IMAP = os.path.join(fin, "Video_Topic_Integrity_Map.md")
    m.REPORT = os.path.join(fin, "Raw_Assets", "_receipt_surface_id_report.md")
    m.SURFACES = [("Video_Topic_Integrity_Map", m.IMAP),
                  ("Launch_Runway", m.RUNWAY),
                  ("_Lead_Magnet_Index", m.INDEX)]

    def restore():
        (m.FIN, m.RECEIPT_GLOB, m.INDEX, m.RUNWAY, m.IMAP, m.REPORT, m.SURFACES,
         m.NOTIFY) = old
    return restore


# --- main()-level zero-scan guard (:289, :297) ------------------------------

def test_zero_scan_exits_75_exactly():
    """Nothing at all present (no receipts dir, no surface files) -> exit 75.
    Kills :297 int 75->76 (asserts the exact literal), and half of :289's
    if-test mutants (forced-False would proceed to a bogus CLEAN/exit 0)."""
    with tempfile.TemporaryDirectory() as td:
        restore = _patch_vault(Path(td) / "FIN")  # not even created
        try:
            rc = m.main([])
        finally:
            restore()
        assert rc == 75, f"zero-scan must exit exactly 75 (EX_TEMPFAIL), got {rc}"


def test_surfaces_missing_receipts_present_still_75():
    """Receipts present, but 0/3 surface files exist -> still SKIP (75), not a
    bogus CLEAN. Kills the :289 flip-bool 'or'->'and' mutant: with 'and' this
    asymmetric case would fall THROUGH the guard (only one side is empty)."""
    with tempfile.TemporaryDirectory() as td:
        fin = Path(td) / "FIN"
        (fin / "Production_Kits").mkdir(parents=True)
        (fin / "Production_Kits" / "Video_04_youtube_upload.json").write_text(
            json.dumps({"video": "Video_04", "video_id": "abcDEF12345"}), encoding="utf-8")
        restore = _patch_vault(fin)
        try:
            rc = m.main([])
        finally:
            restore()
        assert rc == 75, f"0/3 surfaces present (receipts present) must SKIP (75), got {rc}"


def test_receipts_missing_surfaces_present_still_75():
    """The other asymmetric half: surfaces present, 0 receipts -> still 75.
    Kills the same flip-bool mutant from the opposite side."""
    with tempfile.TemporaryDirectory() as td:
        fin = Path(td) / "FIN"
        (fin / "Lead_Magnets").mkdir(parents=True)
        (fin / "Lead_Magnets" / "_Lead_Magnet_Index.md").write_text("no ids here\n", encoding="utf-8")
        (fin / "Production_Kits").mkdir(parents=True)  # dir exists; glob matches nothing
        restore = _patch_vault(fin)
        try:
            rc = m.main([])
        finally:
            restore()
        assert rc == 75, f"0 receipts (surfaces present) must SKIP (75), got {rc}"


def test_populated_scan_exits_0_and_reports_counts():
    """A real, fully-populated scan (all 3 surfaces + 1 receipt, everything
    matches) must exit 0 and its printed summary line must carry the scanned
    counts. Kills the :289 if-test->True mutant (forced-SKIP would wrongly
    return 75 here) and, combined with the direct summary_line calls below,
    every :251 scope-conditional mutant."""
    with tempfile.TemporaryDirectory() as td:
        fin = Path(td) / "FIN"
        (fin / "Production_Kits").mkdir(parents=True)
        (fin / "Lead_Magnets").mkdir(parents=True)
        live_id = "DY2RVnuUb64"
        (fin / "Production_Kits" / "Video_09_youtube_upload.json").write_text(
            json.dumps({"video": "Video_09", "video_id": live_id}), encoding="utf-8")
        row = "| 9 | Generational Wealth | published `%s` |\n" % live_id
        (fin / "Lead_Magnets" / "_Lead_Magnet_Index.md").write_text(row, encoding="utf-8")
        (fin / "Launch_Runway.md").write_text(row, encoding="utf-8")
        (fin / "Video_Topic_Integrity_Map.md").write_text(row, encoding="utf-8")
        restore = _patch_vault(fin)
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = m.main([])
        finally:
            restore()
        assert rc == 0, f"a clean populated scan must exit 0, got {rc}"
        out = buf.getvalue()
        assert "[scanned 3/3 surfaces, 1 receipts]" in out, out


def test_list_shaped_deleted_video_id_flags_both_dead_ids():
    """ROUND-2 fix (2026-07-28): deleted_video_id may now be a LIST (a receipt
    can accumulate >1 dead id via upload_video.write_receipt_preserving_dead_id).
    Both ids in the list must be caught as FLAGs, not silently dropped -- this
    is real production code (load_receipts), unlike the disconnected --selftest
    fixtures, so it must be exercised through main() to actually gate."""
    with tempfile.TemporaryDirectory() as td:
        fin = Path(td) / "FIN"
        (fin / "Production_Kits").mkdir(parents=True)
        (fin / "Lead_Magnets").mkdir(parents=True)
        dead1, dead2 = "ClJVIUtwsVE", "FJljsipxTkA"
        (fin / "Production_Kits" / "Video_13_youtube_upload.json").write_text(
            json.dumps({"video": "Video_13", "video_id": None,
                       # garbage entries (None, empty, whitespace) mixed in --
                       # must be skipped, not just the 2 real ids picked up.
                       "deleted_video_id": [dead1, None, "", "  ", dead2]}),
            encoding="utf-8")
        row = "| 13 | Retirement Math | published `%s` | also published `%s` |\n" % (dead1, dead2)
        (fin / "Lead_Magnets" / "_Lead_Magnet_Index.md").write_text(row, encoding="utf-8")
        (fin / "Launch_Runway.md").write_text("", encoding="utf-8")
        (fin / "Video_Topic_Integrity_Map.md").write_text("", encoding="utf-8")
        restore = _patch_vault(fin)
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = m.main(["--verbose"])
        finally:
            restore()
        assert rc == 1, f"2 dead ids asserted live (no death marker) must FLAG (rc 1), got {rc}"
        out = buf.getvalue()
        assert dead1 in out and dead2 in out, (
            "BOTH ids from the deleted_video_id LIST must be flagged, not just the "
            f"first: {out}")


# --- summary_line() scope segment (:251) — direct, fast, precise -----------

def test_summary_line_scope_present_when_both_counts_given():
    """Kills :251 if-test->False and both negate-compare mutants: forcing the
    branch off, or negating either comparison, drops the scope string here."""
    line = m.summary_line("CLEAN", 0, 0, [], n_surfaces=3, n_receipts=2)
    assert "[scanned 3/3 surfaces, 2 receipts]" in line, line


def test_summary_line_scope_absent_when_neither_given():
    """Kills :251 if-test->True: forcing the branch on with n_surfaces=None
    crashes on '%d' % None instead of silently rendering nothing."""
    line = m.summary_line("CLEAN", 0, 0, [])
    assert "[scanned" not in line, line


def test_summary_line_scope_requires_BOTH_counts_not_either():
    """The asymmetric case is the only one that distinguishes 'and' from 'or'
    (both-True and both-None give identical output either way). Kills the
    :251 flip-bool 'and'->'or' mutant, which crashes here on the unset side."""
    assert "[scanned" not in m.summary_line("CLEAN", 0, 0, [], n_surfaces=3, n_receipts=None)
    assert "[scanned" not in m.summary_line("CLEAN", 0, 0, [], n_surfaces=None, n_receipts=2)


def test_internal_selftest_fixtures_pass():
    """`selftest()`'s own fixtures (incl. fixture 10, the LIST-shaped
    deleted_video_id regression) carried ZERO suite coverage until now --
    --selftest is a manual-only CLI flag, never invoked by any wired test file
    (round-2 audit finding, 2026-07-28). m.selftest() is fully hermetic (calls
    run_check() directly, never main(), so no notify()/live-vault side
    effect -- confirmed by reading the function body)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = m.selftest()
    assert rc == 0, buf.getvalue()
    assert "PASS" in buf.getvalue(), buf.getvalue()


if __name__ == "__main__":
    test_zero_scan_exits_75_exactly()
    test_surfaces_missing_receipts_present_still_75()
    test_receipts_missing_surfaces_present_still_75()
    test_populated_scan_exits_0_and_reports_counts()
    test_list_shaped_deleted_video_id_flags_both_dead_ids()
    test_summary_line_scope_present_when_both_counts_given()
    test_summary_line_scope_absent_when_neither_given()
    test_summary_line_scope_requires_BOTH_counts_not_either()
    test_internal_selftest_fixtures_pass()
    print("test_receipt_surface_id_lint: 9/9 pass")
