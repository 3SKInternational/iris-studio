#!/usr/bin/env python3
"""A-81 — external-precondition re-read sweep (read-only pre-brief pass).

DQ-16 sat 89 days "blocked on" a Mercury precondition Mercury never had. Eleven
weekly briefs re-read the ROW and re-affirmed it; nobody re-read the SOURCE.
The C2 age column (stale_on_you.py) showed the row the whole time and did not
help: age-of-item is not age-of-premise-check.

This flags open Decision_Queue rows that:
  (a) carry a structured `trigger:` in the Deadline cell,
  (b) whose trigger names an EXTERNAL precondition (not a calendar date, not
      Steve's own call, not our own metric/state like subs or video counts),
  (c) are older than AGE_DAYS,
and prints one line per row, at most once per THROTTLE_DAYS per row:

  DQ-X premise re-verify due (N+ days) — re-read the trigger's source, not the row.

Rows come from stale_on_you.parse_decision_queue() — no second DQ parser.
Age is the larger of the Days-pending cell (hand-bumped by vault-gardener, so
it freezes when the gardener is down) and today minus the cell's "(added M/D)".

Exit 0 + "CLEAN": nothing due. Exit 1: lines printed. Exit 75 + SKIP on stderr:
Decision_Queue missing or unreadable — nothing was checked. A bad state ledger
is ignored (rows re-print) and a failed ledger write only warns on stderr.
--dry-run (or SK_MUTATION_RUN=1) never writes the state ledger.
Never pages, never edits the vault; only writes its own state TSV.
"""

from __future__ import annotations

import datetime
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # mutequiv: insert position; scripts/ is the only home of stale_on_you
import stale_on_you  # noqa: E402

STATE_FILE = Path(os.environ.get(
    "PREMISE_REVERIFY_STATE", "/Users/steve/iris_studio/state/premise_reverify_seen.tsv"))
AGE_DAYS = 30
THROTTLE_DAYS = 7

# A trigger matching any of these is gated on a date, on Steve, or on our own
# channel state — re-reading an outside source can't change it. Anything else
# counts as external. Matched after parenthetical asides are stripped, so a
# "(was: 2026-07-15)" or "(re-verified 2026-10-12)" note can't silence a row.
# ponytail: keyword list, not a classifier. Unknown wording falls to "external"
# on purpose: one extra weekly line is cheaper than another 89-day miss.
INTERNAL = re.compile(
    r"^\W*(soft\W*)?\d{4}-\d{2}-\d{2}"   # the trigger IS a calendar date
    r"|steve'?s call|no hard deadline"   # Steve's own decision
    r"|subscriber|\bsubs\b|cadence"      # our channel metrics
    r"|published videos|session [a-z]\b|final cut",  # our production state
    re.IGNORECASE,
)


def trigger_text(deadline: str) -> str | None:
    m = re.search(r"\btrigger\**\s*(?::|\s[-–—]\s)\**\s*(.+)", deadline, re.IGNORECASE)
    return m.group(1).strip() if m else None


def age_days(cell: str, today: datetime.date) -> int:
    days = stale_on_you._days_int(cell)
    m = re.search(r"added (\d{1,2})/(\d{1,2})", cell)
    if m:
        try:
            added = datetime.date(today.year, int(m.group(1)), int(m.group(2)))
            if (added - today).days > 30:  # a December row read in January; a near-future typo stays near
                added = added.replace(year=today.year - 1)  # raises on 2/29 → falls back to the cell
        except ValueError:
            return days
        days = max(days, (today - added).days)
    return days


def due_rows(rows: list[dict[str, str]], today: datetime.date) -> list[tuple[str, int]]:
    """(id, days) for open rows whose external premise is past AGE_DAYS."""
    out = []
    for r in rows:
        trig = trigger_text(r["deadline"])
        if trig is None or INTERNAL.search(re.sub(r"\([^)]*\)", "", trig)):
            continue
        days = age_days(r["days"], today)
        if days > AGE_DAYS:
            out.append((r["id"], days))
    return out


def load_seen(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    seen = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) == 2:
            seen[parts[0]] = parts[1]
    return seen


def throttle(due: list[tuple[str, int]], seen: dict[str, str], today: datetime.date):
    """Split due rows into (emit now, updated seen ledger)."""
    emit, new_seen = [], dict(seen)
    for dq_id, days in due:
        last = seen.get(dq_id)
        if last:
            try:
                if (today - datetime.date.fromisoformat(last)).days < THROTTLE_DAYS:
                    continue
            except ValueError:
                pass  # corrupt date → treat as never flagged
        emit.append((dq_id, days))
        new_seen[dq_id] = today.isoformat()
    return emit, new_seen


def main(argv: list[str]) -> int:
    dry = "--dry-run" in argv or os.environ.get("SK_MUTATION_RUN") == "1"
    today = datetime.date.today()
    try:
        if not stale_on_you.DECISION_QUEUE.exists():
            raise FileNotFoundError(stale_on_you.DECISION_QUEUE)
        rows = stale_on_you.parse_decision_queue()
    except Exception as e:
        print(f"premise_reverify_check: SKIP — Decision_Queue unreadable: {type(e).__name__}: {e}", file=sys.stderr)
        return 75
    try:
        seen = load_seen(STATE_FILE)
    except Exception as e:  # a bad ledger re-prints rows; it must never hide them
        print(f"premise_reverify_check: state ledger unreadable, ignoring it: {type(e).__name__}: {e}", file=sys.stderr)
        seen = {}
    emit, new_seen = throttle(due_rows(rows, today), seen, today)
    if not emit:
        print("premise_reverify_check: CLEAN")
        return 0
    for dq_id, days in emit:
        print(f"{dq_id} premise re-verify due ({days}+ days) — re-read the trigger's source, not the row.")
    if not dry:
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = STATE_FILE.with_suffix(".tmp")
            tmp.write_text("".join(f"{k}\t{v}\n" for k, v in sorted(new_seen.items())), encoding="utf-8")
            tmp.replace(STATE_FILE)
        except Exception as e:  # lines already printed; only the weekly throttle is lost
            print(f"premise_reverify_check: state ledger write failed: {type(e).__name__}: {e}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
