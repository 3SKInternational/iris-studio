#!/usr/bin/env python3
"""Pre-render VO number-hazard linter — catch numbers ElevenLabs mis-speaks.

The bug this exists for: Video_05's VO kit said "$1,043,000" and ElevenLabs read
it aloud as "one thousand forty-three thousand" (it dropped the millions place).
Round magnitudes on the SAME lines rendered fine ($1,000,000, $100,000, $40,000).
The engine fumbles place value on *non-round millions* — a mixed-magnitude number
at million scale. A text reviewer (the vo-reviewer agent) is supposed to catch
this, but an LLM can miss it; a deterministic scan cannot. So this is a script,
not an agent — it can't hallucinate a pass.

What it flags (high-signal only, to stay quiet enough to actually be a gate):
  * Any currency/integer >= 1,000,000 written as comma-grouped digits
    (e.g. $1,043,000, 2,500,000, $1,000,000) — ElevenLabs drops the millions
    place on ALL of them: $1,043,000 -> "one thousand forty-three thousand",
    $1,000,000 -> "one thousand thousand". Round vs non-round makes no
    difference. Thousands ($100,000, $40,000 — a single comma group) stay below
    the regex and pass; spell those out in the kit by hand if they misread too.

Each finding prints `path:line: <offending> -> say "<safe rewrite>"`. Exit code is
1 when any hazard is found, 0 when clean, so it works as a pre-render gate:
  python3 scripts/vo_number_lint.py BRANDS/.../Video_05/_VO_Session_B_Kit.md
Point it at the VO KIT (what actually renders), not the script md — the kit is
narration-only, so it won't false-flag round millions sitting in titles/chapters.

  --selftest  run the built-in asserts and exit (no files needed)

# ponytail: one proven hazard class (comma-grouped millions, round or not). Add a
# new rule only when a real render misreads a new pattern — don't pre-build a
# number grammar. If thousands ($100,000) start misreading too, widen the regex.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# A grouped number with >= 2 comma-groups is >= 1,000,000 (millions scale).
# Optional leading $; the magnitude misread happens with or without the symbol.
_GROUPED_MILLIONS = re.compile(r"\$?\d{1,3}(?:,\d{3}){2,}\b")


def _safe_rewrite(raw: str) -> str:
    """'$1,043,000' -> '$1.043 million'; keep the $ if the original had one.
    ElevenLabs reads the decimal-million form with the correct magnitude.

    The decimal-million form only has thousands resolution, so any value with
    hundreds/tens/units below the thousands place can't round-trip — emitting a
    clean '$X million' there would suggest a DIFFERENT dollar amount, which is the
    exact wrong-number bug this gate exists to prevent. In that case tag the
    suggestion so a reviewer rewrites by hand instead of pasting it blind."""
    has_dollar = raw.lstrip().startswith("$")
    value = int(raw.replace("$", "").replace(",", ""))
    num = f"{value / 1_000_000:.3f}".rstrip("0").rstrip(".")
    text = f"${num} million" if has_dollar else f"{num} million"
    if round(float(num) * 1_000_000) != value:
        text += " (approx — rewrite by hand; exact value below thousands is lost)"
    return text


def find_hazards(text: str) -> list[tuple[str, str]]:
    """Return [(offending_substring, safe_rewrite)] for each comma-grouped
    million-scale number in the line/text. ALL millions are flagged: ElevenLabs
    mis-reads non-round millions ($1,043,000 -> "one thousand forty-three
    thousand") AND round ones ($1,000,000 -> "one thousand thousand") — it drops
    the millions place either way. Sub-million numbers (single comma group, e.g.
    $100,000) are below this regex and not flagged here."""
    out: list[tuple[str, str]] = []
    for m in _GROUPED_MILLIONS.finditer(text):
        raw = m.group(0)
        out.append((raw, _safe_rewrite(raw)))
    return out


def lint_text(text: str, path: Path) -> int:
    findings = 0
    # Split on "\n" only (not str.splitlines, which also breaks on VT/FF/U+2028
    # etc.) so reported line numbers match what the editor/grep shows. rstrip "\r"
    # handles CRLF files.
    for lineno, line in enumerate(text.split("\n"), 1):
        line = line.rstrip("\r")
        for raw, fix in find_hazards(line):
            findings += 1
            print(f'{path}:{lineno}: {raw} -> say "{fix}"')
    return findings


def selftest() -> int:
    assert find_hazards("the screen says $1,043,000.") == [("$1,043,000", "$1.043 million")]
    assert find_hazards("bare 1,043,000 here") == [("1,043,000", "1.043 million")]
    assert find_hazards("$2,500,000 invested") == [("$2,500,000", "$2.5 million")]
    # whole millions now flag too — ElevenLabs drops the millions place either way:
    assert find_hazards("$1,000,000 milestone") == [("$1,000,000", "$1 million")]
    assert find_hazards("4 percent of $1,000,000 is $40,000") == [("$1,000,000", "$1 million")]
    # thousands (single comma group) stay below the regex and do NOT flag:
    assert find_hazards("$100,000 and $40,000") == []
    # exact-thousands values get a clean, round-tripping rewrite:
    assert find_hazards("$1,100,000")[0][1] == "$1.1 million"
    # sub-thousands precision can't round-trip -> tagged, never a clean wrong amount:
    raw, fix = find_hazards("$1,234,567")[0]
    assert fix.startswith("$1.235 million (approx"), fix
    assert "approx" not in find_hazards("$1,043,000")[0][1]
    print("vo_number_lint selftest: OK")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Flag TTS-hazardous numbers in VO text before render.")
    ap.add_argument("files", nargs="*", help="VO kit / script paths to scan")
    ap.add_argument("--selftest", action="store_true", help="run built-in checks and exit")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if not args.files:
        ap.error("no files given (or use --selftest)")
    total = 0
    for f in args.files:
        p = Path(f)
        if not p.is_file():
            print(f"error: not a file: {p}", file=sys.stderr)
            return 2
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            print(f"error: cannot decode {p} as UTF-8 ({exc})", file=sys.stderr)
            return 2
        total += lint_text(text, p)
    if total:
        print(f"\n{total} number hazard(s) — rewrite before the billed VO render.", file=sys.stderr)
        return 1
    print("no number hazards found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
