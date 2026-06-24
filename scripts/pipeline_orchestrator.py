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
    "reviewer-unavailable",              # a BINARY review gate could not RUN (timeout/
                                         # outage/auth-wobble). Treat as infra so the stage
                                         # is left 'ready' and retried — NEVER fail-open
                                         # advance unreviewed work (the V04 regression).
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
    "8_thumbnail":   {"kind": "script", "agent": None,                       "timeout": 600},
    "9_description": {"kind": "agent",  "agent": "video-description-writer",  "timeout": 600},
    "11_analyze":    {"kind": "agent",  "agent": "channel-analyst",          "timeout": 720},
}

# Human-artifact gates that CAN auto-promote needs-steve→done when a real
# artifact lands on disk (non-empty AND fresher than deps). Only gates with a
# fixed, unambiguous on-disk artifact convention are listed. 10 publish has no
# fixed local artifact path, so it NEVER auto-promotes — Steve clears it by
# hand-editing status:done (the always-available manual exit). The orchestrator
# never guesses. NOTE on 3 vo_expand: it is NOT in this plain-artifact table — it
# has its OWN reviewed auto-promote (promote_gate_exits → _maybe_promote_vo_expand),
# advancing only when the billed VO kit clears the vo-reviewer SHIP gate, while the
# ElevenLabs render stays manual (Cowork). Hand-editing status:done stays available.
# NOTE on 8 thumbnail: for videos initialised AFTER 2026-06-20 it is an
# orchestrator-run $0 script-stage (the thumbnail ART renders inside the stage-5
# billed batch, then card_overlay.py burns the title text), so it advances via
# --advance, not via this promote table. Legacy in-flight state files that still
# carry 8 as a steve-gate keep the manual hand-edit exit — unchanged.
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
        # Thumbnail ART renders in the stage-5 billed batch (Video_NN_Thumbnail_A/_B
        # entries in video_NN_hd.json, already cleared by the stage-5 RENDERS gate);
        # this stage is the $0 deterministic title-text burn (card_overlay.py). Needs
        # BOTH the rendered art (5_images) and the approved overlay text (7_packaging).
        "8_thumbnail":   _stage("orchestrator", False, ["5_images", "7_packaging"]),
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


def deps_freshness_threshold(key: str, stages: dict) -> tuple[float | None, bool]:
    """Freshness threshold for an auto-promotion freshness check, plus a fail-closed
    flag. Returns (threshold, deps_incomplete):

      - No deps at all          → (None, False): freshness is vacuously satisfied.
      - All deps have a stamp    → (max_completed_at, False): normal freshness gate.
      - ANY dep lacks completed_at (null) → (max_of_known_or_None, True): the upstream
        stage is NOT actually done, so a downstream artifact must NEVER auto-promote off
        it — the True flag tells the caller to fail CLOSED. This closes the hole where a
        threshold of None (deps present but unstamped) silently skipped the staleness
        check and green-lit a stale/orphan artifact (the stale-VO promotion bug)."""
    deps = stages.get(key, {}).get("deps", [])
    if not deps:
        return None, False
    ts, incomplete = [], False
    for d in deps:
        t = parse_iso(stages.get(d, {}).get("completed_at"))
        if t is None:
            incomplete = True
        else:
            ts.append(t)
    return (max(ts) if ts else None), incomplete


def _artifact_nonempty_and_fresh(spec: dict, video: int, threshold: float | None,
                                 deps_incomplete: bool = False) -> tuple[bool, str]:
    """Decision 1a: (1) exists & non-empty AND (2) mtime strictly newer than
    the deps' max completed_at. Returns (ok, reason-if-not).

    deps_incomplete=True means at least one upstream dep has no completed_at — the
    stage isn't legitimately done, so we fail CLOSED regardless of mtime."""
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
    if deps_incomplete:
        return False, f"upstream dep not yet completed (null completed_at) — won't auto-promote: {rel}"
    if threshold is not None and not (mtime > threshold):
        return False, f"artifact older than its deps (stale): {rel}"
    return True, ""


def promote_gate_exits(stages: dict, video: int) -> tuple[list[str], bool]:
    """Decision 2 step 3: re-evaluate needs-steve exits for gate-parked stages.
    Human-artifact gates auto-promote ONLY when the artifact is non-empty AND
    fresher than deps. failed-parked stages are skipped entirely. The billed
    gate (5) is promoted only by --spend-ok, never here. 3_vo_expand is a special
    REVIEWED auto-promote: it advances only when the billed VO kit clears the
    vo-reviewer gate (SHIP) — see _maybe_promote_vo_expand — while the render stays
    manual. Returns (promoted_keys, changed): `changed` is True when a non-promoting
    mutation (the vo-review verdict cache) must still be persisted by the caller."""
    promoted: list[str] = []
    changed = False
    for key in STAGE_ORDER:
        s = stages.get(key)
        if not s or s.get("status") != "needs-steve":
            continue
        if s.get("park_reason") != "gate":  # N2: failed-parked never auto-promotes
            continue
        if key == "5_images":  # billed gate: spend-ok only
            continue
        threshold, deps_incomplete = deps_freshness_threshold(key, stages)
        if key == "3_vo_expand":  # reviewed auto-promote: vo-reviewer gates the kit
            outcome = _maybe_promote_vo_expand(s, video, threshold, deps_incomplete)
            if outcome == "promoted":
                promoted.append(key)
            elif outcome == "changed":
                changed = True
            continue
        spec = GATE_ARTIFACTS.get(key)
        if not spec:  # no fixed artifact convention → manual hand-edit only
            continue
        ok, reason = _artifact_nonempty_and_fresh(spec, video, threshold, deps_incomplete)
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
    return promoted, changed


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
    """Route an agent stage to its executor under the BINARY quality-control policy
    (Steve, 2026-06-20: every step has a review + feedback-loop gate).

    - Stage 2 is REVIEW-ONLY (stage 1 produced the script): run the script
      review→fix→re-review loop and advance only on a clean SHIP.
    - The producer stages that emit a billable/publishable artifact (7 packaging,
      9 description, 11 analyze) are PRODUCE-THEN-REVIEW: dispatch the producer,
      then a dedicated specialist reviewer gates the output through the same
      binary loop before the stage can mark done.
    - Everything else is a plain producer dispatch.

    Each gate owns its own agent-availability handling: a reviewer that cannot
    RUN (UNAVAILABLE — timeout/outage/unparseable) is treated as an INFRA failure
    (stage left 'ready', retried next sweep, parked after MAX_INFRA), NOT a pass.
    A content stage therefore advances only on a real SHIP and never ships
    unreviewed work, while a transient reviewer outage self-heals on retry."""
    if key == "2_review":
        return run_script_review_gate(video)
    if key in STAGE_REVIEW:
        return run_stage_review_gate(key, video)
    return _dispatch_stage_agent(key, video)


def _dispatch_stage_agent(key: str, video: int) -> tuple[bool, str]:
    """Dispatch a claude subagent exactly as iris.py:_run_dispatch does, blocking.
    This is the bare PRODUCER dispatch (no review) — the review gates call it as
    their produce step, and run_agent_stage calls it directly for ungated stages.

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
    """Local $0 factory stages. Stage 6 assemble: a SINGLE build_video.py
    --assemble call. Stage 8 thumbnail: a SINGLE card_overlay.py title-text burn.
    MUST pass --assemble; MUST NEVER pass --images/--vo."""
    if key == "8_thumbnail":
        return run_thumbnail_overlay_stage(video)
    if key != "6_assemble":
        return False, f"no script executor for stage {key}"
    # Pass B — pre-assemble RENDERS review, pinned to the SAME billed manifest the
    # spend used. BINARY allow-list (Steve, 2026-06-20): assemble ONLY on a clean
    # SHIP — a 100% sign-off that every rendered PNG is on-model and legible.
    # ANYTHING that is not a clean SHIP (REVISE, the retired SHIP WITH FIXES, or
    # any unexpected token) refuses the build: if it is not all good to go, none of
    # it goes. Returns False → consumes a normal retry (bounded by MAX_FAILS) and
    # eventually parks for Steve. The render-fix LOOP closes through the human
    # money-gate, NOT here: regenerating renders is BILLED, so it must NOT auto-run
    # (no-autonomous-spend invariant). The fix path is the same closed loop as Pass
    # A — correct the prompts per the verdict (free) then re-run `/pipeline N
    # spend-ok`, which re-reviews + regenerates, and this gate re-checks the fresh
    # renders. UNAVAILABLE is the single fail-OPEN exception (assembly is free):
    # warn and assemble anyway.
    verdict, vrel, detail = run_image_review("renders", video,
                                             canonical_manifest_rel(video))
    if verdict == "UNAVAILABLE":
        notify(f"⚠️ Video {video}: image-reviewer could not run the pre-assemble "
               f"renders review ({detail}) — assembling anyway (fail-open).")
    elif verdict != "SHIP":
        return False, (f"image-reviewer {verdict} on the rendered images — refusing to "
                       f"assemble. Fix prompts per {vrel}, then re-run /pipeline "
                       f"{video} spend-ok to regenerate (BILLED — human-gated) ({detail}).")
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


def _warn_thumbnail_name_drift(video: int, manifest_abs: Path) -> None:
    """$0 pre-spend guard: if the stage-8 overlay spec exists, warn when it defines
    a thumbnail card with no matching image entry in the billed manifest (the names
    must agree or stage 8 parks after spend). Best-effort and silent on any read
    error or when the spec isn't authored yet — never blocks the billed path."""
    spec_abs = ROOT / f"image_factory/thumb_overlay_v{nn(video)}.json"
    if not spec_abs.exists():
        return
    try:
        spec_cards = set(json.loads(spec_abs.read_text()).get("cards", {}))
        manifest_names = {
            img.get("name") for img in json.loads(manifest_abs.read_text()).get("images", [])
        }
    except (json.JSONDecodeError, OSError):
        return
    missing = sorted(c for c in spec_cards if c not in manifest_names)
    if missing:
        notify(f"⚠️ Video {video}: overlay spec card(s) {missing} have no matching image "
               f"entry in the billed manifest — stage-8 thumbnail burn would park for "
               f"these. Fix the name(s) before spending, or the variant won't render.")


