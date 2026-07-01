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
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

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

# Custom-thumbnail limits. YouTube rejects a thumbnail over 2 MiB outright (the
# bug that shipped V3 with no thumbnail: a ~2.1 MB PNG → HttpError → quiet skip).
# Over the cap (or an unsupported format) we transparently ship a web-optimized
# JPEG copy instead of the master. 2048px wide is comfortably above YouTube's
# recommended 1280×720 while keeping the file small.
MAX_THUMBNAIL_BYTES = 2 * 1024 * 1024  # 2,097,152
THUMBNAIL_MAX_WIDTH = 2048
SUPPORTED_THUMBNAIL_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".bmp"}

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
        "do_not_publish_before": fm.get("do_not_publish_before"),
    }


# --- release-date gate -----------------------------------------------------
# Why: a public publish must never happen before its INTENDED release day. V2
# went public a day early because the only guardrail was --allow-public, a flag
# an automated agent can satisfy on its own. This gate keys off a per-video
# `do_not_publish_before:` date (description-pack frontmatter, receipt fallback)
# and HARD-FAILS — there is deliberately no override flag. To publish early you
# edit the date, an intentional human act.

ET = ZoneInfo("America/New_York")


def parse_release_gate(value: str, *, source: str) -> datetime:
    """Parse a do_not_publish_before value into a tz-aware datetime.

    'YYYY-MM-DD'  -> 00:00 America/New_York that day (the channel's tz).
    Full ISO8601  -> aware value respected; a naive value is read as ET.
    """
    v = str(value).strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            # strptime (not just the regex) validates the calendar date, so a
            # plausible typo like 2026-13-01 / 2026-02-30 is caught here too.
            return datetime.strptime(v, "%Y-%m-%d").replace(tzinfo=ET)
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        die(f"`do_not_publish_before` in {source} is not a valid date/ISO8601: "
            f"{value!r} (use YYYY-MM-DD or a full ISO8601 timestamp).")
    return dt.replace(tzinfo=ET) if dt.tzinfo is None else dt


def enforce_release_gate(*, going_public: bool, effective_moment: datetime,
                         gate_raw, gate_source: str, already_public: bool,
                         vid: str, desc_pack_name: str) -> None:
    """Block a public/scheduled publish that has no declared release date, or
    whose effective publish moment is before that date. No-op for non-public
    targets and for refreshing an already-public video."""
    if not going_public or already_public:
        return
    if not gate_raw:
        die(f"release-date gate: refusing to publish {vid} public — no "
            f"`do_not_publish_before` declared. Add `do_not_publish_before: "
            f"YYYY-MM-DD` to {desc_pack_name} frontmatter (the intended release "
            "day), then publish. No override flag by design.")
    gate_dt = parse_release_gate(gate_raw, source=gate_source)
    if effective_moment < gate_dt:
        die(f"release-date gate: {vid} is not publishable until {gate_raw} "
            f"(do_not_publish_before, from {gate_source}). Effective publish "
            f"moment is {effective_moment.astimezone(ET).isoformat()}. Wait until "
            "then, or change the date in the description pack. No override flag.")


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


def find_managed_caption_track(youtube, video_id: str, lang: str = "en",
                               name: str = "English") -> str | None:
    """Return the id of an existing OWNED caption track to replace, or None.

    captions.list only returns tracks this channel can manage. We skip ASR
    (auto-generated) tracks — they aren't updatable and aren't "ours" — and match
    the language so a re-run replaces our track instead of stacking a second one.
    Prefers an exact name match, else any non-ASR track in the language.
    """
    resp = youtube.captions().list(part="snippet", videoId=video_id).execute()
    base = lang.lower().split("-")[0]
    candidates = []
    for it in resp.get("items", []):
        sn = it.get("snippet", {})
        if (sn.get("trackKind") or "").lower() == "asr":
            continue
        if (sn.get("language") or "").lower().split("-")[0] != base:
            continue
        candidates.append(it)
    if not candidates:
        return None
    for it in candidates:
        if (it.get("snippet", {}).get("name") or "") == name:
            return it["id"]
    return candidates[0]["id"]


