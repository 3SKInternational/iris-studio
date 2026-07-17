#!/usr/bin/env python3
"""A-44 regression guard: the 4_vo_record auto-promote gate and the stage-6
assemble --vo-source must BOTH point at the automated-render VO dir
(Voice_Files/Video_NN_gen). If only one is changed, the gate auto-promotes on a
render the assemble step can't find -> false-green -> broken assemble.

Run under the iris_studio venv:  .venv/bin/python3 scripts/test_vo_gate_convention.py
"""
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pipeline_orchestrator as po  # noqa: E402

# 1) the gate targets the _gen render dir (where generate_vo / build_video --vo write)
gate = po.GATE_ARTIFACTS["4_vo_record"]["path_tmpl"]
assert gate.endswith("_gen"), f"vo_record gate must target _gen, got: {gate}"

# 2) the stage-6 assemble command passes --vo-source ..._gen (same dir the gate proves).
#    Guard the exact coupling: the _gen form present, the bare non-gen form absent.
src = inspect.getsource(po)
assert '"--vo-source", f"Voice_Files/{vid}_gen"' in src, \
    "stage-6 assemble --vo-source must be Voice_Files/{vid}_gen (coupled to the gate)"
assert '"--vo-source", f"Voice_Files/{vid}",' not in src, \
    "found a non-gen --vo-source Voice_Files/{vid} — would false-green the vo_record gate"

print("OK: vo_record gate and stage-6 --vo-source both target Voice_Files/Video_NN_gen")
