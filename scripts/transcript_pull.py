#!/usr/bin/env python3
"""Niche transcript feed — caption hooks for the youtube-researcher agent.

The content companion to ``niche_pull.py``. Where that script gives the agent the
niche's REAL most-viewed videos (titles + view counts from the Data API), this one
gives it the actual WORDS those videos open with — the verbatim cold-open hook —
so ``hook_patterns.md`` and ``viral_teardowns.md`` describe what creators really
said in the first 15 seconds instead of inferring a hook from the title.

It reads the latest ``Niche_Views/<date>_niche-views.md`` (the niche_pull feed),
extracts the top-N video ids, fetches each video's public caption track, and
writes one compact markdown file: per video, the first ~150 words VERBATIM (the
hook) plus the closing ~25 words (the CTA/outro) and a total word count. No middle
is invented — only the verbatim head + tail are kept, so the feed stays lean for
the agent's limited context and nothing is fabricated.

Two backends, tried in order (the "+ API fallback" design Steve picked):
  1. youtube-transcript-api — fast, pure-Python timedtext fetch.
  2. yt-dlp — slower but far more resilient to YouTube's anti-scraping; reads the
     json3 auto-caption track from the info dict when backend 1 fails.
A video where BOTH backends fail is recorded as "(no transcript)" — never guessed.

These are PUBLIC auto-captions fetched via the unofficial timedtext path (the
official captions.download is owner-only). We only ever pull the handful of videos
already in the niche feed, never crawl, to stay polite on rate limits.

This script does NO analysis — it is the deterministic data feed. The
``youtube-research`` routine runs it right after ``niche_pull.py``, then hands both
files to ``youtube-researcher``.

Output: Channel_Intelligence/Niche_Views/<YYYY-MM-DD>_transcripts.md (under $SK_VAULT).
Frontmatter ``status:`` follows the agent status contract so a no-captions run is a
clean skeleton, never a fake success.

Usage:
  python3 scripts/transcript_pull.py --dry-run     # show plan, no network
  python3 scripts/transcript_pull.py               # latest niche feed, top 10
  python3 scripts/transcript_pull.py --top 6       # only the top 6 by views
  python3 scripts/transcript_pull.py --input <feed.md> --out <transcripts.md>
  python3 scripts/transcript_pull.py --hook-words 200 --tail-words 30
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from datetime import date
from pathlib import Path

DEFAULT_VAULT = "~/Documents/3SK/outputs/BRANDS/3SK_Finance"
NICHE_SUBDIR = "Channel_Intelligence/Niche_Views"

DEFAULT_TOP = 10          # how many of the feed's top-by-views videos to fetch.
HOOK_WORDS_DEFAULT = 150  # verbatim words kept from the start (the cold open).
TAIL_WORDS_DEFAULT = 25   # verbatim words kept from the end (the CTA/outro).
FETCH_PAUSE_SECONDS = 1.0 # polite gap between videos to avoid rate-limiting.

# Matches a niche-feed table row's title link + 11-char video id, e.g.
#   [Some Title](https://www.youtube.com/watch?v=jVcoCvJqfE0)
_ROW_RE = re.compile(
    r"\[(?P<title>.*?)\]\(https://www\.youtube\.com/watch\?v=(?P<vid>[A-Za-z0-9_-]{11})\)"
)


def die(msg: str, code: int = 1) -> None:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(code)


def vault() -> Path:
    return Path(os.path.expanduser(os.environ.get("SK_VAULT", DEFAULT_VAULT))).resolve()


def latest_feed(niche_dir: Path) -> Path | None:
    """Newest <date>_niche-views.md by name (date-prefixed sorts chronologically)."""
    feeds = sorted(niche_dir.glob("*_niche-views.md"))
    return feeds[-1] if feeds else None


def parse_feed(feed_path: Path, top: int) -> list[dict]:
    """Top-N [{vid, title}] from a niche feed, in file order (already views-sorted).

    De-dupes on video id so a video that appears under two sources is fetched once.
    """
    text = feed_path.read_text(encoding="utf-8")
    out: list[dict] = []
    seen: set[str] = set()
    for m in _ROW_RE.finditer(text):
        vid = m.group("vid")
        if vid in seen:
            continue
        seen.add(vid)
        # Undo the feed's markdown-cell pipe escaping for a clean display title.
        title = m.group("title").replace("\\|", "|").strip()
        out.append({"vid": vid, "title": title})
        if len(out) >= top:
            break
    return out


# --- transcript backends ---------------------------------------------------

def _words_from_transcript_api(vid: str) -> list[str] | None:
    """Backend 1: youtube-transcript-api. None on any failure (caller falls back)."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        return None
    try:
        fetched = YouTubeTranscriptApi().fetch(vid, languages=["en", "en-US", "en-GB"])
        text = " ".join(snip.text for snip in fetched if snip.text)
        words = text.split()
        return words or None
    except Exception:  # noqa: BLE001 — disabled/none/unavailable/region: all → fall back.
        return None


