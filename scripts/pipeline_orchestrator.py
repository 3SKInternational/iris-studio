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
Per-video CLI (one video):
  python scripts/pipeline_orchestrator.py --video 5 --advance
  python scripts/pipeline_orchestrator.py --video 5 --status
  python scripts/pipeline_orchestrator.py --video 5 --init [--title "..."]
  python scripts/pipeline_orchestrator.py --video 5 --spend-ok
  python scripts/pipeline_orchestrator.py --video 5 --force-reset

Fleet CLI (every video state file; NO --video):
  python scripts/pipeline_orchestrator.py --advance-all   # drain each video to its next gate, then ONE digest
  python scripts/pipeline_orchestrator.py --supervise     # read-only fleet digest (where is everything stuck?)

--advance-all is still a sequencer, not a swarm: it drains each video by calling
the SAME gate-respecting advance path, so the no-spend/publish/VO invariant holds
fleet-wide unchanged — it physically cannot cross a gate. It stops draining a
video at the first gate/failure (never burns retries in one pass), caps stages
per video per run, isolates per-video errors (one bad video never aborts the
sweep), and detects infra failures (e.g. a broken ~/.claude/session-env) so a
host problem leaves stages 'ready' instead of wrongly parking them as 'failed'.
--supervise mutates nothing and takes no lock (atomic writes make lock-free reads
safe), so it can run alongside a live advance.

Stdlib only (json, fcntl, subprocess, ...) — no third-party deps, so it runs
under any python3 regardless of the daemon's venv.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import namedtuple
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
MAX_INFRA = 5  # consecutive INFRA skips → needs-steve (park_reason "infra"): a host outage
               # that never clears must surface, not retry invisibly forever.
SCHEMA_VERSION = 1
MAX_STAGES_PER_RUN = 8   # --advance-all: hard cap on stages drained per video per run
STALE_DAYS = 4           # --supervise/digest: flag a video with no movement in this many days

# Substrings that mark a failure as INFRA (the host/toolchain couldn't even run
# the stage) rather than a genuine task failure. On an infra failure we leave the
# stage 'ready' and do NOT increment fail_count — a broken ~/.claude/session-env
# or a down toolchain must never silently park videos as 'failed'. Matched
# case-insensitively against the captured stderr/error string.
#
# DELIBERATELY NARROW (skeptical-code-reviewer HIGH, 2026-06-18): broad substrings
# like "no such file or directory" / "permission denied" / "operation not
# permitted" / "eperm" also match GENUINE task failures (a missing task input, an
# ssh publickey error, any PermissionError traceback) — misclassifying those as
# infra meant a real failure retried forever and never parked. These markers are
# now restricted to signals that essentially never appear in legitimate agent
# task output: the specific broken-session-env path, host resource exhaustion,
# and the CLI binary itself being absent. A persistent infra outage is still
# bounded by MAX_INFRA so it surfaces rather than looping silently.
INFRA_FAILURE_MARKERS = (
    "session-env",                       # the broken ~/.claude/session-env outage
    "cannot allocate memory",            # ENOMEM — host out of memory
    "resource temporarily unavailable",  # EAGAIN — host fork/thread exhaustion
    "too many open files",               # EMFILE — host fd-limit (launchd RLIMIT_NOFILE)
    "command not found",                 # the claude CLI itself missing from PATH
)

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


# During --advance-all we suppress the per-stage Telegram lines and send ONE
# consolidated digest at the end (so a fleet drain is not a stream of pings).
SUPPRESS_NOTIFY = False


def notify(message: str, force: bool = False) -> None:
    """Best-effort Telegram line via the canonical daemon-decoupled channel.
    When SUPPRESS_NOTIFY is set (a fleet drain), per-stage lines are dropped;
    pass force=True for the consolidated digest and other always-send lines."""
    if SUPPRESS_NOTIFY and not force:
        return
    try:
        subprocess.run([str(NOTIFY_SH), message], timeout=20,
                       capture_output=True, text=True)
    except Exception:
        pass  # reporting must never crash the orchestrator


def die(msg: str, code: int = 1) -> None:
    print(msg, file=sys.stderr)
    sys.exit(code)


class LiveRunError(Exception):
    """A stage is genuinely live (a real process still owns it) — refuse to
    double-run. Distinct from die()/SystemExit (used for corrupt state, bad args,
    etc.) so the fleet loop can tell a benign in-flight run apart from a corrupt
    state file: the former is healthy and skipped, the latter needs attention."""


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
        "infra_count": 0, "park_reason": None, "note": None,
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
        # last FORWARD progress (a stage completing). Distinct from updated_at,
        # which bumps on every save incl. gate-parks and infra-skips. Staleness is
        # measured from this so an infra-retry loop (which bumps updated_at) still
        # goes stale, and a gate parked waiting on Steve still nudges after N days.
        "last_progress_at": now_iso(),
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
            # Raise (not die) so the fleet loop can distinguish this benign live
            # run from a corrupt-state die()/SystemExit. The per-video caller
            # converts it back to die() to preserve the prior CLI behavior.
            raise LiveRunError(
                f"stage {key} already running (pid {pid} since {s.get('started_at')}), "
                f"refusing to double-run. If it is stuck, clear it with: "
                f"--video {video} --force-reset")
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


