#!/usr/bin/env python3
"""Deterministic apply core for the ADAPTS loop (Build: self-adapting agents).

The `adaptation-proposer` agent reads the LEARNS signals (channel-analyst
diagnoses, youtube-researcher intel, image/script-reviewer verdicts, lint flags)
and drafts surgical, single-file, exact old->new edits to a *whitelisted* set of
files — the agent prompts that steer every dispatch + the canonical content
standards. Each draft lands as one markdown proposal in the queue dir. THIS
module is the deterministic gate that applies or rejects them: it never trusts
the (LLM-authored) proposal's claim about what it targets — it re-validates the
target against the allowlist, requires the old text to match exactly once, backs
up before writing, and records every action in an append-only log.

Shared by two callers:
  - the iris.py daemon's `/adapt` Telegram handler (importlib-loaded), so Steve
    approves from his phone (one-tap gate);
  - this file's own CLI, for at-keyboard use / the scheduled scan's smoke checks.

  python3 adaptation.py --list
  python3 adaptation.py --show <id>
  python3 adaptation.py --apply <id>
  python3 adaptation.py --reject <id> [--reason "..."]
  python3 adaptation.py --selftest

Design notes:
  - Stdlib only (urllib-free); the daemon imports it without adding deps.
  - The allowlist is enforced HERE, at apply time, on the real (symlink-resolved)
    path — the proposer is untrusted input. A proposal targeting anything outside
    the allowlist is refused even if the agent wrote it.
  - "Exactly once" is load-bearing: a 0-match means the file drifted since the
    proposal was drafted (stale — refuse, don't guess); a >1-match means the edit
    is ambiguous (refuse — the agent must quote more context).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

# --- Locations ---------------------------------------------------------------
VAULT = Path("/Users/steve/Documents/3SK/outputs")
AGENTS_DIR = (Path.home() / ".claude" / "agents").resolve()

# Canonical content-standard markdown files the proposer may retune. Curated on
# purpose (not a glob): these are the docs that actually steer generation and are
# plain markdown. The Brand Bible is intentionally absent — it's a .docx (binary),
# so it can't take a text old->new edit; a Brand-Bible change stays a human task.
_STANDARDS = [
    VAULT / "BRANDS/3SK_Finance/Discoverability_Playbook.md",
    VAULT / "BRANDS/3SK_Finance/Character_Reference/Master_Character_Prompt.md",
    VAULT / "BRANDS/3SK_Finance/Character_Reference/On_Model_Verification_Protocol.md",
]
ALLOWED_STANDARDS = {p.resolve() for p in _STANDARDS}

# Content-pipeline agent prompts the proposer may retune — an EXPLICIT allowlist,
# not a glob over ~/.claude/agents/. The agents dir holds ~26 files including this
# proposer's own guardrails and the engineering/review agents (skeptical-code-
# reviewer, senior-engineer, etc.); a glob would let an injected proposal rewrite
# the safety machinery itself. The deterministic core is the trust boundary, so
# the constraint lives HERE (not only in the proposer prompt, which is assumed
# hostile). Only these basenames — the agents that actually shape video output —
# are editable. adaptation-proposer.md is deliberately absent: no self-modification.
ALLOWED_AGENT_NAMES = frozenset({
    "scriptwriter.md",
    "packaging-strategist.md",
    "thumbnail-coordinator.md",
    "scene-image-prompt-generator.md",
    "video-description-writer.md",
    "script-reviewer.md",
    "image-reviewer.md",
})

# Proposal queue lives in the vault (the shared brain): visible to Steve, readable
# by the daemon, git-free so a half-write can't dirty a repo.
ADAPT_DIR = VAULT / "06_CEO" / "Adaptations"
QUEUE_DIR = ADAPT_DIR / "queue"
APPLIED_DIR = ADAPT_DIR / "applied"
REJECTED_DIR = ADAPT_DIR / "rejected"
LOG_FILE = ADAPT_DIR / "_log.md"

# Proposal ids are used to build file paths — keep them path-safe.
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

OLD_OPEN, OLD_CLOSE = "<<<ADAPT:OLD", "ADAPT:OLD>>>"
NEW_OPEN, NEW_CLOSE = "<<<ADAPT:NEW", "ADAPT:NEW>>>"

# --- Outcome tagging (closing the learning loop) -----------------------------
# An applied adaptation is a HYPOTHESIS ("this edit will move metric X"), not a
# proven win. Outcome tagging records whether it actually helped, so the loop is
# measurable rather than fire-and-forget. At apply time we stamp an
# `evaluate_after` date (apply date + horizon); once that date passes, the
# adaptation is "due" for an outcome call, recorded with tag_outcome(). The
# horizon is deliberately short — internal QC signals (reviewer block-rate, lint
# flags) mature in days; slower signals (CTR/retention) can be re-tagged later.
EVAL_HORIZON_DAYS = 14
# The closed vocabulary of an outcome verdict. "inconclusive" is honest — many
# pre-launch adaptations have no clean metric yet; it is NOT a failure, just an
# unmeasured change. Only improved/no-change/regressed feed the hit-rate.
OUTCOME_VALUES = ("improved", "no-change", "regressed", "inconclusive")

# --- Evidence floor (sample-size gate: observational → experimental) ----------
# A signal seen on a handful of views is noise, not a law. A permanent canon/agent
# edit drafted off a thin sample overfits — the right home for a thin signal is the
# A/B ledger (test it), not the prompt that steers EVERY future video. The proposer
# is instructed to route thin signals to experiments; this core is the backstop: it
# parses the sample size the proposer recorded and flags a thin-evidence edit so
# Steve (and the daemon) see it BEFORE applying. SOFT gate by design — an explicit
# `/adapt approve` still wins (Steve is sovereign) — but he approves with eyes open,
# and the apply log records that a thin-evidence edit went in. A high-confidence
# severe single miss (an off-model hero shot, a banned-vocab leak) is exempt: n=1 is
# enough when the defect is categorical, not statistical.
MIN_EVIDENCE_N = 1000   # views/impressions/sample behind a signal to call it non-thin
# Recognized `evidence_basis:` tokens that EXEMPT a proposal from the thin flag even
# at small n — the miss is categorical (one occurrence proves the rule), not a trend.
EVIDENCE_EXEMPT_TOKENS = ("severe-single-miss", "categorical", "compliance", "primary-source")

# --- Risk-tiered auto-apply (capability; INERT by default) --------------------
# Most proposals shape voice/strategy and MUST stay human-gated via /adapt. A narrow
# class is mechanical + independently machine-verified (e.g. a stale IRS figure
# confirmed against a primary source by figures-watcher). Such a proposal may carry
# `tier: auto` + a `verified_by:` token naming the trusted verifier. apply_auto_pending()
# applies ONLY those, ONLY when explicitly enabled, capped per run, reusing the full
# apply_proposal safety path. It is OFF unless ADAPT_AUTO_APPLY=1 AND the proposal's
# verified_by is in the trusted set below — nothing auto-applies until a producer
# emits the token AND Steve flips the switch. This is the scaffolding for safe
# autonomy, deliberately gated shut, not a live behavior change.
AUTO_APPLY_VERIFIERS = frozenset({"figures-watcher"})
AUTO_APPLY_MAX_PER_RUN = 3


class AdaptError(Exception):
    """Any refusal/validation failure. Carries a one-line, user-facing reason."""


@dataclass
class Proposal:
    pid: str
    path: Path  # the proposal markdown file on disk
    meta: dict
    old: str
    new: str

    @property
    def target(self) -> str:
        return self.meta.get("target", "")

    @property
    def summary(self) -> str:
        return self.meta.get("summary", "(no summary)")

    @property
    def tier(self) -> str:
        """Apply tier: 'gated' (default — needs /adapt) or 'auto' (eligible for the
        inert-by-default auto-apply path; see apply_auto_pending)."""
        return (self.meta.get("tier") or "gated").strip().lower()

    @property
    def evidence_basis(self) -> str:
        return self.meta.get("evidence_basis", "")

    @property
    def verified_by(self) -> str:
        """The trusted verifier that machine-confirmed this edit (auto-apply gate)."""
        return (self.meta.get("verified_by") or "").strip().lower()

    @property
    def evidence_n(self) -> int | None:
        """The sample size behind the signal (views/impressions/occurrences), or None
        if the proposer didn't record one. Parsed leniently from `evidence_n:` so
        '1,200', '~1200 impressions', or '4 views' all read as an int."""
        raw = self.meta.get("evidence_n")
        if raw is None or str(raw).strip() == "":
            return None
        m = re.search(r"\d[\d,]*", str(raw))
        if not m:
            return None
        try:
            return int(m.group(0).replace(",", ""))
        except ValueError:
            return None

    @property
    def thin_evidence(self) -> bool:
        """True when the recorded sample is below MIN_EVIDENCE_N and the miss is NOT
        flagged categorical/compliance via evidence_basis. Unknown sample (None) is
        NOT flagged here — that's surfaced separately as 'evidence_n missing' so a
        proposer omission can't masquerade as a clean signal."""
        n = self.evidence_n
        if n is None or n >= MIN_EVIDENCE_N:
            return False
        basis = self.evidence_basis.lower()
        if any(tok in basis for tok in EVIDENCE_EXEMPT_TOKENS):
            return False
        return True


