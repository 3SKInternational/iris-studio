#!/usr/bin/env python3
"""Guards for the per-video `rate-wpm` override in pipeline_orchestrator.

WHY THIS EXISTS. `rate-wpm` is a per-video VO rate override: V14 renders at
`--speed 1.0`, so its timestamp spine must be computed at the rate that speed
actually produces (145), not the 180 default. The stage-2 review->fix loop
dispatches `scriptwriter`, which rewrites frontmatter and has no idea the
override is deliberate — on 2026-07-27 it silently reset V14 from 161 back to
180 and rebuilt every scene span and all 16 chapter marks at the wrong rate.
Chapter marks are what viewers click. It was caught only because a human
happened to re-run the gate by hand afterwards.

`_restore_rate_wpm` WRITES TO A PRODUCTION SCRIPT from inside a gate, and the
vault is not git-tracked, so its edge cases are pinned here rather than trusted.
Every shape below was a real finding in code review, not a hypothetical.

Run: python3 tests/test_rate_wpm_guard.py
"""

import importlib.util
import pathlib
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("po", REPO / "scripts" / "pipeline_orchestrator.py")
po = importlib.util.module_from_spec(spec)
spec.loader.exec_module(po)

FAILS = []


def check(name, cond, detail=""):
    if cond:
        print(f"  [ok ] {name}")
    else:
        print(f"  [FAIL] {name} {detail}")
        FAILS.append(name)


def _tmp(text):
    f = tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8")
    f.write(text)
    f.close()
    return pathlib.Path(f.name)


# Call the REAL function. An earlier cut of this file re-composed _fm_bounds +
# _RATE_WPM_RE by hand, which meant the suite could not see the helper drifting away
# from the composition it claimed to pin.
_read = po._rate_wpm_of


FM = '---\ndate: 2026-07-27\nrate-wpm: %s\nx: 1\n---\n\n## SCENE 1 [0:00-0:02]\nbody\n'


def main():
    print("rate-wpm guard")

    # --- reading -----------------------------------------------------------
    check("plain integer read", _read(FM % "145") == "145")
    check("trailing comment read", _read('---\nrate-wpm: 145   # keep me\n---\nb\n') == "145")
    check("CRLF read", _read('---\r\nrate-wpm: 145\r\n---\r\nb\r\n') == "145")
    check("absent key -> None", _read('---\nx: 1\n---\nb\n') is None)
    # PRESENT-BUT-UNPARSEABLE must NOT read as absent: vo_wordcount.parse_rate takes
    # the integer part of `161.5` and computes a real spine from it, so treating it as
    # "no override" would leave a live rate unguarded — H2's silent-pass, other door.
    check("quoted value -> UNPARSEABLE", _read('---\nrate-wpm: "145"\n---\nb\n') == "UNPARSEABLE")
    check("indented (nested) key ignored", _read('---\nx:\n  rate-wpm: 145\n---\nb\n') is None)

    # A col-0 `rate-wpm:` inside a BODY code fence must not be seen: vo_wordcount's
    # parse_rate reads frontmatter only, and the two must agree or the orchestrator
    # would rewrite a line the rate actually comes from somewhere else.
    check("body-fence occurrence ignored",
          _read('---\nx: 1\n---\n\n```\nrate-wpm: 999\n```\n') is None)

    # A float must NOT match. A bare (?!\.) still backtracks — greedy \d+ gives back
    # digits until the lookahead passes, so `161.5` matched `16` and a restore wrote
    # `145.5`, a value nobody intended. (?![\d.]) makes the whole number the unit.
    check("float -> UNPARSEABLE (no backtrack to '16')",
          _read('---\nrate-wpm: 161.5\n---\nb\n') == "UNPARSEABLE")

    # --- restoring ---------------------------------------------------------
    p = _tmp(FM % "180")
    changed = po._restore_rate_wpm(p, "145")
    check("clobbered value restored", changed and _read(p.read_text()) == "145")

    check("idempotent on correct value", po._restore_rate_wpm(p, "145") is False)

    # Only that token may change — everything else byte-identical.
    orig = FM % "180"
    p2 = _tmp(orig)
    po._restore_rate_wpm(p2, "145")
    check("touches ONLY the rate token", p2.read_text() == orig.replace("180", "145", 1))

    p3 = _tmp('---\nrate-wpm: 180   # keep me\n---\nb\n')
    po._restore_rate_wpm(p3, "145")
    check("preserves a trailing comment", p3.read_text() == '---\nrate-wpm: 145   # keep me\n---\nb\n')

    # THE H2 CASE: if the fixer DELETES the key, restore cannot put it back and must
    # report False. The gate treats that as a hard block — otherwise parse_rate
    # silently defaults to 180, the post-check PASSES, and the video advances with the
    # override gone and every chapter mark wrong. Silent-pass is the worst outcome.
    p4 = _tmp('---\nx: 1\n---\nb\n')
    check("deleted key -> False (gate must block)", po._restore_rate_wpm(p4, "145") is False)

    # A float in the file must not be silently rewritten to `145.5`.
    p5 = _tmp('---\nrate-wpm: 161.5\n---\nb\n')
    po._restore_rate_wpm(p5, "145")
    check("float left untouched", p5.read_text() == '---\nrate-wpm: 161.5\n---\nb\n')

    # No frontmatter at all -> no write.
    p6 = _tmp('no frontmatter here\nrate-wpm: 180\n')
    check("no frontmatter -> no write", po._restore_rate_wpm(p6, "145") is False)

    # ATOMICITY — assert the MECHANISM. "no .tmp residue" is satisfied by a plain
    # write_text too (it never creates one), so that check could not fail and was
    # decoration. Monkeypatch os.replace and assert it is actually used.
    p7 = _tmp(FM % "180")
    seen = []
    _real = po.os.replace
    po.os.replace = lambda a, b: (seen.append((str(a), str(b))), _real(a, b))[1]
    try:
        po._restore_rate_wpm(p7, "145")
    finally:
        po.os.replace = _real
    check("write goes through os.replace (atomic)", len(seen) == 1, f"calls={seen}")
    check("no .tmp left behind", not p7.with_suffix(p7.suffix + ".tmp").exists())

    for f in (p, p2, p3, p4, p5, p6, p7):
        f.unlink(missing_ok=True)

    if FAILS:
        print(f"\ntest_rate_wpm_guard: FAIL ({len(FAILS)}): {', '.join(FAILS)}")
        return 1
    print("\ntest_rate_wpm_guard: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
