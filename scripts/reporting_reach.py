#!/usr/bin/env python3
"""Build 4b — thumbnail impressions + impression-CTR feed (YouTube Reporting API v1).

The real-time YouTube Analytics API v2 (``analytics_pull.py``) does NOT serve
thumbnail impressions or impression CTR: it recognizes the identifiers but rejects
them in ``reports().query()``. Those metrics are **bulk-only** — Google exposes
them solely through the YouTube Reporting API (the per-video columns
``video_thumbnail_impressions`` and ``video_thumbnail_impressions_ctr``, added
2026-01-15). The Reporting API works differently from the query API:

  1. You create a persistent *reporting job* for a report type (once).
  2. Google then generates a CSV report per day, asynchronously
     (~24-48h lag; ~30-day backfill from the job's creation date).
  3. You list the job's reports and download the CSV(s) you want.

This module is the deterministic bridge. It is imported by ``analytics_pull.py``
to enrich the per-video table with Impressions + CTR, and it has a small CLI for
operating the job (create / inspect) by hand.

Graceful degradation is a hard requirement: if the Reporting API is disabled in
the GCP project, the job does not exist yet, or no report has been generated yet,
``fetch_reach`` returns an empty dict and ``analytics_pull.py`` simply shows
``n/a`` for those columns — it NEVER fabricates a number and never crashes the
nightly feed.

ONE-TIME ENABLE (Steve): the Reporting API must be enabled in the GCP project
that owns the OAuth client (project 43160144330) at
https://console.cloud.google.com/apis/library/youtubereporting.googleapis.com
The existing token's ``yt-analytics.readonly`` scope is sufficient — no re-auth.

Usage:
  python3 scripts/reporting_reach.py --ensure-job   # create the job if missing
  python3 scripts/reporting_reach.py --list         # show jobs + recent reports
  python3 scripts/reporting_reach.py                # fetch latest reach, print table
  python3 scripts/reporting_reach.py --days 28      # aggregate reports in a window
"""

from __future__ import annotations

import argparse
import csv
import io
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from youtube_client import (  # noqa: E402
    YouTubeAuthError,
    build_reporting_service,
    load_credentials,
)

# Report types that carry per-video thumbnail impressions + CTR, in preference
# order. We discover which one this channel actually has via reportTypes().list()
# rather than hard-coding a single ID, because Google has rev'd these names over
# time (…_a1 → …_a2) and not every channel is granted every type.
PREFERRED_REPORT_TYPES = (
    "channel_reach_basic_a1",
    "channel_reach_combined_a1",
    "channel_combined_a2",
)

JOB_NAME = "3sk-reach-impressions-ctr"  # our managed job's display name.

# CSV column names we read. Reporting-API column names are stable snake_case.
COL_VIDEO = "video_id"
COL_IMPRESSIONS = "video_thumbnail_impressions"
COL_CTR = "video_thumbnail_impressions_ctr"


class ReportingUnavailable(RuntimeError):
    """Reporting API is disabled / no job / no report yet — caller degrades to n/a."""


def _http_reason(exc) -> str:
    """Short reason string from a googleapiclient HttpError (best-effort)."""
    try:
        status = getattr(getattr(exc, "resp", None), "status", "?")
    except Exception:  # noqa: BLE001
        status = "?"
    return f"HTTP {status}: {str(exc).splitlines()[0][:200]}"


def _service(creds):
    return build_reporting_service(creds)


def _list_report_types(svc) -> list[dict]:
    items: list[dict] = []
    page = None
    while True:
        resp = svc.reportTypes().list(pageToken=page).execute()
        items.extend(resp.get("reportTypes", []))
        page = resp.get("nextPageToken")
        if not page:
            break
    return items


def pick_report_type(svc) -> str:
    """Return the best available reach report type id, or raise ReportingUnavailable."""
    from googleapiclient.errors import HttpError

    try:
        available = {rt.get("id") for rt in _list_report_types(svc)}
    except HttpError as exc:
        raise ReportingUnavailable(
            f"cannot list report types ({_http_reason(exc)}). If this says "
            "SERVICE_DISABLED, enable the YouTube Reporting API in the GCP project."
        ) from exc
    for rid in PREFERRED_REPORT_TYPES:
        if rid in available:
            return rid
    raise ReportingUnavailable(
        "none of the preferred reach report types "
        f"{PREFERRED_REPORT_TYPES} are available to this channel "
        f"(saw {sorted(r for r in available if r)[:8]}…)."
    )


