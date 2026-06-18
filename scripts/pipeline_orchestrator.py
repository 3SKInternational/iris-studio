#!/usr/bin/env python3
"""Pipeline Orchestrator — the deterministic video-production sequencer.

For a given Video NN, reads per-video state, runs the SINGLE next ready
non-gate stage, updates state, emits one Telegram line, and STOPS at the next
human/billed gate. It sequences the 11 already-built stages. It is a
sequencer, NOT a swarm: no agent-to-agent messaging, no autonomous spend, no
autonomous publish, no autonomous VO.

The no-autonomous-action invariant is STRUCTURAL (not configured): the stage
selector draws ONLY from {ready AND gate==false AND owner==orchestrator AND in
RUN_TABLE}. Billed (5 images), publish (10), and VO (3/4) stages carry
gate:true / owner:steve and are never in that set. The sole money-spending path
is the explicit, human-typed `--spend-ok` (CLI) / `/pipeline NN spend-ok`
(Telegram) command, which is not routable by the cloud model.

Design: 06_CEO/Designs/2026-06-18_Pipeline_Orchestrator_Architecture.md
CLI:
  python scripts/pipeline_orchestrator.py --video 5 --advance
  python scripts/pipeline_orchestrator.py --video 5 --status
  python scripts/pipeline_orchestrator.py --video 5 --init [--title "..."]
  python scripts/pipeline_orchestrator.py --video 5 --spend-ok
  python scripts/pipeline_orchestrator.py --video 5 --force-reset

Stdlib only (json, fcntl, subprocess, ...) — no third-party deps, so it runs
under any python3 regardless of the daemon's venv.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# === Paths (absolute; never assume cwd) ===
ROOT = Path("/Volumes/AI_Workspace/iris_studio")          # tools + repo
WORKSPACE_DIR = Path("/Users/steve/Documents/3SK/outputs")  # vault (agent cwd)
VAULT_REL = "BRANDS/3SK_Finance"                           # brand subtree
STATE_DIR = WORKSPACE_DIR / VAULT_REL / "Production_Kits"
NOTIFY_SH = ROOT / "scripts" / "notify.sh"
# Host-local lock dir (flock is per-host; keep it OUT of the synced vault).
LOCK_DIR = Path(os.path.expanduser("~/iris_studio/pipeline_locks"))

# Resolve the claude CLI the same way the daemon does (iris.py).
CLAUDE_CLI_PATH = (
    os.environ.get("CLAUDE_CLI_PATH")
    or ("/opt/homebrew/bin/claude" if Path("/opt/homebrew/bin/claude").exists() else None)
    or shutil.which("claude")
    or "claude"
)

MAX_FAILS = 3  # consecutive failures → needs-steve (park_reason "failed"), not auto-retry
SCHEMA_VERSION = 1

# === The run table — the security + invocation envelope ===
# A hardcoded dict: the analogue of the daemon's DISPATCH_AGENTS enum. A stage
# NOT in this table (or marked gate) is never auto-run by --advance.
#  kind: "agent"  -> dispatch a claude subagent (Read/Write tools only)
#        "script" -> shell a free, local factory tool
#        "billed" -> shell the billing-capable image tool; ONLY reachable via
#                    --spend-ok / /pipeline NN spend-ok, NEVER via --advance.
RUN_TABLE: dict[str, dict] = {
    "1_script":      {"kind": "agent",  "agent": "scriptwriter",             "timeout": 1200},
    "2_review":      {"kind": "agent",  "agent": "script-reviewer",          "timeout": 480},
    "5_images":      {"kind": "billed", "agent": None,                       "timeout": 3600},
    "6_assemble":    {"kind": "script", "agent": None,                       "timeout": 1800},
    "7_packaging":   {"kind": "agent",  "agent": "packaging-strategist",     "timeout": 480},
    "9_description": {"kind": "agent",  "agent": "video-description-writer",  "timeout": 600},
    "11_analyze":    {"kind": "agent",  "agent": "channel-analyst",          "timeout": 720},
}

# Human-artifact gates that CAN auto-promote needs-steve→done when a real
# artifact lands on disk (non-empty AND fresher than deps). Only gates with a
# fixed, unambiguous on-disk artifact convention are listed. Gates NOT here
# (3 vo_expand, 8 thumbnail, 10 publish) have no fixed local artifact path, so
# they NEVER auto-promote — Steve clears them by hand-editing status:done (the
# always-available manual exit). The orchestrator never guesses.
#   path_tmpl: vault-relative, "NN" is the zero-padded video number
#   kind: "dir" (≥1 file of `ext`, total size>0) | "file" (size>0)
GATE_ARTIFACTS: dict[str, dict] = {
    "4_vo_record": {"path_tmpl": f"{VAULT_REL}/Voice_Files/Video_NN", "kind": "dir", "ext": ".mp3"},
}

# Short human labels for Telegram lines.
STAGE_LABEL = {
    "1_script": "script", "2_review": "review", "3_vo_expand": "vo-expand",
    "4_vo_record": "vo-record", "5_images": "images", "6_assemble": "assemble",
    "7_packaging": "packaging", "8_thumbnail": "thumbnail", "9_description": "description",
    "10_publish": "publish", "11_analyze": "analyze",
}

STAGE_ORDER = [
    "1_script", "2_review", "3_vo_expand", "4_vo_record", "5_images",
    "6_assemble", "7_packaging", "8_thumbnail", "9_description",
    "10_publish", "11_analyze",
]


# === Small helpers =========================================================

def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str | None) -> float | None:
    """ISO8601 (Z) → epoch seconds; None on missing/unparseable."""
    if not s:
        return None
    try:
        txt = s.strip()
        if txt.endswith("Z"):
            txt = txt[:-1] + "+00:00"
        return datetime.fromisoformat(txt).timestamp()
    except Exception:
        return None


def vault_abs(rel: str | None) -> Path | None:
    if not rel:
        return None
    return (WORKSPACE_DIR / rel).resolve()


def nn(video: int) -> str:
    return f"{video:02d}"


def _pid_alive(pid: int | None) -> bool:
    """True iff SOME process owns this pid right now (does not prove identity)."""
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def _proc_start_token(pid: int | None) -> str | None:
    """An opaque process-start signature via `ps -p PID -o lstart=` — the raw
    wall-clock start string (e.g. "Thu Jun 18 01:17:49 2026"). Dependency-free,
    macOS-compatible (this host's `ps` has no `etimes`). Used ONLY for equality:
    the SAME process always yields the identical string, a RECYCLED pid yields a
    different one (a later process can't share both the pid and the exact start
    second). String equality is immune to tz/DST/NTP clock steps that would skew
    an epoch conversion (Medium fix, 2026-06-18). None if the pid is gone."""
    if not pid:
        return None
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True, text=True, timeout=5,
        )
        s = out.stdout.strip()
        return s or None
    except Exception:
        return None


def notify(message: str) -> None:
    """Best-effort Telegram line via the canonical daemon-decoupled channel."""
    try:
        subprocess.run([str(NOTIFY_SH), message], timeout=20,
                       capture_output=True, text=True)
    except Exception:
        pass  # reporting must never crash the orchestrator


def die(msg: str, code: int = 1) -> None:
    print(msg, file=sys.stderr)
    sys.exit(code)


# === State file load / atomic save ========================================

class StateFile:
    """Owns the per-video JSON + the whole-run advisory flock."""

    def __init__(self, video: int):
        self.video = video
        self.path = STATE_DIR / f"Video_{nn(video)}_pipeline.json"
        self.lock_fh = None
        self.data: dict = {}

    def acquire(self) -> None:
        """Acquire the advisory flock held for the ENTIRE run (load→subprocess
        →final write). Blocks until any concurrent run on THIS video releases."""
        # Lock a HOST-LOCAL sidecar file (flock is per-host, so the lock must NOT
        # live in the synced vault — keeps Production_Kits clean of .lock noise).
        # A stable per-video path means CLI and the daemon-spawned subprocess
        # contend on the same lock and serialize correctly.
        LOCK_DIR.mkdir(parents=True, exist_ok=True)
        lock_path = LOCK_DIR / f"Video_{nn(self.video)}_pipeline.json.lock"
        self.lock_fh = open(lock_path, "w")
        fcntl.flock(self.lock_fh.fileno(), fcntl.LOCK_EX)

    def release(self) -> None:
        if self.lock_fh:
            try:
                fcntl.flock(self.lock_fh.fileno(), fcntl.LOCK_UN)
                self.lock_fh.close()
            except Exception:
                pass
            self.lock_fh = None

    def load(self) -> None:
        if not self.path.exists():
            die(f"No state file for video {self.video}: {self.path}\n"
                f"Seed it first with: --video {self.video} --init")
        try:
            self.data = json.loads(self.path.read_text())
        except json.JSONDecodeError as e:
            die(f"State file is not valid JSON ({self.path}): {e}")

    def save(self) -> None:
        """Atomic write: tmp + os.replace — a crash mid-write never corrupts."""
        self.data["updated_at"] = now_iso()
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, indent=2) + "\n")
        os.replace(tmp, self.path)

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False


# === Seed / --init =========================================================

def default_stages() -> dict:
    return {
        "1_script":      _stage("orchestrator", False, []),
        "2_review":      _stage("orchestrator", False, ["1_script"]),
        "3_vo_expand":   _stage("steve",        True,  ["2_review"]),
        "4_vo_record":   _stage("steve",        True,  ["3_vo_expand"]),
        "5_images":      _stage("steve",        True,  ["2_review"]),
        "6_assemble":    _stage("orchestrator", False, ["4_vo_record", "5_images"]),
        "7_packaging":   _stage("orchestrator", False, ["2_review"]),
        "8_thumbnail":   _stage("steve",        True,  ["7_packaging"]),
        "9_description": _stage("orchestrator", False, ["2_review"]),
        "10_publish":    _stage("steve",        True,  ["6_assemble", "8_thumbnail", "9_description"]),
        "11_analyze":    _stage("orchestrator", False, ["10_publish"]),
    }


def _stage(owner: str, gate: bool, deps: list[str]) -> dict:
    return {
        "status": "blocked", "owner": owner, "gate": gate, "deps": deps,
        "artifact_path": None, "started_at": None, "completed_at": None,
        "pid": None, "pid_start_token": None, "fail_count": 0,
        "park_reason": None, "note": None,
    }


def cmd_init(video: int, title: str | None, force: bool) -> int:
    path = STATE_DIR / f"Video_{nn(video)}_pipeline.json"
    if path.exists() and not force:
        die(f"State file already exists: {path}\n"
            f"Refusing to overwrite. Use --force-init to recreate, or edit it directly.")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "video": video,
        "schema_version": SCHEMA_VERSION,
        "title": title or f"Video {nn(video)}",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "stages": default_stages(),
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)
    print(f"Seeded fresh state file: {path}")
    return 0


# === Resolver ==============================================================

def effective_status(key: str, stages: dict) -> str:
    """done (terminal) | running (preserved) | needs-steve (advisory) | ready | blocked."""
    s = stages[key]
    stored = s.get("status")
    if stored == "done":
        return "done"
    if stored == "running":
        return "running"
    if stored == "needs-steve":
        return "needs-steve"
    # blocked/ready/None → recompute from deps
    deps = s.get("deps", [])
    if all(stages.get(d, {}).get("status") == "done" for d in deps):
        return "ready"
    return "blocked"


def deps_max_completed(key: str, stages: dict) -> float | None:
    ts = [parse_iso(stages.get(d, {}).get("completed_at")) for d in stages[key].get("deps", [])]
    ts = [t for t in ts if t is not None]
    return max(ts) if ts else None


def _artifact_nonempty_and_fresh(spec: dict, video: int, threshold: float | None) -> tuple[bool, str]:
    """Decision 1a: (1) exists & non-empty AND (2) mtime strictly newer than
    the deps' max completed_at. Returns (ok, reason-if-not)."""
    rel = spec["path_tmpl"].replace("NN", nn(video))
    p = vault_abs(rel)
    if p is None or not p.exists():
        return False, f"artifact missing: {rel}"
    if spec["kind"] == "file":
        if p.stat().st_size <= 0:
            return False, f"artifact empty: {rel}"
        mtime = p.stat().st_mtime
    else:  # dir
        ext = spec.get("ext")
        files = [f for f in p.iterdir() if f.is_file() and (not ext or f.name.endswith(ext))]
        if not files:
            return False, f"no {ext or 'expected'} files in {rel}"
        total = sum(f.stat().st_size for f in files)
        if total <= 0:
            return False, f"only zero-byte files in {rel}"
        mtime = max(f.stat().st_mtime for f in files)
    if threshold is not None and not (mtime > threshold):
        return False, f"artifact older than its deps (stale): {rel}"
    return True, ""


def promote_gate_exits(stages: dict, video: int) -> list[str]:
    """Decision 2 step 3: re-evaluate needs-steve exits for gate-parked stages.
    Human-artifact gates auto-promote ONLY when the artifact is non-empty AND
    fresher than deps. failed-parked stages are skipped entirely. The billed
    gate (5) is promoted only by --spend-ok, never here."""
    promoted = []
    for key in STAGE_ORDER:
        s = stages.get(key)
        if not s or s.get("status") != "needs-steve":
            continue
        if s.get("park_reason") != "gate":  # N2: failed-parked never auto-promotes
            continue
        if key == "5_images":  # billed gate: spend-ok only
            continue
        spec = GATE_ARTIFACTS.get(key)
        if not spec:  # no fixed artifact convention → manual hand-edit only
            continue
        ok, reason = _artifact_nonempty_and_fresh(spec, video, deps_max_completed(key, stages))
        if ok:
            s["status"] = "done"
            s["completed_at"] = now_iso()
            s["park_reason"] = None
            s["pid"] = None
            s["pid_start_token"] = None
            s["artifact_path"] = spec["path_tmpl"].replace("NN", nn(video))
            s["note"] = "auto-promoted from needs-steve (artifact landed, non-empty, fresh)"
            promoted.append(key)
        else:
            s["note"] = f"still needs Steve: {reason}"
    return promoted


def reconcile_orphans(stages: dict, video: int) -> list[str]:
    """Decision 3: a `running` stage is orphaned if the orchestrator that owned
    it is gone/recycled/timed-out. Orphan → ready (re-runnable). A genuinely-live
    run aborts the whole invocation."""
    reset = []
    for key in STAGE_ORDER:
        s = stages.get(key)
        if not s or s.get("status") != "running":
            continue
        pid = s.get("pid")
        rec_token = s.get("pid_start_token")
        started = parse_iso(s.get("started_at"))
        timeout = RUN_TABLE.get(key, {}).get("timeout", 1800)
        cur_token = _proc_start_token(pid)
        dead = not _pid_alive(pid)
        # Recycled = pid alive but a DIFFERENT process now owns it (start-token
        # differs). String compare is tz/DST-immune. Only when both tokens known.
        recycled = (not dead and cur_token is not None
                    and rec_token is not None and cur_token != rec_token)
        timed_out = (started is not None and (time.time() - started) > timeout)
        if dead or recycled or timed_out:
            why = "pid-dead" if dead else ("pid-recycled" if recycled else "timed-out")
            s["status"] = "ready"
            s["pid"] = None
            s["pid_start_token"] = None
            s["note"] = f"reset from orphaned 'running' ({why}) at {now_iso()}"
            reset.append(key)
        else:
            # A genuinely-live run of OUR process holds it — refuse to double-run.
            msg = (f"stage {key} already running (pid {pid} since {s.get('started_at')}), "
                   f"refusing to double-run. If it is stuck, clear it with: "
                   f"--video {video} --force-reset")
            die(msg)
    return reset


def select_next(stages: dict) -> str | None:
    """The structural no-autonomous-action core: returns the lowest-numbered
    stage that is ready AND gate==false AND owner==orchestrator AND in RUN_TABLE.
    Billed/human gates are never members of this set."""
    for key in STAGE_ORDER:
        s = stages[key]
        if (effective_status(key, stages) == "ready"
                and not s.get("gate")
                and s.get("owner") == "orchestrator"
                and key in RUN_TABLE
                and RUN_TABLE[key]["kind"] != "billed"):
            return key
    return None


def all_ready_gates(stages: dict) -> list[str]:
    """Every effectively-ready gate (needs Steve), lowest-first — independently
    actionable in parallel."""
    return [k for k in STAGE_ORDER
            if effective_status(k, stages) == "ready"
            and (stages[k].get("gate") or stages[k].get("owner") == "steve")]


def first_ready_gate(stages: dict) -> str | None:
    """Lowest-numbered stage that is effectively ready but is a gate (needs Steve)."""
    gates = all_ready_gates(stages)
    return gates[0] if gates else None


# === Executors =============================================================

AGENTS_DIR = Path(os.path.expanduser("~/.claude/agents"))


def run_agent_stage(key: str, video: int, stages: dict) -> tuple[bool, str]:
    """Dispatch a claude subagent exactly as iris.py:_run_dispatch does, blocking.

    H2 (no silent stall): the agent definition file is what `claude --agent`
    actually needs. If it is missing, hard-error + Telegram alert + non-zero
    exit, never a silent skip. (We intentionally do NOT `import iris` to read its
    DISPATCH_ALLOWED_AGENTS enum — that would execute the whole daemon module's
    top-level side-effects under the venv. The run-table here is the orchestrator's
    own authoritative envelope, and the agent-file check is H2 against the real
    source of truth the dispatch consumes.)"""
    cfg = RUN_TABLE[key]
    agent = cfg["agent"]
    agent_file = AGENTS_DIR / f"{agent}.md"
    if not agent_file.exists():
        msg = (f"⚠️ Video {video}: stage {key} agent '{agent}' definition not found "
               f"at {agent_file} — cannot dispatch. Create the agent or fix the run-table.")
        notify(msg)
        return False, msg
    prompt = _stage_prompt(key, video)
    cmd = [CLAUDE_CLI_PATH, "--print", "--agent", agent,
           "--add-dir", str(WORKSPACE_DIR), "--dangerously-skip-permissions",
           "--", prompt]
    try:
        proc = subprocess.run(cmd, cwd=str(WORKSPACE_DIR), capture_output=True,
                              text=True, timeout=cfg["timeout"])
    except subprocess.TimeoutExpired:
        return False, f"agent '{agent}' timed out after {cfg['timeout']}s"
    if proc.returncode != 0:
        return False, f"agent '{agent}' exited {proc.returncode}: {(proc.stderr or '')[-500:]}"
    return True, (proc.stdout or "").strip()[-500:]


def _stage_prompt(key: str, video: int) -> str:
    v = f"Video_{nn(video)}"
    prompts = {
        "1_script": f"Draft the production script for {v} (3SK Finance). Follow the locked "
                    f"Brand Bible voice + scene-prompt structure. Write to BRANDS/3SK_Finance/Scripts/.",
        "2_review": f"Review the drafted script for {v} (BRANDS/3SK_Finance/Scripts/{v}_Script.md). "
                    f"7-dimension read-only critique → BRANDS/3SK_Finance/Scripts/_REVIEW_PREP/.",
        "7_packaging": f"Build the packaging for {v}: 8-10 titles, 2 cold-open hooks, 3 thumbnail "
                       f"text overlays + CTR rationale, from the script's Thumbnail Concept. "
                       f"Write to BRANDS/3SK_Finance/Packaging/.",
        "9_description": f"Draft the YouTube upload pack for {v} from BRANDS/3SK_Finance/Scripts/"
                         f"{v}_Script.md: description, chapter timestamps, disclosure, hashtags, "
                         f"pinned comment. Write to BRANDS/3SK_Finance/Video_Descriptions/.",
        "11_analyze": f"Analyze {v}'s YouTube performance from the analytics export referenced in "
                      f"BRANDS/3SK_Finance/Channel_Intelligence/Analytics/ (manual CSV/paste mode). "
                      f"Write routable fixes for scriptwriter + packaging.",
    }
    return prompts[key]


def run_script_stage(key: str, video: int) -> tuple[bool, str]:
    """Stage 6 assemble: a SINGLE build_video.py --assemble call (it drives
    assemble.py itself). MUST pass --assemble; MUST NEVER pass --images/--vo."""
    if key != "6_assemble":
        return False, f"no script executor for stage {key}"
    cfg = RUN_TABLE[key]
    vid = f"Video_{nn(video)}"
    cmd = [
        sys.executable, "build_video.py", vid, "--assemble",
        "--image-set", f"Raw_Assets/{vid}_HD",
        "--vo-source", f"Voice_Files/{vid}",
        "--align",
    ]
    try:
        proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True,
                              text=True, timeout=cfg["timeout"])
    except subprocess.TimeoutExpired:
        return False, f"build_video.py --assemble timed out after {cfg['timeout']}s"
    if proc.returncode != 0:
        return False, f"build_video.py exited {proc.returncode}: {(proc.stderr or '')[-500:]}"
    return True, (proc.stdout or "").strip()[-300:]