# --- Parsing -----------------------------------------------------------------
def _parse_frontmatter(text: str) -> dict:
    """Parse the leading `--- ... ---` YAML-ish block as flat key: value pairs.

    Deliberately minimal (no nested structures) — proposal frontmatter is flat.
    """
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    block = text[3:end].strip("\n")
    meta: dict = {}
    for line in block.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, val = line.split(":", 1)
        meta[key.strip()] = val.strip().strip('"').strip("'")
    return meta


def _extract_block(text: str, open_tag: str, close_tag: str, which: str) -> str:
    """Return the verbatim text between a single open/close marker pair.

    The marker line itself is consumed; the inner content is returned EXACTLY
    (only the one newline immediately after the open tag and before the close tag
    is stripped, so the markers can sit on their own lines without injecting blank
    lines into the payload).
    """
    oi = text.find(open_tag)
    if oi == -1:
        raise AdaptError(f"proposal missing {which} block ({open_tag})")
    if text.find(open_tag, oi + len(open_tag)) != -1:
        raise AdaptError(f"proposal has more than one {which} block")
    start = oi + len(open_tag)
    ci = text.find(close_tag, start)
    if ci == -1:
        raise AdaptError(f"proposal {which} block not closed ({close_tag})")
    body = text[start:ci]
    # Drop exactly one leading and one trailing newline introduced by putting the
    # markers on their own lines. Preserve all other whitespace verbatim.
    if body.startswith("\n"):
        body = body[1:]
    if body.endswith("\n"):
        body = body[:-1]
    return body


def _load_proposal_file(path: Path) -> Proposal:
    text = path.read_text(encoding="utf-8")
    meta = _parse_frontmatter(text)
    pid = meta.get("id") or path.stem
    old = _extract_block(text, OLD_OPEN, OLD_CLOSE, "OLD")
    new = _extract_block(text, NEW_OPEN, NEW_CLOSE, "NEW")
    return Proposal(pid=pid, path=path, meta=meta, old=old, new=new)


def _validate_id(pid: str) -> None:
    if not _ID_RE.match(pid):
        raise AdaptError(f"invalid proposal id {pid!r} (allowed: letters/digits . _ -)")


def _queue_path(pid: str) -> Path:
    _validate_id(pid)
    p = (QUEUE_DIR / f"{pid}.md").resolve()
    # Defense in depth: the resolved path must still live in the queue dir.
    if p.parent != QUEUE_DIR.resolve():
        raise AdaptError(f"proposal id {pid!r} escapes the queue dir")
    if not p.is_file():
        raise AdaptError(f"no pending proposal with id {pid!r} (see --list)")
    return p


# --- Allowlist enforcement (the security gate) -------------------------------
def _validate_target(target_str: str) -> Path:
    """Resolve + authorize the edit target. Raises AdaptError on any violation.

    Enforced on the symlink-resolved real path so a symlink in the queue can't
    point the edit at an out-of-scope file.
    """
    if not target_str:
        raise AdaptError("proposal has no `target:`")
    raw = Path(os.path.expanduser(target_str))
    if not raw.is_absolute():
        raise AdaptError(f"target must be an absolute path, got {target_str!r}")
    real = raw.resolve()
    if not real.is_file():
        raise AdaptError(f"target does not exist: {real}")
    if real.suffix != ".md":
        raise AdaptError(f"target is not a .md file: {real}")
    in_agents = real.parent == AGENTS_DIR and real.name in ALLOWED_AGENT_NAMES
    in_standards = real in ALLOWED_STANDARDS
    if not (in_agents or in_standards):
        raise AdaptError(
            f"target not in allowlist (content-pipeline agent prompts or curated "
            f"standards only): {real}"
        )
    return real


