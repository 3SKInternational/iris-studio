#!/usr/bin/env python3
"""agent_output_lint.check_banned_vocab: a two-word banned phrase split by a
line wrap or a double space must still be flagged (A-84, 2026-10-13).

Run: python3 tests/test_agent_output_lint.py
"""
import importlib.util
import pathlib
import sys

_P = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "agent_output_lint.py"
_spec = importlib.util.spec_from_file_location("agent_output_lint", _P)
aol = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(aol)

FAILS = []


def check(name, cond):
    print(f"  [{'ok ' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


def words(text):
    return [f["word"] for f in aol.check_banned_vocab(text)]


check("one space flagged", "dramatic lighting" in words("Flat room, dramatic lighting."))
check("hard wrap flagged", "dramatic lighting" in words("Flat room, dramatic\nlighting."))
check("double space flagged", "soft airbrushing" in words("Flat room, soft  airbrushing."))
check("negated wrap not flagged", "dramatic lighting" not in words("Flat room, no dramatic\nlighting."))

if FAILS:
    print(f"test_agent_output_lint: FAIL ({len(FAILS)})")
    sys.exit(1)
print("test_agent_output_lint: PASS")