def run_thumbnail_overlay_stage(video: int) -> tuple[bool, str]:
    """Stage 8 thumbnail: a deterministic, $0 title-text burn — NO generation,
    NO billing. The thumbnail ART (Video_NN_Thumbnail_A/_B.png) was rendered in
    the SAME stage-5 billed batch as the scene shots (scene-image-prompt-generator
    appends those entries to video_NN_hd.json) and has already cleared the stage-5
    RENDERS on-model gate, so by here it sits in Raw_Assets/Video_NN_HD. Assembly
    ignores it (build_video pulls only `<vid>_Shot_<id>.png` named in the shot
    list, never `<vid>_Thumbnail_*`). This stage composites the packaging-approved
    title text onto that art via card_overlay.py (PIL) into Thumbnails/Video_NN_gen.

    Missing inputs return False so the stage parks for a human (after the normal
    retry budget) rather than silently advancing publish with no thumbnail:
      - overlay spec absent  -> thumbnail-coordinator never emitted it
      - thumbnail art absent -> the stage-5 batch lacked the Thumbnail entries
    """
    vid = f"Video_{nn(video)}"
    spec_rel = f"image_factory/thumb_overlay_v{nn(video)}.json"
    spec_abs = ROOT / spec_rel
    if not spec_abs.exists():
        return False, (f"thumbnail overlay spec missing ({spec_rel}); thumbnail-coordinator "
                       f"must emit it from the approved packaging title text.")
    try:
        spec_cards = set(json.loads(spec_abs.read_text()).get("cards", {}))
    except (json.JSONDecodeError, OSError) as e:
        return False, f"thumbnail overlay spec {spec_rel} unreadable: {e}"
    art_dir = vault_abs(f"{VAULT_REL}/Raw_Assets/{vid}_HD")
    # Burn ONLY the variants whose backplate actually rendered (excluding any
    # already-composited *_FINAL) AND that the spec defines a card for. card_overlay
    # hard-fails both on a missing backplate AND on an --only name absent from the
    # spec, so intersect the two; if the intersection is empty, park for a human.
    rendered = {
        p.stem for p in (art_dir.glob(f"{vid}_Thumbnail_*.png") if art_dir else [])
        if not p.stem.endswith("_FINAL")
    }
    backplates = sorted(rendered & spec_cards)
    if not backplates:
        return False, (f"no overlay-able thumbnail variant for {vid}: rendered backplates "
                       f"{sorted(rendered) or '[]'} vs spec cards {sorted(spec_cards) or '[]'}. "
                       f"The stage-5 batch must render Video_NN_Thumbnail entries whose names "
                       f"match the {spec_rel} card keys.")
    out_abs = vault_abs(f"{VAULT_REL}/Thumbnails/{vid}_gen")
    out_abs.mkdir(parents=True, exist_ok=True)
    cfg = RUN_TABLE["8_thumbnail"]
    cmd = [
        sys.executable, "image_factory/card_overlay.py", str(spec_abs),
        "--base-dir", str(art_dir), "--out-dir", str(out_abs), "--suffix", "_FINAL",
    ]
    for name in backplates:
        cmd += ["--only", name]
    try:
        proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True,
                              text=True, timeout=cfg["timeout"])
    except subprocess.TimeoutExpired:
        return False, f"card_overlay.py timed out after {cfg['timeout']}s"
    if proc.returncode != 0:
        return False, f"card_overlay.py exited {proc.returncode}: {(proc.stderr or '')[-500:]}"
    finals = list(out_abs.glob(f"{vid}_Thumbnail_*_FINAL.png"))
    if not finals:
        return False, "card_overlay.py ran 0-exit but produced no *_FINAL.png"
    # A/B completeness: the feature ships TWO variants. If the spec defines cards we
    # could NOT burn (a variant's backplate failed to render in the stage-5 batch),
    # we still advance on whatever rendered — a usable thumbnail beats parking the
    # whole publish on a non-critical A/B alt — but we must NOT do it silently. Emit
    # a Telegram warning and surface the gap in the stage message.
    dropped = sorted(spec_cards - set(backplates))
    if dropped:
        notify(f"⚠️ Video {video} stage-8 thumbnail: burned {sorted(backplates)} but "
               f"spec variant(s) {dropped} had no rendered backplate — shipping "
               f"{len(finals)} of {len(spec_cards)} thumbnail variants. Re-render the "
               f"missing variant if A/B coverage is needed.")
    tail = (proc.stdout or "").strip()[-220:]
    note = f"burned {len(finals)}/{len(spec_cards)} variants" + (
        f"; DROPPED {dropped}" if dropped else "")
    return True, (f"{note}. {tail}").strip()


# === Image-review gate (image-reviewer subagent) ==========================
# BINARY gate policy (Steve, 2026-06-20): each pass advances ONLY on a clean SHIP
# (a 100% sign-off). Anything less is not advanceable — there is no "ship with
# fixes" middle state.
# Pass A: pre-spend PROMPTS review inside cmd_spend_ok — spends ONLY on SHIP;
#         anything else drives the fix loop, then parks (HOLD-SPEND et al.).
# Pass B: pre-assemble RENDERS review inside run_script_stage — assembles ONLY on
#         SHIP; anything else refuses the cut (REVISE et al., human-gated re-spend).
IMAGE_REVIEWER_AGENT = "image-reviewer"
# The author that FIXES flagged prompts — the image analogue of scriptwriter in
# the scriptwriter↔script-reviewer loop. It edits the manifest in place; $0.
PROMPT_FIXER_AGENT = "scene-image-prompt-generator"
# Stage-2 script gate (BINARY, same policy): script-reviewer critiques, and on
# anything less than a clean SHIP the scriptwriter FIXES the script in place and
# it is re-reviewed — a closed loop, $0, mirroring the image PROMPTS gate.
SCRIPT_REVIEWER_AGENT = "script-reviewer"
SCRIPT_FIXER_AGENT = "scriptwriter"
# Stage-3 VO gate (BINARY, same policy): vo-reviewer audits the billed VO kit for
# TTS hazards (tickers read as words, $7,500, 401(k), homographs, …) BEFORE the
# ElevenLabs render. The render itself STAYS MANUAL (Cowork) — billed, weekly, and
# better with a human who can react to ElevenLabs account warnings and actually
# HEAR a bad clip — so this gate does NOT own the spend. It is wired as a REVIEWED
# AUTO-PROMOTE on the existing manual 3_vo_expand gate (see promote_gate_exits),
# not as an --advance stage: it certifies the kit TTS-clean and emits the SHIP
# go-signal, a REVISE keeps the stage parked with the fix list, and the human
# renders only on SHIP. vo-reviewer is read-only ($0) so re-review is free, but the
# kit mtime is cached so it is dispatched at most once per kit revision.
VO_REVIEWER_AGENT = "vo-reviewer"
VO_REVIEW_TIMEOUT = 600
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
# A CANONICAL verdict line: optional markdown decoration (#, *, _, >, whitespace)
# then the word "verdict" then an optional separator (:, -, *, _, whitespace).
# This recognises "VERDICT:", "## Verdict", "**Verdict** —", "> Verdict:" etc.,
# but NOT prose like "my verdict is that this looks shippable" (no separator-led
# token follows on the line / next line in the structured sense we key on).
_VERDICT_LABEL_RE = re.compile(r"^\s*[*_#>\s]*verdict\b[*_:\s-]*", re.IGNORECASE)


