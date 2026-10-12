#!/usr/bin/env python3
"""manifest_spine_lint._is_card: a data card skips the text-bait and speech-act
checks, so a SCENE carrying the silhouette suppressor (it also says "NO PEOPLE")
must not be mistaken for a card (2026-10-12).

Run: python3 tests/test_manifest_spine_lint_card.py
"""
import importlib.util
import pathlib
import sys

_P = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "manifest_spine_lint.py"
_spec = importlib.util.spec_from_file_location("manifest_spine_lint", _P)
msl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(msl)

_spec2 = importlib.util.spec_from_file_location(
    "pipeline_orchestrator", _P.parent / "pipeline_orchestrator.py")
po = importlib.util.module_from_spec(_spec2)
_spec2.loader.exec_module(po)

FAILS = []


def check(name, cond):
    print(f"  [{'ok ' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


check("strict-phrase card is a card",
      msl._is_card("No character. Flat card. " + po.NO_PEOPLE_SUPPRESSOR))
check("'No character' card is a card", msl._is_card("No character. Flat bar chart."))
check("silhouette scene is NOT a card",
      not msl._is_card("A single figure across a desk. " + po.SILHOUETTE_SUPPRESSOR))
check("plain Three scene is not a card", not msl._is_card("Three at a desk, smiling."))

if FAILS:
    print(f"test_manifest_spine_lint_card: FAIL ({len(FAILS)})")
    sys.exit(1)
print("test_manifest_spine_lint_card: PASS")
