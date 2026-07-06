#!/usr/bin/env python3
"""Deterministic VO word-count + timestamp spine for 3SK Finance scripts.

Why this exists: LLM scriptwriters cannot reliably hand-count VO words. Across
V9/V10 a fabricated word count bounced the script-reviewer gate three times.
This computes the ground truth from the file so no agent has to eyeball it.

VO for a scene = every non-blank line between that scene's `**VO:**` marker and
its `**SCENE PROMPT` line (multi-paragraph VO included), with the marker
stripped. Word count = whitespace tokens (matches `wc -w`; a hyphen removed from
a compound word therefore counts as 2 words, same model the reviewer uses).

Timestamps are derived, one rule: a boundary at cumulative VO words W lands at
round(W / (wpm/60)) seconds. Rate = frontmatter `rate-wpm:` if present, else 180.

`--check` is a GATE: it treats a MISSING or unparseable value as a failure, not a
pass. A script with no `## SCENE` blocks, no `word-count-vo-only`/`runtime-target`
frontmatter, a scene header without a `[mm:ss-mm:ss]` span, or no Timestamps
chapter list FAILS — otherwise omission would be a silent bypass of the very
fabrication this tool exists to stop.

Usage:
  vo_wordcount.py SCRIPT.md            # print per-scene table, total, runtime, spine
  vo_wordcount.py SCRIPT.md --check    # gate: exit 1 on any mismatch OR missing value
  vo_wordcount.py SCRIPT.md --fix      # rewrite drifted values to the computed spine (in place)
  vo_wordcount.py --selftest           # hermetic self-check
"""
import os
import re
import sys

SCENE_RE = re.compile(r'^##\s*SCENE\s+(\d+)\b', re.I)
SCENE_HDR_TS_RE = re.compile(r'^##\s*SCENE\s+(\d+)\s*\[\s*(\d+:\d+)\s*[–—-]\s*(\d+:\d+)\s*\]', re.I)
VO_RE = re.compile(r'^\*\*VO:\*\*', re.I)
PROMPT_RE = re.compile(r'^\*\*SCENE PROMPT', re.I)
CHAPTER_RE = re.compile(r'^\s*(?:[-*]\s*)?(\d+:\d+)\b')  # tolerate a leading "- " / "* " bullet


def mmss(seconds: float) -> str:
    s = round(seconds)
    return f"{s // 60}:{s % 60:02d}"


def frontmatter(text: str) -> str:
    """The YAML frontmatter block only (between the first pair of --- fences)."""
    m = re.match(r'^---\s*\n(.*?)\n---\s*$', text, re.S | re.M)
    return m.group(1) if m else ''


def parse_rate(text: str) -> int:
    m = re.search(r'^rate-wpm:\s*(\d+)', frontmatter(text), re.M)
    return int(m.group(1)) if m else 180


def scene_counts(text: str):
    """Return ordered list of (scene_number, word_count)."""
    lines = text.splitlines()
    out, cur, buf, capturing = [], None, [], False

    def flush():
        if cur is not None:
            out.append((cur, len(" ".join(buf).split())))

    for ln in lines:
        m = SCENE_RE.match(ln)
        if m:
            flush()
            cur, buf, capturing = int(m.group(1)), [], False
            continue
        if VO_RE.match(ln):
            capturing = True
            buf.append(VO_RE.sub('', ln).strip())
            continue
        if PROMPT_RE.match(ln):
            capturing = False
            continue
        if capturing and ln.strip() not in ('', '---'):
            buf.append(ln.strip())
    flush()
    return out


def derive(counts, wpm: int):
    """Return (rows, total, runtime_str); rows = [(scene, words, start, end)]."""
    wps = wpm / 60.0
    cum = 0
    rows = []
    for n, w in counts:
        rows.append((n, w, mmss(cum / wps), mmss((cum + w) / wps)))
        cum += w
    return rows, cum, mmss(cum / wps)


def report(text: str) -> str:
    wpm = parse_rate(text)
    rows, total, runtime = derive(scene_counts(text), wpm)
    lines = [f"rate: {wpm} wpm", ""]
    for n, w, s, e in rows:
        lines.append(f"  S{n:<2} {w:>4}w  [{s}-{e}]")
    lines += ["", f"  TOTAL {total}w  ->  {runtime}"]
    return "\n".join(lines)