def upsert_captions(youtube, video_id: str, srt: Path, lang: str = "en",
                    name: str = "English") -> str:
    """Idempotently attach the timed SRT as the video's caption track.

    captions.list first → captions.update the existing owned track in place (no
    duplicate) or captions.insert a new one. sync=False: our SRT is already timed,
    so YouTube must NOT re-sync it. Returns "insert" or "update". Raises HttpError
    on an API failure and FileNotFoundError if the SRT is missing — callers decide
    whether that is fatal.
    """
    from googleapiclient.http import MediaFileUpload

    srt = Path(srt)
    if not srt.is_file():
        raise FileNotFoundError(f"caption file not found: {srt}")
    existing = find_managed_caption_track(youtube, video_id, lang=lang, name=name)
    media = MediaFileUpload(str(srt), mimetype="application/octet-stream")
    if existing:
        youtube.captions().update(
            part="snippet",
            body={"id": existing, "snippet": {"name": name, "isDraft": False}},
            media_body=media,
            sync=False,
        ).execute()
        return "update"
    youtube.captions().insert(
        part="snippet",
        body={
            "snippet": {
                "videoId": video_id,
                "language": lang,
                "name": name,
                "isDraft": False,
            }
        },
        media_body=media,
        sync=False,
    ).execute()
    return "insert"


def set_captions(youtube, video_id: str, srt: Path) -> bool:
    """Non-fatal idempotent caption attach for the upload/publish flows.

    A caption failure must NEVER sink a successful video upload/publish — including
    a transient network error from the captions.list/insert/update calls (which are
    NOT HttpError), so this catches broadly and only warns. Returns True iff the
    track was attached, so callers record the real result rather than assuming it.
    The standalone upload_captions.py wraps upsert_captions() directly and DOES
    surface errors with a non-zero exit.
    """
    try:
        action = upsert_captions(youtube, video_id, srt)
        verb = "inserted" if action == "insert" else "updated"
        print(f"  ✅ captions {verb} from {Path(srt).name}")
        return True
    except Exception as exc:  # best-effort side channel — never crash the run.
        print(f"  ⚠ caption upload failed ({type(exc).__name__}: {exc}); video is up, "
              "add captions manually.", file=sys.stderr)
        return False


