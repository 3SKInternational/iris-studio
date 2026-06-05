"""Pre-delivery lint for agent dispatched deliverables (A-23).

Two checks:

1. Banned vocabulary — words from the Master_Character_Prompt v3 banned table
   that consistently push image models toward realism ("cinematic", "realistic",
   "dramatic lighting", "photorealistic", "noir", "soft airbrushing",
   "gradient-heavy", and "soft" used as a lighting modifier). Negation-aware:
   "NOT realistic", "no soft airbrushing" are not flagged.

2. Monetary-figure overview — enumerates distinct $N,NNN figures (>= $100),
   sorted by occurrence count, with first three line numbers each. Informational
   only; surfaces script-level inconsistencies a human eye catches instantly
   (V05 calibration defect: $1,847 referenced 5+ times, $7,713 once — visible
   in the overview, easy to flag).

Read-only: writes `<deliverable>_lint.md` next to the deliverable. Never edits
the deliverable itself. Returns a structured result dict so the caller can decide
whether to Telegram-ping Steve.

Run standalone for testing:
    python scripts/agent_output_lint.py <path>
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

# Banned vocab — sourced from BRANDS/3SK_Finance/Character_Reference/Master_Character_Prompt.md
# These are flagged in any text. The "soft" entry is treated specially below
# (only flagged as a lighting modifier, not as a generic adjective like
# "soft red accent" — to avoid false positives).
BANNED_STRICT = [
    "cinematic",
    "realistic",
    "dramatic lighting",
    "photorealistic",
    "noir",
    "gradient-heavy",
    "soft airbrushing",
]

# "soft" + lighting noun → flag. Bare "soft" in non-lighting contexts → skip.
SOFT_LIGHTING_NOUNS = [
    "lighting", "light", "glow", "shadow", "shadows", "shading",
    "blur", "highlights", "rim", "tone", "tones", "wash", "ambient",
    "daylight", "warm glow", "key", "fill", "rays",
]

# Negation lookback: if any of these appears within NEGATION_WINDOW_CHARS of the
# banned-word's start, treat as a negative-constraint phrase ("NOT realistic",
# "no soft airbrushing") and skip.
NEGATION_WINDOW_CHARS = 30
NEGATION_PATTERN = re.compile(
    r"\b(no|not|never|avoid|without|never\s+use|don\'?t)\b",
    re.IGNORECASE,
)

# Monetary regex — handles $1,847, $1,847.00, $1,847/mo, $1,847/month, $1.5M
# (the M-suffix isn't extracted as a numeric, just preserved in the raw match).
MONEY_PATTERN = re.compile(
    r"\$([\d]{1,3}(?:,\d{3})*(?:\.\d{1,2})?)\b",
)

MIN_MONETARY = 100  # skip $10, $50 (too noisy)


def _line_offsets(text: str) -> list[int]:
    """Byte offsets where each line begins (offsets[i] = start of line i+1)."""
    offsets = [0]
    for i, ch in enumerate(text):
        if ch == "\n":
            offsets.append(i + 1)
    return offsets


def _line_for_offset(line_offsets: list[int], off: int) -> int:
    """Binary-search-free; lists are short enough that linear is fine."""
    n = 0
    for i, start in enumerate(line_offsets):
        if start <= off:
            n = i + 1
        else:
            break
    return n


def _is_negated(text: str, start: int) -> bool:
    window_start = max(0, start - NEGATION_WINDOW_CHARS)
    window = text[window_start:start]
    return bool(NEGATION_PATTERN.search(window))


_META_LINE_MARKERS = (
    "banned", "no instances of", "eliminated", "avoid using",
    "do not use", "should not use", "anti-pattern",
)


def _is_quoted(text: str, start: int, end: int) -> bool:
    """True iff the matched word is wrapped in straight or curly quotes —
    e.g. an agent's meta-note listing banned words verbatim. Skip."""
    before = text[max(0, start - 2):start]
    after = text[end:end + 2]
    return ('"' in before and '"' in after) or ('“' in before and '”' in after) or ("'" in before and "'" in after)


def _line_at(text: str, offset: int) -> str:
    """Return the source line containing `offset`."""
    nl_before = text.rfind("\n", 0, offset)
    nl_after = text.find("\n", offset)
    start = 0 if nl_before == -1 else nl_before + 1
    end = len(text) if nl_after == -1 else nl_after
    return text[start:end]


def _is_meta_line(text: str, offset: int) -> bool:
    """True iff the line containing `offset` is a meta-comment that lists or
    describes banned vocabulary (rather than using it). Detected by lowercase
    marker phrases ('banned', 'no instances of', 'eliminated', etc.)."""
    line = _line_at(text, offset).lower()
    return any(marker in line for marker in _META_LINE_MARKERS)


