#!/usr/bin/env python3
"""Pre-brief Pass 12 (A-22, A-35) — expected-vs-actual fire diff (last 24h).

Compares the set of scheduled fires that *should* have happened in the last
24h against on-disk evidence (launchd log mtimes, iris.db rows, deliverable
file mtimes). Prints `OK` when every expected fire is accounted for; prints
a numbered anomaly report otherwise.

A-35: the launchd-job coverage is now auto-derived from the plists in
~/Library/LaunchAgents (com.iris.claude-code-*.plist) via `collect_launchd_expected`,
not a hand-maintained table — so newly-added scheduled routines get silent-skip
detection automatically instead of firing uncovered. Interval/manual jobs are
listed as a coverage note (the daily/weekly model doesn't fit them yet).

The iris.py APScheduler dispatch fires (chief-of-staff-weekly, market-researcher-
monthly, decision-feeder-deadline-watch) are likewise auto-derived from the
daemon's own AUTONOMOUS_DISPATCHES list (`collect_dispatch_expected`) instead of a
re-typed schedule — same single-source-of-truth reason, and it closed the gap
where decision-feeder had no silent-skip coverage at all.

Catches the silent-skip class — the 2026-06-01 App Nap miss where the morning
brief + chief-of-staff-weekly + market-researcher-monthly all skipped with
zero error signal because APScheduler logged "missed by 7:54:35" and moved
on. Pairs with A-21 (rotation refill) for full cadence-observability.

Surface-only. Never modifies state. Safe to run unconditionally.
Output is consumed by `routines/pre-brief.prompt` Pass 12.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
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

# === A-35: auto-derive launchd-job coverage from the plists themselves =========
# The hardcoded table below covers ~12 fires, but ~/Library/LaunchAgents holds
# 38 com.iris.claude-code-*.plist jobs — the rest fired with NO silent-skip
# detection (A-22 reported OK while any could be dead). This derives an Expected
# per calendar-scheduled claude-code plist straight from the schedule on disk.
LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
CLAUDE_PLIST_GLOB = "com.iris.claude-code-*.plist"

# Plists already covered by a hardcoded check below — auto-derive skips them to
# avoid a duplicate Expected for the same fire. These 5 claude-code launchd jobs
# keep their proven tee-log paths.
#
# youtube-research was in this set until 2026-07-22, deferring to a hardcoded
# dispatch-row check. That check could never pass: the job left iris.py's
# APScheduler for a launchd plist on 2026-06-20 (see iris.py's "NOT dispatched
# here" note) and run_claude_job.sh writes no dispatches row, so its last row is
# 2026-06-17. The hardcoded weekday had also rotted independently ({WED}, while
# the plist is Mon+Thu). Auto-derive reads the plist itself — no retyped
# schedule to drift again. Signal weakens from "agent completed" to "wrapper log
# written", which beats a check that always fails; restoring the stronger signal
# means making run_claude_job.sh write a dispatch row, a separate job.
_AUTODERIVE_SKIP = {
    "com.iris.claude-code-nightly",
    "com.iris.claude-code-pre-brief",
    "com.iris.claude-code-hygiene",
    "com.iris.claude-code-credential-check",
    "com.iris.claude-code-automation-scan",
}


def _load_plist(path: Path) -> dict | None:
    """Read a plist via `plutil -convert json` rather than plistlib: 10 of these
    plists carry explanatory XML comments containing `--` (e.g. "claude --print"),
    which is illegal XML that Python's expat rejects but launchd/plutil accept.
    plutil is the same lenient CFPropertyList parser launchd uses. None on any
    failure (missing tool, bad exit, bad JSON) so a single odd plist can't crash
    the whole fire-check."""
    try:
        out = subprocess.run(
            ["plutil", "-convert", "json", "-o", "-", str(path)],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode != 0:
            return None
        return json.loads(out.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _launchd_wd_to_py(wd: int) -> int:
    """launchd Weekday (Sun=0..Sat=6) → Python weekday() (Mon=0..Sun=6)."""
    return (wd + 6) % 7


def _derive_log_path(data: dict, name: str) -> Path:
    """The freshness signal is the wrapper's tee log, NOT StandardOutPath —
    run_job.sh sends real output to its $LOG and stdout to /dev/null, so the
    .stdout.log is near-empty/stale even when the job ran. Both wrappers name
    their log deterministically from the JOB arg:
      run_claude_job.sh <JOB> …  →  claude-code-<JOB>.log
      run_job.sh <JOB> …         →  job-<JOB>.log
    Joining ProgramArguments handles both the `-lc "<script> <JOB> …"` single-
    string form and the split-element form. Falls back to StandardOutPath."""
    joined = " ".join(str(a) for a in (data.get("ProgramArguments") or []))
    if (mcj := re.search(r"run_claude_job\.sh\s+(\S+)", joined)):
        return LOGS_DIR / f"claude-code-{mcj.group(1)}.log"
    if (mj := re.search(r"run_job\.sh\s+(\S+)", joined)):
        return LOGS_DIR / f"job-{mj.group(1)}.log"
    out = data.get("StandardOutPath")
    return Path(out) if out else (LOGS_DIR / f"{name}.log")


def _expected_from_calendar_entry(
    now: datetime, entry: dict, name: str, log_path: Path
) -> Expected | None:
    """One StartCalendarInterval dict → an Expected iff it fired in the last 24h.
    Returns None for hourly/wildcard-hour entries (no single daily fire to model)
    and for fires outside the lookback window."""
    hour = entry.get("Hour")
    if hour is None:
        return None
    minute = entry.get("Minute", 0)
    if "Weekday" in entry:
        t = _last_fire_weekly(now, {_launchd_wd_to_py(entry["Weekday"])}, hour, minute)
    elif "Day" in entry:
        t = _last_fire_monthly(now, entry["Day"], hour, minute)
    else:
        t = _last_fire_daily(now, hour, minute)
    if t is None:
        return None
    return Expected(name, t, "launchd_log", log_path=log_path)


def collect_launchd_expected(
    now: datetime,
) -> tuple[list[Expected], list[tuple[str, str]]]:
    """Auto-derive launchd_log Expecteds from every calendar-scheduled
    com.iris.claude-code-*.plist. Returns (expected, not_checked) where
    not_checked is [(name, reason)] for interval/manual/unparseable plists — so
    the report can be honest about what it does NOT cover (no silent confidence)."""
    expected: list[Expected] = []
    not_checked: list[tuple[str, str]] = []
    if not shutil.which("plutil") or not LAUNCH_AGENTS_DIR.is_dir():
        return expected, not_checked  # not on macOS / no agents dir — hardcoded-only
    for path in sorted(LAUNCH_AGENTS_DIR.glob(CLAUDE_PLIST_GLOB)):
        data = _load_plist(path)
        if data is None:
            not_checked.append((path.stem, "unparseable plist"))
            continue
        label = data.get("Label", path.stem)
        if label in _AUTODERIVE_SKIP:
            continue
        name = label.replace("com.iris.", "")
        sci = data.get("StartCalendarInterval")
        if sci is None:
            interval = data.get("StartInterval")
            reason = (
                f"StartInterval={interval}s — not coverage-checked"
                if interval else "no schedule (RunAtLoad/manual) — not coverage-checked"
            )
            not_checked.append((name, reason))
            continue
        log_path = _derive_log_path(data, name)
        entries = sci if isinstance(sci, list) else [sci]
        for entry in entries:
            exp = _expected_from_calendar_entry(now, entry, name, log_path)
            if exp:
                expected.append(exp)
    return expected, not_checked


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


# === Auto-derive iris.py APScheduler dispatch coverage from the daemon itself ===
# The two dispatch checks below (chief-of-staff-weekly / market-researcher-monthly)
# used to re-type iris.py's schedule by hand — the same drift class that made the
# old youtube-research {WED} check and the morning-brief-hour check phantom-alert
# (a schedule moved in the daemon, the hand-typed copy here rotted). This reads
# iris.py's OWN `AUTONOMOUS_DISPATCHES` list, so the daemon is the single source of
# truth. It also picked up decision-feeder-deadline-watch, which had NO coverage
# at all. Verified every entry writes a `dispatches` row on fire (decision-feeder
# records one daily even on its skip-on-empty nights), so all are detectable.
IRIS_PY_PATH = Path(__file__).resolve().parent.parent / "iris.py"

# APScheduler day_of_week names → Python weekday() ints (both use Mon=0).
_DOW_NAMES = {"mon": MON, "tue": TUE, "wed": WED, "thu": THU,
              "fri": FRI, "sat": SAT, "sun": SUN}


def _parse_dow(spec) -> set[int]:
    """APScheduler day_of_week ('mon', 'mon,thu', or 0-6 Mon=0) → weekday set."""
    out: set[int] = set()
    for tok in str(spec).split(","):
        tok = tok.strip().lower()
        if tok in _DOW_NAMES:
            out.add(_DOW_NAMES[tok])
        elif tok.isdigit():
            out.add(int(tok) % 7)  # APScheduler numeric dow is Mon=0 too
    return out


def _load_autonomous_dispatches(path: Path = IRIS_PY_PATH) -> list[dict]:
    """AST-extract iris.py's AUTONOMOUS_DISPATCHES without importing the daemon
    (importing iris.py would pull telegram/apscheduler/anthropic + run side
    effects). Returns [] on any read/parse failure — degrade safe, never crash the
    pre-brief. Adjacent-string prompts fold to one Constant at parse time, so
    literal_eval handles each entry; a non-literal entry is skipped, not fatal."""
    import ast
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError):
        return []
    for node in tree.body:
        # The real declaration is annotated (`AUTONOMOUS_DISPATCHES: list[dict] =
        # [...]` → ast.AnnAssign), so accept both AnnAssign and plain Assign.
        if isinstance(node, ast.AnnAssign):
            targets = [node.target]
        elif isinstance(node, ast.Assign):
            targets = node.targets
        else:
            continue
        if not isinstance(node.value, ast.List):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "AUTONOMOUS_DISPATCHES"
                   for t in targets):
            continue
        out: list[dict] = []
        for elt in node.value.elts:
            try:
                val = ast.literal_eval(elt)
            except (ValueError, SyntaxError, TypeError):
                continue  # a non-literal entry — skip, don't crash
            if isinstance(val, dict):
                out.append(val)
        return out
    return []


def collect_dispatch_expected(now: datetime, path: Path = IRIS_PY_PATH) -> list[Expected]:
    """Derive the autonomous-dispatch fires straight from iris.py's own schedule."""
    expected: list[Expected] = []
    for entry in _load_autonomous_dispatches(path):
        agent = entry.get("agent_name")
        name = entry.get("name")
        tk = entry.get("trigger_kwargs") or {}
        if not agent or not name or not isinstance(tk, dict):
            continue
        try:
            hour = int(tk.get("hour", 0))
            minute = int(tk.get("minute", 0))
        except (TypeError, ValueError):
            continue
        if "day_of_week" in tk:
            t = _last_fire_weekly(now, _parse_dow(tk["day_of_week"]), hour, minute)
        elif "day" in tk:
            try:
                t = _last_fire_monthly(now, int(tk["day"]), hour, minute)
            except (TypeError, ValueError):
                continue
        else:
            t = _last_fire_daily(now, hour, minute)
        if t:
            expected.append(Expected(name, t, "dispatch", agent_name=agent))
    return expected