def gates_awaiting_steve(stages: dict) -> list[str]:
    """Gate stages that need Steve — in EITHER representation: still effectively
    'ready' (deps met, not yet parked) OR already parked 'needs-steve' with
    park_reason 'gate' by a prior advance. Lowest-first, deduped. (all_ready_gates
    only sees the first; once advance parks a gate it flips to needs-steve, so the
    fleet digest must also count the parked form — else a parked gate reads as
    'blocked'.)"""
    out = []
    for k in STAGE_ORDER:
        s = stages.get(k, {})
        ready_gate = (effective_status(k, stages) == "ready"
                      and (s.get("gate") or s.get("owner") == "steve"))
        parked_gate = (s.get("status") == "needs-steve" and s.get("park_reason") == "gate")
        if ready_gate or parked_gate:
            out.append(k)
    return out


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
    # Pass B — pre-assemble RENDERS review, pinned to the SAME billed manifest the
    # spend used. A REVISE verdict means an off-model / garbled rendered PNG is
    # present: refuse to build it into the cut. Returns False → consumes a normal
    # retry (bounded by MAX_FAILS) and eventually parks for Steve. The render-fix
    # LOOP closes through the human money-gate, NOT here: regenerating renders is
    # BILLED, so it must NOT auto-run (no-autonomous-spend invariant). The fix
    # path is the same closed loop as Pass A — correct the prompts per the verdict
    # (free) then re-run `/pipeline N spend-ok`, which re-reviews + regenerates,
    # and this gate re-checks the fresh renders. Fail-OPEN if the reviewer can't
    # run (assembly is free): warn and assemble anyway.
    verdict, vrel, detail = run_image_review("renders", video,
                                             canonical_manifest_rel(video))
    if verdict == "REVISE":
        return False, (f"image-reviewer REVISE on the rendered images — refusing to "
                       f"assemble. Fix prompts per {vrel}, then re-run /pipeline "
                       f"{video} spend-ok to regenerate (BILLED — human-gated) ({detail}).")
    if verdict == "UNAVAILABLE":
        notify(f"⚠️ Video {video}: image-reviewer could not run the pre-assemble "
               f"renders review ({detail}) — assembling anyway (fail-open).")
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


# === Image-review gate (image-reviewer subagent) ==========================
# Pass A: pre-spend PROMPTS review inside cmd_spend_ok — blocks a billed batch
#         whose prompts are off-model / garble-risk (HOLD-SPEND).
# Pass B: pre-assemble RENDERS review inside run_script_stage — blocks a cut
#         built from off-model / garbled rendered PNGs (REVISE).
IMAGE_REVIEWER_AGENT = "image-reviewer"
# The author that FIXES flagged prompts — the image analogue of scriptwriter in
# the scriptwriter↔script-reviewer loop. It edits the manifest in place; $0.
PROMPT_FIXER_AGENT = "scene-image-prompt-generator"
IMAGE_REVIEW_TIMEOUT = 900
# Closed review→fix→re-review loop budget at the PROMPTS gate. Fixing prompts is
# free, so we iterate, but bounded: after this many fix dispatches still HOLD-
# SPEND, we stop and park for a human rather than burning dispatches forever.
IMAGE_REVIEW_MAX_FIX_ATTEMPTS = 2


def canonical_manifest_rel(video: int, stages: dict | None = None) -> str:
    """The ONE billed image manifest — the same file the image-reviewer audits
    (PROMPTS mode), the prompt-fixer edits, and generate_images.py bills. A state
    file MAY override via stages['5_images']['scene_manifest']; otherwise it is
    the canonical hd batch in Image_Factory. (NOT the legacy Production_Kits
    '_scene_manifest.json' default, which never existed for any video and is the
    wrong schema for generate_images.py — that read off a non-existent path.)"""
    override = (stages or {}).get("5_images", {}).get("scene_manifest")
    return override or \
        f"{VAULT_REL}/Raw_Assets/Image_Factory/manifests/video_{nn(video)}_hd.json"

# Severity rank: a verdict FILE legitimately carries several tokens (the agent
# writes a canonical "VERDICT:" line AND a per-shot table; a line may also echo
# the rubric "SHIP / SHIP WITH FIXES / HOLD-SPEND / REVISE"). We must NOT trust
# regex leftmost-match to pick the blocking one — `re.search` returns the first
# POSITION, not the most-severe token. Instead we scan every VERDICT-bearing
# line, collect all tokens, and FAIL SAFE toward the most-blocking one. A false
# block is cheap (Steve --force overrides, or assembly retries); a false SHIP
# bills real money / ships an off-model cut. Alternation lists the long phrase
# BEFORE its "SHIP" substring so the tokenizer consumes "SHIP WITH FIXES" whole.
_VERDICT_RANK = {"HOLD-SPEND": 4, "REVISE": 3, "SHIP WITH FIXES": 2, "SHIP": 1}
_VERDICT_TOKEN_RE = re.compile(
    r"\b(HOLD-SPEND|SHIP WITH FIXES|REVISE|SHIP)\b", re.IGNORECASE)


