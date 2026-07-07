#!/usr/bin/env python3
"""Build 3b — flip an EXISTING YouTube video private→public + refresh metadata IN PLACE.

`upload_video.py` is insert-only: re-running it creates a DUPLICATE. Once a video
is already on the channel (private review upload), use THIS tool to publish it and
refresh its title/description/tags without re-uploading the mp4.

It calls `videos().update` (NOT insert). The dangerous part of an update is that
`part="snippet"` REPLACES the whole snippet — any field you omit is cleared, and
`title`+`categoryId` are required or the call 400s. To make that safe this tool:

  1. reads the receipt `Production_Kits/Video_NN_youtube_upload.json` for the
     YouTube video id (or --video-id to override),
  2. FETCHES the current resource via `videos().list(part="snippet,status")`,
  3. MERGES our changes onto the live snippet/status (title, description, tags,
     optional categoryId, privacyStatus) — preserving every other field
     (license, embeddable, madeForKids, publicStatsViewable, default audio lang…),
  4. then `videos().update(part="snippet,status")`.

Guardrails mirror the uploader (the roadmap "never auto-public" gate):
  * default target privacy is PUBLIC, which REQUIRES --allow-public.
  * --publish-at <ISO8601> schedules a future public release (status stays private
    + publishAt); also requires --allow-public.
  * unresolved [AFFILIATE LINK]/[WORKSHEET LINK] placeholders block a public/
    scheduled publish unless --allow-placeholders.

Usage:
  python3 scripts/publish_video.py Video_02 --allow-public --dry-run   # validate, no network
  python3 scripts/publish_video.py Video_02 --allow-public             # flip private→PUBLIC + refresh
  python3 scripts/publish_video.py Video_02 --privacy unlisted         # just refresh + go unlisted
  python3 scripts/publish_video.py Video_02 --video-id ABC123 --allow-public
"""

from __future__ import annotations

import argparse
import os
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

# Reuse the uploader's parsing/resolution/IO helpers verbatim so the two tools can
# never drift on how a description pack is read or limits are enforced.
from upload_video import (  # noqa: E402
    MAX_DESCRIPTION,
    MAX_TAGS_CHARS,
    MAX_TITLE,
    PLACEHOLDER_RE,
    _resolve_path,
    die,
    enforce_release_gate,
    normalize_id,
    parse_desc_pack,
    resolve_thumbnail,
    set_captions,
    set_thumbnail,
    vault,
    write_receipt,
)

import json  # noqa: E402


