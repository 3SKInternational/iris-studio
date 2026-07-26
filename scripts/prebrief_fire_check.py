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

Surface-only for the vault. The ONE thing it writes is its own monitoring
state (~/iris_studio/state/interval_job_runs.tsv — the launchd `runs` snapshot
the interval-liveness check diffs against). Safe to run unconditionally.
Output is consumed by `routines/pre-brief.prompt` Pass 12.
"""

from __future__ import annotations

import json
import os
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
# Populated by collect_launchd_expected: [(name, label, interval_seconds)] for
# every StartInterval job, so check_interval_jobs can give them real coverage.
_INTERVAL_JOBS: list[tuple[str, str, int]] = []
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
    _INTERVAL_JOBS.clear()
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
            if interval:
                try:
                    _INTERVAL_JOBS.append((name, label, int(interval)))
                except (TypeError, ValueError):
                    pass   # a malformed StartInterval must not crash the check
            continue
        log_path = _derive_log_path(data, name)
        entries = sci if isinstance(sci, list) else [sci]
        for entry in entries:
            exp = _expected_from_calendar_entry(now, entry, name, log_path)
            if exp:
                expected.append(exp)
    return expected, not_checked



# === Interval-job liveness (added 2026-07-26) ================================
# The calendar model above cannot express a StartInterval job, so 7 of them —
# including the retry runner, the hourly pipeline sweep and the auth canary —
# were reported as "not coverage-checked" and had NO silent-stop detection at all.
#
# Why launchd's counter and not log-mtime: mtime would actually WORK for 5 of
# these 6 (run_job.sh writes "starting at" unconditionally; auth_canary always
# logs) — but NOT for `retry_runner.sh`, which exits silently on an empty queue
# (line ~48), so a healthy idle retry job is byte-identical to a dead one. The
# counter avoids needing per-wrapper knowledge of who logs what, is uniform
# across every job, and additionally catches "the wrapper was never invoked at
# all" — a case no wrapper-written log can report on.
#
# The authoritative signal is launchd's OWN cumulative counter, `runs`, from
# `launchctl print`. It advances on every fire regardless of what the job logs,
# exits, or no-ops. We snapshot it per job and flag when it fails to advance
# across a window in which it should have fired. Derive from the scheduler, never
# from a proxy — the same rule the 2026-07-22 phantom-alert fix established.
#
# `runs` RESETS TO 0 on reload/reboot (verified: a `launchctl unload/load` put
# caption-sweep back to 0 while its sibling sat at 141). A decrease is therefore
# a re-baseline, never a failure.
INTERVAL_STATE = Path.home() / "iris_studio" / "state" / "interval_job_runs.tsv"
# How many whole intervals must elapse with a frozen counter before we call it
# stopped. 2.5 tolerates one skipped fire plus scheduler jitter; at the daily
# pre-brief cadence the real elapsed gap is ~24h, so this only matters when the
# check is run twice in quick succession.
INTERVAL_MISS_FACTOR = 2.5
# Degraded-cadence guard: a job can advance its counter while firing far below
# schedule, which a freeze-only check reads as green forever. Flag when it
# delivers under DEGRADED_FLOOR of its due fires.
#
# 0.25 is argued from this vault's own logs, not picked: real per-day counts for
# hourly jobs run 19-24 of 24 (pipeline-sweep's worst real day was 19 = 79%,
# pure scheduler jitter — comment-sweep and alt-thumbnail hit 24/24 the same
# day). A 0.75 floor would false-alarm on that 19. 0.25 puts the line at 6/24:
# 3.2x headroom under the worst observed natural dip, while still catching the
# caption-sweep class (1/24 = 4%, the month-long stale-plist bug).
#
# NOTE: a separate minimum-sample gate is NOT needed. In this branch the counter
# advanced, so got >= 1; `got < due * 0.25` already implies `due > 4`. An
# explicit DEGRADED_MIN_DUE was unreachable dead code and is deliberately absent.
# If the floor is ever raised above 0.25 that implication weakens and a real
# minimum-sample gate (with a fixture that exercises it) becomes necessary.
DEGRADED_FLOOR = 0.25


def _boot_time() -> datetime | None:
    """When the machine last booted, or None if unreadable.

    A reboot zeroes every job's `runs`. The `cur < prev` reset detector cannot
    see a reset that lands ABOVE a low prior baseline (e.g. a job reloaded just
    before the last check sits at 0; after a reboot it reads 3, which looks like
    "advanced"). The degraded-cadence maths would then measure those 3 fires
    against a window the counter never lived through and report a HEALTHY job at
    17% of schedule. Verified live against the on-disk caption-sweep tuple.
    """
    try:
        out = subprocess.run(["sysctl", "-n", "kern.boottime"],
                             capture_output=True, text=True, timeout=5)
        m = re.search(r"\bsec\s*=\s*(\d+)", out.stdout)  # \b: `usec` must not shadow `sec`
        return datetime.fromtimestamp(int(m.group(1))) if m else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _launchd_runs(label: str) -> int | None:
    """launchd's cumulative fire count for a job, or None if unreadable."""
    try:
        out = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
            capture_output=True, text=True, timeout=10)
        if out.returncode != 0:
            return None
        m = re.search(r"^\s*runs\s*=\s*(\d+)\s*$", out.stdout, re.MULTILINE)
        return int(m.group(1)) if m else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _read_interval_state() -> dict[str, tuple[int, datetime]]:
    out: dict[str, tuple[int, datetime]] = {}
    try:
        for line in INTERVAL_STATE.read_text(encoding="utf-8").splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            try:
                out[parts[0]] = (int(parts[1]), datetime.fromisoformat(parts[2]))
            except ValueError:
                continue
    except OSError:
        pass
    return out


