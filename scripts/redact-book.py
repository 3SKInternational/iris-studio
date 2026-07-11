#!/usr/bin/env python3
# PRIVATE redaction enforcement for the "Building [ASSISTANT]" book. Never publish.
# Hardened 2026-06-15 (skeptical-code-reviewer C1/C2/H1): case-insensitive identity +
# email replacements, full-email pattern (no domain glue), fixer broadened so all-caps
# forms are fixed (not wedged), residual check derived from the same token set.
# Hardened 2026-06-17 (skeptical-code-reviewer C1/C2/H2): added credential redaction
# (API keys, bot tokens, AWS keys, generic KEY=value, any email) shared between the
# substitution pass AND the fail-closed residual check so they cannot drift; guarded I/O.
# Hardened 2026-06-26 (book-update pipeline hit a fail-closed-on-clean-input bug): the
# replace set drifted from the IGNORECASE residual check for compound tokens — `\biris\b`
# missed `_Iris_Memory` and the case-sensitive STEVE_CONTEXT entry missed `steve_context`,
# both of which the residual check flagged. Added IGNORECASE subs mirroring the checks.
# Hardened 2026-06-28 (autonomous redact-set symmetry sweep): closed the last
# check-without-symmetric-replace asymmetry — `ai_workspace` (IGNORECASE residual check)
# was only replaced by the case-SENSITIVE `AI_Workspace` entry, so a lowercased/mixed form
# fail-closed a clean book. Added an IGNORECASE mirror sub (test case 4c). Same pass also
# made the CRED_PATTERNS substitution loop IGNORECASE (its residual check was already
# IGNORECASE), closing the last instance of the drift class for credentials too (case 4d).
# Hardened 2026-07-11 (book-update editors hand-caught a live video ID two nights running):
# added receipts-derived VIDEO-ID redaction — the flagship editor caught `DY2RVnuUb64` (a real
# V09 YouTube id) in changelog prose that the static token list didn't know. Video ids are a
# mechanically-detectable class, so a deterministic gate must own it (not the LLM editor). Set
# is built dynamically from the channel's own upload receipts (self-updating), fail-loud if the
# receipts glob is empty. Case-sensitive replace + check (YouTube ids are case-sensitive).
# Regression test: scripts/test_redact_book.py.
import re, sys, json, glob

# Target book path: first CLI arg if given, else the flagship (backward compatible with
# any caller that runs this bare). The nightly multi-book pipeline passes each maintained
# book's path explicitly so every sellable book gets the same fail-closed scrub.
DEFAULT_P = "/Users/steve/Documents/3SK/outputs/iris-studio-ebook/iris-studio-ebook.md"
P = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_P
try:
    with open(P, encoding="utf-8") as fh:
        s = fh.read()
except OSError as e:
    print(f"REDACTION FAIL — cannot read book source {P}: {e}")
    sys.exit(1)

# 0) Video-ID redaction set — built from the channel's own upload receipts (receipts-win:
#    machine-written + self-updating as videos publish) plus a small supplement of dead
#    re-upload ids no receipt records anymore. The books are sellable + anonymized; a live
#    YouTube video id ties the build story to the real channel, and this class is mechanically
#    detectable, so a deterministic gate must own it (not the LLM editor).
#    ponytail: Mini-coupled — RECEIPTS_GLOB is an absolute vault path; this gate is Mini-bound
#    (matches DEFAULT_P). Missing receipts fail LOUD by design — an empty set would silently
#    stop scrubbing ids, the exact leak class this closes. Upgrade path if it ever runs
#    elsewhere: derive the vault root from DEFAULT_P / an env var.
RECEIPTS_GLOB = "/Users/steve/Documents/3SK/outputs/BRANDS/3SK_Finance/Production_Kits/*_youtube_upload.json"
VIDEO_ID_SUPPLEMENT = ["FJljsipxTkA", "n8l84_UBLUc"]  # dead re-upload ids no receipt records

def _collect_video_ids():
    ids = set(VIDEO_ID_SUPPLEMENT)
    receipts = glob.glob(RECEIPTS_GLOB)
    if not receipts:
        print(f"REDACTION FAIL — no upload receipts matched {RECEIPTS_GLOB}; "
              "refusing to run with an empty video-ID set.")
        sys.exit(1)
    for r in receipts:
        try:
            with open(r, encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError) as e:
            print(f"REDACTION FAIL — cannot parse upload receipt {r}: {e}")
            sys.exit(1)
        for key in ("video_id", "deleted_video_id"):
            v = d.get(key)
            if isinstance(v, str) and v.strip():
                ids.add(v.strip())
    return sorted(ids)

