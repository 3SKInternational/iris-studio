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

def _banned(prompt):
    return [f for f in msl.lint({"images": [{"name": "s", "prompt": prompt}]}, "")
            if f[1] == "banned-vocab"]


check("'dramatic lighting' is banned", bool(_banned("Flat room, dramatic lighting.")))
check("bare 'dramatic' is not (K3, 2026-10-12)", not _banned("A dramatic shadow falls."))
check("'BANNED' holds the phrase, not the bare word",
      "dramatic lighting" in msl.BANNED and "dramatic" not in msl.BANNED)
for term in ("dramatic lighting", "soft lighting"):
    a, b = term.split()
    check(f"'{a}\\n{b}' hard wrap is banned (A-84)", bool(_banned(f"Flat room, {a}\n{b}.")))
    check(f"'{a}  {b}' double space is banned (A-84)", bool(_banned(f"Flat room, {a}  {b}.")))
check("negated hard-wrapped phrase still passes",
      not _banned("Flat room, no dramatic\nlighting."))

if FAILS:
    print(f"test_manifest_spine_lint_card: FAIL ({len(FAILS)})")
    sys.exit(1)
print("test_manifest_spine_lint_card: PASS")