def _image_review_verdict_rel(video: int) -> str:
    return f"{VAULT_REL}/Raw_Assets/Image_Factory/_REVIEW/Video_{nn(video)}_Image_Review.md"


def _parse_image_verdict(text: str) -> str | None:
    """Most-severe verdict token across all VERDICT-bearing lines, or None.

    Only lines that mention 'verdict' are scanned, so prose/shot-finding text
    that happens to contain a token elsewhere can't flip the result; among those
    lines the highest-severity token wins (fail-safe toward blocking)."""
    best, best_rank = None, 0
    for line in (text or "").splitlines():
        if "verdict" not in line.lower():
            continue
        for m in _VERDICT_TOKEN_RE.finditer(line):
            tok = m.group(1).upper()
            r = _VERDICT_RANK[tok]
            if r > best_rank:
                best, best_rank = tok, r
    return best


def run_image_review(mode: str, video: int,
                     manifest_rel: str | None = None) -> tuple[str, str, str]:
    """Dispatch image-reviewer and read back its verdict file.

    mode: "prompts" (Pass A, pre-spend) | "renders" (Pass B, pre-assemble).
    manifest_rel: if given, pin the reviewer to that exact manifest (keeps the
    reviewed file == the spent file even when a state-file override is set).
    Returns (verdict, verdict_rel, detail). verdict is one of
    SHIP / SHIP WITH FIXES / HOLD-SPEND / REVISE, or the sentinel "UNAVAILABLE"
    when the reviewer could not run or emitted no parseable VERDICT line — the
    caller decides fail-open vs fail-closed on UNAVAILABLE per its gate.
    The verdict FILE is the source of truth; stdout is only a fallback parse."""
    verdict_rel = _image_review_verdict_rel(video)
    agent_file = AGENTS_DIR / f"{IMAGE_REVIEWER_AGENT}.md"
    if not agent_file.exists():
        return "UNAVAILABLE", verdict_rel, f"agent definition not found at {agent_file}"
    v = f"Video_{nn(video)}"
    label = "A (PROMPTS, pre-spend)" if mode == "prompts" else "B (RENDERS, pre-assemble)"
    pin = f" Use manifest {manifest_rel} (the exact billed file)." if manifest_rel else ""
    prompt = (
        f"Run image-reviewer MODE {label} for {v} (3SK Finance). mode:{mode}.{pin} "
        f"Verify the canonical billed image manifest (and rendered PNGs in renders "
        f"mode) against the Master Character Prompt v3, the On-Model Verification "
        f"Protocol, and the Background & Color Standard. WRITE your verdict to "
        f"{verdict_rel} including a 'VERDICT:' line "
        f"(SHIP / SHIP WITH FIXES / HOLD-SPEND / REVISE)."
    )
    cmd = [CLAUDE_CLI_PATH, "--print", "--agent", IMAGE_REVIEWER_AGENT,
           "--add-dir", str(WORKSPACE_DIR), "--dangerously-skip-permissions",
           "--", prompt]
    try:
        proc = subprocess.run(cmd, cwd=str(WORKSPACE_DIR), capture_output=True,
                              text=True, timeout=IMAGE_REVIEW_TIMEOUT)
    except subprocess.TimeoutExpired:
        return "UNAVAILABLE", verdict_rel, f"timed out after {IMAGE_REVIEW_TIMEOUT}s"
    if proc.returncode != 0:
        return "UNAVAILABLE", verdict_rel, \
            f"exited {proc.returncode}: {(proc.stderr or '')[-300:]}"
    verdict_abs = vault_abs(verdict_rel)
    text = ""
    if verdict_abs and verdict_abs.exists():
        text = verdict_abs.read_text(encoding="utf-8", errors="replace")
    verdict = _parse_image_verdict(text) or _parse_image_verdict(proc.stdout or "")
    if verdict is None:
        return "UNAVAILABLE", verdict_rel, "no parseable VERDICT line in file or stdout"
    return verdict, verdict_rel, f"{mode} verdict {verdict}"


