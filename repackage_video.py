#!/usr/bin/env python3
"""Apply a flop-recovery repackage (new title + description + tags + custom
thumbnail) to ALREADY-PUBLISHED 3SK Finance videos, from a manifest.

Safe by construction:
  * Reads the live snippet first and preserves categoryId / language fields, so
    the videos().update() never drops a required field or flips a setting we did
    not intend to touch. We update part="snippet" ONLY — status (privacy, kids
    flag, schedule) is never sent, so it cannot be changed.
  * Description + tags are EXTRACTED from the canonical reviewed .md by section
    header, so what ships is exactly what passed review (no retyping).
  * --dry-run (default) shows the before -> after diff and writes NOTHING.
    You must pass --apply to perform the live writes.

Usage:
  python3 repackage_video.py --manifest repackage_manifest.json            # dry-run
  python3 repackage_video.py --manifest repackage_manifest.json --apply    # live
  python3 repackage_video.py --manifest repackage_manifest.json --only V3  # one video
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO / "scripts"))

MAX_TITLE = 100
MAX_DESCRIPTION = 5000
MAX_TAGS_CHARS = 450  # API list cap is ~500 incl. quoting overhead; stay under.

DESC_HEADER = "## Description (paste into YouTube Studio description field)"
TAGS_HEADER = "## Tags (paste into YouTube Studio tags field, comma-separated)"


def _section(md_text: str, header: str) -> str:
    """Return the body under `header`, up to the next standalone '---' fence.

    Raises if the header is absent so a renamed section fails LOUD instead of
    silently shipping an empty field to a live video.
    """
    lines = md_text.splitlines()
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == header)
    except StopIteration:
        raise SystemExit(f"FATAL: section header not found in md: {header!r}")
    body: list[str] = []
    for ln in lines[start + 1 :]:
        if ln.strip() == "---":
            break
        body.append(ln)
    return "\n".join(body).strip()


def extract(desc_md: Path) -> tuple[str, list[str]]:
    text = desc_md.read_text(encoding="utf-8")
    description = _section(text, DESC_HEADER)
    tags_raw = _section(text, TAGS_HEADER)
    tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
    return description, tags


def _cap_tags(tags: list[str]) -> list[str]:
    """Trim from the tail until the YouTube tags-field char budget is met."""
    kept: list[str] = []
    total = 0
    for t in tags:
        # API counts ~len(tag)+2 for a quoted tag containing a space, +1 separator.
        cost = len(t) + (2 if " " in t else 0) + 1
        if total + cost > MAX_TAGS_CHARS:
            print(f"  ⚠ tags over {MAX_TAGS_CHARS}-char budget; dropping tail "
                  f"starting at {t!r}")
            break
        kept.append(t)
        total += cost
    return kept


def _validate(label: str, title: str, description: str, tags_in: list[str],
              tags_out: list[str], thumb: Path) -> None:
    if not title or len(title) > MAX_TITLE:
        raise SystemExit(f"FATAL {label}: title length {len(title)} (cap {MAX_TITLE})")
    if not description or len(description) > MAX_DESCRIPTION:
        raise SystemExit(
            f"FATAL {label}: description length {len(description)} (cap {MAX_DESCRIPTION})")
    # Never let a budget-trim wipe a video that HAD tags — that's the field-drop
    # bug this script exists to prevent. Fail loud instead of shipping tags: [].
    if tags_in and not tags_out:
        raise SystemExit(f"FATAL {label}: tag trim emptied a non-empty tag list")
    if not thumb.is_file():
        raise SystemExit(f"FATAL {label}: thumbnail not found: {thumb}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--apply", action="store_true",
                    help="perform the live writes (default is dry-run)")
    ap.add_argument("--only", help="process only this label (e.g. V3)")
    ap.add_argument("--token", help="override YOUTUBE_TOKEN path")
    args = ap.parse_args()

    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    videos = manifest["videos"]
    if args.only:
        videos = [v for v in videos if v["label"] == args.only]
        if not videos:
            raise SystemExit(f"--only {args.only} matched no video in manifest")

    # Pre-extract + validate EVERYTHING before touching the API, so a bad field
    # aborts the whole run before any live video is mutated.
    plans = []
    for v in videos:
        desc_md = Path(v["desc_md"])
        thumb = Path(v["thumbnail"])
        description, tags_in = extract(desc_md)
        tags = _cap_tags(tags_in)
        _validate(v["label"], v["title"], description, tags_in, tags, thumb)
        plans.append({**v, "description": description, "tags": tags, "thumb": thumb})

    from youtube_client import load_credentials, build_data_service
    from upload_video import set_thumbnail

    creds = load_credentials(args.token)
    yt = build_data_service(creds)

    mode = "APPLY (LIVE WRITES)" if args.apply else "DRY-RUN (no writes)"
    print(f"\n=== repackage — {mode} — {len(plans)} video(s) ===\n")

    failures = 0
    for p in plans:
        vid = p["video_id"]
        print(f"--- {p['label']}  ({vid}) ---")

        # 1) Read the live snippet (needed for categoryId + before/after diff).
        resp = yt.videos().list(part="snippet", id=vid).execute()
        items = resp.get("items") or []
        if not items:
            print(f"  🔴 video id {vid} not found / not owned — SKIP")
            failures += 1
            continue
        live = items[0]["snippet"]

        print(f"  title:  {live.get('title')!r}")
        print(f"       -> {p['title']!r}")
        old_desc = (live.get("description") or "").splitlines()[:1]
        print(f"  desc1:  {(old_desc[0] if old_desc else '')!r}")
        print(f"       -> {p['description'].splitlines()[0]!r}")
        print(f"  tags:   {len(live.get('tags') or [])} -> {len(p['tags'])}")
        print(f"  thumb:  {p['thumb'].name}")

        if not args.apply:
            print("  (dry-run — no write)\n")
            continue

        # 2) Build the snippet body: preserve required/identity fields, override
        #    the three repackage fields. categoryId is REQUIRED by update().
        snippet = {
            "title": p["title"],
            "description": p["description"],
            "tags": p["tags"],
            "categoryId": live.get("categoryId") or "27",  # 27 = Education.
        }
        for k in ("defaultLanguage", "defaultAudioLanguage"):
            if live.get(k):
                snippet[k] = live[k]

        try:
            yt.videos().update(part="snippet", body={"id": vid, "snippet": snippet}).execute()
            print("  ✅ snippet updated (title + description + tags)")
        except Exception as exc:  # noqa: BLE001 — report + continue to next video.
            print(f"  🔴 snippet update FAILED: {type(exc).__name__}: {exc}")
            failures += 1
            continue

        # 3) Custom thumbnail (auto-shrinks >2MB, pages Steve on failure).
        if not set_thumbnail(yt, vid, p["thumb"]):
            failures += 1

        print(f"  → https://youtu.be/{vid}\n")

    if args.apply:
        print(f"=== done: {len(plans) - failures}/{len(plans)} clean, {failures} failure(s) ===")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