def stage_artifact_path(key: str, video: int) -> str | None:
    v = f"Video_{nn(video)}"
    paths = {
        "1_script": f"{VAULT_REL}/Scripts/{v}_Script.md",
        "2_review": f"{VAULT_REL}/Scripts/_REVIEW_PREP",
        "6_assemble": f"{VAULT_REL}/Footage_and_Edits/{v}_v2.mp4",
        "7_packaging": f"{VAULT_REL}/Packaging",
        "9_description": f"{VAULT_REL}/Video_Descriptions/{v}_Description.md",
        "11_analyze": f"{VAULT_REL}/Channel_Intelligence/Analytics",
        "5_images": f"{VAULT_REL}/Raw_Assets/{v}_HD",
    }
    return paths.get(key)


# === Commands ==============================================================

def _mark_running(s: dict) -> None:
    pid = os.getpid()
    s["status"] = "running"
    s["pid"] = pid
    s["pid_start_token"] = _proc_start_token(pid)
    s["started_at"] = now_iso()
    s["note"] = None


def _on_success(s: dict, key: str, video: int, out: str) -> None:
    s["status"] = "done"
    s["completed_at"] = now_iso()
    s["artifact_path"] = stage_artifact_path(key, video)
    s["fail_count"] = 0
    s["park_reason"] = None
    s["pid"] = None
    s["pid_start_token"] = None
    s["note"] = (out or "")[-300:] or "done"


