#!/usr/bin/env python3
"""V.A.U.L.T. — local command-center dashboard for Iris. Mini-only.

Live vitals (parsed from the freshest YouTube analytics api-pull + iris.db
spend) + a one-tap command deck that fires the SAME agents/scripts the daemon
uses. Serves one page on 127.0.0.1 only.

ponytail: one file, starlette+uvicorn (already in the venv), inline HTML, thread
pool for fire jobs. Real data only — tiles show "n/a" when the source is
missing, never fabricated numbers. Run --selftest for an audio/server-free check.

Security: binds 127.0.0.1 deliberately. The /api/fire endpoint spawns agents
(spends Max-sub tokens / runs code) — exposing it on the LAN needs an auth token
first. ponytail: localhost ceiling; add a token before any 0.0.0.0 bind.
"""
from __future__ import annotations
import os, re, sys, json, sqlite3, subprocess, threading, uuid, datetime as dt
from pathlib import Path

VAULT = Path(os.environ.get("SK_VAULT",
             "/Users/steve/Documents/3SK/outputs/BRANDS/3SK_Finance"))
ANALYTICS_DIR = VAULT / "Channel_Intelligence" / "Analytics"
DB_PATH = Path("/Volumes/AI_Workspace/iris_studio/iris.db")
REPO = Path("/Volumes/AI_Workspace/iris_studio")
VENV_PY = REPO / ".venv" / "bin" / "python"
CLAUDE_CLI = os.environ.get("CLAUDE_CLI_PATH", "/opt/homebrew/bin/claude")
DAILY_CAP_USD = 2.0
HOST, PORT = "127.0.0.1", int(os.environ.get("IRIS_DASH_PORT", "8765"))

# Command deck: label, kind, target, prompt. kind "agent" → claude --agent;
# "script" → a repo script. Only agents that run with a generic prompt (no
# required args) are here — keeps every button one-tap.
DECK = [
    {"key": "metrics",  "label": "Metrics pull",   "kind": "script",
     "target": "scripts/analytics_pull.py"},
    {"key": "analyst",  "label": "Channel analyst", "kind": "agent",
     "target": "channel-analyst",
     "prompt": "Read the latest analytics api-pull in Channel_Intelligence/"
               "Analytics and give the diagnosis plus routable fixes."},
    {"key": "topics",   "label": "Topic scout",     "kind": "agent",
     "target": "topic-scout", "prompt": "Refresh the ranked topic backlog."},
    {"key": "status",   "label": "Project status",  "kind": "agent",
     "target": "project-manager",
     "prompt": "Give an honest status read: shipped, in-progress, blocked, next."},
    {"key": "research", "label": "YT research",      "kind": "agent",
     "target": "youtube-researcher",
     "prompt": "Refresh the finance-creator intel: hooks, titles, what's "
               "rewarded now."},
]
DECK_BY_KEY = {d["key"]: d for d in DECK}

JOBS: dict[str, dict] = {}        # id -> {label, status, started, ended, rc, tail}
_LOCK = threading.Lock()


# --- vitals ---------------------------------------------------------------
def _latest_pull() -> Path | None:
    pulls = sorted(ANALYTICS_DIR.glob("*_api-pull.md"))
    return pulls[-1] if pulls else None


def _int(s: str) -> int | None:
    s = s.strip()
    return int(s) if s.isdigit() else None


def parse_pull(path: Path) -> dict:
    """Parse the per-video table + traffic sources. Numbers as written, no fab."""
    text = path.read_text(encoding="utf-8")
    window = (re.search(r"^window:\s*(.+)$", text, re.M) or [None, "—"])[1].strip()
    pdate = (re.search(r"^date:\s*(.+)$", text, re.M) or [None, ""])[1].strip()

    videos, in_video, in_traffic, top_source = [], False, False, None
    for line in text.splitlines():
        if line.startswith("## Per-video"):
            in_video, in_traffic = True, False; continue
        if line.startswith("## Channel traffic"):
            in_video, in_traffic = False, True; continue
        if line.startswith("## ") and not line.startswith("## Channel traffic"):
            in_video = in_traffic = False
        cells = [c.strip() for c in line.split("|")][1:-1] if line.strip().startswith("|") else None
        if not cells or cells[0] in ("Video", "Source") or set(cells[0]) <= {"-"}:
            continue
        if in_video and len(cells) >= 9:
            videos.append({
                "title": cells[0], "impr": cells[1], "ctr": cells[2],
                "views": _int(cells[3]) or 0, "avd": cells[4],
                "avg_pct": cells[5], "subs": _int(cells[6]) or 0,
                "likes": _int(cells[7]) or 0, "comments": _int(cells[8]) or 0,
            })
        elif in_traffic and len(cells) >= 2 and top_source is None:
            top_source = f"{cells[0]} ({cells[1]})"

    age = None
    try:
        age = (dt.date.today() - dt.date.fromisoformat(pdate)).days
    except ValueError:
        pass
    return {
        "window": window, "pull_date": pdate, "pull_age_days": age,
        "videos": videos,
        "total_views": sum(v["views"] for v in videos),
        "subs_gained": sum(v["subs"] for v in videos),
        "total_likes": sum(v["likes"] for v in videos),
        "total_comments": sum(v["comments"] for v in videos),
        "video_count": len(videos),
        "top_source": top_source or "—",
    }


