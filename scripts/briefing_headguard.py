#!/usr/bin/env python3
"""Head-guard for DAILY_BRIEFING.md.

Strips any model preamble the generation may leak ABOVE the '# Daily Briefing'
header before the file is written, and provides a writer-agnostic corruption
check for the 05:00 pre-brief sweep.

The leak class this closes (2026-07-23 + 2026-07-24, two shapes / one writer):
  - an '<analysis>/<summary>' transcript-summary blob prepended above the file
  - a conversational lead-in fused onto the header on ONE line, e.g.
    "I'll generate both outputs. ...briefing.# Daily Briefing — Thursday, ..."
This file feeds the 06:00 Telegram brief + is a session-start canonical, so a
corrupt head degrades every downstream consumer silently.

Pure + stdlib-only, no daemon deps → imported by iris.py's briefing write path
(Task 1) and callable as a deterministic CLI by the pre-brief prompt (Task 2),
and unit-tested in tests/test_briefing_headguard.py.
"""
from __future__ import annotations

import re
import sys

BRIEFING_HEADER = "# Daily Briefing"


def strip_briefing_preamble(content: str) -> str | None:
    """Return `content` sliced to start at the first '# Daily Briefing' header,
    discarding any leaked preamble above it.

    Returns None if no header is present at all — the caller MUST then preserve
    the existing file rather than clobber it with headerless output.

    Prefers a line-start header (the canonical shape). Falls back to the first
    substring occurrence to catch the fused-onto-one-line leak
    ("...briefing.# Daily Briefing — ...") where no newline precedes the header.
    """
    if not content:
        return None
    m = re.search(r"^# Daily Briefing", content, re.MULTILINE)
    if m:
        return content[m.start():]
    idx = content.find(BRIEFING_HEADER)
    if idx == -1:
        return None
    return content[idx:]


def has_valid_head(content: str) -> bool:
    """True iff the FIRST non-blank line is a '# Daily Briefing' header.

    This is the writer-agnostic corruption check (Task 2 / the pre-brief lint):
    stricter than strip_briefing_preamble, because a leaked preamble line makes
    this False even though strip could still recover the body below it. A file
    that passes this needs no repair; one that fails is BRIEFING_HEAD_CORRUPT.
    """
    for line in content.splitlines():
        if line.strip() == "":
            continue
        return line.startswith(BRIEFING_HEADER)
    return False  # empty / whitespace-only file


def _first_nonblank(content: str) -> str:
    for line in content.splitlines():
        if line.strip():
            return line
    return "<empty file>"


def _cli(argv: list[str]) -> int:
    """--check PATH → exit 0 if the file's first content is a valid header,
    else print BRIEFING_HEAD_CORRUPT + the offending first line and exit 1."""
    if len(argv) != 2 or argv[0] != "--check":
        print("usage: briefing_headguard.py --check PATH", file=sys.stderr)
        return 2
    try:
        with open(argv[1], encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError as exc:
        print(f"BRIEFING_HEAD_CORRUPT: cannot read {argv[1]}: {exc}")
        return 1
    if has_valid_head(content):
        return 0
    print(
        "BRIEFING_HEAD_CORRUPT: first line is not "
        f"'{BRIEFING_HEADER}' -> {_first_nonblank(content)[:120]!r}"
    )
    return 1


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