def _on_failure(s: dict, err: str) -> None:
    s["fail_count"] = int(s.get("fail_count") or 0) + 1
    s["pid"] = None
    s["pid_start_token"] = None
    s["note"] = (err or "")[-500:]
    if s["fail_count"] >= MAX_FAILS:
        s["status"] = "needs-steve"
        s["park_reason"] = "failed"
    else:
        s["status"] = "ready"


def cmd_advance(sf: StateFile) -> int:
    stages = sf.data["stages"]
    video = sf.data["video"]
    reset = reconcile_orphans(stages, video)
    promoted = promote_gate_exits(stages, video)
    if reset or promoted:
        sf.save()

    nxt = select_next(stages)
    if nxt is None:
        gate = first_ready_gate(stages)
        if gate is not None:
            s = stages[gate]
            s["status"] = "needs-steve"
            s["park_reason"] = "gate"
            s["note"] = _gate_note(gate, video)
            sf.save()
            if gate == "5_images":
                line = (f"🎬 Video {video}: nothing to auto-advance. ⛔ Next is BILLED: "
                        f"stage {gate} ({STAGE_LABEL[gate]}). Reply /pipeline {video} spend-ok to authorize.")
            else:
                line = (f"🎬 Video {video}: nothing to auto-advance. Next action is yours: "
                        f"stage {gate} ({STAGE_LABEL[gate]}).")
            print(line)
            notify(line)
            return 0
        line = f"🎬 Video {video}: nothing to advance — all stages done or blocked."
        print(line)
        notify(line)
        return 0

    s = stages[nxt]
    cfg = RUN_TABLE[nxt]
    _mark_running(s)
    sf.save()

    if cfg["kind"] == "agent":
        ok, out = run_agent_stage(nxt, video, stages)
    elif cfg["kind"] == "script":
        ok, out = run_script_stage(nxt, video)
    else:
        ok, out = False, f"stage {nxt} is billed and not advancable via --advance"

    if ok:
        _on_success(s, nxt, video, out)
        sf.save()
        gate = first_ready_gate(stages) if select_next(stages) is None else None
        nxt2 = select_next(stages)
        tail = ""
        if nxt2:
            tail = f" → next: stage {nxt2} ({STAGE_LABEL[nxt2]})."
        elif gate == "5_images":
            tail = (f" → next is BILLED: stage {gate} ({STAGE_LABEL[gate]}). "
                    f"Reply /pipeline {video} spend-ok to authorize.")
        elif gate:
            tail = f" → next action is yours: stage {gate} ({STAGE_LABEL[gate]})."
        line = f"🎬 Video {video}: stage {nxt} ({STAGE_LABEL[nxt]}) done.{tail}"
        print(line)
        notify(line)
        return 0
    else:
        _on_failure(s, out)
        sf.save()
        if s["status"] == "needs-steve":
            line = (f"⚠️ Video {video}: stage {nxt} ({STAGE_LABEL[nxt]}) FAILED "
                    f"{MAX_FAILS}× — parked needs-steve. {out[:160]}")
        else:
            line = (f"⚠️ Video {video}: stage {nxt} ({STAGE_LABEL[nxt]}) FAILED — {out[:160]}. "
                    f"Reset to ready; re-run /pipeline {video} to retry.")
        print(line)
        notify(line)
        return 2


