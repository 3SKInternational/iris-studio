#!/usr/bin/env python3
# Fail-closed redaction for build-logger's build-in-public drafts (Building Iris brand).
# Sibling of redact-book.py, with two deliberate differences (skeptical-code-reviewer,
# 2026-06-18):
#   1) BRAND-SAFE: the PUBLIC brand word "Iris" / "Building Iris" is KEPT. Only the
#      INTERNAL infra tokens (iris_studio, iris.py, com.iris.studio, the repo, bot
#      handles, env vars, log/db/script names) are scrubbed. The book redactor blanket-
#      replaces \biris\b -> [ASSISTANT], which is correct for the book but would mangle
#      this brand's own name in every draft.
#   2) OPEN-ENDED INPUTS: build-logger reads live git history + the bridge file + daily
#      notes, which carry identifiers no fixed name-list has seen. So this scrubs by
#      STRUCTURE (any /Users/<x>/ or /Volumes/<x>/ path, any IPv4, any *.local/.internal
#      host, ports) in addition to the known company/person tokens. The book redactor's
#      fixed-list-only approach would print "REDACTION OK" while novel paths/IPs/hosts
#      survived.
# Output contract matches redact-book.py exactly: prints REDACTION OK / REDACTION FAIL
# and exits 0 / 1, because build-logger greps that text to gate its run.
# Drift-proof: the credential list drives BOTH substitution and the residual check, and
# every residual pattern matches ONLY a non-redacted form (placeholders never re-trip it).
import re, sys

if len(sys.argv) < 2:
    print("REDACTION FAIL — usage: redact-buildlog.py <draft.md>")
    sys.exit(1)
P = sys.argv[1]
try:
    with open(P, encoding="utf-8") as fh:
        s = fh.read()
except OSError as e:
    print(f"REDACTION FAIL — cannot read draft {P}: {e}")
    sys.exit(1)

# 1) Filesystem paths -> [PATH]. Collapses the WHOLE path so the username / volume /
#    home structure never leaks regardless of the actual name. Covers home-relative
#    forms (~/…, $HOME/…) AND the identity-revealing absolute roots. The agent's own
#    inputs (bridge file, daily notes) are dense with ~/ paths, so this is load-bearing.
#    Stops at whitespace, quotes, backtick, comma, or a closing paren (markdown forms).
s = re.sub(r"(?:~|\$HOME)/[^\s)'\"`,]+", "[PATH]", s)
s = re.sub(r"(?:/Users/|/Volumes/|/home/)[^\s)'\"`,]+", "[PATH]", s)

