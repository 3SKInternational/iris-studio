#!/usr/bin/env python3
"""Standalone idempotent caption upload for an ALREADY-LIVE YouTube video.

Captions attach to a videoId, so the video must already exist on the channel
(uploaded via upload_video.py, which records the id in the receipt). This tool
resolves that id (or --video-id), finds the matching timed `.srt`, and attaches
it to the English caption track. It calls captions.list FIRST so a re-run UPDATES
the existing track in place instead of stacking a duplicate. sync=False: our SRT
is already timed, so YouTube must not re-sync it (which would mangle dollar
figures on a finance channel).

This is the manual / backfill path. The upload (upload_video.py) and publish
(publish_video.py) flows call the same upsert_captions() automatically.

Resolution from a video id (under the 3SK Finance vault; override $SK_VAULT):
  captions : Footage_and_Edits/Video_NN_v2.srt   (--srt to override)
  video id : Production_Kits/Video_NN_youtube_upload.json -> video_id  (--video-id to override)
  receipt  : Production_Kits/Video_NN_youtube_upload.json  (stamped with captions_*)

ONE-TIME consent: the unattended run loads youtube_token.json (a long-lived
refresh token captured once, interactively, by scripts/youtube_authorize.py).
If you have never authorized, run that first — it opens a browser for the studio@
"Allow" consent and writes the token; after that this tool runs headless.

Usage:
  python3 scripts/upload_captions.py Video_02 --dry-run     # validate, no network
  python3 scripts/upload_captions.py Video_02               # attach/refresh captions
  python3 scripts/upload_captions.py Video_01 --srt /abs/Video_01_v2.srt
  python3 scripts/upload_captions.py Video_02 --video-id Fc1NCIrXVlM
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from youtube_client import (  # noqa: E402
    YouTubeAuthError,
    build_data_service,
    load_credentials,
)

# Reuse the uploader's resolution/IO + the shared idempotent caption core so the
# manual path can never drift from the automatic upload/publish path.
from upload_video import (  # noqa: E402
    _resolve_path,
    die,
    normalize_id,
    upsert_captions,
    vault,
    video_processing_status,
    write_receipt,
)


def load_receipt(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        die(f"could not read receipt {path}: {exc}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="3SK YouTube standalone caption upload (idempotent insert/update)."
    )
    p.add_argument("video", help="Video id, e.g. Video_02 or 02.")
    p.add_argument("--video-id", help="YouTube video id (overrides the receipt).")
    p.add_argument("--srt", help="Override the .srt path (vault-relative or absolute).")
    p.add_argument("--lang", default="en", help="Caption language code (default: en).")
    p.add_argument("--name", default="English", help="Caption track name (default: English).")
    p.add_argument("--replace", action="store_true",
                   help="Delete any existing track and re-insert (heals a stuck, "
                        "non-servable track — an in-place update does NOT un-stick it).")
    p.add_argument("--allow-processing", action="store_true",
                   help="Attach even if the video is still processing (NOT recommended — "
                        "a mid-processing track sticks permanently non-servable).")
    p.add_argument("--token", help="Override path to youtube_token.json.")
    p.add_argument("--dry-run", action="store_true",
                   help="Validate inputs + print the plan; touch no network.")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    vid, nn = normalize_id(args.video)
    vlt = vault()

    receipt_path = vlt / "Production_Kits" / f"{vid}_youtube_upload.json"
    receipt = load_receipt(receipt_path)

    # Resolve the YouTube video id: explicit flag > receipt. Never guess.
    video_id = args.video_id or (receipt.get("video_id") if receipt else None)
    if not video_id:
        die(f"no YouTube video id — pass --video-id, or upload first so "
            f"{receipt_path.name} carries video_id. (Captions attach to an "
            "existing video; this tool does not upload the mp4.)")

    srt = _resolve_path(args.srt, vlt, f"Footage_and_Edits/{vid}_v2.srt")
    if not srt.is_file():
        die(f"caption file not found: {srt} (generate it or pass --srt).")

    # --- plan ---
    print(f"video      : {vid}")
    print(f"video_id   : {video_id}  →  https://youtu.be/{video_id}")
    print(f"captions   : {srt}  ({srt.stat().st_size} bytes)")
    print(f"track      : {args.name} [{args.lang}]  (sync=false — keep our timing)")
    print(f"receipt    : {receipt_path}")
    mode = ("captions.list → DELETE existing + re-insert (heal stuck track)" if args.replace
            else "captions.list → update-in-place if our track exists, else insert")
    print(f"mode       : {mode}")

    if args.dry_run:
        print("\n--- DRY RUN (no network, nothing changed). Drop --dry-run to upload. ---")
        return

    try:
        creds = load_credentials(args.token)
    except YouTubeAuthError as exc:
        die(str(exc), code=2)
    youtube = build_data_service(creds)

    from googleapiclient.errors import HttpError

    # Gate on processing: attaching a track while the video is still processing
    # creates a permanently non-servable track (the V9 bug). Confirm it's done
    # unless the operator explicitly overrides. We ALWAYS read the real status (even
    # under --allow-processing) so the receipt trust stamp below is honest.
    try:
        pstatus = video_processing_status(youtube, video_id)
    except HttpError as exc:
        die(f"could not read processing status for {video_id} "
            f"(HTTP {getattr(getattr(exc, 'resp', None), 'status', '?')}): {exc}", code=3)
    processed = pstatus == "succeeded"
    if not processed and not args.allow_processing:
        die(f"video {video_id} is not ready ({pstatus}); attaching captions now "
            "would create a permanently non-servable track. Wait until processing "
            "finishes, or pass --allow-processing to override.", code=4)

    print(f"\n>>> attaching captions to {video_id}…")
    try:
        action = upsert_captions(youtube, video_id, srt, lang=args.lang, name=args.name,
                                 replace=args.replace)
    except HttpError as exc:
        status = getattr(getattr(exc, "resp", None), "status", "?")
        hint = ""
        if status == 403:
            hint = (" — likely a quota (captions.insert ≈ 400 units) or a missing "
                    "youtube.force-ssl scope; re-run youtube_authorize.py --force if "
                    "the scope was widened.")
        die(f"caption upload failed (HTTP {status}){hint}: {exc}", code=3)
    verb = {"insert": "inserted", "update": "updated",
            "replace": "re-inserted (healed stuck track)"}.get(action, action)
    print(f"  ✅ captions {verb}: {srt.name}  →  https://youtu.be/{video_id}")

    # --- stamp the receipt (keep prior fields). ---
    # captions_post_processing is the TRUST stamp (see _caption_trusted): write it
    # True ONLY when the attach genuinely happened post-processing. Under an
    # --allow-processing override on a still-processing video the track may be stuck,
    # so we mark it untrusted → the sweep will re-insert it once processing finishes.
    out = dict(receipt or {})
    out.update({
        "video": vid,
        "video_id": video_id,
        "captions_set": True,
        "captions_lang": args.lang,
        "captions_name": args.name,
        "captions_action": action,
        "captions_source": str(srt),
        "captions_post_processing": processed,
        "captions_updated_at": datetime.now(timezone.utc).isoformat(),
    })
    if not processed:
        print(f"  ⚠ attached while {pstatus} (--allow-processing) — marked UNTRUSTED; "
              "the caption sweep will re-insert it once processing finishes.",
              file=sys.stderr)
    write_receipt(receipt_path, out)
    print(f"\n✅ receipt → {receipt_path}")


if __name__ == "__main__":
    main()
