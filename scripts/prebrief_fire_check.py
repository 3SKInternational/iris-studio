#!/usr/bin/env python3
"""Pre-brief Pass 12 (A-22) — expected-vs-actual fire diff (last 24h).

Compares the set of scheduled fires that *should* have happened in the last
24h against on-disk evidence (launchd log mtimes, iris.db rows, deliverable
file mtimes). Prints `OK` when every expected fire is accounted for; prints
a numbered anomaly report otherwise.

Catches the silent-skip class — the 2026-06-01 App Nap miss where the morning
brief + project-manager-weekly + market-researcher-monthly all skipped with
zero error signal because APScheduler logged "missed by 7:54:35" and moved
on. Pairs with A-21 (rotation refill) for full cadence-observability.

Surface-only. Never modifies state. Safe to run unconditionally.
Output is consumed by `routines/pre-brief.prompt` Pass 12.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = "/Volumes/AI_Workspace/iris_studio/iris.db"
LOGS_DIR = Path("/Users/steve/iris_studio/logs")
DAILY_BRIEFING_PATH = Path(
    "/Users/steve/Documents/3SK/outputs/DAILY_BRIEFING.md"
)
DB_BACKUP_DIR = Path("/Volumes/AI_Workspace/iris_studio")

# How wide a window is acceptable for "the fire fired near its expected time."
# 2h covers normal apscheduler/launchd jitter + brief Mac-wake delays without
# being so wide it masks a real skip. The 6/1 App Nap skips were ~8h late.
ACCEPTABLE_LATENESS = timedelta(hours=2)
LOOKBACK = timedelta(hours=24)

# Mirror of iris.py MORNING_BRIEFING_HOUR. Kept here as a named constant so the
# fire-diff's expected morning-brief time stays in sync (was hard-coded 6 → stale
# after the 06:00→08:00 move on 2026-06-16, H1).
MORNING_BRIEFING_HOUR = 8

# Python's weekday(): Mon=0..Sun=6. LaunchD's Weekday is Sun=0..Sat=6 so we
# convert at the schedule table.
MON, TUE, WED, THU, FRI, SAT, SUN = 0, 1, 2, 3, 4, 5, 6


@dataclass
class Expected:
    """One scheduled fire that may have occurred in the last 24h."""

    name: str
    expected_at: datetime  # local-time naive (Mac is on ET)
    check: str  # "launchd_log" | "dispatch" | "expense_run" | "morning_brief" | "db_backup_file"
    log_path: Path | None = None
    agent_name: str | None = None


def _last_fire_daily(now: datetime, hour: int, minute: int) -> datetime | None:
    """Most recent (hour, minute) fire <= now within the lookback window."""
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate > now:
        candidate -= timedelta(days=1)
    if now - candidate <= LOOKBACK:
        return candidate
    return None


def _last_fire_weekly(
    now: datetime, weekdays: set[int], hour: int, minute: int
) -> datetime | None:
    """Most recent (hour, minute) fire on any matching weekday <= now within
    the lookback window. weekdays use Python's Mon=0..Sun=6 convention."""
    for days_back in range(0, 8):
        cand = (now - timedelta(days=days_back)).replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        if cand > now:
            continue
        if cand.weekday() in weekdays and now - cand <= LOOKBACK:
            return cand
    return None


def _last_fire_monthly(
    now: datetime, day_of_month: int, hour: int, minute: int
) -> datetime | None:
    """Fire on the Nth of the month at (hour, minute) iff it falls in the
    lookback window."""
    try:
        cand = now.replace(
            day=day_of_month, hour=hour, minute=minute, second=0, microsecond=0
        )
    except ValueError:
        return None
    if cand > now or now - cand > LOOKBACK:
        return None
    return cand