VIDEO_IDS = _collect_video_ids()

# 1) Ordered, case-SENSITIVE structural replacements. Paths + compound tokens appear in
#    fixed forms in the sources; longest/most-specific first so a short token never
#    pre-truncates a superstring.
ordered = [
    ("/Users/steve/Documents/3SK/outputs", "/Users/[USER]/Documents/[COMPANY]/outputs"),
    ("/Users/mainfolder/Documents/3SK/outputs", "/Users/[USER]/Documents/[COMPANY]/outputs"),
    ("/Users/steve", "/Users/[USER]"),
    ("STEVE_CONTEXT", "[AUTHOR]_CONTEXT"),
    ("3SK_workspace", "[COMPANY]_workspace"),
    ("/3SK/", "/[COMPANY]/"),
    ("AI_Workspace", "[SSD]"),
    ("IRIS_TELEGRAM_USER_IDS", "[ASSISTANT]_TELEGRAM_USER_IDS"),
    ("@iris_studio_ai_bot", "@[BOT]"),
    ("iris_studio_ai_bot", "[BOT]"),
    ("iris_studio_bot", "[BOT]"),
    ("com.iris.studio", "com.[ASSISTANT].studio"),
    ("iris_studio", "[ASSISTANT]_studio"),
    ("run_iris.sh", "run_[ASSISTANT].sh"),
    ("iris.err.log", "[ASSISTANT].err.log"),
    ("iris.out.log", "[ASSISTANT].out.log"),
    ("iris.db", "[ASSISTANT].db"),
    ("iris.py", "[ASSISTANT].py"),
]
for a, b in ordered:
    s = s.replace(a, b)

# 2) Case-INSENSITIVE pattern replacements (catch capitalized / relocated forms future
#    content may introduce). Email replaced as a whole token so the domain can't survive.
s = re.sub(r"\bmainfolder\b", "[USER]", s, flags=re.IGNORECASE)
s = re.sub(r"studio@[A-Za-z0-9._%+-]+\.[A-Za-z]{2,}", "[EMAIL]", s, flags=re.IGNORECASE)
s = re.sub(r"\b5582798766\b", "[USER_ID]", s)
s = re.sub(r"3SK", "[COMPANY]", s, flags=re.IGNORECASE)
# AI_Workspace is replaced case-SENSITIVELY in `ordered`, but the residual check
# `ai_workspace` is IGNORECASE -> a lowercased/mixed form (e.g. /volumes/ai_workspace/)
# survives the replace yet fails the check (same drift class as steve_context/iris below).
# Mirror the check so replace-set and check-set can't drift.
s = re.sub(r"ai_workspace", "[SSD]", s, flags=re.IGNORECASE)
# STEVE_CONTEXT only appears in the case-SENSITIVE `ordered` set, but the residual check
# `steve_context` is IGNORECASE — so a lowercase/mixed `steve_context` would survive replace
# yet fail the check (same drift class as the iris fix above). Mirror the check.
s = re.sub(r"steve_context", "[AUTHOR]_CONTEXT", s, flags=re.IGNORECASE)
# Identity words: lowercase standalone "steve" = the macOS username -> [USER]; every other
# case form of Steve/Steven = the person -> [AUTHOR]. Lowercase first to keep the distinction.
s = re.sub(r"\bsteve\b", "[USER]", s)
s = re.sub(r"\bste(?:ven?s?|phens?)\b", "[AUTHOR]", s, flags=re.IGNORECASE)
# "iris" as a compound-token prefix (iris_X / iris.X) has NO leading word boundary when it
# follows a word char (e.g. "_Iris_Memory"), so \biris\b below would miss it while the
# residual check `iris[_.]\w` flags it -> the redaction would fail-closed on a path the
# scrub never rewrote. Mirror the check exactly so replace-set and check-set can't drift.
s = re.sub(r"iris(?=[_.]\w)", "[ASSISTANT]", s, flags=re.IGNORECASE)
s = re.sub(r"\biris\b", "[ASSISTANT]", s, flags=re.IGNORECASE)

# Video ids: exact-substring scrub (also kills every URL form — youtu.be/<id>, watch?v=<id>,
# youtube.com/embed/<id> — since the id substring itself is replaced). Case-SENSITIVE, since a
# different casing is a different YouTube video; the residual check below mirrors this exactly.
for _vid in VIDEO_IDS:
    s = s.replace(_vid, "[VIDEO_ID]")

