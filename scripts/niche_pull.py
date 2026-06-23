#!/usr/bin/env python3
"""Niche view-count feed (YouTube Data API v3) for the youtube-researcher agent.

The competitor analogue of ``analytics_pull.py``. Where that script reads OUR
channel's metrics from the Analytics API, this one reads the OUTSIDE animated-
finance peer set from the public Data API: for a rolling window it runs
``search.list`` (ordered by view count) within each peer channel — the
Willie-anchored animated-finance allowlist (Willie Finance + 100k+ look-alikes
in his animated-documentary format) — then ``videos.list`` to attach the REAL
``viewCount`` (+ channel, publish date, duration) to each hit, and writes a
single markdown file ranked by views. Finance keyword search is available as an
opt-in (``--keyword-search``) for topic discovery, but is OFF by default because
it resurfaces human face-to-camera creators outside the animated peer set.

This exists because youtube-researcher only has WebSearch/WebFetch — it cannot
call the Data API itself, so its ``viral_teardowns.md`` kept saying "Views: No
public data retrieved". This deterministic feed gives it real, ranked numbers to
build the teardown + title-performance intel on instead of guessing.

This script does NO analysis — it is the data feed. The ``youtube-research``
routine runs it first, then hands the file to the agent (see
routines/youtube-research.prompt), exactly mirroring the analytics-feedback loop.

Quota: ``search.list`` costs 100 units/call, ``videos.list`` 1 unit per ≤50 ids.
Default run (allowlist only, 4 channels) ≈ 400 units; ``--keyword-search`` adds
~800 — both well under the 10k/day default quota.

Output: Channel_Intelligence/Niche_Views/<YYYY-MM-DD>_niche-views.md (under $SK_VAULT).
Frontmatter ``status:`` follows the agent status contract so a no-results / quota
run is a clean skeleton, never a fake success.

Usage:
  python3 scripts/niche_pull.py --dry-run        # show plan, no network
  python3 scripts/niche_pull.py                  # last 14 days, animated-peer allowlist (no keyword search)
  python3 scripts/niche_pull.py --keyword-search # also run opt-in keyword queries
  python3 scripts/niche_pull.py --days 7         # last 7 days
  python3 scripts/niche_pull.py --per-query 15   # widen per-query result depth
  python3 scripts/niche_pull.py --queries my_queries.txt   # one query per line
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from youtube_client import (  # noqa: E402
    YouTubeAuthError,
    build_data_service,
    load_credentials,
)

DEFAULT_VAULT = "~/Documents/3SK/outputs/BRANDS/3SK_Finance"
NICHE_SUBDIR = "Channel_Intelligence/Niche_Views"

# PRIMARY high-signal source: the WILLIE-ANCHORED animated-finance peer set.
# 3SK Finance is an animated-documentary finance channel built directly on the
# Willie Finance format (flat-2D illustrated, high image density, data-on-art,
# "Every Level of [X]" / "POV: Your Life at Every Level of Wealth"). The benchmark
# is therefore Willie himself + the channels that share that exact animated format
# at scale — NOT human face-to-camera personal brands (Graham/Humphrey/Ramit/
# Nischa) and NOT live-action faceless-documentary channels (How Money Works /
# Economics Explained), which are a different visual genre.
#
# Curated 2026-06-20 (Steve: "Willie is the main account; find 100k+ look-alikes").
# The pure animated-finance-documentary lane is genuinely thin — only two true
# 100k+ look-alikes exist (Finance With Ryan, Hypothetically); Steve chose this
# tight "core only" set over looser animated-money adjacents. Willie himself is
# ~24K subs (under 100k) but is the named STYLE ANCHOR, so he stays in.
#
# EXACT handles matter: `willie_finance` (with the underscore) — `WillieFinance`
# resolves to a DIFFERENT, unrelated channel. Resolved at runtime via
# channels.list(forHandle=...) so we never hardcode brittle UC ids; a handle that
# fails to resolve is skipped, never fatal. Override with --channels (one handle
# per line). Each contributes its most-viewed uploads in the window.
CREATOR_HANDLES = [
    "willie_finance",   # Willie Finance — STYLE ANCHOR, animated-documentary (~24K)
    "ryanfinanceus",    # Finance With Ryan — animated finance, near-identical (~184K)
    "hypotheticallyhq", # Hypothetically — animated "every level of wealth" (~297K)
    "markbuildsus",     # Mark Invests — animated-character finance (added 2026-06-22, Steve)
]

# SECONDARY source: keyword search. INTENTIONALLY finance-anchored (every query
# carries an unambiguous money/investing term) because a broad seed like "how to
# become a millionaire" gets hijacked by Minecraft/Roblox/GTA "trillionaire"
# videos under order=viewCount. 3SK's bare format phrases (POV / every level of
# wealth) are the WORST offenders for non-finance bleed, so they are deliberately
# NOT seeded here — the creator allowlist captures those formats with real
# finance framing instead. Override with --queries (one per line) to retune.
SEED_QUERIES = [
    "how to build wealth from nothing",
    "net worth by age average american",
    "personal finance for beginners",
    "how to invest in index funds",
    "roth ira explained",
    "passive income dividend investing",
    "money mistakes to avoid in your 20s",
    "financial independence retire early",
]

PER_QUERY_DEFAULT = 10   # search results requested per query/channel (<=50 per call).
DEFAULT_DAYS = 14        # rolling window; matches viral_teardowns' 14-day scope.
TABLE_TOP_N = 40         # cap the ranked table after merging + dedup.
SHORTS_MAX_SECONDS = 60  # <= this duration is a Short (dropped unless --include-shorts).


def die(msg: str, code: int = 1) -> None:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(code)


def vault() -> Path:
    return Path(os.path.expanduser(os.environ.get("SK_VAULT", DEFAULT_VAULT))).resolve()


def load_queries(path: str | None) -> list[str]:
    if not path:
        return list(SEED_QUERIES)
    p = Path(os.path.expanduser(path)).resolve()
    if not p.exists():
        die(f"--queries file not found: {p}")
    qs = [ln.strip() for ln in p.read_text(encoding="utf-8").splitlines()
          if ln.strip() and not ln.lstrip().startswith("#")]
    if not qs:
        die(f"--queries file {p} has no usable (non-comment) lines.")
    return qs


def load_handles(path: str | None) -> list[str]:
    if not path:
        return list(CREATOR_HANDLES)
    p = Path(os.path.expanduser(path)).resolve()
    if not p.exists():
        die(f"--channels file not found: {p}")
    hs = [ln.strip().lstrip("@") for ln in p.read_text(encoding="utf-8").splitlines()
          if ln.strip() and not ln.lstrip().startswith("#")]
    if not hs:
        die(f"--channels file {p} has no usable (non-comment) lines.")
    return hs


def published_after(days: int) -> str:
    """RFC3339 UTC timestamp `days` ago, for search.list publishedAfter."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")


