#!/usr/bin/env python3
"""Build 3 — YouTube upload + scheduled publish (YouTube Data API v3).

Takes a video id (``Video_01`` or ``01``), resolves the orchestrator's rendered
mp4 + the generated ``.srt`` captions + the ``video-description-writer`` pack
(description, tags, pinned comment) + the thumbnail, and uploads to the 3SK
Finance channel as **private by default** (the roadmap's review-gate guardrail:
never auto-public without an explicit opt-in). Optionally schedules a publish
time. Resumable upload survives network drops. Writes a receipt JSON.

Resolution from a video id (under the 3SK Finance vault; override $SK_VAULT):
  video       : Footage_and_Edits/Video_NN_v2.mp4        (--video-file to override)
  captions    : Footage_and_Edits/Video_NN_v2.srt        (--captions to override)
  desc pack   : Video_Descriptions/Video_NN_Description.md (--desc to override)
  thumbnail   : Thumbnails/Video_NN*.png|jpg (best-effort; --thumbnail to override)
  receipt out : Production_Kits/Video_NN_youtube_upload.json

Title precedence: --title  >  desc-pack frontmatter `youtube_title:`. There is no
silent guess from the packaging file — if neither is present the script stops and
prints the packaging path so a human picks the title.

Guardrails (the roadmap's "never auto-public" gate):
  * default privacy is PRIVATE; `--privacy public` requires `--allow-public`.
  * `--publish-at <ISO8601>` schedules a future PUBLIC release, so it ALSO
    requires `--allow-public` (a scheduled publish goes public unattended).
  * unresolved `[AFFILIATE LINK]` / `[WORKSHEET LINK]` placeholders in the
    description block any public/scheduled upload unless `--allow-placeholders`
    (a private review upload only warns).

Usage:
  python3 scripts/upload_video.py Video_01 --title "..." --dry-run   # validate, no network
  python3 scripts/upload_video.py Video_01 --title "..."             # upload PRIVATE
  python3 scripts/upload_video.py Video_01 --title "..." --publish-at 2026-10-01T13:00:00Z --allow-public
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import socket
import ssl
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from youtube_client import (  # noqa: E402  (local module, sys.path set above)
    YouTubeAuthError,
    build_data_service,
    load_credentials,
)

DEFAULT_VAULT = "~/Documents/3SK/outputs/BRANDS/3SK_Finance"

# YouTube hard limits (reject locally rather than eat a 400 mid-upload).
MAX_TITLE = 100
MAX_DESCRIPTION = 5000
MAX_TAGS_CHARS = 450  # API cap is ~500 incl. quoting overhead; stay under.

# Resumable-upload chunk + retry policy. 8 retries with capped exp-backoff covers
# transient 5xx / socket drops without hanging forever on a hard failure.
UPLOAD_CHUNK = 8 * 1024 * 1024  # 8 MiB
MAX_RETRIES = 8
RETRIABLE_STATUS = {500, 502, 503, 504}

# Description placeholders that must be resolved before a public/scheduled push.
PLACEHOLDER_RE = re.compile(r"\[(?:AFFILIATE LINK|WORKSHEET LINK|EMAIL SIGNUP LINK|LINK)\]")

# ~1,600 quota units per upload against the default ~10,000/day = ~6 uploads/day.
# There is NO public endpoint to query remaining quota, so we surface the static
# estimate rather than pretend to read it live.
UPLOAD_QUOTA_UNITS = 1600
DAILY_QUOTA_DEFAULT = 10000


def die(msg: str, code: int = 1) -> None:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(code)


def vault() -> Path:
    return Path(os.path.expanduser(os.environ.get("SK_VAULT", DEFAULT_VAULT))).resolve()


def normalize_id(raw: str) -> tuple[str, str]:
    m = re.search(r"(\d+)", raw)
    if not m:
        die(f"could not parse a video number from '{raw}'")
    nn = f"{int(m.group(1)):02d}"
    return f"Video_{nn}", nn


# --- description-pack parsing ----------------------------------------------

def _split_frontmatter(text: str) -> tuple[dict, str]:
    """Return (frontmatter_dict, body). Only flat `key: value` lines are parsed."""
    fm: dict[str, str] = {}
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            block = text[3:end]
            body = text[end + 4 :]
            for line in block.splitlines():
                if ":" in line and not line.lstrip().startswith("#"):
                    k, _, v = line.partition(":")
                    fm[k.strip()] = v.strip().strip("'\"")
    return fm, body


def _section(body: str, keyword: str) -> str | None:
    """Text under the first `## ...<keyword>...` heading, to the next `##`/`---`.

    Case-insensitive keyword match on the heading. Returns None if no such
    heading. A trailing horizontal rule (`---` on its own line) ends the section
    so we don't bleed into the next block.
    """
    headings = list(re.finditer(r"^##\s+(.+)$", body, re.MULTILINE))
    for i, h in enumerate(headings):
        if keyword.lower() in h.group(1).lower():
            start = h.end()
            end = headings[i + 1].start() if i + 1 < len(headings) else len(body)
            chunk = body[start:end]
            chunk = re.split(r"^---\s*$", chunk, maxsplit=1, flags=re.MULTILINE)[0]
            return chunk.strip()
    return None


def parse_desc_pack(path: Path) -> dict:
    """Extract {title?, description, tags[], pinned_comment?} from the pack."""
    if not path.is_file():
        die(f"description pack not found: {path}")
    fm, body = _split_frontmatter(path.read_text(encoding="utf-8"))
    description = _section(body, "Description")
    if not description:
        die(f"no '## Description' section found in {path.name}")
    tags_raw = _section(body, "Tags") or ""
    # The tags section may carry a leading note line; take comma-bearing lines.
    tag_line = " ".join(
        l for l in tags_raw.splitlines() if "," in l and not l.startswith(("#", ">", "-"))
    )
    tags = [t.strip() for t in tag_line.split(",") if t.strip()]
    return {
        "title": fm.get("youtube_title"),
        "description": description,
        "tags": tags,
        "pinned_comment": _section(body, "Pinned comment"),
    }


# --- input resolution ------------------------------------------------------

def resolve_thumbnail(vlt: Path, vid: str, override: str | None) -> Path | None:
    if override:
        p = Path(os.path.expanduser(override))
        if not p.is_file():
            die(f"--thumbnail not found: {p}")
        return p
    for cand in sorted(vlt.glob(f"Thumbnails/{vid}*.png")) + sorted(
        vlt.glob(f"Thumbnails/{vid}*.jpg")
    ):
        if cand.is_file():
            return cand
    return None


def suggest_title_from_packaging(vlt: Path, vid: str) -> str | None:
    """Best-effort: surface the first packaging title as a *suggestion only*.

    Never used as the actual title (packaging format isn't a stable contract) —
    printed in the no-title error so a human can copy it into --title.
    """
    for cand in sorted(vlt.glob(f"Packaging/Packaging_{vid}*.md")):
        for line in cand.read_text(encoding="utf-8").splitlines():
            m = re.search(r"^\s*(?:\d+[.)]|[-*])\s+[\"“]?(.+?)[\"”]?\s*$", line)
            if m and 15 <= len(m.group(1)) <= MAX_TITLE:
                return m.group(1)
    return None


# --- upload ----------------------------------------------------------------

def _resumable_upload(request, video_path: Path) -> dict:
    """Drive a resumable insert to completion with capped exp-backoff retries."""
    from googleapiclient.errors import HttpError

    response = None
    retries = 0  # per-chunk budget: reset after every chunk that doesn't raise,
                 # so a long upload isn't killed by transient blips spread across
                 # many different (individually-successful) chunks.
    while response is None:
        try:
            status, response = request.next_chunk()
            retries = 0
            if status:
                print(f"  … uploaded {int(status.progress() * 100)}%")
        except HttpError as exc:
            if exc.resp.status in RETRIABLE_STATUS:
                retries = _backoff(retries, f"HTTP {exc.resp.status}")
                continue
            die(f"upload failed (HTTP {exc.resp.status}): {exc}")
        except (ssl.SSLError, socket.error, ConnectionError, OSError, IOError) as exc:
            retries = _backoff(retries, f"{type(exc).__name__}: {exc}")
            continue
    print(f"  ✅ upload complete: {video_path.name}")
    return response


def _backoff(retries: int, why: str) -> int:
    retries += 1
    if retries > MAX_RETRIES:
        die(f"gave up after {MAX_RETRIES} retries (last: {why})")
    sleep = min(2 ** retries, 60) + random.uniform(0, 1)
    print(f"  ⚠ transient error ({why}); retry {retries}/{MAX_RETRIES} in {sleep:.1f}s")
    time.sleep(sleep)
    return retries


def set_captions(youtube, video_id: str, srt: Path) -> None:
    from googleapiclient.http import MediaFileUpload
    from googleapiclient.errors import HttpError

    try:
        youtube.captions().insert(
            part="snippet",
            body={
                "snippet": {
                    "videoId": video_id,
                    "language": "en",
                    "name": "English",
                    "isDraft": False,
                }
            },
            media_body=MediaFileUpload(str(srt), mimetype="application/octet-stream"),
        ).execute()
        print(f"  ✅ captions set from {srt.name}")
    except HttpError as exc:
        print(f"  ⚠ caption upload failed (video is up; add manually): {exc}", file=sys.stderr)


def set_thumbnail(youtube, video_id: str, thumb: Path) -> None:
    from googleapiclient.http import MediaFileUpload
    from googleapiclient.errors import HttpError

    mime = "image/png" if thumb.suffix.lower() == ".png" else "image/jpeg"
    try:
        youtube.thumbnails().set(
            videoId=video_id, media_body=MediaFileUpload(str(thumb), mimetype=mime)
        ).execute()
        print(f"  ✅ thumbnail set from {thumb.name}")
    except HttpError as exc:
        print(f"  ⚠ thumbnail set failed (video is up; add manually): {exc}", file=sys.stderr)


def write_receipt(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# --- CLI -------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="3SK YouTube uploader (Build 3).")
    p.add_argument("video", help="Video id, e.g. Video_01 or 01.")
    p.add_argument("--title", help="Video title (overrides desc-pack frontmatter).")
    p.add_argument("--video-file", help="Override the mp4 path (vault-relative or absolute).")
    p.add_argument("--captions", help="Override the .srt path.")
    p.add_argument("--desc", help="Override the description-pack .md path.")
    p.add_argument("--thumbnail", help="Override the thumbnail image path.")
    p.add_argument("--privacy", choices=["private", "unlisted", "public"],
                   default="private", help="Privacy status (default: private).")
    p.add_argument("--publish-at", help="ISO8601 UTC scheduled publish time "
                   "(e.g. 2026-10-01T13:00:00Z). Implies a future PUBLIC release.")
    p.add_argument("--category", default="27",
                   help="YouTube categoryId (default 27 = Education).")
    p.add_argument("--allow-public", action="store_true",
                   help="Required to upload public OR schedule a publish (the review-gate override).")
    p.add_argument("--allow-placeholders", action="store_true",
                   help="Permit unresolved [AFFILIATE LINK]/[WORKSHEET LINK] in a public/scheduled upload.")
    p.add_argument("--no-captions", action="store_true", help="Skip caption upload.")
    p.add_argument("--no-thumbnail", action="store_true", help="Skip thumbnail set.")
    p.add_argument("--token", help="Override path to youtube_token.json.")
    p.add_argument("--dry-run", action="store_true",
                   help="Validate inputs + metadata and print the plan; touch no network.")
    return p.parse_args()


def _resolve_path(flag_val: str | None, vlt: Path, default_rel: str) -> Path:
    if flag_val:
        p = Path(os.path.expanduser(flag_val))
        return p if p.is_absolute() else (vlt / flag_val)
    return vlt / default_rel


def main() -> None:
    args = parse_args()
    vid, nn = normalize_id(args.video)
    vlt = vault()

    video_file = _resolve_path(args.video_file, vlt, f"Footage_and_Edits/{vid}_v2.mp4")
    srt = _resolve_path(args.captions, vlt, f"Footage_and_Edits/{vid}_v2.srt")
    desc_pack = _resolve_path(args.desc, vlt, f"Video_Descriptions/{vid}_Description.md")
    receipt = vlt / "Production_Kits" / f"{vid}_youtube_upload.json"

    if not video_file.is_file():
        die(f"video file not found: {video_file}")

    meta = parse_desc_pack(desc_pack)
    title = args.title or meta["title"]
    if not title:
        sug = suggest_title_from_packaging(vlt, vid)
        hint = f" Suggestion from packaging: {sug!r}." if sug else ""
        die(f"no title — pass --title or add `youtube_title:` to {desc_pack.name}.{hint}")
    title = title.strip()

    # --- local validation (limits + guardrails) ---
    if len(title) > MAX_TITLE:
        die(f"title is {len(title)} chars (max {MAX_TITLE}).")
    if len(meta["description"]) > MAX_DESCRIPTION:
        die(f"description is {len(meta['description'])} chars (max {MAX_DESCRIPTION}).")
    tags = meta["tags"]
    while sum(len(t) + 1 for t in tags) > MAX_TAGS_CHARS and tags:
        dropped = tags.pop()
        print(f"  ⚠ dropping tag to stay under {MAX_TAGS_CHARS} chars: {dropped!r}")

    publish_at = args.publish_at
    if publish_at:
        try:
            pa_dt = datetime.fromisoformat(publish_at.replace("Z", "+00:00"))
        except ValueError:
            die(f"--publish-at not ISO8601: {publish_at!r} (e.g. 2026-10-01T13:00:00Z).")
        if pa_dt.tzinfo is None:  # naive input -> interpret as UTC
            pa_dt = pa_dt.replace(tzinfo=timezone.utc)
        if pa_dt <= datetime.now(timezone.utc):
            die(f"--publish-at is in the past ({publish_at}); a scheduled publish "
                "must be a future time, else the video goes public immediately.")

    going_public = args.privacy == "public" or bool(publish_at)
    if going_public and not args.allow_public:
        die("refusing to upload public/scheduled without --allow-public "
            "(the roadmap review-gate guardrail).")

    has_placeholders = bool(PLACEHOLDER_RE.search(meta["description"]))
    if has_placeholders:
        if going_public and not args.allow_placeholders:
            die("description still has unresolved [AFFILIATE LINK]/[WORKSHEET LINK] "
                "placeholders — resolve them or pass --allow-placeholders for a "
                "public/scheduled upload.")
        print("  ⚠ description contains unresolved link placeholders (ok for a "
              "private review upload; fix before public).")

    thumb = None if args.no_thumbnail else resolve_thumbnail(vlt, vid, args.thumbnail)
    have_srt = srt.is_file()

    # --- plan ---
    print(f"video      : {vid}")
    print(f"file       : {video_file}  ({video_file.stat().st_size / 1e6:.1f} MB)")
    print(f"title      : {title}")
    print(f"privacy    : {args.privacy}" + (f"  → publishAt {publish_at}" if publish_at else ""))
    print(f"category   : {args.category}")
    print(f"tags       : {len(tags)} ({sum(len(t) + 1 for t in tags)} chars)")
    print(f"captions   : {srt if have_srt else '(none — ' + srt.name + ' missing)'}"
          + (" [skipped]" if args.no_captions else ""))
    print(f"thumbnail  : {thumb if thumb else '(none found)'}"
          + (" [skipped]" if args.no_thumbnail else ""))
    print(f"receipt    : {receipt}")
    print(f"quota est. : ~{UPLOAD_QUOTA_UNITS} units (~{DAILY_QUOTA_DEFAULT // UPLOAD_QUOTA_UNITS} "
          f"uploads/day on the default {DAILY_QUOTA_DEFAULT}/day; no live quota API exists)")

    if args.dry_run:
        print("\n--- DRY RUN (no network, nothing uploaded). Drop --dry-run to upload. ---")
        return

    # --- upload ---
    try:
        creds = load_credentials(args.token)
    except YouTubeAuthError as exc:
        die(str(exc), code=2)
    youtube = build_data_service(creds)

    from googleapiclient.http import MediaFileUpload

    status_part: dict = {
        "privacyStatus": "private" if publish_at else args.privacy,
        "selfDeclaredMadeForKids": False,
    }
    if publish_at:
        # Scheduled publish: status must be private + publishAt; it flips public
        # automatically at that time. (Validated ISO8601 above.)
        status_part["publishAt"] = publish_at.replace("Z", "+00:00")
    body = {
        "snippet": {
            "title": title,
            "description": meta["description"],
            "tags": tags,
            "categoryId": str(args.category),
        },
        "status": status_part,
    }
    media = MediaFileUpload(str(video_file), chunksize=UPLOAD_CHUNK, resumable=True,
                            mimetype="video/*")
    print("\n>>> uploading (resumable)…")
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = _resumable_upload(request, video_file)
    video_id = response["id"]
    url = f"https://youtu.be/{video_id}"
    print(f"  videoId: {video_id}  →  {url}")

    if have_srt and not args.no_captions:
        set_captions(youtube, video_id, srt)
    elif not have_srt:
        print(f"  ⚠ no captions ({srt.name} missing) — add manually.")
    if thumb and not args.no_thumbnail:
        set_thumbnail(youtube, video_id, thumb)

    write_receipt(receipt, {
        "video": vid,
        "video_id": video_id,
        "url": url,
        "title": title,
        "privacy": status_part["privacyStatus"],
        "publish_at": status_part.get("publishAt"),
        "category_id": str(args.category),
        "tags": tags,
        "captions_set": have_srt and not args.no_captions,
        "thumbnail_set": bool(thumb) and not args.no_thumbnail,
        "pinned_comment": meta.get("pinned_comment"),
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
        "source_file": str(video_file),
    })
    print(f"\n✅ receipt → {receipt}")
    if meta.get("pinned_comment"):
        print("  ℹ pinned-comment text saved to the receipt — pin it manually in "
              "Studio (the Data API can't pin comments).")
    if args.privacy == "private" and not publish_at:
        print("  ℹ uploaded PRIVATE — review in Studio, then publish (the review gate).")


if __name__ == "__main__":
    main()
