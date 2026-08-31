#!/usr/bin/env python3
"""pulse_recovery.py — the Cowork Nightly Value Pulse miss-recovery net (A-78).

WHY: the nightly value pulse runs on Cowork's APP-OPEN scheduler, so a missed
night leaves the vault with no runway read, no deadline sweep, and no digest —
nothing degrades gracefully, it just goes dark (8/27–8/29 were three app-OPEN
misses). This launchd job (`com.iris.claude-code-pulse-recovery`, ~05:30 ET, after
pre-brief Pass 17's check window) makes a missed night degrade to a *reduced*
pulse instead of nothing. Routed as option (c) of the DQ-19 pulse-delivery brief,
whose recorded rule is "any future miss re-opens the ledger and auto-answers
'build the net'." Cowork keeps the full pulse; this is only a floor.

WHAT IT DOES:
  1. Detect: is there a `<today>_*Nightly_Value_Pulse*.md` digest in Sessions/?
     (same ground-truth test pulse_audit.sh / Pass 17 use). Present → nothing to
     do → EX_TEMPFAIL(75) so run_job.sh stays silent and drops no false success.
  2. On a MISS, produce a vault-only recovery subset:
       - publish-runway read (the one subtraction over the Video_* upload receipts)
       - deadline-passed sweep of the Decision_Queue open rows (report-only)
       - an INBOX 🌙 recovery stub (idempotent, between its own markers)
       - a one-line daily-note marker (so Pass 17 / book Pass 0 see a record)
       - a Telegram ping (only on a NEW recovery — silent on the happy path)
     Then exit 0 (real work done; the plist sets JOB_QUIET_OK=1 so run_job.sh
     doesn't add a second ✅ — the script's own ping is the signal).

HARD CONSTRAINTS (from the pickup spec):
  - Read-mostly. The ONLY writes are the INBOX stub, the daily-note marker, and
    its own log. No Decision_Queue mutation, no DAILY_BRIEFING overwrite, and it
    MUST NOT write anything matching `*Nightly_Value_Pulse*.md` (Pass 17 keys off
    that glob — a forged digest blinds it to real misses forever).

Stdlib-only. Reuses stale_on_you.parse_decision_queue() as the canonical DQ open-row
parser (a second parser would disagree, and every disagreement is a silent bypass).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path

VAULT = Path("/Users/steve/Documents/3SK/outputs")
SESSIONS_DIR = VAULT / "_Iris_Memory" / "Sessions"
KITS_DIR = VAULT / "BRANDS" / "3SK_Finance" / "Production_Kits"
INBOX = VAULT / "INBOX.md"
DAILY_DIR = VAULT / "_Iris_Memory" / "Daily"
SCRIPT_DIR = Path(__file__).resolve().parent
NOTIFY = SCRIPT_DIR / "notify.sh"
LOG = Path("/Users/steve/iris_studio/logs/pulse-recovery.log")

INBOX_BEGIN = "<!-- PULSE_RECOVERY:BEGIN -->"
INBOX_END = "<!-- PULSE_RECOVERY:END -->"
# Anchor the stub right after the C2 stale-on-you block so Steve sees it near the top.
INBOX_ANCHOR = "<!-- C2_STALE_ON_YOU:END -->"
DAILY_MARKER = "<!-- pulse-recovery-ran -->"
DAILY_HEADING = "## 🤖 Claude Code intra-day log"

EXIT_NOOP = 75  # EX_TEMPFAIL — digest present, nothing to do (run_job stays silent)


# --------------------------------------------------------------------------- #
# detection
# --------------------------------------------------------------------------- #
def find_pulse_digest(sessions_dir: Path, today: str) -> Path | None:
    """A pulse that fired leaves `<today>_*Nightly_Value_Pulse.md`. Return it, or None.
    The glob matches pulse_audit.sh:85 / Pass 17 BYTE-FOR-BYTE (no trailing `*`): a
    second observer with a broader glob silently swallows edge files the canonical
    observer still flags (e.g. `..._Nightly_Value_Pulse_Practice_Run.md`), and the
    divergence fails toward a silent no-op — the exact miss this net exists to catch."""
    hits = sorted(sessions_dir.glob(f"{today}_*Nightly_Value_Pulse.md"))
    return hits[0] if hits else None


# --------------------------------------------------------------------------- #
# publish runway — one subtraction over the Video_* upload receipts (A-74 spec)
# --------------------------------------------------------------------------- #
def _parse_utc(ts: str | None) -> dt.datetime | None:
    if not ts or not isinstance(ts, str):
        return None
    try:
        # fromisoformat parses a trailing 'Z' natively on the pinned 3.12 venv.
        d = dt.datetime.fromisoformat(ts.strip())
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def publish_runway(kits_dir: Path, now: dt.datetime) -> dict:
    """Runway = span until the furthest FUTURE scheduled publish; 0.0 if none queued.
    Days-dark = age of the last channel go-live. Glob is pinned to Video_* on
    purpose — a wider `*_youtube_upload.json` would let future Shorts receipts
    silently change what 'runway' means (A-74)."""
    scheduled_ahead = []
    golive_times = []
    n_receipts = 0
    for f in sorted(kits_dir.glob("Video_*_youtube_upload.json")):
        try:
            r = json.loads(f.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if r.get("status") == "deleted_from_channel":
            continue
        n_receipts += 1
        pub = _parse_utc(r.get("publish_at"))
        if pub is not None and pub > now:
            scheduled_ahead.append(pub)
        # last go-live = latest of any channel-write timestamp on the receipt
        for key in ("publish_at", "last_published_at", "uploaded_at"):
            t = _parse_utc(r.get(key))
            if t is not None and t <= now:
                golive_times.append(t)
    runway_days = (max(scheduled_ahead) - now).total_seconds() / 86400 if scheduled_ahead else 0.0  # mutequiv: 86400±1 vanishes under round(...,1) at day scale
    last_golive = max(golive_times) if golive_times else None
    days_dark = (now - last_golive).total_seconds() / 86400 if last_golive else None  # mutequiv: 86400±1 vanishes under round(...,1) at day scale (runway line uses the same constant)
    return {
        "runway_days": round(runway_days, 1),
        "n_scheduled": len(scheduled_ahead),
        "days_dark": round(days_dark, 1) if days_dark is not None else None,
        "n_receipts": n_receipts,
    }


# --------------------------------------------------------------------------- #
# deadline-passed sweep (report-only) over the canonical open DQ rows
# --------------------------------------------------------------------------- #
_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def deadline_due_ids(rows: list[dict], today: str) -> list[str]:
    """Ids of open DQ rows whose leading calendar deadline is today or past.
    Rows with a trigger-based (non-calendar) deadline have no date and are skipped."""
    due = []
    for r in rows:
        m = _DATE_RE.match((r.get("deadline") or "").strip())
        if m and m.group(1) <= today:
            due.append(r.get("id", "?"))
    return due


def open_deadline_due(today: str) -> tuple[list[str], str | None]:
    """(due ids, error). Reuses stale_on_you's canonical parser; on import failure
    returns an error string instead of a divergent home-grown parse."""
    try:
        sys.path.insert(0, str(SCRIPT_DIR))  # mutequiv: index 0 vs 1 resolves the same import ahead of site-packages
        from stale_on_you import parse_decision_queue
    except Exception as e:  # noqa: BLE001 — degrade the sweep, don't kill the net
        return [], f"deadline sweep unavailable ({type(e).__name__})"
    return deadline_due_ids(parse_decision_queue(), today), None


# --------------------------------------------------------------------------- #
# rendering + idempotent writes
# --------------------------------------------------------------------------- #
def build_stub(today: str, runway: dict, due_ids: list[str], sweep_err: str | None) -> str:
    dark = f"{runway['days_dark']} days dark" if runway["days_dark"] is not None else "no go-live on record"
    runway_line = (
        f"**Runway {runway['runway_days']} days** ({runway['n_scheduled']} scheduled ahead) · "
        f"{dark} · {runway['n_receipts']} live receipts."
    )
    if sweep_err:
        due_line = f"**Deadline sweep:** {sweep_err}."
    elif due_ids:
        due_line = f"**Deadline-due rows (today or past):** {', '.join(due_ids)}."
    else:
        due_line = "**Deadline sweep:** no open row is due today or overdue."
    return "\n".join(
        [
            INBOX_BEGIN,
            f"## 🌙 Pulse missed last night — recovery net ran ({today})",
            "",
            "_The Cowork Nightly Value Pulse left no digest for last night; this is the "
            "reduced-pulse floor (`scripts/pulse_recovery.py`, A-78). Cowork's full pulse "
            "still supersedes this when it fires._",
            "",
            f"- {runway_line}",
            f"- {due_line}",
            "",
            INBOX_END,
        ]
    )


def upsert_inbox_block(inbox_path: Path, block: str) -> bool:
    """Replace the recovery block between markers, or insert it after the anchor.
    Idempotent. Returns True if the file content changed."""
    text = inbox_path.read_text(encoding="utf-8")
    if INBOX_BEGIN in text and INBOX_END in text:
        pattern = re.compile(re.escape(INBOX_BEGIN) + r".*?" + re.escape(INBOX_END), re.DOTALL)
        new = pattern.sub(lambda _m: block, text, count=1)  # mutequiv: count 1 vs 2 — only ever one block between the markers
    elif INBOX_ANCHOR in text:
        new = text.replace(INBOX_ANCHOR, INBOX_ANCHOR + "\n" + block, 1)  # mutequiv: count 1 vs 2 — only one anchor exists
    else:
        new = text.rstrip("\n") + "\n\n" + block + "\n"
    if new == text:
        return False
    inbox_path.write_text(new, encoding="utf-8")
    return True


def append_daily_marker(daily_path: Path, when: str, summary: str) -> bool:
    """Append one marked line under the CC intra-day heading. Idempotent by marker.
    Returns True if a new line was added (the ping gate)."""
    line = f"- {when} — {summary} {DAILY_MARKER}"
    text = daily_path.read_text(encoding="utf-8") if daily_path.exists() else ""
    if DAILY_MARKER in text:
        return False
    if DAILY_HEADING in text:
        new = text.replace(DAILY_HEADING, DAILY_HEADING + "\n" + line, 1)  # mutequiv: count 1 vs 2 — only one heading exists
    else:
        new = text.rstrip("\n") + f"\n\n{DAILY_HEADING}\n{line}\n"
    daily_path.write_text(new, encoding="utf-8")
    return True


def _log(msg: str) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(f"{dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} pulse_recovery: {msg}\n")
    except OSError:
        pass


def _notify(msg: str) -> None:
    if not NOTIFY.exists():
        _log(f"notify.sh missing at {NOTIFY}; skipped ping")
        return
    try:
        subprocess.run([str(NOTIFY), msg], check=False, timeout=30)  # mutequiv: 30 vs 31s timeout — no observable behavior diff
    except (OSError, subprocess.SubprocessError) as e:
        _log(f"notify failed: {e}")


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def run_recovery(today: str, now: dt.datetime, *, dry_run: bool) -> int:
    runway = publish_runway(KITS_DIR, now)
    due_ids, sweep_err = open_deadline_due(today)
    block = build_stub(today, runway, due_ids, sweep_err)
    summary = (
        f"pulse missed — recovery net ran; runway {runway['runway_days']}d; "
        f"deadline-due: {', '.join(due_ids) if due_ids else 'none'}"
    )
    if dry_run:
        print(block)
        print(f"\n[dry-run] daily marker would read: {summary}")
        return 0
    inbox_changed = upsert_inbox_block(INBOX, block)
    daily_new = append_daily_marker(DAILY_DIR / f"{today}.md", when=now.astimezone().strftime("%H:%M"), summary=summary)
    _log(f"recovery ran (inbox_changed={inbox_changed}, daily_new={daily_new}): {summary}")
    # Ping only on a NEW recovery for the day, so a same-day re-run stays quiet.
    if daily_new:
        _notify(
            "🌙 Nightly pulse missed last night — recovery net ran.\n"
            f"Runway {runway['runway_days']}d ({runway['n_scheduled']} scheduled ahead).\n"
            f"Deadline-due rows: {', '.join(due_ids) if due_ids else 'none'}.\n"
            "See INBOX 🌙 recovery stub."
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Cowork pulse miss-recovery net (A-78).")
    ap.add_argument("--force-miss", action="store_true",
                    help="run the recovery subset even if today's digest exists (testing).")
    ap.add_argument("--dry-run", action="store_true",
                    help="compute + print the stub; write nothing, send no ping.")
    ap.add_argument("--today", help="override today's date (YYYY-MM-DD); default = local today.")
    a = ap.parse_args(argv)

    now = dt.datetime.now(dt.timezone.utc)
    today = a.today or dt.datetime.now().strftime("%Y-%m-%d")

    if not a.force_miss:
        digest = find_pulse_digest(SESSIONS_DIR, today)
        if digest is not None:
            _log(f"digest present ({digest.name}); nothing to do")
            return EXIT_NOOP

    _log("no pulse digest for today — running recovery" if not a.force_miss else "forced miss — running recovery")
    return run_recovery(today, now, dry_run=a.dry_run)


if __name__ == "__main__":  # mutequiv: entrypoint guard, not unit-testable
    sys.exit(main())
