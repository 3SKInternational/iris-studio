#!/usr/bin/env python3
r"""Build a VO Session-B kit (markdown) from a 3SK production script.

The kit -- not the script -- is what vo_factory/generate_vo.py reads at render
time (it parses `## Scene N -> \`Video_NN_VO_Scene_MM.mp3\`` blocks). Kits used
to be hand-transcribed from the script, which silently went stale every time the
script changed (wrong voice line, missing the latest VO edits). This makes the
script the single source of truth: regenerate the kit deterministically whenever
the script changes, then render.

What it does (all deterministic -- no LLM, can't hallucinate a number):
  1. Pull every `## SCENE N [mm:ss-mm:ss] LABEL` block from the script.
  2. Extract that scene's `**VO:**` narration (everything up to `**SCENE PROMPT`).
  3. Apply TTS orthography so ElevenLabs reads acronyms/symbols correctly
     (401k -> "four-oh-one-kay", IRA -> "I R A", S&P 500 -> "S and P five
     hundred", % -> "percent"), and spell out risky dollar figures. CONFIRMED
     V04 hazard: ElevenLabs voiced "$847" as "eight forty-seven" ($8.47). Bare
     hundreds and non-round thousands misread the same way, so each becomes a
     {{spoken words|$digits}} dual-form -- the audio is unambiguous while the
     on-screen caption keeps the digits. Whole-thousands ($18,000) read fine
     and stay verbatim; millions+ are left to scripts/vo_number_lint.py (run by
     generate_vo at render).
  4. Convert the author's paragraph breaks into `<break time="0.8s" />` pacing
     pauses (generate_vo collapses newlines, so an explicit tag is the only way
     a paragraph pause survives to the render).

What it does NOT do: invent numbers, reword VO, place mid-paragraph dramatic
pauses (paragraph boundaries only -- hand-tune the kit after if you want more),
or generate {{spoken|caption}} dual-form tokens (only needed for non-round
millions; none of the current scripts use them -- existing tokens pass through).

  python3 build_vo_kit.py <script.md>                 # write the standard kit path
  python3 build_vo_kit.py <script.md> --output K.md   # write a chosen path
  python3 build_vo_kit.py <script.md> --stdout        # print, write nothing
  python3 build_vo_kit.py --selftest                  # run the built-in checks
"""
from __future__ import annotations

import argparse
import datetime as _dt
import re
import sys
from pathlib import Path

# A scene header in the SOURCE script: "## SCENE 4 [1:00-2:10] STAGE 1 - ...".
# The timing dash may be a hyphen or en-dash; the label is free text after it.
_SCENE_RE = re.compile(
    r"^##\s+SCENE\s+(\d+)\s*\[([^\]]*)\]\s*(.*?)\s*$",
    re.MULTILINE | re.IGNORECASE,
)

# The kit header generate_vo.py expects (kept identical to _BLOCK_RE there so a
# built kit is guaranteed parseable). Used by --selftest to verify our output.
_KIT_BLOCK_RE = re.compile(r"^##\s+Scene\s+(\d+)\s*(?:->|→)\s*`([^`]+\.mp3)`", re.MULTILINE)

# TTS orthography: ordered (longer/more-specific patterns first so e.g.
# "Roth IRA" is rewritten before the bare "IRA" rule can touch it).
_ORTHOGRAPHY: list[tuple[re.Pattern, str]] = [
    (re.compile(r"S&P\s*500"), "S and P five hundred"),
    (re.compile(r"S&P\b"), "S and P"),
    (re.compile(r"\bRoth\s+IRA\b"), "Roth I R A"),
    (re.compile(r"401\(k\)"), "four-oh-one-kay"),
    (re.compile(r"\b401k\b"), "four-oh-one-kay"),
    (re.compile(r"403\(b\)"), "four-oh-three-bee"),
    (re.compile(r"\bIRA\b"), "I R A"),
    (re.compile(r"\bHSA\b"), "H S A"),
    (re.compile(r"\s*%"), " percent"),
]