# --- API calls -------------------------------------------------------------

class QuotaExceeded(RuntimeError):
    """Raised when the Data API reports the daily quota is exhausted."""


def _is_quota_error(exc) -> bool:
    txt = str(exc).lower()
    return "quotaexceeded" in txt or "dailylimitexceeded" in txt


def resolve_handle(data_service, handle: str) -> str | None:
    """@handle -> channelId via channels.list(forHandle). None on miss/soft error."""
    from googleapiclient.errors import HttpError

    try:
        resp = (
            data_service.channels()
            .list(part="id", forHandle=handle.lstrip("@"))
            .execute()
        )
    except HttpError as exc:
        if _is_quota_error(exc):
            raise QuotaExceeded(str(exc)) from exc
        print(f"  ⚠ resolve @{handle} failed: {exc}", file=sys.stderr)
        return None
    items = resp.get("items", [])
    return items[0]["id"] if items else None


def search_video_ids(data_service, after: str, per_query: int, *,
                     query: str | None = None,
                     channel_id: str | None = None) -> list[str]:
    """Most-viewed video ids in the window, by `query` and/or within `channel_id`.

    [] on soft failure. Raises QuotaExceeded so the caller can stop early and
    salvage partial data instead of hammering an exhausted quota.
    """
    from googleapiclient.errors import HttpError

    kwargs = dict(part="id", type="video", order="viewCount", publishedAfter=after,
                  maxResults=min(per_query, 50), regionCode="US",
                  relevanceLanguage="en")
    if query is not None:
        kwargs["q"] = query
    if channel_id is not None:
        kwargs["channelId"] = channel_id
    label = query if query is not None else f"channel:{channel_id}"
    try:
        resp = data_service.search().list(**kwargs).execute()
    except HttpError as exc:
        if _is_quota_error(exc):
            raise QuotaExceeded(str(exc)) from exc
        print(f"  ⚠ search for {label!r} failed: {exc}", file=sys.stderr)
        return []
    ids = []
    for it in resp.get("items", []):
        vid = it.get("id", {}).get("videoId")
        if vid:
            ids.append(vid)
    return ids


