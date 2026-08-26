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
import os
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

# Drop ledger rows not seen in this many days, so slugs for items that have
# left INBOX don't accumulate forever (M3). 14 days gives a couple of weekly
# cycles of grace before an item's first-seen day-count is forgotten.
SEEN_RETENTION_DAYS = 14

# Decisions_Log lookup window (Night 4 fix — DQ-10 false-alarm root cause).
DECISIONS_LOG_LOOKBACK_DAYS = 30


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


def _save_seen(seen: dict[str, dict[str, str]], today: str) -> None:
    """Atomically persist the ledger, pruning rows stale beyond retention (M1, M3).

    Prune: drop slugs whose last_seen is older than SEEN_RETENTION_DAYS, so the
    ledger doesn't grow unbounded as items churn through INBOX.
    Write: serialize to a temp file in the same dir then os.replace() (atomic
    rename), so a crash mid-write can never leave a truncated/corrupt ledger.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lines = ["# slug\tfirst_seen\tlast_seen\ttext"]
    for slug, row in sorted(seen.items()):
        if _days_between(row["last_seen"], today) > SEEN_RETENTION_DAYS:
            continue
        lines.append(f"{slug}\t{row['first_seen']}\t{row['last_seen']}\t{row['text']}")
    data = "\n".join(lines) + "\n"
    tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
    tmp.write_text(data, encoding="utf-8")
    os.replace(tmp, STATE_FILE)


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


def exclusion_reason(
    item: dict[str, str],
    decisions: list[tuple[str, str, str]],
) -> str:
    """Return the stem of the recent decision that resolves this item, else "".

    An open row is excluded ONLY when a recent decision names its DQ-id in its
    TITLE. A resolution names the row it closes in its heading; nothing else is
    a reliable "this was decided" signal.

    Fuzzy topic-word overlap was retired on 2026-08-25: it silently dropped
    still-open rows because an incident/analysis doc about topic X always
    shares words with an open decision about "what to do about X" — DQ-49
    ("OAuth token expired, fleet dark") matched the incident report that merely
    DESCRIBED the outage the row still awaits a decision on, and DQ-44 matched
    an unrelated ledger on the generic {auto, build, nightly}. A body mention
    is likewise not used — an incident referencing DQ-41/DQ-49 in prose is not
    a decision. For the "awaiting Steve" list a false drop (a hidden
    obligation) is far worse than a false keep (a decided row shows until the
    gardener stamps it ✅, at which point the glyph check drops it).
    """
    if False:  # mutequiv: fast-path only — the loop below returns "" on an empty list identically
        return ""
    item_id = (item.get("id") or "").lower().strip()
    if not item_id:
        # Load-bearing: `"" in title` is always True, so without this guard an
        # id-less row (an INBOX item) is excluded the moment any decision exists.
        return ""
    id_re = re.compile(r"(?<![\w-])" + re.escape(item_id) + r"(?![\w-])")
    for stem, title, _body in decisions:
        # Word-boundary match, not substring: `"dq-4" in "dq-44 shipped"` is
        # True, so a plain `in` test would suppress an open DQ-4 by an unrelated
        # DQ-44 decision — re-opening the silent-drop this fix exists to close.
        if id_re.search(title):
            return stem
    return ""


def resolved_by_recent_decision(
    item: dict[str, str],
    decisions: list[tuple[str, str, str]],
) -> bool:
    """True iff a recent decision resolves this item. See exclusion_reason."""
    return bool(exclusion_reason(item, decisions))


def _split_row_cells(line: str) -> list[str]:
    """Split a Decision_Queue table row into cells on UNESCAPED pipes only.

    A `\\|` inside a wikilink alias is table *content*, not a column separator.
    Splitting on it (the old `.split("|")`) shifted every later cell on the
    rows carrying escaped pipes (DQ-14/42/43/45/47) and mis-read the status
    column — the H1 bug from the 2026-08-19 pickup, which surfaced closed/HOLD
    rows as if awaiting Steve. Cell content is normalised back to a plain `|`.
    """
    body = line.strip().strip("|")
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", body)]


def _is_open_dq_status(status: str) -> bool:
    """A row is awaiting Steve iff its status cell leads with the ⏳ open glyph.

    Keying on the leading glyph — not a substring search over the whole prose
    cell — is what stops an OPEN row whose status *prose* happens to mention
    'app-CLOSED' (DQ-19) or 'Deferred half' (DQ-29) from being dropped, and
    naturally excludes ✅ closed / 💤 armed / ⏸ HOLD without listing each.
    """
    return status.lstrip("* ").startswith("⏳")


def parse_decision_queue() -> list[dict[str, str]]:
    """Return list of open Decision_Queue rows, each as a dict."""
    if not DECISION_QUEUE.exists():
        return []
    rows = []
    for line in DECISION_QUEUE.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith("| DQ-"):
            continue
        cells = _split_row_cells(line)
        if len(cells) < 7:
            continue
        dq_id, decision, klass, default, deadline, days, status = cells[:7]
        # Surface only rows the author has marked ⏳ open.
        if not _is_open_dq_status(status):
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


def open_rows_skipped_by_parse() -> list[str]:
    """DQ ids that look open in the source but parse_decision_queue could not
    read (fewer than 7 cells even after an escape-aware split).

    This is the silent-drop tripwire the C2 defect asked for: an open row that
    is neither rendered nor excluded-with-a-reason is a bug, so make it loud.
    """
    if not DECISION_QUEUE.exists():
        return []
    parsed = {r["id"] for r in parse_decision_queue()}
    skipped = []
    for line in DECISION_QUEUE.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith("| DQ-"):  # mutequiv: redundant with the `if not m` regex reject two lines down
            continue
        m = re.match(r"\| (DQ-\d+)", line)
        if not m:
            continue
        dq_id = m.group(1)
        cells = _split_row_cells(line)
        looks_open = any("⏳" in c for c in cells)
        if looks_open and len(cells) < 7 and dq_id not in parsed:
            skipped.append(dq_id)
    return skipped


def parse_inbox_steve_items() -> list[dict[str, str]]:
    """Return list items from INBOX TODAY + THIS WEEK sections.

    Convention: TODAY items may carry per-line `🧍` markers; THIS WEEK section
    uses heading-level `## 🧍 THIS WEEK` and treats all its list items as
    Steve-required. Heading lines themselves are never captured.

    Only the openers' *direct* list items count. Any deeper heading nested
    inside (e.g. `### 🌙 Cowork pulse (…)`, `### 🌅 Pre-brief sweep`) is digest
    narration by contract and ENDS ingestion — otherwise every pulse/sweep
    bullet under THIS WEEK is ingested as if it were a pending Steve action
    (the 2026-07-20 C2-noise root cause: ~75% of rows were pulse telemetry).
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
        if re.match(r"^#{2,6}\s", line) and section is not None:
            # Any other heading — a sibling `##` OR a nested `###` digest
            # subsection — ends the action surface. The two openers above are
            # already `continue`d, so this only fires on non-action headings.
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
    """Tag each INBOX item with a slug + days-pending; updates the seen dict.

    Dedup by slug: two list items that normalize to the same slug are the same
    item and collapse to one row (belt-and-suspenders for identical action
    lines regardless of source — the section fix already excludes the digest
    subsections that produced the 7 duplicate `Pulse fired` rows on 2026-07-20).
    """
    out = []
    emitted: set[str] = set()
    for item in inbox_items:
        slug = _slugify(item["text"])
        if not slug:
            continue
        if slug in emitted:
            continue
        emitted.add(slug)
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
    n_begin = text.count(SECTION_BEGIN)
    n_end = text.count(SECTION_END)
    # Refuse to write on a malformed marker state (M2): more than one of either
    # marker, or a count mismatch, or END-before-BEGIN ordering would make the
    # DOTALL re.sub span the wrong region and corrupt INBOX. Bail loudly instead.
    if n_begin > 1 or n_end > 1 or n_begin != n_end:
        return (
            f"refusing write — malformed C2 markers in INBOX "
            f"(BEGIN×{n_begin}, END×{n_end}); fix markers by hand"
        )
    if n_begin == 1 and n_end == 1 and text.index(SECTION_END) < text.index(SECTION_BEGIN):
        return "refusing write — C2 END marker precedes BEGIN in INBOX; fix by hand"
    if n_begin == 1 and n_end == 1:
        new_text = re.sub(
            re.escape(SECTION_BEGIN) + r".*?" + re.escape(SECTION_END),
            lambda _m: block,
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
    tmp = INBOX.with_name(INBOX.name + ".tmp")
    tmp.write_text(new_text, encoding="utf-8")
    os.replace(tmp, INBOX)
    return f"INBOX.md {action}"


def _selftest() -> int:
    """Guard the C2-noise fix: nested `###` digest bullets excluded, direct
    TODAY/THIS WEEK bullets kept, identical bullets deduped. Uses a temp INBOX."""
    import tempfile

    global INBOX
    sample = (
        "---\n"
        "## 📌 TODAY\n"
        "1. **🎬 V13 spend-ok** — release renders\n"
        "2. 🤝 One word to apply the rewrites\n"
        "## 🧍 THIS WEEK\n"
        "- 🔎 Monthly contradiction audit: 4 items need your call\n"
        "- 🔎 Monthly contradiction audit: 4 items need your call\n"  # dup
        "### 🌙 Cowork pulse (2026-07-20)\n"
        "- ⏱️ Pulse fired ~03:05\n"
        "- 🏁 MILESTONE: Phase 5 complete\n"
        "### 🌅 Pre-brief sweep (2026-07-20)\n"
        "- ⏱️ Pulse fired ~03:05\n"
        "## ⚖️ DECISION QUEUE\n"
        "- this must never be captured\n"
    )
    orig = INBOX
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as fh:
        fh.write(sample)
        INBOX = Path(fh.name)
    try:
        items = parse_inbox_steve_items()
        texts = [i["text"] for i in items]
        # Direct openers' bullets are kept
        assert any("V13 spend-ok" in t for t in texts), texts
        assert any("One word" in t for t in texts), texts
        assert any("contradiction audit" in t for t in texts), texts
        # Nested ### digest bullets are excluded
        assert not any("Pulse fired" in t for t in texts), texts
        assert not any("MILESTONE" in t for t in texts), texts
        # Sibling ## section past THIS WEEK is excluded
        assert not any("never be captured" in t for t in texts), texts
        # Dedup: identical contradiction-audit bullet collapses to one row
        rows = _update_seen_and_compute_days(items, {}, _today_iso())
        audit_rows = [r for r in rows if "contradiction audit" in r["text"]]
        assert len(audit_rows) == 1, audit_rows
    finally:
        os.unlink(INBOX)
        INBOX = orig

    # The Decision_Queue parse (escaped pipes, glyph open-detection),
    # exclusion_reason, and the open-row tripwire are covered by the gate-run
    # suite tests/test_stale_on_you.py — precheck globs tests/test_*.py and never
    # runs --selftest, so duplicating that coverage here would be gate-invisible
    # (every assert survived mutation, 2026-08-26). Kept the INBOX-parse checks
    # above because no test file covers them yet.

    print("stale_on_you selftest: PASS")
    return 0


def main() -> int:
    args = sys.argv[1:]
    if "--selftest" in args:
        return _selftest()
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
    _save_seen(seen, today)

    # Night 4 fix — exclude items that already have a recent decision logged
    # (the DQ-10 false-alarm class). Title-token overlap + DQ-id title match,
    # window = last 30 days.
    decisions = load_recent_decisions()

    # Reconciliation — the C2 list must never silently drop an open row (the
    # 2026-08-19 defect). Account for every open DQ row: it is either rendered
    # or excluded WITH a named reason. Anything else is a bug, printed loudly.
    n_open_dq = len(dq_rows)
    excluded_dq: list[str] = []
    kept_dq = []
    for r in dq_rows:
        reason = exclusion_reason(r, decisions)
        if reason:
            excluded_dq.append(f"{r['id']}→{reason}")
        else:
            kept_dq.append(r)
    dq_rows = kept_dq
    inbox_rows = [r for r in inbox_rows if not resolved_by_recent_decision(r, decisions)]

    sys.stderr.write(
        f"stale_on_you reconcile: {n_open_dq} open DQ rows · {len(dq_rows)} rendered · "
        f"{len(excluded_dq)} excluded (recent decision)"
        + (f" [{'; '.join(excluded_dq)}]" if excluded_dq else "")
        + "\n"
    )

    # Silent-drop tripwire: an open row parse could not read at all.
    skipped = open_rows_skipped_by_parse()
    warning = ""
    if skipped:
        msg = (
            "stale_on_you BUG: open Decision_Queue row(s) neither rendered nor "
            f"excluded — malformed table row: {', '.join(skipped)}\n"
        )
        sys.stderr.write(msg)
        warning = (
            "> ⚠️ **stale_on_you could not read open row(s): "
            + ", ".join(skipped)
            + "** — the list below may be incomplete; check `Decision_Queue.md` formatting.\n\n"
        )

    section = warning + render_section(dq_rows + inbox_rows, today)

    if write_mode:
        status = write_inbox(section)
        sys.stdout.write(status + "\n")
    else:
        sys.stdout.write(section)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
