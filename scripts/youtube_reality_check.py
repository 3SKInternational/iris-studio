#!/usr/bin/env python3
"""youtube_reality_check.py — is the vault honest about YouTube?

Read-only daily diff between what the vault CLAIMS is published (the
`Production_Kits/Video_NN_youtube_upload.json` receipts) and what YouTube
ACTUALLY returns (`videos().list(part=status)`). The A-37 triad linter checks
the launch-spine surfaces against each OTHER (internal consistency); this checks
them against external ground truth — the two are complementary. Catches the
silent-observer failure class: a receipt says a video is public while the video
is actually removed / private / re-scheduled and nothing notices.

Signals (highest severity wins, one per video):
  🔴 GONE             receipt=public but YouTube returns no item (deleted/removed)
  🔴 PRIVACY-MISMATCH receipt privacy != YouTube status.privacyStatus
  🟠 SCHEDULE-DRIFT   receipt publish_at != YouTube status.publishAt
  🟡 TRANSIENT        API/HTTP error — never a false GONE
  🟢 CLEAN            vault claim matches reality (incl. expected pre-publish state)

Exit 1 on any 🔴; exit 0 on warn/transient/clean. NEVER auto-corrects — a
mismatch is Steve's ops call (touches DQ-16 / DQ-24 / DQ-30).

  python scripts/youtube_reality_check.py             # daily hard run (exit 1 + Telegram on 🔴)
  python scripts/youtube_reality_check.py --report-only  # gentle pre-brief pass (always exit 0)
  python scripts/youtube_reality_check.py --verbose      # full report to stdout
  python scripts/youtube_reality_check.py --json         # machine-readable
  python scripts/youtube_reality_check.py --selftest     # fixtures, no network
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Vault paths mirror channel_status.py (read-only; stdlib). Receipts are the
# vault's machine-authoritative "we published this" claim.
VAULT = Path("/Users/steve/Documents/3SK/outputs/BRANDS/3SK_Finance")
KITS_DIR = VAULT / "Production_Kits"
REPORT_PATH = VAULT / "Raw_Assets" / "_youtube_reality_report.md"
REPO_ROOT = Path(__file__).resolve().parent.parent
NOTIFY = REPO_ROOT / "scripts" / "notify.sh"

# signal -> (emoji, is_hard) — hard signals drive exit 1 + the MISMATCH verdict.
SIGNALS = {
    "GONE": ("🔴", True),
    "PRIVACY-MISMATCH": ("🔴", True),
    "SCHEDULE-DRIFT": ("🟠", False),
    "ABSENT-NONPUBLIC": ("🟠", False),
    "TRANSIENT": ("🟡", False),
    "CLEAN": ("🟢", False),
}


def _norm_dt(v):
    """publishAt values compared as datetimes when parseable, else as strings.

    Handles the trailing-Z form YouTube returns ("...T14:00:00Z") vs the
    receipt's "+00:00"; None stays None so a null==absent pair reads equal."""
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return str(v)


def _is_future(iso) -> bool:
    dt = _norm_dt(iso)
    return isinstance(dt, datetime) and dt > datetime.now(timezone.utc)


def _receipt_state(receipt: dict) -> str:
    """What the vault believes about this video NOW: 'public' | 'scheduled' | 'private'.

    A receipt's `privacy` field records the upload-time state and is NOT updated
    when a *scheduled* video auto-publishes — so a receipt with `privacy: private`
    plus a `publish_at` already in the PAST is believed-public now (this is the
    live V03/V05/V06 case: scheduled private, YouTube auto-flipped them public,
    receipt still literally says 'private'). Only a future publish_at is
    'scheduled'; privacy private with no publish_at is a genuine 'private'."""
    pubat = receipt.get("publish_at")
    if _is_future(pubat):
        return "scheduled"
    if receipt.get("privacy") == "public" or pubat:  # explicit public, or a past scheduled time
        return "public"
    return "private"