def _is_soft_lighting(text: str, start: int, end: int) -> bool:
    """True iff 'soft' (matched at [start:end]) is followed by a lighting noun
    within the same sentence (window stops at period or newline), so generic
    adjectives like 'Soft red brand accent.\\n\\nLighting: …' don't false-positive
    against the next sentence's 'Lighting'."""
    forward_raw = text[end:end + 30]
    # Stop at sentence boundary
    for stop_char in ".\n":
        idx = forward_raw.find(stop_char)
        if idx != -1:
            forward_raw = forward_raw[:idx]
    forward = forward_raw.lower()
    return any(noun in forward for noun in SOFT_LIGHTING_NOUNS)


def check_banned_vocab(text: str) -> list[dict]:
    """Return [{word, line, snippet}] for each non-negated banned-word match."""
    line_offsets = _line_offsets(text)
    findings: list[dict] = []
    seen: set[tuple[str, int]] = set()  # dedupe identical (word, line) hits

    for word in BANNED_STRICT:
        pat = re.compile(r"\b" + re.escape(word) + r"\b", re.IGNORECASE)
        for m in pat.finditer(text):
            if _is_negated(text, m.start()):
                continue
            if _is_quoted(text, m.start(), m.end()):
                continue
            if _is_meta_line(text, m.start()):
                continue
            line_num = _line_for_offset(line_offsets, m.start())
            if (word, line_num) in seen:
                continue
            seen.add((word, line_num))
            snippet_start = max(0, m.start() - 30)
            snippet_end = min(len(text), m.end() + 30)
            snippet = text[snippet_start:snippet_end].replace("\n", " ").strip()
            findings.append({
                "word": word,
                "line": line_num,
                "snippet": snippet,
            })

    # "soft" — only flag as lighting modifier.
    pat = re.compile(r"\bsoft\b", re.IGNORECASE)
    for m in pat.finditer(text):
        if _is_negated(text, m.start()):
            continue
        if _is_quoted(text, m.start(), m.end()):
            continue
        if _is_meta_line(text, m.start()):
            continue
        if not _is_soft_lighting(text, m.start(), m.end()):
            continue
        line_num = _line_for_offset(line_offsets, m.start())
        if ("soft", line_num) in seen:
            continue
        seen.add(("soft", line_num))
        snippet_start = max(0, m.start() - 30)
        snippet_end = min(len(text), m.end() + 30)
        snippet = text[snippet_start:snippet_end].replace("\n", " ").strip()
        findings.append({
            "word": "soft (lighting modifier)",
            "line": line_num,
            "snippet": snippet,
        })

    findings.sort(key=lambda f: f["line"])
    return findings


def monetary_overview(text: str) -> dict:
    """Enumerate distinct $N figures (>= MIN_MONETARY) with occurrence counts +
    first three line numbers each. Returns {distinct_count, top: [...]}."""
    line_offsets = _line_offsets(text)
    occurrences: dict[str, list[int]] = defaultdict(list)
    counts: Counter = Counter()

    for m in MONEY_PATTERN.finditer(text):
        raw = m.group(1)
        try:
            amount = float(raw.replace(",", ""))
        except ValueError:
            continue
        if amount < MIN_MONETARY:
            continue
        key = f"${raw}"
        counts[key] += 1
        if len(occurrences[key]) < 3:
            occurrences[key].append(_line_for_offset(line_offsets, m.start()))

    # Sort by count descending, then by amount descending. Two sections:
    # (a) top 15 by count (recurring concepts) — drift-detect at a glance.
    # (b) all 1×-occurrence figures (outliers — most likely defects, since
    #     recurring concepts get repeated; one-offs are precision values or
    #     typos). Capped at 30 to avoid runaway in long scripts.
    rows = []
    for key, count in counts.most_common(15):
        rows.append({
            "value": key,
            "count": count,
            "first_lines": occurrences[key],
        })
    singletons = []
    for key, count in counts.items():
        if count == 1:
            singletons.append({
                "value": key,
                "count": 1,
                "first_lines": occurrences[key],
            })
        if len(singletons) >= 60:
            break
    return {
        "distinct_count": len(counts),
        "top": rows,
        "singletons": singletons,
    }