def run_prompt_fixer(video: int, verdict_rel: str,
                     manifest_rel: str) -> tuple[bool, str]:
    """Dispatch the prompt author to FIX the manifest per an image-reviewer
    verdict — the image analogue of scriptwriter fixing what script-reviewer
    flagged. Edits the manifest in place; bills nothing. Returns (ran_ok, detail);
    ran_ok=False means the fixer itself could not run (caller stops looping)."""
    agent_file = AGENTS_DIR / f"{PROMPT_FIXER_AGENT}.md"
    if not agent_file.exists():
        return False, f"agent definition not found at {agent_file}"
    v = f"Video_{nn(video)}"
    prompt = (
        f"image-reviewer flagged the {v} image PROMPTS as not yet shippable. Read its "
        f"verdict at {verdict_rel} and FIX the billed manifest {manifest_rel} IN PLACE: "
        f"for each flagged shot, correct the prompt per the finding (on-model lock, refs "
        f"discipline, hero rule, banned vocab, the no-baked-text rule, the background "
        f"standard, beat alignment). PRESERVE the manifest JSON schema, the shot order, "
        f"every image `name`, and every already-passing shot UNCHANGED. Do not add or "
        f"remove shots. Write the corrected manifest back to {manifest_rel}."
    )
    cmd = [CLAUDE_CLI_PATH, "--print", "--agent", PROMPT_FIXER_AGENT,
           "--add-dir", str(WORKSPACE_DIR), "--dangerously-skip-permissions",
           "--", prompt]
    try:
        proc = subprocess.run(cmd, cwd=str(WORKSPACE_DIR), capture_output=True,
                              text=True, timeout=IMAGE_REVIEW_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, f"prompt-fixer timed out after {IMAGE_REVIEW_TIMEOUT}s"
    if proc.returncode != 0:
        return False, f"prompt-fixer exited {proc.returncode}: {(proc.stderr or '')[-300:]}"
    return True, (proc.stdout or "").strip()[-200:]


def run_prompt_review_loop(video: int, manifest_rel: str) -> tuple[str, str, str]:
    """The PROMPTS gate as a CLOSED feedback loop (review → fix → re-review),
    mirroring scriptwriter↔script-reviewer. Only HOLD-SPEND (the spend-blocking
    verdict) triggers a fix; SHIP / SHIP WITH FIXES are spendable and end the
    loop. Bounded by IMAGE_REVIEW_MAX_FIX_ATTEMPTS so a manifest the fixer can't
    clear parks for a human instead of looping forever. UNAVAILABLE ends the loop
    immediately (the caller fails open). Returns the FINAL (verdict, vrel, detail).
    Fixing prompts is $0, so this whole loop runs BEFORE the single billed spend."""
    verdict, vrel, detail = run_image_review("prompts", video, manifest_rel)
    attempts = 0
    while verdict == "HOLD-SPEND" and attempts < IMAGE_REVIEW_MAX_FIX_ATTEMPTS:
        attempts += 1
        ran, fdetail = run_prompt_fixer(video, vrel, manifest_rel)
        if not ran:
            return verdict, vrel, f"{detail}; fix attempt {attempts} could not run ({fdetail})"
        verdict, vrel, detail = run_image_review("prompts", video, manifest_rel)
    if attempts:
        s = "s" if attempts != 1 else ""
        detail = f"{detail} (after {attempts} auto-fix attempt{s})"
    return verdict, vrel, detail


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
    s["infra_count"] = 0
    s["park_reason"] = None
    s["pid"] = None
    s["pid_start_token"] = None
    s["note"] = (out or "")[-300:] or "done"


def _is_infra_failure(err: str) -> bool:
    """True if the error string looks like the HOST/toolchain couldn't run the
    stage at all (vs the agent/tool running and failing its task). Such failures
    must NOT consume a retry or park the video — they're transient host problems
    (e.g. the broken ~/.claude/session-env that took down book-update tonight)."""
    low = (err or "").lower()
    return any(marker in low for marker in INFRA_FAILURE_MARKERS)


def _on_failure(s: dict, err: str) -> None:
    s["pid"] = None
    s["pid_start_token"] = None
    if _is_infra_failure(err):
        # Host couldn't run it — leave ready, do NOT burn a task-failure retry.
        # But a host outage that NEVER clears must not loop invisibly forever:
        # bound it with MAX_INFRA so it parks (visibly, park_reason "infra") and
        # surfaces in the digest / needs-you count instead (reviewer HIGH).
        s["infra_count"] = int(s.get("infra_count") or 0) + 1
        s["note"] = ("infra failure (host/toolchain, not a task failure) #"
                     + str(s["infra_count"]) + " — " + (err or "")[-300:])
        if s["infra_count"] >= MAX_INFRA:
            s["status"] = "needs-steve"
            s["park_reason"] = "infra"
        else:
            s["status"] = "ready"
        return
    s["fail_count"] = int(s.get("fail_count") or 0) + 1
    s["note"] = (err or "")[-500:]
    if s["fail_count"] >= MAX_FAILS:
        s["status"] = "needs-steve"
        s["park_reason"] = "failed"
    else:
        s["status"] = "ready"


# Structured outcome of one advance step, so the fleet drainer (--advance-all)
# can decide whether to keep draining a video. .code is the CLI exit code.
#   outcome ∈ {"ran_done", "ran_failed", "infra_skip", "parked_gate", "idle"}
AdvanceResult = namedtuple("AdvanceResult", "code outcome stage line")


def advance_once(sf: StateFile) -> AdvanceResult:
    """Run the SINGLE next ready non-gate stage for one video (reconcile orphans
    + promote gate-exits first). The shared core of --advance and --advance-all;
    the gate-respecting structural invariant lives entirely in select_next, so
    every caller of advance_once inherits no-auto-spend/publish/VO unchanged."""
    stages = sf.data["stages"]
    video = sf.data["video"]
    reset = reconcile_orphans(stages, video)   # may die() on a genuinely-live run
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
            return AdvanceResult(0, "parked_gate", gate, line)
        line = f"🎬 Video {video}: nothing to advance — all stages done or blocked."
        print(line)
        notify(line)
        return AdvanceResult(0, "idle", None, line)

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
        sf.data["last_progress_at"] = now_iso()   # forward progress → resets staleness clock
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
        return AdvanceResult(0, "ran_done", nxt, line)

    # Failure. _on_failure decides infra (host couldn't run it — no task-failure
    # retry burned, bounded by MAX_INFRA) vs genuine task failure.
    infra = _is_infra_failure(out)
    _on_failure(s, out)
    sf.save()
    if infra and s["status"] == "needs-steve":
        # Infra outage that never cleared — parked at MAX_INFRA so it surfaces.
        line = (f"⚠️ Video {video}: stage {nxt} ({STAGE_LABEL[nxt]}) could not run "
                f"{MAX_INFRA}× (infra/host issue) — parked needs-steve. {out[:160]}")
        outcome = "ran_failed"
    elif infra:
        line = (f"⏸ Video {video}: stage {nxt} ({STAGE_LABEL[nxt]}) could not run "
                f"(infra/host issue, NOT a task failure) — left ready, will retry "
                f"(attempt {s.get('infra_count')}/{MAX_INFRA}). {out[:160]}")
        outcome = "infra_skip"
    elif s["status"] == "needs-steve":
        line = (f"⚠️ Video {video}: stage {nxt} ({STAGE_LABEL[nxt]}) FAILED "
                f"{MAX_FAILS}× — parked needs-steve. {out[:160]}")
        outcome = "ran_failed"
    else:
        line = (f"⚠️ Video {video}: stage {nxt} ({STAGE_LABEL[nxt]}) FAILED — {out[:160]}. "
                f"Reset to ready; re-run /pipeline {video} to retry.")
        outcome = "ran_failed"
    print(line)
    notify(line)
    return AdvanceResult(2, outcome, nxt, line)


def cmd_advance(sf: StateFile) -> int:
    """Single-video --advance: one stage, exit. Thin wrapper over advance_once.
    A genuinely-live run surfaces as die() (exit 1), preserving prior CLI UX."""
    try:
        return advance_once(sf).code
    except LiveRunError as e:
        die(str(e))


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
    # Strictly read-only: --status NEVER mutates or saves. Orphaned/stale running
    # stages are flagged inline ("running?") via the non-mutating _looks_orphaned
    # probe; the actual reconcile + gate-exit promotion happen on the next --advance
    # (or fleet sweep), which holds the lock and persists.
    title = sf.data.get("title", "")
    print(f"Video {video} — {title}")
    print(f"{'stage':<14} {'effective':<12} {'gate':<5} {'owner':<12} artifact")
    print("-" * 78)
    for key in STAGE_ORDER:
        s = stages[key]
        eff = effective_status(key, stages)
        if eff == "running" and _looks_orphaned(s, key):
            eff = "running?"  # likely orphaned/stale — advance will reconcile it
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


def cmd_spend_ok(sf: StateFile, force: bool = False) -> int:
    """Decision 4 (C1): the ONLY billed-spend path. Confirms stage 5 is ready,
    confirms the scene manifest exists, runs the pre-spend image-review gate,
    then shells generate_images.py exactly once. NEVER reachable from --advance.

    force=True (CLI --force only; the Telegram /pipeline spend-ok path never sets
    it) skips the pre-spend image-review gate as a Steve override."""
    stages = sf.data["stages"]
    video = sf.data["video"]
    # A genuinely-live run surfaces as die() (exit 1) — same CLI contract as
    # cmd_advance — so the billed path gives a clean "refusing to double-run …"
    # message instead of a raw traceback, BEFORE any spend or state mutation.
    try:
        reconcile_orphans(stages, video)
    except LiveRunError as e:
        die(str(e))
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

    # Resolve + confirm the billed image manifest BEFORE spending. This is the
    # SAME file the image-reviewer audits and the prompt-fixer edits.
    manifest_rel = canonical_manifest_rel(video, stages)
    manifest_abs = vault_abs(manifest_rel)
    if manifest_abs is None or not manifest_abs.exists():
        s["note"] = f"spend refused: image manifest not found at {manifest_rel}"
        sf.save()
        line = (f"⚠️ Video {video}: image manifest not found ({manifest_rel}) — "
                f"refusing to spend. Author it first.")
        print(line)
        notify(line)
        return 2

    # Pass A — pre-spend PROMPTS gate, run as a CLOSED review→fix→re-review loop
    # (image-reviewer flags → scene-image-prompt-generator fixes the manifest →
    # re-review, bounded). All of this is $0 and happens BEFORE the single billed
    # spend. A HOLD-SPEND that survives the fix budget refuses the batch; --force
    # (CLI-only) skips the gate; fail-OPEN on reviewer infra (a human authorized
    # this spend — don't block the billed path on review tooling failing to run).
    if force:
        print(f"⏭️  Video {video}: --force — skipping pre-spend image-review gate.")
    else:
        verdict, vrel, detail = run_prompt_review_loop(video, manifest_rel)
        # ALLOW-LIST, not deny-list: spend ONLY on an explicitly spendable verdict
        # (SHIP / SHIP WITH FIXES). UNAVAILABLE is the single fail-OPEN exception
        # (a human authorized THIS spend; review tooling being down must not block
        # it). EVERY other verdict — HOLD-SPEND, REVISE, or anything unexpected the
        # reviewer emits — fails CLOSED and refuses the batch. This matches the
        # parser's fail-safe philosophy: a false block is cheap (--force overrides),
        # a false SHIP bills real money. A deny-list `else: spend` would leak a
        # REVISE (which the loop does NOT auto-fix) straight to a billed spend.
        if verdict == "UNAVAILABLE":
            line = (f"⚠️ Video {video}: image-reviewer could not run the pre-spend "
                    f"prompts review ({detail}) — proceeding with spend (fail-open). "
                    f"Eyeball the prompts manually.")
            print(line)
            notify(line)
        elif verdict in ("SHIP", "SHIP WITH FIXES"):
            line = f"✅ Video {video}: image-reviewer pre-spend verdict {verdict} ({vrel})."
            print(line)
            # SHIP WITH FIXES still spends (fixes are low-cost text/vocab), but
            # surface it on Telegram so the required fixes don't hide in the file.
            if verdict == "SHIP WITH FIXES":
                notify(line + " Low-cost fixes noted — apply post-render.")
        else:
            s["note"] = f"spend held: image-reviewer {verdict} ({vrel})"
            sf.save()
            line = (f"🛑 Video {video}: image-reviewer {verdict} on the image prompts "
                    f"after auto-fix — refusing to spend. Fix per {vrel}, then re-run "
                    f"spend-ok (or override with --force). ({detail})")
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


# === Fleet (multi-video) ===================================================

def discover_videos() -> list[int]:
    """Every Video_NN_pipeline.json in STATE_DIR, ascending. Ignores .tmp/junk."""
    if not STATE_DIR.exists():
        return []
    vids = set()
    for p in STATE_DIR.glob("Video_*_pipeline.json"):
        m = re.fullmatch(r"Video_(\d+)_pipeline\.json", p.name)
        if m:
            vids.add(int(m.group(1)))
    return sorted(vids)


def _looks_orphaned(s: dict, key: str) -> bool:
    """Read-only orphan test (no mutation, no die) — mirrors reconcile_orphans'
    3-condition logic, for the supervise digest's stuck-run detection."""
    pid = s.get("pid")
    started = parse_iso(s.get("started_at"))
    timeout = RUN_TABLE.get(key, {}).get("timeout", 1800)
    dead = not _pid_alive(pid)
    cur = _proc_start_token(pid)
    rec = s.get("pid_start_token")
    recycled = (not dead and cur is not None and rec is not None and cur != rec)
    timed_out = (started is not None and (time.time() - started) > timeout)
    return dead or recycled or timed_out


def _days_since(iso: str | None) -> float | None:
    t = parse_iso(iso)
    return None if t is None else (time.time() - t) / 86400.0


# Digest ordering: surface what needs Steve first, completed last.
_CATEGORY_ORDER = {"blocked-stuck": 0, "needs-steve": 1, "running": 2,
                   "ready": 3, "blocked": 4, "complete": 5}


def _video_summary_line(data: dict, ran: list[str] | None = None) -> tuple[str, str]:
    """Classify one video into (category, one-line-text). Read-only. `ran` (the
    stages just advanced this pass) is prefixed when present (--advance-all)."""
    video = data.get("video")
    title = data.get("title", "") or ""
    stages = data.get("stages", {})
    present = [k for k in STAGE_ORDER if k in stages]
    if present and all(stages[k].get("status") == "done" for k in present):
        return ("complete", f"✅ V{video} ({title}): complete — all stages done")

    stuck_run = [k for k in STAGE_ORDER
                 if stages.get(k, {}).get("status") == "running" and _looks_orphaned(stages[k], k)]
    live_run = [k for k in STAGE_ORDER
                if stages.get(k, {}).get("status") == "running" and not _looks_orphaned(stages[k], k)]
    failed = [k for k in STAGE_ORDER
              if stages.get(k, {}).get("status") == "needs-steve"
              and stages[k].get("park_reason") == "failed"]
    infra_parked = [k for k in STAGE_ORDER
                    if stages.get(k, {}).get("status") == "needs-steve"
                    and stages[k].get("park_reason") == "infra"]
    # Stages auto-retrying after an infra skip (not yet parked) — visible so a
    # host outage can't hide as a healthy "ready" video (reviewer HIGH).
    infra_retry = [k for k in STAGE_ORDER
                   if stages.get(k, {}).get("status") == "ready"
                   and int(stages.get(k, {}).get("infra_count") or 0) > 0]
    gates = gates_awaiting_steve(stages)
    nxt = select_next(stages)

    bits: list[str] = []
    if ran:
        bits.append("advanced " + ", ".join(STAGE_LABEL.get(k, k) for k in ran))

    emoji, category = "⏳", "ready"
    if stuck_run:
        emoji, category = "🔴", "blocked-stuck"
        bits.append("STUCK running: " + ", ".join(STAGE_LABEL.get(k, k) for k in stuck_run)
                    + f" (clear: /pipeline {video} force-reset)")
    if failed:
        emoji, category = "🔴", "blocked-stuck"
        bits.append(f"FAILED {MAX_FAILS}× (needs you): "
                    + ", ".join(STAGE_LABEL.get(k, k) for k in failed))
    if infra_parked:
        emoji, category = "🔴", "blocked-stuck"
        bits.append(f"HOST CAN'T RUN (infra, {MAX_INFRA}× — needs you): "
                    + ", ".join(STAGE_LABEL.get(k, k) for k in infra_parked))
    if infra_retry:
        if category == "ready":
            emoji, category = "🟠", "blocked-stuck"
        bits.append("infra-retrying (host issue, not a task failure): "
                    + ", ".join(f"{STAGE_LABEL.get(k, k)} ×{stages[k].get('infra_count')}"
                                for k in infra_retry))
    if gates:
        gtxt = []
        for g in gates:
            if g == "5_images":
                gtxt.append(f"{STAGE_LABEL[g]} (BILLED → /pipeline {video} spend-ok)")
            else:
                gtxt.append(STAGE_LABEL.get(g, g))
        if category == "ready":
            emoji, category = "⛔", "needs-steve"
        bits.append("waiting on you: " + ", ".join(gtxt))
    if nxt:
        bits.append(f"ready to advance: {STAGE_LABEL.get(nxt, nxt)}")
    if live_run and not stuck_run:
        bits.append("running: " + ", ".join(STAGE_LABEL.get(k, k) for k in live_run))
        if category == "ready":
            emoji, category = "🏃", "running"
    if not bits:
        emoji, category = "🚧", "blocked"
        bits.append("blocked (upstream stages not done)")

    # Staleness from last FORWARD progress, not updated_at (which bumps on every
    # save incl. infra-retry + gate-park), so an infra loop still goes stale and a
    # long-parked gate still nudges. Fall back for pre-field state files.
    progress_at = data.get("last_progress_at") or data.get("updated_at") or data.get("created_at")
    age = _days_since(progress_at)
    stale = f"⏰{int(age)}d no progress · " if (age is not None and age > STALE_DAYS) else ""
    return (category, f"{emoji} V{video} ({title}): {stale}" + " · ".join(bits))


def _fleet_digest(header: str, videos: list[int], lines: list[tuple[str, str]]) -> str:
    ordered = sorted(lines, key=lambda cl: _CATEGORY_ORDER.get(cl[0], 9))
    n_done = sum(1 for c, _ in lines if c == "complete")
    n_action = sum(1 for c, _ in lines if c in ("blocked-stuck", "needs-steve"))
    body = "\n".join(text for _, text in ordered)
    return (f"{header}\n{len(videos)} video(s) · {n_done} complete · {n_action} need you\n"
            + body)


# Outcomes a human must hear about. Under --quiet-idle the fleet digest is sent
# only when one of these occurred this tick; a clean all-gate-parked / idle tick
# stays silent. infra_skip is deliberately included so a host-level failure (a
# stale `claude login`, a broken session-env, EPERM) that wedges the whole fleet
# can NEVER hide behind --quiet-idle — exactly the silent-stall the retry system
# exists to prevent (skeptical-code-reviewer HIGH, 2026-06-18).
NEWS_OUTCOMES = frozenset({"ran_done", "ran_failed", "infra_skip"})


def _outcome_is_news(outcome: str) -> bool:
    """True iff this per-stage outcome should break --quiet-idle silence."""
    return outcome in NEWS_OUTCOMES


def cmd_advance_all(quiet_idle: bool = False) -> int:
    """Fleet drain: for EACH video, advance through clean successes until the
    first gate/failure/idle (capped), then send ONE consolidated digest. Still a
    sequencer — every step goes through advance_once, so it cannot cross a gate,
    spend, publish, or record VO. Per-video errors are isolated; a genuinely-live
    run on one video is skipped, never aborting the sweep.

    quiet_idle: for a frequent (e.g. hourly) cron cadence — suppress the Telegram
    digest on ticks where NO stage actually executed, so steady-state gate-parked
    fleets don't ping every hour. A stage running OR failing counts as news."""
    global SUPPRESS_NOTIFY
    videos = discover_videos()
    if not videos:
        msg = "🎬 Pipeline sweep: no video state files found in Production_Kits."
        print(msg)
        if not quiet_idle:
            notify(msg, force=True)
        return 0
    lines: list[tuple[str, str]] = []
    any_news = False  # did anything happen this tick that Steve must hear about?
    SUPPRESS_NOTIFY = True
    try:
        for v in videos:
            ran: list[str] = []
            try:
                with StateFile(v) as sf:
                    sf.load()
                    if sf.data.get("video") != v:
                        lines.append(("blocked-stuck",
                                      f"⚠️ V{v}: state file video-id mismatch — skipped"))
                        any_news = True  # corrupt state — break --quiet-idle silence
                        continue
                    for _ in range(MAX_STAGES_PER_RUN):
                        res = advance_once(sf)
                        if res.outcome == "ran_done":
                            ran.append(res.stage)
                            any_news = True
                            continue
                        # ran_failed / infra_skip → news; parked_gate / idle → silent.
                        if _outcome_is_news(res.outcome):
                            any_news = True
                        break  # gate / failure / infra / idle → stop draining this video
                    lines.append(_video_summary_line(sf.data, ran or None))
            except LiveRunError:
                # A stage is genuinely live (a real process still owns it) — skip,
                # never abort. Benign: expected when a long stage spans the tick.
                lines.append(("running", f"🏃 V{v}: a stage is genuinely live (left untouched)"))
            except SystemExit:
                # die() — corrupt/unreadable state file, video-id mismatch, etc.
                # (the real reason is printed to stderr → launchd logs). NOT benign:
                # surface it so a broken state file can't read as healthy.
                lines.append(("blocked-stuck",
                              f"⚠️ V{v}: state file unusable/corrupt (see logs) — needs you"))
                any_news = True
            except Exception as e:
                lines.append(("blocked-stuck", f"⚠️ V{v}: skipped after error — {e}"))
                any_news = True  # a genuine error this tick — break --quiet-idle silence
    finally:
        SUPPRESS_NOTIFY = False
    digest = _fleet_digest("🎬 Pipeline sweep — fleet advance", videos, lines)
    print(digest)
    if not (quiet_idle and not any_news):
        notify(digest, force=True)
    return 0


def cmd_supervise() -> int:
    """Read-only fleet digest: classify every video (what's done, in-flight,
    stuck, or waiting on Steve) and send one Telegram line set. Takes NO lock and
    mutates NOTHING — atomic writes make lock-free reads consistent, so it can run
    alongside a live advance without blocking or corrupting it."""
    videos = discover_videos()
    if not videos:
        msg = "📋 Pipeline supervise: no video state files found in Production_Kits."
        print(msg)
        notify(msg, force=True)
        return 0
    lines: list[tuple[str, str]] = []
    for v in videos:
        path = STATE_DIR / f"Video_{nn(v)}_pipeline.json"
        try:
            data = json.loads(path.read_text())  # lock-free; os.replace keeps it consistent
            lines.append(_video_summary_line(data, None))
        except Exception as e:
            lines.append(("blocked-stuck", f"⚠️ V{v}: unreadable state file — {e}"))
    digest = _fleet_digest("📋 Pipeline supervise — fleet status", videos, lines)
    print(digest)
    notify(digest, force=True)
    return 0


# === Main ==================================================================

def main() -> int:
    p = argparse.ArgumentParser(description="Pipeline orchestrator (deterministic video sequencer).")
    p.add_argument("--video", type=int, default=None,
                   help="Video number, e.g. 1 (required for per-video commands; omit for fleet commands).")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--advance", action="store_true", help="Run the next ready non-gate stage.")
    g.add_argument("--status", action="store_true", help="Print the state table; run nothing.")
    g.add_argument("--init", action="store_true", help="Seed a fresh state file.")
    g.add_argument("--spend-ok", action="store_true", help="Authorize the BILLED image batch (stage 5).")
    g.add_argument("--force-reset", action="store_true", help="Reset a wedged running stage (orphan-tested).")
    g.add_argument("--advance-all", action="store_true",
                   help="FLEET: drain every video to its next gate; one digest. No --video.")
    g.add_argument("--supervise", action="store_true",
                   help="FLEET: read-only status digest across every video. No --video.")
    p.add_argument("--title", help="Title for --init.")
    p.add_argument("--force-init", action="store_true", help="Overwrite an existing state file on --init.")
    p.add_argument("--force", action="store_true",
                   help="With --spend-ok: skip the pre-spend image-review gate (Steve override). "
                        "CLI-only; the Telegram /pipeline spend-ok path never sets it.")
    p.add_argument("--quiet-idle", action="store_true",
                   help="With --advance-all: suppress the Telegram digest when no stage executed "
                        "this run (for a frequent cron cadence). No effect on other commands.")
    args = p.parse_args()

    # Fleet commands operate over ALL video state files and take no --video.
    if args.advance_all or args.supervise:
        if args.video is not None:
            die("Fleet commands (--advance-all/--supervise) operate on ALL videos; do not pass --video.")
        return cmd_advance_all(quiet_idle=args.quiet_idle) if args.advance_all else cmd_supervise()

    # Per-video commands require a positive --video.
    if args.video is None:
        die("--video is required for per-video commands (--advance/--status/--init/--spend-ok/--force-reset).")
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
            return cmd_spend_ok(sf, force=args.force)
        if args.force_reset:
            return cmd_force_reset(sf)
    return 0


if __name__ == "__main__":
    sys.exit(main())
