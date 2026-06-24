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

    videos, sources, in_video, in_traffic = [], [], False, False
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
        elif in_traffic and len(cells) >= 2:
            sources.append({"src": cells[0], "views": _int(cells[1]) or 0})

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
        "sources": sources,
        "top_source": f"{sources[0]['src']} ({sources[0]['views']})" if sources else "—",
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
                "sources": [], "top_source": "—", "pull_date": "",
                "pull_age_days": None})
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
:root{--bg:#040605;--fg:#d3d8cf;--dim:#586057;--accent:#8df06a;--accent2:#c7ff9f;
--line:#161a16;--glow:rgba(141,240,106,.45)}
*{box-sizing:border-box}
body{margin:0;color:var(--fg);background:var(--bg);
font:12px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace;min-height:100vh}
body::after{content:"";position:fixed;inset:0;pointer-events:none;z-index:50;
background:repeating-linear-gradient(rgba(0,0,0,0) 0 2px,rgba(0,0,0,.16) 2px 3px)}
.wrap{max-width:1340px;margin:0 auto;padding:16px 20px 32px;position:relative;z-index:1}
/* top bar */
header{display:flex;align-items:center;justify-content:space-between;gap:16px;
border-bottom:1px solid var(--line);padding-bottom:10px}
.brand{font-size:22px;letter-spacing:.42em;font-weight:700;color:#eef2ea;
text-shadow:0 0 10px rgba(141,240,106,.18);white-space:nowrap}
.brand small{display:block;font-size:8px;letter-spacing:.3em;color:var(--dim);
text-shadow:none;font-weight:400;margin-top:3px}
.statusbar{flex:1;text-align:center;font-size:10px;letter-spacing:.28em;color:var(--dim);text-transform:uppercase}
.statusbar b{color:var(--accent2);font-weight:400}
.statusbar .dot{display:inline-block;width:6px;height:6px;border-radius:50%;background:#6fae6f;
margin:0 4px 0 14px;box-shadow:0 0 7px #6fae6f;animation:pulse 2s infinite;vertical-align:middle}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
#clock{font-size:22px;letter-spacing:.08em;color:#eef2ea;
text-shadow:0 0 8px rgba(141,240,106,.16);white-space:nowrap}
#clock small{font-size:11px;color:var(--dim);text-shadow:none}
/* 3-column cockpit */
.cols{display:grid;grid-template-columns:1fr 1.45fr 1fr;gap:14px;margin-top:14px}
@media(max-width:1000px){.cols{grid-template-columns:1fr}}
.col{display:flex;flex-direction:column;gap:14px}
.panel{position:relative;border:1px solid var(--line);padding:13px 14px;background:rgba(255,255,255,.01)}
.panel::before,.panel::after{content:"";position:absolute;width:11px;height:11px;border:1px solid var(--accent);opacity:.8}
.panel::before{top:-1px;left:-1px;border-right:0;border-bottom:0}
.panel::after{bottom:-1px;right:-1px;border-left:0;border-top:0}
.ptitle{font-size:9px;letter-spacing:.32em;color:#9aa39a;text-transform:uppercase;
margin-bottom:12px;display:flex;align-items:center;gap:8px}
.ptitle::before{content:"▸";color:var(--accent)}.ptitle::after{content:"";flex:1;height:1px;background:var(--line)}
/* vitals rows */
.stat{display:flex;align-items:baseline;justify-content:space-between;
padding:9px 0;border-bottom:1px solid var(--line)}
.stat:last-child{border-bottom:0}
.stat .k{font-size:9px;letter-spacing:.16em;color:var(--dim);text-transform:uppercase}
.stat .v{font-size:23px;color:var(--fg);text-shadow:0 0 10px var(--glow)}
.stat .d{font-size:10px;color:var(--accent2)}
.bar{height:4px;background:var(--line);margin-top:7px;overflow:hidden}
.bar>i{display:block;height:100%;background:var(--accent);box-shadow:0 0 8px var(--glow)}
.srcrow{display:flex;justify-content:space-between;font-size:11px;padding:5px 0;color:var(--dim)}
.srcrow b{color:var(--fg);font-weight:400}
/* center reactor */
.center{position:relative;border:1px solid var(--line);background:#000;min-height:520px;overflow:hidden}
.center::before,.center::after{content:"";position:absolute;width:13px;height:13px;border:1px solid var(--accent);opacity:.8;z-index:3}
.center::before{top:-1px;left:-1px;border-right:0;border-bottom:0}
.center::after{bottom:-1px;right:-1px;border-left:0;border-top:0}
#net{position:absolute;inset:0;width:100%;height:100%}
.cdir{position:absolute;bottom:120px;left:0;right:0;text-align:center;z-index:2;
font-size:9px;letter-spacing:.3em;color:#9aa39a;text-transform:uppercase}
.bignum{position:absolute;bottom:54px;left:0;right:0;text-align:center;z-index:2}
.bignum b{font-size:52px;font-weight:700;color:#fff;text-shadow:0 0 30px var(--glow);letter-spacing:.03em}
.bignum span{display:block;font-size:9px;letter-spacing:.45em;color:var(--dim);margin-top:6px}
.cfoot{position:absolute;bottom:16px;left:0;right:0;text-align:center;z-index:2;
font-size:9px;letter-spacing:.18em;color:var(--dim)}
/* command deck */
.deck{display:grid;grid-template-columns:1fr 1fr;gap:8px}
button{display:flex;align-items:center;gap:8px;background:rgba(141,240,106,.04);color:var(--fg);
border:1px solid var(--line);padding:9px 11px;font:inherit;font-size:11px;cursor:pointer;
letter-spacing:.04em;transition:.14s;text-align:left}
button:hover{border-color:var(--accent);color:var(--accent2);background:rgba(141,240,106,.12);box-shadow:0 0 12px var(--glow)}
button:active{transform:translateY(1px)}
button .g{color:var(--accent);font-size:10px}
.job{font-size:11px;padding:6px 0;border-bottom:1px solid var(--line)}
.job:last-child{border-bottom:0}
.run{color:var(--accent2)}.done{color:#6fae6f}.fail{color:#c4564a}
.jtail{color:var(--dim);font-size:10px;white-space:pre-wrap;margin-top:3px;max-height:60px;overflow:hidden}
/* telemetry table */
table{width:100%;border-collapse:collapse;font-size:11px}
td,th{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line)}
td:first-child{color:var(--fg)}td{color:var(--dim)}
th{color:var(--accent);font-weight:400;font-size:9px;letter-spacing:.14em;text-transform:uppercase}
tr:hover td{background:rgba(141,240,106,.04)}
.foot{color:var(--dim);font-size:9px;letter-spacing:.1em;margin-top:14px;text-align:right}
</style></head><body><div class=wrap>
<header>
 <div class=brand>V.A.U.L.T.<small>VITALS · AGENTS · UPLINK · LEDGER · TELEMETRY</small></div>
 <div class=statusbar id=statusbar>CORE · IDLE · LINK<span class=dot></span>ONLINE · RUNNER · ALIVE</div>
 <div id=clock>--:--:--</div>
</header>
<div class=cols>
 <div class=col>
  <section class=panel><div class=ptitle>System vitals</div><div id=vitals></div></section>
  <section class=panel><div class=ptitle>Traffic sources</div><div id=sources></div></section>
 </div>
 <div class=center>
  <div class=cdir id=cdir>Primary directive · road to —</div>
  <canvas id=net></canvas>
  <div class=bignum><b id=bigsubs>—</b><span>Subscribers</span></div>
  <div class=cfoot id=cfoot></div>
 </div>
 <div class=col>
  <section class=panel><div class=ptitle>Command deck</div><div class=deck id=deck></div></section>
  <section class=panel><div class=ptitle>Job queue</div><div id=jobs></div></section>
 </div>
</div>
<section class=panel style=margin-top:14px><div class=ptitle>Video telemetry</div>
<table id=vids><thead><tr><th>Title</th><th>Views</th><th>CTR</th>
<th>Avg %</th><th>AVD</th><th>Subs+</th><th>Likes</th><th>Comments</th></tr></thead><tbody></tbody></table></section>
<div class=foot id=foot></div></div>
<script>
async function j(u,o){return (await fetch(u,o)).json()}
function esc(s){return String(s).replace(/</g,'&lt;')}
function nf(n){return n==null?'n/a':n.toLocaleString()}
function stat(k,v,d){return `<div class=stat><div><div class=k>${k}</div>${d||''}</div>
 <div class=v>${v}</div></div>`}
function milestone(n){if(n==null)return 1000;
 const steps=[100,250,500,1000,2500,5000,10000,25000,50000,100000,250000,1000000];
 for(const s of steps)if(n<s)return s; return Math.ceil(n/1e6)*1e6}
function fmtK(n){return n>=1000?(n%1000?(n/1000).toFixed(1):n/1000)+'K':n}
async function vitals(){
 const d=await j('/api/vitals');const c=d.claude||{};
 const spend=c.spend_usd==null?'DB N/A':`$${c.spend_usd} / $${c.cap_usd}`;
 const cpct=c.pct==null?0:c.pct;
 document.getElementById('vitals').innerHTML=
  stat('YT Subscribers',nf(d.subscribers),
    d.subs_gained!=null?`<div class=d>${d.subs_gained>=0?'+':''}${d.subs_gained} /window</div>`:'')+
  stat('Total views',nf(d.total_views))+
  stat('Videos live',nf(d.video_count))+
  stat('Latest video',(d.videos&&d.videos[0])?nf(d.videos[0].views):'n/a',
    (d.videos&&d.videos[0])?`<div class=d>${esc(d.videos[0].ctr)} CTR</div>`:'')+
  `<div class=stat><div style=width:100%><div class=k>Claude $ / window</div>
    <div class=bar><i style=width:${cpct}%></i></div></div>
    <div class=v style=margin-left:12px>${c.pct==null?'n/a':cpct+'%'}</div></div>
   <div style=font-size:9px;color:var(--dim);text-align:right;margin-top:4px>${spend}</div>`;
 document.getElementById('sources').innerHTML=(d.sources||[]).length?
  d.sources.map(s=>`<div class=srcrow><span>${esc(s.src)}</span><b>${nf(s.views)}</b></div>`).join('')
  :'<div style=color:var(--dim)>no traffic data</div>';
 const goal=milestone(d.subscribers);
 document.getElementById('cdir').textContent=`Primary directive · road to ${fmtK(goal)} subs`;
 document.getElementById('bigsubs').textContent=nf(d.subscribers);
 document.getElementById('cfoot').textContent=
  `3SK FINANCE · PULL ${d.pull_date||'—'}`+(d.pull_age_days!=null?` (${d.pull_age_days}D OLD)`:'')
  +` · WINDOW ${d.window||'—'}`;
 document.getElementById('statusbar').innerHTML=
  `CORE · IDLE · LINK<span class=dot></span>ONLINE · <b>${nf(d.subscribers)}</b> SUBS · ALIVE`;
 document.querySelector('#vids tbody').innerHTML=(d.videos||[]).map(v=>
  `<tr><td>${esc(v.title)}</td><td>${v.views}</td><td>${esc(v.ctr)}</td>
   <td>${esc(v.avg_pct)}</td><td>${esc(v.avd)}</td><td>${v.subs}</td><td>${v.likes}</td><td>${v.comments}</td></tr>`).join('');
 document.getElementById('foot').textContent='SOURCE: '+(d.source||'no pull file');
}
/* command deck */
const DECK=%DECK%;
document.getElementById('deck').innerHTML=DECK.map(b=>
 `<button onclick="fire('${b.key}')"><span class=g>▸</span>${esc(b.label)}</button>`).join('');
async function fire(k){await j('/api/fire',{method:'POST',
 headers:{'content-type':'application/json'},body:JSON.stringify({key:k})});jobs()}
async function jobs(){
 const js=await j('/api/jobs');
 document.getElementById('jobs').innerHTML=js.length?js.map(g=>{
  const cls=g.status=='running'?'run':g.status=='done'?'done':'fail';
  return `<div class=job><span class=${cls}>[${g.status}]</span> ${esc(g.label)}
   <span style=color:var(--dim)>· ${g.started}${g.ended?' → '+g.ended:''}</span>
   ${g.tail?`<div class=jtail>${g.tail.replace(/</g,'&lt;')}</div>`:''}</div>`
 }).join(''):'<div style=color:var(--dim)>no jobs yet — fire a command</div>';
}
/* reactor: center-clustered particle network */
const cv=document.getElementById('net'),cx=cv.getContext('2d');let W,H,nodes;
function initNet(){const r=cv.getBoundingClientRect();W=cv.width=r.width;H=cv.height=r.height;
 const N=Math.max(40,Math.min(110,Math.floor(W*H/7000)));
 nodes=Array.from({length:N},()=>{const a=Math.random()*6.283,
  rad=Math.pow(Math.random(),.62)*Math.min(W,H)*0.43;
  return {x:W/2+Math.cos(a)*rad,y:H/2+Math.sin(a)*rad,
   vx:(Math.random()-.5)*.22,vy:(Math.random()-.5)*.22}})}
function drawNet(){if(!W)return;cx.clearRect(0,0,W,H);
 const g=cx.createRadialGradient(W/2,H/2,0,W/2,H/2,Math.min(W,H)*0.5);
 g.addColorStop(0,'rgba(120,240,90,.20)');g.addColorStop(.5,'rgba(90,200,70,.05)');g.addColorStop(1,'rgba(0,0,0,0)');
 cx.fillStyle=g;cx.fillRect(0,0,W,H);
 for(let i=0;i<nodes.length;i++){const a=nodes[i];a.x+=a.vx;a.y+=a.vy;
  if(a.x<0||a.x>W)a.vx*=-1;if(a.y<0||a.y>H)a.vy*=-1;
  for(let k=i+1;k<nodes.length;k++){const b=nodes[k],dx=a.x-b.x,dy=a.y-b.y,
   dd=Math.hypot(dx,dy);if(dd<88){cx.strokeStyle='rgba(130,235,95,'+(.14*(1-dd/88))+')';
   cx.beginPath();cx.moveTo(a.x,a.y);cx.lineTo(b.x,b.y);cx.stroke();}}}
 cx.shadowColor='rgba(170,255,120,.95)';cx.shadowBlur=7;cx.fillStyle='rgba(190,255,140,.95)';
 for(const a of nodes){cx.beginPath();cx.arc(a.x,a.y,1.4,0,6.283);cx.fill();}
 cx.shadowBlur=0;requestAnimationFrame(drawNet)}
function clock(){const t=new Date();
 document.getElementById('clock').innerHTML=t.toLocaleTimeString([],{hour12:false})
  +` <small>${String(t.getMilliseconds()).padStart(3,'0').slice(0,2)}</small>`}
initNet();drawNet();addEventListener('resize',initNet);
clock();vitals();jobs();
setInterval(clock,80);setInterval(vitals,15000);setInterval(jobs,4000);
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
