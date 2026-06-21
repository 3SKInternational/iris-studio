#!/usr/bin/env python3
"""Standalone idempotent pinned-comment poster for a 3SK Finance YouTube video.

GO-LIVE-AWARE. The Data API can POST a top-level comment (commentThreads.insert)
but a comment can only land on a video the public can see — a still-private or
scheduled video either rejects the insert or hides the comment. So this tool:

  1. resolves video_id + the pinned-comment TEXT from the upload receipt
     (`Production_Kits/Video_NN_youtube_upload.json` — `video_id` + `pinned_comment`,
      both stamped by upload_video.py / publish_video.py),
  2. fetches the LIVE status (videos().list part=status),
  3. posts ONLY when privacyStatus == "public"; a still-private/scheduled video
     is a clean NO-OP (exit 0) so the hourly comment-sweep posts it at go-live,
  4. is IDEMPOTENT: if the receipt already carries `comment_id` it does nothing
     (unless --force), so the sweep / retries never double-post,
  5. stamps `comment_id` + `comment_posted_at` into the receipt and pings Telegram
     "comment posted on Video_NN — pin it in Studio".

⚠ The Data API CANNOT PIN a comment — pinning a comment to the top is UI-only.
This tool only POSTS the comment and reminds you to pin it once (a 5-sec click in
YouTube Studio). Everything else is automatic.

ONE-TIME consent: the unattended run loads youtube_token.json (the long-lived
refresh token captured once by scripts/youtube_authorize.py). The granted
youtube.force-ssl scope already covers commentThreads.insert.

Usage:
  python3 scripts/post_comment.py Video_03 --dry-run    # validate + show plan, no network
  python3 scripts/post_comment.py Video_03              # post if public + not yet posted
  python3 scripts/post_comment.py Video_03 --video-id ABC123
  python3 scripts/post_comment.py Video_03 --force      # post even if comment_id already stamped
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

from youtube_client import (  # noqa: E402
    YouTubeAuthError,
    build_data_service,
    load_credentials,
)
from upload_video import (  # noqa: E402
    _is_transient_api_error,
    die,
    normalize_id,
    vault,
    write_receipt,
)

NOTIFY_SH = REPO / "scripts" / "notify.sh"


def _notify(message: str) -> None:
    """Best-effort Telegram ping (the canonical alert channel). Never fatal:
    a failed notify must not fail the post that already succeeded."""
    if not NOTIFY_SH.is_file():
        return
    try:
        subprocess.run([str(NOTIFY_SH), message], timeout=20,
                       check=False, capture_output=True)
    except Exception:  # noqa: BLE001 — notify is advisory; swallow everything.
        pass


def _live_privacy(youtube, video_id: str) -> str | None:
    """Return the live privacyStatus ('public'|'unlisted'|'private') or None if
    the video id resolves to nothing on the channel."""
    resp = youtube.videos().list(part="status", id=video_id).execute()
    items = resp.get("items") or []
    if not items:
        return None
    return items[0].get("status", {}).get("privacyStatus")


def _norm(text: str) -> str:
    """Whitespace-collapsed form for tolerant text equality."""
    return " ".join((text or "").split())


def _find_existing_comment(youtube, video_id: str, pinned: str,
                           *, max_pages: int = 3) -> str | None:
    """Return the thread id of an existing top-level comment whose text matches
    our pinned text, else None. Guards against double-posting when a comment was
    posted/pinned MANUALLY before this tool existed (or a receipt lost its
    comment_id): receipt-local idempotency can't see those, so we check channel
    truth. Scans up to max_pages of 100 (the launch videos have few comments;
    bounded so the hourly cost stays small)."""
    target = _norm(pinned)
    page = None
    for _ in range(max_pages):
        resp = youtube.commentThreads().list(
            part="snippet", videoId=video_id, maxResults=100,
            textFormat="plainText", pageToken=page).execute()
        for item in resp.get("items", []):
            snip = item.get("snippet", {}).get("topLevelComment", {}).get("snippet", {})
            if _norm(snip.get("textOriginal", "")) == target:
                return item.get("id")
        page = resp.get("nextPageToken")
        if not page:
            break
    return None


def post_pinned_comment(youtube, vlt: Path, label: str, *,
                        video_id_override: str | None = None,
                        force: bool = False, dry_run: bool = False,
                        do_notify: bool = True) -> dict:
    """Post the receipt's pinned comment on ONE video, go-live-aware + idempotent.

    Returns a status dict {label, status, detail} where status is one of:
      posted | would_post | already_posted | not_public | no_comment |
      no_video_id | not_found | comments_disabled | transient | error
    Only `error` (a hard API/4xx failure) should drive a non-zero scheduled exit;
    `not_public` is the normal pre-go-live no-op and `transient` is a retry-later
    blip. Per-video best-effort: an exception here is caught and classified, never
    raised, so a sweep keeps going.
    """
    from googleapiclient.errors import HttpError  # local import: matches upsert path

    rp = vlt / "Production_Kits" / f"{label}_youtube_upload.json"
    if not rp.is_file():
        return {"label": label, "status": "no_video_id",
                "detail": f"no receipt {rp.name}"}
    try:
        receipt = json.loads(rp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return {"label": label, "status": "error", "detail": f"bad receipt: {exc}"}

    video_id = video_id_override or receipt.get("video_id")
    if not video_id:
        return {"label": label, "status": "no_video_id",
                "detail": "receipt has no video_id"}

    pinned = (receipt.get("pinned_comment") or "").strip()
    if not pinned:
        return {"label": label, "status": "no_comment",
                "detail": "receipt has no pinned_comment text"}

    if receipt.get("comment_id") and not force:
        return {"label": label, "status": "already_posted",
                "detail": receipt["comment_id"]}

    # Terminal skip stamped by a prior pass (e.g. comments disabled) — stop
    # re-hitting the API every hour for a state that will never self-heal.
    if receipt.get("comment_skipped") and not force:
        return {"label": label, "status": "skipped",
                "detail": receipt["comment_skipped"]}

    # Go-live gate: never comment on a video the public can't see yet.
    try:
        privacy = _live_privacy(youtube, video_id)
    except HttpError as exc:
        kind = "transient" if _is_transient_api_error(exc) else "error"
        return {"label": label, "status": kind, "detail": f"status fetch: {exc}"}
    except Exception as exc:  # noqa: BLE001 — network blip etc.
        kind = "transient" if _is_transient_api_error(exc) else "error"
        return {"label": label, "status": kind, "detail": f"status fetch: {exc}"}

    if privacy is None:
        return {"label": label, "status": "not_found",
                "detail": f"{video_id} not on channel"}
    if privacy != "public":
        return {"label": label, "status": "not_public",
                "detail": f"privacy={privacy} — will post at go-live"}

    # Duplicate guard: a matching comment may already exist that we never stamped
    # (posted/pinned manually before this tool, or a receipt that lost its
    # comment_id). Posting again would double-comment, so check the live thread
    # first. If the CHECK itself fails we do NOT post — a visible duplicate is
    # worse and harder to undo than a deferred retry.
    try:
        existing_id = _find_existing_comment(youtube, video_id, pinned)
    except Exception as exc:  # noqa: BLE001 — HttpError + network blips
        kind = "transient" if _is_transient_api_error(exc) else "error"
        return {"label": label, "status": kind, "detail": f"dup-check: {exc}"}
    if existing_id:
        if not dry_run:
            out = dict(receipt)
            out.update({
                "video": label,
                "video_id": video_id,
                "comment_id": existing_id,
                "comment_posted_at": datetime.now(timezone.utc).isoformat(),
                "commented_via": "pre-existing",
            })
            write_receipt(rp, out)
        return {"label": label, "status": "already_posted",
                "detail": f"{existing_id} (matched existing comment)"}

    if dry_run:
        return {"label": label, "status": "would_post",
                "detail": f"{video_id}: would post {len(pinned)} chars"}

    body = {
        "snippet": {
            "videoId": video_id,
            "topLevelComment": {"snippet": {"textOriginal": pinned}},
        }
    }
    try:
        resp = youtube.commentThreads().insert(part="snippet", body=body).execute()
    except HttpError as exc:
        status_code = getattr(getattr(exc, "resp", None), "status", "?")
        reason = ""
        try:
            reason = (json.loads(exc.content.decode("utf-8"))
                      .get("error", {}).get("errors", [{}])[0].get("reason", ""))
        except Exception:  # noqa: BLE001
            pass
        if reason == "commentsDisabled":
            # Expected + terminal (Steve may legitimately turn comments off):
            # stamp a skip so later sweeps no-op instead of alerting every hour.
            try:
                out = dict(receipt)
                out["comment_skipped"] = "comments_disabled"
                write_receipt(rp, out)
            except Exception:  # noqa: BLE001
                pass
            return {"label": label, "status": "comments_disabled",
                    "detail": f"{video_id}: comments disabled (skip stamped)"}
        kind = "transient" if _is_transient_api_error(exc) else "error"
        return {"label": label, "status": kind,
                "detail": f"insert HTTP {status_code} ({reason or '?'}): {exc}"}
    except Exception as exc:  # noqa: BLE001
        kind = "transient" if _is_transient_api_error(exc) else "error"
        return {"label": label, "status": kind, "detail": f"insert: {exc}"}

    comment_id = resp.get("id", "")
    out = dict(receipt)
    out.update({
        "video": label,
        "video_id": video_id,
        "comment_id": comment_id,
        "comment_posted_at": datetime.now(timezone.utc).isoformat(),
        "commented_via": "post_comment.py",
    })
    write_receipt(rp, out)

    if do_notify:
        _notify(f"💬 Pinned comment posted on {label} "
                f"(https://youtu.be/{video_id}) — PIN it in Studio "
                f"(the API can't pin). Comment id: {comment_id}")
    return {"label": label, "status": "posted", "detail": comment_id}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="3SK YouTube pinned-comment poster (go-live-aware, idempotent)."
    )
    p.add_argument("video", help="Video id, e.g. Video_03 or 03.")
    p.add_argument("--video-id", help="YouTube video id (overrides the receipt).")
    p.add_argument("--force", action="store_true",
                   help="Bypass the receipt comment_id / skip-marker gate. The "
                        "duplicate guard still applies, so this will NOT post a "
                        "second identical comment if one already exists on the video.")
    p.add_argument("--dry-run", action="store_true",
                   help="Validate + show the plan; touch no network.")
    p.add_argument("--token", help="Override path to youtube_token.json.")
    return p.parse_args()


# Scheduled-sweep fatal set: ONLY a genuine API/4xx fault drives a non-zero exit
# (→ Telegram alert + retry marker). Expected terminal states (comments_disabled,
# not_found) and pre-go-live/transient are NOT faults — alerting on them hourly
# would be a non-self-healing storm.
_FAIL_STATUSES = {"error"}

# The manual CLI is stricter: if you named one video and nothing happened, a
# non-zero exit surfaces the typo/misconfig instead of a silent success.
_CLI_FAIL_STATUSES = _FAIL_STATUSES | {"no_video_id", "not_found", "comments_disabled"}


def main() -> None:
    args = parse_args()
    label, _nn = normalize_id(args.video)
    vlt = vault()

    print(f"video      : {label}")
    print(f"receipt    : {vlt / 'Production_Kits' / f'{label}_youtube_upload.json'}")
    print("mode       : post pinned comment IF public + not already posted "
          "(API cannot pin — pin manually once)")

    if args.dry_run:
        # Dry-run still needs the token to read live status (the go-live gate).
        try:
            creds = load_credentials(args.token)
        except YouTubeAuthError as exc:
            die(str(exc), code=2)
        youtube = build_data_service(creds)
        res = post_pinned_comment(youtube, vlt, label,
                                  video_id_override=args.video_id,
                                  force=args.force, dry_run=True, do_notify=False)
        print(f"\n--- DRY RUN --- {res['status']}: {res['detail']}")
        raise SystemExit(1 if res["status"] in _CLI_FAIL_STATUSES else 0)

    try:
        creds = load_credentials(args.token)
    except YouTubeAuthError as exc:
        die(str(exc), code=2)
    youtube = build_data_service(creds)

    res = post_pinned_comment(youtube, vlt, label,
                              video_id_override=args.video_id, force=args.force)
    icon = {"posted": "✅", "already_posted": "✓", "not_public": "⏳",
            "would_post": "•", "no_comment": "•", "no_video_id": "•",
            "skipped": "•", "not_found": "⚠", "comments_disabled": "⚠",
            "transient": "⚠", "error": "🔴"}.get(res["status"], "•")
    print(f"\n{icon} {res['status']}: {res['detail']}")
    raise SystemExit(1 if res["status"] in _CLI_FAIL_STATUSES else 0)


if __name__ == "__main__":
    main()