# --- Public API --------------------------------------------------------------
def ensure_dirs() -> None:
    for d in (QUEUE_DIR, APPLIED_DIR, REJECTED_DIR):
        d.mkdir(parents=True, exist_ok=True)


def list_proposals() -> list[Proposal]:
    if not QUEUE_DIR.is_dir():
        return []
    out = []
    for f in sorted(QUEUE_DIR.glob("*.md")):
        try:
            out.append(_load_proposal_file(f))
        except AdaptError:
            # A malformed file in the queue shouldn't crash listing; skip it.
            continue
    return out


def show_proposal(pid: str) -> str:
    prop = _load_proposal_file(_queue_path(pid))
    if prop.thin_evidence:
        ev = f"{prop.evidence_n} ⚠ thin (< {MIN_EVIDENCE_N} — A/B-test before baking in)"
    elif prop.evidence_n is None:
        ev = "not recorded ⚠"
    else:
        ev = str(prop.evidence_n)
    lines = [
        f"Proposal {prop.pid}",
        f"  target:     {prop.target}",
        f"  confidence: {prop.meta.get('confidence', '?')}",
        f"  evidence_n: {ev}",
        f"  tier:       {prop.tier}",
        f"  signal:     {prop.meta.get('signal_source', '?')}",
        f"  summary:    {prop.summary}",
        "",
        "--- OLD (exact current text) ---",
        prop.old,
        "",
        "--- NEW (replacement) ---",
        prop.new,
    ]
    return "\n".join(lines)


def _now() -> str:
    return _dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")


def _append_log(action: str, prop: Proposal, detail: str = "") -> None:
    ADAPT_DIR.mkdir(parents=True, exist_ok=True)
    if not LOG_FILE.exists():
        LOG_FILE.write_text("# Adaptation audit log\n\nAppend-only. Newest at bottom.\n\n", encoding="utf-8")
    entry = f"- {_now()} — **{action}** `{prop.pid}` → `{prop.target}` — {prop.summary}"
    if detail:
        entry += f" ({detail})"
    with LOG_FILE.open("a", encoding="utf-8") as fh:
        fh.write(entry + "\n")