def classify(receipt: dict, item_status) -> tuple[str, str]:
    """Return (signal, detail) for one receipt vs its YouTube status.

    item_status is the YouTube `status` dict, or None when the id is absent from
    the API response (the GONE case). Pure — the whole test surface lives here.

    The hard 🔴 PRIVACY-MISMATCH is reserved for a video the funnel RELIES on
    being public that is NOT public (a takedown/restriction — real emergency). A
    believed-private video showing public is only a visibility surprise (usually
    a scheduled auto-publish whose receipt lagged) → soft 🟠, no hard page."""
    r_pubat = receipt.get("publish_at")
    state = _receipt_state(receipt)

    if item_status is None:
        if state == "public":
            return "GONE", "receipt=public but YouTube returns no item (deleted/removed)"
        return "ABSENT-NONPUBLIC", f"receipt={state} but YouTube returns no item (deleted before publish?)"

    y_priv = item_status.get("privacyStatus")
    y_pubat = item_status.get("publishAt")

    if y_pubat:  # YouTube has this scheduled (private-until-publishAt)
        if state == "scheduled" and _norm_dt(y_pubat) == _norm_dt(r_pubat):
            return "CLEAN", f"scheduled {y_pubat} (matches receipt)"
        return "SCHEDULE-DRIFT", f"receipt {state} (publish_at={r_pubat!r}) vs YouTube publishAt={y_pubat!r}"

    # Live / unscheduled on YouTube.
    if state == "public":
        if y_priv == "public":
            return "CLEAN", f"privacy=public (matches receipt)"
        return "PRIVACY-MISMATCH", f"receipt believed public but YouTube privacyStatus={y_priv!r}"
    if state == "scheduled":
        return "SCHEDULE-DRIFT", f"receipt still scheduled for {r_pubat} but YouTube shows it live/unscheduled"
    # state == private
    if y_priv == "public":
        return "SCHEDULE-DRIFT", "receipt private but YouTube public (likely auto-published, receipt lagged)"
    return "CLEAN", f"privacy={y_priv} (matches receipt)"


def evaluate(receipts: list[dict], yt_items: dict | None, transient: bool = False) -> tuple[list[dict], str, int]:
    """Diff every id-bearing receipt against the fetched status map.

    yt_items maps present video_id -> status dict (absent ids = GONE). When
    transient is True the fetch failed: report every believed-published video as
    TRANSIENT and never a false GONE. Returns (results, verdict, exit_code)."""
    results = []
    for r in receipts:
        vid = r.get("video_id")
        if not vid:
            continue  # not uploaded yet — nothing to ground-truth
        claim = _receipt_state(r)
        if r.get("publish_at"):
            claim += f" @{r['publish_at']}"
        row = {
            "video": r.get("video", "?"),
            "id": vid,
            "vault_claim": claim,
        }
        if transient:
            row.update(signal="TRANSIENT", youtube_actual="(api error)",
                       detail="YouTube API unreachable — could not verify")
        else:
            status = (yt_items or {}).get(vid)
            signal, detail = classify(r, status)
            if status is None:
                actual = "(no item)"
            else:
                actual = status.get("privacyStatus", "?") + (f" @{status['publishAt']}" if status.get("publishAt") else "")
            row.update(signal=signal, youtube_actual=actual, detail=detail)
        results.append(row)

    hard = [x for x in results if SIGNALS[x["signal"]][1]]
    soft = [x for x in results if x["signal"] in ("SCHEDULE-DRIFT", "ABSENT-NONPUBLIC")]
    if hard:
        verdict, code = "🔴 MISMATCH", 1
    elif transient:
        verdict, code = "TRANSIENT", 0
    elif soft:
        verdict, code = "WARN", 0
    else:
        verdict, code = "CLEAN", 0
    return results, verdict, code


def load_receipts(kits_dir: Path = KITS_DIR) -> list[dict]:
    out = []
    for p in sorted(kits_dir.glob("Video_*_youtube_upload.json")):
        try:
            out.append(json.loads(p.read_text()))
        except (json.JSONDecodeError, OSError):
            continue  # a malformed receipt is not our tripwire's job; channel_status flags it
    return out