def _image_review_verdict_rel(video: int) -> str:
    return f"{VAULT_REL}/Raw_Assets/Image_Factory/_REVIEW/Video_{nn(video)}_Image_Review.md"


def _tokens_in(line: str):
    """Yield the (token, rank) pairs in a line, normalised + ranked."""
    for m in _VERDICT_TOKEN_RE.finditer(line or ""):
        tok = m.group(1).upper()
        yield tok, _VERDICT_RANK[tok]


def _parse_image_verdict(text: str) -> str | None:
    """Most-severe verdict token, preferring CANONICAL 'VERDICT:' lines, or None.

    Two passes, fail-safe toward the most-blocking token throughout:

    1. CANONICAL pass — scan for lines the reviewer is told to emit ("VERDICT:"
       style, incl. markdown-decorated headers like "## Verdict"). The token may
       sit on the SAME line ("VERDICT: REVISE") or, when the label is a header,
       on the NEXT non-blank line ("## Verdict\\nSHIP"). If ANY canonical label is
       present we trust ONLY those lines and return their most-severe token — this
       kills prose false-positives (a sentence elsewhere that happens to say
       "ship") AND the header false-negative (token on the line after the label).

    2. LEGACY fallback — only if NO canonical label exists anywhere, fall back to
       the old "any line mentioning the word verdict" scan, so older/odd verdict
       files still parse rather than silently degrading to UNAVAILABLE.

    Returns None only when no token is found at all; the caller treats None as
    UNAVAILABLE (blocking under the binary gates), never as SHIP."""
    lines = (text or "").splitlines()
    canon_best, canon_rank = None, 0
    saw_label = False
    for i, line in enumerate(lines):
        if not _VERDICT_LABEL_RE.match(line):
            continue
        saw_label = True
        # Same-line token (e.g. "VERDICT: REVISE").
        found_same_line = False
        for tok, r in _tokens_in(line):
            found_same_line = True
            if r > canon_rank:
                canon_best, canon_rank = tok, r
        # Header form: token on the next non-blank line (e.g. "## Verdict\nSHIP").
        if not found_same_line:
            for j in range(i + 1, len(lines)):
                if not lines[j].strip():
                    continue
                for tok, r in _tokens_in(lines[j]):
                    if r > canon_rank:
                        canon_best, canon_rank = tok, r
                break
    if saw_label:
        return canon_best
    # Legacy fallback: no canonical label anywhere.
    best, best_rank = None, 0
    for line in lines:
        if "verdict" not in line.lower():
            continue
        for tok, r in _tokens_in(line):
            if r > best_rank:
                best, best_rank = tok, r
    return best


def run_image_review(mode: str, video: int,
                     manifest_rel: str | None = None) -> tuple[str, str, str]:
    """Dispatch image-reviewer and read back its verdict file.

    mode: "prompts" (Pass A, pre-spend) | "renders" (Pass B, pre-assemble).
    manifest_rel: if given, pin the reviewer to that exact manifest (keeps the
    reviewed file == the spent file even when a state-file override is set).
    Returns (verdict, verdict_rel, detail). Under the BINARY policy the reviewer
    emits SHIP (clean, advances) or a block token (HOLD-SPEND for prompts / REVISE
    for renders — anything less than 100%); the parser still RECOGNISES the retired
    SHIP WITH FIXES token for robustness, but every caller treats non-SHIP as
    blocking. "UNAVAILABLE" is the sentinel when the reviewer could not run or
    emitted no parseable VERDICT line — the caller decides fail-open vs fail-closed
    on UNAVAILABLE per its gate.
    The verdict FILE is the source of truth; stdout is only a fallback parse."""
    verdict_rel = _image_review_verdict_rel(video)
    agent_file = AGENTS_DIR / f"{IMAGE_REVIEWER_AGENT}.md"
    if not agent_file.exists():
        return "UNAVAILABLE", verdict_rel, f"agent definition not found at {agent_file}"
    v = f"Video_{nn(video)}"
    label = "A (PROMPTS, pre-spend)" if mode == "prompts" else "B (RENDERS, pre-assemble)"
    block_tok = "HOLD-SPEND" if mode == "prompts" else "REVISE"
    pin = f" Use manifest {manifest_rel} (the exact billed file)." if manifest_rel else ""
    prompt = (
        f"Run image-reviewer MODE {label} for {v} (3SK Finance). mode:{mode}.{pin} "
        f"Verify the canonical billed image manifest (and rendered PNGs in renders "
        f"mode) against the Master Character Prompt v3, the On-Model Verification "
        f"Protocol, and the Background & Color Standard. WRITE your verdict to "
        f"{verdict_rel} including a 'VERDICT:' line. The verdict is BINARY: SHIP "
        f"(every shot is 100% good to go) or {block_tok} (anything less — list every "
        f"fix under {block_tok}). There is no 'ship with fixes': if it is not all "
        f"good to go, none of it goes."
    )
    cmd = [CLAUDE_CLI_PATH, "--print", "--agent", IMAGE_REVIEWER_AGENT,
           "--add-dir", str(WORKSPACE_DIR), "--dangerously-skip-permissions",
           "--", prompt]
    try:
        proc = subprocess.run(cmd, cwd=str(WORKSPACE_DIR), capture_output=True,
                              text=True, timeout=IMAGE_REVIEW_TIMEOUT)
    except subprocess.TimeoutExpired:
        return "UNAVAILABLE", verdict_rel, f"timed out after {IMAGE_REVIEW_TIMEOUT}s"
    except OSError as e:
        # CLI binary missing / unspawnable (e.g. a stale CLAUDE_CLI_PATH). Degrade
        # to UNAVAILABLE (an infra condition) rather than crashing the orchestrator.
        return "UNAVAILABLE", verdict_rel, f"could not run reviewer (command not found): {e}"
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
    except OSError as e:
        return False, f"prompt-fixer could not run (command not found): {e}"
    if proc.returncode != 0:
        return False, f"prompt-fixer exited {proc.returncode}: {(proc.stderr or '')[-300:]}"
    return True, (proc.stdout or "").strip()[-200:]


def run_prompt_review_loop(video: int, manifest_rel: str) -> tuple[str, str, str]:
    """The PROMPTS gate as a CLOSED feedback loop (review → fix → re-review),
    mirroring scriptwriter↔script-reviewer. BINARY policy (Steve, 2026-06-20):
    a verdict is either a clean SHIP — the ONLY thing that advances — or it is
    not, and ANYTHING that is not a clean SHIP (HOLD-SPEND, REVISE, the retired
    SHIP WITH FIXES, or any unexpected token) drives a fix and a re-review. There
    is no "advance while imperfect": if the reviewer is not 100% signing off, the
    batch does not move to the billed spend. Bounded by IMAGE_REVIEW_MAX_FIX_
    ATTEMPTS so a manifest the fixer can't clear parks for a human instead of
    looping forever. UNAVAILABLE ends the loop immediately — the reviewer could not
    run, which is NOT an approval. Returns the FINAL (verdict, vrel, detail). Fixing
    prompts is $0, so this whole loop runs BEFORE the single billed spend. On
    UNAVAILABLE the caller (cmd_spend_ok) fails CLOSED: it refuses the billed spend
    and the human re-runs spend-ok once the reviewer recovers, or overrides with
    --force. (A false block is cheap; a false SHIP bills real money.)"""
    verdict, vrel, detail = run_image_review("prompts", video, manifest_rel)
    attempts = 0
    while verdict not in ("SHIP", "UNAVAILABLE") and attempts < IMAGE_REVIEW_MAX_FIX_ATTEMPTS:
        attempts += 1
        ran, fdetail = run_prompt_fixer(video, vrel, manifest_rel)
        if not ran:
            return verdict, vrel, f"{detail}; fix attempt {attempts} could not run ({fdetail})"
        verdict, vrel, detail = run_image_review("prompts", video, manifest_rel)
    if attempts:
        s = "s" if attempts != 1 else ""
        detail = f"{detail} (after {attempts} auto-fix attempt{s})"
    return verdict, vrel, detail


# === Script-review gate (stage 2) — the BINARY analogue of the PROMPTS gate ===
def _script_review_verdict_rel(video: int) -> str:
    return f"{VAULT_REL}/Scripts/_REVIEW_PREP/Video_{nn(video)}_Script_Review.md"


