#!/usr/bin/env python3
"""Tests for pipeline_stage_truth_lint.py's CLI-level guards.

THE GAP (2026-07-28 pre-review gate). Same class as its sibling
test_receipt_surface_id_lint.py: --selftest exercises run_check()/scan_pipeline()
directly but never main(), so the zero-scan guard (exit 75), the scanned-count
"scope" segment in summary_line, and the literal 75 itself carried ZERO suite
coverage. `scripts/precheck.sh --mutate` found 8 survivors, all on the lines the
2026-07-28 zero-scan fix touched (:228 scope conditional, :264 the guard itself,
:268 the 75 literal).

Run: python3 scripts/test_pipeline_stage_truth_lint.py   (exit 0 = pass)
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
    "pipeline_stage_truth_lint", Path(__file__).with_name("pipeline_stage_truth_lint.py")
)
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)


def _patch_vault(fin_dir: Path):
    old = (m.FIN, m.PIPELINE_GLOB, m.RECEIPT_TMPL, m.REPORT, m.NOTIFY)
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
    m.PIPELINE_GLOB = str(Path(fin) / "Production_Kits" / "Video_*_pipeline.json")
    m.RECEIPT_TMPL = str(Path(fin) / "Production_Kits" / "Video_{nn}_youtube_upload.json")
    m.REPORT = str(Path(fin) / "Raw_Assets" / "_pipeline_stage_truth_report.md")

    def restore():
        (m.FIN, m.PIPELINE_GLOB, m.RECEIPT_TMPL, m.REPORT, m.NOTIFY) = old
    return restore


# --- main()-level zero-scan guard (:264, :268) ------------------------------

def test_zero_scan_exits_75_exactly():
    """No Production_Kits dir at all -> exit 75 exactly. Kills :268 int 75->76
    and the :264 if-test->False mutant (forced-False would fall through to a
    bogus CLEAN/exit 0 with n_inflight=0)."""
    with tempfile.TemporaryDirectory() as td:
        restore = _patch_vault(Path(td) / "FIN")  # not even created
        try:
            rc = m.main([])
        finally:
            restore()
        assert rc == 75, f"zero-scan must exit exactly 75 (EX_TEMPFAIL), got {rc}"


def test_glob_matches_nothing_still_75():
    """Dir exists but 0 Video_*_pipeline.json files -> still 75, not CLEAN."""
    with tempfile.TemporaryDirectory() as td:
        fin = Path(td) / "FIN"
        (fin / "Production_Kits").mkdir(parents=True)
        restore = _patch_vault(fin)
        try:
            rc = m.main([])
        finally:
            restore()
        assert rc == 75, f"empty glob must SKIP (75), got {rc}"


def test_populated_scan_exits_0_and_reports_counts():
    """One in-flight pipeline file with no stale-open stages -> exit 0, and the
    printed CLEAN line must carry the scanned counts. Kills the :264
    if-test->True mutant (forced-SKIP would wrongly return 75 here) and, with
    the direct summary_line calls below, every :228 scope-conditional mutant."""
    with tempfile.TemporaryDirectory() as td:
        fin = Path(td) / "FIN"
        (fin / "Production_Kits").mkdir(parents=True)
        (fin / "Production_Kits" / "Video_05_pipeline.json").write_text(
            json.dumps({"video": 5, "stages": {}}), encoding="utf-8")
        restore = _patch_vault(fin)
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = m.main([])
        finally:
            restore()
        assert rc == 0, f"a clean populated scan must exit 0, got {rc}"
        out = buf.getvalue()
        assert "[scanned 1 pipeline file(s), 1 in-flight]" in out, out


# --- summary_line() scope segment (:228) — direct, fast, precise -----------

def test_summary_line_scope_present_when_both_counts_given():
    line = m.summary_line("CLEAN", [], n_pipelines=4, n_inflight=2)
    assert "[scanned 4 pipeline file(s), 2 in-flight]" in line, line


def test_summary_line_scope_absent_when_neither_given():
    """Kills if-test->True: forcing the branch on with n_pipelines=None crashes
    on '%d' % None instead of silently rendering nothing."""
    line = m.summary_line("CLEAN", [])
    assert "[scanned" not in line, line


def test_summary_line_scope_requires_BOTH_counts_not_either():
    """The only case that distinguishes 'and' from 'or' — kills the flip-bool
    mutant, which crashes here on the unset side."""
    assert "[scanned" not in m.summary_line("CLEAN", [], n_pipelines=4, n_inflight=None)
    assert "[scanned" not in m.summary_line("CLEAN", [], n_pipelines=None, n_inflight=2)


if __name__ == "__main__":
    test_zero_scan_exits_75_exactly()
    test_glob_matches_nothing_still_75()
    test_populated_scan_exits_0_and_reports_counts()
    test_summary_line_scope_present_when_both_counts_given()
    test_summary_line_scope_absent_when_neither_given()
    test_summary_line_scope_requires_BOTH_counts_not_either()
    print("test_pipeline_stage_truth_lint: 6/6 pass")