def check(text: str):
    """Gate: return a list of mismatches. Absence of a required value IS a mismatch."""
    wpm = parse_rate(text)
    counts = scene_counts(text)
    if not counts:
        return ["no scenes found (expected '## SCENE N' blocks with **VO:** text)"]
    rows, total, runtime = derive(counts, wpm)
    derived = {n: (s, e) for n, w, s, e in rows}
    fm = frontmatter(text)
    bad = []

    # frontmatter total + runtime — REQUIRED
    m = re.search(r'^word-count-vo-only:\s*(\d+)', fm, re.M)
    if not m:
        bad.append("frontmatter word-count-vo-only missing")
    elif int(m.group(1)) != total:
        bad.append(f"frontmatter word-count-vo-only {m.group(1)} != computed {total}")
    m = re.search(r'^runtime-target:\s*"?(\d+:\d+)"?', fm, re.M)
    if not m:
        bad.append("frontmatter runtime-target missing")
    elif m.group(1) != runtime:
        bad.append(f"frontmatter runtime-target {m.group(1)} != computed {runtime}")

    # every scene must carry a parseable header span, and it must match
    seen_span = set()
    for ln in text.splitlines():
        mh = SCENE_HDR_TS_RE.match(ln)
        if not mh:
            continue
        n, fs, fe = int(mh.group(1)), mh.group(2), mh.group(3)
        seen_span.add(n)
        if n in derived and (fs, fe) != derived[n]:
            bad.append(f"S{n} header [{fs}-{fe}] != computed [{derived[n][0]}-{derived[n][1]}]")
    for n, _ in counts:
        if n not in seen_span:
            bad.append(f"S{n} header missing a [mm:ss-mm:ss] span")

    # Timestamps chapter list — REQUIRED, and one mark per scene, in order
    ts = re.search(r'^##\s*Timestamps\b[^\n]*\n(.*?)(?=^##\s|\Z)', text, re.M | re.S | re.I)
    if not ts:
        bad.append("Timestamps section missing")
    else:
        chap = [m.group(1) for m in (CHAPTER_RE.match(l) for l in ts.group(1).splitlines()) if m]
        starts = [s for _, _, s, _ in rows]
        if not chap:
            bad.append("Timestamps section has no parseable chapter marks")
        elif len(chap) != len(starts):
            bad.append(f"chapter-list count {len(chap)} != scene count {len(starts)}")
        else:
            for i, (c, s) in enumerate(zip(chap, starts)):
                if c != s:
                    bad.append(f"chapter #{i + 1} start {c} != computed {s}")
    return bad


def _sub_frontmatter(text: str, total: int, runtime: str) -> str:
    """Rewrite word-count-vo-only / runtime-target inside the frontmatter block
    only (present keys, values only) — never touches the body or adds keys."""
    m = re.match(r'^(---\s*\n)(.*?)(\n---\s*(?:\n|$))', text, re.S)
    if not m:
        return text
    head, body, tail = m.group(1), m.group(2), m.group(3)
    body = re.sub(r'(?m)^(word-count-vo-only:\s*)\d+',
                  lambda mm: mm.group(1) + str(total), body)
    body = re.sub(r'(?m)^(runtime-target:\s*"?)\d+:\d+',
                  lambda mm: mm.group(1) + runtime, body)
    return head + body + tail + text[m.end():]


