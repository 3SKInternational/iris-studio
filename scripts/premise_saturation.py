#!/usr/bin/env python3
"""premise_saturation.py — is a video premise already crowded on YouTube?

WHY (2026-10-11): V15 was staged as B22 "POV: you become the wealthiest person
in your friend group". A search on dispatch day found a 166.8K-view breakout
(8/26) followed by 11 same-premise copies from tiny channels. None of it showed
up in the niche feed, which only sweeps established peer channels, so the
competitor tripwire could not see it. This measures one premise directly: a
keyword search, keeping only titles that share most of the query's words.

    python3 scripts/premise_saturation.py "B22: POV richest person in your friend group" \
        "B33: first in your family to break the paycheck cycle" [--out FILE]

Per query it reports same-premise uploads in the last 60 days, how many came
from channels under 1K subs (the copy-wave signal), and the biggest hit in 90
days, with a verdict:
    SATURATED  6+ uploads in 60 days, or a 100K+ hit in 90 days
    CROWDED    3-5 uploads in 60 days
    CLEAR      0-2
    ERROR      the search failed (never read as CLEAR)
The verdict is a flag for a human or topic-scout, not a veto.

Stdlib + the yt-dlp binary. Exit 0 = report written, 2 = yt-dlp missing.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

OUT_DIR = Path("/Users/steve/Documents/3SK/outputs/BRANDS/3SK_Finance/"
               "Channel_Intelligence/Premise_Saturation")
FETCH = 40            # search results per query
RECENT_DAYS = 60
BREAKOUT_DAYS = 90
SMALL_SUBS = 1000
SATURATED_COUNT = 6
CROWDED_COUNT = 3
BREAKOUT_VIEWS = 100_000

# "pov" is our format word, not part of any premise.
_STOP = {"a", "an", "the", "of", "in", "to", "your", "you", "and", "or", "for", "on",
         "at", "by", "is", "it", "how", "be", "with", "what", "if", "as", "this", "that",
         "pov"}
_ID_RE = re.compile(r"^\s*([A-Za-z]+\d+)\s*:\s*(.+)$")


def _fold(w: str) -> str:
    """friends -> friend, so plural titles still match (not 'class' -> 'clas')."""
    return w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w


def keywords(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9$]+", text.lower().replace(",", ""))
    return {_fold(w) for w in words if w not in _STOP}


def same_premise(query: str, title: str) -> bool:
    """At least half of the query's keywords (and 2 or more) appear in the title."""
    q = keywords(query)
    hit = len(q & keywords(title))
    return hit >= 2 and hit * 2 >= len(q)


def split_id(arg: str) -> tuple[str, str]:
    """'B22: friend group' -> ('B22', 'friend group'); a bare phrase -> ('', phrase)."""
    m = _ID_RE.match(arg)
    return (m.group(1).upper(), m.group(2).strip()) if m else ("", arg.strip())


def classify(entries: list[dict] | None, query: str, today: dt.date) -> dict:
    """entries: yt-dlp -J entries, or None when the search failed. Returns counts,
    the top hit and a verdict. A failed search is ERROR, never CLEAR."""
    if entries is None:
        return {"query": query, "recent": 0, "small": 0, "top": None,
                "verdict": "ERROR", "matched": []}
    recent = small = 0
    top = None
    matched = []
    for e in entries:
        if not isinstance(e, dict):  # yt-dlp keeps failed videos as None
            continue
        title = e.get("title") or ""
        if not same_premise(query, title):
            continue
        try:
            up = dt.datetime.strptime(e.get("upload_date") or "", "%Y%m%d").date()
        except ValueError:
            continue
        age = (today - up).days
        if age < 0:
            continue
        views = e.get("view_count") or 0
        subs = e.get("channel_follower_count")
        if age <= RECENT_DAYS:
            recent += 1
            if subs is not None and subs < SMALL_SUBS:
                small += 1
        if age <= BREAKOUT_DAYS:
            matched.append({"title": title, "channel": e.get("channel") or "?",
                            "subs": subs, "views": views, "date": up.isoformat(),
                            "url": e.get("webpage_url") or ""})
            if top is None or views > top["views"]:
                top = matched[-1]
    if recent >= SATURATED_COUNT or (top and top["views"] >= BREAKOUT_VIEWS):
        verdict = "SATURATED"
    elif recent >= CROWDED_COUNT:
        verdict = "CROWDED"
    else:
        verdict = "CLEAR"
    matched.sort(key=lambda m: m["views"], reverse=True)
    return {"query": query, "recent": recent, "small": small, "top": top,
            "verdict": verdict, "matched": matched}