def fetch_video_stats(data_service, video_ids: list[str]) -> dict[str, dict]:
    """video_id -> {title, channel, views, published, seconds} for given ids."""
    from googleapiclient.errors import HttpError

    out: dict[str, dict] = {}
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i:i + 50]
        try:
            resp = (
                data_service.videos()
                .list(part="snippet,statistics,contentDetails", id=",".join(batch))
                .execute()
            )
        except HttpError as exc:
            if _is_quota_error(exc):
                raise QuotaExceeded(str(exc)) from exc
            print(f"  ⚠ videos.list batch failed: {exc}", file=sys.stderr)
            continue
        for it in resp.get("items", []):
            vid = it.get("id")
            if not vid:
                continue
            sn = it.get("snippet", {})
            st = it.get("statistics", {})
            cd = it.get("contentDetails", {})
            views = st.get("viewCount")
            out[vid] = {
                "id": vid,
                "title": sn.get("title", "(untitled)"),
                "channel": sn.get("channelTitle", "(unknown)"),
                "published": sn.get("publishedAt", ""),
                "views": int(views) if views is not None and str(views).isdigit() else None,
                "seconds": parse_duration(cd.get("duration", "")),
            }
    return out


# --- formatting ------------------------------------------------------------

_DUR_RE = re.compile(r"P(?:(\d+)D)?T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


def parse_duration(iso: str) -> int | None:
    """ISO-8601 video duration (e.g. PT12M30S) -> total seconds, or None."""
    if not iso:
        return None
    m = _DUR_RE.fullmatch(iso)
    if not m:
        return None
    days, hours, mins, secs = (int(g) if g else 0 for g in m.groups())
    return days * 86400 + hours * 3600 + mins * 60 + secs


def fmt_len(seconds) -> str:
    if seconds is None:
        return "n/a"
    if seconds <= SHORTS_MAX_SECONDS:
        return f"{seconds}s (Short)"
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_views(v) -> str:
    return f"{v:,}" if isinstance(v, int) else "n/a"


def fmt_date(iso: str) -> str:
    return iso[:10] if iso else "n/a"


def _cell(s: str, limit: int = 70) -> str:
    """Make a string safe for a markdown table cell (no pipe/newline breakage).

    Truncate BEFORE escaping so a `|` straddling the limit can't leave a dangling
    backslash that escapes the column delimiter (external YouTube titles).
    """
    flat = str(s).replace("\n", " ").replace("\r", " ").strip()[:limit]
    return flat.replace("|", "\\|")


def write_report(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, path)


def skeleton(path: Path, status: str, note: str, window_days: int) -> None:
    write_report(path, (
        "---\n"
        f"date: {date.today().isoformat()}\n"
        "type: niche-views\n"
        f"status: {status}\n"
        f"window: last {window_days} days\n"
        "source: youtube-data-api\n"
        "maintained-by: niche_pull.py\n"
        "tags:\n  - brand/3sk-finance\n  - niche/view-counts\n"
        "---\n\n"
        f"# 3SK Finance — Niche most-viewed feed (last {window_days} days)\n\n"
        f"{note}\n"
    ))


def build_body(rows: list[dict], queries: list[str], window_days: int,
               quota_note: str, keyword_search: bool) -> str:
    lines = [
        "---",
        f"date: {date.today().isoformat()}",
        "type: niche-views",
        "status: ok",
        f"window: last {window_days} days",
        "source: youtube-data-api",
        "maintained-by: niche_pull.py",
        "tags:",
        "  - brand/3sk-finance",
        "  - niche/view-counts",
        "---",
        "",
        f"# 3SK Finance — Niche most-viewed feed (last {window_days} days)",
        "",
        "Real view counts from the YouTube **Data API** (`search.list` ordered by "
        "views → `videos.list` statistics) over the **Willie-anchored animated-finance "
        "peer set** (Willie Finance + 100k+ look-alikes that share his animated-"
        "documentary format). This is the deterministic data feed for "
        "**youtube-researcher** — use these numbers to rank `viral_teardowns.md` and "
        "gauge `title_performance.md` instead of 'no public data'. Raw data only; no "
        "analysis here.",
        "",
    ]
    if keyword_search:
        lines += [
            f"**Keyword search (opt-in):** {', '.join('`' + q + '`' for q in queries)}",
            "",
        ]
    if quota_note:
        lines += [f"> ⚠️ {quota_note}", ""]
    lines += [
        f"## Top {len(rows)} by views (most-viewed first)",
        "",
        "_Source = `creator:@handle` (an animated-finance peer channel's most-viewed "
        "in window — the primary, replicable-format benchmark) or `q:<query>` "
        "(opt-in keyword search — note: this can surface human face-to-camera "
        "creators outside the animated peer set)._",
        "",
        "| # | Views | Title | Channel | Published | Length | Source |",
        "|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(rows, 1):
        lines.append(
            f"| {i} | {fmt_views(r['views'])} | "
            f"[{_cell(r['title'])}](https://www.youtube.com/watch?v={r['id']}) | "
            f"{_cell(r['channel'], 36)} | {fmt_date(r['published'])} | "
            f"{fmt_len(r['seconds'])} | {_cell(r['source'], 30)} |"
        )
    lines.append("")
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="3SK YouTube niche view-count feed.")
    p.add_argument("--days", type=int, default=DEFAULT_DAYS,
                   help=f"Rolling window in days (default {DEFAULT_DAYS}).")
    p.add_argument("--per-query", type=int, default=PER_QUERY_DEFAULT,
                   help=f"Results per query/channel, 1-50 (default {PER_QUERY_DEFAULT}).")
    p.add_argument("--queries", help="Path to a file of seed queries (one per line; # comments ok).")
    p.add_argument("--channels", help="Path to a file of creator handles (one per line; # comments ok).")
    p.add_argument("--include-shorts", action="store_true",
                   help="Keep Shorts (<=60s) in the feed (dropped by default; 3SK is long-form).")
    p.add_argument("--keyword-search", action="store_true",
                   help="ALSO run the finance keyword searches (default OFF). These surface human "
                        "face-to-camera creators outside 3SK's format; 3SK is an animated-documentary "
                        "channel, so the animated-peer allowlist is the benchmark. Use for "
                        "topic/title discovery only.")
    p.add_argument("--token", help="Override path to youtube_token.json.")
    p.add_argument("--out", help="Override output file path.")
    p.add_argument("--dry-run", action="store_true", help="Show the plan; touch no network.")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    if args.days < 1:
        die(f"--days must be >= 1 (got {args.days}).")
    if not (1 <= args.per_query <= 50):
        die(f"--per-query must be 1-50 (got {args.per_query}).")

    queries = load_queries(args.queries)
    handles = load_handles(args.channels)
    after = published_after(args.days)
    vlt = vault()
    out = (Path(os.path.expanduser(args.out)) if args.out
           else vlt / NICHE_SUBDIR / f"{date.today().isoformat()}_niche-views.md")

    # Rough quota: 1 unit/handle (resolve) + 100/handle-search + 100/query + videos.list.
    est = len(handles) * 101 + (len(queries) * 100 if args.keyword_search else 0)
    print(f"window     : last {args.days} days (publishedAfter {after})")
    print(f"creators   : {len(handles)} ({', '.join('@' + h for h in handles)})")
    print(f"queries    : {len(queries) if args.keyword_search else 0}"
          f"{' (keyword search ON — also includes human creators)' if args.keyword_search else ' (keyword search OFF — animated peer allowlist only)'}")
    print(f"quota est  : ~{est} units (of 10k/day)")
    print(f"shorts     : {'kept' if args.include_shorts else 'dropped (long-form channel)'}")
    print(f"output     : {out}")

    if args.dry_run:
        kw = "per channel + per query" if args.keyword_search else "per channel (keyword search off)"
        print(f"\n--- DRY RUN (no network). Would resolve creator handles, run "
              f"search.list(order=viewCount) {kw}, then videos.list for statistics, "
              "and write the ranked feed. Drop --dry-run to run. ---")
        return

    try:
        creds = load_credentials(args.token)
    except YouTubeAuthError as exc:
        die(str(exc), code=2)
    data = build_data_service(creds)

    # id -> source label. Creators run FIRST (primary signal); setdefault keeps the
    # creator attribution if a keyword search later surfaces the same video.
    source: dict[str, str] = {}
    quota_note = ""
    try:
        for h in handles:
            cid = resolve_handle(data, h)
            if not cid:
                print(f"  creator @{h}: unresolved (skipped)")
                continue
            ids = search_video_ids(data, after, args.per_query, channel_id=cid)
            for vid in ids:
                source.setdefault(vid, f"creator:@{h}")
            print(f"  creator @{h}: {len(ids)} hit(s)")
        if args.keyword_search:
            for q in queries:
                ids = search_video_ids(data, after, args.per_query, query=q)
                for vid in ids:
                    source.setdefault(vid, f"q:{q}")
                print(f"  search {q!r}: {len(ids)} hit(s)")
    except QuotaExceeded:
        quota_note = ("Data API quota was exhausted mid-pull — this feed is PARTIAL "
                      "(only the sources that ran before the cap are included).")
        print("  ⚠ quota exhausted during search — salvaging partial results", file=sys.stderr)

    if not source:
        status = "partial-quota" if quota_note else "blocked-no-results"
        note = (quota_note or
                "No videos found from the animated-finance peer channels in this "
                "window. Widen --days, retune --channels, or add --keyword-search; "
                "re-run next cycle.")
        skeleton(out, status, note, args.days)
        print(f"\nℹ no results → wrote {status} skeleton: {out}")
        return

    try:
        stats = fetch_video_stats(data, list(source))
    except QuotaExceeded:
        quota_note = (quota_note or
                      "Data API quota was exhausted while fetching statistics — view "
                      "counts may be incomplete.")
        stats = {}

    rows = []
    dropped_shorts = 0
    for vid, src in source.items():
        s = stats.get(vid)
        if not s:
            continue
        if not args.include_shorts and s["seconds"] is not None \
                and s["seconds"] <= SHORTS_MAX_SECONDS:
            dropped_shorts += 1
            continue
        s["source"] = src
        rows.append(s)
    if dropped_shorts:
        print(f"  dropped {dropped_shorts} Short(s) (<= {SHORTS_MAX_SECONDS}s)")

    # Rank by real views (None sorts last), cap the table.
    rows.sort(key=lambda r: (r["views"] is not None, r["views"] or 0), reverse=True)
    rows = rows[:TABLE_TOP_N]

    if not rows:
        note = ("Sources returned only Shorts (filtered out — pass --include-shorts to "
                "keep them) or statistics could not be fetched. Re-run next cycle.")
        skeleton(out, "partial-no-stats", note, args.days)
        print(f"\nℹ no long-form stats rows → wrote partial skeleton: {out}")
        return

    write_report(out, build_body(rows, queries, args.days, quota_note, args.keyword_search))
    print(f"\n✅ wrote niche feed ({len(rows)} videos, top by views) → {out}")
    print("   Next: dispatch `youtube-researcher` to read this file for ranked intel.")


if __name__ == "__main__":
    main()
