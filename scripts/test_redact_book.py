#!/usr/bin/env python3
# Regression test for redact-book.py — proves the replace-set covers the residual check-set
# (the _Iris_Memory asymmetry: `\biris\b` missed underscore-prefixed Iris while the
# `iris[_.]\w` residual check flagged it -> fail-closed on an un-rewritten path).
# Runs the real script as a subprocess on a temp file (the script executes at import time,
# so subprocess is the honest way to exercise it). No frameworks.
import ast, subprocess, sys, tempfile, os, glob

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "redact-book.py")
# The redact gate now builds its video-ID set from the channel's upload receipts and fails
# loud if none are present (Mini-bound). Off-Mac the whole suite skips honestly rather than
# reporting spurious failures for the pre-existing identity/cred cases.
RECEIPTS = glob.glob("/Users/steve/Documents/3SK/outputs/BRANDS/3SK_Finance/Production_Kits/*_youtube_upload.json")


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
    if not RECEIPTS:
        print("test_redact_book: SKIP — no upload receipts on this machine (redact gate is Mini-bound)")
        return

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

    # 4c) Same asymmetry class for AI_Workspace: lowercase/mixed must redact clean too.
    for variant in ("ai_workspace", "AI_Workspace", "Ai_Workspace"):
        code, out, msg = run(f"repo at /volumes/{variant}/build on disk")
        assert code == 0 and "ai_workspace" not in out.lower(), \
            f"{variant} should redact clean: {code} {out!r}"

    # 4d) Cred-pattern case symmetry: a case-flipped secret must redact clean, not fail-close.
    for secret in ("SK-ANT-" + "A" * 30, "GHP_" + "a" * 36, "akia" + "0" * 16):
        code, out, msg = run(f"leaked {secret} here")
        assert code == 0 and "[REDACTED]" in out, \
            f"case-flipped secret {secret!r} should redact clean: {code} {out!r}"

    # 4e) PASS-ORDER regression (two-pass audit, lane E): a credential whose VALUE contains an
    #     identity token. When the identity scrub ran first it rewrote the value to
    #     `[ASSISTANT]_studio_live_…`, putting a `[` where the generic NAME=secret pattern
    #     expects [A-Za-z0-9_\-./+] — so the substitution missed it AND the residual check
    #     (which re-scans the same CRED_PATTERNS) was blinded identically. Result: exit 0,
    #     "REDACTION OK", secret tail sitting on disk. Assert the SECRET is gone, not just
    #     that we exited clean — exit 0 was exactly what the bug produced.
    secret_tail = "9f3a2b7c1d"
    code, out, msg = run(f"api_key = iris_studio_live_{secret_tail}\n")
    assert secret_tail not in out, \
        f"fail-OPEN: credential tail survived the scrub (identity pass ran before creds?): {out!r}"
    assert "[REDACTED]" in out, f"credential not redacted: {code} {out!r}"

    # 4f) Same class for an email whose DOMAIN contains an identity token: `founder@3sk.com`
    #     became `founder@[COMPANY].com`, killing the email pattern (`[` is not in its domain
    #     class) and leaving the local part intact. The local part is the identifying half.
    code, out, msg = run("Mail founder@3sk.com to reach us.\n")
    assert "founder" not in out, \
        f"fail-OPEN: email local part survived (identity pass ran before creds?): {out!r}"
    assert "[REDACTED]" in out, f"email not redacted: {code} {out!r}"

    # 4g) PREFIXED credential names (round-6 lane-1 review, 2026-07-29). The
    #     generic NAME=secret pattern led with \b, which cannot match after an
    #     underscore (`_` is a word char) — so every .env-style name, i.e. THIS
    #     repo's own variables and the dominant real shape, sailed through. The
    #     fail-closed residual check is built from the SAME list, so it was
    #     blinded identically: "REDACTION OK", rc=0, secret byte-for-byte intact.
    #     Assert the SECRET is gone, not the exit code — rc=0 was the bug.
    for _name in ("ELEVENLABS_API_KEY", "TELEGRAM_BOT_TOKEN",
                  "YOUTUBE_CLIENT_SECRET", "MY_PASSWORD"):
        _secret = "a1b2c3d4e5f6g7h8i9j0"
        code, out, msg = run(f"{_name}={_secret}\n")
        assert _secret not in out, \
            f"fail-OPEN: {_name} value survived the scrub: {out!r}"
        assert "[REDACTED]" in out, f"{_name} not redacted: {code} {out!r}"

    # 5) Clean text passes.
    code, _, msg = run("This text has nothing to redact.")
    assert code == 0 and "REDACTION OK" in msg, f"clean text should pass: {msg!r}"

    # 6) Video-ID redaction (receipts-derived): a real V09 id and a youtu.be URL form both scrub
    #    (DY2RVnuUb64 = V09, LWWGbGUFFNk = V10 — both live receipt ids on the Mini).
    code, out, msg = run("watch DY2RVnuUb64 or https://youtu.be/LWWGbGUFFNk today")
    assert code == 0, f"video-id doc should redact clean: {msg!r}"
    assert "DY2RVnuUb64" not in out and "LWWGbGUFFNk" not in out, f"video id survived: {out!r}"
    assert "[VIDEO_ID]" in out, f"video id not scrubbed to token: {out!r}"

    # 7) _ids_from_receipt_dict (round-2 audit, 2026-07-28): deleted_video_id may be
    #    a LIST now (upload_video.write_receipt_preserving_dead_id can retire >1 dead
    #    id per receipt). redact-book.py executes fully on import (no __main__ guard,
    #    reads a live file via sys.argv[1]), so a plain import isn't safe here -- this
    #    isolates the ONE pure helper via AST extraction + exec into a blank namespace,
    #    same "unit-test a helper embedded in a script" technique, no live vault needed.
    tree = ast.parse(open(SCRIPT, encoding="utf-8").read())
    fn_node = next(n for n in tree.body
                   if isinstance(n, ast.FunctionDef) and n.name == "_ids_from_receipt_dict")
    ns = {}
    exec(compile(ast.Module(body=[fn_node], type_ignores=[]), "<redact_book_fn>", "exec"), ns)
    ids_from = ns["_ids_from_receipt_dict"]
    assert ids_from({"video_id": "abc12345678"}) == {"abc12345678"}
    assert ids_from({"video_id": None, "deleted_video_id": "ClJVIUtwsVE"}) == {"ClJVIUtwsVE"}
    # THE fix: a LIST of dead ids must contribute BOTH, not just one / none.
    assert ids_from({"video_id": "NEW1", "deleted_video_id": ["OLD1", "OLD2"]}) == \
        {"NEW1", "OLD1", "OLD2"}, "both list entries must survive, not be silently dropped"
    assert ids_from({}) == set()
    assert ids_from({"deleted_video_id": [None, "", "  ", "REAL1"]}) == {"REAL1"}

    print("test_redact_book: 10/10 pass")


if __name__ == "__main__":
    main()