def _list_jobs(svc) -> list[dict]:
    items: list[dict] = []
    page = None
    while True:
        resp = svc.jobs().list(pageToken=page).execute()
        items.extend(resp.get("jobs", []))
        page = resp.get("nextPageToken")
        if not page:
            break
    return items


def find_job(svc) -> dict | None:
    """Return our managed reach job (by name, else first matching report type)."""
    from googleapiclient.errors import HttpError

    try:
        jobs = _list_jobs(svc)
    except HttpError as exc:
        raise ReportingUnavailable(f"cannot list jobs ({_http_reason(exc)}).") from exc
    # Prefer our named job; fall back to any job on a preferred report type.
    for j in jobs:
        if j.get("name") == JOB_NAME:
            return j
    for rid in PREFERRED_REPORT_TYPES:
        for j in jobs:
            if j.get("reportTypeId") == rid:
                print(f"  ⚠ adopting non-managed reporting job {j.get('id')} "
                      f"(name={j.get('name')!r}, type={rid}) — not created by this tool.",
                      file=sys.stderr)
                return j
    return None


def ensure_job(svc, create: bool = True) -> dict:
    """Return the existing reach job, creating it if absent and ``create``.

    Creating a job starts Google's daily report generation + the ~30-day backfill
    clock, so call this as early as possible. Idempotent: if a matching job
    already exists it is returned unchanged.
    """
    from googleapiclient.errors import HttpError

    existing = find_job(svc)
    if existing:
        return existing
    if not create:
        raise ReportingUnavailable(
            f"no reach reporting job exists yet — run with --ensure-job to create "
            f"'{JOB_NAME}' (then wait ~24-48h for the first report)."
        )
    report_type = pick_report_type(svc)
    try:
        job = svc.jobs().create(
            body={"reportTypeId": report_type, "name": JOB_NAME}
        ).execute()
    except HttpError as exc:
        raise ReportingUnavailable(
            f"could not create reach job ({_http_reason(exc)})."
        ) from exc
    return job


_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def _parse_report_time(value: str) -> datetime | None:
    """Parse a Reporting-API RFC3339 timestamp into an aware datetime, or None."""
    if not value:
        return None
    v = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _ts_key(value: str) -> datetime:
    """Sortable timestamp key: parsed datetime, or epoch for unparseable/empty.

    Compares timestamps as datetimes (not raw strings) so mixed fractional-second
    formatting (e.g. '...00Z' vs '...00.5Z') orders correctly.
    """
    return _parse_report_time(value) or _EPOCH


def list_reports(svc, job_id: str) -> list[dict]:
    """All reports for a job, newest first (by startTime, then createTime)."""
    items: list[dict] = []
    page = None
    while True:
        resp = svc.jobs().reports().list(jobId=job_id, pageToken=page).execute()
        items.extend(resp.get("reports", []))
        page = resp.get("nextPageToken")
        if not page:
            break
    items.sort(key=lambda r: (_ts_key(r.get("startTime", "")),
                              _ts_key(r.get("createTime", ""))),
               reverse=True)
    return items


def _select_reports(reports: list[dict], start: date | None, end: date | None) -> list[dict]:
    """Reports to aggregate: one per covered UTC day; else just the newest one.

    Each report covers a single UTC day. Google can RE-ISSUE a report for a day
    (corrections/backfill), so naively summing every report would double-count.
    With a window we therefore keep exactly one report per ``startTime`` day —
    the one with the newest ``createTime`` — and sum across days. Without a window
    we return only the most recent single report (a one-day snapshot).
    """
    if not reports:
        return []
    if start is None or end is None:
        return reports[:1]  # list_reports already sorted newest-first.
    by_day: dict[date, dict] = {}
    for r in reports:
        dt = _parse_report_time(r.get("startTime", ""))
        if not dt:
            continue
        d = dt.date()
        if not (start <= d <= end):
            continue
        prev = by_day.get(d)
        if prev is None or _ts_key(r.get("createTime", "")) > _ts_key(prev.get("createTime", "")):
            by_day[d] = r
    return list(by_day.values())