def _atomic_write(path: Path, content: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(content)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _set_frontmatter_field(text: str, key: str, value: str) -> str:
    """Set `key: value` in the LEADING frontmatter block only — replace the field's
    line there, or inject it right after the opening `---` fence.

    Scoped to the frontmatter block on purpose: a naive `(?m)^key:` over the whole
    document would also match a line in the rationale/payload (e.g. a NEW block that
    legitimately quotes `outcome: ...`), silently corrupting the proposal record and
    leaving the real frontmatter field unstamped. We operate ONLY on the region
    before the closing fence, matching _parse_frontmatter's own bounds.

    Uses a replacement FUNCTION, not a string, so a value containing backslashes or
    `\\1`-style sequences is written literally, never interpreted as a regex backref.
    """
    # No (or malformed) frontmatter: prepend a fresh block. Defensive — every real
    # proposal already carries frontmatter — but never silently drops the field.
    if not text.startswith("---"):
        return f"---\n{key}: {value}\n---\n{text}"
    end = text.find("\n---", 3)
    if end == -1:
        return f"---\n{key}: {value}\n---\n{text}"
    head, tail = text[:end], text[end:]  # head = opening fence + fields; tail = closing fence onward
    pat = re.compile(rf"(?m)^{re.escape(key)}:.*$")
    if pat.search(head):
        head = pat.sub(lambda _m: f"{key}: {value}", head, count=1)
    else:
        # inject right after the opening "---\n" fence (^ = start of string, no re.M)
        head = re.sub(r"^---\n", lambda _m: f"---\n{key}: {value}\n", head, count=1)
    return head + tail


def _move_proposal(prop: Proposal, dest_dir: Path, new_status: str, extra: dict) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    text = prop.path.read_text(encoding="utf-8")
    # Update the status line in frontmatter (or inject one), plus extra stamps.
    text = _set_frontmatter_field(text, "status", new_status)
    for k, v in extra.items():
        text = _set_frontmatter_field(text, k, v)
    dest = dest_dir / prop.path.name
    dest.write_text(text, encoding="utf-8")
    prop.path.unlink()
    return dest


def apply_proposal(pid: str) -> dict:
    """Apply one pending proposal. Returns a result dict on success; raises
    AdaptError (with a one-line reason) on any refusal — nothing is mutated on a
    refusal."""
    prop = _load_proposal_file(_queue_path(pid))
    target = _validate_target(prop.target)
    if prop.old == prop.new:
        raise AdaptError("OLD and NEW are identical — nothing to change")
    # Read preserving original newlines (newline="") so a CRLF target isn't
    # silently LF-normalized whole-file by universal-newline translation — only
    # the matched span should change. _atomic_write writes back verbatim to match.
    with target.open("r", encoding="utf-8", newline="") as fh:
        current = fh.read()
    n = current.count(prop.old)
    if n == 0:
        raise AdaptError(
            "OLD text not found in target — the file changed since this was drafted; "
            "re-run the scan to regenerate the proposal"
        )
    if n > 1:
        raise AdaptError(
            f"OLD text matches {n} places in target — ambiguous; proposal must quote "
            "more surrounding context"
        )
    # Soft sample-size gate: surface (never block) a thin-evidence content edit so an
    # approver sees it went in over a weak signal. Steve's approval is sovereign.
    warnings: list[str] = []
    if prop.thin_evidence:
        warnings.append(
            f"thin evidence (n={prop.evidence_n} < {MIN_EVIDENCE_N}) — this signal would be "
            "better A/B-tested before being baked into canon"
        )
    elif prop.evidence_n is None:
        warnings.append("evidence_n not recorded — sample size behind this signal is unknown")
    # Backup, then atomic single-occurrence replace.
    backup = target.with_name(f"{target.name}.bak-pre-adapt-{prop.pid}")
    shutil.copy2(target, backup)
    updated = current.replace(prop.old, prop.new, 1)
    _atomic_write(target, updated)
    # Stamp the outcome-evaluation horizon: this applied edit is a HYPOTHESIS to be
    # checked on/after this date (see tag_outcome / due_for_review). ISO dates sort
    # lexicographically, so the due-check is a plain string compare.
    eval_after = (_dt.date.today() + _dt.timedelta(days=EVAL_HORIZON_DAYS)).isoformat()
    moved = _move_proposal(
        prop, APPLIED_DIR, "applied",
        {"applied": _now(), "backup": str(backup), "evaluate_after": eval_after},
    )
    detail = f"backup {backup.name}; evaluate_after {eval_after}"
    if warnings:
        detail += f"; WARN: {'; '.join(warnings)}"
    _append_log("APPLIED", prop, detail)
    return {
        "pid": prop.pid,
        "target": str(target),
        "backup": str(backup),
        "proposal": str(moved),
        "summary": prop.summary,
        "evaluate_after": eval_after,
        "warnings": warnings,
    }


def reject_proposal(pid: str, reason: str = "") -> dict:
    prop = _load_proposal_file(_queue_path(pid))
    extra = {"rejected": _now()}
    if reason:
        extra["reject_reason"] = reason.replace("\n", " ")
    moved = _move_proposal(prop, REJECTED_DIR, "rejected", extra)
    _append_log("REJECTED", prop, reason)
    return {"pid": prop.pid, "proposal": str(moved), "summary": prop.summary}


# --- Outcome tagging: did the applied adaptation actually help? ---------------
def _applied_path(pid: str) -> Path:
    """Resolve an APPLIED proposal's file by id, with the same path-safety guard
    as _queue_path (no traversal, must live in the applied dir)."""
    _validate_id(pid)
    p = (APPLIED_DIR / f"{pid}.md").resolve()
    if p.parent != APPLIED_DIR.resolve():
        raise AdaptError(f"proposal id {pid!r} escapes the applied dir")
    if not p.is_file():
        raise AdaptError(f"no applied proposal with id {pid!r} (see --scoreboard)")
    return p


def _load_applied(path: Path) -> Proposal | None:
    """Load an applied-proposal file, or None if it's malformed (skip, don't crash
    a listing). Applied files have the same structure as queued ones plus extra
    frontmatter stamps."""
    try:
        return _load_proposal_file(path)
    except AdaptError:
        return None


def due_for_review() -> list[Proposal]:
    """Applied adaptations whose evaluate_after date has passed and that carry NO
    outcome yet — i.e. matured hypotheses awaiting a verdict. Adaptations applied
    before outcome tagging existed have no evaluate_after and are skipped (they
    predate the mechanism; nothing to score)."""
    if not APPLIED_DIR.is_dir():
        return []
    today = _dt.date.today().isoformat()
    out = []
    for f in sorted(APPLIED_DIR.glob("*.md")):
        prop = _load_applied(f)
        if prop is None or prop.meta.get("outcome"):
            continue
        ea = prop.meta.get("evaluate_after", "")
        if ea and ea <= today:
            out.append(prop)
    return out


def tag_outcome(pid: str, verdict: str, note: str = "") -> dict:
    """Record the outcome of an applied adaptation. verdict ∈ OUTCOME_VALUES.
    Stamps `outcome` / `outcome_measured` / `outcome_note` into the applied file
    in place (does not move it) and appends to the audit log. Re-tagging is
    allowed — a later, better-evidenced call overwrites an earlier one (logged
    each time), so a slow CTR signal can supersede an early reviewer-block read."""
    v = (verdict or "").strip().lower()
    if v not in OUTCOME_VALUES:
        raise AdaptError(
            f"outcome must be one of {', '.join(OUTCOME_VALUES)} — got {verdict!r}"
        )
    path = _applied_path(pid)
    prop = _load_proposal_file(path)
    text = path.read_text(encoding="utf-8")
    text = _set_frontmatter_field(text, "outcome", v)
    text = _set_frontmatter_field(text, "outcome_measured", _now())
    if note:
        text = _set_frontmatter_field(text, "outcome_note", note.replace("\n", " "))
    _atomic_write(path, text)
    _append_log("OUTCOME", prop, f"{v}" + (f": {note}" if note else ""))
    return {"pid": prop.pid, "outcome": v, "proposal": str(path), "summary": prop.summary}


def scoreboard() -> dict:
    """Tally outcomes across all applied adaptations. Returns counts per verdict,
    the number still untagged, the total, and the hit-rate (improved / decisive),
    where decisive = improved+no-change+regressed (inconclusive + untagged are
    excluded — they carry no signal about whether the loop is net-positive)."""
    counts = {v: 0 for v in OUTCOME_VALUES}
    untagged = 0
    total = 0
    if APPLIED_DIR.is_dir():
        for f in sorted(APPLIED_DIR.glob("*.md")):
            prop = _load_applied(f)
            if prop is None:
                continue
            total += 1
            oc = (prop.meta.get("outcome") or "").strip().lower()
            if oc in counts:
                counts[oc] += 1
            else:
                untagged += 1
    decisive = counts["improved"] + counts["no-change"] + counts["regressed"]
    hit_rate = (counts["improved"] / decisive) if decisive else None
    return {
        "total": total,
        "counts": counts,
        "untagged": untagged,
        "decisive": decisive,
        "hit_rate": hit_rate,
    }


# Repeated-failure flag: a group the loop keeps trying and losing on is a canon
# root-cause signal (the rule it edits is fighting reality), not a cue for more
# retries. Conservative thresholds so a single miss never trips it.
REPEATED_FAILURE_MIN_DECISIVE = 2
REPEATED_FAILURE_MAX_HITRATE = 1.0 / 3.0


def _meta_group(buckets: dict, key: str, outcome: str) -> None:
    g = buckets.setdefault(key, {v: 0 for v in OUTCOME_VALUES})
    if outcome in g:
        g[outcome] += 1


def _meta_finalize(buckets: dict) -> list[dict]:
    """Turn raw per-group counts into ranked rows with hit-rate + repeated-failure
    flag. Sorted worst-first (lowest hit-rate among decisive groups) so the canon
    reviewer reads the problem groups at the top."""
    rows = []
    for key, c in buckets.items():
        decisive = c["improved"] + c["no-change"] + c["regressed"]
        hit = (c["improved"] / decisive) if decisive else None
        rows.append({
            "group": key,
            "counts": c,
            "decisive": decisive,
            "hit_rate": hit,
            "repeated_failure": (
                decisive >= REPEATED_FAILURE_MIN_DECISIVE
                and hit is not None
                and hit <= REPEATED_FAILURE_MAX_HITRATE
            ),
        })
    # worst decisive hit-rate first; groups with no decisive outcomes sink to the end.
    rows.sort(key=lambda r: (r["hit_rate"] is None, r["hit_rate"] if r["hit_rate"] is not None else 1.0))
    return rows


def meta_scoreboard() -> dict:
    """Second-order view over applied-adaptation outcomes: group decisive outcomes
    by TARGET (which agent/standard the loop keeps editing) and by SIGNAL-SOURCE
    class (which kind of signal drives wins vs losses), each with a per-group
    hit-rate and a repeated-failure flag. This is the input the canon reviewer uses
    to ask 'is the canon line itself wrong?' instead of proposing yet another edit
    to a target that keeps regressing."""
    by_target: dict = {}
    by_signal: dict = {}
    if APPLIED_DIR.is_dir():
        for f in sorted(APPLIED_DIR.glob("*.md")):
            prop = _load_applied(f)
            if prop is None:
                continue
            oc = (prop.meta.get("outcome") or "").strip().lower()
            if oc not in OUTCOME_VALUES:
                continue
            tgt = Path(prop.target).name if prop.target else "(no target)"
            sig = prop.meta.get("signal_source", "")
            sig_class = Path(sig).name if sig else "(no signal_source)"
            _meta_group(by_target, tgt, oc)
            _meta_group(by_signal, sig_class, oc)
    target_rows = _meta_finalize(by_target)
    signal_rows = _meta_finalize(by_signal)
    return {
        "by_target": target_rows,
        "by_signal": signal_rows,
        "repeated_failures": [r["group"] for r in target_rows if r["repeated_failure"]],
    }


# --- Risk-tiered auto-apply (capability; inert unless explicitly enabled) ------
def auto_apply_eligible(prop: Proposal) -> bool:
    """A proposal may be auto-applied ONLY if it declares tier:auto AND names a
    trusted machine-verifier in verified_by. Everything else stays human-gated."""
    return prop.tier == "auto" and prop.verified_by in AUTO_APPLY_VERIFIERS


def apply_auto_pending(enabled: bool = False, cap: int = AUTO_APPLY_MAX_PER_RUN) -> list[dict]:
    """Apply queued tier:auto, trusted-verifier proposals — ONLY when `enabled` —
    reusing the full apply_proposal safety path (allowlist, exact-once, backup,
    audit log). Capped per run. Returns the applied results (each stamped auto:True).

    INERT by default: enabled=False (the only caller passes ADAPT_AUTO_APPLY==1) →
    returns [] without touching anything. A non-eligible proposal is left in the
    queue for /adapt. A proposal that fails apply (drift/ambiguity) is skipped, not
    retried — it surfaces normally via /adapt list."""
    if not enabled:
        return []
    out: list[dict] = []
    for prop in list_proposals():
        if len(out) >= cap:
            break
        if not auto_apply_eligible(prop):
            continue
        try:
            res = apply_proposal(prop.pid)
        except AdaptError:
            continue
        res["auto"] = True
        res["verified_by"] = prop.verified_by
        out.append(res)
    return out


# --- CLI ---------------------------------------------------------------------
def _proposal_json(p: Proposal) -> dict:
    """The machine-readable view of a proposal the outcome-evaluator/agents read
    (via --json). Flat, JSON-safe — no Path objects."""
    return {
        "id": p.pid,
        "target": p.target,
        "target_name": Path(p.target).name if p.target else "",
        "summary": p.summary,
        "confidence": p.meta.get("confidence", ""),
        "signal_source": p.meta.get("signal_source", ""),
        "metric": p.meta.get("metric", ""),
        "expected_effect": p.meta.get("expected_effect", ""),
        "applied": p.meta.get("applied", ""),
        "evaluate_after": p.meta.get("evaluate_after", ""),
        "evidence_n": p.evidence_n,
        "evidence_basis": p.evidence_basis,
        "thin_evidence": p.thin_evidence,
        "tier": p.tier,
        "verified_by": p.verified_by,
    }


def _emit_json(obj) -> int:
    import json
    print(json.dumps(obj, indent=2, ensure_ascii=False))
    return 0


def _evidence_tag(p: Proposal) -> str:
    if p.thin_evidence:
        return f"  ⚠ thin evidence (n={p.evidence_n})"
    if p.evidence_n is None:
        return "  ⚠ evidence_n missing"
    return f"  evidence n={p.evidence_n}"


def _cli_list(as_json: bool = False) -> int:
    props = list_proposals()
    if as_json:
        return _emit_json([_proposal_json(p) for p in props])
    if not props:
        print("No pending adaptation proposals.")
        return 0
    print(f"{len(props)} pending adaptation proposal(s):\n")
    for p in props:
        tgt = p.target.replace(str(Path.home()), "~")
        tier = "" if p.tier == "gated" else f" [tier:{p.tier}]"
        print(f"  [{p.pid}] ({p.meta.get('confidence', '?')}){tier} {tgt}")
        print(f"      {p.summary}")
        print(f"    {_evidence_tag(p).strip()}")
    print("\nApply:  python3 adaptation.py --apply <id>")
    return 0


def _cli_review_due(as_json: bool = False) -> int:
    props = due_for_review()
    if as_json:
        return _emit_json([_proposal_json(p) for p in props])
    if not props:
        print("No applied adaptations are due for an outcome review.")
        return 0
    print(f"{len(props)} applied adaptation(s) due for an outcome call:\n")
    for p in props:
        tgt = p.target.replace(str(Path.home()), "~")
        eff = p.meta.get("expected_effect") or p.meta.get("metric") or "(no hypothesis recorded)"
        print(f"  [{p.pid}] applied, due {p.meta.get('evaluate_after', '?')} → {tgt}")
        print(f"      {p.summary}")
        print(f"      metric:   {p.meta.get('metric', '(none)')}")
        print(f"      expected: {eff}")
    print("\nTag:  python3 adaptation.py --tag-outcome <id> --outcome "
          f"<{'|'.join(OUTCOME_VALUES)}> [--note \"...\"]")
    return 0


def _cli_scoreboard(as_json: bool = False) -> int:
    sb = scoreboard()
    if as_json:
        return _emit_json(sb)
    c = sb["counts"]
    print("Adaptation outcome scoreboard")
    print(f"  applied total : {sb['total']}")
    print(f"  improved      : {c['improved']}")
    print(f"  no-change     : {c['no-change']}")
    print(f"  regressed     : {c['regressed']}")
    print(f"  inconclusive  : {c['inconclusive']}")
    print(f"  untagged      : {sb['untagged']}")
    if sb["hit_rate"] is None:
        print("  hit-rate      : n/a (no decisive outcomes yet)")
    else:
        print(f"  hit-rate      : {sb['hit_rate']*100:.0f}% "
              f"({c['improved']}/{sb['decisive']} decisive)")
    return 0


def _cli_meta(as_json: bool = False) -> int:
    ms = meta_scoreboard()
    if as_json:
        return _emit_json(ms)
    def _hr(r):
        return "n/a" if r["hit_rate"] is None else f"{r['hit_rate']*100:.0f}%"
    print("Adaptation meta-scoreboard (second-order patterns)\n")
    print("By target (which agent/standard the loop edits):")
    for r in ms["by_target"]:
        flag = "  ⚠ REPEATED FAILURE" if r["repeated_failure"] else ""
        print(f"  {r['group']}: hit-rate {_hr(r)} over {r['decisive']} decisive{flag}")
    print("\nBy signal-source class:")
    for r in ms["by_signal"]:
        print(f"  {r['group']}: hit-rate {_hr(r)} over {r['decisive']} decisive")
    if ms["repeated_failures"]:
        print("\nRepeated-failure targets (canon root-cause candidates): "
              + ", ".join(ms["repeated_failures"]))
    return 0


def _cli_auto_apply() -> int:
    enabled = os.environ.get("ADAPT_AUTO_APPLY") == "1"
    results = apply_auto_pending(enabled=enabled)
    if not enabled:
        print("Auto-apply is OFF (set ADAPT_AUTO_APPLY=1 to enable). No action taken.")
        return 0
    if not results:
        print("Auto-apply ON: no eligible tier:auto proposals to apply.")
        return 0
    print(f"Auto-applied {len(results)} proposal(s):")
    for r in results:
        print(f"  {r['pid']} → {r['target']} (verified_by {r.get('verified_by')})")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ADAPTS loop apply core")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--list", action="store_true")
    g.add_argument("--show", metavar="ID")
    g.add_argument("--apply", metavar="ID")
    g.add_argument("--reject", metavar="ID")
    g.add_argument("--review-due", action="store_true",
                   help="list applied adaptations whose evaluate_after has passed and are untagged")
    g.add_argument("--tag-outcome", metavar="ID",
                   help="record the outcome of an applied adaptation (needs --outcome)")
    g.add_argument("--scoreboard", action="store_true",
                   help="tally applied-adaptation outcomes + hit-rate")
    g.add_argument("--meta", action="store_true",
                   help="second-order patterns: per-target / per-signal hit-rates + repeated-failure flags")
    g.add_argument("--auto-apply", action="store_true",
                   help="apply queued tier:auto trusted-verifier proposals (INERT unless ADAPT_AUTO_APPLY=1)")
    g.add_argument("--selftest", action="store_true")
    ap.add_argument("--reason", default="")
    ap.add_argument("--outcome", default="",
                    help=f"outcome verdict for --tag-outcome: {', '.join(OUTCOME_VALUES)}")
    ap.add_argument("--note", default="", help="optional note for --tag-outcome")
    ap.add_argument("--json", action="store_true",
                    help="machine-readable output for --list/--review-due/--scoreboard/--meta")
    args = ap.parse_args(argv)

    try:
        if args.list:
            return _cli_list(args.json)
        if args.show:
            print(show_proposal(args.show))
            return 0
        if args.apply:
            res = apply_proposal(args.apply)
            print(f"APPLIED {res['pid']} → {res['target']}")
            print(f"  backup: {res['backup']}")
            print(f"  evaluate_after: {res['evaluate_after']}")
            return 0
        if args.reject:
            res = reject_proposal(args.reject, args.reason)
            print(f"REJECTED {res['pid']}")
            return 0
        if args.review_due:
            return _cli_review_due(args.json)
        if args.tag_outcome:
            if not args.outcome:
                print("error: --tag-outcome requires --outcome "
                      f"<{'|'.join(OUTCOME_VALUES)}>", file=sys.stderr)
                return 2
            res = tag_outcome(args.tag_outcome, args.outcome, args.note)
            print(f"OUTCOME {res['pid']} → {res['outcome']}")
            return 0
        if args.scoreboard:
            return _cli_scoreboard(args.json)
        if args.meta:
            return _cli_meta(args.json)
        if args.auto_apply:
            return _cli_auto_apply()
        if args.selftest:
            return _selftest()
    except AdaptError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 1