# 2) Network detail: any IPv4 or IPv6 literal -> [IP]; *.local/.internal/.lan host ->
#    [HOST]; explicit ports -> [PORT]. Hostname scrub runs BEFORE the identity pass so a
#    host like steve-mac-mini.local collapses whole instead of leaking "-mac-mini.local".
#    IPV6_PAT is defined once and used for BOTH the scrub and the residual check (no
#    drift). It is bounded so it can't eat HH:MM[:SS] timestamps (decimal, <=2 colons) or
#    most code "::" scopes (std::, Vec::); all-hex scopes like abc::def over-redact to
#    [IP], which is the safe (fail-closed) direction, not a leak.
IPV6_PAT = (
    r"(?<![\w:.])(?:"
    r"(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}"                                # full 8 hextets
    r"|(?:[0-9A-Fa-f]{1,4}:){1,7}:(?:[0-9A-Fa-f]{1,4}:?){0,6}[0-9A-Fa-f]{0,4}"  # has ::
    r"|::(?:[0-9A-Fa-f]{1,4}:?){1,7}[0-9A-Fa-f]{0,4}"                          # leading ::
    r")(?!\w)"  # exclude trailing word char only (a sentence-ending '.' must not strand the last hextet)
)
s = re.sub(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", "[IP]", s)
s = re.sub(IPV6_PAT, "[IP]", s)
s = re.sub(r"\b[A-Za-z0-9][A-Za-z0-9\-]*\.(?:local|internal|lan)\b", "[HOST]", s)
s = re.sub(r"\bport\s+\d{1,5}\b", "port [PORT]", s, flags=re.IGNORECASE)

# 3) Internal infra compound tokens — NOT the public brand word "Iris". Longest/most-
#    specific first so a short token can't pre-truncate a superstring.
ordered = [
    ("3SKInternational/iris-studio", "[REPO]"),
    ("STEVE_CONTEXT", "[AUTHOR]_CONTEXT"),
    ("IRIS_TELEGRAM_USER_IDS", "[ENVVAR]"),
    ("@iris_studio_ai_bot", "@[BOT]"),
    ("iris_studio_ai_bot", "[BOT]"),
    ("iris_studio_bot", "[BOT]"),
    ("com.iris.studio", "[DAEMON]"),
    ("iris_studio", "[REPO]"),
    ("iris-studio", "[REPO]"),
    ("run_iris.sh", "[SCRIPT]"),
    ("iris.err.log", "[LOG]"),
    ("iris.out.log", "[LOG]"),
    ("iris.db", "[DB]"),
    ("iris.py", "[APP]"),
]
for a, b in ordered:
    s = s.replace(a, b)

# 4) CREDENTIAL redaction — MUST run before the company/person substring scrubs below.
#    A naive "3SK -> [COMPANY]" or "<user-id> -> [USER_ID]" pass would otherwise corrupt
#    an email domain (studio@3sk...) or a Telegram bot-token prefix (<id>:AA...) and break
#    the credential pattern's own match, leaking the secret tail (caught in self-test).
#    High-precision secret shapes that don't match prose. Same list drives the substitution
#    AND the fail-closed residual check (no drift). Mirrors redact-book.py's audited set.
CRED_PATTERNS = [
    (r"sk-ant-[A-Za-z0-9_\-]{20,}", "[REDACTED]"),                 # Anthropic API key
    (r"sk-proj-[A-Za-z0-9_\-]{20,}", "[REDACTED]"),                # OpenAI project key
    (r"sk-[A-Za-z0-9]{20,}", "[REDACTED]"),                        # OpenAI classic key
    (r"\b(?:ghp|gho|ghs|ghu|ghr)_[A-Za-z0-9]{36}\b", "[REDACTED]"),  # GitHub token
    (r"\bgithub_pat_[A-Za-z0-9_]{22,}\b", "[REDACTED]"),           # GitHub fine-grained PAT
    (r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b", "[REDACTED]"),           # Slack token
    (r"\bAKIA[0-9A-Z]{16}\b", "[REDACTED]"),                       # AWS access key id
    (r"\b\d{8,10}:AA[A-Za-z0-9_\-]{30,}\b", "[REDACTED]"),         # Telegram bot token
    (r"(?i)\bclient_secret[A-Za-z0-9_\-]*\.json\b", "[REDACTED]"),  # OAuth client-secret filename
    (r"-----BEGIN[A-Z ]*PRIVATE KEY-----", "[REDACTED]"),          # PEM private key header
    (r"(?i)\b((?:api[_-]?key|secret|password|passwd|access[_-]?token|auth[_-]?token|bearer[_-]?token|client[_-]?secret|token))(\s*[:=]\s*)['\"]?[A-Za-z0-9_\-./+]{8,}['\"]?",
     r"\1\2[REDACTED]"),                                           # generic NAME=secret
    (r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", "[REDACTED]"),  # any email
]
for pat, repl in CRED_PATTERNS:
    s = re.sub(pat, repl, s)

# 5) Company + person identifiers (case-insensitive). Runs AFTER credentials so it can't
#    corrupt an email/token (see step 4). The brand word "Iris" is deliberately ABSENT
#    here — keeping the public brand name intact is the whole point of this redactor.
#    NOTE: arbitrary THIRD-PARTY personal names (people the draft happens to mention) are
#    NOT mechanically detectable here — that is the agent's semantic responsibility
#    (drop-if-unsure, per the spec). This backstop guarantees the mechanical classes:
#    paths, IPs, hosts, credentials, the company, and the known author/operator identity.
s = re.sub(r"3SK", "[COMPANY]", s, flags=re.IGNORECASE)   # plain substr: also kills 3SK_Finance
s = re.sub(r"\bmainfolder\b", "[USER]", s, flags=re.IGNORECASE)
s = re.sub(r"\b5582798766\b", "[USER_ID]", s)
s = re.sub(r"\bsteve\b", "[USER]", s)                      # macOS username (lowercase form)
s = re.sub(r"\bste(?:ven?s?|phens?)\b", "[AUTHOR]", s, flags=re.IGNORECASE)  # the person
s = re.sub(r"\barias\b", "[AUTHOR]", s, flags=re.IGNORECASE)                 # surname

try:
    with open(P, "w", encoding="utf-8") as fh:
        fh.write(s)
except OSError as e:
    print(f"REDACTION FAIL — cannot write redacted draft {P}: {e}")
    sys.exit(1)

# 6) Residual check, fail-closed. Each pattern matches ONLY a non-redacted form, so the
#    [PLACEHOLDER] tokens above can never re-trip it. A surviving identifier or secret
#    makes this exit non-zero -> build-logger must drop the offending item and re-run.
checks = [
    r"/Users/", r"/Volumes/", r"/home/", r"~/", r"\$HOME/",    # any path root
    r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b",                  # any IPv4
    IPV6_PAT,                                                   # any IPv6 (same source as scrub)
    r"\b[A-Za-z0-9][A-Za-z0-9\-]*\.(?:local|internal|lan)\b",  # any internal host
    r"iris[_\-]\w", r"iris\.(?:py|db|err|out|sh)", r"com\.iris", r"@iris",  # infra forms ONLY
    r"3sk",                                                     # company
    r"steve", r"\bste(?:ven?s?|phens?)\b", r"\barias\b",       # person (catches steve_* compounds)
    r"\bmainfolder\b", r"\b5582798766\b",
] + [pat for pat, _ in CRED_PATTERNS]
bad = []
for c in checks:
    bad += re.findall(c, s, flags=re.IGNORECASE)
# the NAME=secret pattern returns tuples (groups); normalize for display
bad = [b if isinstance(b, str) else (b[0] if isinstance(b, tuple) else str(b)) for b in bad]
if bad:
    print("REDACTION FAIL — identifiers or credentials survived:", sorted(set(bad)))
    sys.exit(1)
print("REDACTION OK — zero known identifiers or credentials remain")