def claude_spend() -> dict:
    today = dt.date.today().isoformat()
    spent = 0.0
    try:
        with sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5) as c:
            row = c.execute(
                "SELECT COALESCE(SUM(cost_usd),0.0) FROM daily_stats "
                "WHERE date=? AND tier='tier4'", (today,)).fetchone()
            spent = float(row[0]) if row else 0.0
    except Exception:
        return {"spend_usd": None, "cap_usd": DAILY_CAP_USD, "pct": None}
    return {"spend_usd": round(spent, 2), "cap_usd": DAILY_CAP_USD,
            "pct": min(100, round(spent / DAILY_CAP_USD * 100)) if DAILY_CAP_USD else 0}


def vitals() -> dict:
    p = _latest_pull()
    base = {"source": str(p.relative_to(VAULT.parent.parent)) if p else None}
    base.update(parse_pull(p) if p else {"window": "—", "videos": [],
                "total_views": 0, "subs_gained": 0, "video_count": 0,
                "total_likes": 0, "total_comments": 0, "top_source": "—",
                "pull_date": "", "pull_age_days": None})
    base["claude"] = claude_spend()
    return base


# --- command deck (background jobs) --------------------------------------
def _run_job(job_id: str, cmd: list[str]) -> None:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        tail = (p.stdout or p.stderr or "").strip()[-400:]
        status, rc = ("done" if p.returncode == 0 else "failed"), p.returncode
    except Exception as e:
        tail, status, rc = f"{type(e).__name__}: {e}", "failed", -1
    with _LOCK:
        JOBS[job_id].update(status=status, rc=rc, tail=tail,
                            ended=dt.datetime.now().strftime("%H:%M:%S"))


def fire(key: str) -> dict:
    d = DECK_BY_KEY.get(key)
    if not d:
        return {"error": "unknown command"}
    if d["kind"] == "script":
        cmd = [str(VENV_PY), str(REPO / d["target"])]
    else:
        cmd = [CLAUDE_CLI, "--print", "--agent", d["target"], d["prompt"]]
    job_id = uuid.uuid4().hex[:8]
    with _LOCK:
        JOBS[job_id] = {"label": d["label"], "status": "running", "rc": None,
                        "tail": "", "started": dt.datetime.now().strftime("%H:%M:%S"),
                        "ended": None}
    threading.Thread(target=_run_job, args=(job_id, cmd), daemon=True).start()
    return {"id": job_id, "label": d["label"]}


def recent_jobs() -> list[dict]:
    with _LOCK:
        items = [{"id": k, **v} for k, v in JOBS.items()]
    return sorted(items, key=lambda j: j["started"], reverse=True)[:8]


# --- selftest (no server) -------------------------------------------------
def selftest() -> int:
    v = vitals()
    assert "videos" in v and isinstance(v["total_views"], int)
    p = _latest_pull()
    assert p is not None, f"no api-pull file in {ANALYTICS_DIR}"
    assert v["video_count"] >= 1, "parsed zero videos from a real pull"
    assert {d["key"] for d in DECK} == set(DECK_BY_KEY)
    print(f"selftest OK: {v['video_count']} videos, {v['total_views']} views, "
          f"window {v['window']}, claude {v['claude']}, deck {len(DECK)} buttons")
    return 0


# --- web app --------------------------------------------------------------
def build_app():
    from starlette.applications import Starlette
    from starlette.responses import HTMLResponse, JSONResponse
    from starlette.routing import Route

    async def home(_):
        return HTMLResponse(PAGE)

    async def api_vitals(_):
        return JSONResponse(vitals())

    async def api_fire(request):
        try:
            body = await request.json()
            key = body.get("key", "") if isinstance(body, dict) else ""
        except Exception:
            key = ""
        return JSONResponse(fire(key))

    async def api_jobs(_):
        return JSONResponse(recent_jobs())

    return Starlette(routes=[
        Route("/", home),
        Route("/api/vitals", api_vitals),
        Route("/api/fire", api_fire, methods=["POST"]),
        Route("/api/jobs", api_jobs),
    ])


