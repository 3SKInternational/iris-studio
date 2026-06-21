#!/usr/bin/env python3
"""Sweep every uploaded video and post its pinned comment once it is LIVE.

Verify-and-backfill, go-live-aware. For each upload receipt
(`Production_Kits/Video_NN_youtube_upload.json` → video_id + pinned_comment):
  * already has comment_id            → SKIP (idempotent, cheap status check)
  * public + no comment yet           → POST the pinned comment, stamp the receipt
  * still private / scheduled         → NO-OP (will post on a later pass at go-live)
  * no pinned_comment text            → SKIP (benign)

This is the engine behind "auto-comment on publish": videos publish on a SCHEDULE,
so the comment can't be posted when publish_video.py *schedules* the release — only
once YouTube has actually flipped the video to public. Running this hourly catches
each video within ~1h of go-live, then skips it forever after (idempotent).

⚠ The Data API cannot PIN a comment. This posts it and pings Telegram to remind
you to pin it once in Studio (a 5-sec click). Pinning stays manual.

Quota: videos.list (status) ≈ 1 unit/video for the skip/gate path; a real
commentThreads.insert ≈ 50 units. Skipping already-commented videos keeps a full
sweep cheap.

Usage:
  python3 scripts/sweep_comments.py --dry-run     # report state, change nothing
  python3 scripts/sweep_comments.py               # post where public + missing
  python3 scripts/sweep_comments.py --force       # repost even if comment_id exists
  python3 scripts/sweep_comments.py --only Video_03
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
from upload_video import die, iter_upload_receipts, normalize_id, vault  # noqa: E402
from post_comment import post_pinned_comment  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Post the pinned comment on every uploaded video once it is public."
    )
    p.add_argument("--only", help="Limit to one video, e.g. Video_03 or 03.")
    p.add_argument("--force", action="store_true",
                   help="Repost even if the receipt already has a comment_id.")
    p.add_argument("--dry-run", action="store_true",
                   help="Report what would change (still reads live status); no writes.")
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

    mode = "DRY RUN — read-only" if args.dry_run else ("FORCE" if args.force else "live")
    print(f">>> comment sweep ({mode})…")

    only_label = normalize_id(args.only)[0] if args.only else None
    summary = {"checked": 0, "posted": 0, "already_posted": 0, "not_public": 0,
               "no_comment": 0, "would_post": 0, "skipped": 0, "terminal": 0,
               "transient": 0, "errors": 0}

    for label, _video_id, _rp, _data in iter_upload_receipts(vlt):
        if only_label and label != only_label:
            continue
        summary["checked"] += 1
        res = post_pinned_comment(youtube, vlt, label, force=args.force,
                                  dry_run=args.dry_run)
        status = res["status"]
        if status == "posted":
            summary["posted"] += 1
            print(f"  ✅ {label}: posted → {res['detail']}")
        elif status == "already_posted":
            summary["already_posted"] += 1
            print(f"  ✓ {label}: already posted — skip")
        elif status == "not_public":
            summary["not_public"] += 1
            print(f"  ⏳ {label}: {res['detail']}")
        elif status == "would_post":
            summary["would_post"] += 1
            print(f"  • {label}: {res['detail']}")
        elif status in ("no_comment", "no_video_id"):
            summary["no_comment"] += 1
            print(f"  • {label}: {res['detail']}")
        elif status == "skipped":
            summary["skipped"] += 1
            print(f"  • {label}: skipped ({res['detail']})")
        elif status in ("not_found", "comments_disabled"):
            # Expected terminal states — NOT faults. Report, don't alert.
            summary["terminal"] += 1
            print(f"  ⚠ {label}: {status} — {res['detail']}")
        elif status == "transient":
            summary["transient"] += 1
            print(f"  ⚠ {label}: transient — {res['detail']}", file=sys.stderr)
        else:  # error — a genuine API/4xx fault
            summary["errors"] += 1
            print(f"  🔴 {label}: {status} — {res['detail']}", file=sys.stderr)

    print(
        f"\nsummary: {summary['checked']} checked · {summary['posted']} posted · "
        f"{summary['already_posted']} already · {summary['not_public']} pre-go-live · "
        f"{summary['would_post']} would-post · {summary['no_comment']} no-text · "
        f"{summary['skipped']} skipped · {summary['terminal']} terminal · "
        f"{summary['transient']} transient · {summary['errors']} errors"
    )
    # Non-zero only on a real per-video error (the launchd wrapper turns that into
    # a Telegram alert + retry marker). pre-go-live / transient are expected.
    raise SystemExit(1 if summary["errors"] else 0)


if __name__ == "__main__":
    main()