def _download_csv(svc, download_url: str) -> str:
    """Download a report's CSV body as text via the public media-download API.

    A Reporting-API Report carries a ``downloadUrl`` (the report's pre-signed media
    URL); the bytes are fetched through the ``media()`` resource, NOT ``jobs().reports()``
    (which only has list/get for metadata — there is no ``get_media`` there). We
    follow Google's canonical sample: build a ``media().download_media`` request
    with a placeholder resourceName, then point it at the report's downloadUrl.
    """
    from googleapiclient.http import MediaIoBaseDownload

    request = svc.media().download_media(resourceName="")
    request.uri = download_url
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _status, done = downloader.next_chunk()
    return buf.getvalue().decode("utf-8", errors="replace")


def _aggregate_csv(text: str, acc: dict[str, dict[str, float]]) -> None:
    """Accumulate impressions + clicks per video_id from one report CSV into ``acc``.

    CTR cannot be averaged across rows/days, so we reconstruct clicks
    (impressions × ctr) per row, sum impressions and clicks per video, and let
    the caller derive overall CTR = clicks / impressions.
    """
    reader = csv.reader(io.StringIO(text))
    rows = iter(reader)
    try:
        header = next(rows)
    except StopIteration:
        return
    idx = {name: i for i, name in enumerate(header)}
    if COL_VIDEO not in idx or COL_IMPRESSIONS not in idx:
        return  # report type lacks the columns we need; skip silently.
    has_ctr = COL_CTR in idx
    for row in rows:
        if len(row) <= idx[COL_IMPRESSIONS]:
            continue
        vid = row[idx[COL_VIDEO]].strip()
        if not vid:
            continue
        try:
            imp = float(row[idx[COL_IMPRESSIONS]] or 0)
        except ValueError:
            continue
        bucket = acc.setdefault(
            vid, {"impressions": 0.0, "clicks": 0.0, "ctr_present": False})
        bucket["impressions"] += imp
        # Only accumulate clicks (and mark CTR present) when this report actually
        # carries the CTR column — so a CTR-less report type yields n/a downstream
        # rather than a fabricated 0.0%.
        if has_ctr and len(row) > idx[COL_CTR]:
            try:
                ctr = float(row[idx[COL_CTR]] or 0)
            except ValueError:
                ctr = 0.0
            bucket["clicks"] += imp * ctr
            bucket["ctr_present"] = True


def fetch_reach(creds, start: date | None = None, end: date | None = None,
                create_job: bool = False) -> dict[str, dict[str, float]]:
    """Return {video_id: {"impressions": int[, "ctr": float]}} or {} if unavailable.

    Best-effort enrichment with a HARD no-raise contract: every failure — API
    disabled, no job, no report yet, missing columns, AND any transport/network/
    auth error — returns {} so the nightly analytics feed degrades to ``n/a``
    instead of crashing. ``ctr`` is omitted (not 0) when the report carries no CTR
    column, so downstream renders ``n/a`` rather than a fabricated 0.0%. Set
    ``create_job`` to provision the job on the fly.
    """
    try:
        return _fetch_reach_inner(creds, start, end, create_job)
    except Exception as exc:  # noqa: BLE001 — best-effort: never crash the caller.
        print(f"  ⚠ reach unavailable (unexpected: {type(exc).__name__}: "
              f"{str(exc).splitlines()[0][:160]})", file=sys.stderr)
        return {}


def _fetch_reach_inner(creds, start: date | None, end: date | None,
                       create_job: bool) -> dict[str, dict[str, float]]:
    from googleapiclient.errors import HttpError

    try:
        svc = _service(creds)
        job = ensure_job(svc, create=create_job)
        reports = list_reports(svc, job["id"])
    except ReportingUnavailable as exc:
        print(f"  ⚠ reach unavailable: {exc}", file=sys.stderr)
        return {}
    except HttpError as exc:
        print(f"  ⚠ reach query failed: {_http_reason(exc)}", file=sys.stderr)
        return {}

    selected = _select_reports(reports, start, end)
    if not selected:
        print("  ⚠ reach: no report generated yet (jobs lag ~24-48h after creation).",
              file=sys.stderr)
        return {}

    acc: dict[str, dict[str, float]] = {}
    for rep in selected:
        rid = rep.get("id")
        url = rep.get("downloadUrl")
        if not rid or not url:
            continue
        try:
            text = _download_csv(svc, url)
        except Exception as exc:  # noqa: BLE001 — skip a bad report, keep the rest.
            print(f"  ⚠ reach: report {rid} download failed "
                  f"({type(exc).__name__}: {str(exc).splitlines()[0][:120]})",
                  file=sys.stderr)
            continue
        _aggregate_csv(text, acc)

    out: dict[str, dict[str, float]] = {}
    for vid, b in acc.items():
        imp = b["impressions"]
        if imp <= 0:
            continue
        entry: dict[str, float] = {"impressions": int(round(imp))}
        if b.get("ctr_present"):
            entry["ctr"] = b["clicks"] / imp
        out[vid] = entry
    return out


