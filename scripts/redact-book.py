#!/usr/bin/env python3
# PRIVATE redaction enforcement for the "Building [ASSISTANT]" book. Never publish.
# Hardened 2026-06-15 (skeptical-code-reviewer C1/C2/H1): case-insensitive identity +
# email replacements, full-email pattern (no domain glue), fixer broadened so all-caps
# forms are fixed (not wedged), residual check derived from the same token set.
import re, sys

P = "/Users/steve/Documents/3SK/outputs/iris-studio-ebook/iris-studio-ebook.md"
s = open(P, encoding="utf-8").read()

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
# Identity words: lowercase standalone "steve" = the macOS username -> [USER]; every other
# case form of Steve/Steven = the person -> [AUTHOR]. Lowercase first to keep the distinction.
s = re.sub(r"\bsteve\b", "[USER]", s)
s = re.sub(r"\bsteven?s?\b", "[AUTHOR]", s, flags=re.IGNORECASE)
s = re.sub(r"\biris\b", "[ASSISTANT]", s, flags=re.IGNORECASE)

open(P, "w", encoding="utf-8").write(s)

# 3) Residual check derived from the full token set (case-insensitive, fail-closed).
checks = [
    r"\bsteven?s?\b",
    r"\biris\b",
    r"iris[_.]\w",
    r"3sk",
    r"\bmainfolder\b",
    r"studio@",
    r"5582798766",
    r"steve_context",
]
bad = []
for c in checks:
    bad += re.findall(c, s, flags=re.IGNORECASE)
if bad:
    print("REDACTION FAIL — known identifiers survived:", sorted(set(bad)))
    sys.exit(1)
print("REDACTION OK — zero known identifiers remain")