def fetch_status_map(ids: list[str]) -> dict:
    """{id: status_dict} for existing ids; absent ids simply don't appear.

    Raises on any API/network fault so the caller can mark TRANSIENT rather than
    mistake an unreachable API for a wall of GONE videos."""
    from youtube_client import load_credentials, build_data_service  # noqa: E402

    creds = load_credentials()
    svc = build_data_service(creds)
    out: dict[str, dict] = {}
    for i in range(0, len(ids), 50):  # videos().list caps at 50 ids/call
        chunk = ids[i:i + 50]
        resp = svc.videos().list(part="status", id=",".join(chunk)).execute()
        for item in resp.get("items", []):
            out[item["id"]] = item.get("status", {})
    return out


def render_report(results: list[dict], verdict: str, when: str) -> str:
    lines = [
        f"# YouTube reality vs vault receipts",
        f"",
        f"- Run: {when}",
        f"- Verdict: **{verdict}**",
        f"- Checked: {len(results)} published receipt(s)",
        f"",
        f"| video | id | vault_claim | youtube_actual | signal | detail |",
        f"| --- | --- | --- | --- | --- | --- |",
    ]
    order = {"GONE": 0, "PRIVACY-MISMATCH": 1, "SCHEDULE-DRIFT": 2, "ABSENT-NONPUBLIC": 3, "TRANSIENT": 4, "CLEAN": 5}
    for x in sorted(results, key=lambda r: order.get(r["signal"], 9)):
        emoji = SIGNALS[x["signal"]][0]
        lines.append(f"| {x['video']} | {x['id']} | {x['vault_claim']} | {x['youtube_actual']} "
                     f"| {emoji} {x['signal']} | {x['detail']} |")
    return "\n".join(lines) + "\n"


def _summary_line(results: list[dict], verdict: str) -> str:
    flagged = [x for x in results if x["signal"] not in ("CLEAN",)]
    published = len(results)
    if verdict.endswith("MISMATCH") or verdict == "WARN":
        heads = "; ".join(f"{x['video']} ({x['id']}) {x['signal']}" for x in flagged[:4])
        return f"youtube_reality_check: {verdict} — {heads}"
    if verdict == "TRANSIENT":
        return f"youtube_reality_check: TRANSIENT ({published} published, API unreachable)"
    return f"youtube_reality_check: CLEAN ({published} published, 0 mismatch)"


