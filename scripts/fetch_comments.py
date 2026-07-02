#!/usr/bin/env python3
"""Fetch INCOMING viewer comments for each published 3SK Finance video and write
a dated export the `community-manager` agent reads — closing the LEARNS-side gap
where incoming comments were manual-paste-only.

This is the READ counterpart to post_comment.py / sweep_comments.py (which WRITE
the pinned comment). It reuses the SAME long-lived youtube.force-ssl token:
`commentThreads().list` is already covered by that scope (the same scope
`commentThreads().insert` needs), so NO new OAuth consent is required.

Go-live-aware + read-only against YouTube. The ONLY writes are the export files
under BRANDS/3SK_Finance/Channel_Intelligence/Engagement/:
  * <YYYY-MM-DD>_<label>_comments.md  — dated snapshot
  * <label>_comments_latest.md        — stable path the ingest routine points
                                        community-manager at (overwritten each run)

Never fabricates: a video with comments disabled or zero comments produces an
honest empty/zeroed export, never invented rows.

Usage:
  python3 scripts/fetch_comments.py                 # all published videos
  python3 scripts/fetch_comments.py --video Video_03 # one video by label
  python3 scripts/fetch_comments.py --max-pages 5   # bound the per-video pull
  python3 scripts/fetch_comments.py --token /path/to/youtube_token.json
"""
from __future__ import annotations

import argparse
import datetime as _dt
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from upload_video import die, iter_upload_receipts, vault  # noqa: E402
from youtube_client import build_data_service, load_credentials, resolve_channel  # noqa: E402

# vault() resolves to the brand dir (.../BRANDS/3SK_Finance), so this is relative
# to it — matching how iter_upload_receipts reads vlt/"Production_Kits".
ENGAGEMENT_SUBPATH = "Channel_Intelligence/Engagement"


def _today() -> str:
    return _dt.date.today().isoformat()


def _classify_state(items: list) -> str:
    """Pure classifier for a videos().list(part='status') response's items.

    Returns:
      * 'public'     — privacyStatus == 'public' (has a public comment thread).
      * 'not-public' — the video exists but is private/unlisted/scheduled.
      * 'missing'    — videos.list returned NO item. For the channel OWNER
                       (this token is the owner) that does NOT mean "unpublished":
                       the owner sees their own private/scheduled videos too. No
                       item = the id is deleted or the receipt is STALE. Surfacing
                       this instead of collapsing it into a benign 'not public yet'
                       is what turns silent receipt drift into a visible signal —
                       a re-uploaded/deleted video otherwise looks identical to a
                       scheduled one, so its comments go silently un-ingested
                       (V4 ClJVIUtwsVE, ~1 week 2026-06-22 → 2026-07-02)."""
    if not items:
        return "missing"
    return "public" if items[0].get("status", {}).get("privacyStatus") == "public" else "not-public"


def _video_state(youtube, video_id: str) -> str:
    """Live one-shot state for a receipt's video_id (see _classify_state)."""
    resp = youtube.videos().list(part="status", id=video_id).execute()
    return _classify_state(resp.get("items", []))


def _author_channel_id(snippet: dict) -> str:
    return snippet.get("authorChannelId", {}).get("value", "")


_MD_ESCAPE = ("\\", "`", "*", "_", "[", "]")


def _clean(s: str) -> str:
    """Untrusted viewer text → one safe single-line markdown token.

    community-manager treats this export as ground truth ("every line is a real
    viewer comment"), so a comment must NEVER be able to forge a second row or a
    system `> NOTE:` line. Flattening all whitespace kills the line-start attack
    (a `-`/`>`/`#` only becomes structural after a newline); escaping the emphasis
    chars protects the `**@author**` bullet structure. One comment = one row,
    always."""
    flat = " ".join(str(s).split())
    for ch in _MD_ESCAPE:
        flat = flat.replace(ch, "\\" + ch)
    return flat