def _notify(msg: str) -> None:
    """Best-effort LOUD alert to Steve's Telegram via scripts/notify.sh.

    Never raises and never blocks the upload — notify.sh is the canonical alert
    channel, but a missing/failed notifier must not sink a good upload.
    """
    notify = REPO / "scripts" / "notify.sh"
    if not notify.is_file():
        return
    try:
        subprocess.run([str(notify), msg], timeout=20, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _thumbnail_mime(suffix: str) -> str:
    s = suffix.lower()
    if s == ".png":
        return "image/png"
    if s == ".gif":
        return "image/gif"
    if s == ".bmp":
        return "image/bmp"
    return "image/jpeg"


def prepare_thumbnail_for_upload(thumb: Path) -> tuple[Path, str, bool]:
    """Return (path_to_upload, mimetype, is_temp) for a YouTube-uploadable thumb.

    If ``thumb`` is already a supported format under YouTube's 2 MiB cap it is
    uploaded verbatim → (thumb, mime, False). Otherwise (over the cap, or an
    unsupported format) a web-optimized JPEG copy is written to a temp file:
    converted to RGB, downscaled to ≤2048px wide (aspect preserved), and saved
    with quality stepped down until it fits under the cap. The master is never
    touched. ``is_temp`` tells the caller to delete the returned path afterward.

    Raises (PIL / OSError) only on a genuine image-processing failure; the caller
    catches broadly so a thumbnail problem never crashes a good upload.
    """
    suffix = thumb.suffix.lower()
    size = thumb.stat().st_size
    if suffix in SUPPORTED_THUMBNAIL_SUFFIXES and size <= MAX_THUMBNAIL_BYTES:
        return thumb, _thumbnail_mime(suffix), False

    reason = (f"{size / 1e6:.2f} MB over the 2 MB cap" if size > MAX_THUMBNAIL_BYTES
              else f"unsupported format '{suffix or thumb.name}'")

    from PIL import Image  # lazy: keep Pillow off the cold path.

    with Image.open(thumb) as src:
        img = src.convert("RGB")  # flatten alpha/palette → JPEG-safe.

    fd, tmp_name = tempfile.mkstemp(suffix=".jpg", prefix=f"{thumb.stem}_web_")
    os.close(fd)
    tmp = Path(tmp_name)

    # Own the temp file from here: if encoding raises (corrupt frame, disk full,
    # encoder error) we must delete it before propagating, since the caller only
    # learns is_temp=True via our return value — an exception skips that.
    try:
        width = min(img.width, THUMBNAIL_MAX_WIDTH)
        quality = 92
        while True:
            if width != img.width:
                height = max(1, round(img.height * (width / img.width)))
                frame = img.resize((width, height), Image.LANCZOS)
            else:
                frame = img
            frame.save(tmp, format="JPEG", quality=quality, optimize=True, progressive=True)
            out_size = tmp.stat().st_size
            if out_size <= MAX_THUMBNAIL_BYTES:
                print(f"  ℹ thumbnail downscaled ({reason}) → {width}px / q{quality} / "
                      f"{out_size / 1e6:.2f} MB (master untouched)")
                return tmp, "image/jpeg", True
            # Shrink quality first, then dimensions; guaranteed to terminate
            # because width strictly decreases once quality bottoms out.
            if quality > 70:
                quality -= 7
            elif width > 1024:
                width = int(width * 0.85)
                quality = 85
            else:
                print(f"  ⚠ thumbnail still {out_size / 1e6:.2f} MB after max "
                      f"compression — uploading the smallest copy anyway.",
                      file=sys.stderr)
                return tmp, "image/jpeg", True
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def set_thumbnail(youtube, video_id: str, thumb: Path) -> bool:
    """Non-fatal thumbnail set; returns True iff it succeeded.

    Auto-shrinks an over-cap / unsupported source to a web-optimized JPEG copy
    first (see prepare_thumbnail_for_upload) so an oversized PNG can no longer
    silently lose the thumbnail. A failure is LOUD — a missing custom thumbnail
    tanks CTR — so it both warns on stderr and pages Steve's Telegram, but still
    never crashes a successful upload/publish (a transient network error here is
    NOT HttpError, so catch broadly).
    """
    from googleapiclient.http import MediaFileUpload

    upload_path = thumb
    is_temp = False
    try:
        upload_path, mime, is_temp = prepare_thumbnail_for_upload(thumb)
        youtube.thumbnails().set(
            videoId=video_id, media_body=MediaFileUpload(str(upload_path), mimetype=mime)
        ).execute()
        print(f"  ✅ thumbnail set from {upload_path.name}")
        return True
    except Exception as exc:  # best-effort side channel — never crash the run.
        msg = (f"🔴 thumbnail FAILED for {video_id} ({type(exc).__name__}: {exc}) — "
               f"video is LIVE with NO custom thumbnail (tanks CTR). Set it manually "
               f"in Studio. Source: {thumb}")
        print(f"  ⚠ {msg}", file=sys.stderr)
        _notify(msg)
        return False
    finally:
        if is_temp:
            try:
                upload_path.unlink(missing_ok=True)
            except OSError:
                pass


# --- caption sweep (verify-and-backfill across all uploaded videos) ---------

def _is_transient_api_error(exc: Exception) -> bool:
    """True for a blip worth waiting out — a network/SSL/timeout error, or an HTTP
    5xx/429 — versus a HARD error (quota 403, bad scope 401/403, 404, config) that
    needs Steve's attention. The daily sweep pages only on hard errors; a transient
    one is logged and left for the next sweep (or the inline post-upload attach) to
    heal, so a one-off hiccup never red-alerts an unattended run."""
    from googleapiclient.errors import HttpError  # lazy: keep import cost off cold paths
    if isinstance(exc, HttpError):
        status = getattr(exc.resp, "status", None)
        return status in RETRIABLE_STATUS or status == 429
    # ConnectionError, socket.timeout, ssl.SSLError, TimeoutError all subclass OSError.
    return isinstance(exc, OSError)


def _caption_error_bucket(exc: Exception) -> str:
    """Bucket a caption API failure: 'gone' (HTTP 404 — the video no longer exists,
    e.g. deleted or a stale receipt id; captioning is impossible and NEVER becomes
    possible, so it must not page every run), 'transient' (network/5xx/429 — heals on
    the next sweep), or 'errors' (a real hard error that needs Steve). Only 'errors'
    drives the non-zero exit / Telegram alert."""
    from googleapiclient.errors import HttpError
    if isinstance(exc, HttpError) and getattr(exc.resp, "status", None) == 404:
        return "gone"
    return "transient" if _is_transient_api_error(exc) else "errors"


def iter_upload_receipts(vlt: Path):
    """Yield (video_label, video_id, receipt_path, receipt_dict) for every upload
    receipt that carries a YouTube video_id. These receipts are the canonical map
    of the videos WE uploaded (and therefore can caption — each maps to a known
    SRT). A malformed receipt is skipped, not fatal."""
    for rp in sorted((vlt / "Production_Kits").glob("*_youtube_upload.json")):
        try:
            data = json.loads(rp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        video_id = data.get("video_id")
        if not video_id:
            continue
        label = data.get("video") or rp.stem.replace("_youtube_upload", "")
        yield label, video_id, rp, data


def sweep_captions(youtube, vlt: Path, *, force: bool = False,
                   only: str | None = None, dry_run: bool = False) -> dict:
    """Verify the English caption track on every uploaded video; add where missing.

    For each upload receipt: captions.list (cheap). If our managed English track is
    already present → SKIP (no re-upload) unless force=True (then update in place).
    If absent → insert the timed SRT. This is the safety net behind the inline
    attach: a brand-new upload can still be processing when its inline caption
    insert runs, so it may miss; the next sweep catches the straggler while skipping
    every video that's already fine. Per-video best-effort — one error never aborts
    the sweep. Errors are split: a HARD error (quota/scope/4xx) counts toward
    ``errors`` (drives a non-zero exit → Telegram alert on the scheduled job); a
    transient network/5xx/429 blip counts toward ``transient`` only (logged, left
    for the next sweep to heal) so an unattended run never pages on a hiccup.
    Returns a summary dict.
    """
    summary = {"checked": 0, "added": 0, "updated": 0, "skipped": 0,
               "no_srt": 0, "gone": 0, "transient": 0, "errors": 0}
    only_label = normalize_id(only)[0] if only else None
    for label, video_id, rp, data in iter_upload_receipts(vlt):
        if only_label and label != only_label:
            continue
        summary["checked"] += 1
        srt = vlt / "Footage_and_Edits" / f"{label}_v2.srt"
        try:
            existing = find_managed_caption_track(youtube, video_id)
        except Exception as exc:  # API/network failure — skip this one, keep sweeping.
            kind = _caption_error_bucket(exc)
            tag = {"transient": "transient ", "gone": "gone-404 "}.get(kind, "")
            print(f"  ⚠ {label} ({video_id}): caption check failed "
                  f"({tag}{type(exc).__name__}: {exc})", file=sys.stderr)
            summary[kind] += 1
            continue
        if existing and not force:
            print(f"  ✓ {label} ({video_id}): captions present — skip")
            summary["skipped"] += 1
            continue
        if not srt.is_file():
            print(f"  ⚠ {label} ({video_id}): no SRT at {srt.name} — can't add captions",
                  file=sys.stderr)
            summary["no_srt"] += 1
            continue
        if dry_run:
            verb = "update" if existing else "add"
            print(f"  • {label} ({video_id}): would {verb} captions from {srt.name}")
            summary["updated" if existing else "added"] += 1
            continue
        try:
            action = upsert_captions(youtube, video_id, srt)
        except Exception as exc:
            kind = _caption_error_bucket(exc)
            tag = {"transient": "transient ", "gone": "gone-404 "}.get(kind, "")
            print(f"  ⚠ {label} ({video_id}): caption upload failed "
                  f"({tag}{type(exc).__name__}: {exc})", file=sys.stderr)
            summary[kind] += 1
            continue
        verb = "updated" if action == "update" else "added"
        print(f"  ✅ {label} ({video_id}): captions {verb} from {srt.name}")
        summary["updated" if action == "update" else "added"] += 1
        data.update({
            "captions_set": True,
            "captions_action": action,
            "captions_source": str(srt),
            "captions_updated_at": datetime.now(timezone.utc).isoformat(),
        })
        try:
            write_receipt(rp, data)
        except OSError:
            pass  # receipt is a convenience stamp; never fail the sweep over it.
    return summary


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
    p.add_argument("--no-caption-sweep", action="store_true",
                   help="Skip the post-upload verify-and-backfill caption sweep over all videos.")
    p.add_argument("--no-thumbnail", action="store_true", help="Skip thumbnail set.")
    p.add_argument("--skip-preflight", action="store_true",
                   help="Skip the deterministic pre-upload signoff (cut freshness + completeness). "
                        "Escape hatch only — the gate exists because V6 shipped a stale-signoff cut.")
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

    enforce_release_gate(
        going_public=going_public,
        effective_moment=(pa_dt if publish_at else datetime.now(timezone.utc)),
        gate_raw=meta.get("do_not_publish_before"),
        gate_source=desc_pack.name,
        already_public=False,  # insert path: the video is not on the channel yet.
        vid=vid,
        desc_pack_name=desc_pack.name,
    )

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

    # --- pre-upload signoff gate (deterministic; fail-closed) ---
    # V6 shipped with a PRE-render signoff: images were regenerated and the cut
    # re-assembled after the last human GO, and nothing re-checked the final video.
    # This gate verifies the cut is COMPLETE + FRESH before any network call. Runs
    # in --dry-run too, so a bad cut is caught without touching YouTube. No agent can
    # watch the mp4 — playback review stays the human step (the private upload).
    if not args.skip_preflight:
        from preflight_publish import check_publish_ready, write_signoff
        ok, failures, warnings = check_publish_ready(
            vlt, vid, video_file=video_file, desc_pack=desc_pack,
            thumb=thumb, srt=srt if have_srt else None,
        )
        for w in warnings:
            print(f"  ⚠ preflight: {w}")
        if not ok:
            die("pre-upload signoff FAILED — the cut is not ready:\n  - "
                + "\n  - ".join(failures)
                + "\nFix the above and re-assemble, or pass --skip-preflight to override.")
        if args.dry_run:
            print("  ✅ preflight: cut is fresh + complete (dry-run — stamp written on real upload)")
        else:
            stamp = write_signoff(vlt, vid, video_file, thumb, srt if have_srt else None)
            print(f"  ✅ preflight signoff: cut is fresh + complete → {stamp.name}")

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
        # ponytail: every 3SK video is AI-animated + TTS, so the "altered or synthetic
        # media" disclosure is always required. Set at INSERT — videos.update accepts it
        # (response echoes True) but videos.list never reads it back, so insert-time is
        # the clean place. Add a CLI flag only if a non-synthetic upload ever ships here.
        "containsSyntheticMedia": True,
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
    # The insert response is the ONLY surface that reflects containsSyntheticMedia
    # (videos.list never returns it), so confirm the disclosure stuck here and warn loudly
    # if it didn't — a missing A/S disclosure is a policy risk on an AI-generated channel.
    synthetic_ok = response.get("status", {}).get("containsSyntheticMedia") is True
    if not synthetic_ok:
        print("  ⚠ altered/synthetic-media disclosure NOT confirmed in the insert response — "
              "set it manually in Studio (Editing → 'Altered or synthetic content').", file=sys.stderr)

    # Persist the receipt NOW — the video is live and its id is the only thing a
    # re-run needs to avoid a duplicate upload. The caption/thumbnail steps below
    # are best-effort side channels; writing the receipt first means a hiccup in
    # them can never cost us the video_id. We rewrite with their real results after.
    receipt_data = {
        "video": vid,
        "video_id": video_id,
        "url": url,
        "title": title,
        "privacy": status_part["privacyStatus"],
        "publish_at": status_part.get("publishAt"),
        "contains_synthetic_media": synthetic_ok,
        "category_id": str(args.category),
        "tags": tags,
        "captions_set": False,
        "thumbnail_set": False,
        "pinned_comment": meta.get("pinned_comment"),
        "do_not_publish_before": meta.get("do_not_publish_before"),
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
        "source_file": str(video_file),
    }
    write_receipt(receipt, receipt_data)

    if have_srt and not args.no_captions:
        receipt_data["captions_set"] = set_captions(youtube, video_id, srt)
    elif not have_srt:
        print(f"  ⚠ no captions ({srt.name} missing) — add manually.")
    if thumb and not args.no_thumbnail:
        receipt_data["thumbnail_set"] = set_thumbnail(youtube, video_id, thumb)

    write_receipt(receipt, receipt_data)
    print(f"\n✅ receipt → {receipt}")
    if meta.get("pinned_comment"):
        print("  ℹ pinned-comment text saved to the receipt — pin it manually in "
              "Studio (the Data API can't pin comments).")
    if args.privacy == "private" and not publish_at:
        print("  ℹ uploaded PRIVATE — review in Studio, then publish (the review gate).")

    # Post-upload safety net: sweep EVERY uploaded video and fill any missing
    # caption track (skip the ones already captioned — including the one we just
    # attached inline). This heals a straggler whose inline insert failed because
    # the video was still processing. Best-effort — a sweep hiccup never affects
    # this upload's success. Opt out with --no-caption-sweep.
    if not args.no_caption_sweep:
        print("\n>>> verifying captions across all uploaded videos…")
        try:
            s = sweep_captions(youtube, vlt)
            print(f"  caption sweep: {s['checked']} checked · {s['added']} added · "
                  f"{s['updated']} updated · {s['skipped']} present · "
                  f"{s['no_srt']} missing-srt · {s['gone']} gone-404 · "
                  f"{s['transient']} transient · {s['errors']} errors")
        except Exception as exc:  # never let the sweep sink a good upload.
            print(f"  ⚠ caption sweep skipped ({type(exc).__name__}: {exc})",
                  file=sys.stderr)


if __name__ == "__main__":
    main()
