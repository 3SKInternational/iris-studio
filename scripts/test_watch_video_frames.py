#!/usr/bin/env python3
"""Regression guard: watch_video.py frame labels == true CONTENT time.

Separate from test_watch_video.py (deliberately ffmpeg-free) because this needs
ffmpeg. It renders a synthetic clip, samples it through the REAL extract_frames(),
and ground-truths each frame's content time by CHECKSUM CORRELATION — decode the clip
once with `showinfo` (no fps) to map every source frame's checksum -> its true decode
time, then look up the checksums of the frames the `fps=1/interval` filter actually
emits. The label formula never enters the ground truth, so this catches the ~interval/2
off-by-half bug (a fps output frame carries the pixels of the source frame nearest the
slot CENTER, so content = start + (i+0.5)*interval, not start + i*interval).

Run: <repo>/.venv/bin/python scripts/test_watch_video_frames.py
Skips cleanly (exit 0) if ffmpeg is missing.
"""
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from watch_video import extract_frames  # noqa: E402

if not shutil.which("ffmpeg"):
    print("skip: ffmpeg not on PATH — frame-label ground-truth check not run")
    sys.exit(0)

# showinfo emits e.g. "... pts_time:6.0 ... checksum:1A2B3C4D ..." per frame.
INFO_RE = re.compile(r"pts_time:([0-9.]+).*?checksum:([0-9A-Fa-f]+)")


def showinfo(vfilter: str, clip: Path, extra: list[str], n: int | None) -> list[tuple[float, str]]:
    """[(pts_time, checksum)] for every frame ffmpeg outputs through `vfilter`."""
    cmd = ["ffmpeg", "-hide_banner", *extra, "-i", str(clip), "-vf", vfilter]
    if n is not None:
        cmd += ["-frames:v", str(n)]
    cmd += ["-f", "null", "-"]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, f"ffmpeg failed ({p.returncode}): {p.stderr[-300:]}"
    return [(float(m.group(1)), m.group(2).upper()) for m in INFO_RE.finditer(p.stderr)]


with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    clip = tmp / "tv.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", "testsrc=duration=120:size=160x120:rate=25", str(clip)],
        check=True, timeout=120)

    # checksum -> true decode time of every SOURCE frame (unique for testsrc's moving pattern)
    src = {ck: t for t, ck in showinfo("showinfo", clip, [], None)}
    assert len(src) > 100, "source checksum map too small — testsrc frames not unique?"

    # whole-clip integer interval, whole-clip non-integer interval, mid-clip -ss/-to window
    for start, end, n in [(0.0, 60.0, 10), (0.0, 120.0, 7), (30.0, 90.0, 8)]:
        interval = max((end - start) / n, 0.5)
        labels = [t for t, _ in extract_frames(clip, tmp / f"f_{start}_{n}", start, end, n, 128)]
        # true content time of each emitted frame, via the same filter but checksum-keyed
        seek = ["-ss", str(start), "-to", str(end)]
        emitted = showinfo(f"fps=1/{interval},showinfo", clip, seek, n)
        truth = [src[ck] for _, ck in emitted]  # absolute content time (src map decoded without -ss)
        assert len(labels) == len(truth) == n, (start, end, n, len(labels), len(truth))
        for lab, real in zip(labels, truth):
            # frames land on their label within one source-frame; the old start+i*interval
            # formula was off by ~interval/2 (>= 3s here) and would blow this tolerance.
            assert abs(lab - real) < 0.2, \
                f"label {lab} vs true content-time {real} (interval {interval}) — off-by-half bug"

print("ok: watch_video frame labels match true content time (interval-center, no off-by-half)")