def fetch_threads(youtube, video_id: str, *, max_pages: int,
                  owner_channel_id: str = "") -> tuple[list[dict], str]:
    """Return (threads, note). Each thread: {author, published, text, likes, replies:[...]}.
    note is '' on success, or a human reason string when the pull could not run
    (comments disabled, etc.) — in which case threads is []. Top-level comments
    authored by the channel OWNER (our own pinned/outgoing comment — handled by
    post_comment.py, not an incoming viewer comment) are skipped so the
    community-manager only triages genuine audience replies."""
    from googleapiclient.errors import HttpError  # local import: dep present at runtime

    threads: list[dict] = []
    page = None
    try:
        for _ in range(max_pages):
            resp = youtube.commentThreads().list(
                part="snippet,replies", videoId=video_id, maxResults=100,
                textFormat="plainText", order="time", pageToken=page,
            ).execute()
            for item in resp.get("items", []):
                top = item.get("snippet", {}).get("topLevelComment", {}).get("snippet", {})
                if owner_channel_id and _author_channel_id(top) == owner_channel_id:
                    continue  # our own outgoing comment — not incoming audience signal
                replies = []
                for r in item.get("replies", {}).get("comments", []):
                    rs = r.get("snippet", {})
                    if owner_channel_id and _author_channel_id(rs) == owner_channel_id:
                        continue  # our own reply — not incoming audience signal
                    replies.append({
                        "author": rs.get("authorDisplayName", ""),
                        "published": rs.get("publishedAt", ""),
                        "text": rs.get("textOriginal", ""),
                    })
                threads.append({
                    "author": top.get("authorDisplayName", ""),
                    "published": top.get("publishedAt", ""),
                    "text": top.get("textOriginal", ""),
                    "likes": top.get("likeCount", 0),
                    "replies": replies,
                })
            page = resp.get("nextPageToken")
            if not page:
                break
    except HttpError as e:
        reason = ""
        try:
            reason = e.error_details[0].get("reason", "")  # type: ignore[attr-defined]
        except Exception:
            reason = ""
        if "commentsDisabled" in str(e) or reason == "commentsDisabled":
            return [], "comments disabled on this video"
        return [], f"YouTube API error: {reason or e}"
    return threads, ""


