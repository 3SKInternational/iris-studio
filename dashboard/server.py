#!/usr/bin/env python3
"""V.A.U.L.T. — RETIRED 2026-06-26. Superseded by the always-on jarvis-hud
cockpit at http://127.0.0.1:3107 (launchd com.jarvis.hud), which absorbed this
dashboard's tabs (Fleet / Reports / Schedules / Approvals / Connectors) plus the
C-Suite org console. This port-8765 Starlette server is no longer launched by
launch.json or setup.sh and is kept only as reference for the merge. See
06_CEO/Designs/2026-06-26_Merge_VAULT_into_jarvis-hud.md.

V.A.U.L.T. — local command-center dashboard for Iris. Mini-only.

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


# --- tabs: agents / reports / schedules / ops / chat ---------------------
# Real data only — every tile traces to a file/command output, never fabricated.
ORG_CHART = REPO / "dashboard" / "org_chart.json"
AGENTS_DIR = Path.home() / ".claude" / "agents"
OUTPUTS = VAULT.parents[1]                      # /Users/steve/Documents/3SK/outputs
ADAPT = REPO / "scripts" / "adaptation.py"
REPORT_DIRS = ["06_CEO/Org_Briefs", "05_Research_and_Intelligence",
               "06_CEO/Designs", "06_CEO/Decisions_Log"]
CHAT_SESSION = {"id": str(uuid.uuid4()), "first": True}   # claude --resume thread
VOICE_PROC: dict[str, subprocess.Popen | None] = {"p": None}


def _agent_meta(stem: str) -> dict:
    """Frontmatter (model + description) for one agent def. exists=False if the
    org chart names a stem with no def on disk — surfaced, never hidden."""
    p = AGENTS_DIR / f"{stem}.md"
    if not stem or not p.exists():
        return {"stem": stem, "exists": False, "model": None, "desc": ""}
    model, desc, in_fm = None, "", False
    for i, line in enumerate(p.read_text(encoding="utf-8").splitlines()):
        if i == 0 and line.strip() == "---":
            in_fm = True; continue
        if in_fm and line.strip() == "---":
            break
        if in_fm and line.startswith("model:"):
            model = line.split(":", 1)[1].strip()
        elif in_fm and line.startswith("description:"):
            desc = line.split(":", 1)[1].strip()
    return {"stem": stem, "exists": True, "model": model, "desc": desc[:200]}


def org_fleet() -> dict:
    try:
        chart = json.loads(ORG_CHART.read_text(encoding="utf-8"))
    except Exception as e:
        return {"error": f"org_chart.json unreadable: {e}", "departments": []}
    ceo = chart.get("ceo", {})
    ceo_out = {**_agent_meta(ceo.get("agent", "")),
               "title": ceo.get("title"), "scope": ceo.get("scope"),
               "staff": [_agent_meta(s) for s in ceo.get("staff", [])]}
    depts = [{
        "key": d.get("key"), "title": d.get("title"), "domain": d.get("domain"),
        "lead": _agent_meta(d.get("lead", "")),
        "members": [_agent_meta(m) for m in d.get("members", [])],
    } for d in chart.get("departments", [])]
    return {"ceo": ceo_out, "departments": depts}


def list_reports() -> list[dict]:
    out = []
    for rel in REPORT_DIRS:
        base = OUTPUTS / rel
        if not base.exists():
            continue
        for p in base.rglob("*.md"):
            try:
                mt = p.stat().st_mtime
            except OSError:
                continue
            out.append({"path": str(p.relative_to(OUTPUTS)), "dir": rel,
                        "name": p.name,
                        "mtime": dt.datetime.fromtimestamp(mt).strftime("%Y-%m-%d %H:%M")})
    out.sort(key=lambda r: r["mtime"], reverse=True)
    return out[:60]


def read_report(rel: str) -> dict:
    """Path-guarded read: must resolve under OUTPUTS and be a .md file."""
    try:
        target = (OUTPUTS / rel).resolve()
        target.relative_to(OUTPUTS.resolve())
    except (ValueError, RuntimeError):
        return {"error": "path outside outputs"}
    if target.suffix != ".md" or not target.is_file():
        return {"error": "not a markdown file"}
    return {"path": rel, "text": target.read_text(encoding="utf-8")[:80000]}


def list_schedules() -> list[dict]:
    try:
        out = subprocess.run(["launchctl", "list"], capture_output=True,
                             text=True, timeout=10).stdout
    except Exception as e:
        return [{"label": f"launchctl error: {e}", "pid": "", "status": "", "ok": False}]
    rows = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and "com.iris" in parts[2]:   # our fleet, not Claude Desktop
            rows.append({"label": parts[2], "pid": parts[0], "status": parts[1],
                         "ok": parts[1] in ("0", "-")})
    rows.sort(key=lambda r: r["label"])
    return rows


def list_approvals() -> list[dict]:
    """Pending /adapt proposals (read-only — approval stays Steve's via Telegram)."""
    try:
        p = subprocess.run([str(VENV_PY), str(ADAPT), "--list", "--json"],
                           capture_output=True, text=True, timeout=30)
        data = json.loads(p.stdout or "[]")
    except Exception as e:
        return [{"id": "", "summary": f"adaptation error: {e}",
                 "target_name": "", "confidence": ""}]
    return [{"id": d.get("id"), "summary": (d.get("summary") or "")[:280],
             "target_name": d.get("target_name", ""),
             "confidence": d.get("confidence", ""),
             "signal_source": d.get("signal_source", "")} for d in data]


def _parse_connectors(out: str) -> list[dict]:
    """Parse `claude mcp list`. Format: "<name>: <url-or-command> - <status>".
    Split on the LAST " - " (status), then take the name before the first ": "
    (the value may be a URL or an stdio command — both kept, so stdio MCPs are
    not dropped). Names may contain colons (e.g. plugin:github:github)."""
    rows = []
    for line in out.splitlines():
        if " - " not in line or ": " not in line:
            continue
        left, _, status = line.rpartition(" - ")
        name = left.split(": ", 1)[0].strip()
        if not name or name.lower().startswith("checking"):
            continue
        rows.append({"name": name, "ok": ("✔" in status or "Connected" in status)})
    return rows


def list_connectors() -> list[dict]:
    try:
        out = subprocess.run([CLAUDE_CLI, "mcp", "list"], capture_output=True,
                             text=True, timeout=30).stdout
    except Exception as e:
        return [{"name": f"mcp list error: {e}", "ok": False}]
    return _parse_connectors(out)


def chat(text: str, history: list[dict]) -> dict:
    """Same hybrid brain as voice_chat: Ollama for chatter, claude CLI when the
    turn names an agent / a hard verb."""
    text = (text or "").strip()
    if not text:
        return {"reply": "", "via": "none"}
    sys.path.insert(0, str(REPO / "voice"))
    try:
        import voice_chat as vc
    except Exception as e:
        return {"reply": f"voice router unavailable: {e}", "via": "error"}
    try:
        agents = vc.agent_names()
        if vc.should_escalate(text, agents):
            with _LOCK:                       # claim the first-turn slot atomically
                first = CHAT_SESSION["first"]
                if first:
                    CHAT_SESSION["first"] = False
            reply = vc.ask_claude(text, CHAT_SESSION["id"], first)
            if reply is None and first:       # creation failed → reopen first slot
                with _LOCK:
                    CHAT_SESSION["first"] = True
            return {"reply": reply or "(the full brain didn't answer)", "via": "claude"}
        reply = vc.ask_ollama(text, history[-8:] if isinstance(history, list) else [])
        return {"reply": reply, "via": "ollama"}
    except Exception as e:
        return {"reply": f"chat error: {e}", "via": "error"}


def voice_start() -> dict:
    """Launch the existing voice listener (mic → STT → router → TTS) detached.
    Locked so two near-simultaneous starts can't pile up mic listeners."""
    script = REPO / "voice" / "voice_chat.py"
    if not script.exists():
        return {"error": "voice_chat.py not found"}
    with _LOCK:
        vp = VOICE_PROC["p"]
        if vp is not None and vp.poll() is None:
            return {"pid": vp.pid, "status": "already running"}
        try:
            p = subprocess.Popen([str(VENV_PY), str(script)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
        except Exception as e:
            return {"error": str(e)}
        VOICE_PROC["p"] = p
        return {"pid": p.pid, "status": "launched"}


# --- selftest (no server) -------------------------------------------------
def selftest() -> int:
    v = vitals()
    assert "videos" in v and isinstance(v["total_views"], int)
    p = _latest_pull()
    assert p is not None, f"no api-pull file in {ANALYTICS_DIR}"
    assert v["video_count"] >= 1, "parsed zero videos from a real pull"
    assert {d["key"] for d in DECK} == set(DECK_BY_KEY)
    # tabs: org chart parses, every named stem resolves to a real def (or is
    # honestly flagged), reports glob, and the path guard rejects traversal.
    fleet = org_fleet()
    assert "error" not in fleet, fleet.get("error")
    assert fleet["ceo"]["exists"], "CEO def missing on disk"
    missing = [m["stem"] for d in fleet["departments"] for m in [d["lead"], *d["members"]]
               if not m["exists"]]
    assert not missing, f"org chart names defs with no file: {missing}"
    assert isinstance(list_reports(), list)
    assert "error" in read_report("../../etc/passwd"), "path guard let traversal through"
    # connector parser keeps stdio commands + colon-in-name, not just URLs
    conn = _parse_connectors(
        "Checking MCP server health…\n"
        "filesystem-vault: npx -y server-filesystem /x - ✔ Connected\n"
        "claude.ai Gmail: https://gmailmcp.googleapis.com/mcp/v1 - ✔ Connected\n"
        "plugin:github:github: https://api.githubcopilot.com/mcp/ (HTTP) - ✘ Failed\n")
    names = {c["name"]: c["ok"] for c in conn}
    assert names == {"filesystem-vault": True, "claude.ai Gmail": True,
                     "plugin:github:github": False}, f"connector parse wrong: {names}"
    # CSRF guard: token-spending POSTs must reject a request with no X-Vault header
    from starlette.testclient import TestClient
    tc = TestClient(build_app())
    assert tc.post("/api/fire", json={"key": "metrics"}).status_code == 403
    assert tc.post("/api/voice/start").status_code == 403
    assert tc.post("/api/fire", json={"key": "nope"},
                   headers={"x-vault": "1"}).status_code == 200
    print(f"selftest OK: {v['video_count']} videos, {v['total_views']} views, "
          f"window {v['window']}, claude {v['claude']}, deck {len(DECK)} buttons, "
          f"{len(fleet['departments'])} depts, {len(list_reports())} reports")
    return 0


# --- web app --------------------------------------------------------------
def build_app():
    from starlette.applications import Starlette
    from starlette.responses import HTMLResponse, JSONResponse
    from starlette.routing import Route

    def _csrf_ok(request) -> bool:
        # CSRF guard for the state-changing / token-spending POSTs. Same-origin
        # page JS sets X-Vault:1; a cross-origin page cannot set a custom header
        # without a CORS preflight this server never approves, so a malicious
        # site can't fire agents / start the mic via the user's browser.
        # ponytail: localhost API, custom-header guard; add a real token before
        # any 0.0.0.0 bind (see module header).
        return request.headers.get("x-vault") == "1"

    async def home(_):
        return HTMLResponse(PAGE)

    async def api_vitals(_):
        return JSONResponse(vitals())

    async def api_fire(request):
        if not _csrf_ok(request):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        try:
            body = await request.json()
            key = body.get("key", "") if isinstance(body, dict) else ""
        except Exception:
            key = ""
        return JSONResponse(fire(key))

    async def api_jobs(_):
        return JSONResponse(recent_jobs())

    async def api_agents(_):
        return JSONResponse(org_fleet())

    async def api_reports(_):
        return JSONResponse(list_reports())

    async def api_report(request):
        return JSONResponse(read_report(request.query_params.get("path", "")))

    async def api_schedules(_):
        return JSONResponse(list_schedules())

    async def api_approvals(_):
        return JSONResponse(list_approvals())

    async def api_connectors(_):
        return JSONResponse(list_connectors())

    async def api_chat(request):
        if not _csrf_ok(request):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        try:
            body = await request.json()
        except Exception:
            body = {}
        text = body.get("text", "") if isinstance(body, dict) else ""
        hist = body.get("history", []) if isinstance(body, dict) else []
        return JSONResponse(chat(text, hist))

    async def api_voice_start(request):
        if not _csrf_ok(request):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        return JSONResponse(voice_start())

    return Starlette(routes=[
        Route("/", home),
        Route("/api/vitals", api_vitals),
        Route("/api/fire", api_fire, methods=["POST"]),
        Route("/api/jobs", api_jobs),
        Route("/api/agents", api_agents),
        Route("/api/reports", api_reports),
        Route("/api/report", api_report),
        Route("/api/schedules", api_schedules),
        Route("/api/approvals", api_approvals),
        Route("/api/connectors", api_connectors),
        Route("/api/chat", api_chat, methods=["POST"]),
        Route("/api/voice/start", api_voice_start, methods=["POST"]),
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
/* tabs */
.tabs{display:flex;gap:2px;margin-top:14px;flex-wrap:wrap;border-bottom:1px solid var(--line)}
.tabs button{border:1px solid var(--line);border-bottom:0;background:rgba(255,255,255,.01);
padding:9px 16px;font-size:10px;letter-spacing:.2em;text-transform:uppercase;color:var(--dim);width:auto}
.tabs button.on{color:var(--accent2);border-color:var(--accent);background:rgba(141,240,106,.09);
box-shadow:0 -2px 10px var(--glow)}
.tab{display:none;margin-top:14px}.tab.active{display:block}
/* agents org chart */
.org{display:flex;flex-direction:column;gap:14px}
.ceo-card,.dept{border:1px solid var(--line);padding:13px 14px;background:rgba(255,255,255,.01);position:relative}
.ceo-card{border-color:var(--accent)}
.lead{color:var(--accent2);font-size:13px;letter-spacing:.08em}
.deptgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:10px;margin-top:11px}
.agent{border:1px solid var(--line);padding:8px 10px}
.agent .nm{color:var(--fg);font-size:11px}
.agent .md{font-size:8px;letter-spacing:.1em;padding:1px 6px;border:1px solid var(--line);
color:var(--accent);text-transform:uppercase;white-space:nowrap}
.agent .ds{color:var(--dim);font-size:10px;margin-top:4px;line-height:1.4}
.miss{color:#c4564a;border-color:#c4564a}
.row2{display:flex;justify-content:space-between;align-items:center;gap:8px}
/* report viewer */
.repwrap{display:grid;grid-template-columns:310px 1fr;gap:14px}
@media(max-width:820px){.repwrap{grid-template-columns:1fr}}
.replist{max-height:620px;overflow:auto;border:1px solid var(--line)}
.repitem{padding:8px 10px;border-bottom:1px solid var(--line);cursor:pointer;font-size:11px;color:var(--fg)}
.repitem:hover{background:rgba(141,240,106,.07);color:var(--accent2)}
.repitem .mt{color:var(--dim);font-size:9px;margin-top:2px}
#repview{white-space:pre-wrap;font-size:11px;color:var(--fg);max-height:620px;overflow:auto;
border:1px solid var(--line);padding:14px;background:#000}
/* chat */
.chatlog{border:1px solid var(--line);background:#000;height:440px;overflow:auto;padding:14px;
display:flex;flex-direction:column;gap:10px}
.msg{max-width:82%;padding:8px 11px;border:1px solid var(--line);font-size:12px;white-space:pre-wrap}
.msg.you{align-self:flex-end;border-color:var(--accent);color:var(--accent2)}
.msg.iris{align-self:flex-start;color:var(--fg)}
.msg .tag{font-size:8px;letter-spacing:.15em;color:var(--dim);text-transform:uppercase;display:block;margin-bottom:3px}
.chatin{display:flex;gap:8px;margin-top:10px}
.chatin input{flex:1;background:#000;border:1px solid var(--line);color:var(--fg);padding:11px;font:inherit}
.chatin input:focus{outline:0;border-color:var(--accent)}
.chatin button{width:auto;padding:0 18px}
.vbtn{border-color:var(--accent);color:var(--accent2)}
.statline{font-size:9px;color:var(--dim);margin-top:8px;letter-spacing:.1em}
</style></head><body><div class=wrap>
<header>
 <div class=brand>V.A.U.L.T.<small>VITALS · AGENTS · UPLINK · LEDGER · TELEMETRY</small></div>
 <div class=statusbar id=statusbar>CORE · IDLE · LINK<span class=dot></span>ONLINE · RUNNER · ALIVE</div>
 <div id=clock>--:--:--</div>
</header>
<nav class=tabs id=tabs>
 <button class=on data-t=dashboard>Dashboard</button>
 <button data-t=agents>Agents</button>
 <button data-t=reports>Reports</button>
 <button data-t=schedules>Schedules</button>
 <button data-t=ops>Approvals · Connectors</button>
 <button data-t=chat>CEO Chat</button>
</nav>

<section id=tab-dashboard class="tab active">
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
<div class=foot id=foot></div>
</section>

<section id=tab-agents class=tab>
 <section class=panel><div class=ptitle>Org chart · C-suite oversees the fleet (each lead recommends, dispatch stays with Steve)</div>
  <div id=org class=org>loading…</div></section>
</section>

<section id=tab-reports class=tab>
 <section class=panel><div class=ptitle>Department briefs &amp; research deliverables</div>
  <div class=repwrap>
   <div class=replist id=replist>loading…</div>
   <div id=repview>select a report</div>
  </div></section>
</section>

<section id=tab-schedules class=tab>
 <section class=panel><div class=ptitle>Scheduled jobs (launchd · last exit)</div>
  <div id=sched>loading…</div></section>
</section>

<section id=tab-ops class=tab>
 <div class=cols style=grid-template-columns:1.4fr_1fr>
  <section class=panel><div class=ptitle>Approvals · pending /adapt proposals (approve via Telegram /adapt)</div>
   <div id=approvals>loading…</div></section>
  <section class=panel><div class=ptitle>Connectors · MCP servers</div>
   <div id=connectors>loading…</div></section>
 </div>
</section>

<section id=tab-chat class=tab>
 <section class=panel><div class=ptitle>CEO chat · text + voice (Ollama for chatter, full Claude brain when you name an agent / a task)</div>
  <div class=chatlog id=chatlog></div>
  <div class=chatin>
   <input id=chatinp placeholder="message the CEO… e.g. 'have the channel-analyst read the latest pull'" autocomplete=off>
   <button onclick=sendChat()>Send</button>
   <button class=vbtn onclick=startVoice()>● Voice</button>
  </div>
  <div class=statline id=voicestat>voice button launches the local mic listener — then say "Iris …"</div>
 </section>
</section>

</div>
<script>
async function j(u,o){return (await fetch(u,o)).json()}
async function post(u,body){return j(u,{method:'POST',
 headers:{'content-type':'application/json','x-vault':'1'},body:JSON.stringify(body||{})})}
const ESCM={'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'};
function esc(s){return String(s).replace(/[&<>"']/g,c=>ESCM[c])}
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
async function fire(k){await post('/api/fire',{key:k});jobs()}
async function jobs(){
 const js=await j('/api/jobs');
 document.getElementById('jobs').innerHTML=js.length?js.map(g=>{
  const cls=g.status=='running'?'run':g.status=='done'?'done':'fail';
  return `<div class=job><span class=${cls}>[${g.status}]</span> ${esc(g.label)}
   <span style=color:var(--dim)>· ${g.started}${g.ended?' → '+g.ended:''}</span>
   ${g.tail?`<div class=jtail>${esc(g.tail)}</div>`:''}</div>`
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
/* tabs */
const loaded={};
const LOADERS={agents:loadAgents,reports:loadReports,schedules:loadSchedules,ops:loadOps};
function show(t){
 document.querySelectorAll('.tabs button').forEach(b=>b.classList.toggle('on',b.dataset.t===t));
 document.querySelectorAll('.tab').forEach(s=>s.classList.toggle('active',s.id==='tab-'+t));
 if(!loaded[t]){loaded[t]=1;(LOADERS[t]||function(){})();}
}
document.getElementById('tabs').addEventListener('click',e=>{if(e.target.dataset.t)show(e.target.dataset.t)});
/* agents */
function agentCard(a){
 const md=a.exists?`<span class=md>${esc(a.model||'?')}</span>`:'<span class="md miss">missing</span>';
 return `<div class="agent${a.exists?'':' miss'}"><div class=row2><span class=nm>${esc(a.stem)}</span>${md}</div>
  <div class=ds>${esc(a.desc||'')}</div></div>`;
}
async function loadAgents(){
 const d=await j('/api/agents');const el=document.getElementById('org');
 if(d.error){el.innerHTML='<div class=miss>'+esc(d.error)+'</div>';return}
 const c=d.ceo||{};
 const cmd=c.exists?`<span class=md>${esc(c.model||'?')}</span>`:'<span class="md miss">missing</span>';
 let h=`<div class=ceo-card><div class=row2><span class=lead>${esc(c.stem)} · ${esc(c.title||'CEO')}</span>${cmd}</div>
  <div class=ds>${esc(c.scope||'')}</div>
  <div class=deptgrid>${(c.staff||[]).map(agentCard).join('')}</div></div>`;
 h+=(d.departments||[]).map(dp=>{
  const lm=dp.lead.exists?`<span class=md>${esc(dp.lead.model||'?')}</span>`:'<span class="md miss">missing</span>';
  return `<div class=dept><div class=row2><span class=lead>${esc(dp.lead.stem)} · ${esc(dp.title)} — ${esc(dp.domain)}</span>${lm}</div>
   <div class=ds>${esc(dp.lead.desc||'')}</div>
   <div class=deptgrid>${dp.members.map(agentCard).join('')}</div></div>`;
 }).join('');
 el.innerHTML=h;
}
/* reports */
async function loadReports(){
 const d=await j('/api/reports');
 document.getElementById('replist').innerHTML=d.length?d.map(r=>
  `<div class=repitem onclick="openRep('${encodeURIComponent(r.path)}')">${esc(r.name)}
   <div class=mt>${esc(r.dir)} · ${esc(r.mtime)}</div></div>`).join('')
  :'<div style=padding:10px;color:var(--dim)>no reports yet</div>';
}
async function openRep(p){
 const d=await j('/api/report?path='+p);
 document.getElementById('repview').textContent=d.error?('['+d.error+']'):d.text;
}
/* schedules */
async function loadSchedules(){
 const d=await j('/api/schedules');
 document.getElementById('sched').innerHTML='<table><thead><tr><th>Job</th><th>PID</th><th>Last exit</th></tr></thead><tbody>'+
  d.map(s=>`<tr><td>${esc(s.label)}</td><td>${esc(s.pid)}</td>
   <td class=${s.ok?'done':'fail'}>${esc(s.status)}</td></tr>`).join('')+'</tbody></table>';
}
/* ops: approvals + connectors */
async function loadOps(){
 const ap=await j('/api/approvals');const co=await j('/api/connectors');
 document.getElementById('approvals').innerHTML=ap.length&&ap[0].id?ap.map(a=>
  `<div class=repitem style=cursor:default><div class=row2><b style=color:var(--accent2)>${esc(a.id)}</b><span class=md>${esc(a.confidence)}</span></div>
   <div class=ds>${esc(a.summary)}</div><div class=mt>→ ${esc(a.target_name)}</div></div>`).join('')
  :'<div style=color:var(--dim)>queue empty</div>';
 document.getElementById('connectors').innerHTML=co.length?co.map(c=>
  `<div class=srcrow><span>${esc(c.name)}</span><b class=${c.ok?'done':'fail'}>${c.ok?'connected':'failed'}</b></div>`).join('')
  :'<div style=color:var(--dim)>none</div>';
}
/* chat */
const chatHist=[];
function addMsg(who,text,tag){
 const log=document.getElementById('chatlog');
 const div=document.createElement('div');div.className='msg '+who;
 div.innerHTML=(tag?`<span class=tag>${esc(tag)}</span>`:'')+esc(text);
 log.appendChild(div);log.scrollTop=log.scrollHeight;return div;
}
async function sendChat(){
 const inp=document.getElementById('chatinp');const t=inp.value.trim();if(!t)return;
 inp.value='';addMsg('you',t);const ph=addMsg('iris','…','thinking');
 try{
  const d=await post('/api/chat',{text:t,history:chatHist});
  ph.remove();addMsg('iris',d.reply||'(no reply)',d.via);
  chatHist.push({role:'user',content:t},{role:'assistant',content:d.reply||''});
 }catch(e){ph.remove();addMsg('iris','request failed: '+e,'error');}
}
async function startVoice(){
 const s=document.getElementById('voicestat');s.textContent='launching voice listener…';
 const d=await post('/api/voice/start');
 s.textContent=d.error?('voice: '+d.error)
  :('voice listener '+(d.status||'launched')+' (pid '+d.pid+') — say "Iris …" to the mic');
}
document.getElementById('chatinp').addEventListener('keydown',e=>{if(e.key==='Enter')sendChat()});
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