# 2b) CREDENTIAL redaction. These are high-precision secret shapes that don't match prose.
#     Each (pattern, flags) here is applied as a substitution AND re-scanned in the
#     fail-closed residual check below — single source of truth so the two can't drift
#     (the drift between replace-set and check-set was the original C2 leak class).
#     (pattern, replacement) — replacement keeps any capture groups it needs.
CRED_PATTERNS = [
    (r"sk-ant-[A-Za-z0-9_\-]{20,}", "[REDACTED]"),                 # Anthropic API key
    (r"sk-proj-[A-Za-z0-9_\-]{20,}", "[REDACTED]"),               # OpenAI project key
    (r"sk-[A-Za-z0-9]{20,}", "[REDACTED]"),                       # OpenAI classic key
    (r"\b(?:ghp|gho|ghs|ghu|ghr)_[A-Za-z0-9]{36}\b", "[REDACTED]"),  # GitHub token
    (r"\bgithub_pat_[A-Za-z0-9_]{22,}\b", "[REDACTED]"),          # GitHub fine-grained PAT
    (r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b", "[REDACTED]"),          # Slack token
    (r"\bAKIA[0-9A-Z]{16}\b", "[REDACTED]"),                      # AWS access key id
    (r"\b\d{8,10}:AA[A-Za-z0-9_\-]{30,}\b", "[REDACTED]"),        # Telegram bot token
    (r"(?i)\bclient_secret[A-Za-z0-9_\-]*\.json\b", "[REDACTED]"),  # OAuth client-secret filename
    (r"-----BEGIN[A-Z ]*PRIVATE KEY-----", "[REDACTED]"),         # PEM private key header
    # generic NAME=secret / NAME: secret assignments (keep the name, redact the value)
    (r"(?i)\b((?:api[_-]?key|secret|password|passwd|access[_-]?token|auth[_-]?token|bearer[_-]?token|client[_-]?secret|token))(\s*[:=]\s*)['\"]?[A-Za-z0-9_\-./+]{8,}['\"]?",
     r"\1\2[REDACTED]"),
    # any email address (the studio@ pass above only caught one prefix)
    (r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", "[REDACTED]"),
]
for pat, repl in CRED_PATTERNS:
    # IGNORECASE so the substitution scope EXACTLY matches the residual check below
    # (line scans CRED_PATTERNS with re.IGNORECASE). Without it, a case-flipped secret
    # (SK-ANT-…, GHP_…, akia…) survived the sub yet tripped the check -> fail-closed.
    # Same single-source-of-truth invariant as the identity tokens. The fix matters for
    # the prefix LITERALS (sk-ant-, ghp_, AKIA) which are case-specific; patterns with an
    # inline (?i) are unaffected, and the alpha char-classes were already both-case.
    s = re.sub(pat, repl, s, flags=re.IGNORECASE)

try:
    with open(P, "w", encoding="utf-8") as fh:
        fh.write(s)
except OSError as e:
    print(f"REDACTION FAIL — cannot write redacted book {P}: {e}")
    sys.exit(1)

# 3) Residual check, fail-closed. Identity tokens + every credential shape from the same
#    CRED_PATTERNS list, so a surviving secret can never pass as "REDACTION OK".
checks = [
    r"\bste(?:ven?s?|phens?)\b",
    r"\biris\b",
    r"iris[_.]\w",
    r"3sk",
    r"ai_workspace",
    r"\bmainfolder\b",
    r"studio@",
    r"5582798766",
    r"steve_context",
] + [pat for pat, _ in CRED_PATTERNS]
bad = []
for c in checks:
    bad += re.findall(c, s, flags=re.IGNORECASE)
# the assignment pattern returns tuples (groups); normalize for display
bad = [b if isinstance(b, str) else (b[0] if isinstance(b, tuple) else str(b)) for b in bad]
# Video ids checked case-SENSITIVELY (exact str, mirroring the str.replace above) so the
# replace-set and check-set can't drift — the same single-source-of-truth invariant the
# identity/credential passes enforce.
bad += [v for v in VIDEO_IDS if v in s]
if bad:
    print("REDACTION FAIL — identifiers or credentials survived:", sorted(set(bad)))
    sys.exit(1)
print("REDACTION OK — zero known identifiers or credentials remain")
