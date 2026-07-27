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
#
# The id accepts a LETTER SUFFIX ('4a', '4b', '4c') — scripts split one beat into
# parts, and V14 does it for 23 of its 28 scenes. The old `(\d+)\s*\[` could not
# match those, and the failure mode was DATA LOSS, not a loud error: an unmatched
# header did not start a new block, so the previous scene's block swallowed it, and
# extract_vo() takes only the FIRST **VO:** in a block — so every lettered scene's
# narration was silently dropped from the kit and never rendered into the video.
# V14 escaped only because the contiguity guard below happened to fire first.
_SCENE_RE = re.compile(
    r"^##\s+SCENE\s+(\d+[a-z]*)\s*\[([^\]]*)\]\s*(.*?)\s*$",
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
# _SKIP_TOKEN — IDEMPOTENCE. The figure after a "|" is the CAPTION half of an existing
# {{words|$X}} token; re-wrapping it nests {{a|{{a|$X}}}}, and generate_vo._DUAL_RE
# (which excludes braces on both halves) then matches only the INNER token and sends
# literal "{{ … | … }}" to ElevenLabs in a BILLED render, braces into the SRT too.
# The old `val >= 1_000_000: skip` was accidentally load-bearing here — it shielded
# every already-dual-formed millions figure, so removing it exposed this. The
# documented remediation loop (reviewer flags a bare million -> a fix pass writes the
# dual-form into the SCRIPT -> rebuild the kit) fed straight into it: V12 nested on
# rebuild. Anchor idempotence in the regex, not in a value guard.
_SKIP_TOKEN = r"(\{\{[^{}]*\}\})|"   # an existing dual-form: matched first, returned as-is
_DOLLAR_RE = re.compile(
    _SKIP_TOKEN +
    r"\$(\d(?:[\d,]*\d)?)(?!\.\d)(?!,\d)(?!\d)(?![kKmMbB])"  # (?!,\d) kills the partial match ending pre-comma ("$1,234.56" → "$1" mangle)
    r"(?!\s+(?:[Mm]illion|[Bb]illion|[Tt]rillion)\b)"  # "$10 million" reads fine; dual-forming yields "ten dollars million"
)
# Decimal dollars ("$31.40") — voiced "thirty-one dollars forty" without the
# "and … cents"; dual-form them separately (exactly 2 decimals = cents; a
# longer decimal is a rate/price-point, leave it alone). The % guard only
# matters on direct calls — in the pipeline apply_orthography rewrites % to
# " percent" BEFORE spell_dollars runs.
_DOLLAR_CENTS_RE = re.compile(
    _SKIP_TOKEN +
    r"\$(\d(?:[\d,]*\d)?)\.(\d{2})(?!\d)(?![kKmMbB%])"
    r"(?!\s+(?:[Mm]illion|[Bb]illion|[Tt]rillion)\b)"  # "$2.25 billion" must stay verbatim
)


def _words_under_1000(n: int) -> str:
    if n < 20:
        return _ONES[n]
    if n < 100:
        t, o = divmod(n, 10)
        return _TENS[t] + (f"-{_ONES[o]}" if o else "")
    h, r = divmod(n, 100)
    return _ONES[h] + " hundred" + (f" {_words_under_1000(r)}" if r else "")


def num_to_words(n: int) -> str:
    """Cardinal words for 0..999,999,999 (US style, no 'and').

    Millions were out of range until 2026-07-27, which is why spell_dollars had to
    skip every figure >= 1M and leave it as bare digits for the renderer. That is
    the exact shape ElevenLabs mis-speaks: it read V05's $1,043,000 as "one
    thousand forty-three thousand", dropping the millions place. V14 surfaced it at
    scale -- $1,240,000, the number the whole video is ABOUT, was bare in 6 spoken
    scenes. Spelling them is the fix; the lint that flagged them is warn-only and
    never blocked a render."""
    if not 0 <= n < 1_000_000_000:
        raise ValueError(f"num_to_words out of range: {n}")
    if n >= 1_000_000:
        mm, r = divmod(n, 1_000_000)
        return (_words_under_1000(mm) + " million"
                + (f" {num_to_words(r)}" if r else ""))
    if n < 1000:
        return _words_under_1000(n)
    th, r = divmod(n, 1000)
    return _words_under_1000(th) + " thousand" + (f" {_words_under_1000(r)}" if r else "")


def spell_dollars(text: str) -> str:
    """Spell integer dollar figures ElevenLabs would misread as decimals.

    Each risky figure -> {{<words> dollars|$digits}} dual-form (spoken words,
    captioned digits). Whole-thousands read fine; millions+ are gated elsewhere."""
    def repl(m: "re.Match[str]") -> str:
        if m.group(1):                     # already a {{...}} token — never re-wrap
            return m.group(1)
        raw = m.group(2)
        val = int(raw.replace(",", ""))
        # Whole millions ($1,000,000, $5,000,000) voice cleanly and read naturally
        # as digits; NON-round millions ($1,240,000) are the documented hazard.
        if val >= 1_000_000_000:              # num_to_words stops below 1e9; a
            return m.group(0)                 # non-round billion used to crash the build
        if val >= 1_000_000 and val % 1_000_000 == 0:
            return m.group(0)
        if val < 1_000_000 and val >= 1000 and val % 1000 == 0:
            return m.group(0)              # whole-thousands ($18,000) voice cleanly
        return "{{" + num_to_words(val) + " dollars|$" + raw + "}}"
    text = _DOLLAR_RE.sub(repl, text)
    return _DOLLAR_CENTS_RE.sub(_repl_cents, text)


def _repl_cents(m: "re.Match[str]") -> str:
    # "$31.40" is voiced "thirty-one dollars forty" — spell "and forty cents"
    # (Steve directive 2026-07-04, heard live on the V07 pump line).
    if m.group(1):                         # already a {{...}} token — never re-wrap
        return m.group(1)
    dollars = int(m.group(2).replace(",", ""))
    if dollars >= 1_000_000:                  # unchanged: millions-with-cents stay verbatim
        return m.group(0)                     # (rare, verbose spelled, and not the hazard)
    cents = int(m.group(3))
    spoken = num_to_words(dollars) + (" dollar" if dollars == 1 else " dollars")
    if cents:
        spoken += " and " + num_to_words(cents) + (" cent" if cents == 1 else " cents")
    return "{{" + spoken + "|$" + m.group(2) + "." + m.group(3) + "}}"


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
        src = h.group(1).lower()
        scenes.append({
            # KIT ordinal: always a plain 1..N integer, because the kit block format
            # and generate_vo._BLOCK_RE both require `## Scene <digits>` and the mp3
            # name is `_VO_Scene_%02d`. Source ids may be lettered; the kit's are not.
            "scene": len(scenes) + 1,
            "src": src,
            "timing": h.group(2).strip(),
            "label": h.group(3).strip(),
            "vo": vo,
        })
    # Contiguity guard, preserved in meaning: its job is "no source scene silently
    # skipped". With lettered ids the sequence is 1,2,3,4a,4b,5a..., so check the
    # NUMERIC PREFIXES instead — unique prefixes must be exactly 1..M with no gap,
    # and must appear in non-decreasing order (a jump from 4b back to 3 is a
    # mis-ordered script, same class of defect the old check caught).
    prefixes = [int(re.match(r"\d+", s["src"]).group(0)) for s in scenes]
    if prefixes != sorted(prefixes):
        raise SystemExit(f"scene numbers are out of order: {[s['src'] for s in scenes]}")
    uniq = sorted(set(prefixes))
    if uniq != list(range(1, len(uniq) + 1)):
        raise SystemExit(f"scene numbers are not contiguous 1..N: {[s['src'] for s in scenes]}")
    # DUPLICATE ids. The old `nums != list(range(1, N+1))` caught a repeated scene
    # number as a side effect (1,2,2,3 is not 1..4); the prefix/uniq rewrite above
    # does NOT, because a repeat leaves the set unchanged. Restored explicitly.
    # This is the LAST net for it: the kit renumbers positionally, so from a
    # duplicate onward kit scene N != script scene N, while the shot list and image
    # manifest stay keyed on SCRIPT numbers — every image after the dupe drifts one
    # scene out of sync with its narration. vo_wordcount does not catch it either
    # (its `derived` dict silently collapses duplicate ids and --check reports clean).
    srcs = [s["src"] for s in scenes]
    if len(set(srcs)) != len(srcs):
        dupes = sorted({s for s in srcs if srcs.count(s) > 1})
        raise SystemExit(f"duplicate scene number(s) {dupes} in: {srcs}")
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
        if s["src"] != str(s["scene"]):          # lettered source -> keep traceability
            label = f"src SCENE {s['src']}; {label}"
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


def _st_script(headers: list[str]) -> str:
    """Minimal parseable script from a list of scene headers, one VO line each."""
    out = ["# 3SK FINANCE — VIDEO #99", "## T", ""]
    for i, h in enumerate(headers):
        out += [h, "", f"**VO:** narration for block number {i} here.", "",
                "**SCENE PROMPT:**", "Scene: ignore.", "", "---", ""]
    return "\n".join(out)


def _st_lettered():
    """(scene_count, src ids, kit ordinals, all-VO-distinct) for a 1/2a/2b/3 script."""
    sc = parse_scenes(_st_script([
        "## SCENE 1 [0:00–0:10] A", "## SCENE 2a [0:10–0:20] B",
        "## SCENE 2b [0:20–0:30] C", "## SCENE 3 [0:30–0:40] D"]))
    vos = [s["vo"] for s in sc]
    return (len(sc), [s["src"] for s in sc], [s["scene"] for s in sc],
            len(set(vos)) == len(vos) and all(v.strip() for v in vos))


def _st_raises(headers: list[str], needle: str) -> bool:
    """parse_scenes(headers) must abort with `needle` in the message."""
    try:
        parse_scenes(_st_script(headers))
    except SystemExit as e:
        return needle in str(e)
    return False


def _st_dupe_raises(dupe_header: str) -> bool:
    """A repeated scene id must abort. The kit renumbers positionally, so a dupe
    silently desyncs every later image (keyed on SCRIPT number) from its narration."""
    first = dupe_header.split("[")[0].strip()          # e.g. '## SCENE 2a'
    try:
        parse_scenes(_st_script([
            "## SCENE 1 [0:00–0:10] A", f"{first} [0:10–0:20] B", dupe_header]))
    except SystemExit as e:
        return "duplicate scene number" in str(e)
    return False


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
        "**VO:** Save $347 a month — that's $1,847 a year, not $18,000 — not $1,000,000 or $5M someday, and past $10 million eventually. Skip the $3.47 latte, the $1.50 tip, the $1,234.56 splurge, the $2.25 billion fantasy, and the $1,234,567.89 daydream.\n\n"
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
        "$3.47 cents dual-formed": "{{three dollars and forty-seven cents|$3.47}}" in kit,
        "$1.50 singular dollar": "{{one dollar and fifty cents|$1.50}}" in kit,
        "$1,234.56 comma-decimal dual-formed": "{{one thousand two hundred thirty-four dollars and fifty-six cents|$1,234.56}}" in kit,
        "$2.25 billion kept verbatim": "$2.25 billion" in kit and "two dollars and twenty-five cents" not in kit,
        "$1,234,567.89 big-decimal kept verbatim (no crash)": "$1,234,567.89" in kit,
        "$1,847 comma-grouped spelled": "{{one thousand eight hundred forty-seven dollars|$1,847}}" in kit,
        "$1,000,000 million kept verbatim": "$1,000,000" in kit and "one million" not in kit,
        "$5M suffix untouched": "$5M" in kit and "five dollars" not in kit,
        "$10 million word-suffix kept verbatim": "$10 million" in kit and "ten dollars" not in kit,
        # --- NON-ROUND millions (2026-07-27) ---------------------------------
        # The documented ElevenLabs hazard: it read V05's $1,043,000 as "one
        # thousand forty-three thousand", dropping the millions place. V14 had
        # $1,240,000 -- the number the whole video is about -- bare in 6 spoken
        # scenes. num_to_words was capped below 1M, so spell_dollars had no choice
        # but to skip them. Whole millions still stay verbatim; only non-round ones
        # are dual-formed.
        "non-round million dual-formed": spell_dollars("$1,240,000")
            == "{{one million two hundred forty thousand dollars|$1,240,000}}",
        "V05 regression case dual-formed": spell_dollars("$1,043,000")
            == "{{one million forty-three thousand dollars|$1,043,000}}",
        "sub-thousand tail preserved": spell_dollars("$1,127,200")
            == "{{one million one hundred twenty-seven thousand two hundred dollars|$1,127,200}}",
        "whole millions still verbatim": spell_dollars("$1,000,000") == "$1,000,000"
            and spell_dollars("$5,000,000") == "$5,000,000",
        # IDEMPOTENCE (C1): feeding an already-dual-formed figure back in must be a
        # no-op. Absent this, the tool nested on its own documented rebuild loop.
        "dual-form is idempotent": spell_dollars("{{one million four thousand dollars|$1,004,000}}")
            == "{{one million four thousand dollars|$1,004,000}}",
        "cents dual-form idempotent": spell_dollars("{{one dollar and fifty cents|$1.50}}")
            == "{{one dollar and fifty cents|$1.50}}",
        # Idempotence must hold for EVERY spelling generate_vo._DUAL_RE accepts, not
        # just the exact bytes "|$". A (?<!\|) lookbehind covered only the tight form;
        # the spaced one is what a human actually types, and it nested. Anchor on the
        # TOKEN via _SKIP_TOKEN, and pin all four loose spellings here.
        "idempotent: space after pipe": spell_dollars("{{a dollars| $1,004,000}}")
            == "{{a dollars| $1,004,000}}",
        "idempotent: prefixed caption": spell_dollars("{{a dollars|about $1,004,000}}")
            == "{{a dollars|about $1,004,000}}",
        "idempotent: spaces both sides": spell_dollars("{{ a dollars | $1,004,000 }}")
            == "{{ a dollars | $1,004,000 }}",
        "idempotent: suffixed caption": spell_dollars("{{a dollars|$1,004,000 net}}")
            == "{{a dollars|$1,004,000 net}}",
        # ...while a BARE figure outside any token is still dual-formed.
        "bare figure still wrapped": "{{one million two hundred forty thousand dollars|$1,240,000}}"
            in spell_dollars("costs $1,240,000 total"),
        # H1: a non-round BILLION must pass through, not crash num_to_words.
        "non-round billion passes through": spell_dollars("$1,234,567,890") == "$1,234,567,890",
        # --- lettered scenes + duplicate guard (2026-07-26) -------------------
        # Before these, EVERY mutation of the lettered-scene fix left this suite
        # green — including reverting _SCENE_RE outright. A green run carried no
        # information about the one change it was supposed to cover.
        "lettered scenes all parsed": _st_lettered()[0] == 4,
        "lettered scene ids preserved": _st_lettered()[1] == ["1", "2a", "2b", "3"],
        "kit ordinals stay 1..N": _st_lettered()[2] == [1, 2, 3, 4],
        "no VO swallowed across lettered scenes": _st_lettered()[3] is True,
        "duplicate scene number raises": _st_dupe_raises("## SCENE 2 [0:1-0:2] B"),
        "duplicate lettered id raises": _st_dupe_raises("## SCENE 2a [0:1-0:2] B"),
        # the two guards the prefix-rewrite inherited from the old `nums != 1..N`
        "gap in scene numbers raises": _st_raises(
            ["## SCENE 1 [0:0-0:1] A", "## SCENE 2 [0:1-0:2] B", "## SCENE 4 [0:2-0:3] D"],
            "not contiguous"),
        "out-of-order scenes raise": _st_raises(
            ["## SCENE 1 [0:0-0:1] A", "## SCENE 3 [0:1-0:2] C", "## SCENE 2 [0:2-0:3] B"],
            "out of order"),
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
