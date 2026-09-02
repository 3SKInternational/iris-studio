#!/usr/bin/env python3
"""Pre-brief deploy-freshness check (A-79) — is the live daemon on current code?

Alerts when the on-disk `iris.py` was modified AFTER the running
`com.iris.studio` daemon process started — i.e. a fix is committed to disk but
the daemon was never restarted, so it is still executing the OLD code. Nothing
reloads the process when `iris.py` changes, and this exact gap ran the Telegram
daemon on Jul-29 code for 20 days while a fix for that day's symptom sat
deployed-to-disk-but-undeployed (2026-08-30 nightly-eng incident — the 4th
"built but not live" instance).

Deterministic, read-only. Prints exactly one verdict line to stdout:
  OK      — daemon start-time >= iris.py mtime (running current code)
  DRIFT   — iris.py mtime > daemon start-time (running STALE code) → pages Steve
  UNKNOWN — daemon not running / start-time unreadable (reported, never a silent OK)

Consumed by routines/pre-brief.prompt (folded into the A-22 fire-diff pass). On
DRIFT it ALSO pings Steve's Telegram directly via notify.sh, so the alert does
not depend on the LLM pass remembering to escalate. Never blocks the routine
(exit 0 always), matching prebrief_fire_check.py.

ponytail: scope is iris.py only — the file the incident named and the one that
gets edited+committed. A daemon also imports local_inference.py / scheduled_jobs.py
etc.; if one of those ever ships stale-but-uncaught, widen `iris_mtime` to the
newest mtime among the daemon's source files. Not built now (YAGNI — no such
incident yet).
"""

from __future__ import annotations

import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
IRIS_PY = SCRIPT_DIR.parent / "iris.py"
NOTIFY = SCRIPT_DIR / "notify.sh"
DAEMON_LABEL = "com.iris.studio"

# A real deploy restarts the daemon within seconds of the edit, so its start-time
# lands after the mtime. This grace only absorbs edit-then-immediate-restart clock
# jitter; a genuine stale deploy is hours-to-days late, far outside it.
GRACE = timedelta(minutes=2)


def daemon_pid(label: str = DAEMON_LABEL) -> int | None:
    """PID of the running daemon from `launchctl list <label>`, or None if it is
    not running / unreadable. `launchctl list <label>` prints a plist-ish block
    with a `"PID" = N;` line only while the job has a live process."""
    try:
        out = subprocess.run(
            ["launchctl", "list", label],
            capture_output=True, text=True, timeout=10,  # mutequiv: 10 vs 11s timeout — no observable behavior diff
        )
        if out.returncode != 0:
            return None
        m = re.search(r'"PID"\s*=\s*(\d+)', out.stdout)
        return int(m.group(1)) if m else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def process_start(pid: int) -> datetime | None:
    """Wall-clock start time of a process via `ps -o lstart=`, or None.

    lstart looks like `Wed Sep  2 02:03:56 2026` (day is space-padded → collapse
    runs of whitespace before parsing). %a/%b parse English abbreviations, which
    is what ps emits in the C locale launchd/cron run under; Python's strptime is
    likewise in the C locale here (setlocale is never called)."""
    try:
        out = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True, text=True, timeout=10,  # mutequiv: 10 vs 11s timeout — no observable behavior diff
        )
        # A dead PID / empty output makes strptime("") raise ValueError, which the
        # except below turns into None — so no explicit empty-guard is needed.
        return datetime.strptime(" ".join(out.stdout.split()), "%a %b %d %H:%M:%S %Y")
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def iris_mtime(path: Path = IRIS_PY) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return None


def evaluate(
    mtime: datetime | None,
    start: datetime | None,
    pid: int | None,
    grace: timedelta = GRACE,
) -> tuple[str, str]:
    """Pure verdict from the three collected facts. Returns (verdict, message).

    UNKNOWN (not a silent OK) whenever a fact is missing — honest about what it
    could not check, per this module family's no-silent-confidence contract."""
    if pid is None:
        return "UNKNOWN", (
            f"daemon {DAEMON_LABEL} is not running (no PID from launchctl) — "
            f"cannot check deploy freshness; a down Telegram daemon is itself "
            f"worth a look"
        )
    if start is None:
        return "UNKNOWN", (
            f"daemon {DAEMON_LABEL} (pid {pid}) is running but its start-time is "
            f"unreadable via ps — cannot check deploy freshness"
        )
    if mtime is None:
        return "UNKNOWN", f"iris.py mtime unreadable at {IRIS_PY}"
    if mtime > start + grace:
        late = mtime - start
        return "DRIFT", (
            f"iris.py was modified {mtime:%Y-%m-%d %H:%M} — {_fmt_delta(late)} "
            f"AFTER the daemon started ({start:%Y-%m-%d %H:%M}). The running "
            f"process (pid {pid}) predates the edit, so it is executing STALE "
            f"code. Redeploy: launchctl kickstart -k gui/$(id -u)/{DAEMON_LABEL}"
        )
    return "OK", (
        f"daemon started {start:%Y-%m-%d %H:%M} >= iris.py mtime "
        f"{mtime:%Y-%m-%d %H:%M} — running current code"
    )


def _fmt_delta(d: timedelta) -> str:
    secs = int(d.total_seconds())
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {mins}m"
    return f"{mins}m"


def _notify(msg: str) -> None:
    if not NOTIFY.exists():
        return
    try:
        subprocess.run([str(NOTIFY), msg], check=False, timeout=30)  # mutequiv: 30 vs 31s timeout — no observable behavior diff
    except (OSError, subprocess.SubprocessError):
        pass


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        # Gate coverage lives in tests/test_deploy_freshness.py (precheck globs
        # tests/test_*.py, never an in-script --selftest). This is a human smoke
        # switch only.
        pid = daemon_pid()
        print(f"selftest: pid={pid} start={process_start(pid) if pid else None} "
              f"mtime={iris_mtime()}")
        return 0

    pid = daemon_pid()
    start = process_start(pid) if pid is not None else None
    verdict, msg = evaluate(iris_mtime(), start, pid)
    print(f"{verdict} — {msg}")
    if verdict == "DRIFT":
        _notify(f"🟠 iris.py deploy drift: {msg}")
    return 0  # surface-only; never block the pre-brief


if __name__ == "__main__":  # mutequiv: entrypoint guard, not unit-testable
    sys.exit(main(sys.argv[1:]))  # mutequiv: argv slice under the __main__ entrypoint, not unit-testable
