#!/usr/bin/env python3
"""channel_status.py — one-glance 3SK Finance channel state for the boot sequence.

Reads the LATEST analytics api-pull (Channel_Intelligence/Analytics/*_api-pull.md)
and the per-video publish receipts (Production_Kits/Video_NN_youtube_upload.json),
then prints a compact summary: subscribers, the per-video metrics table, traffic
sources, and which videos are actually live. Read-only; stdlib only.

  python scripts/channel_status.py            # print the summary
  python scripts/channel_status.py --selftest # parser self-check (no vault needed)
"""
from __future__ import annotations

import glob
import json
import re
import sys
from pathlib import Path

VAULT = Path("/Users/steve/Documents/3SK/outputs/BRANDS/3SK_Finance")
ANALYTICS_DIR = VAULT / "Channel_Intelligence" / "Analytics"
KITS_DIR = VAULT / "Production_Kits"


def latest_api_pull(analytics_dir: Path) -> Path | None:
    """Newest *_api-pull.md by the YYYY-MM-DD date in its filename (lexical max
    works for ISO dates). None if none exist."""
    files = sorted(analytics_dir.glob("*_api-pull.md"))
    return files[-1] if files else None


def frontmatter(text: str) -> dict[str, str]:
    """Flat key: value scan of the leading --- ... --- block. Good enough for the
    scalar fields we surface (date, subscribers, channel_videos); ignores nesting."""
    out: dict[str, str] = {}
    m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    if not m:
        return out
    for line in m.group(1).splitlines():
        km = re.match(r"^([A-Za-z_][\w-]*):\s*(.*?)\s*$", line)
        if km and km.group(2):
            out[km.group(1)] = km.group(2)
    return out


def section(text: str, heading: str) -> str:
    """The block from a '## heading' line up to (not incl.) the next '## ' line.
    Empty string if the heading is absent."""
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines)
                  if ln.strip().lower() == f"## {heading}".lower()), None)
    if start is None:
        return ""
    end = next((j for j in range(start + 1, len(lines))
                if lines[j].strip().startswith("## ")), len(lines))
    return "\n".join(lines[start:end]).strip()


def live_videos(kits_dir: Path) -> list[str]:
    """One line per publish receipt: 'Video_NN  url  (publish_at, via)'."""
    rows = []
    for p in sorted(kits_dir.glob("Video_*_youtube_upload.json")):
        try:
            d = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            rows.append(f"  {p.name}  (unreadable)")
            continue
        when = (d.get("publish_at") or d.get("last_published_at")
                or d.get("uploaded_at") or "?")
        rows.append(
            f"  {d.get('video', p.stem):10s} {d.get('url', 'no-url')}"
            f"  ({when}, {d.get('published_via', '?')})"
        )
    return rows


def render() -> str:
    out = ["# 3SK Finance — channel status\n"]
    pull = latest_api_pull(ANALYTICS_DIR)
    if pull is None:
        out.append("No api-pull found in " + str(ANALYTICS_DIR))
    else:
        text = pull.read_text()
        fm = frontmatter(text)
        out.append(f"Latest analytics pull: {pull.name}")
        out.append(
            f"  subscribers: {fm.get('subscribers', '?')}   "
            f"videos: {fm.get('channel_videos', '?')}   "
            f"window: {fm.get('window', '?')}\n"
        )
        for h in ("Per-video metrics", "Channel traffic sources (by views)"):
            blk = section(text, h)
            if blk:
                out.append(blk + "\n")
    live = live_videos(KITS_DIR)
    out.append("## Published (upload receipts)")
    out.extend(live or ["  (none)"])
    return "\n".join(out)


def _selftest() -> int:
    sample = (
        "---\ndate: 2026-06-24\nsubscribers: 4\nchannel_videos: 4\n"
        "window: a..b\n---\n\n# T\n\n## Per-video metrics\n\n| x |\n| 1 |\n\n"
        "## Channel traffic sources (by views)\n\n| s | v |\n\n## Next\nzzz\n"
    )
    fm = frontmatter(sample)
    assert fm["subscribers"] == "4" and fm["date"] == "2026-06-24", fm
    pvm = section(sample, "Per-video metrics")
    assert "| 1 |" in pvm and "Next" not in pvm and "traffic" not in pvm.lower(), pvm
    assert section(sample, "Nope") == ""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        for name in ("2026-06-19_api-pull.md", "2026-06-24_api-pull.md", "notes.md"):
            Path(td, name).write_text("x")
        assert latest_api_pull(Path(td)).name == "2026-06-24_api-pull.md"
        empty = Path(td, "empty")
        empty.mkdir()
        assert latest_api_pull(empty) is None
    print("selftest ok")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv[1:]:
        sys.exit(_selftest())
    print(render())
