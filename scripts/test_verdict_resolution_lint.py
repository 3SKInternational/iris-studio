#!/usr/bin/env python3
"""Tests for verdict_resolution_lint.py's CLI-level guards.

Same family, same gap as test_receipt_surface_id_lint.py /
test_pipeline_stage_truth_lint.py: this linter's --selftest exercises
classify_path()/run_check() directly but never main(), so main()'s zero-scan
guard, the scanned-count "scope" segment in summary_line, and the literal 75
itself carried zero suite coverage. `scripts/precheck.sh --mutate` only found
this on the two files it was pointed at (2026-07-28); this one is "equally
uncovered" per the same-day audit follow-up, since the guard/scope shape here
is byte-for-byte the same pattern.

Run: python3 scripts/test_verdict_resolution_lint.py   (exit 0 = pass)
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
    "verdict_resolution_lint", Path(__file__).with_name("verdict_resolution_lint.py")
)
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)


def _patch_vault(fin_dir: Path):
    old = (m.FIN, m.RECEIPT_GLOB, m.REPORT, m.NOTIFY)
    # NOTIFY MUST be redirected too: notify() only skips when the target is not
    # executable, and scripts/notify.sh IS executable — so a flagging fixture driven
    # through main() sends a REAL Telegram alert to Steve. The sibling suite for
    # receipt_surface_id_lint did exactly that on 2026-07-28, hundreds of times,
    # because the mutation gate runs every suite once PER MUTANT. A test whose side
    # effect escapes the test is not hermetic.
    # See feedback_force_stub_side_effect_modules.
    m.NOTIFY = os.path.join(str(fin_dir), "_no_notify_in_tests.sh")

    fin = str(fin_dir)
    m.FIN = fin
    m.RECEIPT_GLOB = str(Path(fin) / "Production_Kits" / "*_youtube_upload.json")
    m.REPORT = str(Path(fin) / "Raw_Assets" / "_verdict_resolution_report.md")

    def restore():
        (m.FIN, m.RECEIPT_GLOB, m.REPORT, m.NOTIFY) = old
    return restore


# --- main()-level zero-scan guard --------------------------------------------

def test_zero_scan_exits_75_exactly():
    """Nothing present at all -> exit 75 exactly. Kills the int 75->76 mutant
    and half of the guard's if-test mutants (forced-False falls through to a
    bogus CLEAN/exit 0)."""
    with tempfile.TemporaryDirectory() as td:
        restore = _patch_vault(Path(td) / "FIN")  # not even created
        try:
            rc = m.main([])
        finally:
            restore()
        assert rc == 75, f"zero-scan must exit exactly 75 (EX_TEMPFAIL), got {rc}"


def test_review_files_missing_receipts_present_still_75():
    """Receipts present, but every REVIEW_DIRS dir is empty/missing -> still
    75, not CLEAN. Kills the guard's flip-bool 'or'->'and' mutant: with 'and'
    this asymmetric case falls through the guard."""
    with tempfile.TemporaryDirectory() as td:
        fin = Path(td) / "FIN"
        (fin / "Production_Kits").mkdir(parents=True)
        (fin / "Production_Kits" / "Video_11_youtube_upload.json").write_text(
            json.dumps({"video": "Video_11", "video_id": "abcDEF12345"}), encoding="utf-8")
        restore = _patch_vault(fin)
        try:
            rc = m.main([])
        finally:
            restore()
        assert rc == 75, f"0 review files (receipts present) must SKIP (75), got {rc}"


def test_receipts_missing_review_files_present_still_75():
    """The other asymmetric half: a review file exists, but 0 receipts."""
    with tempfile.TemporaryDirectory() as td:
        fin = Path(td) / "FIN"
        rdir = fin / m.REVIEW_DIRS[0]
        rdir.mkdir(parents=True)
        (rdir / "Video_11_Script_Review.md").write_text(
            "---\nvideo: Video_11\n---\n\n**VERDICT: SHIP**\n", encoding="utf-8")
        (fin / "Production_Kits").mkdir(parents=True)  # dir exists, glob matches nothing
        restore = _patch_vault(fin)
        try:
            rc = m.main([])
        finally:
            restore()
        assert rc == 75, f"0 receipts (review files present) must SKIP (75), got {rc}"


def test_populated_scan_exits_0_and_reports_counts():
    """A real, fully-populated scan (1 receipt, 1 clean-SHIP review) must exit
    0 and its printed CLEAN line must carry the scanned counts."""
    with tempfile.TemporaryDirectory() as td:
        fin = Path(td) / "FIN"
        (fin / "Production_Kits").mkdir(parents=True)
        (fin / "Production_Kits" / "Video_11_youtube_upload.json").write_text(
            json.dumps({"video": "Video_11", "video_id": "abcDEF12345"}), encoding="utf-8")
        rdir = fin / m.REVIEW_DIRS[0]
        rdir.mkdir(parents=True)
        (rdir / "Video_11_Script_Review.md").write_text(
            "---\nvideo: Video_11\n---\n\n**VERDICT: SHIP**\n", encoding="utf-8")
        restore = _patch_vault(fin)
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = m.main([])
        finally:
            restore()
        assert rc == 0, f"a clean populated scan must exit 0, got {rc}"
        out = buf.getvalue()
        assert "[scanned 1 review files / 1 receipts]" in out, out


# --- summary_line() scope segment -------------------------------------------

def test_summary_line_scope_present_when_both_counts_given():
    line = m.summary_line("CLEAN", 0, [], n_reviews=5, n_receipts=3)
    assert "[scanned 5 review files / 3 receipts]" in line, line


def test_summary_line_scope_absent_when_neither_given():
    """Kills if-test->True: forcing the branch on with n_reviews=None crashes
    on '%d' % None instead of silently rendering nothing."""
    line = m.summary_line("CLEAN", 0, [])
    assert "[scanned" not in line, line


def test_summary_line_scope_requires_BOTH_counts_not_either():
    assert "[scanned" not in m.summary_line("CLEAN", 0, [], n_reviews=5, n_receipts=None)
    assert "[scanned" not in m.summary_line("CLEAN", 0, [], n_reviews=None, n_receipts=3)


def test_internal_selftest_fixtures_pass():
    """`selftest()`'s own fixtures (the 17 check()/check_group() cases, incl.
    HIGH-2's nested-resolution regression) carried ZERO suite coverage until
    now -- --selftest is a manual-only CLI flag, never invoked by any wired
    test file (round-2 audit finding, 2026-07-28). Wired here rather than left
    as an ambiguous mutation survivor: m.selftest() is fully hermetic (uses
    _tmp() + calls run_check()/classify_path() directly, never main(), so no
    live vault path or notify() side effect -- confirmed by reading the
    function body, same reasoning _patch_vault's docstring above already
    established for main()'s side effects)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = m.selftest()
    assert rc == 0, buf.getvalue()
    assert "OK" in buf.getvalue(), buf.getvalue()


if __name__ == "__main__":
    test_zero_scan_exits_75_exactly()
    test_review_files_missing_receipts_present_still_75()
    test_receipts_missing_review_files_present_still_75()
    test_populated_scan_exits_0_and_reports_counts()
    test_summary_line_scope_present_when_both_counts_given()
    test_summary_line_scope_absent_when_neither_given()
    test_summary_line_scope_requires_BOTH_counts_not_either()
    test_internal_selftest_fixtures_pass()
    print("test_verdict_resolution_lint: 8/8 pass")