# --------------------------------------------------------------------------- CLI


def _cmd_list(creds) -> int:
    svc = _service(creds)
    try:
        jobs = _list_jobs(svc)
    except Exception as exc:  # noqa: BLE001
        print(f"cannot list jobs: {exc}", file=sys.stderr)
        return 2
    if not jobs:
        print("no reporting jobs. Run --ensure-job to create the reach job.")
        return 0
    for j in jobs:
        print(f"job {j.get('id')} | {j.get('reportTypeId')} | "
              f"name={j.get('name')} | created={j.get('createTime')}")
        try:
            reps = list_reports(svc, j["id"])
        except Exception as exc:  # noqa: BLE001
            print(f"    (cannot list reports: {exc})")
            continue
        for r in reps[:5]:
            print(f"    report {r.get('id')} | {r.get('startTime')} → "
                  f"{r.get('endTime')} | created={r.get('createTime')}")
        if len(reps) > 5:
            print(f"    … and {len(reps) - 5} older report(s)")
    return 0


def _cmd_ensure(creds) -> int:
    svc = _service(creds)
    try:
        job = ensure_job(svc, create=True)
    except ReportingUnavailable as exc:
        print(f"could not ensure job: {exc}", file=sys.stderr)
        return 2
    print(f"✅ reach job ready: id={job.get('id')} "
          f"type={job.get('reportTypeId')} name={job.get('name')}")
    print("   First report lands in ~24-48h; ~30-day backfill from now.")
    return 0


def _cmd_fetch(creds, start: date | None, end: date | None) -> int:
    data = fetch_reach(creds, start, end, create_job=False)
    if not data:
        print("no reach data available yet (see warnings above).")
        return 0
    print(f"{'video_id':<14} {'impressions':>12} {'ctr':>8}")
    for vid, m in sorted(data.items(), key=lambda kv: -kv[1]["impressions"]):
        print(f"{vid:<14} {m['impressions']:>12} {m['ctr'] * 100:>7.2f}%")
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="3SK YouTube reach feed (Build 4b).")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--ensure-job", action="store_true",
                   help="Create the reach reporting job if it does not exist, then exit.")
    g.add_argument("--list", action="store_true",
                   help="List reporting jobs + their recent reports, then exit.")
    p.add_argument("--days", type=int,
                   help="Aggregate reports over the last N days (default: latest report only).")
    p.add_argument("--start", help="Window start YYYY-MM-DD (with --end).")
    p.add_argument("--end", help="Window end YYYY-MM-DD (with --start).")
    p.add_argument("--token", help="Override path to youtube_token.json.")
    return p.parse_args()


def _window(args) -> tuple[date | None, date | None]:
    if args.start or args.end:
        if not (args.start and args.end):
            print("error: pass BOTH --start and --end, or neither.", file=sys.stderr)
            raise SystemExit(1)
        return date.fromisoformat(args.start), date.fromisoformat(args.end)
    if args.days:
        if args.days < 1:
            print("error: --days must be >= 1.", file=sys.stderr)
            raise SystemExit(1)
        end = date.today()
        return end - timedelta(days=args.days), end
    return None, None


def main() -> int:
    args = parse_args()
    try:
        creds = load_credentials(args.token)
    except YouTubeAuthError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.ensure_job:
        return _cmd_ensure(creds)
    if args.list:
        return _cmd_list(creds)
    start, end = _window(args)
    return _cmd_fetch(creds, start, end)


if __name__ == "__main__":
    raise SystemExit(main())