class _NullLogger:
    """Swallows yt-dlp's own logging so a handled 'Video unavailable' doesn't
    print an ERROR line to stderr that run_claude_job.sh would surface to Telegram
    as a false failure. The video still degrades to (no transcript) cleanly."""

    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


def _words_from_ytdlp(vid: str) -> list[str] | None:
    """Backend 2: yt-dlp json3 auto-captions. None on any failure."""
    try:
        from yt_dlp import YoutubeDL
    except ImportError:
        return None
    try:
        # socket_timeout bounds extract_info's network metadata fetch — without it
        # a stalled YouTube connection hangs the unattended 2am job forever (the
        # routine's best-effort guard only catches non-zero exits, not a hang).
        # ignoreerrors + the null logger keep a legitimately-unavailable video from
        # printing an ERROR line to stderr that would masquerade as a job failure.
        opts = {"quiet": True, "skip_download": True, "no_warnings": True,
                "socket_timeout": 30, "ignoreerrors": True, "logger": _NullLogger()}
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(
                f"https://www.youtube.com/watch?v={vid}", download=False
            )
        if not info:  # ignoreerrors=True returns None on an unavailable video.
            return None
        tracks = (info.get("subtitles") or {})
        autos = (info.get("automatic_captions") or {})
        track = None
        for lang in ("en", "en-US", "en-GB"):
            track = tracks.get(lang) or autos.get(lang)
            if track:
                break
        if not track:
            return None
        pick = next((t for t in track if t.get("ext") == "json3"), track[0])
        raw = urllib.request.urlopen(pick["url"], timeout=30).read()
        events = json.loads(raw).get("events", [])
        segs = [seg.get("utf8", "") for ev in events for seg in (ev.get("segs") or [])]
        words = "".join(segs).split()
        return words or None
    except Exception:  # noqa: BLE001
        return None


def fetch_words(vid: str) -> tuple[list[str] | None, str]:
    """(words, backend_label). words is None when both backends fail."""
    words = _words_from_transcript_api(vid)
    if words:
        return words, "transcript-api"
    words = _words_from_ytdlp(vid)
    if words:
        return words, "yt-dlp"
    return None, "none"


# --- formatting ------------------------------------------------------------

def _flat(s: str) -> str:
    return str(s).replace("\n", " ").replace("\r", " ").strip()


def write_report(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, path)


def skeleton(path: Path, status: str, note: str) -> None:
    write_report(path, (
        "---\n"
        f"date: {date.today().isoformat()}\n"
        "type: niche-transcripts\n"
        f"status: {status}\n"
        "source: youtube-captions\n"
        "maintained-by: transcript_pull.py\n"
        "tags:\n  - brand/3sk-finance\n  - niche/transcripts\n"
        "---\n\n"
        "# 3SK Finance — Niche transcript feed (caption hooks)\n\n"
        f"{note}\n"
    ))


