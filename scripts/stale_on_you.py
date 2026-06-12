#!/usr/bin/env python3
"""C2 — Consolidated "Stale on you" surface generator (Redesign Night 3, second half).

Reads:
  - /Users/steve/Documents/3SK/outputs/06_CEO/Decision_Queue.md (open rows)
  - /Users/steve/Documents/3SK/outputs/INBOX.md (TODAY + THIS WEEK 🧍 lines)

State (for INBOX day-counts):
  - /Users/steve/iris_studio/state/stale_on_you_seen.tsv
    Columns: slug\tfirst_seen_ISO\tlast_seen_ISO\ttext

Output: a single consolidated `## ⏳ Stale on you` markdown section on stdout.
The pre-brief routine captures stdout and writes it between
`<!-- STALE_ON_YOU:BEGIN -->` and `<!-- STALE_ON_YOU:END -->` markers in INBOX.

Sort order: Decision_Queue items first (sorted by Days descending — oldest hurts
most), then INBOX 🧍 items (also by Days descending). Closed/deferred DQ rows
are excluded.

Stdlib-only. Read-only over the vault; only writes to its state TSV.

Spec: 2026-06-09_Workflow_Autonomy_Redesign.md § Phase C → C2.
Shipped: 2026-06-11 (Redesign Night 3 second half — A2 still blocked on Steve).
"""

from __future__ import annotations

import datetime
import re
import sys
import unicodedata
from pathlib import Path

VAULT = Path("/Users/steve/Documents/3SK/outputs")
DECISION_QUEUE = VAULT / "06_CEO" / "Decision_Queue.md"
DECISIONS_LOG = VAULT / "06_CEO" / "Decisions_Log"
INBOX = VAULT / "INBOX.md"

STATE_DIR = Path("/Users/steve/iris_studio/state")
STATE_FILE = STATE_DIR / "stale_on_you_seen.tsv"

# Decisions_Log lookup defaults (Night 4 fix — DQ-10 false-alarm root cause).
DECISIONS_LOG_LOOKBACK_DAYS = 30
DECISIONS_LOG_MIN_TOKEN_LEN = 4
DECISIONS_LOG_MIN_HITS = 2
DECISIONS_LOG_STOPWORDS = {
    "that", "this", "with", "from", "have", "will", "into", "your",
    "their", "these", "those", "should", "must", "would", "could",
    "make", "give", "need", "want", "when", "what", "where", "while",
    "been", "than", "then", "they", "them", "also", "such",
}


def _today_iso() -> str:
    return datetime.date.today().isoformat()


