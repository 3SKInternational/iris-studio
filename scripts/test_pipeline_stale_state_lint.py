#!/usr/bin/env python3
"""Tests for pipeline_stale_state_lint.py's main()-level zero-scan guard.

Same family gap as the other three house linters' new test files: --selftest
exercises lint_doc()/collect()/is_published() directly (and does — see its own
"is_published: pin the video_id-non-empty contract" block) but never main(), so
main()'s own zero-scan guard and the literal 75 carried zero suite coverage.
This file has a SIMPLER guard than its siblings (`if not _pipelines:`, no
`and`/`or`/negate-compare scope conditional — its scanned-count line is a plain
f-string with no optional-args gate) so there are fewer mutable sites, but the
two that exist (if-test True/False, int 75->76) are just as uncovered.

Run: python3 scripts/test_pipeline_stale_state_lint.py   (exit 0 = pass)
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
    "pipeline_stale_state_lint", Path(__file__).with_name("pipeline_stale_state_lint.py")
)
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)


def test_zero_scan_exits_75_exactly():
    """No Production_Kits dir at all -> exit 75 exactly. Kills the int 75->76
    mutant and the if-test->False mutant (forced-False falls through to
    collect(), whose kits.is_dir() guard also returns [] -> a bogus CLEAN/0).

    Also asserts stdout is EMPTY: without --json the zero-scan message goes to
    STDERR only (see test_zero_scan_json_output_is_exact for the --json case).
    Kills the :370 args.json if-test->True mutant, which would emit the JSON
    payload to stdout here even though --json was never passed."""
    with tempfile.TemporaryDirectory() as td:
        old = os.environ.get("SK_VAULT")
        os.environ["SK_VAULT"] = td  # empty dir, no Production_Kits subdir
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = m.main([])
        finally:
            if old is None:
                os.environ.pop("SK_VAULT", None)
            else:
                os.environ["SK_VAULT"] = old
        assert rc == 75, f"zero-scan must exit exactly 75 (EX_TEMPFAIL), got {rc}"
        assert buf.getvalue() == "", \
            f"non-json zero-scan must print nothing to stdout, got: {buf.getvalue()!r}"


def test_glob_matches_nothing_still_75():
    """Production_Kits dir exists but has 0 Video_*_pipeline.json files."""
    with tempfile.TemporaryDirectory() as td:
        (Path(td) / "Production_Kits").mkdir(parents=True)
        old = os.environ.get("SK_VAULT")
        os.environ["SK_VAULT"] = td
        try:
            rc = m.main([])
        finally:
            if old is None:
                os.environ.pop("SK_VAULT", None)
            else:
                os.environ["SK_VAULT"] = old
        assert rc == 75, f"empty glob must SKIP (75), got {rc}"


def test_zero_scan_json_output_is_exact():
    """--json on the zero-scan path must print an exact, machine-parseable
    payload — not just 'some stderr text'. Kills the :370 args.json if-test
    mutants (forced-False sends nothing to stdout; forced-True is already
    caught by the human-output tests above going silently JSON), the :372
    count 0->1 mutant, and the :373 indent 2->3 mutant (no consumer parses
    this today, but an exact-string pin still catches a format regression
    rather than declaring indent 'equivalent' without checking)."""
    with tempfile.TemporaryDirectory() as td:
        old = os.environ.get("SK_VAULT")
        os.environ["SK_VAULT"] = td  # empty dir, no Production_Kits subdir
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = m.main(["--json"])
            kits_dir = m.vault() / m.KITS_SUBDIR
        finally:
            if old is None:
                os.environ.pop("SK_VAULT", None)
            else:
                os.environ["SK_VAULT"] = old
        assert rc == 75, rc
        expected = json.dumps(
            {"status": "skip", "reason": "kits dir missing",
             "kits_dir": str(kits_dir), "count": 0, "findings": []},
            indent=2,
        ) + "\n"
        assert buf.getvalue() == expected, buf.getvalue()


def test_populated_scan_exits_0_and_reports_count():
    """One clean pipeline file -> exit 0, and the CLEAN line must carry the
    scanned count. Kills the if-test->True mutant (forced-SKIP would wrongly
    return 75 here)."""
    with tempfile.TemporaryDirectory() as td:
        kits = Path(td) / "Production_Kits"
        kits.mkdir(parents=True)
        (kits / "Video_05_pipeline.json").write_text(
            json.dumps({"video": 5, "stages": {}}), encoding="utf-8")
        old = os.environ.get("SK_VAULT")
        os.environ["SK_VAULT"] = td
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = m.main([])
        finally:
            if old is None:
                os.environ.pop("SK_VAULT", None)
            else:
                os.environ["SK_VAULT"] = old
        assert rc == 0, f"a clean populated scan must exit 0, got {rc}"
        assert "scanned 1 pipeline file(s)" in buf.getvalue(), buf.getvalue()


if __name__ == "__main__":
    test_zero_scan_exits_75_exactly()
    test_glob_matches_nothing_still_75()
    test_zero_scan_json_output_is_exact()
    test_populated_scan_exits_0_and_reports_count()
    print("test_pipeline_stale_state_lint: 4/4 pass")
