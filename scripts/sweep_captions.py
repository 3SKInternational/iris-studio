#!/usr/bin/env python3
"""Sweep every uploaded video and ensure it has an English caption track.

Verify-and-backfill: for each video that has an upload receipt
(`Production_Kits/Video_NN_youtube_upload.json` → video_id), check the live
caption tracks. If our managed "English" track is already present → SKIP (a cheap
captions.list, no re-upload). If it's missing → attach the timed SRT
(`Footage_and_Edits/Video_NN_v2.srt`). Idempotent and cheap to re-run.

This is the standalone form of the post-upload sweep that upload_video.py runs
automatically. It exists because a brand-new upload can still be processing when
its inline caption insert fires (which then silently fails) — this sweep catches
that straggler on a later pass while skipping every video that's already fine.

Scope note: only videos with an upload receipt are covered (those are the ones we
can map to a known SRT). A video uploaded outside the pipeline won't appear here.

Quota: captions.list ≈ 50 units per video (the skip path); a needed insert ≈ 400.
Skipping already-captioned videos keeps a full sweep cheap.

Usage:
  python3 scripts/sweep_captions.py --dry-run     # report state, change nothing
  python3 scripts/sweep_captions.py               # add where missing, skip present
  python3 scripts/sweep_captions.py --force       # update even where a track exists
  python3 scripts/sweep_captions.py --only Video_02
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from youtube_client import (  # noqa: E402
    YouTubeAuthError,
    build_data_service,
    load_credentials,
)
from upload_video import die, sweep_captions, vault  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Verify + backfill English captions across all uploaded videos."
    )
    p.add_argument("--only", help="Limit to one video, e.g. Video_02 or 02.")
    p.add_argument("--force", action="store_true",
                   help="Update captions even if a track already exists (default: skip present).")
    p.add_argument("--dry-run", action="store_true",
                   help="Report what would change (still LISTS tracks); makes no writes.")
    p.add_argument("--token", help="Override path to youtube_token.json.")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    vlt = vault()

    try:
        creds = load_credentials(args.token)
    except YouTubeAuthError as exc:
        die(str(exc), code=2)
    youtube = build_data_service(creds)

    mode = "DRY RUN — read-only" if args.dry_run else ("FORCE update" if args.force else "live")
    print(f">>> caption sweep ({mode})…")
    summary = sweep_captions(youtube, vlt, force=args.force, only=args.only,
                             dry_run=args.dry_run)
    print(
        f"\nsummary: {summary['checked']} checked · {summary['added']} added · "
        f"{summary['updated']} updated · {summary['skipped']} present/skipped · "
        f"{summary['no_srt']} missing-srt · {summary['transient']} transient · "
        f"{summary['errors']} errors"
    )
    # Non-zero exit on any per-video error so a scheduled run surfaces a real problem
    # (the launchd wrapper turns a non-zero exit into a Telegram alert).
    raise SystemExit(1 if summary["errors"] else 0)


if __name__ == "__main__":
    main()
