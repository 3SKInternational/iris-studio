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
# Regression test: scripts/test_redact_book.py.
import re, sys

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
    s = re.sub(pat, repl, s)

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
if bad:
    print("REDACTION FAIL — identifiers or credentials survived:", sorted(set(bad)))
    sys.exit(1)
print("REDACTION OK — zero known identifiers or credentials remain")