def fix(text: str):
    """Rewrite the drift-prone values (frontmatter total/runtime, scene header
    spans, Timestamps chapter marks) to the computed spine. Deterministic and
    idempotent. NEVER fabricates missing structure (a missing Timestamps section,
    a missing frontmatter key, a scene with no span, or a chapter-count/scene-count
    mismatch is left for a human). Returns (new_text, residual_mismatches)."""
    counts = scene_counts(text)
    if not counts:
        return text, check(text)
    wpm = parse_rate(text)
    rows, total, runtime = derive(counts, wpm)
    derived = {n: (s, e) for n, w, s, e in rows}
    starts = [s for _, _, s, _ in rows]

    text = _sub_frontmatter(text, total, runtime)
    trailing_nl = text.endswith("\n")
    lines = text.splitlines()

    # scene header spans — swap only the two time tokens; preserve the author's
    # separator (en/em-dash vs hyphen — all equivalent to check()) and any title,
    # so a correct header is never cosmetically churned.
    for i, ln in enumerate(lines):
        mh = SCENE_HDR_TS_RE.match(ln)
        if mh and int(mh.group(1)) in derived:
            s, e = derived[int(mh.group(1))]
            lines[i] = re.sub(
                r'\[(\s*)\d+:\d+(\s*[–—-]\s*)\d+:\d+(\s*)\]',
                lambda m: f'[{m.group(1)}{s}{m.group(2)}{e}{m.group(3)}]', ln, count=1)

    # Timestamps chapter marks — only the FIRST Timestamps section (mirror check(),
    # which validates only the first), and only if the count already matches so a
    # stale duplicate section is never churned.
    in_ts, seen_ts, chap_idx = False, False, []
    for i, ln in enumerate(lines):
        if re.match(r'^##\s', ln):  # any h2 heading opens the first Timestamps / closes any section
            in_ts = re.match(r'^##\s*Timestamps\b', ln, re.I) is not None and not seen_ts
            seen_ts = seen_ts or in_ts
            continue
        if in_ts and CHAPTER_RE.match(ln):
            chap_idx.append(i)
    if len(chap_idx) == len(starts):
        for k, i in enumerate(chap_idx):
            lines[i] = re.sub(r'\d+:\d+', starts[k], lines[i], count=1)

    new_text = "\n".join(lines) + ("\n" if trailing_nl else "")
    return new_text, check(new_text)