def _gate_note(key: str, video: int) -> str:
    v = f"Video_{nn(video)}"
    notes = {
        "3_vo_expand": "Draft/expand the VO script. Hand-edit this stage to status:done when ready.",
        "4_vo_record": f"Record the VO in ElevenLabs (Brian) → BRANDS/3SK_Finance/Voice_Files/{v}/ "
                       f"(auto-promotes when ≥1 fresh .mp3 lands).",
        "5_images": f"BILLED image batch. Confirm the scene manifest exists, then authorize: "
                    f"/pipeline {video} spend-ok (or --spend-ok).",
        "8_thumbnail": "Run thumbnail-coordinator + have the designer deliver the thumbnail. "
                       "Hand-edit this stage to status:done when ready.",
        "10_publish": "Upload to YouTube. Hand-edit this stage to status:done after publishing.",
    }
    return notes.get(key, "Needs Steve.")


def cmd_status(sf: StateFile) -> int:
    stages = sf.data["stages"]
    video = sf.data["video"]
    # Read-only-ish: reconcile + gate-exit re-eval so --status reflects truth.
    reset = reconcile_orphans(stages, video)
    promoted = promote_gate_exits(stages, video)
    if reset or promoted:
        sf.save()
    title = sf.data.get("title", "")
    print(f"Video {video} — {title}")
    print(f"{'stage':<14} {'effective':<12} {'gate':<5} {'owner':<12} artifact")
    print("-" * 78)
    for key in STAGE_ORDER:
        s = stages[key]
        eff = effective_status(key, stages)
        art = s.get("artifact_path") or "-"
        print(f"{key:<14} {eff:<12} {str(bool(s.get('gate'))):<5} "
              f"{str(s.get('owner')):<12} {art}")
    nxt = select_next(stages)
    gate = first_ready_gate(stages)
    if nxt:
        print(f"\nNext auto-runnable: {nxt} ({STAGE_LABEL[nxt]}) — run --advance.")
    elif gate == "5_images":
        print(f"\nNext is BILLED: {gate} — authorize with --spend-ok / /pipeline {video} spend-ok.")
    elif gate:
        print(f"\nNext action is Steve's: {gate} ({STAGE_LABEL[gate]}).")
    else:
        print("\nNothing ready — all done or blocked.")
    # Surface ALL independently-actionable ready gates, not just the lowest, so
    # Steve sees parallel work (e.g. images + thumbnail) he can do at once.
    ready_gates = all_ready_gates(stages)
    if len(ready_gates) > 1:
        labels = ", ".join(f"{g} ({STAGE_LABEL[g]})" for g in ready_gates)
        print(f"All ready gates (parallel-actionable): {labels}")
    return 0