def build_body(entries: list[dict], feed_name: str, hook_words: int,
               tail_words: int, ok_count: int) -> str:
    lines = [
        "---",
        f"date: {date.today().isoformat()}",
        "type: niche-transcripts",
        "status: ok" if ok_count == len(entries) else f"status: partial-{len(entries) - ok_count}-missing",
        "source: youtube-captions",
        "maintained-by: transcript_pull.py",
        "tags:",
        "  - brand/3sk-finance",
        "  - niche/transcripts",
        "---",
        "",
        "# 3SK Finance — Niche transcript feed (caption hooks)",
        "",
        f"Verbatim caption HOOKS for the top videos in `{feed_name}` (the niche_pull "
        f"feed). Per video: the first ~{hook_words} words (the cold open) + the closing "
        f"~{tail_words} words (CTA/outro), pulled from public YouTube captions. The "
        "middle is omitted — only the verbatim head + tail are kept, nothing invented. "
        "Use these for **youtube-researcher**'s `hook_patterns.md` and "
        "`viral_teardowns.md`: quote what creators ACTUALLY opened with instead of "
        "inferring a hook from the title. `(no transcript)` = captions unavailable for "
        "that video — never a guessed hook.",
        "",
    ]
    for i, e in enumerate(entries, 1):
        lines.append(f"## {i}. {_flat(e['title'])}")
        lines.append(f"`https://www.youtube.com/watch?v={e['vid']}`")
        if e.get("words"):
            words = e["words"]
            total = len(words)
            hook = " ".join(words[:hook_words])
            lines.append(f"_{total:,} caption words · via {e['backend']}_")
            lines.append("")
            lines.append(f"**Hook:** {hook}")
            if total > hook_words + tail_words:
                tail = " ".join(words[-tail_words:])
                lines.append("")
                lines.append(f"**Closing:** …{tail}")
        else:
            lines.append("_(no transcript — captions unavailable)_")
        lines.append("")
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="3SK YouTube niche transcript feed.")
    p.add_argument("--input", help="Path to a niche_pull feed .md (default: newest in Niche_Views/).")
    p.add_argument("--out", help="Override output file path.")
    p.add_argument("--top", type=int, default=DEFAULT_TOP,
                   help=f"How many top-by-views videos to fetch (default {DEFAULT_TOP}).")
    p.add_argument("--hook-words", type=int, default=HOOK_WORDS_DEFAULT,
                   help=f"Verbatim words kept from the start (default {HOOK_WORDS_DEFAULT}).")
    p.add_argument("--tail-words", type=int, default=TAIL_WORDS_DEFAULT,
                   help=f"Verbatim words kept from the end (default {TAIL_WORDS_DEFAULT}).")
    p.add_argument("--dry-run", action="store_true", help="Show the plan; touch no network.")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    if args.top < 1:
        die(f"--top must be >= 1 (got {args.top}).")
    if args.hook_words < 1 or args.tail_words < 0:
        die("--hook-words must be >= 1 and --tail-words >= 0.")

    vlt = vault()
    niche_dir = vlt / NICHE_SUBDIR
    feed = (Path(os.path.expanduser(args.input)).resolve() if args.input
            else latest_feed(niche_dir))
    out = (Path(os.path.expanduser(args.out)) if args.out
           else niche_dir / f"{date.today().isoformat()}_transcripts.md")

    print(f"feed       : {feed if feed else '(none found)'}")
    print(f"top        : {args.top}")
    print(f"output     : {out}")

    if feed is None or not feed.exists():
        if args.dry_run:
            print("\n--- DRY RUN: no niche feed present; would write a blocked skeleton. ---")
            return
        skeleton(out, "blocked-no-feed",
                 "No niche_pull feed found to read video ids from. Run "
                 "`niche_pull.py` first (the youtube-research routine does this), "
                 "then re-run. No transcripts fetched.")
        print(f"\nℹ no feed → wrote blocked-no-feed skeleton: {out}")
        return

    entries = parse_feed(feed, args.top)
    print(f"videos     : {len(entries)} parsed from feed")

    if args.dry_run:
        print("\n--- DRY RUN (no network). Would fetch captions for the videos above "
              "(youtube-transcript-api, then yt-dlp fallback) and write the hook feed. "
              "Drop --dry-run to run. ---")
        for e in entries:
            print(f"  • {e['vid']}  {e['title'][:60]}")
        return

    if not entries:
        skeleton(out, "blocked-no-rows",
                 f"The feed {feed.name} had no parseable video rows (it may be a "
                 "blocked/partial niche_pull skeleton). No transcripts fetched.")
        print(f"\nℹ no rows → wrote blocked-no-rows skeleton: {out}")
        return

    ok = 0
    for idx, e in enumerate(entries):
        words, backend = fetch_words(e["vid"])
        e["words"] = words
        e["backend"] = backend
        if words:
            ok += 1
            print(f"  ✓ {e['vid']}: {len(words)} words via {backend}")
        else:
            print(f"  ✗ {e['vid']}: no transcript")
        if idx < len(entries) - 1:
            time.sleep(FETCH_PAUSE_SECONDS)

    if ok == 0:
        skeleton(out, "blocked-no-transcripts",
                 f"None of the {len(entries)} top videos had fetchable captions this "
                 "cycle (disabled, region-locked, or YouTube rate-limited the timedtext "
                 "endpoint). The agent proceeds on titles + web research. Re-run next "
                 "cycle.")
        print(f"\nℹ 0 transcripts → wrote blocked-no-transcripts skeleton: {out}")
        return

    write_report(out, build_body(entries, feed.name, args.hook_words,
                                 args.tail_words, ok))
    status = "ok" if ok == len(entries) else f"partial ({ok}/{len(entries)})"
    print(f"\n✅ wrote transcript feed [{status}] → {out}")
    print("   Next: dispatch `youtube-researcher` to read this alongside the niche feed.")


if __name__ == "__main__":
    main()