PAGE = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>V.A.U.L.T.</title><style>
:root{--bg:#0d0f12;--fg:#e8e3d8;--dim:#6b6f76;--accent:#d2683f;--line:#23262b}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}
.wrap{max-width:1000px;margin:0 auto;padding:24px}
h1{font-size:18px;letter-spacing:.4em;margin:0 0 2px}
.sub{color:var(--dim);font-size:11px;margin-bottom:20px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.tile{border:1px solid var(--line);padding:14px;border-radius:6px}
.tile .k{color:var(--dim);font-size:10px;letter-spacing:.15em;text-transform:uppercase}
.tile .v{font-size:26px;margin-top:6px}.tile .v small{font-size:12px;color:var(--dim)}
.accent{color:var(--accent)}
h2{font-size:11px;letter-spacing:.2em;color:var(--dim);text-transform:uppercase;
margin:28px 0 10px;border-bottom:1px solid var(--line);padding-bottom:6px}
.deck{display:flex;flex-wrap:wrap;gap:10px}
button{background:#16191e;color:var(--fg);border:1px solid var(--line);
padding:10px 16px;border-radius:6px;font:inherit;cursor:pointer}
button:hover{border-color:var(--accent);color:var(--accent)}
table{width:100%;border-collapse:collapse;font-size:12px}
td,th{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line)}
th{color:var(--dim);font-weight:400;font-size:10px;letter-spacing:.1em;text-transform:uppercase}
.job{font-size:12px;padding:6px 8px;border-bottom:1px solid var(--line)}
.run{color:var(--accent)}.done{color:#6fae6f}.fail{color:#c4564a}
.foot{color:var(--dim);font-size:10px;margin-top:24px}
</style></head><body><div class=wrap>
<h1>V.A.U.L.T.</h1><div class=sub id=sub>3SK FINANCE · COMMAND CENTER</div>
<div class=grid id=tiles></div>
<h2>Command deck</h2><div class=deck id=deck></div>
<h2>Jobs</h2><div id=jobs></div>
<h2>Videos</h2><table id=vids><thead><tr><th>Title</th><th>Views</th><th>CTR</th>
<th>Avg %</th><th>Subs+</th><th>Likes</th></tr></thead><tbody></tbody></table>
<div class=foot id=foot></div></div>
<script>
async function j(u,o){return (await fetch(u,o)).json()}
function esc(s){return String(s).replace(/</g,'&lt;')}
function tile(k,v,s){return `<div class=tile><div class=k>${k}</div>
<div class=v>${v}${s?` <small>${s}</small>`:''}</div></div>`}
async function vitals(){
 const d=await j('/api/vitals');
 const c=d.claude||{};const cw=c.pct==null?'n/a':c.pct+'%';
 document.getElementById('tiles').innerHTML=
  tile('Total views',d.total_views)+
  tile('Subs gained',(d.subs_gained>=0?'+':'')+d.subs_gained,'window')+
  tile('Videos',d.video_count)+
  tile('Likes',d.total_likes)+
  tile('Comments',d.total_comments)+
  tile('Top source',`<span style=font-size:14px>${esc(d.top_source)}</span>`)+
  tile('Claude window',`<span class=accent>${cw}</span>`,c.spend_usd==null?'db n/a':`$${c.spend_usd}/$${c.cap_usd}`);
 document.getElementById('sub').textContent=
  `3SK FINANCE · COMMAND CENTER · pull ${d.pull_date||'—'}`+
  (d.pull_age_days!=null?` (${d.pull_age_days}d old)`:'');
 document.querySelector('#vids tbody').innerHTML=(d.videos||[]).map(v=>
  `<tr><td>${esc(v.title)}</td><td>${v.views}</td><td>${esc(v.ctr)}</td>
   <td>${esc(v.avg_pct)}</td><td>${v.subs}</td><td>${v.likes}</td></tr>`).join('');
 document.getElementById('foot').textContent='source: '+(d.source||'no pull file');
}
const DECK=%DECK%;
document.getElementById('deck').innerHTML=DECK.map(b=>
 `<button onclick="fire('${b.key}')">${b.label}</button>`).join('');
async function fire(k){await j('/api/fire',{method:'POST',
 headers:{'content-type':'application/json'},body:JSON.stringify({key:k})});jobs()}
async function jobs(){
 const js=await j('/api/jobs');
 document.getElementById('jobs').innerHTML=js.length?js.map(g=>{
  const cls=g.status=='running'?'run':g.status=='done'?'done':'fail';
  return `<div class=job><span class=${cls}>[${g.status}]</span> ${g.label}
   <span style=color:#6b6f76>· ${g.started}${g.ended?' → '+g.ended:''}</span>
   ${g.tail?`<div style=color:#6b6f76;font-size:11px;white-space:pre-wrap;margin-top:4px>${g.tail.replace(/</g,'&lt;')}</div>`:''}</div>`
 }).join(''):'<div style=color:#6b6f76>no jobs yet</div>';
}
vitals();jobs();setInterval(vitals,15000);setInterval(jobs,4000);
</script></body></html>"""


def main() -> int:
    if "--selftest" in sys.argv:
        return selftest()
    import uvicorn
    # inject the deck (labels+keys only) into the page
    deck_json = json.dumps([{"key": d["key"], "label": d["label"]} for d in DECK])
    globals()["PAGE"] = PAGE.replace("%DECK%", deck_json)
    print(f"V.A.U.L.T. on http://{HOST}:{PORT}  (localhost only)")
    uvicorn.run(build_app(), host=HOST, port=PORT, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