def collect_expected(now: datetime) -> list[Expected]:
    """Build the list of fires that *should* have happened in (now-24h, now]."""
    expected: list[Expected] = []

    # === Launchd cron jobs ===
    if (t := _last_fire_daily(now, 3, 0)):
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
    if (t := _last_fire_weekly(now, {MON, THU}, 3, 30)):
        expected.append(Expected(
            "claude-code-automation-scan", t, "launchd_log",
            log_path=LOGS_DIR / "claude-code-automation-scan.log",
        ))
    if (t := _last_fire_daily(now, 4, 20)):
        expected.append(Expected(
            "db-backup", t, "db_backup_file",
            log_path=LOGS_DIR / "db-backup.log",
        ))
    if (t := _last_fire_weekly(now, {SUN}, 3, 10)):
        expected.append(Expected(
            "log-rotate", t, "launchd_log",
            log_path=LOGS_DIR / "log-rotate.log",
        ))

    # === iris.py daemon (APScheduler) ===
    # MUST match iris.py MORNING_BRIEFING_HOUR (currently 8 — moved 06:00→08:00
    # ET on 2026-06-16). This is the ONLY scheduled writer of DAILY_BRIEFING.md;
    # overnight rewrites (Claude Code sessions / manual /briefing) only ever make
    # it fresher, and _check_morning_brief treats fresher-than-expected as OK.
    if (t := _last_fire_daily(now, MORNING_BRIEFING_HOUR, 0)):
        expected.append(Expected(
            "morning_briefing", t, "morning_brief",
        ))
    if (t := _last_fire_weekly(now, {SUN}, 4, 0)):
        expected.append(Expected(
            "expense_categorizer_sweep", t, "expense_run",
        ))
    # chief-of-staff-weekly / market-researcher-monthly / decision-feeder-daily
    # are auto-derived from iris.py's AUTONOMOUS_DISPATCHES (see collect_dispatch_
    # expected) — single source of truth, no re-typed schedule to rot. youtube-
    # researcher-weekly used to be hand-typed here too; it moved to a launchd plist
    # (now covered by collect_launchd_expected off the plist, per _AUTODERIVE_SKIP).
    expected += collect_dispatch_expected(now)

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
    # Freshness is DIRECTIONAL: a briefing NEWER than the expected 08:00 fire is
    # never a skip. Overnight Claude Code sessions and manual /briefing regens
    # legitimately rewrite DAILY_BRIEFING.md before 08:00 (e.g. 03:26), which
    # only makes it MORE current — the old symmetric abs() window wrongly flagged
    # those fresher files as anomalies EVERY such night (A-22 false-flag). Only a
    # STALE file (older than expected minus grace) signals the brief silently
    # didn't happen — the 2026-06-01 App Nap class this check exists for.
    if not DAILY_BRIEFING_PATH.exists():
        return False, f"DAILY_BRIEFING.md missing at {DAILY_BRIEFING_PATH}"
    mtime = datetime.fromtimestamp(DAILY_BRIEFING_PATH.stat().st_mtime)
    floor = exp.expected_at - ACCEPTABLE_LATENESS
    if mtime >= floor:
        return True, f"DAILY_BRIEFING.md mtime {mtime:%Y-%m-%d %H:%M} (fresh; ≥ {floor:%H:%M} floor)"
    return False, (
        f"DAILY_BRIEFING.md mtime {mtime:%Y-%m-%d %H:%M} is stale — older than "
        f"expected fire {exp.expected_at:%Y-%m-%d %H:%M} minus grace — brief may have skipped"
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


def _selftest_youtube_coverage() -> int:
    """Regression pin for the 2026-07-22 fix: youtube-research must be covered by
    auto-derive off its own plist, and only on its real fire days (Mon+Thu 02:00).
    The old hardcoded {WED} dispatch check alerted on a day the plist doesn't
    schedule while never checking the two days it does — and no selftest case
    existed to catch it. Reads the live plist, so it also fails if the schedule
    changes on disk without this expectation following: that is the point.
    Skips (0 failures) off-macOS / with no LaunchAgents dir."""
    label = "com.iris.claude-code-youtube-research"
    if not shutil.which("plutil") or not (LAUNCH_AGENTS_DIR / f"{label}.plist").is_file():
        print("  [SKIP] youtube-research coverage: plist/plutil unavailable")
        return 0
    if label in _AUTODERIVE_SKIP:
        print(f"  [FAIL] youtube-research coverage: {label} is back in _AUTODERIVE_SKIP "
              "— auto-derive is its only coverage, so skipping it leaves the job unchecked")
        return 1
    failures = 0
    # 2026-07-20 Mon / 2026-07-23 Thu are fire days; 2026-07-21 Tue and
    # 2026-07-22 Wed (the old phantom-alert day) are not.
    for label_txt, now, want in [
        ("Mon 2026-07-20 05:00 (fire day)", datetime(2026, 7, 20, 5, 0), True),
        ("Thu 2026-07-23 05:00 (fire day)", datetime(2026, 7, 23, 5, 0), True),
        ("Tue 2026-07-21 05:00 (not a fire day)", datetime(2026, 7, 21, 5, 0), False),
        ("Wed 2026-07-22 05:00 (old phantom day)", datetime(2026, 7, 22, 5, 0), False),
    ]:
        derived, _ = collect_launchd_expected(now)
        got = any(e.name == "claude-code-youtube-research" for e in derived)
        status = "PASS" if got == want else "FAIL"
        if got != want:
            failures += 1
        print(f"  [{status}] youtube-research {label_txt}: expected-emitted={got} (want {want})")
    return failures


def _selftest_dispatch_coverage() -> int:
    """Pins the iris.py dispatch auto-derive (the drift-rot fix). Two parts:
    (1) HERMETIC — parse a fixture AUTONOMOUS_DISPATCHES (weekly/monthly/daily,
        with an adjacent-string prompt like the real entries) and assert each
        maps to an Expected at fire-time and to nothing off-schedule.
    (2) LIVE — read the real iris.py; skip if unavailable; else assert every
        entry is structurally coverable (agent_name + name + a trigger we map),
        so a schedule that stops being parseable/coverable fails loudly here."""
    import os
    import tempfile
    failures = 0

    fixture = (
        'AUTONOMOUS_DISPATCHES = [\n'
        '    {\n'
        '        "name": "cos-weekly",\n'
        '        "agent_name": "chief-of-staff",\n'
        '        # a comment between keys, like the real entries\n'
        '        "trigger_kwargs": {"day_of_week": "mon", "hour": 5, "minute": 30},\n'
        '        "prompt": (\n'
        '            "line one "\n'
        '            "line two"\n'
        '        ),\n'
        '    },\n'
        '    {\n'
        '        "name": "mr-monthly",\n'
        '        "agent_name": "market-researcher",\n'
        '        "trigger_kwargs": {"day": 1, "hour": 2, "minute": 0},\n'
        '        "prompt": "x",\n'
        '    },\n'
        '    {\n'
        '        "name": "df-daily",\n'
        '        "agent_name": "decision-feeder",\n'
        '        "trigger_kwargs": {"hour": 3, "minute": 40},\n'
        '        "prompt": "y",\n'
        '    },\n'
        ']\n'
    )
    fd, tmp = tempfile.mkstemp(suffix="_iris_fixture.py")
    os.close(fd)
    try:
        Path(tmp).write_text(fixture, encoding="utf-8")
        tp = Path(tmp)
        # Mon 2026-07-20 06:00 — weekly + daily fired in last 24h, monthly did not.
        got = {e.agent_name for e in collect_dispatch_expected(datetime(2026, 7, 20, 6, 0), tp)}
        for want_agent, want_in in [
            ("chief-of-staff", True),   # Mon 05:30 < now, within 24h
            ("decision-feeder", True),  # 03:40 today < now, within 24h
            ("market-researcher", False),  # monthly 1st — not near 7/20
        ]:
            ok = (want_agent in got) == want_in
            if not ok:
                failures += 1
            print(f"  [{'PASS' if ok else 'FAIL'}] dispatch fixture {want_agent} "
                  f"emitted={want_agent in got} (want {want_in})")
        # 1st of month 03:00 — monthly fired (02:00 < now, <24h); weekly (Mon) did
        # not unless the 1st is a Monday. 2026-07-01 is a Wednesday, so weekly off.
        got2 = {e.agent_name for e in collect_dispatch_expected(datetime(2026, 7, 1, 3, 0), tp)}
        for want_agent, want_in in [("market-researcher", True), ("chief-of-staff", False)]:
            ok = (want_agent in got2) == want_in
            if not ok:
                failures += 1
            print(f"  [{'PASS' if ok else 'FAIL'}] dispatch fixture(1st) {want_agent} "
                  f"emitted={want_agent in got2} (want {want_in})")
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass

    # (2) live coverage pin — the source-of-truth is only useful if it parses.
    live = _load_autonomous_dispatches()
    if not live:
        print("  [SKIP] live dispatch coverage: iris.py unreadable / no entries")
        return failures
    for entry in live:
        name = entry.get("name", "<unnamed>")
        tk = entry.get("trigger_kwargs")
        coverable = (
            bool(entry.get("agent_name")) and bool(entry.get("name"))
            and isinstance(tk, dict)
            and ("day_of_week" in tk or "day" in tk or "hour" in tk)
        )
        if not coverable:
            failures += 1
        print(f"  [{'PASS' if coverable else 'FAIL'}] live dispatch coverable: {name}")
    return failures


def _selftest() -> int:
    """Hermetic check of the directional morning-brief freshness rule (A-22 fix).
    Sets DAILY_BRIEFING.md mtime to controlled values and asserts fresh≥floor is
    OK while stale is an anomaly. No network/db; temp file only."""
    import os
    import tempfile

    global DAILY_BRIEFING_PATH
    real = DAILY_BRIEFING_PATH
    now = datetime(2026, 7, 5, 5, 0, 0)  # a 05:00 pre-brief run
    exp = Expected("morning_briefing", datetime(2026, 7, 4, 8, 0, 0), "morning_brief")
    # floor = expected_at − 2h grace = 2026-07-04 06:00 (cases below straddle it)
    fd, tmp = tempfile.mkstemp(suffix="_DAILY_BRIEFING.md")
    os.close(fd)
    try:
        DAILY_BRIEFING_PATH = Path(tmp)
        cases = [
            # (label, mtime, expect_ok)
            ("at-expected 08:01", datetime(2026, 7, 4, 8, 1), True),
            ("overnight regen 03:26 (the false-flag case)", datetime(2026, 7, 5, 3, 26), True),
            ("at floor 06:00", datetime(2026, 7, 4, 6, 0), True),
            ("one sec below floor 05:59", datetime(2026, 7, 4, 5, 59), False),
            ("two-days stale (real skip)", datetime(2026, 7, 3, 8, 1), False),
        ]
        failures = 0
        for label, mtime, want in cases:
            ts = mtime.timestamp()
            os.utime(tmp, (ts, ts))
            ok, msg = _check_morning_brief(exp, now)
            status = "PASS" if ok == want else "FAIL"
            if ok != want:
                failures += 1
            print(f"  [{status}] {label}: ok={ok} (want {want}) — {msg}")
        # missing-file case
        DAILY_BRIEFING_PATH = Path(tmp + ".nope")
        ok, msg = _check_morning_brief(exp, now)
        status = "PASS" if not ok else "FAIL"
        if ok:
            failures += 1
        print(f"  [{status}] missing file: ok={ok} (want False) — {msg}")
        failures += _selftest_youtube_coverage()
        failures += _selftest_dispatch_coverage()
        print("SELFTEST OK" if failures == 0 else f"SELFTEST FAILED ({failures})")
        return 1 if failures else 0
    finally:
        DAILY_BRIEFING_PATH = real
        try:
            os.unlink(tmp)
        except OSError:
            pass


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        return _selftest()
    now = datetime.now()
    # Allow callers to skip checking the pre-brief job itself (we ARE the
    # pre-brief — our log hasn't been written yet at run-time).
    skip_self = "--skip-pre-brief-self" in argv

    expected = collect_expected(now)
    auto_expected, not_checked = collect_launchd_expected(now)
    expected += auto_expected
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

    # Surface unparseable plists as anomalies (a plist launchd can't reload is a
    # silently-dead job — exactly the class this check exists for). Interval/manual
    # jobs are listed once as a coverage note, not flagged.
    bad_plists = [n for n, r in not_checked if r == "unparseable plist"]
    coverage_notes = [(n, r) for n, r in not_checked if r != "unparseable plist"]
    for name in bad_plists:
        findings.append(f"{name}: plist unparseable by plutil — launchd cannot (re)load it")

    if not findings:
        print(f"OK ({green}/{green} expected fires verified)")
    else:
        print(f"ANOMALIES ({len(findings)} of {green + len(findings)} expected fires):")
        for i, line in enumerate(findings, 1):
            print(f"  {i}. {line}")
    if coverage_notes:
        print(
            f"NOT coverage-checked ({len(coverage_notes)} interval/manual jobs): "
            + ", ".join(f"{n} ({r})" for n, r in coverage_notes)
        )
    return 0  # surface-only; never block the routine


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