def run_script_review(video: int) -> tuple[str, str, str]:
    """Dispatch script-reviewer and read back its BINARY verdict.

    Returns (verdict, vrel, detail). verdict is SHIP (clean → advance), REVISE
    (block), or the sentinel UNAVAILABLE (could not run / no parseable VERDICT).
    The shared _parse_image_verdict also recognises the retired SHIP WITH FIXES
    token and ranks it as blocking, so a drifting reviewer can't sneak it past."""
    verdict_rel = _script_review_verdict_rel(video)
    agent_file = AGENTS_DIR / f"{SCRIPT_REVIEWER_AGENT}.md"
    if not agent_file.exists():
        return "UNAVAILABLE", verdict_rel, f"agent definition not found at {agent_file}"
    v = f"Video_{nn(video)}"
    timeout = RUN_TABLE["2_review"]["timeout"]
    prompt = (
        f"Review the drafted script for {v} (BRANDS/3SK_Finance/Scripts/{v}_Script.md). "
        f"7-dimension read-only critique → {verdict_rel}. Include a one-line 'VERDICT:' "
        f"line. The verdict is BINARY: SHIP (every dimension passes, zero fixes) or "
        f"REVISE (anything less — list every fix under REVISE). There is no 'ship with "
        f"fixes': if it is not all good to go, none of it goes."
    )
    cmd = [CLAUDE_CLI_PATH, "--print", "--agent", SCRIPT_REVIEWER_AGENT,
           "--add-dir", str(WORKSPACE_DIR), "--dangerously-skip-permissions",
           "--", prompt]
    try:
        proc = subprocess.run(cmd, cwd=str(WORKSPACE_DIR), capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return "UNAVAILABLE", verdict_rel, f"timed out after {timeout}s"
    except OSError as e:
        return "UNAVAILABLE", verdict_rel, f"could not run reviewer (command not found): {e}"
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
    return verdict, verdict_rel, f"script verdict {verdict}"


def run_script_fixer(video: int, verdict_rel: str) -> tuple[bool, str]:
    """Dispatch scriptwriter to FIX the script per a script-reviewer block — the
    script analogue of run_prompt_fixer. Edits the script in place; bills nothing.
    Returns (ran_ok, detail); ran_ok=False means the fixer could not run."""
    agent_file = AGENTS_DIR / f"{SCRIPT_FIXER_AGENT}.md"
    if not agent_file.exists():
        return False, f"agent definition not found at {agent_file}"
    v = f"Video_{nn(video)}"
    timeout = RUN_TABLE["1_script"]["timeout"]
    prompt = (
        f"script-reviewer flagged the {v} script as not yet shippable. Read its review at "
        f"{verdict_rel} and FIX the script BRANDS/3SK_Finance/Scripts/{v}_Script.md IN "
        f"PLACE: apply every line-level fix it lists (voice, structure, retention beats, "
        f"the email-list CTA rule, number-spine, beat alignment). PRESERVE the locked Brand "
        f"Bible voice, the Universal Intro/Outro structure, the scene-prompt format, and "
        f"every already-passing section UNCHANGED. Write the corrected script back to the "
        f"same path."
    )
    cmd = [CLAUDE_CLI_PATH, "--print", "--agent", SCRIPT_FIXER_AGENT,
           "--add-dir", str(WORKSPACE_DIR), "--dangerously-skip-permissions",
           "--", prompt]
    try:
        proc = subprocess.run(cmd, cwd=str(WORKSPACE_DIR), capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"script-fixer timed out after {timeout}s"
    except OSError as e:
        return False, f"script-fixer could not run (command not found): {e}"
    if proc.returncode != 0:
        return False, f"script-fixer exited {proc.returncode}: {(proc.stderr or '')[-300:]}"
    return True, (proc.stdout or "").strip()[-200:]


def run_script_review_loop(video: int) -> tuple[str, str, str]:
    """Stage 2 as a CLOSED feedback loop (review → fix → re-review), mirroring
    run_prompt_review_loop. BINARY policy (Steve, 2026-06-20): a clean SHIP is the
    ONLY thing that lets the script advance; ANYTHING less (REVISE, the retired
    SHIP WITH FIXES, or any unexpected token) drives a scriptwriter fix and a
    re-review. Bounded by IMAGE_REVIEW_MAX_FIX_ATTEMPTS so a script the fixer can't
    clear parks for a human instead of looping forever. UNAVAILABLE ends the loop
    immediately (the caller treats it as an INFRA failure → retry, never advance
    unreviewed). Returns the FINAL (verdict, vrel, detail)."""
    verdict, vrel, detail = run_script_review(video)
    attempts = 0
    while verdict not in ("SHIP", "UNAVAILABLE") and attempts < IMAGE_REVIEW_MAX_FIX_ATTEMPTS:
        attempts += 1
        ran, fdetail = run_script_fixer(video, vrel)
        if not ran:
            return verdict, vrel, f"{detail}; fix attempt {attempts} could not run ({fdetail})"
        verdict, vrel, detail = run_script_review(video)
    if attempts:
        s = "s" if attempts != 1 else ""
        detail = f"{detail} (after {attempts} auto-fix attempt{s})"
    return verdict, vrel, detail


def run_script_review_gate(video: int) -> tuple[bool, str]:
    """Stage-2 executor under the BINARY policy: run the review→fix→re-review loop
    and advance ONLY on a clean SHIP. UNAVAILABLE (the reviewer could not RUN —
    timeout/outage/unparseable) is NOT a pass: it returns an INFRA failure so the
    stage is left 'ready' and retried next sweep (self-heals when a transient
    reviewer/auth outage clears; parks needs-steve after MAX_INFRA if it stays
    down). This is the fix for the V04 fail-open regression — unreviewed work
    never advances; Steve can --force if he must. REVISE (or any non-SHIP
    surviving the fix budget) parks: returns False so the stage goes needs-steve.
    Returns the (ok, out) the dispatcher expects."""
    verdict, vrel, detail = run_script_review_loop(video)
    if verdict == "SHIP":
        return True, f"script-reviewer SHIP ({vrel}). {detail}"
    if verdict == "UNAVAILABLE":
        return False, (f"reviewer-unavailable: stage-2 script review could not run "
                       f"({detail}) — left ready to retry, NOT advanced (binary gate).")
    return False, (f"script-reviewer {verdict} on the script after auto-fix — refusing to "
                   f"advance. Fix per {vrel}, then re-run /pipeline {video}. ({detail})")


# === Stage-3 VO-kit review (vo-reviewer) — reviewed auto-promote, render manual ===
def vo_kit_rel(video: int) -> str:
    r"""The billed VO kit — the EXACT markdown generate_vo.py parses into scene mp3s
    (`## Scene N -> \`Video_NN_VO_Scene_MM.mp3\``). This is the truest text to gate,
    so vo-reviewer reviews THIS, not the script or the expanded prose draft."""
    return f"{VAULT_REL}/Voice_Files/Video_{nn(video)}/_VO_Session_B_Kit.md"


def _vo_review_verdict_rel(video: int) -> str:
    # vo-reviewer's OWN dir, deliberately separate from script-reviewer's _REVIEW_PREP.
    return f"{VAULT_REL}/Scripts/_VO_Review_Prep/Video_{nn(video)}_VO_Review.md"


def run_vo_review(video: int, kit_rel: str) -> tuple[str, str, str]:
    """Dispatch vo-reviewer on the billed VO kit and read back its BINARY verdict.
    Returns (verdict, vrel, detail): SHIP (clean → safe to render), REVISE (block),
    or the sentinel UNAVAILABLE (could not run / no parseable VERDICT). Reuses the
    shared _parse_image_verdict, which also ranks the retired SHIP WITH FIXES token
    as blocking so a drifting reviewer can't sneak it past. $0 — read-only review."""
    verdict_rel = _vo_review_verdict_rel(video)
    agent_file = AGENTS_DIR / f"{VO_REVIEWER_AGENT}.md"
    if not agent_file.exists():
        return "UNAVAILABLE", verdict_rel, f"agent definition not found at {agent_file}"
    v = f"Video_{nn(video)}"
    prompt = (
        f"Run vo-reviewer for {v} (3SK Finance). video_number: {video}. "
        f"script: {kit_rel} — review THIS exact VO kit (the billed ElevenLabs text "
        f"generate_vo.py renders), not the script or expanded draft. WRITE your verdict "
        f"to {verdict_rel} including a one-line 'VERDICT:' line. The verdict is BINARY: "
        f"SHIP (every dimension passes, zero fixes) or REVISE (anything less — list every "
        f"offending substring → TTS-safe rewrite under REVISE). There is no 'ship with "
        f"fixes': if it is not all good to go, none of it goes to the billed render."
    )
    cmd = [CLAUDE_CLI_PATH, "--print", "--agent", VO_REVIEWER_AGENT,
           "--add-dir", str(WORKSPACE_DIR), "--dangerously-skip-permissions",
           "--", prompt]
    try:
        proc = subprocess.run(cmd, cwd=str(WORKSPACE_DIR), capture_output=True,
                              text=True, timeout=VO_REVIEW_TIMEOUT)
    except subprocess.TimeoutExpired:
        return "UNAVAILABLE", verdict_rel, f"timed out after {VO_REVIEW_TIMEOUT}s"
    except OSError as e:
        return "UNAVAILABLE", verdict_rel, f"could not run reviewer (command not found): {e}"
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
    return verdict, verdict_rel, f"VO kit verdict {verdict}"


def _maybe_promote_vo_expand(s: dict, video: int, threshold: float | None,
                             deps_incomplete: bool = False) -> str:
    """Reviewed auto-promote for the manual 3_vo_expand gate. Promote to done ONLY
    when the billed VO kit exists, is fresh vs deps, AND vo-reviewer returns a clean
    SHIP on it; a REVISE keeps it parked with the fix-list path. The kit mtime is
    cached in s['vo_review'] so vo-reviewer is dispatched at most ONCE per kit
    revision (a parked REVISE does NOT re-dispatch or re-notify every hourly sweep —
    only a fresh kit edit, which bumps the mtime, triggers a re-review). The render
    itself stays MANUAL (Cowork) — a SHIP is the go-signal, never an auto-spend.
    UNAVAILABLE does NOT fail open here (unlike the free script path): a money-
    adjacent gate must not green-light an unreviewed kit because the reviewer was
    down — it parks for the human, and the render is manual anyway.

    deps_incomplete=True means an upstream dep has no completed_at — the kit can't be
    legitimately fresh vs an unfinished dep, so we park (fail CLOSED) without even
    dispatching the (billed-adjacent) reviewer.

    Returns one of:
      'promoted' — stage flipped to done (caller counts it; triggers a save)
      'changed'  — state mutated and must be persisted (cache write / note change)
      'parked'   — no persistable change this sweep
    """
    kit_rel = vo_kit_rel(video)
    kit_abs = vault_abs(kit_rel)
    if kit_abs is None or not kit_abs.exists() or kit_abs.stat().st_size <= 0:
        note = "still needs Steve/Cowork: VO kit not produced yet (_VO_Session_B_Kit.md)"
        changed = s.get("note") != note
        s["note"] = note
        return "changed" if changed else "parked"
    kit_mtime = kit_abs.stat().st_mtime
    if deps_incomplete:
        note = "still needs Steve/Cowork: upstream dep not yet completed (null completed_at) — won't auto-promote VO"
        changed = s.get("note") != note
        s["note"] = note
        return "changed" if changed else "parked"
    if threshold is not None and not (kit_mtime > threshold):
        note = "still needs Steve/Cowork: VO kit older than its deps (stale) — re-expand"
        changed = s.get("note") != note
        s["note"] = note
        return "changed" if changed else "parked"
    cache = s.get("vo_review") or {}
    detail = ""
    if cache.get("kit_mtime") == kit_mtime:
        # A DECIDED verdict (any parseable token — SHIP, REVISE, or the retired
        # HOLD-SPEND / SHIP WITH FIXES block tokens) is on file for THIS exact kit
        # revision — reuse it, no re-dispatch. Only the could-not-run sentinel
        # UNAVAILABLE is deliberately never cached (see below), so a reviewer outage
        # re-dispatches next sweep and recovers rather than parking the gate forever.
        verdict, vrel, dispatched = cache.get("verdict"), cache.get("verdict_rel"), False
    else:
        verdict, vrel, detail = run_vo_review(video, kit_rel)
        dispatched = True
    # NOTE on force=True: the production invocation is the hourly fleet drain
    # (`--advance-all --quiet-idle`), which sets SUPPRESS_NOTIFY to collapse per-stage
    # pings into one digest. But a parked VO gate returns 'parked_gate', which is NOT a
    # NEWS_OUTCOME, so that digest is itself suppressed — meaning a plain notify() here
    # would be swallowed while the dedup markers below still arm, permanently muting the
    # alert. These three VO lines are each already deduped to fire at most ONCE per kit
    # revision (SHIP/REVISE via `dispatched`; UNAVAILABLE via vo_unavail_mtime), so
    # forcing them through is safe — they cannot spam — and guarantees delivery.
    if verdict == "SHIP":
        if dispatched:
            s["vo_review"] = {"kit_mtime": kit_mtime, "verdict": "SHIP", "verdict_rel": vrel}
            s.pop("vo_unavail_mtime", None)
            notify(f"✅ Video {video}: VO kit SHIP (vo-reviewer, {vrel}) — TTS-clean, "
                   f"safe to render in ElevenLabs.", force=True)
        s["status"] = "done"
        s["completed_at"] = now_iso()
        s["park_reason"] = None
        s["pid"] = None
        s["pid_start_token"] = None
        s["artifact_path"] = kit_rel
        s["note"] = f"auto-promoted: VO kit reviewed SHIP by vo-reviewer ({vrel})"
        return "promoted"
    if verdict != "UNAVAILABLE":
        # Any parseable BLOCK token — REVISE, and the retired HOLD-SPEND / SHIP WITH
        # FIXES, which _parse_image_verdict still ranks as blocking. The reviewer DID
        # run and said "not clean," so cache it (no re-dispatch) and surface the actual
        # token — never the misleading "could not run" message in the UNAVAILABLE tail.
        if dispatched:
            s["vo_review"] = {"kit_mtime": kit_mtime, "verdict": verdict, "verdict_rel": vrel}
            s.pop("vo_unavail_mtime", None)
            notify(f"⚠️ Video {video}: VO kit {verdict} (vo-reviewer) — fix the TTS hazards "
                   f"per {vrel} BEFORE rendering (a billed garbled re-read otherwise).",
                   force=True)
        note = f"still needs Steve/Cowork: vo-reviewer {verdict} on the VO kit — fix per {vrel}"
        changed = dispatched or s.get("note") != note
        s["note"] = note
        return "changed" if changed else "parked"
    # UNAVAILABLE — reviewer down / unparseable. Do NOT cache it (so recovery re-
    # dispatches next sweep) and do NOT fail open (a money-adjacent manual gate must
    # never green-light an unreviewed kit). Notify at most ONCE per kit revision while
    # the reviewer is down, then retry silently so we don't spam Telegram hourly.
    first_outage = s.get("vo_unavail_mtime") != kit_mtime
    if first_outage:
        notify(f"⚠️ Video {video}: vo-reviewer could not review the VO kit ({detail}) "
               f"— review it by hand before rendering (auto-retries when it recovers).",
               force=True)
    s["vo_unavail_mtime"] = kit_mtime
    note = ("still needs Steve/Cowork: vo-reviewer could not run on the VO kit "
            "— review it by hand before rendering")
    changed = first_outage or s.get("note") != note
    s["note"] = note
    return "changed" if changed else "parked"


# === Generic producer-stage review gate (every-step QC, Steve 2026-06-20) ====
# The PRODUCE-THEN-REVIEW analogue of the stage-2 script gate, generalised across
# every producer stage that emits a publishable artifact but previously had NO
# review gate. Each stage names a dedicated specialist REVIEWER (binary SHIP /
# REVISE) and reuses its own PRODUCER as the FIXER (edits in place; $0). The flow
# per stage: produce → review → (fix → re-review)* → advance ONLY on a clean SHIP.
# UNAVAILABLE fails OPEN (free content path must not wedge on review tooling being
# down). Everything reuses the proven image/script-gate machinery: _parse_image_
# verdict, the IMAGE_REVIEW_MAX_FIX_ATTEMPTS budget, TimeoutExpired+OSError
# graceful degradation. The reviewer defs live in ~/.claude/agents/.
STAGE_REVIEW: dict[str, dict] = {
    "7_packaging": {
        "reviewer": "packaging-reviewer",
        "fixer": "packaging-strategist",
        "verdict_tmpl": f"{VAULT_REL}/Packaging/_REVIEW/Video_NN_Packaging_Review.md",
        "review_prompt": (
            "Review the packaging for {v} (BRANDS/3SK_Finance/Packaging/) — the title "
            "variants, cold-open hooks, thumbnail text overlays, and CTR rationale — "
            "against the Discoverability_Playbook and the first-generation-wealth moat. "
            "WRITE your verdict to {verdict_rel} including a one-line 'VERDICT:' line. The "
            "verdict is BINARY: SHIP (every element is 100% good to go) or REVISE (anything "
            "less — list every fix under REVISE). There is no 'ship with fixes': if it is "
            "not all good to go, none of it goes."
        ),
        "fix_prompt": (
            "packaging-reviewer flagged the {v} packaging as not yet shippable. Read its "
            "review at {verdict_rel} and FIX the packaging in BRANDS/3SK_Finance/Packaging/ "
            "IN PLACE: apply every fix it lists (title quality, hook strength, thumbnail "
            "text, CTR rationale, moat alignment). PRESERVE the Discoverability_Playbook "
            "format and every already-passing element UNCHANGED. Write the corrected "
            "packaging back to the same path(s)."
        ),
    },
    "9_description": {
        "reviewer": "description-reviewer",
        "fixer": "video-description-writer",
        "verdict_tmpl": f"{VAULT_REL}/Video_Descriptions/_REVIEW/Video_NN_Description_Review.md",
        "review_prompt": (
            "Review the YouTube upload pack for {v} (BRANDS/3SK_Finance/Video_Descriptions/"
            "{v}_Description.md) — description copy, chapter timestamps, FTC affiliate "
            "disclosure, hashtags, pinned comment — against the Brand Bible voice and FTC "
            "compliance. WRITE your verdict to {verdict_rel} including a one-line 'VERDICT:' "
            "line. The verdict is BINARY: SHIP (every element is 100% good to go) or REVISE "
            "(anything less — list every fix under REVISE). There is no 'ship with fixes': "
            "if it is not all good to go, none of it goes."
        ),
        "fix_prompt": (
            "description-reviewer flagged the {v} upload pack as not yet shippable. Read its "
            "review at {verdict_rel} and FIX BRANDS/3SK_Finance/Video_Descriptions/"
            "{v}_Description.md IN PLACE: apply every fix it lists (disclosure compliance, "
            "timestamp accuracy, Brand Bible voice, hashtags, pinned comment). PRESERVE the "
            "upload-pack structure and every already-passing section UNCHANGED. Write the "
            "corrected pack back to the same path."
        ),
    },
    "11_analyze": {
        "reviewer": "analyze-reviewer",
        "fixer": "channel-analyst",
        "verdict_tmpl": f"{VAULT_REL}/Channel_Intelligence/Analytics/_REVIEW/Video_NN_Analysis_Review.md",
        "review_prompt": (
            "Review the performance analysis for {v} (BRANDS/3SK_Finance/Channel_Intelligence/"
            "Analytics/) — verify EVERY claim is backed by an actual metric in the analytics "
            "export (no fabricated or hallucinated numbers) and that the routed fixes for "
            "scriptwriter + packaging are specific and actionable. WRITE your verdict to "
            "{verdict_rel} including a one-line 'VERDICT:' line. The verdict is BINARY: SHIP "
            "(every claim is metric-backed and every routed fix is actionable) or REVISE "
            "(anything less — list every fix under REVISE). There is no 'ship with fixes': "
            "if it is not all good to go, none of it goes."
        ),
        "fix_prompt": (
            "analyze-reviewer flagged the {v} performance analysis as not yet shippable. Read "
            "its review at {verdict_rel} and FIX the analysis in BRANDS/3SK_Finance/"
            "Channel_Intelligence/Analytics/ IN PLACE: correct or remove any claim not backed "
            "by a real metric, drop fabrication, and sharpen the routed fixes for scriptwriter "
            "+ packaging into specific actionable items. PRESERVE every metric-backed finding "
            "UNCHANGED. Write the corrected analysis back to the same path."
        ),
    },
}


def _stage_review_verdict_rel(key: str, video: int) -> str:
    return STAGE_REVIEW[key]["verdict_tmpl"].replace("NN", nn(video))


def run_stage_review(key: str, video: int) -> tuple[str, str, str]:
    """Dispatch a producer stage's dedicated reviewer and read back its BINARY
    verdict. Returns (verdict, vrel, detail): SHIP (clean → advance), REVISE
    (block), or UNAVAILABLE (could not run / no parseable VERDICT). Reuses the
    shared _parse_image_verdict, which also ranks the retired SHIP WITH FIXES as
    blocking so a drifting reviewer can't sneak it past."""
    cfg = STAGE_REVIEW[key]
    reviewer = cfg["reviewer"]
    verdict_rel = _stage_review_verdict_rel(key, video)
    agent_file = AGENTS_DIR / f"{reviewer}.md"
    if not agent_file.exists():
        return "UNAVAILABLE", verdict_rel, f"agent definition not found at {agent_file}"
    v = f"Video_{nn(video)}"
    timeout = RUN_TABLE[key]["timeout"]
    prompt = cfg["review_prompt"].format(v=v, verdict_rel=verdict_rel)
    cmd = [CLAUDE_CLI_PATH, "--print", "--agent", reviewer,
           "--add-dir", str(WORKSPACE_DIR), "--dangerously-skip-permissions",
           "--", prompt]
    try:
        proc = subprocess.run(cmd, cwd=str(WORKSPACE_DIR), capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return "UNAVAILABLE", verdict_rel, f"timed out after {timeout}s"
    except OSError as e:
        return "UNAVAILABLE", verdict_rel, f"could not run reviewer (command not found): {e}"
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
    return verdict, verdict_rel, f"{key} verdict {verdict}"


def run_stage_fixer(key: str, video: int, verdict_rel: str) -> tuple[bool, str]:
    """Dispatch a producer stage's own author to FIX its artifact per the reviewer
    block — the generic analogue of run_script_fixer / run_prompt_fixer. Edits in
    place; bills nothing. Returns (ran_ok, detail); ran_ok=False means the fixer
    could not run (caller stops looping)."""
    cfg = STAGE_REVIEW[key]
    fixer = cfg["fixer"]
    agent_file = AGENTS_DIR / f"{fixer}.md"
    if not agent_file.exists():
        return False, f"agent definition not found at {agent_file}"
    v = f"Video_{nn(video)}"
    timeout = RUN_TABLE[key]["timeout"]
    prompt = cfg["fix_prompt"].format(v=v, verdict_rel=verdict_rel)
    cmd = [CLAUDE_CLI_PATH, "--print", "--agent", fixer,
           "--add-dir", str(WORKSPACE_DIR), "--dangerously-skip-permissions",
           "--", prompt]
    try:
        proc = subprocess.run(cmd, cwd=str(WORKSPACE_DIR), capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"{fixer} timed out after {timeout}s"
    except OSError as e:
        return False, f"{fixer} could not run (command not found): {e}"
    if proc.returncode != 0:
        return False, f"{fixer} exited {proc.returncode}: {(proc.stderr or '')[-300:]}"
    return True, (proc.stdout or "").strip()[-200:]


def run_stage_review_loop(key: str, video: int) -> tuple[str, str, str]:
    """A producer stage's review→fix→re-review loop, mirroring run_script_review_
    loop. Advances only on a clean SHIP; anything less drives a fixer dispatch and
    a re-review, bounded by IMAGE_REVIEW_MAX_FIX_ATTEMPTS. UNAVAILABLE ends the loop
    immediately (caller treats it as an INFRA failure → retry, never advance
    unreviewed). Returns the FINAL (verdict, vrel, detail)."""
    verdict, vrel, detail = run_stage_review(key, video)
    attempts = 0
    while verdict not in ("SHIP", "UNAVAILABLE") and attempts < IMAGE_REVIEW_MAX_FIX_ATTEMPTS:
        attempts += 1
        ran, fdetail = run_stage_fixer(key, video, vrel)
        if not ran:
            return verdict, vrel, f"{detail}; fix attempt {attempts} could not run ({fdetail})"
        verdict, vrel, detail = run_stage_review(key, video)
    if attempts:
        s = "s" if attempts != 1 else ""
        detail = f"{detail} (after {attempts} auto-fix attempt{s})"
    return verdict, vrel, detail


def run_stage_review_gate(key: str, video: int) -> tuple[bool, str]:
    """PRODUCE-THEN-REVIEW executor for a producer stage under the BINARY policy.
    Step 1: dispatch the producer (the bare stage agent). If it fails to run, return
    that failure unchanged — no point reviewing a non-existent artifact. Step 2: run
    the review→fix→re-review loop and advance ONLY on a clean SHIP. UNAVAILABLE (the
    reviewer could not RUN) is NOT a pass: it returns an INFRA failure so the stage is
    left 'ready' and retried next sweep (self-heals on a transient reviewer/auth
    outage; parks after MAX_INFRA). The whole produce-then-review gate re-runs on
    retry — acceptable: these stages bill nothing. REVISE (or any non-SHIP surviving
    the fix budget) parks: returns False so the stage goes needs-steve."""
    ok, out = _dispatch_stage_agent(key, video)
    if not ok:
        return False, out
    verdict, vrel, detail = run_stage_review_loop(key, video)
    if verdict == "SHIP":
        return True, f"{STAGE_REVIEW[key]['reviewer']} SHIP ({vrel}). {detail}"
    if verdict == "UNAVAILABLE":
        return False, (f"reviewer-unavailable: {key} review ({STAGE_REVIEW[key]['reviewer']}) "
                       f"could not run ({detail}) — left ready to retry, NOT advanced.")
    return False, (f"{STAGE_REVIEW[key]['reviewer']} {verdict} on the {key} output after "
                   f"auto-fix — refusing to advance. Fix per {vrel}, then re-run "
                   f"/pipeline {video}. ({detail})")


def stage_artifact_path(key: str, video: int) -> str | None:
    v = f"Video_{nn(video)}"
    paths = {
        "1_script": f"{VAULT_REL}/Scripts/{v}_Script.md",
        "2_review": f"{VAULT_REL}/Scripts/_REVIEW_PREP",
        "6_assemble": f"{VAULT_REL}/Footage_and_Edits/{v}_v2.mp4",
        "7_packaging": f"{VAULT_REL}/Packaging",
        "8_thumbnail": f"{VAULT_REL}/Thumbnails/{v}_gen",
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


def _producer_artifact_ok(key: str, video: int) -> tuple[bool, str]:
    """A producer/script stage can exit 0 yet leave NO artifact (a half-run, a
    silent write to the wrong path, an empty file). Before we trust exit-0 and mark
    the stage done, confirm the DECLARED artifact actually exists and is non-empty.
    Returns (ok, reason-if-not). When the stage has no known artifact convention
    (path is None / unresolvable) we can't verify, so we don't block (ok=True)."""
    rel = stage_artifact_path(key, video)
    if not rel:
        return True, ""
    p = vault_abs(rel)
    if p is None:
        return True, ""
    if not p.exists():
        return False, f"declared artifact missing after exit 0: {rel}"
    if p.is_file():
        if p.stat().st_size <= 0:
            return False, f"declared artifact is empty after exit 0: {rel}"
        return True, ""
    # Directory artifact: require at least one non-zero-byte file somewhere under it.
    total = 0
    for f in p.rglob("*"):
        if f.is_file():
            try:
                total += f.stat().st_size
            except OSError:
                pass
    if total <= 0:
        return False, f"declared artifact dir empty after exit 0: {rel}"
    return True, ""


def _on_success(s: dict, key: str, video: int, out: str) -> tuple[bool, str]:
    """Mark a stage done after a clean exit — but ONLY if its declared artifact
    actually landed. Returns (done, reason): done=False means the producer exited 0
    without leaving its artifact; the caller must treat that as a task failure (it
    does NOT mutate fail_count here — the caller routes it through _on_failure once)."""
    ok_art, reason = _producer_artifact_ok(key, video)
    if not ok_art:
        return False, reason
    s["status"] = "done"
    s["completed_at"] = now_iso()
    s["artifact_path"] = stage_artifact_path(key, video)
    s["fail_count"] = 0
    s["infra_count"] = 0
    s["park_reason"] = None
    s["pid"] = None
    s["pid_start_token"] = None
    s["note"] = (out or "")[-300:] or "done"
    return True, ""


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
        # Reset the OPPOSITE counter: each counter measures a CONSECUTIVE streak of
        # its own failure type, so an intervening infra blip mustn't leave a stale
        # task-failure tally that prematurely parks a later genuine retry (and v.v.).
        s["fail_count"] = 0
        s["infra_count"] = int(s.get("infra_count") or 0) + 1
        s["note"] = ("infra failure (host/toolchain, not a task failure) #"
                     + str(s["infra_count"]) + " — " + (err or "")[-300:])
        if s["infra_count"] >= MAX_INFRA:
            s["status"] = "needs-steve"
            s["park_reason"] = "infra"
        else:
            s["status"] = "ready"
        return
    s["infra_count"] = 0
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
    promoted, vo_changed = promote_gate_exits(stages, video)
    if reset or promoted or vo_changed:
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
        done_ok, art_reason = _on_success(s, nxt, video, out)
        if done_ok:
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
        # Exit 0 but the artifact didn't land — demote to a task failure and fall
        # through to the failure handler (which calls _on_failure exactly once).
        ok = False
        out = f"stage exited 0 but {art_reason}"

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


# Deterministic pre-spend guard (added 2026-06-21). The recurring, $-wasting
# failure mode is a `use_references: false` card whose billed prompt OMITS the
# verbatim no-people suppressor, so gpt-image-2 hallucinates a generic off-model
# figure onto a data card (14 V3 cards re-spent 2026-06-19; the soft phrasing
# slipped past again on V4's first manifest). On_Model_Verification_Protocol §2
# mandates this exact string. The LLM PROMPTS gate catches it, but is
# LLM-dependent; this check makes the failure impossible to BILL — it is
# deterministic, runs FIRST in the non-force spend path, and fails CLOSED. The
# fix is always a single global manifest edit.
NO_PEOPLE_SUPPRESSOR = ("ABSOLUTELY NO PEOPLE, NO CHARACTERS, NO FIGURES, "
                        "NO CHIBI, NO CARTOON HUMANS OF ANY KIND.")


def manifest_suppressor_offenders(manifest_abs: Path) -> list[str]:
    """Names of every `use_references: false` image whose billed prompt is MISSING
    the verbatim no-people suppressor. Empty list == clean. Raises on
    unreadable/malformed JSON so the caller decides (we treat read errors as
    fail-open, mirroring the other best-effort pre-spend guards). A Three shot
    (use_references defaults True) legitimately depicts a figure and is skipped.

    Shape-defensive: a well-formed-JSON-but-wrong-shape manifest (top-level not a
    dict, or non-dict img entries) yields an empty/partial offender list rather
    than an AttributeError, so the caller's documented fail-open path triggers
    instead of crashing the spend command. The exact-substring match is
    intentional and case-sensitive — it mirrors the verbatim canon string in
    On_Model_Verification_Protocol §2; do NOT loosen it to a fuzzy match."""
    data = json.loads(manifest_abs.read_text())
    images = data.get("images", []) if isinstance(data, dict) else []
    # The .get default only covers an ABSENT key; a present-but-non-iterable
    # value ("images": null / int / bool) would still blow up the `for` below.
    if not isinstance(images, list):
        images = []
    offenders: list[str] = []
    for img in images:
        if not isinstance(img, dict):
            continue
        if img.get("use_references", True):
            continue
        # A non-string prompt (scalar/None/list) can't contain the verbatim
        # suppressor and would make `<str> not in <scalar>` raise TypeError, so
        # coerce to "" — that correctly flags the card as an offender (fail
        # CLOSED on a malformed real card) rather than crashing the spend path.
        prompt = img.get("prompt")
        if not isinstance(prompt, str):
            prompt = ""
        if NO_PEOPLE_SUPPRESSOR not in prompt:
            # Coerce a non-string/empty name to "<unnamed>" — a truthy non-string
            # name (e.g. 0-typo'd to an int) would slip past `or` and put a
            # non-str in offenders, crashing the call-site `", ".join(...)`.
            name = img.get("name")
            offenders.append(name if isinstance(name, str) and name else "<unnamed>")
    return offenders


# Deterministic thumbnail-presence guard (added 2026-06-22). The thumbnail ART
# now renders INSIDE the stage-5 billed batch (scene-image-prompt-generator must
# append `Video_NN_Thumbnail_A`/`_B` entries to video_NN_hd.json). When those
# entries are ABSENT, the batch bills the scene shots but produces NO thumbnail
# backplate — stage 8 then has nothing to burn and the video ships with a bland
# late-patched (or missing) thumbnail. This recurred: V2/V3 hd manifests carried
# ZERO thumbnail entries, V4 carried only A (the B A/B-alternate was dropped). The
# LLM PROMPTS gate is supposed to notice, but it's LLM-dependent; this check makes
# a thumbnail-less billed batch impossible to BILL — deterministic, $0, runs in
# the non-force spend path, fails CLOSED on zero entries (the catastrophic case)
# and WARNS on an incomplete A/B pair. Fix is always a single manifest edit
# (append the missing Thumbnail entry per the scene-image-prompt-generator def).
def manifest_thumbnail_entries(manifest_abs: Path, video: int) -> list[str]:
    """Sorted names of every image entry that is a thumbnail backplate for this
    video (name starts with `Video_NN_Thumbnail`). Empty list == the manifest
    carries no thumbnail art at all (the V2/V3 bug). Raises on unreadable/malformed
    JSON so the caller decides (treated as fail-open, mirroring the suppressor
    guard). Shape-defensive against a wrong-shaped manifest exactly like
    manifest_suppressor_offenders."""
    data = json.loads(manifest_abs.read_text())
    images = data.get("images", []) if isinstance(data, dict) else []
    if not isinstance(images, list):
        images = []
    prefix = f"Video_{nn(video)}_Thumbnail"
    names: list[str] = []
    for img in images:
        if not isinstance(img, dict):
            continue
        name = img.get("name")
        if isinstance(name, str) and name.startswith(prefix):
            names.append(name)
    return sorted(names)


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

    # Thumbnail A/B name-drift guard (best-effort, $0). The stage-8 overlay burn
    # intersects rendered backplates with the overlay spec's card keys; if the two
    # agents authored mismatched names, stage 8 would park AFTER this billed spend.
    # When the overlay spec already exists at spend time, surface any spec card that
    # has no matching manifest image entry NOW (a warning, not a block — the bulk
    # value here is the scene shots; we don't refuse the whole batch over a
    # secondary thumbnail name, but we make the gap loud so it's fixed before stage 8).
    _warn_thumbnail_name_drift(video, manifest_abs)

    # Pass A — pre-spend PROMPTS gate, run as a CLOSED review→fix→re-review loop
    # (image-reviewer flags → scene-image-prompt-generator fixes the manifest →
    # re-review, bounded). All of this is $0 and happens BEFORE the single billed
    # spend. A HOLD-SPEND that survives the fix budget refuses the batch; --force
    # (CLI-only) skips the gate; fail-OPEN on reviewer infra (a human authorized
    # this spend — don't block the billed path on review tooling failing to run).
    if force:
        print(f"⏭️  Video {video}: --force — skipping pre-spend image-review gate.")
        # The deterministic suppressor guard is cheap ($0) and catches the exact
        # V3 re-spend bug. Even under --force we still RUN it — but downgrade from
        # a block to a loud warning, so a Steve override never silently bills an
        # off-model batch he forgot to clean. The LLM gate is the thing --force
        # skips; this guard just shouts.
        try:
            offenders = manifest_suppressor_offenders(manifest_abs)
        except (json.JSONDecodeError, OSError):
            offenders = []
        if offenders:
            preview = ", ".join(offenders[:8]) + (" …" if len(offenders) > 8 else "")
            line = (f"⚠️ Video {video}: --force spend with {len(offenders)} no-character "
                    f"card(s) MISSING the verbatim no-people suppressor ({preview}). "
                    f"Proceeding because you overrode, but these will likely hallucinate "
                    f"off-model figures (the V3 re-spend bug). Drop --force to block.")
            print(line)
            notify(line)
        # Thumbnail-presence: under --force we never block, but shout if the batch
        # would bill with no thumbnail backplate (the V2/V3 no-thumbnail bug).
        try:
            thumbs = manifest_thumbnail_entries(manifest_abs, video)
        except (json.JSONDecodeError, OSError):
            thumbs = ["<unreadable>"]
        if not thumbs:
            line = (f"⚠️ Video {video}: --force spend with NO thumbnail entries in "
                    f"{manifest_rel} — this batch will render no thumbnail backplate and "
                    f"stage 8 will have nothing to burn (the V2/V3 bland-thumbnail bug). "
                    f"Append Video_{nn(video)}_Thumbnail_A/_B per the "
                    f"scene-image-prompt-generator def. Drop --force to block.")
            print(line)
            notify(line)
    else:
        # Deterministic suppressor guard — runs BEFORE the LLM gate, fails CLOSED.
        # Cheap, no model call, catches the exact V3 re-spend bug instantly. A
        # read/parse error fails OPEN (the LLM gate + downstream schema checks
        # still run); only a genuinely-missing suppressor blocks.
        try:
            offenders = manifest_suppressor_offenders(manifest_abs)
        except (json.JSONDecodeError, OSError):
            offenders = []
        if offenders:
            preview = ", ".join(offenders[:8]) + (" …" if len(offenders) > 8 else "")
            s["note"] = (f"spend refused: {len(offenders)} no-character card(s) missing the "
                         f"verbatim no-people suppressor ({preview})")
            sf.save()
            line = (f"🛑 Video {video}: {len(offenders)} no-character card(s) in {manifest_rel} "
                    f"are MISSING the verbatim no-people suppressor — refusing to spend "
                    f"(deterministic guard; this is the V3 re-spend bug). Offenders: {preview}. "
                    f"Fix: every use_references:false prompt must contain "
                    f"\"{NO_PEOPLE_SUPPRESSOR}\" — then re-run spend-ok.")
            print(line)
            notify(line)
            return 2
        # Deterministic thumbnail-presence guard — runs BEFORE the LLM gate, fails
        # CLOSED on ZERO thumbnail entries (the catastrophic V2/V3 no-thumbnail
        # bug: a billed batch with no backplate for stage 8 to burn). A read/parse
        # error fails OPEN (the LLM gate + downstream checks still run). An
        # incomplete A/B pair (e.g. V4 had only _A) is a loud WARNING, not a block —
        # one backplate still yields a usable thumbnail, so we don't refuse the
        # whole scene batch, but we make the gap impossible to miss.
        try:
            thumbs = manifest_thumbnail_entries(manifest_abs, video)
        except (json.JSONDecodeError, OSError):
            thumbs = None  # fail OPEN: let the LLM gate / schema checks handle it
        if thumbs is not None:
            if not thumbs:
                s["note"] = (f"spend refused: {manifest_rel} has no Video_{nn(video)}_Thumbnail "
                             f"entries — billed batch would produce no thumbnail backplate")
                sf.save()
                line = (f"🛑 Video {video}: {manifest_rel} has NO thumbnail entries — refusing "
                        f"to spend (deterministic guard; the V2/V3 bland-thumbnail bug). The "
                        f"thumbnail ART must render in this billed batch. Fix: append "
                        f"Video_{nn(video)}_Thumbnail_A and _B image entries per the "
                        f"scene-image-prompt-generator def, then re-run spend-ok.")
                print(line)
                notify(line)
                return 2
            if len(thumbs) < 2:
                line = (f"⚠️ Video {video}: only {thumbs} in {manifest_rel} — the canonical "
                        f"set is BOTH Video_{nn(video)}_Thumbnail_A and _B (A/B test). "
                        f"Proceeding (one backplate is usable), but append the missing "
                        f"variant before stage 8 for the A/B pair.")
                print(line)
                notify(line)
        verdict, vrel, detail = run_prompt_review_loop(video, manifest_rel)
        # BINARY allow-list (Steve, 2026-06-20; fail-closed hardened 2026-06-22):
        # spend ONLY on a clean SHIP — a 100% sign-off. EVERYTHING else fails
        # CLOSED and refuses the batch, INCLUDING UNAVAILABLE (the reviewer could
        # not RUN — timeout/outage/unparseable). A review that did not run is not
        # an approval: spending past it would bill real money on an unreviewed
        # (possibly off-model) batch exactly when the safety check is blind. The
        # human's spend-ok is honored by RETRYING once the reviewer recovers, or by
        # an explicit --force override (which takes the deterministic-guard-only
        # branch above). A false block is cheap; a false SHIP bills money.
        if verdict == "UNAVAILABLE":
            s["note"] = f"spend held: image-reviewer could not run the pre-spend review ({detail})"
            sf.save()
            line = (f"🛑 Video {video}: image-reviewer could not run the pre-spend prompts "
                    f"review ({detail}) — refusing to spend (binary gate, fail-CLOSED). "
                    f"Retry /pipeline {video} spend-ok when it recovers, or override with --force.")
            print(line)
            notify(line)
            return 2
        elif verdict == "SHIP":
            line = f"✅ Video {video}: image-reviewer pre-spend verdict SHIP ({vrel})."
            print(line)
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

    done_ok, art_reason = (False, "") if not ok else _on_success(s, key, video, out)
    if ok and done_ok:
        sf.save()
        nxt = select_next(stages)
        tail = f" → next: stage {nxt} ({STAGE_LABEL[nxt]})." if nxt else ""
        line = f"💸 Video {video}: stage 5 (images) GENERATED.{tail}"
        print(line)
        notify(line)
        return 0
    else:
        if ok and not done_ok:
            # generate_images exited 0 but the HD render dir is empty — don't
            # silently mark a billed stage done with no output.
            out = f"generate_images exited 0 but {art_reason}"
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
