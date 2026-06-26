#!/usr/bin/env python3
"""Self-check for the cost-ledger / re-render tracking in generate_images.py.

Runs offline (no API, no manifest). `python3 test_cost_ledger.py` → exits 0 on pass.
"""
import json
import tempfile
from pathlib import Path

from generate_images import (
    video_label,
    load_render_counts,
    ledger_append,
)


def test_video_label():
    assert video_label("Video_05_orchestrated.json") == "Video_05"
    assert video_label("Video_06_fix_07g.json") == "Video_06"
    assert video_label("oddball.json") == "oddball"  # falls back to stem


def test_append_and_rerender_counts():
    with tempfile.TemporaryDirectory() as d:
        led = Path(d) / "cost_ledger.jsonl"
        # First render of two distinct shots.
        ledger_append(led, {"video": "Video_05", "shot": "Shot_14a", "cost_usd": 0.12, "rerender": False})
        ledger_append(led, {"video": "Video_05", "shot": "Shot_21a", "cost_usd": 0.13, "rerender": False})
        # A different video must not bleed into V5's counts.
        ledger_append(led, {"video": "Video_06", "shot": "Shot_14a", "cost_usd": 0.10, "rerender": False})

        counts = load_render_counts(led, "Video_05")
        assert counts == {"Shot_14a": 1, "Shot_21a": 1}, counts

        # Re-render Shot_14a → it should now read as a repeat for V5.
        ledger_append(led, {"video": "Video_05", "shot": "Shot_14a", "cost_usd": 0.12, "rerender": True})
        counts = load_render_counts(led, "Video_05")
        assert counts["Shot_14a"] == 2, counts

        # Every line is valid JSON and fsync'd (file is non-empty).
        lines = [l for l in led.read_text().splitlines() if l.strip()]
        assert len(lines) == 4
        for l in lines:
            json.loads(l)


def test_saved_field_roundtrips():
    with tempfile.TemporaryDirectory() as d:
        led = Path(d) / "cost_ledger.jsonl"
        # A billed-but-unsaved row (save failed after the API bill) must persist.
        ledger_append(led, {"video": "Video_05", "shot": "Shot_99z", "cost_usd": 0.12,
                            "rerender": False, "saved": False})
        rec = json.loads(led.read_text().splitlines()[0])
        assert rec["saved"] is False
        # It still counts toward this video's render count (the spend happened).
        assert load_render_counts(led, "Video_05") == {"Shot_99z": 1}


def test_torn_line_tolerated():
    with tempfile.TemporaryDirectory() as d:
        led = Path(d) / "cost_ledger.jsonl"
        ledger_append(led, {"video": "Video_07", "shot": "Shot_03a", "cost_usd": 0.14, "rerender": False})
        # Simulate a crash mid-write leaving a torn final line.
        with open(led, "a") as fh:
            fh.write('{"video": "Video_07", "shot": "trunc')
        counts = load_render_counts(led, "Video_07")
        assert counts == {"Shot_03a": 1}, counts  # torn line ignored, good row survives


if __name__ == "__main__":
    test_video_label()
    test_append_and_rerender_counts()
    test_saved_field_roundtrips()
    test_torn_line_tolerated()
    print("cost-ledger self-check: PASS")