def _selftest():
    sample = (
        '---\nrate-wpm: 180\nword-count-vo-only: 12\nruntime-target: "0:04"\n---\n'
        '## SCENE 1 [0:00-0:02]\n'
        '**VO:** one two three four five six\n'
        '**SCENE PROMPT (paste):**\n'
        'Scene: ignored words here should not count\n'
        'Lighting: also ignored\n'
        '## SCENE 2 [0:02-0:04]\n'
        '**VO:** seven eight nine\n'
        '\n'
        'ten eleven twelve\n'
        '**SCENE PROMPT (paste):**\n'
        'Scene: ignored\n'
        '## Timestamps\n'
        '0:00 cold open\n'
        '0:02 second\n'
    )
    # core math
    counts = scene_counts(sample)
    assert counts == [(1, 6), (2, 6)], counts        # S2 multi-paragraph = 6, prompt excluded
    rows, total, runtime = derive(counts, 180)
    assert total == 12 and runtime == '0:04', (total, runtime)
    assert rows[0][2:] == ('0:00', '0:02') and rows[1][2:] == ('0:02', '0:04'), rows
    assert check(sample) == [], check(sample)        # matching file -> clean

    # mismatches are caught
    assert any('chapter' in b for b in check(sample.replace('0:02 second', '0:03 second')))
    assert any('word-count' in b for b in check(sample.replace('word-count-vo-only: 12', 'word-count-vo-only: 99')))

    # absence-is-a-mismatch (the gate holes the reviewer found)
    assert check('') == ['no scenes found (expected \'## SCENE N\' blocks with **VO:** text)'], check('')
    assert any('word-count-vo-only missing' in b for b in check(sample.replace('word-count-vo-only: 12\n', '')))
    assert any('runtime-target missing' in b for b in check(sample.replace('runtime-target: "0:04"\n', '')))
    assert any('Timestamps section missing' in b
               for b in check(re.sub(r'## Timestamps.*', '', sample, flags=re.S)))
    spanless = sample.replace('## SCENE 1 [0:00-0:02]', '## SCENE 1')
    assert any('S1 header missing a [mm:ss-mm:ss] span' in b for b in check(spanless)), check(spanless)

    # bulleted chapter list still parses (tolerance), and a corrupted bullet is caught
    bulleted = sample.replace('0:00 cold open\n0:02 second', '- 0:00 cold open\n- 0:02 second')
    assert check(bulleted) == [], check(bulleted)
    assert any('chapter' in b for b in check(bulleted.replace('- 0:02 second', '- 0:03 second')))

    # unknown flag must fail closed (a pipeline typo cannot silently disable the gate)
    assert main(['prog', '--bogus']) == 2
    assert main(['prog', 'x', '--chek']) == 2

    # --fix repairs span + chapter drift to the computed spine, and is idempotent
    drifted = sample.replace('0:02 second', '0:03 second').replace('[0:02-0:04]', '[0:02-0:05]')
    assert check(drifted), "sanity: drifted sample should fail check"
    fixed, residual = fix(drifted)
    assert residual == [], residual
    assert check(fixed) == [], check(fixed)
    assert fix(fixed)[0] == fixed, "fix must be idempotent"

    # --fix repairs frontmatter total/runtime drift, in the frontmatter only
    fm_drift = sample.replace('word-count-vo-only: 12', 'word-count-vo-only: 11') \
                     .replace('runtime-target: "0:04"', 'runtime-target: "0:05"')
    ff, r3 = fix(fm_drift)
    assert r3 == [] and check(ff) == [], (r3, check(ff))
    assert 'word-count-vo-only: 12' in ff and 'runtime-target: "0:04"' in ff

    # --fix never fabricates missing structure — leaves it as residual for a human
    noheader = re.sub(r'## Timestamps.*', '', sample, flags=re.S)
    _, r4 = fix(noheader)
    assert any('Timestamps' in b for b in r4), r4

    # --fix preserves the author's span separator (en-dash) — no cosmetic churn
    endash = sample.replace('[0:00-0:02]', '[0:00–0:02]').replace('[0:02-0:04]', '[0:02–0:04]')
    assert check(endash) == [], check(endash)      # en-dash spans are valid to check()
    assert fix(endash)[0] == endash, "fix must not rewrite a correct en-dash span"

    # duplicate Timestamps sections: fix() touches only the FIRST (mirror check),
    # never churns a stale duplicate, and still surfaces the count residual.
    dup = (sample.replace('0:00 cold open\n0:02 second\n', '0:00 cold open\n')
                 + '## Timestamps\n0:07 stale duplicate\n')
    assert any('chapter-list count' in b for b in check(dup)), check(dup)
    dfixed, dres = fix(dup)
    assert '0:07 stale duplicate' in dfixed, "must not rewrite a stale duplicate mark"
    assert any('chapter-list count' in b for b in dres), dres

    print("selftest OK")


def main(argv):
    flags = [a for a in argv[1:] if a.startswith('--')]
    unknown = [f for f in flags if f not in ('--check', '--fix', '--selftest')]
    if unknown:
        print(f"unknown flag(s): {' '.join(unknown)}", file=sys.stderr)
        return 2
    if '--selftest' in flags:
        _selftest()
        return 0
    args = [a for a in argv[1:] if not a.startswith('--')]
    if not args:
        print(__doc__.strip())
        return 2
    try:
        text = open(args[0], encoding='utf-8').read()
    except OSError as e:
        print(f"cannot read {args[0]}: {e}", file=sys.stderr)
        return 2
    if '--check' in flags:
        bad = check(text)
        if bad:
            print("MISMATCH:")
            for b in bad:
                print("  -", b)
            return 1
        print("OK: counts + timestamp spine consistent")
        return 0
    if '--fix' in flags:
        new_text, residual = fix(text)
        if new_text != text:
            bak = args[0] + '.bak-pre-vofix'
            with open(bak, 'w', encoding='utf-8') as f:
                f.write(text)
            tmp = args[0] + '.tmp-vofix'
            with open(tmp, 'w', encoding='utf-8') as f:
                f.write(new_text)
            os.replace(tmp, args[0])  # atomic
            print(f"FIXED {args[0]} (backup: {bak})")
        else:
            print("no drift to fix")
        if residual:
            print("RESIDUAL (needs a human — not auto-fixable):")
            for b in residual:
                print("  -", b)
            return 1
        print("OK: spine now consistent")
        return 0
    print(report(text))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
