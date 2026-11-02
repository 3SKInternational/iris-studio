#!/usr/bin/env python3
"""Regression test for redact-buildlog.py's CRED_PATTERNS case-sensitivity drift.

THE DEFECT (round-2 audit, 2026-07-28). The substitution loop ran `re.sub(pat,
repl, s)` with NO re.IGNORECASE, while the residual check re-scans the SAME
CRED_PATTERNS list with `flags=re.IGNORECASE`. A case-flipped secret whose
pattern relies on a case-specific literal prefix (SK-ANT-..., GHP_..., AKIA...)
survived the substitution untouched, then tripped the residual check -> exit 1.
Fail-CLOSED (no leak past this script), but UNCONVERGEABLE:
routines/build-logger.prompt:10 turns that exit 1 into ROUTINE_INCOMPLETE, so
the draft is discarded while the file this script already WROTE to disk at
step 6 (before the residual check runs) still carries the raw, unredacted
secret -- on disk, in the Drive-mirrored vault. redact-book.py closed the
identical drift class 2026-06-28 (see its own IGNORECASE comment); this is the
same fix ported to its sibling.

Runs the real script as a subprocess on a temp file (the script executes at
import time, so subprocess is the honest way to exercise it). No frameworks.

Run: python3 scripts/test_redact_buildlog.py   (exit 0 = pass)
"""
import os
import subprocess
import sys
import tempfile

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "redact-buildlog.py")


def run(text):
    """Redact `text` via the real script; return (exit_code, scrubbed_text, stdout)."""
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
        f.write(text)
        path = f.name
    try:
        r = subprocess.run([sys.executable, SCRIPT, path], capture_output=True, text=True)
        with open(path, encoding="utf-8") as fh:
            return r.returncode, fh.read(), r.stdout
    finally:
        os.unlink(path)


def main():
    # 1) THE regression: an uppercase Anthropic key must still redact clean, not
    # just fail-closed. Lowercase already worked before this fix (control case).
    code, out, msg = run("token sk-ant-" + "A" * 30 + " leaked")
    assert code == 0 and "sk-ant-" not in out.lower() and "[REDACTED]" in out, \
        f"lowercase (control) should already redact clean: {code} {out!r} {msg!r}"

    code, out, msg = run("token SK-ANT-" + "A" * 30 + " leaked")
    assert code == 0, f"uppercase key must redact CLEAN (not fail-closed): {code} {out!r} {msg!r}"
    assert "SK-ANT-" not in out, f"case-flipped secret tail survived the sub: {out!r}"
    assert "[REDACTED]" in out, f"expected a redaction marker: {out!r}"

    # 2) Same drift class for AKIA (AWS) and ghp_ (GitHub) prefixes.
    code, out, msg = run("key akia" + "0" * 16 + " and " + "GHP_" + "b" * 36)
    assert code == 0, f"case-flipped AWS/GitHub keys must redact clean: {code} {out!r} {msg!r}"
    assert "akia" not in out.lower() and "ghp_" not in out.lower(), \
        f"a case-flipped key survived: {out!r}"

    # 3) PREFIXED credential names (round-7 review, 2026-07-29). The generic
    #    NAME=secret pattern led with \b, which cannot match after an underscore
    #    (`_` is a word char), so every .env-style name — this repo's OWN variable
    #    names, and the dominant real shape — sailed through. The fail-closed
    #    residual check is built from the SAME CRED_PATTERNS list, so it was
    #    blinded identically: "REDACTION OK", rc=0, secret byte-for-byte intact.
    #
    #    THIS CASE EXISTS BECAUSE THE FIX WAS PORTED HERE WITH NO GUARD. It landed
    #    in both redactors, but the fixture went only into test_redact_book.py
    #    while a comment in redact-buildlog.py claimed a regression test covered
    #    it. Reverting the fix left THIS suite printing "2/2 pass" rc=0 over a live
    #    leak, and mutation abstained on the same file ("no mutable sites") — both
    #    halves of the mandatory gate silent at once. A ported fix needs its own
    #    fixture in its own sibling suite; a comment citing the original's test is
    #    not coverage.
    #
    #    Assert the SECRET is ABSENT, never the exit code — rc=0 IS the bug.
    for _name in ("ELEVENLABS_API_KEY", "TELEGRAM_BOT_TOKEN",
                  "YOUTUBE_CLIENT_SECRET", "MY_PASSWORD"):
        _secret = "a1b2c3d4e5f6g7h8i9j0"
        code, out, msg = run(f"{_name}={_secret}\n")
        assert _secret not in out, \
            f"fail-OPEN: {_name} value survived the scrub: {code} {out!r} {msg!r}"
        assert "[REDACTED]" in out, f"{_name} not redacted: {code} {out!r}"

    # 4) Identity token inside a snake_case identifier (A-114, 2026-11-02). `_` is a word
    #    character, so the old \b...\b sub AND check both missed these: the reviewer repro
    #    printed "REDACTION OK", rc 0, text unchanged. Assert the name is gone and rc 0.
    for text, token in (("rsync to air_mainfolder_copy", "mainfolder"),
                        ("see x_stephens_y", "stephen"),
                        ("call _arias_z now", "arias"),
                        ("load_Steve_prefs", "steve"),
                        ("calls parse_inbox_steve_items()", "steve"),
                        ("user id_5582798766_x", "5582798766"),
                        ("mainfolder2 and arias9 and steven_", "arias")):
        code, out, msg = run(text)
        assert code == 0 and token not in out.lower(), \
            f"{token} not redacted inside an identifier: {code} {out!r} {msg!r}"
    code, out, _ = run("mainfolder2 and arias9 and steven_")
    assert out == "[USER]2 and [AUTHOR]9 and [AUTHOR]_", f"wrong tokens: {out!r}"
    code, out, _ = run("calls parse_inbox_steve_items()")
    assert out == "calls parse_inbox_[USER]_items()", f"lowercase steve must be [USER]: {out!r}"
    code, out, _ = run("load_Steve_prefs")
    assert out == "load_[AUTHOR]_prefs", f"capitalised Steve must be [AUTHOR]: {out!r}"
    # Capitalised forms (review 2026-11-02: dropping IGNORECASE on these subs stayed green).
    code, out, _ = run("Steve Arias, MAINFOLDER")
    assert code == 0 and out == "[AUTHOR] [AUTHOR], [USER]", f"case forms: {code} {out!r}"
    # Letter boundaries still hold: a longer word that merely contains a token is untouched.
    code, out, _ = run("Stephenson met Zacharias, Ariasson and Xstephens.")
    assert code == 0 and out == "Stephenson met Zacharias, Ariasson and Xstephens.", \
        f"over-redacted: {code} {out!r}"
    # Glued to letters/digits on both sides: the sub skips it, the bare check fails closed.
    for text in ("xmainfoldery", "xmainfolder.", "mainfoldery", "xsteve.",
                 "x15582798766", "55827987660y"):
        code, out, _ = run(text)
        assert code == 1 and out == text, f"bare check must fail closed: {text!r} -> {code} {out!r}"

    print("test_redact_buildlog: 4/4 pass")


if __name__ == "__main__":
    main()