def cmd_spend_ok(sf: StateFile) -> int:
    """Decision 4 (C1): the ONLY billed-spend path. Confirms stage 5 is ready,
    confirms the scene manifest exists, then shells generate_images.py exactly
    once. NEVER reachable from --advance."""
    stages = sf.data["stages"]
    video = sf.data["video"]
    reconcile_orphans(stages, video)
    key = "5_images"
    s = stages[key]
    eff = effective_status(key, stages)
    if s.get("status") == "done":
        line = f"Video {video}: stage 5 (images) already done — nothing to spend on."
        print(line)
        return 0
    # ready means deps met; needs-steve (gate-parked) also acceptable here.
    deps_done = all(stages.get(d, {}).get("status") == "done" for d in s.get("deps", []))
    if not deps_done:
        line = (f"⚠️ Video {video}: stage 5 (images) deps not met — refusing to spend. "
                f"Need: {', '.join(s.get('deps', []))} done.")
        print(line)
        notify(line)
        return 2

    # Resolve + confirm the scene manifest BEFORE spending.
    manifest_rel = s.get("scene_manifest") or f"{VAULT_REL}/Production_Kits/Video_{nn(video)}_scene_manifest.json"
    manifest_abs = vault_abs(manifest_rel)
    if manifest_abs is None or not manifest_abs.exists():
        s["note"] = f"spend refused: scene manifest not found at {manifest_rel}"
        sf.save()
        line = (f"⚠️ Video {video}: scene manifest not found ({manifest_rel}) — "
                f"refusing to spend. Author it first.")
        print(line)
        notify(line)
        return 2

    out_rel = f"Raw_Assets/Video_{nn(video)}_HD"      # build_video reads same dir
    out_abs = vault_abs(f"{VAULT_REL}/{out_rel}")
    _mark_running(s)
    sf.save()
    cmd = [
        sys.executable, "image_factory/generate_images.py",
        str(manifest_abs), "--output", str(out_abs),
    ]
    cfg = RUN_TABLE[key]
    try:
        proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True,
                              text=True, timeout=cfg["timeout"])
        ok = proc.returncode == 0
        out = (proc.stdout or "").strip()[-300:] if ok else \
              f"generate_images exited {proc.returncode}: {(proc.stderr or '')[-400:]}"
    except subprocess.TimeoutExpired:
        ok, out = False, f"generate_images timed out after {cfg['timeout']}s"

    if ok:
        _on_success(s, key, video, out)
        sf.save()
        nxt = select_next(stages)
        tail = f" → next: stage {nxt} ({STAGE_LABEL[nxt]})." if nxt else ""
        line = f"💸 Video {video}: stage 5 (images) GENERATED.{tail}"
        print(line)
        notify(line)
        return 0
    else:
        _on_failure(s, out)
        sf.save()
        line = f"⚠️ Video {video}: stage 5 (images) spend FAILED — {out[:200]}"
        print(line)
        notify(line)
        return 2


