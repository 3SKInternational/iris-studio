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
    p.add_argument("--no-synthetic", action="store_true",
                   help="Do NOT declare altered/synthetic media on this update. Default is "
                        "to declare it: every 3SK Finance video is AI-generated, and "
                        "videos.update DELETES any status property the request omits.")
    p.add_argument("--clear-schedule", action="store_true",
                   help="Explicitly DROP an existing scheduled publishAt (unschedule). "
                        "Without this flag an existing schedule is PRESERVED across a "
                        "metadata refresh — omitting publishAt on a status update clears it.")
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


def build_status(target_privacy: str, live_status: dict, publish_at: str | None,
                 clear_schedule: bool, synthetic: bool = True) -> dict:
    """Build the writable `status` part for videos.update.

    videos.update REPLACES the status part wholesale, so every field we want kept
    must be re-sent. Extracted from main() 2026-07-28 (two-pass codebase audit,
    lane C) specifically so the schedule-preservation rule below is testable —
    the defect it fixes was silent, and an inline branch had no runnable check.

    Rules:
      - explicit publish_at            -> schedule it
      - clear_schedule                 -> drop any schedule (omission clears it)
      - existing publishAt, staying private -> PRESERVE it
      - going public OR unlisted       -> never carry publishAt (YouTube pairs a
                                        schedule with privacyStatus=private ONLY;
                                        schedule_publish.py:170 records this too)
    """
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
        # Scheduled: privacyStatus stays 'private' (set by the caller via
        # target_privacy) and publishAt flips it public at that time.
        status["publishAt"] = publish_at.replace("Z", "+00:00")
    elif clear_schedule:
        # Explicit unschedule. Omitting publishAt on a status update is what clears
        # it server-side, so this branch deliberately does nothing.
        pass
    elif live_status.get("publishAt") and target_privacy == "private":
        # PRESERVE an existing schedule. publishAt used to be written ONLY when
        # --publish-at was passed, while the live publishAt was never read back — so
        # the documented metadata-refresh form
        #     publish_video.py Video_09 --privacy private
        # silently CANCELLED a scheduled release: the video went private with no
        # publishAt and never published, with no warning (going_public is False, so
        # the release gate never fires). The receipt write then stored
        # publish_at: None, destroying the local record, and youtube_reality_check
        # compared private-vs-private and reported CLEAN — the same command rewrote
        # the receipt the tripwire diffs against. 9 of 13 in-tree receipts carry a
        # publish_at. Sibling schedule_publish.py:173-176 has always done this and
        # names the reason: "re-send so an omission can't clear an existing schedule."
        #
        # ROUND-2 REGRESSION (2026-07-28 review): `!= "public"` also matched
        # "unlisted", but YouTube requires privacyStatus=private to PAIR with a
        # scheduled publishAt (schedule_publish.py:170 records this as required);
        # a `--privacy unlisted` update on a scheduled video would have sent an
        # illegal privacyStatus+publishAt combination and most likely 400'd. main()
        # now die()s that combination explicitly before it reaches the API (see
        # the unlisted+schedule guard ahead of build_status's call site).
        status["publishAt"] = live_status["publishAt"]

    # RE-ASSERT the altered/synthetic-media disclosure on every update.
    # videos.update REPLACES the status part, and the API reference is explicit:
    # "If you are submitting an update request, and your request does not specify
    # a value for a property that already has a value, the property's existing
    # value will be deleted." This file never set containsSyntheticMedia at all,
    # so EVERY metadata refresh silently cleared the AI-disclosure flag on a
    # public finance channel — and nothing could detect it, because videos.list
    # does not return the property to the owner (verified: it reads None for all
    # 12 live videos). Six receipts record published_via: publish_video.py while
    # still claiming contains_synthetic_media: true, a local record asserting a
    # disclosure that is no longer on the video.
    #
    # Defaults TRUE because every 3SK Finance video is AI-generated end to end
    # (AI-generated imagery + ElevenLabs TTS narration), and over-disclosing is
    # harmless while under-disclosing is the compliance risk. --no-synthetic is
    # the deliberate escape hatch for a future non-AI upload.
    # 2026-07-29, resolved from the API docs rather than by testing on a live video.
    if synthetic:
        status["containsSyntheticMedia"] = True
    return status


