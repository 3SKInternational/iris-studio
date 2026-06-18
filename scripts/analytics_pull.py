#!/usr/bin/env python3
"""Build 4 — performance-feedback data feed (YouTube Analytics API v2).

Pulls OUR channel's per-video metrics (views, CTR, average view duration, avg %
viewed, subscribers gained) + channel traffic sources + the top videos' audience
retention curve over a window, and writes them as a **metrics block** markdown
file under ``Channel_Intelligence/Analytics/``. That file is the exact input the
``channel-analyst`` agent already consumes — Build 4 swaps the agent's manual
YouTube-Studio-CSV paste for this automated pull. The agent's analysis logic is
unchanged; only the input is now automated.

This script does NO analysis — it is the deterministic data feed. ``channel-
analyst`` reads the file it writes and produces the routable fixes.

Output: Channel_Intelligence/Analytics/<YYYY-MM-DD>_api-pull.md (under $SK_VAULT).
Frontmatter ``status:`` follows the agent status contract so a no-data run is a
clean ``blocked-no-videos`` skeleton, never a fake success.

Usage:
  python3 scripts/analytics_pull.py --dry-run          # show plan, no network
  python3 scripts/analytics_pull.py                    # last 28 days
  python3 scripts/analytics_pull.py --days 7           # last 7 days
  python3 scripts/analytics_pull.py --start 2026-10-01 --end 2026-10-31
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from youtube_client import (  # noqa: E402
    YouTubeAuthError,
    build_analytics_service,
    build_data_service,
    load_credentials,
    resolve_channel,
)

DEFAULT_VAULT = "~/Documents/3SK/outputs/BRANDS/3SK_Finance"
ANALYTICS_SUBDIR = "Channel_Intelligence/Analytics"

# Core per-video metrics that exist for every channel report. CTR + impressions
# are requested SEPARATELY (some channels/date-ranges reject them) and degrade to
# "n/a" rather than failing the whole pull.
CORE_METRICS = [
    "views",
    "estimatedMinutesWatched",
    "averageViewDuration",
    "averageViewPercentage",
    "subscribersGained",
    "likes",
    "comments",
    "shares",
]
CTR_METRICS = ["impressions", "impressionsClickThroughRate"]

RETENTION_TOP_N = 5   # pull the audience-retention curve for the top-N by views.
MAX_VIDEOS = 50       # cap the per-video table (one analytics row each).


def die(msg: str, code: int = 1) -> None:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(code)


def vault() -> Path:
    return Path(os.path.expanduser(os.environ.get("SK_VAULT", DEFAULT_VAULT))).resolve()


def date_window(args) -> tuple[str, str]:
    """Resolve (start, end) ISO dates from --start/--end or --days (default 28)."""
    if args.start or args.end:
        if not (args.start and args.end):
            die("pass BOTH --start and --end, or neither (use --days).")
        for d in (args.start, args.end):
            try:
                date.fromisoformat(d)
            except ValueError:
                die(f"bad date {d!r} (want YYYY-MM-DD).")
        if args.start > args.end:
            die(f"--start ({args.start}) is after --end ({args.end}).")
        return args.start, args.end
    if args.days < 1:
        die(f"--days must be >= 1 (got {args.days}).")
    end = date.today()
    start = end - timedelta(days=args.days)
    return start.isoformat(), end.isoformat()


def list_uploads(data_service, uploads_playlist: str) -> list[dict]:
    """All videos in the channel's uploads playlist: [{id,title,published}]."""
    videos: list[dict] = []
    page = None
    while True:
        resp = (
            data_service.playlistItems()
            .list(part="contentDetails,snippet", playlistId=uploads_playlist,
                  maxResults=50, pageToken=page)
            .execute()
        )
        for it in resp.get("items", []):
            cd = it.get("contentDetails", {})
            sn = it.get("snippet", {})
            vid_id = cd.get("videoId")
            if vid_id:
                videos.append({
                    "id": vid_id,
                    "title": sn.get("title", "(untitled)"),
                    "published": cd.get("videoPublishedAt", sn.get("publishedAt", "")),
                })
        page = resp.get("nextPageToken")
        if not page:
            break
    return videos


def query_per_video(analytics, channel_id: str, start: str, end: str,
                    metrics: list[str]) -> dict[str, list]:
    """video_id -> metric row, for `metrics`, sorted by views desc. {} on failure."""
    from googleapiclient.errors import HttpError

    try:
        resp = (
            analytics.reports()
            .query(ids=f"channel=={channel_id}", startDate=start, endDate=end,
                   metrics=",".join(metrics), dimensions="video",
                   sort="-views", maxResults=MAX_VIDEOS)
            .execute()
        )
    except HttpError as exc:
        print(f"  ⚠ per-video query for {metrics} failed: {exc}", file=sys.stderr)
        return {}
    headers = [h["name"] for h in resp.get("columnHeaders", [])]
    out: dict[str, list] = {}
    for row in resp.get("rows", []):
        rec = dict(zip(headers, row))
        vid_id = rec.pop("video", None)
        if vid_id:
            out[vid_id] = rec
    return out