def cmd_force_reset(sf: StateFile) -> int:
    """Decision 3 (N3): recovery hatch for a WEDGED running stage. Applies the
    same 3-condition orphan test under the same flock. Refuses a genuinely-live
    run (never a force-kill)."""
    stages = sf.data["stages"]
    video = sf.data["video"]
    cleared, refused = [], []
    for key in STAGE_ORDER:
        s = stages.get(key)
        if not s or s.get("status") != "running":
            continue
        pid = s.get("pid")
        rec_token = s.get("pid_start_token")
        started = parse_iso(s.get("started_at"))
        timeout = RUN_TABLE.get(key, {}).get("timeout", 1800)
        cur_token = _proc_start_token(pid)
        dead = not _pid_alive(pid)
        # Recycled = pid alive but a DIFFERENT process now owns it (start-token
        # differs). String compare is tz/DST-immune. Only when both tokens known.
        recycled = (not dead and cur_token is not None
                    and rec_token is not None and cur_token != rec_token)
        timed_out = (started is not None and (time.time() - started) > timeout)
        if dead or recycled or timed_out:
            why = "pid-dead" if dead else ("pid-recycled" if recycled else "timed-out")
            s["status"] = "ready"
            s["pid"] = None
            s["pid_start_token"] = None
            s["note"] = f"manual --force-reset ({why}) at {now_iso()}"
            cleared.append(key)
        else:
            refused.append((key, pid, s.get("started_at")))
    if cleared:
        sf.save()
    if refused:
        for key, pid, ts in refused:
            print(f"stage {key} is genuinely live, pid {pid}, started {ts} — not resetting. "
                  f"Wait for it, or kill pid {pid} yourself first.")
        return 2
    if cleared:
        print(f"Video {video}: reset {', '.join(cleared)} → ready.")
        return 0
    print(f"Video {video}: no running stages to reset.")
    return 0