def _write_interval_state(state: dict[str, tuple[int, datetime]]) -> bool:
    """Persist the snapshot. Returns False if it could NOT be written.

    A failure here silently DISABLES the whole check — every subsequent run
    re-baselines, so a permanently dead job is never flagged. The caller must
    surface that rather than keep printing "liveness-checked": this module's
    contract is to be honest about what it does NOT cover (no silent confidence).
    """
    try:
        INTERVAL_STATE.parent.mkdir(parents=True, exist_ok=True)
        INTERVAL_STATE.write_text(
            "".join(f"{k}\t{v[0]}\t{v[1].isoformat()}\n" for k, v in sorted(state.items())),
            encoding="utf-8")
        return True
    except OSError:
        return False


def check_interval_jobs(now: datetime, jobs: list[tuple[str, str, int]],
                        runs_fn=_launchd_runs, state=None, boot=None
                        ) -> tuple[list[str], dict[str, tuple[int, datetime]]]:
    """Anomaly lines for interval jobs whose launchd `runs` counter has frozen.

    jobs: [(name, label, interval_seconds)]. Returns (anomalies, new_state) —
    pure apart from runs_fn, so the selftest can drive it with a fake counter.
    """
    prior = _read_interval_state() if state is None else state
    if boot is None:
        boot = _boot_time()
    new: dict[str, tuple[int, datetime]] = {}
    anomalies: list[str] = []
    for name, label, interval in jobs:
        cur = runs_fn(label)
        if cur is None:
            new[name] = prior.get(name, (0, now))
            continue
        seen = prior.get(name)
        if seen is None:
            new[name] = (cur, now)            # first sight — baseline, never flag
            continue
        prev_runs, prev_ts = seen
        # A reboot restarts every counter at 0, so any window that predates boot
        # is not a window this counter lived through. Clamp it on BOTH paths:
        # the freeze path would otherwise report a job that booted 30 min ago as
        # "frozen for 17.8h — loaded but not firing", which is strictly more
        # alarming than the degraded false-positive and equally wrong.
        rebooted = boot is not None and boot > prev_ts
        window_start = max(prev_ts, boot) if boot else prev_ts
        if cur > prev_runs:
            # Advancing, but is it advancing ENOUGH? A job firing 1x/day instead
            # of 24x still moves the counter and would read green forever. That
            # is not hypothetical: caption-sweep ran 1x/day at 04:20 for a month
            # while its repo plist said hourly (the installed plist was stale),
            # and pipeline-sweep is currently ~6 fires short over 5.9 days.
            # Only judge over a window long enough to be meaningful, and use a
            # loose 25% floor so sleep/wake deferrals don't cry wolf.
            elapsed = (now - prev_ts).total_seconds()
            due = elapsed / interval
            got = cur - prev_runs
            if not rebooted and got < due * DEGRADED_FLOOR:
                anomalies.append(
                    f"{name}: fired {got}x in {elapsed/3600:.1f}h but a {interval}s "
                    f"interval is ~{due:.0f}x due — running at {got/due*100:.0f}% "
                    f"of its schedule. Not stopped, but degraded (a stale installed "
                    f"plist, sleep/App-Nap, or a wedged run can cause this).")
            new[name] = (cur, now)            # advanced => alive
        elif cur < prev_runs:
            new[name] = (cur, now)            # counter reset (reload/reboot) => re-baseline
        else:
            elapsed = (now - window_start).total_seconds()
            if elapsed >= interval * INTERVAL_MISS_FACTOR:
                missed = int(elapsed // interval)
                anomalies.append(
                    f"{name}: launchd `runs` frozen at {cur} for "
                    f"{elapsed/3600:.1f}h — a {interval}s-interval job should have "
                    f"fired ~{missed}x in that window. The job is loaded but not "
                    f"firing (log mtime cannot show this: an idle no-op writes "
                    f"nothing).")
                # Re-baseline the TIMESTAMP so the next message's elapsed/miss
                # figures stay truthful about the current window. This does NOT
                # de-duplicate: at the daily pre-brief cadence elapsed (~24h)
                # re-crosses every threshold (<=7.5h), so a still-dead job
                # re-alerts each run. That is intended — a permanently dead job
                # going quiet after one ping would be the worse failure.
                new[name] = (cur, now)
            else:
                new[name] = seen              # too early to judge — KEEP the old
                                              # timestamp so the window accumulates
    return anomalies, new

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



def _selftest_interval_liveness() -> int:
    """Pins the interval-job liveness contract (added 2026-07-26).

    The three properties that make this safe to alert on: a frozen counter only
    flags AFTER its window elapses; a counter RESET (reload/reboot sets runs=0)
    is a re-baseline and never a failure; and an inconclusive tick must NOT bump
    the stored timestamp, or the window could never accumulate and the check
    could never fire at all."""
    t0 = datetime(2026, 7, 26, 12, 0, 0)
    JOB = [("sweep", "com.iris.x", 3600)]
    cases = [
        # (label, prior, fake_runs, want_flag, want_baseline_preserved)
        ("first sight -> baseline, never flag", {}, 10, False, None),
        # Advance must be PROPORTIONAL to be "alive": 5 fires in 5h on an hourly
        # job. (Written as 11 before the degraded-cadence guard existed — 1 fire
        # in 5h is 20% of schedule, which the guard now correctly flags.)
        ("counter advanced at schedule -> alive",
         {"sweep": (10, t0 - timedelta(hours=5))}, 15, False, False),
        ("counter RESET (reload/reboot) -> re-baseline, NOT a failure",
         {"sweep": (500, t0 - timedelta(hours=5))}, 0, False, False),
        ("frozen 5h on a 1h job -> FLAG",
         {"sweep": (10, t0 - timedelta(hours=5))}, 10, True, False),
        ("frozen only 1h on a 1h job -> too early, KEEP baseline",
         {"sweep": (10, t0 - timedelta(hours=1))}, 10, False, True),
        # want_keep=True is load-bearing: with None this fixture was VACUOUS —
        # `cur = runs_fn(...) or 0`, dropping the prior tuple, and bumping the ts
        # on the None path all survived it. An intermittently-unreadable
        # launchctl would then reset the accumulation window every run and the
        # check could never fire.
        ("launchctl unreadable -> no flag AND baseline preserved",
         {"sweep": (10, t0 - timedelta(hours=5))}, None, False, True),
    ]
    # Degraded cadence: advancing, but far under schedule.
    cases += [
        ("degraded: 2 fires in 24h on a 1h job -> FLAG",
         {"sweep": (10, t0 - timedelta(hours=24))}, 12, True, False),
        ("healthy: 24 fires in 24h on a 1h job -> no flag",
         {"sweep": (10, t0 - timedelta(hours=24))}, 34, False, False),
        ("1 fire in 2h on a 1h job (50%) -> above the floor, no flag",
         {"sweep": (10, t0 - timedelta(hours=2))}, 11, False, False),
    ]
    # Boot far in the past unless a case overrides it -> the reboot guard is
    # inert for every case that isn't specifically testing it.
    OLD_BOOT = t0 - timedelta(days=30)
    cases = [(c[0], c[1], c[2], c[3], c[4], OLD_BOOT) for c in cases]
    cases += [
        # M5: pins DEGRADED_FLOOR. 12/24 = 50% must NOT flag — this fails if the
        # floor drifts up to 0.75 and passes at 0.25. Without it the one tuned
        # number in the check was the one number no fixture constrained.
        ("degraded floor: 12 fires in 24h (50%) -> no flag",
         {"sweep": (10, t0 - timedelta(hours=24))}, 22, False, False, OLD_BOOT),
        # H3: a reboot inside the window zeroes `runs`; a low prior baseline makes
        # the post-boot count look "advanced". Measuring it against the full
        # window reported a HEALTHY job at 17% of schedule. Live-armed by
        # caption-sweep sitting at 0 after today's reload.
        ("reboot inside window: healthy job must NOT read as degraded",
         {"sweep": (0, t0 - timedelta(hours=18))}, 3, False, False,
         t0 - timedelta(hours=16)),
        ("no reboot: same numbers DO flag as degraded",
         {"sweep": (0, t0 - timedelta(hours=18))}, 3, True, False, OLD_BOOT),
        # H4 mirror pair — the FREEZE path had the identical reboot artifact:
        # a job that booted 30 min ago sits at runs=0 (which is also what a
        # recently-reloaded baseline looks like), and an unclamped window called
        # it "frozen for 17.8h — loaded but not firing".
        ("reboot 30min ago: healthy job at runs=0 must NOT read as frozen",
         {"sweep": (0, t0 - timedelta(hours=18))}, 0, False, True,
         t0 - timedelta(minutes=30)),
        ("no reboot: same runs=0 over 18h DOES flag as frozen",
         {"sweep": (0, t0 - timedelta(hours=18))}, 0, True, False, OLD_BOOT),
    ]
    failures = 0

    # LIVE pin: fixtures inject boot=, so _boot_time()'s own parse is never
    # exercised by them. If sysctl's output shape ever changes (e.g. `usec`
    # ordering), the guard would silently degrade to fail-open with the suite
    # still green. Same live-pin pattern as the youtube-research/dispatch
    # coverage checks in this file.
    if shutil.which("sysctl"):
        b = _boot_time()
        # Recency bound, not just "in the past": an unanchored `sec` parse would
        # yield the usec field (e.g. epoch 747401 -> 1970-01-09), which IS in the
        # past and would pass a naive check while the guard sits inert. The bound
        # is what actually catches a sysctl output-shape change.
        ok = (isinstance(b, datetime)
              and b < datetime.now()
              and b > datetime.now() - timedelta(days=365))
        if not ok:
            failures += 1
        print(f"  [{'PASS' if ok else 'FAIL'}] interval: _boot_time() live-reads "
              f"a past datetime ({b})")

    for label, prior, fake, want_flag, want_keep, boot_at in cases:
        anoms, new_state = check_interval_jobs(
            t0, JOB, runs_fn=lambda _l, v=fake: v, state=dict(prior),
            boot=boot_at)
        ok = bool(anoms) == want_flag
        if ok and want_keep is True:
            ok = new_state["sweep"][1] == prior["sweep"][1]
        elif ok and want_keep is False:
            ok = new_state["sweep"][1] == t0
        if not ok:
            failures += 1
        print(f"  [{'PASS' if ok else 'FAIL'}] interval: {label}")
    return failures


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
        failures += _selftest_interval_liveness()
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
    for name in bad_plists:
        findings.append(f"{name}: plist unparseable by plutil — launchd cannot (re)load it")

    # Interval jobs get real coverage from the launchd `runs`-counter check
    # rather than the calendar model. Anything flagged here is a silent stop.
    interval_anoms, new_state = check_interval_jobs(now, list(_INTERVAL_JOBS))
    findings.extend(interval_anoms)
    state_ok = _write_interval_state(new_state) if _INTERVAL_JOBS else True
    if not state_ok:
        findings.append(
            f"interval liveness: state file unwritable at {INTERVAL_STATE} — "
            f"silent-stop detection for {len(_INTERVAL_JOBS)} interval job(s) is "
            f"INACTIVE (every run re-baselines, so a dead job would never flag)")
    interval_names = {n for n, _, _ in _INTERVAL_JOBS}
    coverage_notes = [(n, r) for n, r in not_checked
                      if r != "unparseable plist" and n not in interval_names]

    if not findings:
        print(f"OK ({green}/{green} expected fires verified)")
    else:
        print(f"ANOMALIES ({len(findings)} of {green + len(findings)} expected fires):")
        for i, line in enumerate(findings, 1):
            print(f"  {i}. {line}")
    if _INTERVAL_JOBS and state_ok:
        print(f"Interval jobs liveness-checked via launchd `runs`: "
              f"{len(_INTERVAL_JOBS)} ({', '.join(sorted(n for n, _, _ in _INTERVAL_JOBS))})")
    if coverage_notes:
        print(
            f"NOT coverage-checked ({len(coverage_notes)} manual/unschedulable): "
            + ", ".join(f"{n} ({r})" for n, r in coverage_notes)
        )
    return 0  # surface-only; never block the routine


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