def _selftest() -> int:
    """Self-contained tests using a temp sandbox (no real vault writes)."""
    import unittest

    global VAULT, AGENTS_DIR, ALLOWED_STANDARDS, ADAPT_DIR, QUEUE_DIR, APPLIED_DIR, REJECTED_DIR, LOG_FILE

    class T(unittest.TestCase):
        def setUp(self):
            global VAULT, AGENTS_DIR, ALLOWED_STANDARDS, ADAPT_DIR, QUEUE_DIR, APPLIED_DIR, REJECTED_DIR, LOG_FILE
            self.tmp = Path(tempfile.mkdtemp())
            AGENTS_DIR = (self.tmp / "agents").resolve()
            AGENTS_DIR.mkdir()
            self.std = (self.tmp / "Standard.md").resolve()
            self.std.write_text("standard body\n", encoding="utf-8")
            ALLOWED_STANDARDS = {self.std}
            ADAPT_DIR = self.tmp / "Adaptations"
            QUEUE_DIR = ADAPT_DIR / "queue"
            APPLIED_DIR = ADAPT_DIR / "applied"
            REJECTED_DIR = ADAPT_DIR / "rejected"
            LOG_FILE = ADAPT_DIR / "_log.md"
            ensure_dirs()
            self.agent = AGENTS_DIR / "scriptwriter.md"
            self.agent.write_text("You are the scriptwriter.\nUse hook style A.\nEnd.\n", encoding="utf-8")

        def _write(self, pid, target, old, new):
            QUEUE_DIR.mkdir(parents=True, exist_ok=True)
            (QUEUE_DIR / f"{pid}.md").write_text(
                f"---\nid: {pid}\nstatus: pending\ntarget: {target}\n"
                f"confidence: high\nsummary: test {pid}\n---\n\n"
                f"## Rationale\nbecause\n\n{OLD_OPEN}\n{old}\n{OLD_CLOSE}\n\n{NEW_OPEN}\n{new}\n{NEW_CLOSE}\n",
                encoding="utf-8",
            )

        def test_apply_agent(self):
            self._write("p1", str(self.agent), "Use hook style A.", "Use hook style B.")
            res = apply_proposal("p1")
            self.assertIn("hook style B.", self.agent.read_text())
            self.assertTrue(Path(res["backup"]).exists())
            self.assertFalse((QUEUE_DIR / "p1.md").exists())
            self.assertTrue((APPLIED_DIR / "p1.md").exists())

        def test_apply_standard(self):
            self._write("p2", str(self.std), "standard body", "standard body v2")
            apply_proposal("p2")
            self.assertEqual(self.std.read_text(), "standard body v2\n")

        def test_reject_out_of_allowlist(self):
            evil = self.tmp / "outside.md"
            evil.write_text("x\n", encoding="utf-8")
            self._write("p3", str(evil), "x", "y")
            with self.assertRaises(AdaptError):
                apply_proposal("p3")
            self.assertEqual(evil.read_text(), "x\n")  # untouched

        def test_zero_match(self):
            self._write("p4", str(self.agent), "NONEXISTENT", "y")
            with self.assertRaises(AdaptError):
                apply_proposal("p4")

        def test_ambiguous_match(self):
            self.agent.write_text("dup\ndup\n", encoding="utf-8")
            self._write("p5", str(self.agent), "dup", "x")
            with self.assertRaises(AdaptError):
                apply_proposal("p5")

        def test_traversal_id(self):
            with self.assertRaises(AdaptError):
                apply_proposal("../../etc/passwd")

        def test_identical_noop(self):
            self._write("p6", str(self.agent), "Use hook style A.", "Use hook style A.")
            with self.assertRaises(AdaptError):
                apply_proposal("p6")

        def test_reject_moves(self):
            self._write("p7", str(self.agent), "End.", "Fin.")
            reject_proposal("p7", "not now")
            self.assertTrue((REJECTED_DIR / "p7.md").exists())
            self.assertEqual(self.agent.read_text(), "You are the scriptwriter.\nUse hook style A.\nEnd.\n")

        def test_non_md_target(self):
            txt = AGENTS_DIR / "notes.txt"
            txt.write_text("a\n", encoding="utf-8")
            self._write("p8", str(txt), "a", "b")
            with self.assertRaises(AdaptError):
                apply_proposal("p8")

        def test_multiline_block(self):
            self._write("p9", str(self.agent), "You are the scriptwriter.\nUse hook style A.",
                        "You are the scriptwriter.\nUse hook style C.")
            apply_proposal("p9")
            self.assertIn("hook style C.", self.agent.read_text())

        # --- outcome tagging --------------------------------------------------
        def _backdate(self, pid, date_iso):
            """Force an applied proposal's evaluate_after into the past so the
            due-for-review check fires deterministically in the test."""
            p = APPLIED_DIR / f"{pid}.md"
            t = _set_frontmatter_field(p.read_text(encoding="utf-8"),
                                       "evaluate_after", date_iso)
            p.write_text(t, encoding="utf-8")

        def test_apply_stamps_evaluate_after(self):
            self._write("o1", str(self.agent), "Use hook style A.", "Use hook style B.")
            res = apply_proposal("o1")
            meta = _load_proposal_file(APPLIED_DIR / "o1.md").meta
            self.assertEqual(meta.get("evaluate_after"), res["evaluate_after"])
            # it's a future date, horizon days out
            expected = (_dt.date.today() + _dt.timedelta(days=EVAL_HORIZON_DAYS)).isoformat()
            self.assertEqual(meta["evaluate_after"], expected)

        def test_tag_outcome_writes_fields_and_log(self):
            self._write("o2", str(self.agent), "Use hook style A.", "Use hook style B.")
            apply_proposal("o2")
            tag_outcome("o2", "Improved", "block-rate dropped")  # case-insensitive
            meta = _load_proposal_file(APPLIED_DIR / "o2.md").meta
            self.assertEqual(meta["outcome"], "improved")
            self.assertEqual(meta["outcome_note"], "block-rate dropped")
            self.assertIn("OUTCOME", LOG_FILE.read_text())

        def test_tag_outcome_ignores_body_field_line(self):
            # Regression: a payload/body line starting with a frontmatter key must
            # NOT be rewritten when that key is stamped — only the real frontmatter
            # field changes, and the body is preserved verbatim.
            self._write("o6", str(self.agent), "Use hook style A.",
                        "Use hook style B.\noutcome: must always be measured")
            apply_proposal("o6")
            tag_outcome("o6", "improved")
            applied = (APPLIED_DIR / "o6.md").read_text(encoding="utf-8")
            meta = _load_proposal_file(APPLIED_DIR / "o6.md").meta
            self.assertEqual(meta["outcome"], "improved")               # frontmatter stamped
            self.assertIn("outcome: must always be measured", applied)  # body untouched

        def test_tag_outcome_invalid_verdict(self):
            self._write("o3", str(self.agent), "Use hook style A.", "Use hook style B.")
            apply_proposal("o3")
            with self.assertRaises(AdaptError):
                tag_outcome("o3", "great-success")

        def test_tag_outcome_unknown_id(self):
            with self.assertRaises(AdaptError):
                tag_outcome("nope", "improved")

        def test_tag_outcome_traversal_id(self):
            with self.assertRaises(AdaptError):
                tag_outcome("../../etc/passwd", "improved")

        def test_due_for_review_lifecycle(self):
            self._write("o4", str(self.agent), "Use hook style A.", "Use hook style B.")
            apply_proposal("o4")
            # freshly applied → not yet due (evaluate_after is in the future)
            self.assertEqual([p.pid for p in due_for_review()], [])
            self._backdate("o4", "2000-01-01")
            self.assertEqual([p.pid for p in due_for_review()], ["o4"])
            # once tagged, it drops out of the due list
            tag_outcome("o4", "no-change")
            self.assertEqual([p.pid for p in due_for_review()], [])

        def test_scoreboard_counts_and_hitrate(self):
            for pid, new in (("s1", "B."), ("s2", "C."), ("s3", "D.")):
                self.agent.write_text("You are the scriptwriter.\nUse hook style A.\nEnd.\n",
                                      encoding="utf-8")
                self._write(pid, str(self.agent), "Use hook style A.", f"Use hook style {new}")
                apply_proposal(pid)
            tag_outcome("s1", "improved")
            tag_outcome("s2", "regressed")
            tag_outcome("s3", "inconclusive")
            sb = scoreboard()
            self.assertEqual(sb["total"], 3)
            self.assertEqual(sb["counts"]["improved"], 1)
            self.assertEqual(sb["counts"]["regressed"], 1)
            self.assertEqual(sb["counts"]["inconclusive"], 1)
            self.assertEqual(sb["untagged"], 0)
            self.assertEqual(sb["decisive"], 2)  # inconclusive excluded
            self.assertAlmostEqual(sb["hit_rate"], 0.5)

        # --- evidence floor (sample-size gate) --------------------------------
        def _write_ex(self, pid, target, old, new, extra=""):
            """Like _write but lets a test inject extra frontmatter lines."""
            QUEUE_DIR.mkdir(parents=True, exist_ok=True)
            (QUEUE_DIR / f"{pid}.md").write_text(
                f"---\nid: {pid}\nstatus: pending\ntarget: {target}\n"
                f"confidence: high\nsummary: test {pid}\n{extra}---\n\n"
                f"## Rationale\nbecause\n\n{OLD_OPEN}\n{old}\n{OLD_CLOSE}\n\n{NEW_OPEN}\n{new}\n{NEW_CLOSE}\n",
                encoding="utf-8",
            )

        def test_evidence_n_parsing(self):
            self._write_ex("e1", str(self.agent), "Use hook style A.", "Use hook style B.",
                           extra="evidence_n: ~1,200 impressions\n")
            p = _load_proposal_file(QUEUE_DIR / "e1.md")
            self.assertEqual(p.evidence_n, 1200)
            self.assertFalse(p.thin_evidence)

        def test_thin_evidence_flagged_below_floor(self):
            self._write_ex("e2", str(self.agent), "Use hook style A.", "Use hook style B.",
                           extra="evidence_n: 4 views\n")
            p = _load_proposal_file(QUEUE_DIR / "e2.md")
            self.assertEqual(p.evidence_n, 4)
            self.assertTrue(p.thin_evidence)

        def test_thin_evidence_exempt_categorical(self):
            self._write_ex("e3", str(self.agent), "Use hook style A.", "Use hook style B.",
                           extra="evidence_n: 1\nevidence_basis: severe-single-miss off-model hero\n")
            p = _load_proposal_file(QUEUE_DIR / "e3.md")
            self.assertFalse(p.thin_evidence)  # categorical miss exempt at n=1

        def test_apply_warns_thin_then_applies(self):
            self._write_ex("e4", str(self.agent), "Use hook style A.", "Use hook style B.",
                           extra="evidence_n: 4\n")
            res = apply_proposal("e4")
            self.assertTrue(any("thin evidence" in w for w in res["warnings"]))
            self.assertIn("hook style B.", self.agent.read_text())  # soft gate: still applied
            self.assertIn("WARN", LOG_FILE.read_text())

        def test_apply_warns_missing_evidence_n(self):
            self._write("e5", str(self.agent), "Use hook style A.", "Use hook style B.")
            res = apply_proposal("e5")
            self.assertTrue(any("evidence_n not recorded" in w for w in res["warnings"]))

        # --- meta scoreboard --------------------------------------------------
        def _apply_tagged(self, pid, new, outcome, target=None, signal=None):
            self.agent.write_text("You are the scriptwriter.\nUse hook style A.\nEnd.\n",
                                  encoding="utf-8")
            extra = f"signal_source: {signal}\n" if signal else ""
            self._write_ex(pid, target or str(self.agent), "Use hook style A.",
                           f"Use hook style {new}", extra=extra)
            apply_proposal(pid)
            tag_outcome(pid, outcome)

        def test_meta_scoreboard_repeated_failure(self):
            # same target regresses twice → flagged; signal grouping present
            self._apply_tagged("m1", "B.", "regressed", signal="Analytics/read-1.md")
            self._apply_tagged("m2", "C.", "no-change", signal="Analytics/read-2.md")
            self._apply_tagged("m3", "D.", "improved", signal="Analytics/read-3.md")
            ms = meta_scoreboard()
            tgt = self.agent.name
            self.assertIn(tgt, ms["repeated_failures"])  # 1/3 hit-rate over 3 decisive
            names = {r["group"] for r in ms["by_signal"]}
            self.assertIn("read-1.md", names)

        # --- risk-tiered auto-apply (inert by default) ------------------------
        def test_auto_apply_inert_by_default(self):
            self._write_ex("a1", str(self.agent), "Use hook style A.", "Use hook style B.",
                           extra="tier: auto\nverified_by: figures-watcher\n")
            self.assertEqual(apply_auto_pending(enabled=False), [])
            self.assertIn("hook style A.", self.agent.read_text())  # untouched
            self.assertTrue((QUEUE_DIR / "a1.md").exists())

        def test_auto_apply_applies_when_enabled_and_trusted(self):
            self._write_ex("a2", str(self.agent), "Use hook style A.", "Use hook style B.",
                           extra="tier: auto\nverified_by: figures-watcher\n")
            res = apply_auto_pending(enabled=True)
            self.assertEqual([r["pid"] for r in res], ["a2"])
            self.assertTrue(res[0]["auto"])
            self.assertIn("hook style B.", self.agent.read_text())

        def test_auto_apply_skips_untrusted_verifier(self):
            self._write_ex("a3", str(self.agent), "Use hook style A.", "Use hook style B.",
                           extra="tier: auto\nverified_by: some-random-agent\n")
            self.assertEqual(apply_auto_pending(enabled=True), [])
            self.assertIn("hook style A.", self.agent.read_text())  # untouched

        def test_auto_apply_skips_gated_tier(self):
            self._write_ex("a4", str(self.agent), "Use hook style A.", "Use hook style B.",
                           extra="verified_by: figures-watcher\n")  # tier defaults to gated
            self.assertEqual(apply_auto_pending(enabled=True), [])

        def test_proposal_json_shape(self):
            self._write_ex("j1", str(self.agent), "Use hook style A.", "Use hook style B.",
                           extra="evidence_n: 4\nmetric: block-rate\n")
            p = _load_proposal_file(QUEUE_DIR / "j1.md")
            j = _proposal_json(p)
            self.assertEqual(j["id"], "j1")
            self.assertEqual(j["evidence_n"], 4)
            self.assertTrue(j["thin_evidence"])
            self.assertEqual(j["tier"], "gated")
            self.assertEqual(j["metric"], "block-rate")

    suite = unittest.TestLoader().loadTestsFromTestCase(T)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