def load_receipt(receipt: Path) -> dict | None:
    if not receipt.is_file():
        return None
    try:
        return json.loads(receipt.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        die(f"could not read receipt {receipt}: {exc}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="3SK YouTube publish/refresh in place (videos.update).")
    p.add_argument("video", help="Video id, e.g. Video_02 or 02.")
    p.add_argument("--video-id", help="YouTube video id to update (overrides the receipt).")
    p.add_argument("--title", help="Title (overrides desc-pack frontmatter).")
    p.add_argument("--desc", help="Override the description-pack .md path.")
    p.add_argument("--privacy", choices=["private", "unlisted", "public"],
                   default="public", help="Target privacy (default: public).")
    p.add_argument("--publish-at", help="ISO8601 UTC scheduled publish time "
                   "(e.g. 2026-10-01T13:00:00Z). Keeps status private + publishAt.")
    p.add_argument("--category", help="Override categoryId (default: keep the video's current one).")
    p.add_argument("--allow-public", action="store_true",
                   help="Required to publish public OR schedule a publish.")
    p.add_argument("--allow-placeholders", action="store_true",
                   help="Permit unresolved [AFFILIATE LINK]/[WORKSHEET LINK] in a public/scheduled publish.")
    p.add_argument("--set-thumbnail", action="store_true",
                   help="Also re-resolve + set the thumbnail (default: leave the existing one).")
    p.add_argument("--thumbnail", help="Explicit thumbnail path (implies --set-thumbnail).")
    p.add_argument("--captions", help="Override the .srt path for the caption refresh.")
    p.add_argument("--no-captions", action="store_true",
                   help="Skip the idempotent caption (re)attach on publish.")
    p.add_argument("--token", help="Override path to youtube_token.json.")
    p.add_argument("--dry-run", action="store_true",
                   help="Validate + print the plan; touch no network.")
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
        die(f"no YouTube video id — pass --video-id, or upload first so {receipt_path.name} "
            "carries video_id. (This tool UPDATES an existing video; it does not upload.)")

    desc_pack = (Path(os.path.expanduser(args.desc)) if args.desc
                 else vlt / "Video_Descriptions" / f"{vid}_Description.md")
    meta = parse_desc_pack(desc_pack)
    title = (args.title or meta["title"] or "").strip()
    if not title:
        die(f"no title — pass --title or add `youtube_title:` to {desc_pack.name}.")

    # --- local validation (limits) ---
    if len(title) > MAX_TITLE:
        die(f"title is {len(title)} chars (max {MAX_TITLE}).")
    if args.category and not str(args.category).isdigit():
        die(f"--category must be a numeric YouTube categoryId, got {args.category!r} "
            "(e.g. 27 = Education).")
    if len(meta["description"]) > MAX_DESCRIPTION:
        die(f"description is {len(meta['description'])} chars (max {MAX_DESCRIPTION}).")
    tags = list(meta["tags"])
    while sum(len(t) + 1 for t in tags) > MAX_TAGS_CHARS and tags:
        dropped = tags.pop()
        print(f"  ⚠ dropping tag to stay under {MAX_TAGS_CHARS} chars: {dropped!r}")

    publish_at = args.publish_at
    if publish_at:
        try:
            pa_dt = datetime.fromisoformat(publish_at.replace("Z", "+00:00"))
        except ValueError:
            die(f"--publish-at not ISO8601: {publish_at!r} (e.g. 2026-10-01T13:00:00Z).")
        if pa_dt.tzinfo is None:
            pa_dt = pa_dt.replace(tzinfo=timezone.utc)
        if pa_dt <= datetime.now(timezone.utc):
            die(f"--publish-at is in the past ({publish_at}); must be a future time.")

    going_public = args.privacy == "public" or bool(publish_at)
    if going_public and not args.allow_public:
        die("refusing to publish public/scheduled without --allow-public (the review-gate guardrail).")

    gate_raw = meta.get("do_not_publish_before") or (receipt or {}).get("do_not_publish_before")
    gate_source = desc_pack.name if meta.get("do_not_publish_before") else "receipt"
    effective_moment = pa_dt if publish_at else datetime.now(timezone.utc)
    # Release-date gate (pre-network, also covers --dry-run). Trust the receipt's
    # last-known privacy to exempt a metadata refresh of an already-public video;
    # the authoritative live re-check happens after the fetch below.
    enforce_release_gate(
        going_public=going_public,
        effective_moment=effective_moment,
        gate_raw=gate_raw,
        gate_source=gate_source,
        already_public=(receipt or {}).get("privacy") == "public",
        vid=vid,
        desc_pack_name=desc_pack.name,
    )

    if PLACEHOLDER_RE.search(meta["description"]):
        if going_public and not args.allow_placeholders:
            die("description still has unresolved [AFFILIATE LINK]/[WORKSHEET LINK] placeholders "
                "— resolve them or pass --allow-placeholders for a public/scheduled publish.")
        print("  ⚠ description contains unresolved link placeholders.")

    set_thumb = args.set_thumbnail or bool(args.thumbnail)
    thumb = resolve_thumbnail(vlt, vid, args.thumbnail) if set_thumb else None
    if set_thumb and not thumb:
        die("--set-thumbnail/--thumbnail given but no thumbnail found.")

    target_privacy = "private" if publish_at else args.privacy

    # --- plan ---
    print(f"video      : {vid}")
    print(f"video_id   : {video_id}  →  https://youtu.be/{video_id}")
    print(f"title      : {title}")
    print(f"privacy    : {target_privacy}" + (f"  → publishAt {publish_at}" if publish_at else ""))
    print(f"category   : {args.category or '(keep current)'}")
    print(f"tags       : {len(tags)} ({sum(len(t) + 1 for t in tags)} chars)")
    print(f"desc len   : {len(meta['description'])} chars")
    print(f"thumbnail  : {thumb if thumb else '(leave existing)'}")
    print(f"receipt    : {receipt_path}")
    print("mode       : videos.update (in-place; NO re-upload, NO duplicate)")

    if args.dry_run:
        print("\n--- DRY RUN (no network, nothing changed). Drop --dry-run to publish. ---")
        return

    # --- network ---
    try:
        creds = load_credentials(args.token)
    except YouTubeAuthError as exc:
        die(str(exc), code=2)
    youtube = build_data_service(creds)

    from googleapiclient.errors import HttpError

    # 1) FETCH the live resource so we merge (never blank) other snippet/status fields.
    try:
        current = youtube.videos().list(part="snippet,status", id=video_id).execute()
    except HttpError as exc:
        die(f"could not fetch video {video_id} (HTTP {exc.resp.status}): {exc}")
    items = current.get("items") or []
    if not items:
        die(f"video {video_id} not found / not owned by this channel — check --video-id.")
    live = items[0]
    live_snippet = live.get("snippet") or {}
    live_status = live.get("status") or {}
    prev_privacy = live_status.get("privacyStatus")

    # Authoritative release-date gate: re-check against the LIVE privacy (the
    # receipt can be stale). A genuine refresh of an already-public video is
    # exempt; a private→public transition is held to the declared release date.
    enforce_release_gate(
        going_public=going_public,
        effective_moment=effective_moment,
        gate_raw=gate_raw,
        gate_source=gate_source,
        already_public=(prev_privacy == "public"),
        vid=vid,
        desc_pack_name=desc_pack.name,
    )

    # 2) Build the update body from a WHITELIST of WRITABLE fields only.
    # videos.update REPLACES the snippet/status parts wholesale, and echoing
    # read-only fields back (madeForKids, thumbnails, channelId/Title, publishedAt,
    # uploadStatus, liveBroadcastContent, localized, …) risks a 400. So we start
    # from clean dicts, carry over only the writable fields that already exist on
    # the video, then apply our overrides — never `dict(live[...])`.
    snippet = {
        "title": title,
        "description": meta["description"],
        "tags": tags,
    }
    for k in ("defaultLanguage", "defaultAudioLanguage"):
        if live_snippet.get(k):
            snippet[k] = live_snippet[k]
    if args.category:
        snippet["categoryId"] = str(args.category)
    else:
        snippet["categoryId"] = live_snippet.get("categoryId") or "27"  # required by update.

    status = {
        "privacyStatus": target_privacy,
        # selfDeclaredMadeForKids is the WRITABLE form; madeForKids is its read-only
        # echo. Preserve the video's current kids designation (prefer the declared
        # value, fall back to the read-only one) so we never flip it unexpectedly.
        "selfDeclaredMadeForKids": bool(
            live_status.get("selfDeclaredMadeForKids", live_status.get("madeForKids", False))
        ),
    }
    for k in ("license", "embeddable", "publicStatsViewable"):
        if k in live_status:
            status[k] = live_status[k]
    if publish_at:
        # Scheduled: privacyStatus stays 'private' (set above via target_privacy)
        # and publishAt flips it public at that time. Non-scheduled omits publishAt
        # entirely (no stale value carried over, since we built status fresh).
        status["publishAt"] = publish_at.replace("Z", "+00:00")

    body = {"id": video_id, "snippet": snippet, "status": status}

    # 3) UPDATE in place.
    print(f"\n>>> updating {video_id} ({prev_privacy} → {target_privacy})…")
    try:
        resp = youtube.videos().update(part="snippet,status", body=body).execute()
    except HttpError as exc:
        die(f"update failed (HTTP {exc.resp.status}): {exc}")
    new_privacy = (resp.get("status") or {}).get("privacyStatus", target_privacy)
    url = f"https://youtu.be/{video_id}"
    print(f"  ✅ updated: privacy now '{new_privacy}'  →  {url}")

    if thumb:
        set_thumbnail(youtube, video_id, thumb)

    # Idempotent caption (re)attach: captions live on the videoId, so a successful
    # publish is a safe point to guarantee the timed track is present. set_captions
    # is list→update/insert (no duplicate) and non-fatal (a caption hiccup never
    # sinks a good publish). Skipped if --no-captions or the SRT isn't there.
    captions_set = False
    if not args.no_captions:
        srt = _resolve_path(args.captions, vlt, f"Footage_and_Edits/{vid}_v2.srt")
        if srt.is_file():
            captions_set = set_captions(youtube, video_id, srt)
        else:
            print(f"  ℹ no captions ({srt.name} missing) — skipping caption attach.")

    # 4) refresh the receipt (keep prior fields; stamp the publish).
    out = dict(receipt or {})
    out.update({
        "video": vid,
        "video_id": video_id,
        "url": url,
        "title": title,
        "privacy": new_privacy,
        "publish_at": status.get("publishAt"),
        "category_id": snippet.get("categoryId"),
        "tags": tags,
        "pinned_comment": meta.get("pinned_comment") or out.get("pinned_comment"),
        "do_not_publish_before": gate_raw or out.get("do_not_publish_before"),
        "thumbnail_set": bool(thumb) or out.get("thumbnail_set", False),
        "captions_set": captions_set or out.get("captions_set", False),
        "last_published_at": datetime.now(timezone.utc).isoformat(),
        "published_via": "publish_video.py",
    })
    if captions_set:
        out["captions_updated_at"] = datetime.now(timezone.utc).isoformat()
        out["captions_post_processing"] = True  # set_captions only attaches post-processing.
    write_receipt(receipt_path, out)
    print(f"\n✅ receipt → {receipt_path}")

    # Auto-post the pinned comment. The poster is go-live-aware + idempotent: if we
    # just went public it posts now; if this was a SCHEDULED publish (still private
    # until publishAt) it's a clean no-op and the hourly comment-sweep posts it at
    # go-live. Best-effort — a comment hiccup never sinks a good publish. The Data
    # API still can't PIN, so it pings Telegram to pin manually once.
    if out.get("pinned_comment") and not out.get("comment_id"):
        try:
            from post_comment import post_pinned_comment  # local: avoid import cost on dry-run
            res = post_pinned_comment(youtube, vlt, vid)
            msg = {"posted": "✅ pinned comment posted (now PIN it in Studio)",
                   "not_public": "⏳ scheduled — comment will auto-post at go-live",
                   "already_posted": "✓ pinned comment already posted"}.get(
                       res["status"], f"ℹ pinned comment: {res['status']} ({res['detail']})")
            print(f"  {msg}")
        except Exception as exc:  # noqa: BLE001 — never let a comment failure sink publish
            print(f"  ⚠ pinned-comment auto-post skipped ({type(exc).__name__}: {exc}); "
                  "the hourly comment-sweep will retry.")


if __name__ == "__main__":
    main()