def collect_expected(now: datetime) -> list[Expected]:
    """Build the list of fires that *should* have happened in (now-24h, now]."""
    expected: list[Expected] = []

    # === Launchd cron jobs ===
    if (t := _last_fire_daily(now, 2, 0)):
        expected.append(Expected(
            "claude-code-nightly", t, "launchd_log",
            log_path=LOGS_DIR / "claude-code-nightly.log",
        ))
    if (t := _last_fire_daily(now, 5, 0)):
        expected.append(Expected(
            "claude-code-pre-brief", t, "launchd_log",
            log_path=LOGS_DIR / "claude-code-pre-brief.log",
        ))
    if (t := _last_fire_weekly(now, {SUN}, 4, 30)):
        expected.append(Expected(
            "claude-code-hygiene", t, "launchd_log",
            log_path=LOGS_DIR / "claude-code-hygiene.log",
        ))
    if (t := _last_fire_monthly(now, 1, 4, 15)):
        expected.append(Expected(
            "claude-code-credential-check", t, "launchd_log",
            log_path=LOGS_DIR / "claude-code-credential-check.log",
        ))
    if (t := _last_fire_weekly(now, {MON, THU}, 3, 0)):
        expected.append(Expected(
            "claude-code-automation-scan", t, "launchd_log",
            log_path=LOGS_DIR / "claude-code-automation-scan.log",
        ))
    if (t := _last_fire_daily(now, 4, 20)):
        expected.append(Expected(
            "db-backup", t, "db_backup_file",
            log_path=LOGS_DIR / "db-backup.log",
        ))
    if (t := _last_fire_weekly(now, {SUN}, 3, 0)):
        expected.append(Expected(
            "log-rotate", t, "launchd_log",
            log_path=LOGS_DIR / "log-rotate.log",
        ))

    # === iris.py daemon (APScheduler) ===
    # MUST match iris.py MORNING_BRIEFING_HOUR (currently 8 — moved 06:00→08:00
    # ET on 2026-06-16). A stale value here makes the check perpetually false-flag
    # the brief as a skip (H1); keep this in sync if iris.py changes.
    if (t := _last_fire_daily(now, MORNING_BRIEFING_HOUR, 0)):
        expected.append(Expected(
            "morning_briefing", t, "morning_brief",
        ))
    if (t := _last_fire_weekly(now, {SUN}, 4, 0)):
        expected.append(Expected(
            "expense_categorizer_sweep", t, "expense_run",
        ))
    if (t := _last_fire_weekly(now, {MON}, 5, 30)):
        expected.append(Expected(
            "project-manager-weekly", t, "dispatch",
            agent_name="project-manager",
        ))
    if (t := _last_fire_weekly(now, {WED}, 3, 0)):
        expected.append(Expected(
            "youtube-researcher-weekly", t, "dispatch",
            agent_name="youtube-researcher",
        ))
    if (t := _last_fire_monthly(now, 1, 3, 0)):
        expected.append(Expected(
            "market-researcher-monthly", t, "dispatch",
            agent_name="market-researcher",
        ))

    return expected


def _check_launchd_log(exp: Expected, now: datetime) -> tuple[bool, str]:
    p = exp.log_path
    if not p or not p.exists():
        return False, f"log file missing: {p}"
    mtime = datetime.fromtimestamp(p.stat().st_mtime)
    delta = mtime - exp.expected_at
    if abs(delta) <= ACCEPTABLE_LATENESS:
        return True, f"log mtime {mtime:%H:%M} (expected {exp.expected_at:%H:%M})"
    if delta < -ACCEPTABLE_LATENESS:
        return False, (
            f"log mtime {mtime:%Y-%m-%d %H:%M} predates expected fire "
            f"{exp.expected_at:%Y-%m-%d %H:%M} — routine did not run"
        )
    return False, (
        f"log mtime {mtime:%Y-%m-%d %H:%M} is {delta} after expected fire "
        f"{exp.expected_at:%Y-%m-%d %H:%M} — late or unrelated write"
    )


def _check_db_backup_file(exp: Expected, now: datetime) -> tuple[bool, str]:
    """Expect iris.db.YYYY-MM-DD.sql.gz dated on the expected fire date."""
    date_token = exp.expected_at.strftime("%Y-%m-%d")
    fname = DB_BACKUP_DIR / f"iris.db.{date_token}.sql.gz"
    if fname.exists():
        sz = fname.stat().st_size
        return True, f"{fname.name} present ({sz} B)"
    # Fall back to log mtime — the dump script may have rotated naming.
    return _check_launchd_log(exp, now)


def _check_morning_brief(exp: Expected, now: datetime) -> tuple[bool, str]:
    if not DAILY_BRIEFING_PATH.exists():
        return False, f"DAILY_BRIEFING.md missing at {DAILY_BRIEFING_PATH}"
    mtime = datetime.fromtimestamp(DAILY_BRIEFING_PATH.stat().st_mtime)
    delta = abs(mtime - exp.expected_at)
    if delta <= ACCEPTABLE_LATENESS:
        return True, f"DAILY_BRIEFING.md mtime {mtime:%H:%M}"
    return False, (
        f"DAILY_BRIEFING.md mtime {mtime:%Y-%m-%d %H:%M} too far from expected "
        f"{exp.expected_at:%Y-%m-%d %H:%M}"
    )