BREAK_TAG = '<break time="0.8s" />'

# --- Dollar-figure spelling (V04 phantom-decimal fix) ------------------------
_ONES = ("zero one two three four five six seven eight nine ten eleven twelve "
         "thirteen fourteen fifteen sixteen seventeen eighteen nineteen").split()
_TENS = ("", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy",
         "eighty", "ninety")
# A dollar figure whose digits start AND end with a digit (so a trailing
# sentence comma isn't swallowed), not followed by a decimal (skip real cents
# like $3.47), another digit (don't half-match $1234), or a k/M/B magnitude
# suffix -- leave $10k/$5M untouched for vo_number_lint / human review rather
# than voicing "$5" + a dangling "M" (the wrong value, billed).
_DOLLAR_RE = re.compile(r"\$(\d(?:[\d,]*\d)?)(?!\.\d)(?!\d)(?![kKmMbB])")


def _words_under_1000(n: int) -> str:
    if n < 20:
        return _ONES[n]
    if n < 100:
        t, o = divmod(n, 10)
        return _TENS[t] + (f"-{_ONES[o]}" if o else "")
    h, r = divmod(n, 100)
    return _ONES[h] + " hundred" + (f" {_words_under_1000(r)}" if r else "")


def num_to_words(n: int) -> str:
    """Cardinal words for 0..999,999 (US style, no 'and')."""
    if not 0 <= n < 1_000_000:
        raise ValueError(f"num_to_words out of range: {n}")
    if n < 1000:
        return _words_under_1000(n)
    th, r = divmod(n, 1000)
    return _words_under_1000(th) + " thousand" + (f" {_words_under_1000(r)}" if r else "")


def spell_dollars(text: str) -> str:
    """Spell integer dollar figures ElevenLabs would misread as decimals.

    Each risky figure -> {{<words> dollars|$digits}} dual-form (spoken words,
    captioned digits). Whole-thousands read fine; millions+ are gated elsewhere."""
    def repl(m: "re.Match[str]") -> str:
        raw = m.group(1)
        val = int(raw.replace(",", ""))
        if val >= 1_000_000:                  # ponytail: millions gated by vo_number_lint, not here
            return m.group(0)
        if val >= 1000 and val % 1000 == 0:   # whole-thousands ($18,000) voice cleanly
            return m.group(0)
        return "{{" + num_to_words(val) + " dollars|$" + raw + "}}"
    return _DOLLAR_RE.sub(repl, text)