def _render_export(label: str, video_id: str, threads: list[dict], note: str) -> str:
    total = sum(1 + len(t["replies"]) for t in threads)
    lines = [
        "---",
        f"date: {_today()}",
        "type: comment-export",
        f"video: {label}",
        f"video_id: {video_id}",
        f"top_level: {len(threads)}",
        f"total_comments: {total}",
        "source: youtube-data-api (commentThreads.list)",
        "tags:",
        "  - brand/3sk-finance",
        "  - engagement/comments",
        "---",
        "",
        f"# {label} — incoming viewer comments ({_today()})",
        "",
        "_Raw export for the `community-manager` agent. Every line is a real "
        "viewer comment pulled from the YouTube Data API — none invented. One "
        "top-level comment per bullet; replies are nested._",
        "",
    ]
    if note:
        lines += [f"> NOTE: {note}", ""]
    if not threads:
        lines += ["_No comments on this video yet._", ""]
        return "\n".join(lines)
    for t in threads:
        likes = f" ({t['likes']} likes)" if t["likes"] else ""
        lines.append(f"- **@{_clean(t['author'])}**{likes} — {_clean(t['text'])}")
        for r in t["replies"]:
            lines.append(f"    - ↳ **@{_clean(r['author'])}** — {_clean(r['text'])}")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="Fetch incoming viewer comments per published video.")
    ap.add_argument("--video", help="Only this video label (e.g. Video_03).")
    ap.add_argument("--max-pages", type=int, default=10,
                    help="Max 100-comment pages per video (default 10 = up to 1000).")
    ap.add_argument("--token", help="Override path to youtube_token.json.")
    args = ap.parse_args()

    vlt = vault()
    out_dir = vlt / ENGAGEMENT_SUBPATH
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        creds = load_credentials(args.token)
        youtube = build_data_service(creds)
        owner_channel_id = resolve_channel(youtube).get("id", "")
    except Exception as e:  # noqa: BLE001 — surface auth failure to the wrapper
        die(f"could not load YouTube credentials: {e}", code=2)

    if not owner_channel_id:
        # Fail CLOSED: without the owner id the owner-comment filter is disabled,
        # and our own pinned/outgoing comment would leak in as "incoming" audience
        # signal — a fabrication-adjacent leak. Refuse rather than degrade.
        die("resolved YouTube channel has no id — refusing to run so our own "
            "comments cannot leak in as incoming audience signal", code=2)

    receipts = list(iter_upload_receipts(vlt))
    if args.video:
        receipts = [r for r in receipts if r[0] == args.video]
        if not receipts:
            die(f"no upload receipt for label {args.video!r}", code=2)

    if not receipts:
        print("fetch_comments: no upload receipts with a video_id — nothing to fetch.")
        # Canonical machine-readable line, identical shape to the normal run so the
        # routine can match one token (`status: ok (<N> comments)`) in every branch.
        print("total comments across all public videos: 0")
        print("status: ok (0 comments)")
        return

    grand_total = 0
    per_video: list[str] = []
    hard_errors: list[str] = []
    drift: list[str] = []
    for label, video_id, _rp, _data in receipts:
        try:
            state = _video_state(youtube, video_id)
        except Exception as e:  # noqa: BLE001
            per_video.append(f"  {label}: status check failed ({e}) — skipped")
            continue
        if state == "missing":
            # The receipt's video_id is not on the channel (deleted / re-uploaded
            # under a new id / stale receipt). Skip like any non-public video so
            # live-video ingestion still succeeds, but record it as DRIFT so it is
            # surfaced, not hidden behind a benign-looking "not public yet".
            drift.append(f"{label}: receipt video_id {video_id} not found on channel")
            per_video.append(
                f"  {label}: video_id {video_id} NOT FOUND on channel — "
                "RECEIPT DRIFT (deleted/re-uploaded/stale receipt), skipped")
            continue
        if state == "not-public":
            per_video.append(f"  {label}: not public yet — skipped")
            continue
        threads, note = fetch_threads(youtube, video_id, max_pages=args.max_pages,
                                      owner_channel_id=owner_channel_id)
        is_hard = note.startswith("YouTube API error")
        if is_hard:
            # A hard API fault (auth scope / quota / 4xx) — NOT comments-disabled.
            # Don't bury it as an ok per-video note; collect it to fail the run so
            # the routine's INCOMPLETE path pages Steve. Skip the writes so we keep
            # the last good export rather than clobbering it with an empty one.
            hard_errors.append(f"{label}: {note}")
            per_video.append(f"  {label}: FETCH FAILED — {note} (kept last export)")
            continue
        total = sum(1 + len(t["replies"]) for t in threads)
        grand_total += total
        body = _render_export(label, video_id, threads, note)
        latest = out_dir / f"{label}_comments_latest.md"
        latest.write_text(body, encoding="utf-8")
        # Only snapshot a dated copy when there's real signal — avoids accumulating
        # one near-identical empty file per video per day forever.
        if total > 0:
            (out_dir / f"{_today()}_{label}_comments.md").write_text(body, encoding="utf-8")
        tag = f" [{note}]" if note else ""
        per_video.append(f"  {label}: {total} comment(s){tag} → {latest.relative_to(vlt)}")

    print("fetch_comments: pulled incoming comments for published videos")
    print("\n".join(per_video))
    print(f"total comments across all public videos: {grand_total}")
    if hard_errors:
        print("HARD API ERRORS (not comments-disabled):")
        for he in hard_errors:
            print(f"  {he}")
        die(f"{len(hard_errors)} video(s) failed to fetch — likely token scope/quota",
            code=2)
    if drift:
        # Not a hard error (live videos still ingested → keep exit 0 + the
        # `status: ok` contract the comment-ingest routine matches), but a real
        # "someone should reconcile the receipt" signal. Print a distinct,
        # greppable block so the routine + a human reading the log both see it.
        print(f"RECEIPT DRIFT ({len(drift)}) — receipt video_id(s) not on the "
              "channel (deleted / re-uploaded under a new id / stale receipt); "
              "reconcile the Production_Kits upload receipt(s):")
        for d in drift:
            print(f"  {d}")
    print(f"status: ok ({grand_total} comments)")


if __name__ == "__main__":
    main()