def _notify(msg: str) -> None:
    try:
        subprocess.run([str(NOTIFY), msg], timeout=20, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        pass  # best-effort; never block the check on a notify failure


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Diff vault publish receipts against live YouTube state.")
    ap.add_argument("--report-only", action="store_true", help="always exit 0 (gentle pre-brief pass)")
    ap.add_argument("--verbose", action="store_true", help="print the full report to stdout")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    ap.add_argument("--selftest", action="store_true", help="run fixtures (no network) and exit")
    args = ap.parse_args(argv)

    if args.selftest:
        return _selftest()

    receipts = load_receipts()
    ids = [r["video_id"] for r in receipts if r.get("video_id")]
    transient = False
    yt_items: dict = {}
    if ids:
        try:
            yt_items = fetch_status_map(ids)
        except Exception as exc:  # noqa: BLE001 — YouTubeAuthError, HttpError, OSError, etc.
            # Auth failure is not transient: it's a real "token dead" that needs Steve.
            if type(exc).__name__ == "YouTubeAuthError":
                print(f"youtube_reality_check: AUTH ERROR — {exc}", file=sys.stderr)
                _notify(f"⚠️ youtube_reality_check: YouTube token dead — re-run youtube_authorize.py. {exc}")
                return 0 if args.report_only else 2
            transient = True

    results, verdict, code = evaluate(receipts, yt_items, transient=transient)
    when = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    report = render_report(results, verdict, when)
    try:
        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        REPORT_PATH.write_text(report)
    except OSError as exc:
        print(f"youtube_reality_check: WARN could not write report ({exc})", file=sys.stderr)

    if args.json:
        print(json.dumps({"verdict": verdict, "results": results}, indent=2))
    elif args.verbose:
        print(report)
    else:
        print(_summary_line(results, verdict))

    # Page on hard mismatch or schedule drift — but not from the gentle pre-brief
    # pass (it surfaces via the report file into the morning brief instead).
    if not args.report_only and (code == 1 or verdict == "WARN"):
        _notify(_summary_line(results, verdict))

    return 0 if args.report_only else code


def _selftest() -> int:
    pub = {"video": "Video_04", "video_id": "ClJVIUtwsVE", "privacy": "public", "publish_at": None}

    # 1. Clean — live item, receipt public, YouTube public.
    r, v, c = evaluate([pub], {"ClJVIUtwsVE": {"privacyStatus": "public"}})
    assert (r[0]["signal"], v, c) == ("CLEAN", "CLEAN", 0), r

    # 2. GONE — receipt public, id absent from response (the live 2026-07-01 state).
    r, v, c = evaluate([pub], {})
    assert (r[0]["signal"], v, c) == ("GONE", "🔴 MISMATCH", 1), r

    # 3. PRIVACY-MISMATCH — receipt public, YouTube private (unscheduled).
    r, v, c = evaluate([pub], {"ClJVIUtwsVE": {"privacyStatus": "private"}})
    assert (r[0]["signal"], v, c) == ("PRIVACY-MISMATCH", "🔴 MISMATCH", 1), r

    # 4. SCHEDULE-DRIFT — receipt immediate (null), YouTube shows a future publishAt.
    r, v, c = evaluate([pub], {"ClJVIUtwsVE": {"privacyStatus": "private", "publishAt": "2099-01-01T14:00:00Z"}})
    assert (r[0]["signal"], v, c) == ("SCHEDULE-DRIFT", "WARN", 0), r

    # 5. TRANSIENT — API failed; must NOT report a false GONE.
    r, v, c = evaluate([pub], None, transient=True)
    assert (r[0]["signal"], v, c) == ("TRANSIENT", "TRANSIENT", 0), r

    # 6. Pre-publish — receipt private, YouTube agrees private: no signal.
    priv = {"video": "Video_09", "video_id": "zzz", "privacy": "private", "publish_at": None}
    r, v, c = evaluate([priv], {"zzz": {"privacyStatus": "private"}})
    assert (r[0]["signal"], v, c) == ("CLEAN", "CLEAN", 0), r

    # 6b. Scheduled-then-auto-published — receipt literally 'private' with a PAST
    # publish_at, YouTube now public. This is the live V03/V05/V06 case that a
    # naive privacy-string compare false-flagged as 🔴 — must read CLEAN.
    autopub = {"video": "Video_03", "video_id": "a3", "privacy": "private", "publish_at": "2020-01-01T18:00:00+00:00"}
    r, v, c = evaluate([autopub], {"a3": {"privacyStatus": "public"}})
    assert (r[0]["signal"], v, c) == ("CLEAN", "CLEAN", 0), r

    # 6c. Believed-private (no publish_at) unexpectedly public — soft surprise, not a hard page.
    r, v, c = evaluate([priv], {"zzz": {"privacyStatus": "public"}})
    assert (r[0]["signal"], v, c) == ("SCHEDULE-DRIFT", "WARN", 0), r

    # 7. Scheduled-as-expected — receipt + YouTube agree on the same publishAt.
    sched = {"video": "Video_07", "video_id": "s7", "privacy": "public", "publish_at": "2099-06-01T13:00:00+00:00"}
    r, v, c = evaluate([sched], {"s7": {"privacyStatus": "private", "publishAt": "2099-06-01T13:00:00Z"}})
    assert (r[0]["signal"], v, c) == ("CLEAN", "CLEAN", 0), r

    # 8. No video_id yet (pre-upload) — skipped entirely, not counted.
    r, v, c = evaluate([{"video": "Video_10", "privacy": "public"}], {})
    assert r == [] and v == "CLEAN" and c == 0, r

    # 9. Mixed batch — one GONE dominates the verdict + exit even beside a clean.
    r, v, c = evaluate([pub, {"video": "Video_05", "video_id": "ok5", "privacy": "public"}],
                       {"ok5": {"privacyStatus": "public"}})
    assert v == "🔴 MISMATCH" and c == 1 and len(r) == 2, r

    print("selftest ok (11 cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
