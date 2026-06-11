#!/usr/bin/env python3
"""Pre-brief Pass 13 (A-9) — routine + dispatch digest into today's daily note.

Re-scoped per the 2026-06-01 scan: dispatches-table-first (the most valuable
signal in the last 24h is what the daemon dispatched, not raw routine log
tails). Prints a self-contained markdown section that the pre-brief routine
appends once per morning to `_Iris_Memory/Daily/YYYY-MM-DD.md`.

Sources:
  - iris.db `dispatches` rows started in the last 24h
  - iris.db `expense_categorizer_runs` rows started in the last 24h
  - launchd routine log mtimes (claude-code-* + db-backup + log-rotate)
  - DAILY_BRIEFING.md mtime (morning briefing signal)

Surface-only. Output is the markdown body — the routine writes it.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = "/Volumes/AI_Workspace/iris_studio/iris.db"
LOGS_DIR = Path("/Users/steve/iris_studio/logs")
DAILY_BRIEFING_PATH = Path(
    "/Users/steve/Documents/3SK/outputs/DAILY_BRIEFING.md"
)
LOOKBACK = timedelta(hours=24)

LAUNCHD_LOG_NAMES = [
    ("claude-code-nightly", "claude-code-nightly.log"),
    ("claude-code-pre-brief", "claude-code-pre-brief.log"),
    ("claude-code-automation-scan", "claude-code-automation-scan.log"),
    ("claude-code-hygiene", "claude-code-hygiene.log"),
    ("claude-code-credential-check", "claude-code-credential-check.log"),
    ("db-backup", "db-backup.log"),
    ("log-rotate", "log-rotate.log"),
]


def _fmt_local(epoch_or_iso) -> str:
    if epoch_or_iso is None:
        return "—"
    if isinstance(epoch_or_iso, (int, float)):
        return datetime.fromtimestamp(epoch_or_iso).strftime("%H:%M")
    # iso-string fallback (completed_at, finished_at — set via datetime.now())
    try:
        return datetime.fromisoformat(str(epoch_or_iso)).strftime("%H:%M")
    except ValueError:
        return str(epoch_or_iso)[-8:-3]


def _duration_str(start_epoch, completed_iso) -> str:
    if start_epoch is None or completed_iso is None:
        return "—"
    try:
        end = datetime.fromisoformat(completed_iso)
        dur = end - datetime.fromtimestamp(start_epoch)
        m, s = divmod(int(dur.total_seconds()), 60)
        return f"{m}m {s}s"
    except (ValueError, TypeError):
        return "—"


def _dispatches_24h(cutoff_epoch: float) -> list[dict]:
    try:
        with sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                "SELECT id, agent_name, status, started_epoch, completed_at, "
                "deliverable_path, error "
                "FROM dispatches WHERE started_epoch IS NOT NULL "
                "AND started_epoch >= ? "
                "ORDER BY started_epoch ASC",
                (cutoff_epoch,),
            )
            return [dict(r) for r in cur.fetchall()]
    except sqlite3.Error:
        return []


def _expense_runs_24h(cutoff_local: datetime) -> list[dict]:
    # `started_at` is UTC (SQLite CURRENT_TIMESTAMP). Convert cutoff to UTC.
    offset = datetime.utcnow() - datetime.now()
    cutoff_utc = cutoff_local + offset
    try:
        with sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                "SELECT id, status, started_at, finished_at, draft_path, "
                "candidate_count "
                "FROM expense_categorizer_runs WHERE started_at >= ? "
                "ORDER BY started_at ASC",
                (cutoff_utc.isoformat(sep=" "),),
            )
            return [dict(r) for r in cur.fetchall()]
    except sqlite3.Error:
        return []


def _routine_logs_24h(now: datetime) -> list[tuple[str, datetime, int]]:
    """Return (label, mtime, size_bytes) for each launchd log written in
    the lookback window, sorted by mtime ascending."""
    cutoff = now - LOOKBACK
    out = []
    for label, fname in LAUNCHD_LOG_NAMES:
        p = LOGS_DIR / fname
        if not p.exists():
            continue
        st = p.stat()
        mtime = datetime.fromtimestamp(st.st_mtime)
        if mtime >= cutoff:
            out.append((label, mtime, st.st_size))
    out.sort(key=lambda t: t[1])
    return out


def _short(path: str | None, max_len: int = 60) -> str:
    if not path:
        return "—"
    if len(path) <= max_len:
        return path
    return "…" + path[-(max_len - 1):]


def main(argv: list[str]) -> int:
    now = datetime.now()
    cutoff_local = now - LOOKBACK
    cutoff_epoch = cutoff_local.timestamp()

    today = now.strftime("%Y-%m-%d")
    lines: list[str] = [
        f"## 🔔 Routine + dispatch digest (last 24h, pre-brief {today})",
        "",
        f"_Window: {cutoff_local:%Y-%m-%d %H:%M} → {now:%Y-%m-%d %H:%M} ET. "
        f"Source: iris.db dispatches + expense_categorizer_runs tables + "
        f"launchd log mtimes. Pass 13 (A-9) of pre-brief._",
        "",
    ]

    dispatches = _dispatches_24h(cutoff_epoch)
    lines.append("**Daemon dispatches:**")
    if not dispatches:
        lines.append("- (none in window)")
    else:
        for d in dispatches:
            started = _fmt_local(d["started_epoch"])
            dur = _duration_str(d["started_epoch"], d["completed_at"])
            status = d["status"]
            short_id = (d["id"] or "")[:8]
            tail = ""
            if d["deliverable_path"]:
                tail = f" → `{_short(d['deliverable_path'])}`"
            elif d["error"]:
                tail = f" — error: {str(d['error'])[:80]}"
            lines.append(
                f"- {started} — {d['agent_name']} ({status}, {dur}, id "
                f"{short_id}){tail}"
            )
    lines.append("")

    runs = _expense_runs_24h(cutoff_local)
    lines.append("**Expense-categorizer sweeps:**")
    if not runs:
        lines.append("- (none in window)")
    else:
        for r in runs:
            short_id = (r["id"] or "")[:8]
            count = r["candidate_count"] or 0
            tail = f" → `{_short(r['draft_path'])}`" if r["draft_path"] else ""
            lines.append(
                f"- {r['started_at']} UTC — status `{r['status']}`, "
                f"{count} candidate(s), id {short_id}{tail}"
            )
    lines.append("")

    routine_logs = _routine_logs_24h(now)
    lines.append("**Launchd routine activity:**")
    if not routine_logs:
        lines.append("- (no routine logs touched in window)")
    else:
        for label, mtime, size in routine_logs:
            lines.append(
                f"- {mtime:%H:%M} — `{label}` log written ({size:,} B)"
            )
    lines.append("")

    if DAILY_BRIEFING_PATH.exists():
        mb_mtime = datetime.fromtimestamp(DAILY_BRIEFING_PATH.stat().st_mtime)
        if mb_mtime >= cutoff_local:
            lines.append(
                f"**Morning briefing:** DAILY_BRIEFING.md last regenerated "
                f"{mb_mtime:%Y-%m-%d %H:%M}."
            )
        else:
            lines.append(
                f"**Morning briefing:** DAILY_BRIEFING.md mtime "
                f"{mb_mtime:%Y-%m-%d %H:%M} predates 24h window — possible "
                f"morning-brief skip (cross-check with A-22 Pass 12 output)."
            )
        lines.append("")

    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
