#!/usr/bin/env python3
# Regression test for redact-book.py — proves the replace-set covers the residual check-set
# (the _Iris_Memory asymmetry: `\biris\b` missed underscore-prefixed Iris while the
# `iris[_.]\w` residual check flagged it -> fail-closed on an un-rewritten path).
# Runs the real script as a subprocess on a temp file (the script executes at import time,
# so subprocess is the honest way to exercise it). No frameworks.
import subprocess, sys, tempfile, os

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "redact-book.py")


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
    # 1) The exact regression: an _Iris_Memory path must redact clean (was the hard-fail).
    code, out, msg = run("See _Iris_Memory/Sessions/CLAUDE_CODE_HANDOFF.md for the bridge.")
    assert code == 0, f"_Iris_Memory path still fails redaction: {msg!r}"
    assert "Iris" not in out and "iris" not in out, f"iris survived: {out!r}"
    assert "_[ASSISTANT]_Memory" in out, f"unexpected rewrite: {out!r}"

    # 2) Sibling case the narrow fix would have missed — compound with a dot / other suffix.
    code, out, _ = run("logged to _Iris_Patterns and iris.log on disk")
    assert code == 0 and "iris" not in out.lower(), f"sibling compound leaked: {out!r}"

    # 3) Standalone "Iris" still redacts (didn't break the \biris\b path).
    code, out, _ = run("Iris is the operator persona.")
    assert code == 0 and out.startswith("[ASSISTANT] is"), f"standalone Iris broke: {out!r}"

    # 4) Credential redaction still works — a planted API key is scrubbed to [REDACTED].
    code, out, msg = run("token sk-ant-" + "A" * 30 + " leaked")
    assert code == 0 and "sk-ant-" not in out and "[REDACTED]" in out, \
        f"credential should be redacted: {code} {out!r}"

    # 4b) Same asymmetry class for STEVE_CONTEXT: lowercase/mixed must redact clean too.
    for variant in ("steve_context", "Steve_Context", "STEVE_CONTEXT"):
        code, out, msg = run(f"state lives in {variant} on disk")
        assert code == 0 and "steve" not in out.lower(), \
            f"{variant} should redact clean: {code} {out!r}"

    # 5) Clean text passes.
    code, _, msg = run("This text has nothing to redact.")
    assert code == 0 and "REDACTION OK" in msg, f"clean text should pass: {msg!r}"

    print("test_redact_book: 5/5 pass")


if __name__ == "__main__":
    main()