def query_traffic_sources(analytics, channel_id: str, start: str, end: str) -> list[tuple[str, int]]:
    from googleapiclient.errors import HttpError

    try:
        resp = (
            analytics.reports()
            .query(ids=f"channel=={channel_id}", startDate=start, endDate=end,
                   metrics="views", dimensions="insightTrafficSourceType",
                   sort="-views")
            .execute()
        )
    except HttpError as exc:
        print(f"  ⚠ traffic-source query failed: {exc}", file=sys.stderr)
        return []
    return [(r[0], int(r[1])) for r in resp.get("rows", [])]


def query_retention(analytics, channel_id: str, video_id: str, start: str, end: str):
    """[(elapsedRatio, audienceWatchRatio)] for one video, or [] on failure."""
    from googleapiclient.errors import HttpError

    try:
        resp = (
            analytics.reports()
            .query(ids=f"channel=={channel_id}", startDate=start, endDate=end,
                   metrics="audienceWatchRatio", dimensions="elapsedVideoTimeRatio",
                   filters=f"video=={video_id}", sort="elapsedVideoTimeRatio")
            .execute()
        )
    except HttpError as exc:
        print(f"  ⚠ retention query for {video_id} failed: {exc}", file=sys.stderr)
        return []
    return [(float(r[0]), float(r[1])) for r in resp.get("rows", [])]


def biggest_dip(curve: list[tuple[float, float]]) -> str:
    """Human note for the steepest drop in an audience-retention curve."""
    if len(curve) < 2:
        return "insufficient retention data"
    worst_drop = 0.0
    at = 0.0
    for (r0, w0), (r1, w1) in zip(curve, curve[1:]):
        drop = w0 - w1
        if drop > worst_drop:
            worst_drop, at = drop, r1
    return (f"steepest drop ≈{worst_drop * 100:.0f}% around {at * 100:.0f}% "
            "through the video") if worst_drop > 0 else "no notable drop"


def fmt_avd(seconds) -> str:
    try:
        s = int(round(float(seconds)))
    except (TypeError, ValueError):
        return "n/a"
    return f"{s // 60}:{s % 60:02d}"


def fmt_ctr(v) -> str:
    try:
        return f"{float(v):.2f}%"
    except (TypeError, ValueError):
        return "n/a"


def write_report(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, path)


def skeleton(path: Path, status: str, note: str, start: str, end: str) -> None:
    write_report(path, (
        "---\n"
        f"date: {date.today().isoformat()}\n"
        "type: analytics-pull\n"
        f"status: {status}\n"
        f"window: {start}..{end}\n"
        "source: youtube-analytics-api\n"
        "tags:\n  - brand/3sk-finance\n  - analytics/api-pull\n"
        "---\n\n"
        f"# 3SK Finance — Analytics API pull ({start} → {end})\n\n"
        f"{note}\n"
    ))