def lint(path: Path) -> dict:
    """Run both checks against the file at `path`. Returns a result dict the
    caller can use to decide whether to write a report + Telegram-ping."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {
            "path": str(path),
            "error": f"read failed: {exc}",
            "ok": False,
            "should_alert": False,
        }

    banned = check_banned_vocab(text)
    overview = monetary_overview(text)
    # Worth-reporting threshold: any banned hit, OR >= 3 distinct monetary
    # figures (i.e. the doc has enough $ talk that the overview is useful).
    worth_reporting = bool(banned) or overview["distinct_count"] >= 3
    return {
        "path": str(path),
        "banned": banned,
        "monetary": overview,
        "worth_reporting": worth_reporting,
        # Telegram pings only when banned vocab flagged (numeric is informational).
        "should_alert": bool(banned),
        "ok": not banned,
    }


def format_report(result: dict) -> str:
    """Markdown report next to the deliverable."""
    lines = [
        f"# Lint report — {Path(result['path']).name}",
        "",
        f"_Generated {datetime.now().isoformat(timespec='seconds')} by `agent_output_lint.py` (A-23)._",
        "",
        f"**Source:** `{result['path']}`",
        "",
    ]
    if "error" in result:
        lines.append(f"**❌ Lint error:** {result['error']}")
        return "\n".join(lines) + "\n"

    banned = result.get("banned", [])
    overview = result.get("monetary", {})

    if not banned and overview["distinct_count"] < 3:
        lines.append("**✅ CLEAN** — no banned vocabulary; monetary content sparse.")
        return "\n".join(lines) + "\n"

    if banned:
        lines.append(f"## ⚠️ Banned vocabulary ({len(banned)} occurrence(s))")
        lines.append("")
        lines.append(
            "Per Master Character Prompt v3 banned-vocab table — these words "
            "consistently push image models toward realism. Replace before re-dispatching."
        )
        lines.append("")
        for b in banned:
            lines.append(f"- **`{b['word']}`** line {b['line']}: …{b['snippet']}…")
        lines.append("")
    else:
        lines.append("**✅ Banned vocabulary:** clean.")
        lines.append("")

    if overview["distinct_count"] >= 3:
        lines.append(
            f"## 💰 Monetary overview ({overview['distinct_count']} distinct figures)"
        )
        lines.append("")
        lines.append(
            "Informational only — surfaces script-level inconsistencies a human eye "
            "catches instantly. If a single 1×-occurrence outlier should reconcile with "
            "a high-frequency headline figure (e.g. the V05 `$1,847/mo` vs `$7,713/mo` "
            "Path-A defect), fix before publishing."
        )
        lines.append("")
        lines.append("### Top by count (recurring concepts)")
        lines.append("")
        lines.append("| Figure | Count | First lines |")
        lines.append("|---|---:|---|")
        for row in overview["top"]:
            lines_str = ", ".join(str(n) for n in row["first_lines"])
            lines.append(f"| `{row['value']}` | {row['count']} | {lines_str} |")
        lines.append("")
        singletons = overview.get("singletons", [])
        if singletons:
            lines.append(f"### 1× outliers ({len(singletons)} figures — defect-prone)")
            lines.append("")
            lines.append("| Figure | Line |")
            lines.append("|---|---:|")
            for row in singletons:
                line_str = row["first_lines"][0] if row["first_lines"] else "?"
                lines.append(f"| `{row['value']}` | {line_str} |")
            lines.append("")
    elif overview["distinct_count"] > 0:
        lines.append(f"**💰 Monetary:** {overview['distinct_count']} distinct figure(s); below 3-figure overview threshold.")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("_A-23 linter is read-only — it never edits the deliverable, only flags. Decide whether to re-dispatch or accept._")
    return "\n".join(lines) + "\n"


def report_path_for(deliverable: Path) -> Path:
    """`Video_05_Script.md` → `Video_05_Script_lint.md` (same dir)."""
    return deliverable.with_name(f"{deliverable.stem}_lint{deliverable.suffix}")


def lint_and_report(deliverable: Path) -> dict:
    """Top-level convenience: lint + write report if worth reporting. Returns
    the result dict with `report_path` added if written. Never raises."""
    try:
        result = lint(deliverable)
        if result.get("worth_reporting"):
            report = report_path_for(deliverable)
            report.write_text(format_report(result), encoding="utf-8")
            result["report_path"] = str(report)
        return result
    except Exception as exc:  # pragma: no cover — defensive
        return {
            "path": str(deliverable),
            "error": f"lint exception: {exc!r}",
            "ok": False,
            "should_alert": False,
        }


def _main() -> int:
    ap = argparse.ArgumentParser(description="Lint an agent deliverable.")
    ap.add_argument("path", type=Path, help="File to lint.")
    ap.add_argument("--no-write", action="store_true",
                    help="Print report to stdout instead of writing to disk.")
    args = ap.parse_args()

    if not args.path.exists():
        print(f"ERROR: {args.path} does not exist", file=sys.stderr)
        return 2

    if args.no_write:
        result = lint(args.path)
        print(format_report(result))
    else:
        result = lint_and_report(args.path)
        if "report_path" in result:
            print(f"Wrote {result['report_path']}")
        elif result.get("ok"):
            print(f"Clean — no report written for {args.path}")
        else:
            print(f"Error: {result.get('error', 'unknown')}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(_main())