def _slugify(text: str) -> str:
    """Stable key for an INBOX 🧍 item — lowercase, alnum + dashes, max 60 chars."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return text[:60]


def _load_seen() -> dict[str, dict[str, str]]:
    """Returns slug -> {first_seen, last_seen, text}."""
    seen: dict[str, dict[str, str]] = {}
    if not STATE_FILE.exists():
        return seen
    try:
        for line in STATE_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip() or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 4:
                continue
            slug, first_seen, last_seen, text = parts[0], parts[1], parts[2], parts[3]
            seen[slug] = {"first_seen": first_seen, "last_seen": last_seen, "text": text}
    except Exception:
        # State file is best-effort. If it's corrupt, treat as empty.
        pass
    return seen


def _save_seen(seen: dict[str, dict[str, str]]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lines = ["# slug\tfirst_seen\tlast_seen\ttext"]
    for slug, row in sorted(seen.items()):
        lines.append(f"{slug}\t{row['first_seen']}\t{row['last_seen']}\t{row['text']}")
    STATE_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _days_between(iso_a: str, iso_b: str) -> int:
    try:
        a = datetime.date.fromisoformat(iso_a)
        b = datetime.date.fromisoformat(iso_b)
        return (b - a).days
    except Exception:
        return 0


def load_recent_decisions(
    days: int = DECISIONS_LOG_LOOKBACK_DAYS,
) -> list[tuple[str, str, str]]:
    """Return [(filename_stem, title_lower, body_lower)] for decisions logged
    in the last `days` days. Filename convention: `YYYY-MM-DD_<topic>.md`.

    Used by the Decisions_Log lookup that prevents a re-decided item from
    appearing as awaiting-Steve. Root cause for the Night 3 false alarm:
    Tier-2 packet "pass all" was decided 2026-06-09 and logged at
    `Decisions_Log/2026-06-09_Tier2_Calibration_Pass_All.md`, but the C2
    stale-tracker had no awareness of the decisions log and surfaced the
    item anyway (DQ-10). This lookup closes that gap.
    """
    if not DECISIONS_LOG.exists():
        return []
    cutoff = datetime.date.today() - datetime.timedelta(days=days)
    out: list[tuple[str, str, str]] = []
    for p in sorted(DECISIONS_LOG.glob("*.md")):
        m = re.match(r"^(\d{4})-(\d{2})-(\d{2})_", p.name)
        if not m:
            continue
        try:
            d = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
        if d < cutoff:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        h1_match = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
        title = h1_match.group(1).strip() if h1_match else p.stem
        out.append((p.stem, title.lower(), text.lower()))
    return out


def _significant_tokens(text: str) -> list[str]:
    raw = re.findall(r"[a-z0-9]+", text.lower())
    return [
        t for t in raw
        if len(t) >= DECISIONS_LOG_MIN_TOKEN_LEN
        and t not in DECISIONS_LOG_STOPWORDS
    ]


def resolved_by_recent_decision(
    item: dict[str, str],
    decisions: list[tuple[str, str, str]],
) -> bool:
    """True iff the item's text or DQ id strongly overlaps a recent decision.

    Heuristic:
      (a) If the item carries a DQ-N id and that exact id appears anywhere in
          any recent decision body → resolved.
      (b) Otherwise, extract significant tokens (len >= 4, non-stopword) from
          the item text. If at least MIN_HITS distinct tokens appear in any
          decision title → resolved.

    Body matches are intentionally NOT used for token overlap — decision
    bodies tend to mention many topics by reference, which would
    over-match. Title overlap is the precise signal.
    """
    if not decisions:
        return False
    item_id = (item.get("id") or "").lower().strip()
    item_text = (item.get("text") or "").lower()
    tokens = _significant_tokens(item_text)
    for _stem, title, body in decisions:
        if item_id and item_id in body:
            return True
        if len(tokens) < DECISIONS_LOG_MIN_HITS:
            continue
        hits = {t for t in tokens if t in title}
        if len(hits) >= DECISIONS_LOG_MIN_HITS:
            return True
    return False


def parse_decision_queue() -> list[dict[str, str]]:
    """Return list of open Decision_Queue rows, each as a dict."""
    if not DECISION_QUEUE.exists():
        return []
    rows = []
    for line in DECISION_QUEUE.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith("| DQ-"):
            continue
        # split on `|`, strip surrounding whitespace
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 7:
            continue
        dq_id, decision, klass, default, deadline, days, status = cells[:7]
        # Skip closed/deferred — only surface what's actively pending.
        status_low = status.lower()
        if "closed" in status_low or "✅" in status:
            continue
        if "deferred" in status_low or "💤" in status:
            continue
        # Trim decision text for the table — keep first sentence-ish.
        short_decision = re.split(r"[—–]| - ", decision, maxsplit=1)[0].strip()
        if len(short_decision) > 95:
            short_decision = short_decision[:92] + "..."
        rows.append(
            {
                "id": dq_id,
                "text": short_decision,
                "class": klass,
                "days": days,
                "deadline": deadline,
                "source": "DQ",
            }
        )
    return rows


def parse_inbox_steve_items() -> list[dict[str, str]]:
    """Return list items from INBOX TODAY + THIS WEEK sections.

    Convention: TODAY items may carry per-line `🧍` markers; THIS WEEK section
    uses heading-level `## 🧍 THIS WEEK` and treats all its list items as
    Steve-required. Heading lines themselves are never captured.
    """
    if not INBOX.exists():
        return []
    text = INBOX.read_text(encoding="utf-8", errors="replace")
    items = []
    section = None  # "TODAY" | "THIS_WEEK" | None
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        # Detect section transitions
        if re.match(r"^##\s+📌\s+TODAY", line):
            section = "TODAY"
            continue
        if re.match(r"^##\s+🧍\s+THIS\s+WEEK", line):
            section = "THIS_WEEK"
            continue
        if line.startswith("## ") and section is not None:
            # Any other ## heading ends the action surface
            section = None
            continue
        if section is None:
            continue
        # Skip non-list lines (blank, prose, sub-headings)
        if not re.match(r"^\s*([-*]|\d+\.)\s+", line):
            continue
        # Trim list markers + bold + leading 🧍 emoji
        clean = re.sub(r"^\s*([-*]|\d+\.)\s+", "", line).strip()
        clean = clean.replace("**", "").strip()
        clean = re.sub(r"^🧍\s*", "", clean)
        # Trim trailing parenthetical metadata for table-friendliness
        short = re.split(r"\s+—\s+|\s+-\s+|\s+\(", clean, maxsplit=1)[0].strip()
        if len(short) < 6:
            continue
        if len(short) > 95:
            short = short[:92] + "..."
        items.append({"text": short, "raw": clean, "section": section})
    return items


def _update_seen_and_compute_days(
    inbox_items: list[dict[str, str]],
    seen: dict[str, dict[str, str]],
    today: str,
) -> list[dict[str, str]]:
    """Tag each INBOX item with a slug + days-pending; updates the seen dict."""
    out = []
    for item in inbox_items:
        slug = _slugify(item["text"])
        if not slug:
            continue
        if slug in seen:
            seen[slug]["last_seen"] = today
            # If text drifted, keep the freshest wording
            seen[slug]["text"] = item["text"]
            first_seen = seen[slug]["first_seen"]
        else:
            seen[slug] = {"first_seen": today, "last_seen": today, "text": item["text"]}
            first_seen = today
        days = _days_between(first_seen, today)
        out.append(
            {
                "id": "",
                "text": item["text"],
                "class": "🧍",
                "days": str(days),
                "deadline": "",
                "source": "INBOX",
                "slug": slug,
            }
        )
    return out


def _days_int(s: str) -> int:
    """Parse a days-pending cell (e.g. '5', '~46 (since April)', '10 (since 5/31)', '0 (added 6/11)')."""
    m = re.match(r"\s*~?(\d+)", s)
    if m:
        return int(m.group(1))
    return 0


def render_section(rows: list[dict[str, str]], today: str) -> str:
    """Render the consolidated markdown section."""
    if not rows:
        return (
            "## ⏳ Stale on you (auto-generated " + today + " — pre-brief Pass 15)\n\n"
            "_Single canonical list of items awaiting Steve, generated by `scripts/stale_on_you.py`._\n\n"
            "**Nothing awaiting you.** Steve, you are stale-free. 🎉\n\n"
            "_Sources: [[Decision_Queue]] open rows · INBOX 🧍 markers. C2 — Redesign Night 3._\n"
        )
    # Sort: DQ rows first by days desc, then INBOX rows by days desc.
    dq_rows = sorted(
        [r for r in rows if r["source"] == "DQ"],
        key=lambda r: _days_int(r["days"]),
        reverse=True,
    )
    inbox_rows = sorted(
        [r for r in rows if r["source"] == "INBOX"],
        key=lambda r: _days_int(r["days"]),
        reverse=True,
    )
    ordered = dq_rows + inbox_rows

    out = [
        "## ⏳ Stale on you (auto-generated " + today + " — pre-brief Pass 15)",
        "",
        "_Single canonical list of items awaiting Steve, generated by `scripts/stale_on_you.py`. "
        "Day-count = days since first seen in this surface. Individual cadences should LINK here "
        "rather than re-flag these items in their own voices (Redesign C2)._",
        "",
        "| ID | Item | Class | Days | Deadline / unlock |",
        "|---|---|---|---|---|",
    ]
    for r in ordered:
        item_id = r["id"] or "—"
        text = r["text"].replace("|", "\\|")
        deadline = r["deadline"] or "—"
        out.append(f"| {item_id} | {text} | {r['class']} | {r['days']} | {deadline} |")
    out.append("")
    out.append(
        "_Sources: [[Decision_Queue]] (DQ rows: official days-pending column) · INBOX 🧍 markers "
        "(days computed from `state/stale_on_you_seen.tsv` first-seen ledger). "
        "C2 — Redesign Night 3._"
    )
    return "\n".join(out) + "\n"


SECTION_BEGIN = "<!-- C2_STALE_ON_YOU:BEGIN -->"
SECTION_END = "<!-- C2_STALE_ON_YOU:END -->"


def write_inbox(section: str) -> str:
    """Idempotently update the C2 section in INBOX.md.

    Inserts between `<!-- C2_STALE_ON_YOU:BEGIN -->` / `:END -->` markers,
    placed right after the leading `---` separator (before `## 📌 TODAY`).
    Returns a one-line status for stdout logging.
    """
    if not INBOX.exists():
        return "INBOX.md not found — skipping write"
    text = INBOX.read_text(encoding="utf-8")
    block = f"{SECTION_BEGIN}\n{section}\n{SECTION_END}"
    if SECTION_BEGIN in text and SECTION_END in text:
        new_text = re.sub(
            re.escape(SECTION_BEGIN) + r".*?" + re.escape(SECTION_END),
            block.replace("\\", r"\\"),
            text,
            count=1,
            flags=re.DOTALL,
        )
        action = "updated existing C2 block"
    else:
        # Insert after the first `---` separator (which sits between header note
        # and the TODAY action surface).
        m = re.search(r"^---\s*$", text, re.MULTILINE)
        if not m:
            return "no `---` separator found in INBOX — skipping write"
        insert_at = m.end()
        new_text = text[:insert_at] + "\n\n" + block + text[insert_at:]
        action = "inserted new C2 block"
    if new_text == text:
        return "C2 block unchanged"
    INBOX.write_text(new_text, encoding="utf-8")
    return f"INBOX.md {action}"


def main() -> int:
    args = sys.argv[1:]
    write_mode = "--write-inbox" in args

    today = _today_iso()
    seen = _load_seen()

    dq_rows = parse_decision_queue()
    inbox_items = parse_inbox_steve_items()
    inbox_rows = _update_seen_and_compute_days(inbox_items, seen, today)

    # Persist the updated first-seen ledger BEFORE the decisions-log filter so
    # an item that gets correctly excluded still has its first_seen ledger row,
    # which keeps day-counts honest if the decision later proves stale and the
    # item re-emerges.
    _save_seen(seen)

    # Night 4 fix — exclude items that already have a recent decision logged
    # (the DQ-10 false-alarm class). Title-token overlap + DQ-id body grep,
    # window = last 30 days.
    decisions = load_recent_decisions()
    pre_count = len(dq_rows) + len(inbox_rows)
    dq_rows = [r for r in dq_rows if not resolved_by_recent_decision(r, decisions)]
    inbox_rows = [r for r in inbox_rows if not resolved_by_recent_decision(r, decisions)]
    dropped = pre_count - len(dq_rows) - len(inbox_rows)
    if dropped:
        sys.stderr.write(
            f"stale_on_you: excluded {dropped} item(s) with recent decisions "
            f"in {DECISIONS_LOG.name}/ ({DECISIONS_LOG_LOOKBACK_DAYS}-day window)\n"
        )

    section = render_section(dq_rows + inbox_rows, today)

    if write_mode:
        status = write_inbox(section)
        sys.stdout.write(status + "\n")
    else:
        sys.stdout.write(section)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
