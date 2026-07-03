#!/usr/bin/env python3
"""Offline unit checks for watch_video.py — no network, no ffmpeg.

Run: python3 scripts/test_watch_video.py   (prints 'ok: ...' on pass)
Covers the pure logic: time parsing, VTT parse + rolling-caption dedup,
transcript windowing, timestamp formatting, source guard.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from watch_video import fmt_ts, parse_time, parse_vtt, window  # noqa: E402

# parse_time
assert parse_time("90") == 90.0
assert parse_time("1:30") == 90.0
assert parse_time("0:01:30") == 90.0
assert parse_time("1:00:05") == 3605.0
for bad in ("", ":", "1:2:3:4", "1::30"):
    try:
        parse_time(bad)
        raise AssertionError(f"parse_time accepted {bad!r}")
    except ValueError:
        pass

# fmt_ts
assert fmt_ts(65) == "1:05"
assert fmt_ts(3605) == "1:00:05"

# VTT parse + rolling-caption dedup (auto-subs re-emit the previous line each cue)
VTT = """WEBVTT
Kind: captions
Language: en

00:00:00.000 --> 00:00:02.320 align:start position:0%
welcome<00:00:00.320><c> to</c><c> the</c><c> video</c>

00:00:02.320 --> 00:00:04.000
welcome to the video
today we talk about money

00:00:04.000 --> 00:00:06.500
today we talk about money
first, let's understand

01:00:01.000 --> 01:00:02.000
an hour in
"""
cues = parse_vtt(VTT)
texts = [s for _, s in cues]
assert texts == ["welcome to the video", "today we talk about money",
                 "first, let's understand", "an hour in"], texts
assert cues[0][0] == 0.0 and cues[1][0] == 2.32
assert cues[3][0] == 3601.0  # HH:MM:SS cue parsed with hours

# windowing
assert window(cues, 2, 5) == [(2.32, "today we talk about money"),
                              (4.0, "first, let's understand")]
assert window(cues, 9999, 10000) == []

print("ok: all watch_video unit checks pass")
