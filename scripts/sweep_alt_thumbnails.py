#!/usr/bin/env python3
"""Sweep every uploaded video and Telegram Steve its ALTERNATE thumbnail(s) once it is LIVE.

Sibling of sweep_comments.py — same go-live-aware, idempotent, hourly pattern.
For each upload receipt (`Production_Kits/Video_NN_youtube_upload.json` → video_id):
  * already stamped `alt_thumbs_sent`  → SKIP (idempotent, cheap status check)
  * public + not yet sent + has alts   → notify.sh --photo each B/reserve thumb, stamp
  * still private / scheduled          → NO-OP (a later pass sends at go-live)
  * no alternate thumbnails on disk     → SKIP (benign)

Why: the PRIMARY thumbnail (`Thumbnail_A_FINAL`) is already the live one. The B /
reserve variants are the A/B challengers — Steve uploads them via YouTube's
"Test & Compare". Videos publish on a SCHEDULE, so we can't push the alternates
when publish_video.py *schedules* the release — only once YouTube flips the video
public. Running this hourly catches each within ~1h of go-live, then skips forever.

"Public" is read from the LIVE API status (videos().list part=status), not the
clock — so a manual publish or a delayed rollout is handled correctly.

Quota: videos.list (status) ≈ 1 unit/video; the Telegram send costs no quota.
Skipping already-sent videos keeps a full sweep cheap.

Usage:
  python3 scripts/sweep_alt_thumbnails.py --dry-run   # report state, send nothing
  python3 scripts/sweep_alt_thumbnails.py             # send where public + not sent
  python3 scripts/sweep_alt_thumbnails.py --force     # resend even if already stamped
  python3 scripts/sweep_alt_thumbnails.py --only Video_08
  python3 scripts/sweep_alt_thumbnails.py --selftest  # offline self-check, no network
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from upload_video import (  # noqa: E402
    _is_transient_api_error,
    die,
    iter_upload_receipts,
    normalize_id,
    vault,
    write_receipt,
)

NOTIFY_SH = REPO / "scripts" / "notify.sh"
IMAGE_EXTS = {".jpg", ".jpeg", ".png"}


def find_alt_thumbnails(vlt: Path, label: str) -> list[Path]:
    """Return the alternate (B / reserve) thumbnail image files for a video, sorted.

    Matches `_Thumbnail_B*` image files and keeps only image files — so the
    `_B_FINAL.jpg` and any `_B_reserve_*.jpg` are included, while sidecar briefs
    (`_B_regen_brief.md`) are filtered out by extension. The PRIMARY `Thumbnail_A_*`
    is the live thumbnail and is intentionally excluded.

    Organized layout (2026-07-08): alts live under `Thumbnails/<label>/working/`;
    the legacy flat `Thumbnails/<label>_Thumbnail_B*` is kept as a fallback."""
    tdir = vlt / "Thumbnails"
    if not tdir.is_dir():
        return []
    found: dict[str, Path] = {}
    for pat in (f"{label}/working/{label}_Thumbnail_B*",
                f"{label}_Thumbnail_B*"):
        for p in tdir.glob(pat):
            if p.is_file() and p.suffix.lower() in IMAGE_EXTS:
                found.setdefault(p.name, p)  # first location (working/) wins
    return sorted(found.values())


def _send_photo(path: Path, caption: str) -> bool:
    """Push one image to Steve's Telegram via notify.sh --photo. Returns True on
    delivery (notify.sh exits 0), False otherwise. Best-effort: never raises."""
    if not NOTIFY_SH.is_file():
        return False
    try:
        r = subprocess.run(
            [str(NOTIFY_SH), "--photo", str(path), caption],
            timeout=90, check=False, capture_output=True,
        )
        return r.returncode == 0
    except Exception:  # noqa: BLE001 — a send blip is not fatal; retry next sweep.
        return False


def _live_privacy(youtube, video_id: str) -> str | None:
    """Live privacyStatus ('public'|'unlisted'|'private') or None if the id resolves
    to nothing on the channel. Mirrors post_comment._live_privacy (kept local so this
    sweep has no dependency on the comment poster)."""
    resp = youtube.videos().list(part="status", id=video_id).execute()
    items = resp.get("items") or []
    if not items:
        return None
    return items[0].get("status", {}).get("privacyStatus")


def send_alt_thumbnails(youtube, vlt: Path, label: str, *,
                        force: bool = False, dry_run: bool = False,
                        do_notify: bool = True, sender=_send_photo) -> dict:
    """Send a video's alternate thumbnails once it is public, go-live-aware + idempotent.

    Returns {label, status, detail} where status is one of:
      sent | would_send | already_sent | not_public | no_alts | no_video_id |
      not_found | transient | error
    Only `error` drives a non-zero scheduled exit; `not_public` is the normal
    pre-go-live no-op and `transient` is a retry-later blip. Per-video best-effort.
    """
    from googleapiclient.errors import HttpError  # local import: matches sibling sweeps

    rp = vlt / "Production_Kits" / f"{label}_youtube_upload.json"
    if not rp.is_file():
        return {"label": label, "status": "no_video_id", "detail": f"no receipt {rp.name}"}
    try:
        receipt = json.loads(rp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return {"label": label, "status": "error", "detail": f"bad receipt: {exc}"}

    video_id = receipt.get("video_id")
    if not video_id:
        return {"label": label, "status": "no_video_id", "detail": "receipt has no video_id"}

    if receipt.get("alt_thumbs_sent") and not force:
        return {"label": label, "status": "already_sent", "detail": receipt["alt_thumbs_sent"]}

    alts = find_alt_thumbnails(vlt, label)
    if not alts:
        return {"label": label, "status": "no_alts", "detail": "no B/reserve thumbnails on disk"}

    # Go-live gate: never push alternates for a video the public can't see yet —
    # there is nothing to A/B test until it is live.
    try:
        privacy = _live_privacy(youtube, video_id)
    except HttpError as exc:
        kind = "transient" if _is_transient_api_error(exc) else "error"
        return {"label": label, "status": kind, "detail": f"status fetch: {exc}"}
    except Exception as exc:  # noqa: BLE001 — network blip etc.
        kind = "transient" if _is_transient_api_error(exc) else "error"
        return {"label": label, "status": kind, "detail": f"status fetch: {exc}"}

    if privacy is None:
        return {"label": label, "status": "not_found", "detail": f"{video_id} not on channel"}
    if privacy != "public":
        return {"label": label, "status": "not_public",
                "detail": f"privacy={privacy} — will send at go-live"}

    names = ", ".join(p.name for p in alts)
    if dry_run:
        return {"label": label, "status": "would_send",
                "detail": f"{video_id}: would send {len(alts)} alt(s): {names}"}

    title = (receipt.get("title") or "").strip()
    title_bit = f" ({title})" if title else ""
    ok_all = True
    for p in alts:
        caption = (f"🅱️ {label}{title_bit} alternate thumbnail: {p.name}. "
                   f"Upload via YouTube \"Test & Compare\" to A/B test the live thumbnail. "
                   f"https://youtu.be/{video_id}")
        if not sender(p, caption):
            ok_all = False

    # ponytail: stamp only on a fully-clean send; a partial failure leaves the
    # receipt unstamped so the next hourly pass retries — re-sending the ones that
    # already went (a double-send is acceptable per spec, and Telegram fails rarely).
    if not ok_all:
        return {"label": label, "status": "transient",
                "detail": f"{video_id}: a photo send failed — retry next sweep"}

    out = dict(receipt)
    out.update({
        "video": label,
        "video_id": video_id,
        "alt_thumbs_sent": datetime.now(timezone.utc).isoformat(),
        "alt_thumbs_files": [p.name for p in alts],
    })
    write_receipt(rp, out)
    return {"label": label, "status": "sent", "detail": f"{len(alts)} alt(s): {names}"}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Telegram Steve each video's alternate thumbnails once it is public."
    )
    p.add_argument("--only", help="Limit to one video, e.g. Video_08 or 08.")
    p.add_argument("--force", action="store_true",
                   help="Resend even if the receipt already has alt_thumbs_sent.")
    p.add_argument("--dry-run", action="store_true",
                   help="Report what would send (still reads live status); no sends/writes.")
    p.add_argument("--token", help="Override path to youtube_token.json.")
    p.add_argument("--selftest", action="store_true",
                   help="Run the offline self-check (no network) and exit.")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    if args.selftest:
        _selftest()
        return

    from youtube_client import (  # noqa: E402 — deferred so --selftest needs no creds
        YouTubeAuthError, build_data_service, load_credentials,
    )
    vlt = vault()
    try:
        creds = load_credentials(args.token)
    except YouTubeAuthError as exc:
        die(str(exc), code=2)
    youtube = build_data_service(creds)

    mode = "DRY RUN — read-only" if args.dry_run else ("FORCE" if args.force else "live")
    print(f">>> alt-thumbnail sweep ({mode})…")

    only_label = normalize_id(args.only)[0] if args.only else None
    summary = {"checked": 0, "sent": 0, "already_sent": 0, "not_public": 0,
               "no_alts": 0, "would_send": 0, "not_found": 0, "transient": 0, "errors": 0}

    for label, _video_id, _rp, _data in iter_upload_receipts(vlt):
        if only_label and label != only_label:
            continue
        summary["checked"] += 1
        res = send_alt_thumbnails(youtube, vlt, label, force=args.force, dry_run=args.dry_run)
        status = res["status"]
        if status == "sent":
            summary["sent"] += 1
            print(f"  ✅ {label}: sent → {res['detail']}")
        elif status == "already_sent":
            summary["already_sent"] += 1
            print(f"  ✓ {label}: already sent — skip")
        elif status == "not_public":
            summary["not_public"] += 1
            print(f"  ⏳ {label}: {res['detail']}")
        elif status == "would_send":
            summary["would_send"] += 1
            print(f"  • {label}: {res['detail']}")
        elif status in ("no_alts", "no_video_id"):
            summary["no_alts"] += 1
            print(f"  • {label}: {res['detail']}")
        elif status == "not_found":
            summary["not_found"] += 1
            print(f"  ⚠ {label}: not_found — {res['detail']}")
        elif status == "transient":
            summary["transient"] += 1
            print(f"  ⚠ {label}: transient — {res['detail']}", file=sys.stderr)
        else:  # error — a genuine API/4xx/IO fault
            summary["errors"] += 1
            print(f"  🔴 {label}: {status} — {res['detail']}", file=sys.stderr)

    print(
        f"\nsummary: {summary['checked']} checked · {summary['sent']} sent · "
        f"{summary['already_sent']} already · {summary['not_public']} pre-go-live · "
        f"{summary['would_send']} would-send · {summary['no_alts']} no-alts · "
        f"{summary['not_found']} not-found · {summary['transient']} transient · "
        f"{summary['errors']} errors"
    )
    # Non-zero only on a real error (the launchd wrapper turns that into a Telegram
    # alert + retry marker). pre-go-live / no-alts / transient are expected.
    raise SystemExit(1 if summary["errors"] else 0)


# --- self-test -------------------------------------------------------------

class _FakeYouTube:
    """Minimal stand-in for the Data API: videos().list(...).execute() → status."""
    def __init__(self, privacy: str | None):
        self._p = privacy

    def videos(self):
        return self

    def list(self, **_kw):
        return self

    def execute(self):
        if self._p is None:
            return {"items": []}
        return {"items": [{"status": {"privacyStatus": self._p}}]}


def _selftest() -> None:
    """Mock a receipt + a public status → assert one send is attempted and the
    marker is set, then a second run is a no-op. Also covers the pre-go-live gate."""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        vlt = Path(td)
        (vlt / "Production_Kits").mkdir()
        (vlt / "Thumbnails").mkdir()
        label = "Video_99"
        rp = vlt / "Production_Kits" / f"{label}_youtube_upload.json"
        rp.write_text(json.dumps({"video": label, "video_id": "TESTID123",
                                  "title": "Self Test"}), encoding="utf-8")
        # Two B alternates + a sidecar brief (must be ignored) + the primary A (excluded).
        # Organized layout: alts live under Thumbnails/<label>/working/.
        work = vlt / "Thumbnails" / label / "working"
        work.mkdir(parents=True)
        (work / f"{label}_Thumbnail_B_FINAL.jpg").write_bytes(b"jpg")
        (work / f"{label}_Thumbnail_B_reserve_X.jpg").write_bytes(b"jpg")
        (work / f"{label}_Thumbnail_B_regen_brief.md").write_text("brief")
        (work / f"{label}_Thumbnail_A_FINAL.jpg").write_bytes(b"jpg")

        assert [p.name for p in find_alt_thumbnails(vlt, label)] == [
            f"{label}_Thumbnail_B_FINAL.jpg", f"{label}_Thumbnail_B_reserve_X.jpg"
        ], "find_alt_thumbnails must pick both B images, skip the .md and the A primary"

        calls: list[str] = []
        fake_send = lambda p, c: (calls.append(p.name), True)[1]

        # 1) Not-public → no send, no marker.
        r = send_alt_thumbnails(_FakeYouTube("private"), vlt, label, sender=fake_send)
        assert r["status"] == "not_public", r
        assert calls == [], "must not send before go-live"
        assert "alt_thumbs_sent" not in json.loads(rp.read_text())

        # 2) Public → sends both, stamps the marker.
        r = send_alt_thumbnails(_FakeYouTube("public"), vlt, label, sender=fake_send)
        assert r["status"] == "sent", r
        assert len(calls) == 2, f"expected 2 sends, got {calls}"
        rec = json.loads(rp.read_text())
        assert rec.get("alt_thumbs_sent"), "marker must be stamped"
        assert rec.get("alt_thumbs_files") == [
            f"{label}_Thumbnail_B_FINAL.jpg", f"{label}_Thumbnail_B_reserve_X.jpg"
        ]

        # 3) Second run → idempotent no-op, no further sends.
        r = send_alt_thumbnails(_FakeYouTube("public"), vlt, label, sender=fake_send)
        assert r["status"] == "already_sent", r
        assert len(calls) == 2, "second run must not re-send"

        # 4) --force → re-sends despite the marker.
        r = send_alt_thumbnails(_FakeYouTube("public"), vlt, label, sender=fake_send, force=True)
        assert r["status"] == "sent", r
        assert len(calls) == 4, "force must re-send both"

        # 5) A partial send failure leaves the marker unset so the next pass retries.
        rp.write_text(json.dumps({"video": label, "video_id": "TESTID123"}), encoding="utf-8")
        flaky = lambda p, c: p.name.endswith("B_FINAL.jpg")  # one succeeds, one fails
        r = send_alt_thumbnails(_FakeYouTube("public"), vlt, label, sender=flaky)
        assert r["status"] == "transient", r
        assert "alt_thumbs_sent" not in json.loads(rp.read_text()), "no marker on partial failure"

    print("✅ sweep_alt_thumbnails self-test passed")


if __name__ == "__main__":
    main()