# === Main ==================================================================

def main() -> int:
    p = argparse.ArgumentParser(description="Pipeline orchestrator (deterministic video sequencer).")
    p.add_argument("--video", type=int, required=True, help="Video number, e.g. 1.")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--advance", action="store_true", help="Run the next ready non-gate stage.")
    g.add_argument("--status", action="store_true", help="Print the state table; run nothing.")
    g.add_argument("--init", action="store_true", help="Seed a fresh state file.")
    g.add_argument("--spend-ok", action="store_true", help="Authorize the BILLED image batch (stage 5).")
    g.add_argument("--force-reset", action="store_true", help="Reset a wedged running stage (orphan-tested).")
    p.add_argument("--title", help="Title for --init.")
    p.add_argument("--force-init", action="store_true", help="Overwrite an existing state file on --init.")
    args = p.parse_args()

    if args.video < 1:
        die("--video must be a positive integer.")

    if args.init:
        return cmd_init(args.video, args.title, args.force_init)

    with StateFile(args.video) as sf:
        sf.load()
        if sf.data.get("video") != args.video:
            die(f"State file video mismatch: file says {sf.data.get('video')}, asked {args.video}.")
        if args.status:
            return cmd_status(sf)
        if args.advance:
            return cmd_advance(sf)
        if args.spend_ok:
            return cmd_spend_ok(sf)
        if args.force_reset:
            return cmd_force_reset(sf)
    return 0


if __name__ == "__main__":
    sys.exit(main())