def apply_orthography(text: str) -> str:
    for pat, rep in _ORTHOGRAPHY:
        text = pat.sub(rep, text)
    text = spell_dollars(text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def extract_vo(block: str) -> str | None:
    """The `**VO:**` narration in one scene block, up to the `**SCENE PROMPT` marker."""
    # IGNORECASE so a mis-cased marker (**vo:** / **Scene Prompt:**) can't cause a
    # missed VO (loud fail) or let image-prompt text bleed into the spoken render.
    m = re.search(r"\*\*VO:\*\*(.*?)(?=\*\*SCENE PROMPT|\Z)", block, re.DOTALL | re.IGNORECASE)
    if not m:
        return None
    vo = m.group(1).strip()
    return vo or None


def narration_to_kit_body(vo: str) -> str:
    """Paragraphs -> orthography-corrected narration with paragraph-boundary breaks.

    Each paragraph but the last gets a trailing break tag (the pacing pause the
    author signalled with a blank line); paragraphs stay on their own lines for
    human review -- generate_vo.clean_vo_text collapses the newlines but keeps the
    break tags."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", vo) if p.strip()]
    paras = [apply_orthography(p) for p in paras]
    out = []
    for i, p in enumerate(paras):
        out.append(p if i == len(paras) - 1 else f"{p} {BREAK_TAG}")
    return "\n\n".join(out)


def video_number(script_text: str, script_path: Path) -> str:
    """Zero-padded 2-digit video number from the frontmatter `video:` field
    (e.g. 'Video_07_...') or, failing that, the script filename."""
    for hay in (script_text[:2000], script_path.name):
        m = re.search(r"Video[_\s]?(\d{1,2})", hay)
        if m:
            return f"{int(m.group(1)):02d}"
    raise SystemExit(f"could not find a video number in {script_path.name} frontmatter or filename")


def parse_scenes(script_text: str) -> list[dict]:
    """Ordered [{scene, timing, label, vo}] for every SCENE block with narration."""
    heads = list(_SCENE_RE.finditer(script_text))
    if not heads:
        raise SystemExit("no '## SCENE N [...]' headers found -- is this a production script?")
    scenes = []
    for i, h in enumerate(heads):
        start = h.end()
        end = heads[i + 1].start() if i + 1 < len(heads) else len(script_text)
        vo = extract_vo(script_text[start:end])
        if vo is None:
            raise SystemExit(f"SCENE {h.group(1)} has no **VO:** block")
        scenes.append({
            "scene": int(h.group(1)),
            "timing": h.group(2).strip(),
            "label": h.group(3).strip(),
            "vo": vo,
        })
    nums = [s["scene"] for s in scenes]
    if nums != list(range(1, len(nums) + 1)):
        raise SystemExit(f"scene numbers are not contiguous 1..N: {nums}")
    return scenes


def build_kit(script_text: str, script_path: Path, vid: str) -> str:
    scenes = parse_scenes(script_text)
    title_m = re.search(r"^##\s+(.+)$", script_text[script_text.find("# 3SK"):], re.MULTILINE)
    title = title_m.group(1).strip() if title_m else f"Video {vid}"
    today = _dt.date.today().isoformat()
    lines = [
        "---",
        f"date: {today}",
        "type: vo-session-kit",
        f"video: Video_{vid}",
        "status: ok",
        "voice: config-driven (generate_vo.py DEFAULT_VOICE_ID, current id UgBBYS2sOqTuMpoF3BR0, speed 1.1)",
        f'source: "[[{script_path.stem}]]"',
        "generated-by: scripts/build_vo_kit.py",
        "tags:",
        f"  - production/video-{vid}",
        "  - production/voice",
        "---",
        "",
        f"# Video {vid} — VO Session Kit ({title})",
        "",
        f"> Auto-built from `{script_path.name}` by `scripts/build_vo_kit.py` on "
        f"{today}. One `## Scene N` block per mp3; paragraph breaks rendered as "
        f"`<break/>` pacing pauses; TTS orthography applied (401k, IRA, S&P 500, "
        f"percent-sign → spoken forms). Every dollar figure traces verbatim to the script — "
        f"no derived or invented numbers. Re-run the builder whenever the script "
        f"changes; do not hand-edit (edits are lost on the next build).",
        "",
    ]
    for s in scenes:
        fname = f"Video_{vid}_VO_Scene_{s['scene']:02d}.mp3"
        label = f"{s['label']}, {s['timing']}" if s["label"] else s["timing"]
        lines.append(f"## Scene {s['scene']} → `{fname}` ({label})")
        lines.append("")
        lines.append(narration_to_kit_body(s["vo"]))
        lines.append("")
        lines.append("---")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def default_output(vid: str, script_path: Path) -> Path:
    """Standard kit path: BRANDS/3SK_Finance/Voice_Files/Video_NN/_VO_Session_B_Kit.md.
    Derived by walking up to the BRANDS root from the script location."""
    for parent in script_path.resolve().parents:
        cand = parent / "Voice_Files" / f"Video_{vid}"
        if cand.is_dir():
            return cand / "_VO_Session_B_Kit.md"
    raise SystemExit(
        f"could not locate Voice_Files/Video_{vid}/ above {script_path}; pass --output explicitly"
    )


def selftest() -> int:
    sample = (
        "# 3SK FINANCE — VIDEO #09\n"
        "## A Test Title\n\n"
        "## SCENE 1 [0:00–0:18] COLD OPEN\n\n"
        "**VO:** You opened a Roth IRA and a 401k. The S&P 500 returned 7%.\n\n"
        "Second paragraph here. The HSA matters too.\n\n"
        "**SCENE PROMPT (paste after Master Character Prompt):**\n\n"
        "Scene: ignore me.\n\n"
        "---\n\n"
        "## SCENE 2 [0:18–0:40] THE PROMISE\n\n"
        "**VO:** Save $347 a month — that's $1,847 a year, not $18,000 — not $1,000,000 or $5M someday. Skip the $3.47 latte.\n\n"
        "**SCENE PROMPT:**\nScene: ignore.\n"
    )
    kit = build_kit(sample, Path("Video_09_Test.md"), "09")
    nw = {
        0: "zero", 7: "seven", 19: "nineteen", 20: "twenty", 21: "twenty-one",
        100: "one hundred", 347: "three hundred forty-seven", 1000: "one thousand",
        22100: "twenty-two thousand one hundred", 999999: "nine hundred ninety-nine thousand nine hundred ninety-nine",
    }
    checks = {
        "Roth I R A": "Roth I R A" in kit,
        "four-oh-one-kay": "four-oh-one-kay" in kit,
        "S and P five hundred": "S and P five hundred" in kit,
        "7 percent (no % sign)": "7 percent" in kit and "%" not in kit,
        "H S A": "H S A" in kit,
        "break between paragraphs": kit.count(BREAK_TAG) == 1,  # only scene 1 has 2 paras
        "two parseable kit blocks": len(_KIT_BLOCK_RE.findall(kit)) == 2,
        "mp3 names zero-padded": "Video_09_VO_Scene_01.mp3" in kit and "Video_09_VO_Scene_02.mp3" in kit,
        "scene-prompt excluded": "ignore me" not in kit and "ignore." not in kit,
        "label carried": "COLD OPEN, 0:00–0:18" in kit,
        "num_to_words table": all(num_to_words(k) == v for k, v in nw.items()),
        "$347 spelled (dual-form)": "{{three hundred forty-seven dollars|$347}}" in kit,
        "$18,000 whole-thousand kept": "$18,000" in kit and "eighteen thousand dollars" not in kit,
        "$3.47 cents untouched": "$3.47" in kit and "three dollars" not in kit,
        "$1,847 comma-grouped spelled": "{{one thousand eight hundred forty-seven dollars|$1,847}}" in kit,
        "$1,000,000 million kept verbatim": "$1,000,000" in kit and "one million" not in kit,
        "$5M suffix untouched": "$5M" in kit and "five dollars" not in kit,
    }
    ok = all(checks.values())
    for name, passed in checks.items():
        print(f"  [{'ok ' if passed else 'FAIL'}] {name}")
    print("selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main() -> None:
    ap = argparse.ArgumentParser(description="Build a VO Session-B kit from a 3SK production script.")
    ap.add_argument("script", nargs="?", help="Path to the production script .md")
    ap.add_argument("--output", help="Kit output path (default: the standard Voice_Files/Video_NN/ kit)")
    ap.add_argument("--stdout", action="store_true", help="Print the kit to stdout; write nothing")
    ap.add_argument("--selftest", action="store_true", help="Run built-in checks and exit")
    args = ap.parse_args()

    if args.selftest:
        raise SystemExit(selftest())
    if not args.script:
        ap.error("script path required (or use --selftest)")

    script_path = Path(args.script).expanduser()
    if not script_path.is_file():
        raise SystemExit(f"script not found: {script_path}")
    script_text = script_path.read_text(encoding="utf-8")
    vid = video_number(script_text, script_path)
    kit = build_kit(script_text, script_path, vid)

    if args.stdout:
        sys.stdout.write(kit)
        return
    out = Path(args.output).expanduser() if args.output else default_output(vid, script_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(kit, encoding="utf-8")
    n = len(_KIT_BLOCK_RE.findall(kit))
    print(f"wrote {out}  ({n} scenes, Video_{vid})")


if __name__ == "__main__":
    main()
