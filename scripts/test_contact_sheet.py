#!/usr/bin/env python3
"""Tests for contact_sheet.py's _render_count_label (no PIL needed — pure logic).

THE DEFECT (2026-07-28 two-pass codebase audit). shot_order() filters to shots
present on disk, so 'present' and 'len(order)' were equal BY CONSTRUCTION and
the header's old 'N/M rendered' phrasing was tautologically N/N — a batch where
8 of 40 shots failed to render still read as a clean '32/32 rendered'. Fixed by
reporting a render COUNT with no implied fraction, since the only candidate
denominator (the manifest) is stale by design (V6's hd manifest still lists 42
pre-consolidation shots while only 30 render dirs exist) and reading a total
from it would silently reintroduce phantom "missing" counts.

Run: python3 scripts/test_contact_sheet.py   (exit 0 = pass)
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "contact_sheet", Path(__file__).with_name("contact_sheet.py")
)
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)


def test_singular_is_grammatically_correct():
    assert m._render_count_label(1) == "1 render shown (present-on-disk only)", \
        m._render_count_label(1)


def test_plural_count():
    assert m._render_count_label(8) == "8 renders shown (present-on-disk only)", \
        m._render_count_label(8)


def test_zero_count():
    assert m._render_count_label(0) == "0 renders shown (present-on-disk only)", \
        m._render_count_label(0)


def test_label_never_implies_a_fraction():
    """THE regression this exists to catch: a bare 'N/M' phrasing always reads
    N==M by construction here and silently claims a completeness check the
    script cannot perform."""
    for n in (0, 1, 8, 32, 42):
        label = m._render_count_label(n)
        assert "/" not in label, f"label must not imply a fraction: {label!r}"
        assert "rendered" not in label, \
            f"must not use the old completeness-check wording: {label!r}"


if __name__ == "__main__":
    test_singular_is_grammatically_correct()
    test_plural_count()
    test_zero_count()
    test_label_never_implies_a_fraction()
    print("test_contact_sheet: 4/4 pass")