def _check_dispatch(exp: Expected, now: datetime) -> tuple[bool, str]:
    # iris.py writes `started_at` via SQLite's CURRENT_TIMESTAMP (UTC) but also
    # stores `started_epoch` as the timezone-independent ground truth — match
    # on that to dodge the UTC-vs-ET timestamp ambiguity. Old rows pre-dating
    # the epoch column would return NULL; the IS NOT NULL clause skips them.
    window_start_epoch = (exp.expected_at - ACCEPTABLE_LATENESS).timestamp()
    window_end_epoch = (exp.expected_at + ACCEPTABLE_LATENESS).timestamp()
    try:
        with sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True) as conn:
            cur = conn.execute(
                "SELECT id, status, started_epoch FROM dispatches "
                "WHERE agent_name = ? AND started_epoch IS NOT NULL "
                "AND started_epoch BETWEEN ? AND ? "
                "ORDER BY started_epoch DESC LIMIT 5",
                (exp.agent_name, window_start_epoch, window_end_epoch),
            )
            rows = cur.fetchall()
    except sqlite3.Error as e:
        return False, f"sqlite read failed: {e}"
    if not rows:
        return False, (
            f"no dispatches row for agent={exp.agent_name} near "
            f"{exp.expected_at:%Y-%m-%d %H:%M} (±2h) — cadence skipped"
        )
    statuses = [r[1] for r in rows]
    if "completed" in statuses:
        return True, f"dispatch completed (id {str(rows[0][0])[:8]}, status {rows[0][1]})"
    return False, (
        f"dispatch row exists but no 'completed' status — "
        f"{len(rows)} row(s), statuses={statuses}"
    )


def _check_expense_run(exp: Expected, now: datetime) -> tuple[bool, str]:
    # expense_categorizer_runs has no epoch column — convert expected fire from
    # local ET to UTC via the system offset to compare against the UTC-stored
    # `started_at`.
    offset = datetime.utcnow() - datetime.now()
    window_start_utc = exp.expected_at - ACCEPTABLE_LATENESS + offset
    window_end_utc = exp.expected_at + ACCEPTABLE_LATENESS + offset
    try:
        with sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True) as conn:
            cur = conn.execute(
                "SELECT id, status, started_at FROM expense_categorizer_runs "
                "WHERE started_at BETWEEN ? AND ? "
                "ORDER BY started_at DESC LIMIT 5",
                (window_start_utc.isoformat(sep=" "),
                 window_end_utc.isoformat(sep=" ")),
            )
            rows = cur.fetchall()
    except sqlite3.Error as e:
        return False, f"sqlite read failed: {e}"
    if not rows:
        return False, (
            f"no expense_categorizer_runs row near "
            f"{exp.expected_at:%Y-%m-%d %H:%M} (±2h) — sweep skipped"
        )
    statuses = [r[1] for r in rows]
    if "failed" in statuses:
        return False, f"expense run failed (id {str(rows[0][0])[:8]})"
    return True, f"expense run {rows[0][1]} (id {str(rows[0][0])[:8]})"


CHECKERS = {
    "launchd_log": _check_launchd_log,
    "db_backup_file": _check_db_backup_file,
    "morning_brief": _check_morning_brief,
    "dispatch": _check_dispatch,
    "expense_run": _check_expense_run,
}


def main(argv: list[str]) -> int:
    now = datetime.now()
    # Allow callers to skip checking the pre-brief job itself (we ARE the
    # pre-brief — our log hasn't been written yet at run-time).
    skip_self = "--skip-pre-brief-self" in argv

    expected = collect_expected(now)
    findings: list[str] = []
    green = 0
    for exp in expected:
        if skip_self and exp.name == "claude-code-pre-brief":
            continue
        checker = CHECKERS.get(exp.check)
        if not checker:
            findings.append(
                f"INTERNAL: no checker for kind={exp.check} ({exp.name})"
            )
            continue
        ok, msg = checker(exp, now)
        if ok:
            green += 1
        else:
            findings.append(
                f"{exp.name}: expected {exp.expected_at:%Y-%m-%d %H:%M} — {msg}"
            )

    if not findings:
        print(f"OK ({green}/{green} expected fires verified)")
        return 0

    print(f"ANOMALIES ({len(findings)} of {green + len(findings)} expected fires):")
    for i, line in enumerate(findings, 1):
        print(f"  {i}. {line}")
    return 0  # surface-only; never block the routine


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
