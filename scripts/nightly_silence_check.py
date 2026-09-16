#!/usr/bin/env python3
"""Pre-brief nightly silent-run tripwire (A-80).

A launchd routine can fire, run a Claude session, and leave NO durable record —
no commit, no bridge entry, no daily-note line. A-22 (prebrief_fire_check) treats
a launchd-log mtime as proof-of-fire, so it reads such a night as clean. The
nightly-eng (the auto-build worker) fired and recorded nothing for eight straight
nights (2026-09-07..14) and nothing caught it — the queue it drains stayed
correctly ranked while throughput was zero. This pass catches "fired but shipped
nothing" for that one job.

Signal, in order of certainty:
  1. a git commit in iris_studio dated at/after the nightly slot (commits are
     ground truth, never narration), OR
  2. a bridge-file HEADER for the slot date whose text names "nightly-eng", OR
  3. a daily-note CC-log bullet whose AUTHOR-PREFIX names "nightly-eng".
If the slot fired (an in-window `.activity` Claude session exists) and none of
those three is present, emit SILENT (session did work, recorded nothing) or
ABORT (few tool calls, no write — died early).

Author positions ONLY, never body prose: the bare token "nightly" collides with
"Nightly book-update", and the automation-scan/gardener routinely narrate
"nightly-eng builder silent" in their line BODIES — matching those would mask the
exact silence this exists to find. A CC-log bullet's author-prefix (the text
right after the em dash, up to ':' / arrow / '(') and a markdown header are the
only positions the nightly-eng authors about ITSELF.

Time is NOT used to bind a record to the session: daily-note "HH:MM —" prefixes
are often prompt literals (expense fires 00:00 but writes "05:00 —"), so a
timestamp can't tie a line to a run. Job identity + git do.

The slot itself is read from the nightly plist at run time (reusing
prebrief_fire_check's plutil loader + last-fire math) — never a hand-typed
schedule (a hand table drifts: Pattern_fire_expectations_must_derive_from_schedule).
collect_launchd_expected can't supply it — the nightly is in that function's
_AUTODERIVE_SKIP set, so we read its plist directly.

Deterministic, read-only. Prints `OK` when the nightly left a record (or did not
fire), else one SILENT/ABORT line for pre-brief Pass 12 to fold into the A-22
anomaly block. Exit 0 always — a monitor never blocks the routine.

ponytail: scoped to the nightly auto-build worker, the one job the incident hit
and the fleet's most important. A general fleet-wide version needs per-job
record-location knowledge (gardener writes a section, expense a literal prefix,
tripwire Bash-appends) — deferred until a second job silently fails (YAGNI).
Git-since-slot can be fooled by an unrelated commit in the same window; at 03:00
only the nightly commits, and checks 2/3 are the primary. Widen if that changes.

Usage: nightly_silence_check.py   (no args; paths are fixed to this deployment)
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

VAULT = Path("/Users/steve/Documents/3SK/outputs")
ACTIVITY_DIR = VAULT / "_Iris_Memory" / "Daily" / ".activity"
DAILY_DIR = VAULT / "_Iris_Memory" / "Daily"
BRIDGE = VAULT / "_Iris_Memory" / "Sessions" / "CLAUDE_CODE_HANDOFF.md"
REPO = "/Volumes/AI_Workspace/iris_studio"
NIGHTLY_PLIST = Path.home() / "Library" / "LaunchAgents" / "com.iris.claude-code-nightly.plist"
# The nightly's distinctive self-label. "nightly" alone collides with "Nightly
# book-update"; "nightlyeng" (normalized) is unambiguous to this job.
RECORD_TOKEN = "nightlyeng"

# launchd fires on time or late (a sleeping Mac wakes late); it does not fire
# early. Bind a session whose start sits in [slot - BEFORE, slot + AFTER]. The
# neighbouring slots (book-update 02:30, automation-scan 03:30) are >=30 min off
# and nearest-binding separates them, so 45 min of lateness is safe.
BIND_BEFORE = timedelta(minutes=10)  # mutequiv: window knob — nearest-slot binding separates neighbours regardless of the exact minute
BIND_AFTER = timedelta(minutes=45)  # mutequiv: window knob — nearest-slot binding separates neighbours regardless of the exact minute

# A session with <= this many tool calls and no productive write "died early"
# (ABORT) rather than "ran and stayed silent" (SILENT). Cosmetic label only —
# both are flagged; only the wording the human/retry reads differs.
ABORT_MAX = 8  # mutequiv: label threshold, not a gate — SILENT vs ABORT wording, both flag

# A commit counts as the nightly's record only if it lands in [slot, slot+WINDOW].
# Without an upper bound, `git log --since=slot` on a past slot sweeps in every
# LATER commit and reports a long-dead night as healthy. 6h covers the 03:00 fire
# plus its morning retry replays (retry runner is every 30 min) and still ends
# well before the next nightly, so one night's commits can't vouch for another's.
RECORD_WINDOW = timedelta(hours=6)  # mutequiv: knob — any span shorter than the ~21h to the next nightly works; 6h just also covers morning retries

PRODUCTIVE = {"Write", "Edit", "MultiEdit", "NotebookEdit", "Agent"}
CC_ROOTS = (str(VAULT), REPO)

_HDR = re.compile(r"^#{1,6}\s")


def _norm(s: str) -> str:
    """Lowercase, keep [a-z0-9] only — so 'nightly-eng', 'Nightly Eng' and
    'nightly_eng' all collapse to 'nightlyeng'."""
    return re.sub(r"[^a-z0-9]", "", s.lower())


@dataclass
class Session:
    sid: str
    start: datetime
    end: datetime
    cwd: str
    n: int
    has_productive: bool
    first: str


def parse_sessions(text: str) -> list[Session]:
    """Group one .activity ledger's JSONL lines by session id. A malformed line
    is skipped, not fatal (the ledger is appended live and can have a torn tail)."""
    groups: dict[str, list[dict]] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:  # mutequiv: fast-path — a blank line falls through to json.loads("") → ValueError → skipped anyway
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if "session" not in e or "ts" not in e:
            continue
        groups.setdefault(e["session"], []).append(e)
    out = []
    for sid, evs in groups.items():
        evs.sort(key=lambda x: x["ts"])
        try:
            start = datetime.fromisoformat(evs[0]["ts"])
            end = datetime.fromisoformat(evs[-1]["ts"])
        except ValueError:
            continue  # unparseable timestamps → can't bind it to a slot anyway
        has_prod = any(_is_productive(ev.get("tool", "")) for ev in evs)
        out.append(Session(sid, start, end, evs[0].get("cwd", ""),
                            len(evs), has_prod, evs[0].get("summary", "")))
    return out


def _is_productive(tool: str) -> bool:
    """A tool call that writes/produces, so a session that made one is not a
    read-only abort. Native writers plus Agent (a dispatched subagent that
    writes) plus any MCP write/append/patch/edit tool."""
    if tool in PRODUCTIVE:
        return True
    return tool.startswith("mcp__") and any(
        k in tool for k in ("write", "append", "patch", "edit"))


def bound_session(sessions, slot, roots=CC_ROOTS,
                  before=BIND_BEFORE, after=BIND_AFTER):
    """The CC session nearest the slot whose start is in [slot-before, slot+after],
    or None. cwd must be under a CC root so a stray non-routine session can't
    bind. Nearest-by-start separates adjacent slots."""
    cands = [s for s in sessions
             if any(s.cwd.startswith(r) for r in roots)
             and slot - before <= s.start <= slot + after]
    if not cands:
        return None
    return min(cands, key=lambda s: abs((s.start - slot).total_seconds()))


def cc_log_author_prefixes(daily_text: str) -> list[str]:
    """Normalized author-prefix of every bullet inside the daily note's
    "Claude Code intra-day log" section — the text after the first em dash, up to
    the first ':' / arrow / '('. This is where a routine names ITSELF; body prose
    (where another job may mention 'nightly-eng') is excluded."""
    out = []
    in_sec = False
    for line in daily_text.splitlines():
        if line.startswith("## "):
            in_sec = "Claude Code intra-day log" in line
            continue
        if in_sec and line.lstrip().startswith("- ") and "—" in line:
            prefix = line.split("—", 1)[1]  # mutequiv: maxsplit 1 vs 2 — the author token leads the prefix and is cut at ':'/arrow/'(' before any body em dash, so the normalized token is identical
            for cut in (":", "→", "->", "("):
                i = prefix.find(cut)
                if i != -1:
                    prefix = prefix[:i]
            out.append(_norm(prefix))
    return out


def bridge_headers_for_date(bridge_text: str, date_str: str) -> list[str]:
    """Normalized markdown-header lines that carry the slot date — so a stale
    nightly-eng header from a prior day can't count as today's record."""
    return [_norm(l) for l in bridge_text.splitlines()
            if _HDR.match(l) and date_str in l]


def record_exists(daily_text, bridge_text, date_str, committed) -> bool:
    if committed:
        return True
    if any(RECORD_TOKEN in p for p in cc_log_author_prefixes(daily_text)):
        return True
    return any(RECORD_TOKEN in h for h in bridge_headers_for_date(bridge_text, date_str))


def committed_since(slot: datetime, repo: str = REPO,
                    window: timedelta = RECORD_WINDOW) -> bool:
    """True if iris_studio has a commit dated in [slot, slot+window]. Ground truth
    the nightly can't fake — a commit is the whole point of the auto-build job.
    The upper bound is essential: `--since` alone would let a later night's commit
    vouch for a silent one."""
    try:
        out = subprocess.run(
            ["git", "-C", repo, "log", f"--since={slot:%Y-%m-%dT%H:%M:%S}",
             f"--until={slot + window:%Y-%m-%dT%H:%M:%S}", "--format=%H"],
            capture_output=True, text=True, timeout=10)  # mutequiv: subprocess timeout knob, no exact-value contract
        return out.returncode == 0 and out.stdout.strip() != ""
    except (OSError, subprocess.SubprocessError):
        return False


def evaluate(session, recorded, abort_max=ABORT_MAX) -> tuple[str, str]:
    """(verdict, message). verdict is OK / SILENT / ABORT."""
    if session is None:
        return "OK", ("nightly slot: no in-window Claude session — did not fire, "
                      "or fired as a non-Claude job (A-22's domain, not this one)")
    sid8 = session.sid[:8]  # mutequiv: short-id display length, cosmetic
    if recorded:
        return "OK", f"nightly-eng left a record ({sid8})"
    span = f"{session.start:%H:%M:%S}-{session.end:%H:%M:%S}"
    kind = "ABORT" if session.n <= abort_max and not session.has_productive else "SILENT"
    detail = ("died early — read-only, no write"
              if kind == "ABORT" else "ran but shipped nothing")
    return kind, (
        f"{kind}: nightly-eng fired ({sid8} {span}, {session.n} tool "
        f"calls, {detail}) but left NO record — no commit since the slot, no "
        f"bridge 'nightly-eng' header, no daily-note nightly-eng line. First "
        f"action: {session.first!r}. Read ~/iris_studio/logs/claude-code-nightly.log."
    )


def nightly_slot(now: datetime):
    """Most recent nightly fire <= now within the last 24h, read from the plist
    (never a hand-typed time). None if the plist is missing/unschedulable. Reuses
    prebrief_fire_check's plutil loader (plistlib chokes on the '--' in the
    'claude --print' args) and its last-fire math."""
    try:
        from prebrief_fire_check import _load_plist, _last_fire_daily
        data = _load_plist(NIGHTLY_PLIST)
        sci = data.get("StartCalendarInterval") if data else None
        entry = sci[0] if isinstance(sci, list) else sci
        if not entry or "Hour" not in entry:  # mutequiv: explicit path only — a malformed/no-Hour entry falls through to entry["Hour"] → KeyError/TypeError → the `except Exception` below → None just the same (verified: deleting this guard still returns None)
            return None
        return _last_fire_daily(now, entry["Hour"], entry.get("Minute", 0))
    except Exception:  # noqa: BLE001 — a monitor must never crash the pre-brief
        return None


def main(argv=None) -> int:
    now = datetime.now()
    slot = nightly_slot(now)
    if slot is None:
        print("OK — nightly slot not scheduled in the last 24h (or unreadable)")
        return 0

    sessions: list[Session] = []
    for d in {now.date(), (now - timedelta(days=1)).date()}:
        try:
            sessions += parse_sessions((ACTIVITY_DIR / f"{d:%Y-%m-%d}.jsonl").read_text())
        except OSError:
            pass  # ledger evicted/missing → fewer sessions; git still anchors
    session = bound_session(sessions, slot)

    committed = committed_since(slot)
    date_str = f"{slot:%Y-%m-%d}"
    try:
        daily = (DAILY_DIR / f"{date_str}.md").read_text()
    except OSError:
        daily = ""
    try:
        bridge = BRIDGE.read_text()
    except OSError:
        bridge = ""
    recorded = record_exists(daily, bridge, date_str, committed)

    verdict, msg = evaluate(session, recorded)
    print("OK" if verdict == "OK" else msg)
    return 0


if __name__ == "__main__":  # mutequiv: standard entrypoint guard, not run on import
    sys.exit(main(sys.argv))