def unlisted_schedule_conflict_message(target_privacy: str, clear_schedule: bool,
                                       live_status: dict, vid: str) -> str | None:
    """None if safe to proceed; else the die() message for main().

    --privacy unlisted cannot carry a schedule -- YouTube pairs a scheduled
    publishAt with privacyStatus=private only (schedule_publish.py:170 records
    this as a required pairing). Extracted to a pure function (mirrors
    upload_video.reupload_guard_message) so this refuses BEFORE the API call
    with an actionable message, instead of surfacing as an opaque 400
    (round-2 audit finding, 2026-07-28). --clear-schedule is exempt -- that IS
    how you get out of this state."""
    if (target_privacy == "unlisted" and not clear_schedule
            and live_status.get("publishAt")):
        return (f"{vid} has a scheduled publishAt {live_status['publishAt']}; "
                "--privacy unlisted cannot carry a schedule (YouTube pairs a scheduled "
                "publish with privacyStatus=private only) — pass --clear-schedule to "
                "unschedule, or --privacy private to keep the schedule.")
    return None


def schedule_notice(status: dict, live_status: dict, publish_at: str | None,
                    clear_schedule: bool) -> list[str]:
    """Operator-facing line(s) about what is happening to an existing schedule.

    Returns a LIST — empty when there is nothing schedule-related to say — rather
    than a string-or-None the caller branches on. Extracting this from main()
    (2026-07-28) pinned the logic but left `if notice:` at the call site, which
    survived mutation because no suite drives main(). Returning a list lets the
    caller loop, so the silent case is structural and there is no wiring branch
    left to be wrong.

    Callers print these AFTER the live fetch: the plan block runs before the
    fetch and cannot know the current publishAt, so this is the first point where
    the schedule's fate is both known and still changeable.
    """
    if status.get("publishAt") and not publish_at:
        return [f"schedule   : PRESERVING existing publishAt {status['publishAt']} "
                "(pass --clear-schedule to drop it)"]
    # `and not publish_at`: with BOTH --publish-at and --clear-schedule, the run
    # SETS a new schedule (build_status writes publish_at first), so announcing
    # "CLEARING existing publishAt <old>" described the opposite of what happened
    # — the operator was told a schedule was being dropped on the very run that
    # installed one (round-3 review, 2026-07-28).
    if clear_schedule and live_status.get("publishAt") and not publish_at:
        return [f"schedule   : CLEARING existing publishAt {live_status['publishAt']} "
                "(--clear-schedule)"]
    return []


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

    conflict_msg = unlisted_schedule_conflict_message(
        target_privacy, args.clear_schedule, live_status, vid)
    if conflict_msg:
        die(conflict_msg)

    status = build_status(target_privacy, live_status, publish_at,
                         args.clear_schedule, synthetic=not args.no_synthetic)

    for _line in schedule_notice(status, live_status, publish_at, args.clear_schedule):
        print(_line)

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

    # Confirm the disclosure PERSISTED. The update response is the only place it
    # is observable — videos.list does not return containsSyntheticMedia to the
    # owner (verified: None for all 12 live videos), so a list-based check
    # false-negatives every time. schedule_publish.py:199 has always done this;
    # this file never did, which is how the flag could be dropped silently.
    if status.get("containsSyntheticMedia"):
        echoed = (resp.get("status") or {}).get("containsSyntheticMedia")
        if echoed is not True:
            die(f"synthetic-media disclosure NOT accepted — update response says "
                f"containsSyntheticMedia={echoed!r}. The video may now be public "
                f"WITHOUT its AI-disclosure label. Re-run, or set it in YouTube "
                f"Studio before treating this video as compliant.")
        print("  ✅ altered/synthetic-media disclosure confirmed on the video")

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