def build_report_body(channel: dict, start: str, end: str, rows: list[dict],
                      traffic: list[tuple[str, int]], retention: dict[str, str],
                      ctr_available: bool) -> str:
    lines = [
        "---",
        f"date: {date.today().isoformat()}",
        "type: analytics-pull",
        "status: ok",
        f"window: {start}..{end}",
        "source: youtube-analytics-api",
        "tags:",
        "  - brand/3sk-finance",
        "  - analytics/api-pull",
        "---",
        "",
        f"# 3SK Finance — Analytics API pull ({start} → {end})",
        "",
        f"Channel **{_cell(channel['title'])}** ({channel['id']}). Automated pull for the "
        "`channel-analyst` agent — feed this file's path (or the table below) to it "
        "for the diagnosis + routable fixes. Raw data only; no analysis here.",
        "",
        "## Per-video metrics",
        "",
    ]
    if ctr_available:
        lines += [
            "| Video | Views | Impressions | CTR | AVD | Avg % | Subs+ | Likes | Comments |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for r in rows:
            lines.append(
                f"| {_cell(r['title'], 48)} | {r.get('views', 0)} | "
                f"{r.get('impressions', 'n/a')} | {fmt_ctr(r.get('impressionsClickThroughRate'))} | "
                f"{fmt_avd(r.get('averageViewDuration'))} | "
                f"{_pct(r.get('averageViewPercentage'))} | {r.get('subscribersGained', 0)} | "
                f"{r.get('likes', 0)} | {r.get('comments', 0)} |"
            )
    else:
        lines += [
            "_CTR/impressions unavailable for this window (the Analytics API rejected "
            "them — often too little data yet); core metrics only._",
            "",
            "| Video | Views | AVD | Avg % | Subs+ | Likes | Comments |",
            "|---|---|---|---|---|---|---|",
        ]
        for r in rows:
            lines.append(
                f"| {_cell(r['title'], 48)} | {r.get('views', 0)} | "
                f"{fmt_avd(r.get('averageViewDuration'))} | {_pct(r.get('averageViewPercentage'))} | "
                f"{r.get('subscribersGained', 0)} | {r.get('likes', 0)} | {r.get('comments', 0)} |"
            )
    lines += ["", "## Channel traffic sources (by views)", ""]
    if traffic:
        lines.append("| Source | Views |")
        lines.append("|---|---|")
        for src, v in traffic:
            lines.append(f"| {_cell(src)} | {v} |")
    else:
        lines.append("_No traffic-source data in this window._")
    lines += ["", "## Audience retention — biggest drop-off (top videos)", ""]
    if retention:
        for title, note in retention.items():
            lines.append(f"- **{_cell(title)}** — {note}")
    else:
        lines.append("_No retention data in this window._")
    lines.append("")
    return "\n".join(lines)


def _pct(v) -> str:
    try:
        return f"{float(v):.1f}%"
    except (TypeError, ValueError):
        return "n/a"


def _cell(s: str, limit: int = 60) -> str:
    """Make a string safe for a markdown table cell (no pipe/newline breakage)."""
    return str(s).replace("|", "\\|").replace("\n", " ").replace("\r", " ").strip()[:limit]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="3SK YouTube analytics pull (Build 4).")
    p.add_argument("--days", type=int, default=28,
                   help="Window length ending today (default 28). Ignored if --start/--end given.")
    p.add_argument("--start", help="Window start YYYY-MM-DD (with --end).")
    p.add_argument("--end", help="Window end YYYY-MM-DD (with --start).")
    p.add_argument("--token", help="Override path to youtube_token.json.")
    p.add_argument("--out", help="Override output file path (default Channel_Intelligence/Analytics/<date>_api-pull.md).")
    p.add_argument("--dry-run", action="store_true", help="Show the plan; touch no network.")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    start, end = date_window(args)
    vlt = vault()
    out = (Path(os.path.expanduser(args.out)) if args.out
           else vlt / ANALYTICS_SUBDIR / f"{date.today().isoformat()}_api-pull.md")

    print(f"window     : {start} → {end}")
    print(f"output     : {out}")

    if args.dry_run:
        print("\n--- DRY RUN (no network). Would resolve channel, list uploads, and "
              "query per-video + traffic + retention metrics. Drop --dry-run to run. ---")
        return

    try:
        creds = load_credentials(args.token)
    except YouTubeAuthError as exc:
        die(str(exc), code=2)
    data = build_data_service(creds)
    analytics = build_analytics_service(creds)

    try:
        channel = resolve_channel(data)
    except YouTubeAuthError as exc:
        die(str(exc), code=2)
    print(f"channel    : {channel['title']} ({channel['id']})")

    videos = list_uploads(data, channel["uploads_playlist"])
    if not videos:
        skeleton(out, "blocked-no-videos",
                 "No published videos on the channel yet — nothing to analyze. This "
                 "is expected pre-launch; re-run once videos are live (Build 3 uploads).",
                 start, end)
        print(f"\nℹ no videos yet → wrote blocked-no-videos skeleton: {out}")
        return
    print(f"uploads    : {len(videos)} video(s)")

    title_by_id = {v["id"]: v["title"] for v in videos}
    core = query_per_video(analytics, channel["id"], start, end, CORE_METRICS)
    ctr = query_per_video(analytics, channel["id"], start, end, CTR_METRICS)
    ctr_available = bool(ctr)

    # Merge core + CTR per video; keep core ordering (already -views sorted).
    rows: list[dict] = []
    for vid_id, rec in core.items():
        merged = {"id": vid_id, "title": title_by_id.get(vid_id, vid_id)}
        merged.update(rec)
        if vid_id in ctr:
            merged.update(ctr[vid_id])
        rows.append(merged)

    if not rows:
        skeleton(out, "partial-no-window-data",
                 f"{len(videos)} video(s) exist but the Analytics API returned no rows "
                 f"for {start}..{end} (data can lag ~48–72h after publish). Re-run later.",
                 start, end)
        print(f"\nℹ no analytics rows for window → wrote partial skeleton: {out}")
        return

    traffic = query_traffic_sources(analytics, channel["id"], start, end)
    retention: dict[str, str] = {}
    for r in rows[:RETENTION_TOP_N]:
        curve = query_retention(analytics, channel["id"], r["id"], start, end)
        retention[r["title"]] = biggest_dip(curve)

    write_report(out, build_report_body(channel, start, end, rows, traffic,
                                        retention, ctr_available))
    print(f"\n✅ wrote analytics feed ({len(rows)} videos) → {out}")
    print("   Next: dispatch `channel-analyst` with this file's path for the diagnosis.")


if __name__ == "__main__":
    main()
