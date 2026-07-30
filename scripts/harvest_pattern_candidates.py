#!/usr/bin/env python3
"""Harvest `PATTERN CANDIDATE:` lines out of agent output into a review queue.

WHY THIS EXISTS (2026-07-28, Steve). 36 of 44 agents sit outside the ADAPTS
allowlist and can never be auto-edited (deliberately — a glob over
~/.claude/agents/ would let an injected proposal rewrite the safety machinery).
Their only learning channel was "Claude Code writes the lesson back by hand in
the same session", which measurably did not hold: `force-stub side-effecting
modules` and `guards must not re-parse` — both expensive, both generalizable —
lived only in the bridge file, which no agent reads.

So 19 agent defs now emit a one-line marker when a run yields a durable lesson:

    PATTERN CANDIDATE: <one-sentence rule> - evidence: <file / incident / number>

This script collects them. It is a GREP, not an agent, on purpose: a
summarizing agent asked to "find the lessons" can invent one, and a fabricated
pattern note is canon-poisoning that every downstream agent then reads. A
regex can only ever report text that a real agent really wrote.

It never promotes. It writes a queue; Steve approves; the Pattern note is
written by hand. Same shape as adaptation.py: propose, never apply.

⚠️ THE EMISSION SURFACE IS NOT THIS SCRIPT'S PROBLEM TO SOLVE, AND IT IS THE
WHOLE BALLGAME. A subagent's reply goes to its CALLER's transcript, not to a
vault `.md` file, so nothing here can see it unless the caller writes it down.
On 2026-07-29 the gardener's librarian emitted a real candidate, the caller
PARAPHRASED it into prose ("Emitted 1 PATTERN CANDIDATE (add ...)"), and this
script correctly reported zero — publishing "no pending candidates" on a day a
lesson was lost. Worse than not shipping: false assurance.

Each DISPATCHING SURFACE has to transcribe for itself, and only one does so far:
  * COVERED — `routines/vault-gardener.prompt` step 10a: appends every returned
    marker line VERBATIM to the daily note before 10b runs, and reports the
    count so a skipped step is distinguishable from "nobody emitted".
  * COVERED — `routines/security-audit.prompt`: same transcription clause for
    the `security-auditor` it dispatches (it does not run the harvest; the
    gardener owns that).
  * NOT COVERED — every OTHER routine that dispatches one of the 19, plus
    interactive and daemon dispatches. Their emissions are still lost. Adding a
    surface means pasting the same clause into that routine.
Do not read this file as closing the class. It closes nothing; it only refuses
to silently drop what reaches it — hence "malformed" and "mention" are reported
rather than skipped, and a paraphrase surfaces as a visible mention.

Usage (--vault is REQUIRED; see the VAULT_HINT comment for why):
  harvest_pattern_candidates.py --vault /path/to/vault
  harvest_pattern_candidates.py --vault /path/to/vault --days 14   # narrower
  harvest_pattern_candidates.py --vault /path/to/vault --dry-run   # no writes

Exit codes (the scheduled-job contract):
  0  = queue written, and something needs a human (pending / malformed /
       invalid state / an INCOMPLETE scan)
  75 = nothing pending and nothing wrong (healthy no-op — stays quiet, per the
       alert policy)
  1  = error

Gate coverage lives in tests/test_harvest_pattern_candidates.py. There is
deliberately NO `--selftest` here: precheck's mutation pass cannot kill a
mutation inside a selftest (weakening one of its checks still returns 0), so a
selftest in this file would be 130 lines of parallel assertions that
permanently BLOCK the gate while duplicating the real suite.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

# NO implicit production default. `--vault` is REQUIRED, and that is a safety
# property, not ergonomics. When it defaulted to the real vault, any harness that
# merely IMPORTED this module with the `__main__` guard disturbed — which is
# precisely what `mutate.py` does, three times — ran main() with an empty argv,
# resolved the default, and rewrote `_Candidate_Queue.md` and
# `_candidate_state.json` in Steve's live Drive-synced vault. It then exited 0
# with zero tests run, which mutate.py scored as a PASSING suite, so the mutant
# was reported SURVIVED-equivalent. Worse, that verdict was contingent on live
# vault content (empty vault -> exit 75 -> reported killed): an instrument whose
# answer depends on production state carries no information.
# A guard cannot fix this, because the guard is the thing being mutated. Removing
# the default does: a bare main() now dies in argparse (exit 2) and writes
# nothing. Same shape as [[Pattern_force_stub_side_effecting_modules]] — the
# import IS the side effect — and a green gate could not have told you.
VAULT_HINT = "/Users/steve/Documents/3SK/outputs"
PATTERNS_SUBDIR = "_Iris_Memory/Patterns"
QUEUE_NAME = "_Candidate_Queue.md"
STATE_NAME = "_candidate_state.json"

# Dirs that never hold real agent output. iris-studio-ebook is book snapshots;
# 07_Archive is by definition already-dealt-with; graphify-out is machine-
# generated and already contains a verbatim copy of this queue.
SKIP_DIR_NAMES = {".git", ".obsidian", "07_Archive", "iris-studio-ebook",
                  "node_modules", "__pycache__", ".venv", "graphify-out"}
SCAN_SUFFIXES = {".md"}

# DETECTION is deliberately looser than EMISSION, in two tiers, because a line
# that names the marker without exact punctuation must still become a visible
# Finding rather than vanish. Live proof: the 2026-07-29 gardener wrote
# "Emitted 1 PATTERN CANDIDATE (add `provenance:` ...)" — colon-less, and a
# colon-required detector dropped it without a trace.
#   * WITH a colon → any casing, and `-`/`_` accepted between the words.
#   * WITHOUT a colon → any casing, but SINGULAR only.
#
# The singular-only rule on the colon-less tier is the whole trick, and it was
# MEASURED against the real 30-day corpus rather than argued:
#   ALL-CAPS-only        → 1 hit, but silently drops "Pattern Candidate" —
#                          i.e. the 7/29 incident line one capitalization away.
#   any case, any number → 4 hits, one of which is the routine's own
#                          "### 🧪 Pattern candidates emitted" heading, which
#                          would recur in the report every single day forever.
#   any case, singular   → 3 hits: +2 one-off mentions, no recurring heading.
# The plural exclusion is what buys case-insensitivity without the permanent
# noise, and noise in the transparency section is not free — it is exactly what
# makes people stop reading it.
# The COLON tier is permissive about the marker's own spelling — plural and a
# missing/odd separator are accepted — because a colon proves intent, so there is
# no noise to trade against. Measured on the live corpus: `candidates:` (plural
# WITH colon) has 0 occurrences, so widening costs nothing and closes a silent
# drop; `PATTERN CANDIDATES: rule - evidence: e` used to return None, violating
# this file's one invariant.
_MARKER = r"pattern[\s_-]*candidates?"
_DETECT_COLON_RE = re.compile(_MARKER + r"\s*[`*_]*\s*:", re.IGNORECASE)
# The LOOSE tier (no colon) stays SINGULAR. Measured over 1759 live files /
# 176,570 lines, and only ONE of the two restrictions earns its keep:
#   * `(?!s)` IS load-bearing — dropping it adds 7 permanent findings, including
#     the routine's own "### 🧪 Pattern candidates emitted" heading every single
#     day and three matches on this script's OWN filename.
#   * requiring a separator (`[\s_-]+` vs `*`) changes 0 classifications. Kept
#     only because it costs nothing, NOT because it is load-bearing.
# Stated precisely on purpose: this file already carries one scar (see the
# _LINE_START_LOOSE_RE note) from a comment that called a restriction
# load-bearing when it was inert, and a false "this matters" misleads the next
# maintainer just as badly as a missing warning.
_DETECT_LOOSE_RE = re.compile(r"pattern[\s_-]+candidate(?!s)", re.IGNORECASE)


def _names_marker(line: str) -> bool:
    return bool(_DETECT_COLON_RE.search(line) or _DETECT_LOOSE_RE.search(line))

# A real emission OWNS its line (optionally behind a list bullet, blockquote,
# heading, numbered-list marker, bold run or backtick) and carries the colon.
# Prose that names the marker mid-sentence is a mention, not an emission.
# Verified against the real corpus before shipping: the first live dry-run
# produced two "malformed" hits, both ordinary prose in the bridge and daily
# note. Left unfixed, the malformed section fills with documentation noise and
# stops being read — destroying the loud-on-malformed property it exists for.
# `\[[ xX]\]` accepts a task-list bullet: `* [ ] PATTERN CANDIDATE: r - evidence: e`
# was classed a mention and its rule discarded — same shape as the placeholder
# over-match, and checklists are ordinary agent-report formatting.
_PREFIX = r"^[\s>]*(?:[-*+]\s+|\d+[.)]\s+|#+\s+|\[[ xX]\]\s*)*[`*_\s]*"
# Two line-owning tests, matching the two detection tiers.
# The loose one carries NO plural exclusion, and an earlier comment here claiming
# it needed one (or the routine's plural heading would own its line and land in
# the LOUD section daily) was simply wrong: _names_marker rejects that heading
# first, so this regex is never reached for a plural. Measured — allowing plural
# here changes classification on 0 of 176,520 live corpus lines. The exclusion
# that IS load-bearing lives in _DETECT_LOOSE_RE, which goes red when dropped.
_LINE_START_COLON_RE = re.compile(_PREFIX + _DETECT_COLON_RE.pattern, re.IGNORECASE)
_LINE_START_LOOSE_RE = re.compile(_PREFIX + _MARKER, re.IGNORECASE)

# Anchored on the literal `evidence:` keyword rather than on the separator glyph.
# The block tells agents to use a dash, but harvesting must not be one unicode
# variant away from silently dropping a real lesson — so any run of dash-ish or
# decoration characters before `evidence:` is accepted, and the keyword is what
# actually delimits the two fields.
_FIELDS_RE = re.compile(
    _MARKER + r"\s*[`*_]*\s*:\s*(?P<rule>.+?)\s*[—–\-\s`*_]*"
    r"evidence\s*[`*_]*\s*:\s*(?P<ev>.+?)\s*$",
    re.IGNORECASE,
)

# A field that is ENTIRELY a placeholder, e.g. `<one-sentence rule>`. Anchored
# whole-field on purpose: an earlier `<[^>]{2,}>` searched anywhere and bucketed
# real lessons as template text — "always run precheck.sh --mutate
# <changed_file.py> first" and "strip <script> before rendering user html" both
# became "not a lesson", and that section does not even echo the rule. Angle
# brackets are this repo's standing convention for a slot, so they turn up
# inside exactly the engineering rules most worth keeping.
_PLACEHOLDER_RE = re.compile(r"^<[^>]+>$")

# Decoration an LLM habitually wraps a label in. Stripped from the ENDS of both
# fields only, so a mid-rule backtick (`--mutate <file>`) survives intact.
_DECORATION = " \t`*_—–-"

# A rule accumulates one ref per sighting; line numbers shift with every edit
# above them, so an un-capped list grows stale entries forever. Ten is enough to
# see the spread and cheap to render.
MAX_REFS = 10

# The only statuses a human may set. Anything else is a typo to be reported, not
# a silent filter-out.
VALID_STATUS = frozenset({"pending", "promoted", "rejected"})


def normalize(rule: str) -> str:
    """Collapse a rule to its dedupe key. Deliberately crude: whitespace, case
    and trailing punctuation only. Anything cleverer (stemming, synonyms) would
    merge two genuinely different lessons, and a lost lesson is the failure this
    script exists to prevent — so it errs toward keeping duplicates."""
    s = re.sub(r"\s+", " ", rule.strip().lower())
    return s.rstrip(" .;,:!")


HASH_WIDTH = 12          # STATE-MIGRATION-BREAKING. See rule_hash.


def rule_hash(rule: str) -> str:
    """Stable dedupe key for a rule. NOT a display detail.

    ⚠️ HASH_WIDTH is baked into every key in `_candidate_state.json`, so changing
    it is a silent data migration: existing rows keep the old key, the same rules
    re-hash to new ones, and every `promoted`/`rejected` decision a human made
    comes back as `pending` while the row is double-booked. Measured, not
    theorised: `[:12]` -> `[:16]` resurrected an already-rejected rule into the
    pending queue with the whole gate green.

    An earlier `# mutequiv:` here blessed "any length in roughly 8..64" on the
    grounds that the digest is "an opaque dedupe/display key" — the third false
    equivalence marker in this file, and the worst, because it excused a change
    that breaks the #1 property the suite claims to protect. Width is pinned by
    an independent literal in the tests instead: a change must be a decision.
    """
    return hashlib.sha256(normalize(rule).encode("utf-8")).hexdigest()[:HASH_WIDTH]


class Finding:
    """One marker occurrence. `kind` is ok | malformed | placeholder | mention."""

    def __init__(self, kind: str, source: str, line_no: int, rule: str = "",
                 evidence: str = "", raw: str = ""):
        self.kind = kind
        self.source = source
        self.line_no = line_no
        self.rule = rule
        self.evidence = evidence
        self.raw = raw

    @property
    def ref(self) -> str:
        return f"{self.source}:{self.line_no}"


def parse_line(line: str, source: str, line_no: int) -> Finding | None:
    """Classify one line. Returns None ONLY when the marker is truly absent.

    Every line naming the marker produces a Finding — one that cannot be parsed
    is reported as `malformed`, never dropped. Silent skipping is the one
    behaviour this script must not have: an agent that wrote a slightly-off
    marker has still done the hard part (noticing the lesson), and losing that
    is indistinguishable from the failure mode we are fixing.

    `kind` is one of:
      ok          a real, parseable candidate
      malformed   the marker owns the line but the fields are unreadable — LOUD
      placeholder the instruction template quoted in docs, not a real lesson
      mention     the marker named mid-sentence in prose, not an emission
    """
    if not _names_marker(line):
        return None
    raw = line.strip()
    if not _LINE_START_COLON_RE.match(line):
        # Owns its line but has no colon (the paraphrase form) -> LOUD. Names the
        # marker mid-sentence -> a mention, which is transparency, not a lesson.
        kind = "malformed" if _LINE_START_LOOSE_RE.match(line) else "mention"
        return Finding(kind, source, line_no, raw=raw)
    # No explicit colon check here: a line that owns the marker but lacks the
    # colon (the paraphrase form, "PATTERN CANDIDATE add provenance") fails
    # _FIELDS_RE below — which requires the colon — and falls out as malformed
    # anyway. NOT semantically identical to having the guard, and an earlier
    # comment wrongly claimed it was: with the guard, a line that names the
    # marker colon-lessly and then again WITH a colon later on the same line was
    # malformed; now it parses the second occurrence as ok. That is arguably
    # better, and the dup-guard below only counts colon-forms, so it does not
    # fire. Removed because it was redundant for every shape that matters — not
    # because the two are equivalent in the abstract.
    # Two emissions crammed onto one line would silently fold the second into
    # the first's evidence. Refuse loudly instead of merging.
    if len(_DETECT_COLON_RE.findall(line)) > 1:
        return Finding("malformed", source, line_no, raw=raw)
    m = _FIELDS_RE.search(line)
    if not m:
        return Finding("malformed", source, line_no, raw=raw)
    # Strip decoration off the ends of both fields. With an EMPTY rule
    # (`PATTERN CANDIDATE:  - evidence: x`) the lazy `.+?` happily captures the
    # separator itself, and a rule of "-" is non-empty enough to pass a naive
    # check — queueing a dash as a lesson.
    rule = m.group("rule").strip(_DECORATION)
    evidence = m.group("ev").strip(_DECORATION)
    if not rule or not evidence:
        return Finding("malformed", source, line_no, raw=raw)
    if _PLACEHOLDER_RE.match(rule) or _PLACEHOLDER_RE.match(evidence):
        return Finding("placeholder", source, line_no, rule=rule,
                       evidence=evidence, raw=raw)
    return Finding("ok", source, line_no, rule=rule, evidence=evidence, raw=raw)


def scan_file(path: Path, rel: str) -> list[Finding]:
    # A file we cannot read is reported by the caller, never counted clean.
    text = path.read_text(encoding="utf-8", errors="replace")
    out: list[Finding] = []
    for i, line in enumerate(text.splitlines(), start=1):
        f = parse_line(line, rel, i)
        if f is not None:
            out.append(f)
    return out


def scan(vault: Path, days: float, now: float) -> tuple[list[Finding], list[str]]:
    """Walk the vault for markers in files touched within `days`.

    Returns (findings, unreadable). Unreadable paths are surfaced, never
    swallowed: a permission error that silently shrinks the scan set would make
    an empty queue look like a healthy no-op — the exact false-green this whole
    script is supposed to be immune to.

    Uses os.walk with an onerror hook rather than Path.rglob because rglob
    SWALLOWS a scandir PermissionError: an unreadable DIRECTORY holding a real
    candidate produced findings=[] unreadable=[] and read as perfectly healthy.
    """
    findings: list[Finding] = []
    unreadable: list[str] = []
    patterns_dir = vault / PATTERNS_SUBDIR
    # Queue only. The state file is `.json`, which SCAN_SUFFIXES rejects before
    # this test is ever reached, so listing it here was unreachable code.
    own_files = {patterns_dir / QUEUE_NAME}
    # mutequiv: 86400 -> 86401. A one-second shift in a window measured in days;
    # no file's inclusion can turn on it in any reachable scenario.
    cutoff = now - days * 86400

    def _onerror(err: OSError) -> None:
        p = getattr(err, "filename", None)
        try:
            rel = str(Path(p).relative_to(vault)) if p else "<unknown>"
        except ValueError:
            rel = str(p)
        unreadable.append(f"{rel} (directory)")

    for root, dirs, files in os.walk(vault, onerror=_onerror):
        rel_root = Path(root).relative_to(vault)
        # Prune in place so os.walk never descends into a skipped tree. This is
        # the ONLY skip check: a second `any(part in SKIP_DIR_NAMES ...)` on
        # rel_root was unreachable once pruning exists (walk can never hand us a
        # root under a pruned name), and mutating it away changed nothing.
        dirs[:] = [d for d in dirs if d not in SKIP_DIR_NAMES]
        for name in files:
            p = Path(root) / name
            if p.suffix.lower() not in SCAN_SUFFIXES:
                continue
            if p in own_files:
                continue        # never harvest our own output back in
            # No "." special case: pathlib already normalises Path(".") / "x"
            # to Path("x"), so the conditional both branches produced was
            # identical (mutating the comparison changed no output).
            rel_path = rel_root / name
            try:
                if p.stat().st_mtime < cutoff:
                    continue
                findings.extend(scan_file(p, str(rel_path)))
            except OSError:
                unreadable.append(str(rel_path))
    return findings, unreadable


def existing_pattern_titles(vault: Path) -> list[str]:
    d = vault / PATTERNS_SUBDIR
    # mutequiv: `not d.is_dir()` -> False. With the early return removed,
    # Path.glob on a missing directory yields nothing rather than raising, so
    # the function still returns []. Kept as an explicit statement of intent.
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.md") if not p.stem.startswith("_"))


_STOP = {"the", "a", "an", "is", "it", "to", "of", "and", "or", "in", "on", "for",
         "that", "not", "be", "must", "never", "always", "you", "your", "this",
         "with", "at", "by", "as", "its", "any", "every", "from", "into", "than"}


def coverage_hint(rule: str, titles: list[str]) -> str | None:
    """Best-effort 'we may already have this' note. A HINT ONLY — it is printed
    beside the candidate and never filters it out. Token overlap is a weak
    signal, and a wrong auto-drop loses the lesson permanently, so the weak
    signal is only ever allowed to add a sentence a human reads."""
    words = {w for w in re.findall(r"[a-z]{3,}", normalize(rule)) if w not in _STOP}
    # mutequiv: `not words` -> False. With the guard removed, an empty word set
    # intersects every title to 0, so `n > best_n` never fires, best_n stays 0
    # and the `>= 2` gate below returns None anyway. Same answer, more work.
    if not words:
        return None
    # mutequiv: initial best_n 0 -> 1. The final gate is `best_n >= 2`, so a
    # title needs 2+ shared tokens to be reported either way; starting at 1 only
    # skips recording a 1-token "best" that could never be emitted. NOT true for
    # 0 -> 2, which is KILLED.
    best, best_n = None, 0
    for t in titles:
        tw = {w for w in re.findall(r"[a-z]{3,}", t.lower()) if w not in _STOP}
        n = len(words & tw)
        if n > best_n:
            best, best_n = t, n
    return best if best_n >= 2 else None


def load_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # A corrupt state file must not wipe the queue. Refuse rather than
        # silently start from empty — starting empty would re-open every
        # already-rejected candidate and drop every carried-forward one.
        raise SystemExit(f"harvest: state file unreadable/corrupt: {path}")
    if not isinstance(data, dict):
        # Same refusal for VALID json of the wrong shape (`[]`, `null`, a bare
        # string). The old `else {}` fallback was the precise silent-reset the
        # comment above forbids, and it triggered on the designed workflow —
        # this is the file a human hand-edits to set promoted/rejected.
        raise SystemExit(
            f"harvest: state file is {type(data).__name__}, expected object: {path}")
    return data


def partition_state(state: dict) -> tuple[dict, list[str]]:
    """Split state into usable entries and unusable ones.

    A human edits this file by hand, so a bare string or a missing `rule` is a
    realistic typo. Previously either one crashed the harvest with an uncaught
    AttributeError/KeyError — loud, so no data was lost, but it bricked the run
    behind a traceback. Keep going, and report the bad rows in the queue where
    the person who typed them will see them.
    """
    good: dict = {}
    bad: list[str] = []
    for h, e in state.items():
        if not isinstance(e, dict):
            bad.append(f"`{h}` — expected an object, got {type(e).__name__}")
            continue
        if not isinstance(e.get("rule"), str) or not e["rule"].strip():
            bad.append(f"`{h}` — missing or empty `rule`")
            continue
        # Every field the renderer or merger touches structurally must be the
        # right TYPE, not merely present. Validating `rule` alone closed the
        # instance and left the class open: `"sources": 5` raised TypeError in
        # render_queue, `"sources": "a"` raised AttributeError on the next
        # sighting, and a string `sources` silently rendered one bullet PER
        # CHARACTER before crashing later. All loud and lossless, but a
        # traceback still bricks the run — on the one file a human hand-edits.
        wrong = [k for k in ("sources", "evidence")
                 if k in e and not isinstance(e[k], list)]
        if wrong:
            bad.append(f"`{h}` — {', '.join(wrong)} must be a list")
            continue
        # `status` LAST but most important: it is the ONLY field the queue's own
        # instructions tell a human to edit, and it was the only one not checked —
        # the three above are machine-written and never hand-touched. render_queue
        # filters `status == "pending"`, so "Pending", "pendign", "", null and 5
        # all made the candidate DISAPPEAR with rc=75 "healthy, stay quiet" and
        # the routine reporting "no pending candidates". Exactly the false
        # assurance this module's docstring calls worse than not shipping.
        # Safe to route to `bad` only because unreadable rows are now PRESERVED on
        # write (see run()); before that, this would have turned a silent hide
        # into a silent delete.
        if e.get("status", "pending") not in VALID_STATUS:
            bad.append(f"`{h}` — unknown status {e.get('status')!r}; expected one "
                       f"of {', '.join(sorted(VALID_STATUS))}")
            continue
        good[h] = e
    return good, bad


def merge_state(state: dict, findings: list[Finding], today: str) -> dict:
    """Union previously-known candidates with this run's finds.

    Carrying state forward is what stops a lesson from ageing out of the mtime
    window unnoticed. Status is only ever set to `pending` HERE; `promoted` and
    `rejected` are written by a human editing the state file, and are never
    reset by a later run that re-encounters the same line.
    """
    out = dict(state)
    for f in findings:
        if f.kind != "ok":
            continue
        h = rule_hash(f.rule)
        entry = out.get(h)
        if entry is None:
            out[h] = {"rule": f.rule, "first_seen": today, "last_seen": today,
                      "status": "pending", "sources": [f.ref],
                      "evidence": [f.evidence]}
            continue
        entry["last_seen"] = today
        if f.ref not in entry.get("sources", []):
            entry.setdefault("sources", []).append(f.ref)
            del entry["sources"][:-MAX_REFS]
        if f.evidence not in entry.get("evidence", []):
            entry.setdefault("evidence", []).append(f.evidence)
            del entry["evidence"][:-MAX_REFS]
    return out


def render_queue(state: dict, findings: list[Finding], unreadable: list[str],
                 titles: list[str], today: str, days: float,
                 bad_state: list[str] | None = None) -> tuple[str, int]:
    bad_state = bad_state or []
    pending = [(h, e) for h, e in state.items()
               if e.get("status", "pending") == "pending"]
    # str() the sort key: a hand-edited `"first_seen": null` (or a bare int) made
    # this raise TypeError comparing str to NoneType as soon as a SECOND row
    # existed — invisible with one row, so it would surface later and elsewhere.
    pending.sort(key=lambda kv: (str(kv[1].get("first_seen", "")), kv[0]))
    malformed = [f for f in findings if f.kind == "malformed"]
    placeholders = [f for f in findings if f.kind == "placeholder"]
    mentions = [f for f in findings if f.kind == "mention"]

    L = []
    L.append("---")
    L.append(f"date: {today}")
    L.append("type: queue")
    L.append("status: living")
    L.append("purpose: Pending PATTERN CANDIDATE lines harvested from agent output, "
             "awaiting promotion to a Pattern note")
    L.append("tags:")
    L.append("  - meta/queue")
    L.append("  - vault/knowledge-graph")
    L.append("---")
    L.append("")
    L.append("# Pattern candidates — review queue")
    L.append("")
    L.append(f"Generated {today} by `scripts/harvest_pattern_candidates.py` "
             f"(mtime window: {days:g}d). **Nothing here is canon yet.**")
    L.append("")
    L.append("To promote: write the Pattern note in `_Iris_Memory/Patterns/`, add its "
             "row to [[_Patterns_MOC]], then set this candidate's `status` to "
             f"`promoted` in `{STATE_NAME}`. To decline: set `status` to `rejected` "
             "(with a note). Anything left `pending` is carried forward every run — "
             "candidates do not age out.")
    L.append("")
    if not pending:
        L.append("_No pending candidates._")
        L.append("")
    for h, e in pending:
        # Collapse whitespace: a rule carrying a newline (possible via a
        # hand-edited state file) split the `## ` heading across lines and broke
        # the document structure every consumer reads.
        # partition_state guarantees `rule` is a non-empty str, so no str() here.
        L.append(f"## `{h}` — {' '.join(e['rule'].split())}")
        L.append("")
        L.append(f"- **First seen:** {e.get('first_seen','?')}  "
                 f"**Last seen:** {e.get('last_seen','?')}")
        for ev in e.get("evidence", []):
            L.append(f"- **Evidence:** {ev}")
        # f-string, not `'`' + s + '`'`: string concatenation raised TypeError on
        # `"sources": [5]` / `[None]` / `[{...}]`, so validating the CONTAINER
        # type closed the field and left the ELEMENTS open. An f-string
        # stringifies anything, which closes the whole class without another
        # validator — a hand-edit typo now renders oddly instead of crashing.
        L.append(f"- **Source:** {', '.join(f'`{s}`' for s in e.get('sources', []))}")
        hint = coverage_hint(e["rule"], titles)
        if hint:
            L.append(f"- ⚠️ **Possible existing coverage:** [[{hint}]] — "
                     "check before writing a second note (hint only, not a verdict)")
        L.append("")
    if malformed:
        L.append("## ⚠️ Malformed markers (not queued — fix the emitting agent)")
        L.append("")
        L.append("These lines name the marker but could not be parsed. They are "
                 "listed rather than skipped: the agent did the hard part by "
                 "noticing, and a silent drop is the failure this script prevents.")
        L.append("")
        for f in malformed:
            L.append(f"- `{f.ref}` — {f.raw}")
        L.append("")
    if placeholders or mentions:
        n = len(placeholders) + len(mentions)
        L.append(f"## Not candidates ({n}) — template text and prose mentions")
        L.append("")
        L.append("The marker quoted in documentation, or named mid-sentence in "
                 "prose, rather than emitted by a run. Each line is echoed in "
                 "full so a real lesson misfiled here is still visible — nothing "
                 "is dropped silently, it is just not being treated as a lesson.")
        L.append("")
        for f in placeholders:
            L.append(f"- `{f.ref}` — template placeholder — {f.raw}")
        for f in mentions:
            L.append(f"- `{f.ref}` — prose mention — {f.raw}")
        L.append("")
    if bad_state:
        L.append(f"## ⚠️ Unusable state entries ({len(bad_state)}) — hand-edit typos")
        L.append("")
        L.append(f"Rows in `{STATE_NAME}` this run could not read. They are skipped, "
                 "not deleted — fix the row and they return.")
        L.append("")
        for b in bad_state:
            L.append(f"- {b}")
        L.append("")
    if unreadable:
        L.append(f"## ⚠️ Unreadable paths ({len(unreadable)}) — scan was INCOMPLETE")
        L.append("")
        L.append("A candidate may exist here and not be listed above. This run is "
                 "**not** a clean bill of health.")
        L.append("")
        for u in unreadable:
            L.append(f"- `{u}`")
        L.append("")
    return "\n".join(L).rstrip() + "\n", len(pending)


def _write_atomic(path: Path, text: str) -> None:
    """Write via temp + os.replace. `write_text` truncates first, so a crash or
    a full disk mid-write leaves truncated JSON — which load_state (correctly)
    now refuses, turning a transient failure into a permanent hard stop with no
    copy of the promoted/rejected history."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def run(vault: Path, days: float, dry_run: bool, now: float) -> int:
    patterns_dir = vault / PATTERNS_SUBDIR
    if not patterns_dir.is_dir():
        print(f"harvest: patterns dir not found: {patterns_dir}", file=sys.stderr)
        return 1
    state_path = patterns_dir / STATE_NAME
    queue_path = patterns_dir / QUEUE_NAME
    today = time.strftime("%Y-%m-%d", time.localtime(now))

    findings, unreadable = scan(vault, days, now)
    raw_state = load_state(state_path)
    good_state, bad_state = partition_state(raw_state)
    # Rows partition_state could not read are held aside VERBATIM and written back
    # untouched below. Without this, they were simply absent from the rebuilt
    # state and the next write DELETED them — while the queue printed "skipped,
    # not deleted — fix the row and they return." That promise was false, and the
    # gardener rewrites the queue daily, so the one report naming the row expired
    # after a single morning: the rule text AND the human's promoted/rejected
    # decision were gone with no trace. Held out of `good_state` (not merged back
    # in) on purpose — a bare-string row hits `.get("status")` in render_queue and
    # raises AttributeError, so it must never re-enter the read path.
    preserved = {k: v for k, v in raw_state.items() if k not in good_state}
    state = merge_state(good_state, findings, today)
    # A quarantined key must also be kept OUT of the merged result, not merely
    # held aside. Preserving alone was not enough: a bad key is absent from
    # `good_state`, so merge_state sees the still-live emission as brand new and
    # re-creates the SAME key as a fresh `pending` row — which then won the merge
    # and silently reset the human's `promoted`/`rejected` back to pending, with
    # first_seen bumped to today and no report on the following run. That is the
    # round-4 data-loss narrative re-entering through its own fix.
    # Not a corner case: an emission stays in the window for 30 days after its
    # file's mtime (indefinitely for a file that keeps being touched, like the
    # bridge), so every row created in the first month collides.
    # Note the obvious alternative is WRONG: flipping the merge to
    # {**state, **preserved} keeps the decision but render_queue is handed
    # `state`, so the queue would list a pending candidate the state file does not
    # hold — a phantom that vanishes when the emission ages out.
    state = {k: v for k, v in state.items() if k not in preserved}
    titles = existing_pattern_titles(vault)
    text, n_pending = render_queue(state, findings, unreadable, titles, today,
                                   days, bad_state)

    n_new = sum(1 for f in findings if f.kind == "ok")
    n_bad = sum(1 for f in findings if f.kind == "malformed")
    # An INCOMPLETE scan or an unusable state row needs a human just as much as a
    # pending candidate does. Leaving `unreadable` out of this expression meant a
    # permission error returned 75 = "healthy, stay quiet" — and the routine then
    # told Steve "no pending candidates". Non-theoretical here: vault reads fail
    # with EDEADLK (see Patterns/Vault_file_read_EDEADLK_provenance_xattr.md).
    needs_human = bool(n_pending or n_bad or unreadable or bad_state)
    if dry_run:
        print(text)
        print(f"\n(dry-run — nothing written. {n_pending} pending, "
              f"{n_new} marker(s) in window, {n_bad} malformed.)")
        return 0 if needs_human else 75

    _write_atomic(queue_path, text)
    # The two dicts are disjoint by construction (quarantined keys were removed
    # from `state` above), so merge ORDER here is irrelevant — which is the point:
    # an earlier version depended on the ordering and got it wrong in the
    # collision case.
    payload = {**preserved, **state}
    # mutequiv: indent=2 -> any other int. The state file is machine-read back
    # through json.loads (load_state), so indentation has no observable effect on
    # any behaviour a test could assert. Cosmetic only. (Kept on the SAME line as
    # the literal — the gate attaches a marker to the adjacent line, so splitting
    # this call across two lines silently detached it and the mutant came back
    # unclassified.)
    _write_atomic(state_path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(f"harvest: {n_pending} pending candidate(s), {n_new} marker(s) in a "
          f"{days:g}d window, {n_bad} malformed → {queue_path}")
    if bad_state:
        print(f"harvest: WARNING {len(bad_state)} unusable state entr(ies) skipped")
    if unreadable:
        print(f"harvest: WARNING scan incomplete — {len(unreadable)} unreadable path(s)")
    return 0 if needs_human else 75


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Harvest PATTERN CANDIDATE lines "
                                             "into a review queue")
    ap.add_argument("--vault", type=Path, required=True,
                    help=f"vault root to scan, e.g. {VAULT_HINT}. REQUIRED on "
                         "purpose: an implicit production default turned every "
                         "importer of this module into a production writer.")
    ap.add_argument("--days", type=float, default=30.0,
                    help="only scan files modified within this many days "
                         "(default 30; pending candidates are carried forward "
                         "regardless, so this bounds the scan, not the queue)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    return run(a.vault, a.days, a.dry_run, time.time())


# NOTE: no `mutequiv:` token here any more, deliberately. All three mutants on
# this line are now KILLED by the subprocess CLI test, and `mutate.py` consults an
# equivalence marker ONLY for survivors — so a marker on a killed line lies
# dormant and would silently auto-accept the mutant the day that test is deleted
# or weakened, with no reviewer ever seeing it. The prose is kept because the
# history matters; the token is not, because it is a trap set for later.
#
# History: an earlier marker here claimed all three were unobservable. Those
# make main() run AT IMPORT, and the earlier marker asserting they were
# unobservable was simply wrong — they wrote the live vault and exited 0, which
# the gate scored as a passing suite. They are now killed rather than excused:
# with `--vault` required, an import-time main() exits 2 on the missing argument
# and touches nothing. See the VAULT_HINT comment for the full account.
if __name__ == "__main__":
    sys.exit(main())
