#!/usr/bin/env python3
"""Report routine prompts whose line-1 run time disagrees with their launchd plist (A-101).

Agents copy the stated time into daily notes and briefs, so a stale "Fri 6:45 AM"
spreads (five cases by 2026-10-26). For every com.iris.*.plist whose
ProgramArguments name a routines/*.prompt and that has StartCalendarInterval,
compare the FIRST clock time on the prompt's first line with the plist's
Hour:Minute. Only the first: later times on line 1 name other jobs ("before the
06:00 brief"), and letting any of them match would hide drift. A prompt with no
time on line 1 is counted, not flagged; a plist naming a missing prompt is listed.

Report only, always exits 0. Usage: prompt_time_check.py [--agents DIR] [--routines DIR]
"""
import argparse
import json
import os
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TIME = re.compile(r"\b(\d{1,2}):(\d{2})(?:\s*([AaPp])\.?[Mm]\b)?")
DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]


def first_time(line):
    t = TIME.search(line)
    if not t:
        return None
    h, m, ap = int(t[1]), int(t[2]), t[3]
    if ap:
        h = h % 12 + (12 if ap.lower() == "p" else 0)
    return h, m  # a nonsense time like 99:99 just never matches


def load_plist(path):
    # plutil, not plistlib: some live plists hold characters plistlib rejects.
    r = subprocess.run(["plutil", "-convert", "json", "-o", "-", str(path)],
                       capture_output=True, text=True)
    return json.loads(r.stdout) if r.returncode == 0 else None


def matches(h, m, entry):
    # launchd treats a missing key as "every", so it matches anything.
    return entry.get("Hour", h) == h and entry.get("Minute", m) == m


def fires(entry):
    day = f"{DAYS[entry['Weekday'] % 7]} " if "Weekday" in entry else (
        f"day {entry['Day']} " if "Day" in entry else "")
    hh, mm = (f"{entry[k]:02d}" if k in entry else "**" for k in ("Hour", "Minute"))
    return f"{day}{hh}:{mm}"


def check(agents, routines):
    rows, checked, no_time, unreadable, missing = [], 0, [], [], []
    for plist in sorted(Path(agents).glob("com.iris.*.plist")):
        d = load_plist(plist)
        if d is None:
            unreadable.append(plist.name)
            continue
        sched = d.get("StartCalendarInterval")
        prompts = re.findall(r"\S+\.prompt\b", " ".join(d.get("ProgramArguments", [])))
        if not sched or not prompts:
            continue
        prompt = Path(routines) / Path(prompts[0]).name
        if not prompt.exists():
            missing.append(f"{plist.name} -> {prompt.name}")
            continue
        entries = sched if isinstance(sched, list) else [sched]
        with open(prompt, encoding="utf-8", errors="replace") as f:
            said = first_time(f.readline())
        checked += 1
        if not said:
            no_time.append(prompt.name)
            continue
        h, m = said
        if not any(matches(h, m, e) for e in entries):
            rows.append(f"MISMATCH {prompt.name}: line 1 says {h:02d}:{m:02d}; "
                        f"{plist.name} fires {', '.join(fires(e) for e in entries)}")
    return rows, checked, no_time, unreadable, missing


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--agents", default=os.path.expanduser("~/Library/LaunchAgents"))
    ap.add_argument("--routines", default=str(REPO / "routines"))
    a = ap.parse_args()
    rows, checked, no_time, unreadable, missing = check(a.agents, a.routines)
    for r in rows:
        print(r)
    for name in unreadable:
        print(f"UNREADABLE {name}: plutil could not parse it")
    for pair in missing:
        print(f"MISSING-PROMPT {pair}")
    print(f"prompt-time check: {checked} checked, {len(rows)} mismatch, "
          f"{len(no_time)} with no time on line 1, {len(unreadable)} unreadable plist, "
          f"{len(missing)} missing prompt")


if __name__ == "__main__":  # mutequiv: tests run the script as a subprocess, never import it
    main()
