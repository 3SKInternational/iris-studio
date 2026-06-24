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
    subs = (re.search(r"^subscribers:\s*(.+)$", text, re.M) or [None, ""])[1].strip()
    chvids = (re.search(r"^channel_videos:\s*(.+)$", text, re.M) or [None, ""])[1].strip()

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
        "subscribers": _int(subs),          # channel total (None if "n/a"/missing)
        "total_likes": sum(v["likes"] for v in videos),
        "total_comments": sum(v["comments"] for v in videos),
        # Channel upload count (truth) falls back to parsed-row count if absent.
        "video_count": _int(chvids) if _int(chvids) is not None else len(videos),
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
                "total_views": 0, "subs_gained": 0, "subscribers": None,
                "video_count": 0, "total_likes": 0, "total_comments": 0,
                "top_source": "—", "pull_date": "", "pull_age_days": None})
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
:root{--bg:#08090b;--panel:#0c0e11;--fg:#e8e3d8;--dim:#6b6f76;
--accent:#d2683f;--accent2:#f0a878;--line:#1c1f24;--glow:rgba(210,104,63,.55)}
*{box-sizing:border-box}
body{margin:0;color:var(--fg);font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;
background:
 linear-gradient(rgba(210,104,63,.025) 1px,transparent 1px) 0 0/100% 34px,
 linear-gradient(90deg,rgba(210,104,63,.025) 1px,transparent 1px) 0 0/34px 100%,
 radial-gradient(120% 80% at 50% -10%,#15110d 0%,var(--bg) 60%);
min-height:100vh}
/* scanline overlay */
body::after{content:"";position:fixed;inset:0;pointer-events:none;z-index:9;
background:repeating-linear-gradient(rgba(0,0,0,0) 0 2px,rgba(0,0,0,.18) 2px 3px)}
.wrap{max-width:1100px;margin:0 auto;padding:22px 24px 40px;position:relative;z-index:1}
/* header */
header{display:flex;align-items:flex-end;justify-content:space-between;
border-bottom:1px solid var(--line);padding-bottom:12px;margin-bottom:22px}
.brand{font-size:26px;letter-spacing:.5em;font-weight:700;
color:var(--accent);text-shadow:0 0 18px var(--glow)}
.brand small{display:block;font-size:9px;letter-spacing:.35em;color:var(--dim);
text-shadow:none;font-weight:400;margin-top:4px}
.status{text-align:right;font-size:10px;letter-spacing:.18em;color:var(--dim);text-transform:uppercase}
.status #clock{color:var(--accent2);font-size:18px;letter-spacing:.1em;display:block;margin-bottom:3px}
.dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:#6fae6f;
margin-right:6px;box-shadow:0 0 8px #6fae6f;animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}
/* HUD panels with corner brackets */
.panel{position:relative;background:linear-gradient(180deg,rgba(255,255,255,.012),transparent);
border:1px solid var(--line);padding:16px 18px;margin-bottom:18px}
.panel::before,.panel::after{content:"";position:absolute;width:14px;height:14px;
border:2px solid var(--accent)}
.panel::before{top:-1px;left:-1px;border-right:0;border-bottom:0}
.panel::after{bottom:-1px;right:-1px;border-left:0;border-top:0}
.ptitle{font-size:10px;letter-spacing:.3em;color:var(--accent);text-transform:uppercase;
margin-bottom:14px;display:flex;align-items:center;gap:8px}
.ptitle::before{content:"▸"}
.ptitle::after{content:"";flex:1;height:1px;background:var(--line)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.tile{border:1px solid var(--line);padding:14px;background:rgba(255,255,255,.015);position:relative}
.tile::before{content:"";position:absolute;top:6px;left:6px;width:6px;height:6px;
border-top:1px solid var(--accent);border-left:1px solid var(--accent);opacity:.6}
.tile .k{color:var(--dim);font-size:9px;letter-spacing:.18em;text-transform:uppercase}
.tile .v{font-size:30px;margin-top:8px;color:var(--accent2);text-shadow:0 0 12px var(--glow)}
.tile .v small{font-size:11px;color:var(--dim);text-shadow:none}
.accent{color:var(--accent)}
/* claude ring gauge */
.ring{display:flex;align-items:center;gap:14px}
.gauge{width:62px;height:62px;border-radius:50%;flex:0 0 62px;display:grid;place-items:center}
.hole{width:48px;height:48px;border-radius:50%;background:var(--panel);display:grid;place-items:center;
font-size:14px;color:var(--accent2);text-shadow:0 0 10px var(--glow)}
.deck{display:flex;flex-wrap:wrap;gap:10px}
button{background:rgba(210,104,63,.06);color:var(--fg);border:1px solid var(--line);
padding:11px 18px;font:inherit;cursor:pointer;letter-spacing:.05em;transition:.15s;position:relative}
button:hover{border-color:var(--accent);color:var(--accent2);
box-shadow:0 0 14px var(--glow);background:rgba(210,104,63,.12)}
button:active{transform:translateY(1px)}
table{width:100%;border-collapse:collapse;font-size:12px}
td,th{text-align:left;padding:8px;border-bottom:1px solid var(--line)}
td:first-child{color:var(--fg)}td{color:var(--dim)}
th{color:var(--accent);font-weight:400;font-size:9px;letter-spacing:.15em;text-transform:uppercase}
tr:hover td{background:rgba(210,104,63,.04)}
.job{font-size:12px;padding:7px 8px;border-bottom:1px solid var(--line)}
.run{color:var(--accent2)}.done{color:#6fae6f}.fail{color:#c4564a}
.foot{color:var(--dim);font-size:9px;letter-spacing:.1em;margin-top:18px;text-align:right}
</style></head><body><div class=wrap>
<header>
 <div class=brand>V.A.U.L.T.<small>VITALS · AGENTS · UPLINK · LEDGER · TELEMETRY</small></div>
 <div class=status><span id=clock>--:--:--</span>
  <div><span class=dot></span>SYSTEM ONLINE</div>
  <div id=sub>3SK FINANCE · COMMAND CENTER</div></div>
</header>
<section class=panel><div class=ptitle>Channel vitals</div><div class=grid id=tiles></div></section>
<section class=panel><div class=ptitle>Command deck</div><div class=deck id=deck></div></section>
<section class=panel><div class=ptitle>Job queue</div><div id=jobs></div></section>
<section class=panel><div class=ptitle>Video telemetry</div>
<table id=vids><thead><tr><th>Title</th><th>Views</th><th>CTR</th>
<th>Avg %</th><th>AVD</th><th>Subs+</th><th>Likes</th></tr></thead><tbody></tbody></table></section>
<div class=foot id=foot></div></div>
<script>
async function j(u,o){return (await fetch(u,o)).json()}
function esc(s){return String(s).replace(/</g,'&lt;')}
function tile(k,v,s){return `<div class=tile><div class=k>${k}</div>
<div class=v>${v}${s?` <small>${s}</small>`:''}</div></div>`}
function ring(c){
 const p=c.pct==null?0:c.pct;
 const sub=c.spend_usd==null?'DB N/A':`$${c.spend_usd} / $${c.cap_usd}`;
 return `<div class="tile ring">
  <div class=gauge style="background:conic-gradient(var(--accent) ${p*3.6}deg,var(--line) 0)">
   <div class=hole>${c.pct==null?'n/a':p+'%'}</div></div>
  <div><div class=k>Claude window</div><div style="font-size:11px;color:var(--dim);margin-top:6px">${sub}</div></div>
 </div>`;
}
function clock(){document.getElementById('clock').textContent=
 new Date().toLocaleTimeString([],{hour12:false})}
async function vitals(){
 const d=await j('/api/vitals');const c=d.claude||{};
 document.getElementById('tiles').innerHTML=
  tile('Total views',d.total_views)+
  tile('Subscribers',d.subscribers==null?'n/a':d.subscribers,
       d.subs_gained!=null?`${d.subs_gained>=0?'+':''}${d.subs_gained} window`:'')+
  tile('Videos',d.video_count)+
  tile('Likes',d.total_likes)+
  tile('Comments',d.total_comments)+
  tile('Top source',`<span style="font-size:15px">${esc(d.top_source)}</span>`)+
  ring(c);
 document.getElementById('sub').textContent=
  `3SK FINANCE · PULL ${d.pull_date||'—'}`+
  (d.pull_age_days!=null?` (${d.pull_age_days}D OLD)`:'');
 document.querySelector('#vids tbody').innerHTML=(d.videos||[]).map(v=>
  `<tr><td>${esc(v.title)}</td><td>${v.views}</td><td>${esc(v.ctr)}</td>
   <td>${esc(v.avg_pct)}</td><td>${esc(v.avd)}</td><td>${v.subs}</td><td>${v.likes}</td></tr>`).join('');
 document.getElementById('foot').textContent='SOURCE: '+(d.source||'no pull file');
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
clock();vitals();jobs();
setInterval(clock,1000);setInterval(vitals,15000);setInterval(jobs,4000);
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