def search(ytdlp: str, query: str) -> list[dict] | None:
    """One full-extract pass: -J gives upload_date, views and subs together.
    None means the search itself failed: timeout, no output, or the `null`
    yt-dlp prints under --ignore-errors when the whole search errors out."""
    try:
        proc = subprocess.run([ytdlp, f"ytsearch{FETCH}:{query}", "-J", "--no-warnings",
                               "--ignore-errors", "--no-update"],
                              capture_output=True, text=True, timeout=600)  # mutequiv: 600 vs 601s timeout — no observable behavior diff
    except (subprocess.TimeoutExpired, OSError):
        return None
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    return [e for e in (data.get("entries") or []) if isinstance(e, dict)]


def _cell(text: str) -> str:
    """Safe inside a markdown table cell and link text: no pipes, brackets, tags."""
    return (str(text).replace("|", "/").replace("[", "(").replace("]", ")")
            .replace("#", "").replace("\n", " "))


def render(results: list[dict], today: dt.date) -> str:
    failed = any(r["verdict"] == "ERROR" for r in results)
    lines = [
        "---", f"date: {today}", "type: premise-saturation",
        f"status: {'partial-search-failed' if failed else 'ok'}",
        "maintained-by: premise_saturation.py",
        "tags: [brand/3sk-finance, research/premise-saturation]", "---", "",
        f"# Premise saturation ({today})", "",
        f"Keyword search, {FETCH} results per query, kept only when the title shares "
        f"most of the query's words. Recent = last {RECENT_DAYS} days. Small = under "
        f"{SMALL_SUBS:,} subs. SATURATED = {SATURATED_COUNT}+ recent or a "
        f"{BREAKOUT_VIEWS:,}+ view hit in {BREAKOUT_DAYS} days (a big hit proves demand "
        f"as well as crowding); CROWDED = {CROWDED_COUNT}+ recent. ERROR = the search "
        f"failed, so nothing is known. A flag for topic-scout, not a veto.", "",
        "| ID | Query | Verdict | Recent | From small channels | Biggest hit (90d) |",
        "|---|---|---|---|---|---|",
    ]
    for r in results:
        t = r["top"]
        top = (f"{t['views']:,} views: {t['title']} ({t['channel']}, {t['date']})"
               if t else "none")
        lines.append(f"| {r.get('id') or '-'} | {_cell(r['query'])} | **{r['verdict']}** | "
                     f"{r['recent']} | {r['small']} | {_cell(top)} |")
    for r in results:
        lines += ["", f"## {(r.get('id') + ': ') if r.get('id') else ''}{_cell(r['query'])}", ""]
        if r["verdict"] == "ERROR":
            lines.append("Search failed (timeout, rate limit or no output). Re-run; do not read as clear.")
            continue
        if not r["matched"]:
            lines.append("No same-premise uploads in the last 90 days.")
            continue
        lines += ["| Views | Title | Channel (subs) | Published |", "|---|---|---|---|"]
        for m in r["matched"][:12]:
            subs = f"{m['subs']:,}" if m["subs"] is not None else "n/a"
            lines.append(f"| {m['views']:,} | [{_cell(m['title'])}]({m['url']}) "
                         f"| {_cell(m['channel'])} ({subs}) | {m['date']} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])  # mutequiv: help text only
    ap.add_argument("queries", nargs="+",
                    help='one search phrase per premise, optionally "B22: phrase"')
    ap.add_argument("--out", help="report path (default: OUT_DIR/<today>_premise-saturation.md)")
    a = ap.parse_args(argv)
    ytdlp = shutil.which("yt-dlp") or "/opt/homebrew/bin/yt-dlp"
    if not Path(ytdlp).exists():
        print("premise_saturation: yt-dlp not found (brew install yt-dlp)", file=sys.stderr)
        return 2
    # upload_date is a UTC date; a local date would make a US-evening upload "tomorrow".
    today = dt.datetime.now(dt.timezone.utc).date()
    out = Path(a.out) if a.out else OUT_DIR / f"{today}_premise-saturation.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    results = []
    for arg in a.queries:
        rid, phrase = split_id(arg)
        r = classify(search(ytdlp, phrase), phrase, today)
        r["id"] = rid
        results.append(r)
        # Rewrite after every query (~1 min each): a killed run still leaves a file.
        out.write_text(render(results, today), encoding="utf-8")
        print(f"{r['verdict']:9} recent={r['recent']:2} small={r['small']:2}  "
              f"{rid + ': ' if rid else ''}{phrase}", flush=True)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":  # mutequiv: entrypoint guard, not unit-testable
    sys.exit(main())
