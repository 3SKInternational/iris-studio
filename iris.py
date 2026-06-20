"""Iris Studio v0.7 (2026-05-28) — adds the Phase 5 (P5-2) multi-agent dispatcher.

What's new in v0.7 (P5-2 — multi-agent dispatch):
  - In-process SDK MCP tool `dispatch_subagent` exposed to the cloud tier
    (interactive chat turns only — NOT briefing/regen calls). Lets Iris spawn
    specialist subagents (market-researcher, scriptwriter, thumbnail-coordinator,
    sponsor-outreach-drafter) as background asyncio tasks and deliver their
    output back to Steve via Telegram when they finish. Implements the locked
    Interface Contract in 06_CEO/Designs/2026-05-26_Phase_5_Multi_Agent_Architecture.md.
  - Hardcoded allowed-agent enum = security envelope (model cannot spawn
    arbitrary `claude --agent <anything>`). Each subagent's own tools: frontmatter
    (Read/Write/Grep, no Bash) is the real capability restriction.
  - New SQLite tables: `dispatches` (state machine: pending/running/completed/
    failed/timed_out) + `pending_notifications` (deliver-on-reconnect queue).
  - Per-agent timeouts, 3-concurrent semaphore, boot-time orphan reconciliation,
    deliverable-by-mtime detection with stdout fallback.
  - Debug `/agent <name> <prompt>` + `/dispatches` Telegram commands. The `echo`
    pseudo-agent short-circuits the subprocess for a deterministic plumbing test.
  - Everything from v0.4-v0.6 (Tier 1/2 local, MCP stack, token reduction,
    morning briefing, router v2) unchanged.

Deploy: see 06_CEO/Designs/2026-05-28_Phase_5_P5-2_Dispatcher_Deploy.md
        (HISTORICAL — the one-time dispatcher cutover already happened on 2026-05-28.
        This file IS the live daemon now; do NOT copy any iris-py-v0.7-dispatcher.py
        over it — that legacy file is a frozen ancestor and would revert months of work.)
        launchctl kickstart -k gui/$(id -u)/com.iris.studio
        Test: Telegram "/agent echo hello world" → expect "📦 echo done ... hello world"

Original v0.4 notes:

What's new in v0.4:
  - WebSearch + WebFetch enabled on Tier 3 (cloud) calls via
    ClaudeAgentOptions(allowed_tools=["WebSearch", "WebFetch"]).
  - TELEGRAM_BEHAVIOR_PREFIX updated to instruct Iris to use these tools
    SPARINGLY — only when Steve explicitly asks for research, lookup, or
    current-data verification. Routine chat still answers from the loaded
    prompt context, no tool call (preserves fast UX for the 90% case).
  - No other changes. Tier 1 routing, SQLite memory, runtime date,
    workspace awareness, Quick Capture bridge, OAuth-aware error reply,
    daily stats, morning briefing scheduler, hybrid router v2 all
    unchanged from v0.3 (the 2026-05-25 evening deploy).

Deploy: cp this file → /Volumes/AI_Workspace/iris_studio/iris.py.bak-pre-v0.4
        cp this file → /Volumes/AI_Workspace/iris_studio/iris.py
        launchctl kickstart -k gui/$(id -u)/com.iris.studio
        Test with phone message: "look up the latest Llama 3.1 release date"
        Expect: cloud tier picks up the WebSearch tool, returns sourced answer.
        Test routine: "hi" → should still route local, no WebSearch.

Original v0.3 docstring follows:

Iris Studio — Telegram bot with Tier 1 (local Llama 3.1 8B via MLX), Tier 3 (Claude Max sub OAuth at Haiku 4.5), conversation memory (SQLite), and runtime date injection.

Architecture per the canonical Iris Remote Assistant Plan (06_CEO/):

  Tier 1 (LOCAL FAST):  Llama 3.1 8B Instruct 4-bit via MLX on the Mac Mini.
                        ~24 tok/sec, free. Handles short non-3SK-specific
                        questions per the router conservative v1 rules.
  Tier 3 (CLOUD via Max sub OAuth): Claude Haiku 4.5. ~5 sec response.
                        Handles everything else — 3SK-specific knowledge,
                        long prompts, complex tasks.
  Tier 4 (paid API):    Not yet wired. API key in .env preserved but
                        stripped from env at startup so it cannot leak.

Conversation memory:
  Last N messages persisted to SQLite at ~/iris_studio/iris.db (local disk
  per the canonical plan; doesn't need to be on X9). Local tier gets last
  10 messages as multi-turn context; cloud tier gets last 20 messages
  formatted into the system prompt. Memory survives daemon restarts.
  Clear history: sqlite3 ~/iris_studio/iris.db "DELETE FROM messages;"

Runtime date injection:
  Today date + day-name are injected into both system prompts at every
  message, so Iris never reports a stale date from hardcoded text.

Auth (cloud path):
  Routes through Steve Max subscription via the claude CLI OAuth.
  ANTHROPIC_API_KEY is stripped from the process env before SDK import.

Allowlist enforcement:
  Only IRIS_TELEGRAM_USER_IDS gets responses; fail-closed default if
  the env var is empty.

System prompt assembly:
  - Cloud: TELEGRAM_BEHAVIOR_PREFIX + runtime date + Operator Blueprint
           + STEVE_CONTEXT + last-20-messages-history. Loaded fresh
           every message.
  - Local: TIER1_SYSTEM_PROMPT + runtime date, with last 10 messages
           passed as multi-turn messages array to the chat template.

Force routing:
  Send a message prefixed with /cloud or /local (or !cloud / !local)
  to override the router for that message. Filter passes both slash and
  bang prefixes through.

Timeouts:
  - Cloud: 60 sec via asyncio.wait_for
  - Local: 30 sec via asyncio.wait_for around mlx_generate in a thread

Lazy model load:
  Local model loads on first use (not at daemon startup) so launchd
  kickstart stays fast. First message after restart pays a ~5 sec
  one-time load cost.
"""
import asyncio
import contextlib
import contextvars
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from dotenv import load_dotenv
from telegram import Update
from telegram.error import NetworkError, RetryAfter, TimedOut
from telegram.ext import Application, ContextTypes, MessageHandler, filters

PROJECT_DIR = Path(__file__).parent
load_dotenv(PROJECT_DIR / ".env")

# A-23 — agent output linter. Loaded via importlib so the scripts/ dir doesn't
# need to be on sys.path (keeps the loader local + side-effect-free).
import importlib.util as _importlib_util  # noqa: E402
_lint_spec = _importlib_util.spec_from_file_location(
    "_agent_output_lint", PROJECT_DIR / "scripts" / "agent_output_lint.py"
)
_agent_output_lint = _importlib_util.module_from_spec(_lint_spec)
_lint_spec.loader.exec_module(_agent_output_lint)

# scriptwriter post-dispatch docx hook helper. Same local-importlib pattern so
# the scripts/ dir stays off sys.path. Best-effort: a load failure here must not
# stop the daemon, so guard it and degrade to "no auto-docx" on any error.
try:
    _docx_spec = _importlib_util.spec_from_file_location(
        "_script_to_docx", PROJECT_DIR / "scripts" / "script_to_docx.py"
    )
    _script_to_docx = _importlib_util.module_from_spec(_docx_spec)
    _docx_spec.loader.exec_module(_script_to_docx)
except Exception as _docx_load_exc:  # noqa: BLE001 - never block daemon boot
    _script_to_docx = None
    logging.getLogger("iris").warning(
        f"script_to_docx helper unavailable (auto-docx disabled): {_docx_load_exc}"
    )

# ADAPTS loop apply core. Same local-importlib pattern. Best-effort: a load
# failure must not stop the daemon, so guard it and degrade to "no /adapt".
try:
    _adapt_spec = _importlib_util.spec_from_file_location(
        "_adaptation", PROJECT_DIR / "scripts" / "adaptation.py"
    )
    _adaptation = _importlib_util.module_from_spec(_adapt_spec)
    _adapt_spec.loader.exec_module(_adaptation)
except Exception as _adapt_load_exc:  # noqa: BLE001 - never block daemon boot
    _adaptation = None
    logging.getLogger("iris").warning(
        f"adaptation apply core unavailable (/adapt disabled): {_adapt_load_exc}"
    )

# Force Agent SDK to use the claude CLI OAuth (Max sub).
ANTHROPIC_API_KEY_FALLBACK = os.environ.pop("ANTHROPIC_API_KEY", None)

# Point HF cache at X9 so the local model is portable with the drive.
os.environ.setdefault("HF_HOME", "/Volumes/AI_Workspace/models")

# Import the Agent SDK AFTER the env strip.
from claude_agent_sdk import (  # noqa: E402
    query,
    ClaudeAgentOptions,
    create_sdk_mcp_server,
    tool,
)

# MLX import is allowed to fail; in that case Tier 1 is disabled.
MLX_AVAILABLE: bool = False
try:
    from mlx_lm import load as mlx_load, generate as mlx_generate  # noqa: E402
    MLX_AVAILABLE = True
except ImportError as _mlx_import_exc:
    _MLX_IMPORT_ERROR = repr(_mlx_import_exc)

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

ALLOWED_USER_IDS: set[int] = {
    int(uid.strip())
    for uid in os.environ.get("IRIS_TELEGRAM_USER_IDS", "").split(",")
    if uid.strip().isdigit()
}

WORKSPACE_DIR = Path("/Users/steve/Documents/3SK/outputs")
CONTEXT_FILE = PROJECT_DIR / "STEVE_CONTEXT.md"
BLUEPRINT_FILE = WORKSPACE_DIR / "_MAP" / "Iris_Operator_Blueprint.md"

# Task #6 — workspace awareness (READ side): the daemon loads these files
# fresh on every cloud-tier call, so Iris always knows current state.
# Local tier keeps the trimmed TIER1 prompt — 70KB of context wastes Llama 8B.
INBOX_FILE = WORKSPACE_DIR / "INBOX.md"
DAILY_BRIEFING_FILE = WORKSPACE_DIR / "DAILY_BRIEFING.md"
# Fix (2026-06-16): briefing .bak files land in the archive, not the vault root
# (root .bak clutter was the single biggest recurring librarian violation).
DAILY_BRIEFING_BAK_DIR = WORKSPACE_DIR / "07_Archive" / "daily_briefing_baks"
DAILY_BRIEFING_BAK_RETENTION = 30
TODO_FILE = WORKSPACE_DIR / "TODO.md"
DECISIONS_DIR = WORKSPACE_DIR / "06_CEO" / "Decisions_Log"
SESSIONS_DIR = WORKSPACE_DIR / "_Iris_Memory" / "Sessions"
RECENT_DECISIONS_LIMIT = 3
RECENT_SESSIONS_LIMIT = 3

# Context expansion (2026-05-27): three additional canonical files Iris loads
# on every cloud-tier message so she's fully clued in to (a) what other agents
# did most recently — bridge file last entry, (b) the current Phase 4/5 work
# state — Build Queue, (c) the vault navigation contract — Vault_MOC.
BRIDGE_FILE = SESSIONS_DIR / "CLAUDE_CODE_HANDOFF.md"
BUILD_QUEUE_FILE = WORKSPACE_DIR / "06_CEO" / "Designs" / "2026-05-26_Phase_4_and_5_Build_Queue.md"
VAULT_MOC_FILE = WORKSPACE_DIR / "_MAP" / "Vault_MOC.md"

# Task #6 — Quick Capture bridge (WRITE side, bridge solution per
# post-transfer-additions/iris-future-enhancements.md #2b Option A):
# When Steve sends a Quick Capture-prefixed message on Telegram, the daemon
# appends it to TELEGRAM_CAPTURE.md. Cowork-Iris reads this file at next
# session start and routes each entry to its canonical location per
# Operator Blueprint Section 5, then truncates the file (preserving header).
QUICK_CAPTURE_FILE = WORKSPACE_DIR / "TELEGRAM_CAPTURE.md"
QUICK_CAPTURE_PREFIXES = (
    "RECEIPT:", "PURCHASE:", "PAYMENT:", "DECISION:", "MEETING:",
    "MILESTONE:", "STATEMENT:", "IDEA:", "QUESTION:", "FEEDBACK:",
)

# --- Second-brain quick-capture (2026-06-16) ---
# The personal Zettelkasten ("second brain") lives at the PARENT of WORKSPACE_DIR:
# /Users/steve/Documents/3SK/  (folders "00 - INBOX" … "08 - REFERENCES").
# It was built, scheduled, and never fed — because capture required opening
# Obsidian. This bridge lets Steve text `BRAIN: <thought>` (or `NOTE:`) on
# Telegram and have it land as a friction-free, timestamped capture note in the
# second brain's "00 - INBOX/", structured with the vault's capture-template
# scaffold so the nightly inbox-processor can route it. This is the keystone
# that "turns the water on" for the whole second-brain pipeline. It is handled
# as a dedicated fast-path (no model call, instant ack) and deliberately does
# NOT touch the business vault's TELEGRAM_CAPTURE.md (different vault, different
# routing owner). The prefix is matched case-insensitively, longest-first, and
# is checked BEFORE the business Quick-Capture prefixes so it can't be shadowed.
SECOND_BRAIN_DIR = Path("/Users/steve/Documents/3SK")
SECOND_BRAIN_INBOX = SECOND_BRAIN_DIR / "00 - INBOX"
SECOND_BRAIN_PREFIXES = ("BRAIN:", "NOTE:", "ZK:")

# A4 (Redesign Night 4, 2026-06-13) — Telegram receipt-photo auto-routing.
# When Steve sends a photo on Telegram with a `RECEIPT:` caption, the daemon
# (a) downloads the photo to `02_Finance/Receipts/` for the audit trail,
# (b) parses the caption for amount + vendor + date,
# (c) writes a single-row draft into `02_Finance/Expense_Tracker_Drafts/`
#     with the same format the expense-categorizer agent produces, so
# (d) the existing `/approve <run-id>` Telegram handler fills the
#     Paste-ready CSV block exactly as it does for the Sunday Gmail sweep.
# Reuses the SQLite expense_categorizer_runs + processed_msg_ids tables;
# the msg_id for a Telegram photo is `tg-photo-<telegram_message_id>`
# (won't collide with Gmail ids, which are 16-hex-ish, and stays inside
# the same dedup namespace so a re-send of the same Telegram photo is a
# no-op).
RECEIPTS_DIR = WORKSPACE_DIR / "02_Finance" / "Receipts"
EXPENSE_DRAFTS_DIR = WORKSPACE_DIR / "02_Finance" / "Expense_Tracker_Drafts"
# Caption amount regex: matches "$7.23", "$1,234.56", "$10". Captures the
# numeric portion sans dollar sign. First match wins (receipts usually have
# a single dollar amount).
_RECEIPT_AMOUNT_RE = re.compile(r"\$([0-9]+(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)")
# Optional date: M/D, M/D/YY, M/D/YYYY, or YYYY-MM-DD anywhere in the caption.
_RECEIPT_DATE_RE = re.compile(
    r"(?P<iso>\d{4}-\d{1,2}-\d{1,2})"
    r"|(?P<us>\d{1,2}/\d{1,2}(?:/\d{2,4})?)"
)

# === Telegram media send/receive (2026-06-19) ===
# Bidirectional media for the daemon. Outbound covers three trigger paths that
# all funnel through one shared send core (send_media_to_chat):
#   1. internal helper  — send_photo_to_steve / send_file_to_steve, callable by
#      any scheduled job / pipeline stage that already holds `bot`.
#   2. external-script CLI — scripts/tg_send.sh drops a JSON job into OUTBOX_DIR;
#      a 10s APScheduler interval job (drain_outbox) sends + deletes it. A local
#      FILE queue, deliberately NOT a network listener (hard rule).
#   3. LLM chat tool — the cloud model emits a `[[SEND_FILE: <path|url>]]`
#      sentinel; handle_message post-processes it through the vault-containment
#      guard (_safe_vault_path) before sending. Interactive turns only.
# Inbound: photos without a RECEIPT: caption + any document download into
# INBOX_MEDIA_DIR and log a Quick Capture line (capture-only; Cowork files it).
# Telegram bot-API ceilings: sendPhoto ≤ 10 MB, sendDocument ≤ 50 MB. Oversize
# images fall back to document; oversize documents raise (caller surfaces it).
PHOTO_MAX_BYTES = 10 * 1024 * 1024
DOC_MAX_BYTES = 50 * 1024 * 1024
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
TELEGRAM_CAPTION_MAX = 1024  # Telegram caption hard limit
# Local file queue the daemon watches (logs already live under this dir).
OUTBOX_DIR = Path("/Users/steve/iris_studio/outbox")
OUTBOX_DRAIN_INTERVAL_SECONDS = 10
# Generic inbound media (non-RECEIPT photos + documents) land here, under the
# daemon's own capture surface in _Iris_Memory — NOT under 02_Finance/Receipts/
# (that subtree is the RECEIPT-photo pipeline's, which stays untouched). The
# matching Quick Capture line lets Cowork-Iris file each item next session.
INBOX_MEDIA_DIR = WORKSPACE_DIR / "_Iris_Memory" / "Telegram_Inbound"
# Sentinel the cloud model emits to push a file to Steve mid-reply. ASCII (the
# model emits it reliably); honored only on cloud turns, only for paths
# resolving inside WORKSPACE_DIR or http(s) URLs (see _safe_vault_path).
# The capture class EXCLUDES ']' (not a lazy `.+?`), AND there is no `\s*`
# directly before the group: either of those alone still leaves a quantifier
# pair (`\s*` vs `[^\]]+`, both matching whitespace) that an unterminated
# `[[SEND_FILE:` + long whitespace run drives into O(n²) backtracking, freezing
# the event loop. With neither, the match is linear. Any leading whitespace in
# the captured path is removed by .strip() at the call site.
_SEND_FILE_RE = re.compile(r"\[\[\s*SEND_FILE\s*:([^\]]+)\]\]", re.IGNORECASE)

# === Cloud (Tier 3) config ===
ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
QUERY_TIMEOUT_SECONDS = 60.0

# === Phase 4 (W6) — MCP servers wired into Tier 3 ===
# Obsidian: structured vault access (frontmatter, tags, wikilinks) via the
#   Local REST API plugin on 127.0.0.1:27124.
# Google Workspace: Gmail + Calendar for studio@3skinternational.com via
#   OAuth refresh tokens cached at /Users/steve/iris_mcps/mcp-google-workspace.
#   GMAIL_ALLOW_SENDING is forced false here as the daemon-level hard rule
#   (the design doc 2026-05-26_Cowork_Skills_and_MCPs called for drafts only
#   month-1). Drafting is allowed; sending is not. To enable sending later,
#   flip GMAIL_ALLOW_SENDING to "true" here and redeploy.
MCP_SERVERS: dict = {
    "obsidian": {
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "obsidian-mcp-server"],
        "env": {
            "OBSIDIAN_API_KEY": os.environ.get("OBSIDIAN_API_KEY", ""),
            "OBSIDIAN_BASE_URL": "https://127.0.0.1:27124",
            "OBSIDIAN_VERIFY_SSL": "false",
        },
    },
    "google-workspace": {
        "type": "stdio",
        "command": "/Users/steve/iris_mcps/mcp-google-workspace/launch",
        "args": [],
        "env": {
            "GMAIL_ALLOW_SENDING": "false",
            "GMAIL_ALLOW_DRAFTS": "true",
        },
    },
}

# === Phase 5 (P5-2) — Multi-agent dispatcher config ===
# The dispatcher lets the cloud-tier Iris model spawn specialist subagents
# (defined as ~/.claude/agents/<name>.md) as background asyncio tasks, then
# delivers their output to Steve via Telegram when they finish. The model
# invokes them through ONE in-process MCP tool: dispatch_subagent. DISPATCH_AGENTS
# below is the SECURITY ENVELOPE — the model cannot spawn arbitrary
# `claude --agent <anything>`; only these names are accepted. Each subagent's own
# `tools:` frontmatter (Read/Write/Grep, no Bash) is the real capability limit;
# --dangerously-skip-permissions only suppresses interactive prompts for the
# unattended subprocess. Implements the locked Interface Contract in
# 06_CEO/Designs/2026-05-26_Phase_5_Multi_Agent_Architecture.md.

# Resolve the claude CLI binary. launchd jobs don't reliably have /opt/homebrew
# on PATH, so prefer an explicit env override, then the known Homebrew path,
# then a PATH lookup, then a bare "claude".
CLAUDE_CLI_PATH = (
    os.environ.get("CLAUDE_CLI_PATH")
    or ("/opt/homebrew/bin/claude" if Path("/opt/homebrew/bin/claude").exists() else None)
    or shutil.which("claude")
    or "claude"
)

# Per-agent: timeout + the vault-relative dir the agent is expected to write its
# deliverable into. Deliverable detection scans that dir for files modified after
# dispatch start; if none, it falls back to the newest file modified anywhere in
# the vault during the run, then to the subagent's stdout. Timeouts per the
# architecture doc's "Per-agent timeouts" table.
DISPATCH_AGENTS: dict[str, dict] = {
    "market-researcher": {
        "timeout_seconds": 1800,  # 30 min — multi-source web research is slow
        "deliverable_dir": "05_Research_and_Intelligence/Research_Reports",
    },
    "scriptwriter": {
        # 20 min — bumped 2026-05-30 from 10 min after a flagship dispatch hit
        # the cap. The original 10-min figure was a design-doc estimate; reality
        # for a 2,250-word structured draft + 3 reference reads runs longer.
        "timeout_seconds": 1200,
        "deliverable_dir": "BRANDS/3SK_Finance/Scripts",
    },
    # --- Added 2026-06-18: the skeptical reviewer pair for scriptwriter. ---
    # script-reviewer: read-only second pass on a drafted script before Steve
    # reads it (PASS/REVISE per dimension + line-level fixes). Pairs with
    # scriptwriter exactly as skeptical-code-reviewer pairs with senior-engineer.
    "script-reviewer": {
        "timeout_seconds": 480,  # 8 min — read-only single-pass critique, no web
        "deliverable_dir": "BRANDS/3SK_Finance/Scripts/_REVIEW_PREP",
    },
    "thumbnail-coordinator": {
        "timeout_seconds": 300,  # 5 min — structured transformation, no research
        "deliverable_dir": "BRANDS/3SK_Finance/Thumbnails",
    },
    # --- Added 2026-06-18: the CTR lever (titles + hooks + thumb-text) ---
    # packaging-strategist owns the title, the cold-open hook, and the thumbnail
    # TEXT — the levers that actually move CTR. thumbnail-coordinator owns the
    # IMAGE; this agent owns the words. Operationalizes the Discoverability_Playbook
    # against the "first-gen wealth" moat. Seed (scriptwriter's Thumbnail Concept)
    # → finished package, the same expand-not-duplicate split.
    # deliverable_dir is its OWN dir (sibling of Thumbnails, NOT nested under it):
    # packaging is meant to run "alongside" thumbnail-coordinator, and a SHARED
    # deliverable_dir lets _find_deliverable's newest-in-window scan return the
    # other agent's file as this dispatch's deliverable (mis-attributed Telegram
    # notification + status-hook read of the wrong file). A dedicated non-nested
    # dir removes the collision with zero change to the shared detection logic.
    "packaging-strategist": {
        "timeout_seconds": 480,  # 8 min — read canon + draft 8-10 titles/hooks/text
        "deliverable_dir": "BRANDS/3SK_Finance/Packaging",
    },
    "sponsor-outreach-drafter": {
        "timeout_seconds": 900,  # 15 min — light web research per sponsor + drafting
        "deliverable_dir": "04_Marketing_and_Sponsors/Outreach",
    },
    # --- Added 2026-05-30: 4 vault-native agents (the "easy adds" pass) ---
    "scene-image-prompt-generator": {
        "timeout_seconds": 600,  # 10 min — Haiku, mechanical 5-field transformation
        "deliverable_dir": "BRANDS/3SK_Finance/Scene_Image_Prompts",
    },
    "video-description-writer": {
        "timeout_seconds": 600,  # 10 min — Sonnet, single-pass YouTube upload pack
        "deliverable_dir": "BRANDS/3SK_Finance/Video_Descriptions",
    },
    "researcher": {
        "timeout_seconds": 1800,  # 30 min — general technical/factual web research
        "deliverable_dir": "05_Research_and_Intelligence/Research_Reports",
    },
    "project-manager": {
        "timeout_seconds": 600,  # 10 min — reads vault state + writes status report
        "deliverable_dir": "06_CEO/Status_Reports",
    },
    # --- Added 2026-05-31 (Session 17): YouTube channel intelligence ---
    # youtube-researcher: continuously monitors the finance-creator YouTube
    # landscape and writes 5 structured intelligence files to
    # BRANDS/3SK_Finance/Channel_Intelligence/ that the channel content agents
    # (scriptwriter, thumbnail-coordinator, video-description-writer,
    # scene-image-prompt-generator) read at every dispatch. Distinct from
    # market-researcher (business/sponsor scope) and researcher (general tech).
    "youtube-researcher": {
        "timeout_seconds": 1800,  # 30 min — multi-source web research + 5 file writes
        "deliverable_dir": "BRANDS/3SK_Finance/Channel_Intelligence",
    },
    # --- Added 2026-06-18: the inward-facing analytics counterpart ---
    # channel-analyst: reads OUR own YouTube performance (CTR/retention/AVD/
    # traffic/subs) and turns it into routable fixes for scriptwriter +
    # packaging. Inward sibling to youtube-researcher (which reads the outside
    # niche) — do not conflate. INPUT: until YouTube OAuth lands (Builds 3+4 of
    # the YouTube Autonomy Roadmap) it has no live API; the dispatch prompt must
    # carry a YouTube Studio analytics-export CSV path OR a pasted metrics block.
    # That makes it most natural to invoke INTERACTIVELY / from Cowork with the
    # export path in the prompt, not from a bare Telegram message — it's in the
    # enum for completeness. Post-OAuth follow-on swaps the manual input for a
    # live fetch; the analysis logic stays identical.
    "channel-analyst": {
        "timeout_seconds": 720,  # 12 min — read benchmarks + analyze one export + write
        "deliverable_dir": "BRANDS/3SK_Finance/Channel_Intelligence/Analytics",
    },
    # --- Added 2026-06-18: image REUSE to avoid regeneration spend ---
    # asset-librarian: reads the prebuilt asset_index.json (built deterministically
    # by scripts/build_asset_index.py — cheap, no tokens) and matches an upcoming
    # video's scenes against the ~140 already-generated PNGs, returning reuse
    # candidates so we don't pay to regenerate shots we already own. Haiku by
    # design: it reads ONE index, never re-scans the raw files. Dispatch BEFORE
    # authoring a new scene manifest. deliverable_dir is a DEDICATED subdir
    # (Reuse_Reports), NOT the shared Image_Factory root: that root holds the
    # manifests/ subtree + the index JSON, and _find_deliverable rglobs its dir,
    # so a manifest edited by Cowork/Steve during the 5-min window could be
    # mis-returned as this dispatch's deliverable. A dedicated dir removes that
    # collision (same fix the packaging-strategist entry above made).
    "asset-librarian": {
        "timeout_seconds": 300,  # 5 min — Haiku, reads one index + writes one report
        "deliverable_dir": "BRANDS/3SK_Finance/Raw_Assets/Image_Factory/Reuse_Reports",
    },
    # --- Added 2026-05-31 (Session 15, P5-12 dispatcher-side batch) ---
    # expense-categorizer: scans studio@ Gmail for receipts, drafts Expense_Tracker
    # rows with Schedule C categorization, surfaces a draft for Steve's /approve.
    # NEVER writes to Expense_Tracker.xlsx directly (draft-only contract). Paired
    # with the scheduled-sweep job (09:00 ET) + /approve handler below.
    "expense-categorizer": {
        "timeout_seconds": 600,  # 10 min — Gmail scan + categorize + draft write
        "deliverable_dir": "02_Finance/Expense_Tracker_Drafts",
    },
    # --- Added 2026-06-16: the Decision Feeder (founder's highest-leverage workflow) ---
    # Scans BOTH the business vault (cwd) AND the personal second-brain Zettelkasten
    # (granted via extra_add_dirs below — it lives at the PARENT of WORKSPACE_DIR) for
    # every note bearing on a decision, then writes a DECISION BRIEF. Fires on demand
    # via `/decision <q>` + the `decision-feeder-deadline-watch` daily auto-fire.
    "decision-feeder": {
        "timeout_seconds": 900,  # 15 min — two-vault wide grep + synthesis + brief write
        "deliverable_dir": "06_CEO/Decision_Briefs",
        # Grant read of the whole 3SK tree (parent of WORKSPACE_DIR) so the agent can
        # reach the second brain at /Users/steve/Documents/3SK/ "02 - PERMANENT/" etc.
        "extra_add_dirs": ["/Users/steve/Documents/3SK"],
    },
    # --- Added 2026-06-18: the Building Iris build-in-public content engine ---
    # build-logger: turns work that ALREADY shipped (the bridge file + git history
    # of the iris_studio repo + recent session digests) into build-in-public post
    # drafts (X thread + newsletter blurb) for the Building Iris brand. The keystone
    # of the Building Iris content line (Charter §4/§5) — proves the brand earns its
    # place as an automated byproduct of existing work, not net-new human hours.
    # DRAFTS ONLY — it never posts (publishing target is undecided pending the
    # brand-split call, Charter §7.2). Redaction is fail-closed: it scrubs
    # [AUTHOR]/[COMPANY]/[ASSISTANT]/[EMAIL]/[BOT]/[USER] to placeholders, runs the
    # brand-safe deterministic scrub (scripts/redact-buildlog.py — keeps the public
    # "Iris" brand name, scrubs infra tokens + structural paths/IPs/hosts/creds) on its
    # own draft as a MANDATORY post-write pass, and DROPS any item it can't safely
    # redact. It
    # reads git via its own Bash tool (`git -C <repo> log`),
    # which is NOT sandboxed by --add-dir, so no extra_add_dirs is needed — the
    # bridge file + session digests it Reads all live inside WORKSPACE_DIR.
    # deliverable_dir is build-logger's DEDICATED, non-nested dir (its own subtree
    # under Build_Log) — never shared with another agent's dir, which avoids the
    # newest-in-window mis-attribution class that bit packaging-strategist/
    # asset-librarian above.
    "build-logger": {
        "timeout_seconds": 600,  # 10 min — Sonnet, reads bridge+git+digests, drafts 1-3 posts
        "deliverable_dir": "BRANDS/Building_Iris/Build_Log/_drafts",
    },
    # The 4 engineering agents (senior-systems-architect, senior-engineer,
    # skeptical-code-reviewer, performance-optimizer) are deliberately NOT in this
    # enum yet — their target is a codebase (typically /Volumes/AI_Workspace/iris_studio/),
    # not the vault, and this dispatcher hardcodes cwd=WORKSPACE_DIR. Add them
    # once a per-agent working_dir override is wired (the dispatch spawn block uses
    # WORKSPACE_DIR in two places; parameterize via cfg.get("working_dir", ...)).
    # They remain fully usable via interactive Claude Code's Task tool.
}
# Agent names the MODEL is allowed to dispatch (the dispatch_subagent enum).
DISPATCH_ALLOWED_AGENTS = tuple(DISPATCH_AGENTS.keys())

# Autonomous dispatch cadences — scheduled via APScheduler in _post_init. Each
# entry fires through the same _start_dispatch path Steve's /agent and the cloud
# model's dispatch_subagent MCP tool use, so per-agent timeouts + semaphore +
# capture-on-timeout + Telegram notification all apply uniformly. The
# `autonomous_label` propagates into the Telegram message as a `🤖 Autonomous:`
# prefix so Steve can tell scheduled runs from his own / the model's dispatches.
#
# Adding a cadence: append an entry, daemon kickstart, done — no DB schema or
# new infrastructure. If you need a goal-state idempotency check (skip-on-empty),
# wire it into the entry's prompt with explicit short-circuit instructions
# ("if no new X since last run, return 'no work, skipping' and exit").
AUTONOMOUS_DISPATCHES: list[dict] = [
    {
        "name": "project-manager-weekly",
        "agent_name": "project-manager",
        # weekly Mondays 05:30 ET — lands a week-over-week status report 30 min
        # before Monday's morning brief. (Was daily 05:30 from 5/30→5/31; flipped
        # to weekly 5/31 because daily was overkill given the operation's actual
        # change rate. Mid-week pulse can be added later if the signal density warrants.)
        "trigger_kwargs": {"day_of_week": "mon", "hour": 5, "minute": 30},
        "prompt": (
            "Daily autonomous status sweep. Read the canonical operation state — "
            "the Build Queue, bridge file (latest 3 sessions), INBOX, today's daily "
            "note, plus live `launchctl list | grep -E iris\\|claude` and "
            "`git -C /Volumes/AI_Workspace/iris_studio log --since='24 hours ago'` — "
            "and write a status report to "
            "06_CEO/Status_Reports/[YYYY-MM-DD]_status.md. Cover: what shipped "
            "since the last status report (verify against disk + git, do NOT trust "
            "the queue markers blindly), what's in flight (owner + last movement), "
            "what's blocked (categorize: Steve / dependency / stalled-no-excuse), "
            "where contract and reality have drifted, and the ranked 3-5 actions "
            "only Steve can do. Be honest — distinguish confirmed from unverified. "
            "Return the headline 🧍 needs-Steve list on stdout (4-6 ranked lines)."
        ),
    },
    # NOTE: youtube-researcher is NOT dispatched here. It moved to a launchd
    # routine (com.iris.claude-code-youtube-research → routines/youtube-research.prompt)
    # on 2026-06-20, bumped to 2x/week (Mon + Thu 02:00 ET) and fronted by a
    # niche_pull.py Data-API view-count feed so its intel is backed by REAL ranked
    # view counts. Mirrors the analytics-feedback loop. Do not re-add it here or it
    # will double-fire.
    {
        "name": "market-researcher-monthly",
        "agent_name": "market-researcher",
        # monthly 1st @ 02:00 ET — ahead of the 03:00 nightly so the month's
        # content work reads fresh market intel, and so the heavy LLM jobs stop
        # colliding at 03:00 (de-staggered 6/18, Session 66).
        "trigger_kwargs": {"day": 1, "hour": 2, "minute": 0},
        "prompt": (
            "Monthly autonomous research sweep. Read "
            "05_Research_and_Intelligence/Competitor_Analysis/_rotation.md — pick "
            "the next ⚪ item from the Queue section. If the queue is empty (all "
            "✅), return 'rotation exhausted, please refill' on stdout and exit "
            "without burning a dispatch. Otherwise execute that one item, write "
            "the deliverable to the appropriate vault path (teardowns → "
            "05_Research_and_Intelligence/Competitor_Analysis/<name>_Teardown.md; "
            "sponsor/newsletter cohorts → "
            "04_Marketing_and_Sponsors/Sponsor_Prospects/[YYYY-MM-DD]_<topic>.md; "
            "trend scans → "
            "05_Research_and_Intelligence/Trend_Scans/[YYYY-MM-DD]_<topic>.md). "
            "Then edit _rotation.md: move the executed item from Queue → Done with "
            "today's date + a one-line result pointer. Return a 3-line summary on "
            "stdout: what was scanned, top finding, next rotation item."
        ),
    },
    {
        "name": "decision-feeder-deadline-watch",
        "agent_name": "decision-feeder",
        # daily 03:40 ET — between Cowork's 03:05 pulse and the 04:10 gardener, so a
        # near-deadline decision gets its evidence brief in place before the morning
        # brief. Conditional/skip-on-empty: the prompt makes the agent FIRST check
        # for 💰/📤/⚖️ rows within 2 days of deadline (without a fresh brief) and exit
        # cheaply if there are none — so most days this is a no-op, not a burn.
        "trigger_kwargs": {"hour": 3, "minute": 40},
        "prompt": (
            "Mode B — deadline-watch auto-fire. Run the skip-or-work gate FIRST: read "
            "06_CEO/Decision_Queue.md, find every OPEN row classed 💰 / 📤 / ⚖️ (never "
            "AUTO, never 🎨) whose deadline is within the next 2 days (or, if no explicit "
            "deadline, days-pending ≥ 5). For each, skip it if a brief from the last 7 "
            "days already exists in 06_CEO/Decision_Briefs/ matching its slug. If NOTHING "
            "qualifies after both filters, output exactly 'No near-deadline decisions "
            "without a brief — skipping.' and exit WITHOUT writing any file. Otherwise "
            "build a DECISION BRIEF for each qualifying row (cap 3), scanning BOTH the "
            "business vault and the personal second brain, and additively link each brief "
            "from its Decision_Queue row. Return a short stdout summary of what you briefed "
            "or the skip line."
        ),
    },
]
# Max simultaneous subagent subprocesses (asyncio semaphore). A request beyond
# this still gets a dispatch_id but waits for a free slot (reported as queued).
DISPATCH_MAX_CONCURRENT = 3
# Debug-only pseudo-agent: short-circuits the subprocess and returns the prompt
# verbatim so the dispatch -> notify round trip can be smoke-tested
# deterministically (P5-2 acceptance: `/agent echo hello world` -> "hello world").
# Only reachable via the /agent debug command, never via the model.
DISPATCH_ECHO_AGENT = "echo"

# Appended to the cloud system prompt ONLY on interactive chat turns (chat_id set),
# never on briefing/regen calls. Teaches the model when/how to dispatch.
DISPATCHER_MODE_SUFFIX = """

---

# === DISPATCHER MODE (Phase 5) ===

You can delegate substantive, slow work to specialist subagents via the
`dispatch_subagent` tool. Use it ONLY when Steve asks for real deliverable work
that takes minutes — never for quick chat answers you can give from context.

Available subagents (agent_name -> what it does):
- `market-researcher` — web research -> structured report (YouTube competitors,
  sponsor prospects, niche/trend analysis). ~30 min.
- `researcher` — general technical/factual/comparative research (libraries, tools,
  prior art, how-X-works). Distinct from market-researcher (which is business/
  niche). Pair with engineering work when facts are missing. ~30 min.
- `youtube-researcher` — YouTube channel intelligence specialist (hook/title/
  thumbnail/algorithm patterns) that feeds the channel content agents'
  freshness layer in BRANDS/3SK_Finance/Channel_Intelligence/. Runs weekly
  autonomously; dispatch ad-hoc for focused deep-dives. ~30 min.
- `channel-analyst` — reads OUR OWN channel's analytics (CTR/retention/AVD/
  traffic/subs) and turns them into routable fixes for scriptwriter + packaging.
  Inward sibling of youtube-researcher (which reads the outside niche). REQUIRES
  a YouTube Studio analytics-export CSV path OR a pasted metrics block in the
  prompt (no live API until YouTube OAuth lands) — best invoked interactively /
  from Cowork with the export, not a bare Telegram message. ~12 min.
- `scriptwriter` — drafts a full 3SK Finance video script from a topic/format. ~20 min.
- `script-reviewer` — skeptical second pass on a drafted script before Steve reads
  it (voice, banned vocab, number-spine, hook, VO density, structure/CTA, moat).
  Read-only — writes a review, never edits the script. Dispatch right after
  scriptwriter. ~8 min.
- `scene-image-prompt-generator` — turns a script's scene blocks into paste-ready
  ChatGPT 5-field SCENE PROMPTs + verbatim Master Character Prompt v3. ~10 min.
- `video-description-writer` — drafts a YouTube upload pack from a finished script
  (description, chapter timestamps, affiliate disclosure, hashtags, pinned). ~10 min.
- `thumbnail-coordinator` — turns a script into a thumbnail brief (image-gen
  prompt + title overlay spec). Owns the thumbnail IMAGE. ~5 min.
- `packaging-strategist` — the CTR lever: 8-10 title variants (tagged by lever),
  2 cold-open hooks, 3 thumbnail TEXT overlays + a per-title CTR rationale, all
  against the Discoverability_Playbook and the "first-gen wealth" moat. Owns the
  title/hook/thumb-text WORDS (thumbnail-coordinator owns the image). Dispatch
  when a script/topic is set, before or alongside thumbnail-coordinator. ~8 min.
- `sponsor-outreach-drafter` — drafts a personalized cold sponsor email. ~15 min.
- `project-manager` — honest status read on the whole operation (what shipped,
  what's blocked, what needs Steve, where contract & reality drift). ~10 min.
- `expense-categorizer` — scans studio@ Gmail for receipt-shaped emails, drafts
  Expense_Tracker rows with Schedule C categorization (draft only — Steve approves
  via `/approve <run-id>`). Runs daily 09:00 ET automatically; dispatch on demand
  for backfills with a `since` date. ~10 min.
- `decision-feeder` — the founder's Decision Feeder: scans BOTH the business vault
  AND Steve's personal second brain for every note bearing on a decision, then writes
  a DECISION BRIEF (evidence for/against/nuance + the gaps to fill). Dispatch when
  Steve wants the full picture his own notes hold before a real call ("brief me on
  the ESP decision"). He can also trigger it directly with `/decision <q>`. ~15 min.
- `build-logger` — the Building Iris build-in-public content engine: reads work
  that already shipped (the bridge file + the iris_studio git history + recent
  session digests) and drafts 1-3 build-in-public posts (an X thread + a newsletter
  blurb each), scars-included voice. DRAFTS ONLY — never posts anywhere (publishing
  target pending the brand-split call). Redaction is fail-closed. Dispatch when Steve
  wants build-log content; optional arg = a window ("3 days") or an item to spotlight.
  ~10 min.

When you dispatch:
1. Call `dispatch_subagent` with agent_name, a clear self-contained prompt, and
   your best expected_turnaround_minutes.
2. The tool returns immediately with a dispatch_id. The subagent runs in the
   background; its deliverable is sent to Steve via Telegram when it finishes.
3. In your reply, confirm what you dispatched and that you'll ping him when it
   lands (e.g. "Dispatched to market-researcher — ~20 min, I'll send the report
   here when it's ready."). Do NOT pretend you already have the result.

Do NOT dispatch for things you can answer directly. Do NOT dispatch the same work
twice. If unsure which agent fits, ask Steve a one-line clarifying question.

---

# === SENDING STEVE A FILE ===

When Steve asks you to send him a file or image that exists in the vault ("send
me the V1 thumbnail", "text me that PDF"), end your reply with a sentinel line:

    [[SEND_FILE: /Users/steve/Documents/3SK/outputs/<relative>/<file>]]

One sentinel per file; you may include several. Rules:
- The path MUST be an absolute path INSIDE the vault
  (/Users/steve/Documents/3SK/outputs/...) or an http(s) URL. Anything else is
  refused by a containment guard — never try to send system paths.
- The daemon strips the sentinel from your reply before Steve sees it and sends
  the actual file, so write a normal short sentence ("Here's the V1 thumbnail:")
  THEN the sentinel on its own line.
- Only emit it when you are confident the file exists at that path. If you're not
  sure of the exact path, say so and ask — don't guess a sentinel.
"""

# === Local (Tier 1) config ===
LOCAL_MODEL_PATH = "mlx-community/Llama-3.1-8B-Instruct-4bit"
LOCAL_MAX_TOKENS = 400
LOCAL_TIMEOUT_SECONDS = 30.0

# === Tier 2 — Local Qwen 2.5 14B (W3 from engineering handoff) ===
# Added 2026-05-27 to leverage the Mac Mini M4 + 24GB RAM more fully:
# Tier 2 fills the gap between Llama 3.1 8B (Tier 1, fast but limited) and
# Haiku 4.5 via cloud (Tier 3, smart but cloud-bound). Qwen 14B at 4-bit fits
# in ~9GB RAM and runs ~12-15 tok/sec on M4. Accessed via /tier2 or !tier2
# force prefix from Telegram. Router auto-routing to Tier 2 deferred to a
# follow-on tuning pass.
LOCAL_TIER2_MODEL_PATH = "mlx-community/Qwen2.5-14B-Instruct-4bit"
LOCAL_TIER2_MAX_TOKENS = 600
LOCAL_TIER2_TIMEOUT_SECONDS = 60.0

# === Conversation memory (Task #5) config ===
DB_PATH = Path("/Volumes/AI_Workspace/iris_studio/iris.db")
HISTORY_LIMIT_LOCAL = 10  # Llama 8B is small; tight history keeps it focused
HISTORY_LIMIT_CLOUD = 20  # Haiku 4.5 handles longer context cleanly

# === Pitch #15 — Daily Tier 4 spend cap + per-tier usage telemetry ===
# Today's effect: /usage command returns tier-split message counts.
# Future effect: when Pitch #16 full lands (auto-fallback to Tier 4), the
# cap will gate Tier 4 calls before they fire — preventing runaway burn.
# Server-side cap on the Anthropic API console is a separate manual change
# (drop $25/mo → $5/mo per the canonical plan; completed 2026-05-25).
DAILY_TIER4_CAP_USD = 2.0  # canonical plan: "$2/day ($60/mo ceiling)"

# === Phase 3 — Scheduled morning briefing ===
# 7:00 AM Eastern daily. Cron job triggers send_morning_briefing which
# generates a Haiku-via-Max-sub brief from workspace + telemetry context
# and sends via Telegram to Steve. Also exposed as /briefing slash command
# for on-demand testing without waiting for 7 AM.
TIMEZONE = ZoneInfo("America/New_York")
MORNING_BRIEFING_HOUR = 8  # 7→6 (2026-05-25); →8 AM ET 2026-06-16 per Steve (window-align: overnight CC jobs open the 5-hr usage window ~02:30, it resets ~07:30, so an 8 AM brief lands after reset → Steve gets a fresh window when he opens Claude Code)
MORNING_BRIEFING_MINUTE = 0
_scheduler: AsyncIOScheduler | None = None

# Lazy-loaded singletons.
_local_model = None
_local_tokenizer = None
_local_load_lock: asyncio.Lock | None = None

# Tier 2 Qwen 14B — separate singletons so both can be loaded simultaneously.
_local_tier2_model = None
_local_tier2_tokenizer = None
_local_tier2_load_lock: asyncio.Lock | None = None

_db_initialized = False
_db_init_lock: asyncio.Lock | None = None

# Phase 5 dispatcher runtime state.
_telegram_bot = None  # set in _post_init; used to deliver subagent results
_dispatch_semaphore: asyncio.Semaphore | None = None  # created on the loop at boot
_DISPATCHER_MCP = None  # in-process MCP server config, built after the tool is defined
# Carries the current chat_id into the in-process dispatch tool handler. Set in
# query_cloud on interactive turns; read by the tool so the deliverable goes to
# the right Telegram chat. Background dispatch tasks capture it at create_task().
_current_dispatch_chat_id: contextvars.ContextVar = contextvars.ContextVar(
    "current_dispatch_chat_id", default=None
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("iris")

# A-4: silence Telegram getUpdates polling INFO from httpx/httpcore.
# Was burying real iris errors under ~360 lines/hour of HTTP/1.1 200 OK noise
# in iris.err.log. Real errors from these libs still surface at WARNING+.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

TELEGRAM_MAX_MSG = 4000


TELEGRAM_BEHAVIOR_PREFIX = """# === TELEGRAM-CONTEXT BEHAVIOR (OVERRIDES BLUEPRINT SESSION RITUAL) ===

You are responding to Steve via Telegram on his phone. Different rules apply
than for a Cowork desk session:

- **Answer from the context loaded into this prompt by default.** The
  system prompt below is your primary information source.
- **Research tools (WebSearch + WebFetch) are AVAILABLE but use them
  sparingly.** Only invoke when Steve explicitly asks you to look something
  up, research a topic, check current data, or verify a fact you don't have
  in context. For routine chat (status questions, file lookups, opinions,
  acknowledgments), answer from the prompt — DO NOT search the web.
  Examples that warrant a WebSearch: "look up the latest YouTube finance
  RPM", "what's the current Mercury sign-up bonus". Examples that do NOT:
  "what's in INBOX", "did I lock the WY filing", "how do I feel about X".
- **Keep responses short and mobile-friendly.** 1-4 short paragraphs max.
  No headers. Minimal bullet lists. Plain prose. Code blocks only if
  strictly necessary, and short.
- **Lead with the answer.** Don't preamble. Don't open with "Sure!" or
  "Great question." Just answer.
- **If you genuinely need information not in this prompt to answer**,
  briefly say so and suggest Steve open Cowork on the Mini for the deeper
  work. Don't fabricate.
- **Stay in Iris persona.** First person, sign with "— Iris" only when
  natural.
- **Use the conversation history below to maintain continuity.** If Steve
  references something he said earlier in the day, you should already know it
  from the RECENT CONVERSATION section.
- **Quick Capture awareness:** When Steve sends a message starting with
  RECEIPT:, PURCHASE:, PAYMENT:, DECISION:, MEETING:, MILESTONE:,
  STATEMENT:, IDEA:, QUESTION:, or FEEDBACK:, the daemon AUTOMATICALLY
  appends the raw message to TELEGRAM_CAPTURE.md in the workspace.
  Cowork-Iris reads that file at the next desktop session and routes
  each entry to its canonical home per Operator Blueprint Section 5.
  Your job in that case: acknowledge the capture in your reply, summarize
  what was captured, and be honest about the mechanism — you (the
  Telegram daemon) captured it; Cowork-Iris will FILE it properly
  later. Do NOT claim to have filed it yourself.
- **Formatting hygiene:** Do NOT auto-link filenames or paths as
  markdown URLs. Filenames like `iris.py` or `INBOX.md` are NOT
  websites — never wrap them as [iris.py](https://iris.py/) or similar.
  Use backticks or plain text for filenames. Plain prose with minimal
  markdown is the Telegram standard.

The full Operator Blueprint, Technical Addendum, and recent conversation
history follow. Use them as your knowledge base.

---

"""

# Tier 1 system prompt. Date is injected at runtime — no hardcoded date.
TIER1_SYSTEM_PROMPT_TEMPLATE = """You are Iris, Steve Arias AI business operator for 3SK International (Wyoming LLC, parent) and 3SK Finance (first YouTube brand, character "Three" — 2D flat chibi with dot eyes).

You are answering a quick Telegram message from Steve on his phone. Reply rules:

- 1-3 short paragraphs max, plain prose
- No headers, no bullet lists unless essential
- Lead with the answer — no preamble like "Sure!" or "Great question"
- Stay in Iris persona, first person
- Sign with "— Iris" only when it feels natural at the end
- If you do not know an answer or need data you do not have, briefly say so and suggest Steve open Cowork on the Mini for the deeper context — do not fabricate

Operating context you should know:
- You run as a daemon on Steve Mac Mini M4 at home, available via Telegram as @iris_studio_ai_bot
- This message is being handled by the LOCAL tier (Llama 3.1 8B). The CLOUD tier (Claude Haiku 4.5 via Max subscription OAuth) handles anything 3SK-specific, complex, or long.
- {date_block}
- Steve hardware: Mac Mini M4 (server) + MacBook Air (mobile) + encrypted X9 SSD for AI workspace + Tailscale for remote access.

If the question is 3SK-business-specific (LLC filing, expenses, Mercury, EIN, Iris build, decisions, etc.) you almost certainly do NOT have enough context — tell Steve to open Cowork on the Mini for that one.

If the user message references something earlier in this conversation, the multi-turn history below has the context you need."""


def _runtime_date_block() -> str:
    """Return a one-line "Today is ..." statement injected into prompts."""
    now = datetime.now()
    return f"Today is {now.strftime('%A, %Y-%m-%d')}."


def _read_file(path: Path) -> str:
    """Read a context file, returning empty string if missing or unreadable."""
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        logger.warning(f"Context file not found at {path} — skipping.")
        return ""
    except Exception as exc:
        logger.warning(f"Failed to read {path}: {exc}")
        return ""


def _read_bridge_latest_entry(path: Path) -> str:
    """Read the CLAUDE_CODE_HANDOFF.md bridge file and return only the latest
    session entry (from the last "## [...] Session N" header to EOF). Past
    sessions are historical; the latest is what Iris needs to be clued in to.
    Returns empty string if file missing or no session headers found."""
    full = _read_file(path)
    if not full:
        return ""
    # Find the LAST occurrence of a "## [" session header.
    last_header_idx = full.rfind("\n## [")
    if last_header_idx == -1:
        # No session headers — return the whole file (it's just header/intro).
        return full
    return full[last_header_idx + 1:].strip()


def _detect_quick_capture(text: str) -> str | None:
    """If text starts with a Quick Capture prefix, return the matched prefix uppercased; else None."""
    stripped = text.strip()
    upper = stripped.upper()
    for prefix in QUICK_CAPTURE_PREFIXES:
        if upper.startswith(prefix):
            return prefix
    return None


def _save_quick_capture_sync(prefix: str, raw_text: str, user_id: int, username: str) -> None:
    """Append a Quick Capture entry to TELEGRAM_CAPTURE.md (sync; called via run_in_executor)."""
    now = datetime.now()
    QUICK_CAPTURE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not QUICK_CAPTURE_FILE.exists():
        header = (
            "# Telegram Quick Captures — Pending Routing\n\n"
            "Iris-on-Telegram appends every message starting with a Quick Capture\n"
            "prefix (RECEIPT:, PURCHASE:, PAYMENT:, DECISION:, MEETING:,\n"
            "MILESTONE:, STATEMENT:, IDEA:, QUESTION:, FEEDBACK:) here.\n"
            "Cowork-Iris reads this file at session start and routes each entry\n"
            "to its canonical location per Operator Blueprint Section 5, then\n"
            "truncates the file (preserving this header).\n\n"
            "Format: ## YYYY-MM-DD HH:MM:SS | PREFIX | @username (id=...)\n\n"
            "---\n"
        )
        QUICK_CAPTURE_FILE.write_text(header, encoding="utf-8")
    entry = (
        f"\n## {now.strftime('%Y-%m-%d %H:%M:%S')} | {prefix} | "
        f"@{username} (id={user_id})\n\n{raw_text}\n"
    )
    with open(QUICK_CAPTURE_FILE, "a", encoding="utf-8") as f:
        f.write(entry)


async def save_quick_capture(prefix: str, raw_text: str, user_id: int, username: str) -> bool:
    """Append a Quick Capture to TELEGRAM_CAPTURE.md. Returns True on success."""
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: _save_quick_capture_sync(prefix, raw_text, user_id, username),
        )
        logger.info(
            f"Quick Capture saved: prefix={prefix} length={len(raw_text)} from @{username}"
        )
        return True
    except Exception as exc:
        logger.warning(f"Quick Capture save failed: {exc}. Reply continues normally.")
        return False


def _detect_brain_capture(text: str) -> str | None:
    """If text starts with a second-brain capture prefix (BRAIN:/NOTE:/ZK:),
    return the matched prefix uppercased; else None. Checked BEFORE the business
    Quick-Capture prefixes so a second-brain thought is never shadowed."""
    upper = text.strip().upper()
    for prefix in SECOND_BRAIN_PREFIXES:
        if upper.startswith(prefix):
            return prefix
    return None


def _save_brain_capture_sync(prefix: str, raw_text: str, user_id: int, username: str, ts: datetime) -> str:
    """Write a friction-free capture note into the second brain's '00 - INBOX/'.
    Uses the vault's capture-template scaffold so the nightly inbox-processor can
    route it. Returns the absolute path written. Raises on filesystem error."""
    stripped = raw_text.strip()
    content = stripped[len(prefix):].strip() if stripped.upper().startswith(prefix) else stripped
    if not content:
        content = "(empty capture)"

    first_line = content.splitlines()[0].strip()
    slug_source = first_line[:60] or "untitled"
    slug = re.sub(r"[^a-z0-9]+", "_", slug_source.lower()).strip("_")[:40] or "untitled"
    date_str = ts.strftime("%Y-%m-%d")
    time_str = ts.strftime("%H%M%S")

    SECOND_BRAIN_INBOX.mkdir(parents=True, exist_ok=True)
    dest = SECOND_BRAIN_INBOX / f"{date_str}_{time_str}_{slug}.md"
    # Guard against a same-second + same-first-line collision silently overwriting
    # an earlier capture (write_text truncates). Effectively unreachable for a human
    # typist, but cheap insurance — append a short uuid so both notes survive.
    if dest.exists():
        dest = SECOND_BRAIN_INBOX / f"{date_str}_{time_str}_{slug}_{uuid.uuid4().hex[:4]}.md"
    # Capture-template scaffold (07 - SYSTEM/templates/capture.md). The structured
    # lines are left blank for Steve/the inbox-processor — they're optional and the
    # processor honors any that get filled. Title = the first line of the thought.
    body = (
        "---\n"
        "type: capture\n"
        f"created: \"{date_str}\"\n"
        "captured_via: telegram-brain-capture\n"
        f"captured_by: \"@{username} (id={user_id})\"\n"
        "status: inbox\n"
        "---\n\n"
        f"# {first_line[:80]}\n\n"
        f"{content}\n\n"
        "---\n"
        "**CONNECTS TO:** \n"
        "**MIGHT USE FOR:** \n"
        "**RAISES THE QUESTION:** \n"
        "**COULD APPLY TO / ACTION IF TRUE:** \n\n"
        f"<!-- Captured friction-free from Telegram at {ts.strftime('%Y-%m-%d %H:%M:%S')} ET. "
        "The nightly second-brain inbox-processor triages this against the Four Uses and routes it. -->\n"
    )
    dest.write_text(body, encoding="utf-8")
    return str(dest)


async def save_brain_capture(prefix: str, raw_text: str, user_id: int, username: str) -> str | None:
    """Async wrapper for the second-brain capture write. Returns the vault-relative
    path (under SECOND_BRAIN_DIR) on success, else None. Best-effort — logged + swallowed."""
    try:
        loop = asyncio.get_event_loop()
        ts = datetime.now()
        dest = await loop.run_in_executor(
            None,
            lambda: _save_brain_capture_sync(prefix, raw_text, user_id, username, ts),
        )
        rel = str(Path(dest).relative_to(SECOND_BRAIN_DIR))
        logger.info(f"Second-brain capture saved: prefix={prefix} -> {rel} from @{username}")
        return rel
    except Exception as exc:
        logger.warning(f"Second-brain capture failed for {prefix}: {exc}. Reply continues normally.")
        return None


# A-1: Quick Capture routing — when a markdown-safe prefix arrives, route the
# entry to its canonical home inline (in addition to the TELEGRAM_CAPTURE.md
# append above, which remains the audit-trail of record). xlsx-bound prefixes
# (RECEIPT, PURCHASE, PAYMENT, STATEMENT, MILESTONE) are intentionally not
# routed here — Cowork-Iris still handles those at desktop sessions because
# the Expense_Tracker.xlsx surface is risky to auto-modify from the daemon.
_QC_NEW_FILE_ROUTES = {
    "DECISION": ("06_CEO", "Decisions_Log"),
    "MEETING":  ("06_CEO", "Meeting_Notes"),
}
_QC_APPEND_ROUTES = {
    "IDEA":     ("06_CEO", "Improvements_Backlog.md"),
    "QUESTION": ("_Iris_Memory", "Questions.md"),
    "FEEDBACK": ("_Iris_Memory", "Feedback.md"),
}


def _route_quick_capture_sync(prefix: str, raw_text: str, user_id: int, username: str, timestamp: datetime) -> str | None:
    """Route a Quick Capture entry to its canonical home if the prefix is markdown-safe.
    Returns the vault-relative path written, or None if the prefix is xlsx-bound or unrecognized.
    Raises on filesystem errors so the async wrapper can log + swallow."""
    prefix_clean = prefix.rstrip(":").upper()

    stripped = raw_text.strip()
    content = stripped[len(prefix):].strip() if stripped.upper().startswith(prefix) else stripped

    date_str = timestamp.strftime("%Y-%m-%d")
    time_str = timestamp.strftime("%H%M%S")

    slug_source = content[:60] if content else "untitled"
    slug = re.sub(r"[^a-z0-9]+", "_", slug_source.lower()).strip("_")[:40] or "untitled"

    if prefix_clean in _QC_NEW_FILE_ROUTES:
        folder = WORKSPACE_DIR.joinpath(*_QC_NEW_FILE_ROUTES[prefix_clean])
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / f"{date_str}_{slug}.md"
        if dest.exists():
            dest = folder / f"{date_str}_{slug}_{time_str}.md"
        body = (
            "---\n"
            f"type: {prefix_clean.lower()}\n"
            f"date: {date_str}\n"
            "captured_via: telegram-quick-capture\n"
            f"captured_by: \"@{username} (id={user_id})\"\n"
            "---\n\n"
            f"# {prefix_clean.title()}: {slug_source[:60]}\n\n"
            f"_Auto-routed from `TELEGRAM_CAPTURE.md` by iris.py daemon at "
            f"{timestamp.strftime('%Y-%m-%d %H:%M:%S')} ET._\n\n"
            f"{content}\n"
        )
        dest.write_text(body, encoding="utf-8")
        return str(dest.relative_to(WORKSPACE_DIR))

    if prefix_clean in _QC_APPEND_ROUTES:
        dest = WORKSPACE_DIR.joinpath(*_QC_APPEND_ROUTES[prefix_clean])
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            header = (
                f"# {prefix_clean.title()} Log\n\n"
                "Auto-routed entries from Telegram Quick Capture (`TELEGRAM_CAPTURE.md`) "
                "by the iris.py daemon. Most recent on top.\n\n"
                "---\n\n"
            )
            dest.write_text(header, encoding="utf-8")
        entry = (
            f"## {timestamp.strftime('%Y-%m-%d %H:%M:%S')} — @{username}\n\n"
            f"{content}\n\n"
        )
        existing = dest.read_text(encoding="utf-8")
        if "---\n\n" in existing:
            head, rest = existing.split("---\n\n", 1)
            dest.write_text(head + "---\n\n" + entry + rest, encoding="utf-8")
        else:
            with open(dest, "a", encoding="utf-8") as f:
                f.write("\n" + entry)
        return str(dest.relative_to(WORKSPACE_DIR))

    return None


async def route_quick_capture(prefix: str, raw_text: str, user_id: int, username: str) -> str | None:
    """Async wrapper for _route_quick_capture_sync. Best-effort — failures are logged + swallowed,
    since TELEGRAM_CAPTURE.md already holds the entry for manual filing if routing fails."""
    try:
        loop = asyncio.get_event_loop()
        timestamp = datetime.now()
        dest = await loop.run_in_executor(
            None,
            lambda: _route_quick_capture_sync(prefix, raw_text, user_id, username, timestamp),
        )
        if dest:
            logger.info(f"Quick Capture routed: prefix={prefix} -> {dest}")
        return dest
    except Exception as exc:
        logger.warning(
            f"Quick Capture routing failed for {prefix}: {exc}. "
            "TELEGRAM_CAPTURE.md append still holds the entry for manual filing."
        )
        return None


def _read_recent_markdown_files(directory: Path, limit: int, max_lines_per_file: int | None = None) -> str:
    """Read N most recent .md files from a directory by mtime; return concatenated text with filename headers.

    Used for Decisions_Log/ and Sessions/ where the most recent few entries
    are the most relevant context for Iris-on-Telegram.

    Token-reduction (2026-05-27): pass `max_lines_per_file` to truncate each
    file to its first N lines. The headline + summary of a session/decision
    digest is at the top; the body is detail Iris can pull on demand via
    the Obsidian MCP if needed. Capping prevents the recent-sessions section
    from dominating the system-prompt budget when digests grow long.
    """
    try:
        if not directory.exists() or not directory.is_dir():
            return ""
        files = sorted(
            directory.glob("*.md"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:limit]
        if not files:
            return ""
        sections: list[str] = []
        for f in files:
            content = _read_file(f)
            if not content:
                continue
            if max_lines_per_file is not None:
                lines = content.splitlines()
                if len(lines) > max_lines_per_file:
                    truncated = "\n".join(lines[:max_lines_per_file])
                    content = truncated + f"\n\n_[truncated to first {max_lines_per_file} lines — full digest at `{f.name}`; pull via Obsidian MCP if needed]_"
            sections.append(f"## {f.name}\n\n{content}")
        return "\n\n".join(sections)
    except Exception as exc:
        logger.warning(f"Failed to read recent files from {directory}: {exc}")
        return ""


def _format_history_for_cloud(history: list[dict]) -> str:
    """Render conversation history as a markdown block for the cloud system prompt."""
    if not history:
        return ""
    lines = ["# === RECENT CONVERSATION HISTORY (chronological) ==="]
    lines.append("")
    for h in history:
        role = h.get("role", "?")
        content = h.get("content", "").strip()
        ts = h.get("created_at", "")
        tier = h.get("tier")
        if role == "user":
            label = f"[Steve @ {ts}]"
        elif role == "assistant":
            label = f"[Iris @ {ts}" + (f" ({tier})]" if tier else "]")
        else:
            label = f"[{role} @ {ts}]"
        lines.append(f"{label}")
        lines.append(content)
        lines.append("")
    return "\n".join(lines).rstrip()


def load_system_prompt_cloud(history: list[dict]) -> str:
    """Compose the cloud-tier system prompt with full workspace awareness.

    Sections (each freshly loaded on every message):
      1. TELEGRAM_BEHAVIOR_PREFIX
      2. Runtime date
      3. Operator Blueprint (canonical identity + business map)
      4. STEVE_CONTEXT.md (technical addendum)
      5. Vault_MOC.md (AI-agent contract + navigation index)
      6. INBOX.md (active items this week)
      7. DAILY_BRIEFING.md (today's status snapshot)
      8. Build Queue (current Phase 4/5 work state)
      9. CLAUDE_CODE_HANDOFF.md — LATEST entry only (what other agents did most recently)
     10. Last 3 Decisions_Log entries (by mtime — most recent first)
     11. Last 3 Session digests (by mtime — most recent first)
     12. Recent conversation history (last 20 messages)
    """
    blueprint = _read_file(BLUEPRINT_FILE)
    addendum = _read_file(CONTEXT_FILE)
    vault_moc = _read_file(VAULT_MOC_FILE)
    inbox = _read_file(INBOX_FILE)
    briefing = _read_file(DAILY_BRIEFING_FILE)
    build_queue = _read_file(BUILD_QUEUE_FILE)
    bridge_latest = _read_bridge_latest_entry(BRIDGE_FILE)
    recent_decisions = _read_recent_markdown_files(DECISIONS_DIR, RECENT_DECISIONS_LIMIT, max_lines_per_file=40)
    recent_sessions = _read_recent_markdown_files(SESSIONS_DIR, RECENT_SESSIONS_LIMIT, max_lines_per_file=30)
    date_block = _runtime_date_block()
    history_block = _format_history_for_cloud(history)

    sections = [TELEGRAM_BEHAVIOR_PREFIX, f"# === RUNTIME DATE ===\n\n{date_block}"]
    if blueprint:
        sections.append(
            "# === IRIS OPERATOR BLUEPRINT (canonical) ===\n\n" + blueprint
        )
    if addendum:
        sections.append(
            "# === TECHNICAL ADDENDUM (current deployment state) ===\n\n"
            + addendum
        )
    if vault_moc:
        sections.append(
            "# === VAULT MAP OF CONTENT (AI-agent boot sequence + registry + coordination conventions) ===\n\n"
            + vault_moc
        )
    if inbox:
        sections.append(
            "# === INBOX (active items this week) ===\n\n" + inbox
        )
    if briefing:
        sections.append(
            "# === DAILY BRIEFING (today's status snapshot) ===\n\n" + briefing
        )
    if build_queue:
        sections.append(
            "# === BUILD QUEUE (current Phase 4/5 work state — see for what's done, in-progress, queued, blocked) ===\n\n"
            + build_queue
        )
    if bridge_latest:
        sections.append(
            "# === CLAUDE CODE BRIDGE — LATEST SESSION (what other agents did most recently — for Iris's awareness when asked about engineering state) ===\n\n"
            + bridge_latest
        )
    if recent_decisions:
        sections.append(
            f"# === RECENT DECISIONS (last {RECENT_DECISIONS_LIMIT}, most recent first) ===\n\n"
            + recent_decisions
        )
    if recent_sessions:
        sections.append(
            f"# === RECENT SESSION DIGESTS (last {RECENT_SESSIONS_LIMIT}, most recent first) ===\n\n"
            + recent_sessions
        )
    if history_block:
        sections.append(history_block)

    return "\n\n---\n\n".join(sections)


# ============================================================
# Conversation memory (SQLite)
# ============================================================

@contextlib.contextmanager
def _db():
    """Single DB-connection factory. Guarantees the connection is CLOSED (the bare
    `with sqlite3.connect(...)` form commits but never closes — that was the H1 leak),
    sets a 30s busy_timeout + WAL journal so concurrent executor-thread writers wait
    instead of raising `database is locked` and silently dropping rows (H2)."""
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        with conn:  # commit on success / rollback on exception
            yield conn
    finally:
        conn.close()


def _init_db_sync() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                tier TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_id_created ON messages(chat_id, created_at)"
        )
        # Pitch #15 — per-message tier telemetry + cost accumulator.
        # One row per assistant response. tier in {local, cloud, cloud_fallback, tier4}.
        # cost_usd is 0.0 for local + cloud (free), > 0 for tier4 (paid API).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                tier TEXT NOT NULL,
                cost_usd REAL DEFAULT 0.0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_daily_stats_date_tier ON daily_stats(date, tier)"
        )
        # === Phase 5 (P5-2) — dispatcher state machine ===
        # One row per dispatch. status in {pending, running, completed, failed,
        # timed_out}. started_epoch is a float wall-clock used for deliverable
        # detection (files modified at/after start).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dispatches (
                id TEXT PRIMARY KEY,
                agent_name TEXT NOT NULL,
                prompt TEXT NOT NULL,
                status TEXT NOT NULL,
                chat_id TEXT,
                pid INTEGER,
                deliverable_path TEXT,
                error TEXT,
                expected_turnaround_minutes INTEGER,
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                started_epoch REAL,
                completed_at TIMESTAMP
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_dispatches_status ON dispatches(status)"
        )
        # Notifications that couldn't be delivered (bot not ready / Telegram down).
        # Flushed on next daemon boot (and after a successful reconnect).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                text TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        # === Phase 5 (P5-12) — expense-categorizer state machine ===
        # One row per scheduled or on-demand sweep. status in
        # {pending, drafted, approved, rejected, failed}. draft_path is the
        # Expense_Tracker_Drafts/*.md the subagent wrote; candidate_count is
        # filled by the post-completion ingest hook + by /approve.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS expense_categorizer_runs (
                id TEXT PRIMARY KEY,
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                finished_at TIMESTAMP,
                since_date TEXT,
                until_date TEXT,
                draft_path TEXT,
                candidate_count INTEGER DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending'
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_expense_runs_status "
            "ON expense_categorizer_runs(status)"
        )
        # Per-Gmail-msg dedup. Populated by the post-completion ingest hook
        # (status='drafted') when a draft is parsed; flipped to 'approved' or
        # 'rejected' by /approve / /reject. The subagent must filter against
        # this table so it never re-drafts a previously processed message.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS expense_categorizer_processed_msg_ids (
                msg_id TEXT PRIMARY KEY,
                run_id TEXT,
                status TEXT NOT NULL DEFAULT 'drafted',
                processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )


async def _ensure_db() -> None:
    global _db_initialized, _db_init_lock
    if _db_initialized:
        return
    if _db_init_lock is None:
        _db_init_lock = asyncio.Lock()
    async with _db_init_lock:
        if _db_initialized:
            return
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _init_db_sync)
        _db_initialized = True
        logger.info(f"Conversation memory DB ready at {DB_PATH}")


def _get_history_sync(chat_id: str, limit: int) -> list[dict]:
    with _db() as conn:
        cursor = conn.execute(
            "SELECT role, content, tier, created_at FROM messages "
            "WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        )
        rows = cursor.fetchall()
    rows.reverse()  # chronological order
    return [
        {"role": r[0], "content": r[1], "tier": r[2], "created_at": r[3]}
        for r in rows
    ]


async def get_history(chat_id: str, limit: int) -> list[dict]:
    try:
        await _ensure_db()
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: _get_history_sync(chat_id, limit))
    except Exception as exc:
        logger.warning(f"DB history fetch failed for chat_id={chat_id}: {exc}. Continuing without history.")
        return []


def _save_message_sync(chat_id: str, user_id: int, role: str, content: str, tier: str | None) -> None:
    with _db() as conn:
        conn.execute(
            "INSERT INTO messages (chat_id, user_id, role, content, tier) VALUES (?, ?, ?, ?, ?)",
            (chat_id, user_id, role, content, tier),
        )


async def save_message(chat_id: str, user_id: int, role: str, content: str, tier: str | None = None) -> None:
    try:
        await _ensure_db()
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, lambda: _save_message_sync(chat_id, user_id, role, content, tier)
        )
    except Exception as exc:
        logger.warning(f"DB save failed for chat_id={chat_id} role={role}: {exc}. Continuing without persistence.")


# ============================================================
# Pitch #15 — daily tier telemetry + spend tracker
# ============================================================

def _today_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _record_message_stat_sync(tier: str, cost_usd: float) -> None:
    with _db() as conn:
        conn.execute(
            "INSERT INTO daily_stats (date, tier, cost_usd) VALUES (?, ?, ?)",
            (_today_iso(), tier, cost_usd),
        )


async def record_message_stat(tier: str, cost_usd: float = 0.0) -> None:
    """Record one message's tier + cost for today's telemetry."""
    try:
        await _ensure_db()
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, lambda: _record_message_stat_sync(tier, cost_usd)
        )
    except Exception as exc:
        logger.warning(f"Tier-stat save failed for tier={tier} cost={cost_usd}: {exc}")


def _get_today_stats_sync() -> dict:
    today = _today_iso()
    with _db() as conn:
        cursor = conn.execute(
            "SELECT tier, COUNT(*), COALESCE(SUM(cost_usd), 0.0) "
            "FROM daily_stats WHERE date = ? GROUP BY tier",
            (today,),
        )
        rows = cursor.fetchall()
    stats: dict = {
        "local": 0,
        "cloud": 0,
        "cloud_fallback": 0,
        "tier4": 0,
        "tier4_spend_usd": 0.0,
        "total": 0,
        "date": today,
    }
    for tier, count, cost in rows:
        if tier in stats:
            stats[tier] = count
        stats["total"] += count
        if tier == "tier4":
            stats["tier4_spend_usd"] = float(cost)
    return stats


async def get_today_stats() -> dict:
    try:
        await _ensure_db()
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _get_today_stats_sync)
    except Exception as exc:
        logger.warning(f"get_today_stats failed: {exc}")
        return {
            "local": 0, "cloud": 0, "cloud_fallback": 0,
            "tier4": 0, "tier4_spend_usd": 0.0, "total": 0,
            "date": _today_iso(),
        }


async def is_tier4_capped() -> bool:
    """Return True if today's Tier 4 spend has reached or exceeded the daily cap.

    Used by future Pitch #16 full (auto-fallback to Tier 4) to refuse calls
    once the daily safety net is breached. Today this function returns False
    because no Tier 4 calls fire (OAuth path handles everything).
    """
    stats = await get_today_stats()
    return stats["tier4_spend_usd"] >= DAILY_TIER4_CAP_USD


# ============================================================
# Phase 5 (P5-2) — Multi-agent dispatcher
# Implements the locked Interface Contract:
#   06_CEO/Designs/2026-05-26_Phase_5_Multi_Agent_Architecture.md
# ============================================================

def _now_epoch() -> float:
    return datetime.now().timestamp()


def _vault_rel(path_str: str | None) -> str:
    if not path_str:
        return "—"
    try:
        return str(Path(path_str).relative_to(WORKSPACE_DIR))
    except ValueError:
        return path_str


# --- SQLite helpers (sync; run via executor like the rest of the daemon) ---

def _insert_dispatch_sync(d_id, agent_name, prompt, chat_id, expected, started_epoch):
    with _db() as conn:
        conn.execute(
            "INSERT INTO dispatches (id, agent_name, prompt, status, chat_id, "
            "expected_turnaround_minutes, started_epoch) "
            "VALUES (?, ?, ?, 'pending', ?, ?, ?)",
            (d_id, agent_name, prompt, chat_id, expected, started_epoch),
        )


def _update_dispatch_sync(d_id, fields: dict):
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    vals = list(fields.values()) + [d_id]
    with _db() as conn:
        conn.execute(f"UPDATE dispatches SET {cols} WHERE id = ?", vals)


def _list_dispatches_by_status_sync(status):
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute("SELECT * FROM dispatches WHERE status = ?", (status,))
        return [dict(r) for r in cur.fetchall()]


def _count_active_dispatches_sync():
    with _db() as conn:
        cur = conn.execute(
            "SELECT COUNT(*) FROM dispatches WHERE status IN ('pending', 'running')"
        )
        return cur.fetchone()[0]


def _list_recent_dispatches_sync(limit):
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT * FROM dispatches ORDER BY started_at DESC LIMIT ?", (limit,)
        )
        return [dict(r) for r in cur.fetchall()]


def _queue_notification_sync(chat_id, text):
    with _db() as conn:
        conn.execute(
            "INSERT INTO pending_notifications (chat_id, text) VALUES (?, ?)",
            (chat_id, text),
        )


def _pop_pending_notifications_sync():
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM pending_notifications ORDER BY id"
        ).fetchall()]
        if rows:
            conn.execute("DELETE FROM pending_notifications")
    return rows


async def _db_call(fn, *args):
    """Run a sync DB helper in the executor (mirrors get_history's pattern)."""
    await _ensure_db()
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: fn(*args))


# --- Phase 5 (P5-12) — expense-categorizer SQLite helpers ---

def _insert_expense_run_sync(run_id: str, since_date: str, until_date: str) -> None:
    with _db() as conn:
        conn.execute(
            "INSERT INTO expense_categorizer_runs "
            "(id, since_date, until_date, status) VALUES (?, ?, ?, 'pending')",
            (run_id, since_date, until_date),
        )


def _update_expense_run_sync(run_id: str, fields: dict) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    vals = list(fields.values()) + [run_id]
    with _db() as conn:
        conn.execute(
            f"UPDATE expense_categorizer_runs SET {cols} WHERE id = ?", vals
        )


def _get_expense_run_sync(run_id: str) -> dict | None:
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM expense_categorizer_runs WHERE id = ?", (run_id,)
        ).fetchone()
        return dict(row) if row else None


def _get_last_expense_run_until_sync() -> str | None:
    """Most recent run's until_date (any status). Used to pick the next since."""
    with _db() as conn:
        row = conn.execute(
            "SELECT until_date FROM expense_categorizer_runs "
            "ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else None


def _list_processed_msg_ids_sync(limit: int = 500) -> list[str]:
    """Most recent processed msg_ids (any status). Passed to the agent so it
    can dedup against prior runs. Capped to keep the brief tractable."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT msg_id FROM expense_categorizer_processed_msg_ids "
            "ORDER BY processed_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [r[0] for r in rows]


def _upsert_processed_msg_id_sync(msg_id: str, run_id: str, status: str) -> None:
    """Insert or update a msg_id row. Idempotent: re-running the ingest or
    /approve on the same msg_id flips status without duplicating."""
    with _db() as conn:
        conn.execute(
            "INSERT INTO expense_categorizer_processed_msg_ids "
            "(msg_id, run_id, status) VALUES (?, ?, ?) "
            "ON CONFLICT(msg_id) DO UPDATE SET "
            "run_id = excluded.run_id, status = excluded.status, "
            "processed_at = CURRENT_TIMESTAMP",
            (msg_id, run_id, status),
        )


# --- Phase 5 (P5-12) — draft parser + CSV emit ---

# Pulls Gmail message ids out of the agent's draft. The agent emits them as
# `msg-id <id>` in the Receipt source column; tolerate either backtick/quote
# wrapping or none. ids in our experience are 16-hex-ish but Gmail's API uses
# variable-length tokens — keep the character class permissive but anchored.
_MSG_ID_RE = re.compile(
    r"msg-id\s*[`'\"]?([A-Za-z0-9_-]{6,})[`'\"]?",
    re.IGNORECASE,
)

# Pulls one candidate row out of a markdown pipe table. The agent's contract
# is exactly 7 columns: Date | Vendor | Amount | Category | Card | Confidence
# | Receipt source. The header + separator row are skipped by header-match.
_TABLE_ROW_RE = re.compile(r"^\s*\|(.+)\|\s*$")


def _parse_candidate_rows(draft_text: str) -> tuple[list[list[str]], list[str]]:
    """Extract candidate rows + every msg_id referenced anywhere in the draft.

    Rows are returned as 7-column lists in the order they appear under the
    `## Candidate Rows` section, stopping at the next `##` header or EOF.
    msg_ids are deduped + lowercase-normalized.
    """
    rows: list[list[str]] = []
    in_section = False
    seen_header = False
    for raw_line in draft_text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip().lower()
        if stripped.startswith("## "):
            if "candidate rows" in stripped:
                in_section = True
                seen_header = False
                continue
            if in_section:
                break  # exited the section
            continue
        if not in_section:
            continue
        m = _TABLE_ROW_RE.match(line)
        if not m:
            continue
        cells = [c.strip() for c in m.group(1).split("|")]
        if not seen_header:
            # The first matched row is the header (Date | Vendor | ...).
            # The next row is the markdown separator (|---|---|...). Skip both.
            seen_header = True
            continue
        if all(set(c) <= set("-: ") for c in cells):
            # Separator row (|---|---|---|).
            continue
        if len(cells) < 7:
            # Pad to 7 columns so downstream CSV emit doesn't IndexError.
            cells = cells + [""] * (7 - len(cells))
        rows.append(cells[:7])
    msg_ids: list[str] = []
    seen_ids: set[str] = set()
    for m in _MSG_ID_RE.finditer(draft_text):
        mid = m.group(1).strip()
        if mid and mid.lower() not in seen_ids:
            seen_ids.add(mid.lower())
            msg_ids.append(mid)
    return rows, msg_ids


def _csv_escape(field: str) -> str:
    """RFC-4180 CSV escape: quote if the field contains comma, quote, or newline."""
    f = field or ""
    if any(c in f for c in (",", '"', "\n", "\r")):
        return '"' + f.replace('"', '""') + '"'
    return f


def _build_paste_ready_csv(rows: list[list[str]]) -> str:
    """Emit the CSV block (header + N rows) that gets dropped into the draft."""
    header = ["Date", "Vendor", "Amount", "Category", "Card", "Confidence", "Receipt source"]
    lines = [",".join(_csv_escape(c) for c in header)]
    for r in rows:
        lines.append(",".join(_csv_escape(c) for c in r))
    return "\n".join(lines)


def _replace_paste_ready_block(draft_text: str, csv_block: str, when_local: str) -> str:
    """Replace the draft's `## Paste-ready CSV` section with a filled CSV.

    Matches the section header (any suffix in parens) and overwrites the body
    up to the next `##` header or EOF. If no such section exists, append one.
    """
    lines = draft_text.splitlines()
    out: list[str] = []
    i = 0
    n = len(lines)
    replaced = False
    new_body = [
        f"## Paste-ready CSV (filled {when_local})",
        "",
        "```csv",
        csv_block,
        "```",
        "",
    ]
    while i < n:
        line = lines[i]
        if line.lstrip().lower().startswith("## paste-ready csv"):
            out.extend(new_body)
            i += 1
            while i < n and not lines[i].lstrip().startswith("## "):
                i += 1
            replaced = True
            continue
        out.append(line)
        i += 1
    if not replaced:
        if out and out[-1].strip():
            out.append("")
        out.extend(new_body)
    return "\n".join(out) + ("\n" if not draft_text.endswith("\n") else "")


# --- A4 (Redesign Night 4) — Telegram receipt-photo capture helpers ---
#
# Compounds with expense-categorizer: the Sunday sweep handles studio@ Gmail
# receipts; this handles the last manual leg — receipts paid in-person where
# the only artifact is a phone-camera photo. Both feed the same draft +
# /approve workflow, so Steve's mental model is one paste-ready CSV per run
# regardless of channel.

def _parse_receipt_caption(caption: str, now: datetime) -> dict:
    """Extract amount + date + vendor-ish description from a `RECEIPT:` caption.
    Best-effort: empty / unparseable strings get sane fallbacks so the draft
    table is never broken (Steve fills in via /approve review)."""
    raw = caption.strip()
    # Strip the leading RECEIPT: prefix (case-insensitive, optional whitespace).
    if raw.upper().startswith("RECEIPT:"):
        raw = raw[len("RECEIPT:"):].strip()
    amount = ""
    m = _RECEIPT_AMOUNT_RE.search(raw)
    if m:
        amount = "$" + m.group(1)
    date_str = now.strftime("%Y-%m-%d")  # fallback = today
    dm = _RECEIPT_DATE_RE.search(raw)
    if dm:
        # Both branches must validate the result via datetime construction —
        # the regex matches the SHAPE (digits + separators) but does not bound
        # month ∈ [1,12] or day ∈ [1,28..31]. A garbage match like "13/45/26"
        # would propagate "2026-13-45" into the YAML frontmatter (which PyYAML
        # rejects) AND the draft filename. Falling back to `now` keeps the
        # downstream pipeline honest. (Skeptical-review finding #1, 2026-06-13.)
        if dm.group("iso"):
            try:
                parts = dm.group("iso").split("-")
                if len(parts) == 3 and len(parts[0]) == 4:
                    y, mo, d = int(parts[0]), int(parts[1]), int(parts[2])
                    datetime(y, mo, d)  # validates calendar range
                    date_str = f"{y:04d}-{mo:02d}-{d:02d}"
            except (ValueError, IndexError):
                pass
        elif dm.group("us"):
            try:
                bits = dm.group("us").split("/")
                month = int(bits[0])
                day = int(bits[1])
                if len(bits) == 3:
                    year_raw = bits[2]
                    if len(year_raw) == 2:
                        year = 2000 + int(year_raw)
                    else:
                        year = int(year_raw)
                else:
                    year = now.year
                datetime(year, month, day)  # validates calendar range
                date_str = f"{year:04d}-{month:02d}-{day:02d}"
            except (ValueError, IndexError):
                pass
    # Strip recognized amount + date substrings from raw to get vendor-ish text.
    vendor = raw
    if m:
        vendor = vendor.replace(m.group(0), " ").strip()
    if dm:
        vendor = vendor.replace(dm.group(0), " ").strip()
    # Neutralize markdown-table-breaking chars: a `|` would split the 7-column draft
    # row (downstream column-misparse → wrong expense CSV), a newline would inject extra
    # rows. Collapse both to safe separators before truncation (M2).
    vendor = vendor.replace("|", "/").replace("\n", " ").replace("\r", " ")
    vendor = re.sub(r"\s+", " ", vendor).strip(" ,.-")
    if not vendor:
        vendor = "[VERIFY]"
    return {
        "amount": amount or "[VERIFY]",
        "date": date_str,
        "vendor": vendor[:80],
    }


def _write_telegram_receipt_draft_sync(
    run_id: str,
    parsed: dict,
    msg_id: str,
    photo_rel_path: str,
    caption_raw: str,
    username: str,
    now: datetime,
) -> Path:
    """Write the single-row draft into Expense_Tracker_Drafts/ in the same
    format expense-categorizer produces, so the existing /approve handler
    consumes it without code changes."""
    EXPENSE_DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    draft_path = EXPENSE_DRAFTS_DIR / f"{parsed['date']}_{run_id}.md"
    when_str = now.strftime("%Y-%m-%d %H:%M ET")
    body = (
        "---\n"
        f"date: {parsed['date']}\n"
        "type: expense-draft\n"
        "status: ok\n"
        f"run_id: {run_id}\n"
        f"since: {parsed['date']}\n"
        f"until: {parsed['date']}\n"
        "source: telegram-photo\n"
        "candidate_count: 1\n"
        "tags:\n"
        "  - finance/expense-draft\n"
        "  - source/telegram-receipt-photo\n"
        "---\n\n"
        f"# Expense Tracker Draft — Telegram receipt {run_id} ({when_str})\n\n"
        f"_From @{username}'s Telegram photo capture. Receipt image saved at "
        f"`{photo_rel_path}`. Single-row draft — run `/approve {run_id}` after "
        "verifying the row's fields below to fill the Paste-ready CSV block._\n\n"
        "## Candidate Rows\n\n"
        "| Date | Vendor | Amount | Category | Card | Confidence | Receipt source |\n"
        "|---|---|---|---|---|---|---|\n"
        f"| {parsed['date']} | {parsed['vendor']} | {parsed['amount']} | "
        "[VERIFY] | [VERIFY] | Low (telegram-photo caption parse) | "
        f"msg-id `{msg_id}`, photo `{photo_rel_path}` |\n\n"
        "## Categorization rationale\n\n"
        "_Single-row Telegram capture — category was not auto-inferred. Steve "
        "fills the Category + Card columns from the photo + memory before "
        "`/approve`._\n\n"
        "## Original caption\n\n"
        f"```\n{caption_raw.strip()}\n```\n\n"
        "## Paste-ready CSV (filled after `/approve`)\n\n"
        f"_Empty until Steve runs `/approve {run_id}` from Telegram._\n\n"
        "## Run metadata\n\n"
        "| Field | Value |\n"
        "|---|---|\n"
        f"| Run ID | {run_id} |\n"
        f"| Dispatched | Telegram photo capture ({username}) |\n"
        f"| Since | {parsed['date']} |\n"
        f"| Until | {parsed['date']} |\n"
        f"| Source | telegram-photo |\n"
        f"| Receipt photo | `{photo_rel_path}` |\n"
        f"| Candidate rows | 1 |\n"
        f"| Draft written | `02_Finance/Expense_Tracker_Drafts/{draft_path.name}` |\n"
    )
    draft_path.write_text(body, encoding="utf-8")
    return draft_path


async def _handle_receipt_photo(update: Update,
                                context: ContextTypes.DEFAULT_TYPE,
                                caption: str) -> None:
    """End-to-end: download photo → save → write draft → SQLite row →
    Telegram ack with the /approve hint. Best-effort throughout; any
    individual failure is logged and surfaced to Steve as a soft warning
    (the photo + caption are still in Telegram, so nothing is lost)."""
    username = update.effective_user.username or update.effective_user.first_name
    message = update.message
    now = datetime.now()
    # Idempotency key: Telegram's per-chat message id is monotonically
    # increasing and globally unique within the chat scope. Suffix it with
    # `tg-photo-` so it's distinguishable from Gmail ids (which are
    # 16-hex-ish) in the shared dedup table.
    telegram_msg_id = str(message.message_id)
    msg_id = f"tg-photo-{telegram_msg_id}"
    # Short run_id derived from msg_id so two re-sends of the same Telegram
    # photo produce the same run_id (and the second attempt is a no-op when
    # the existing draft is found below).
    run_id = uuid.uuid5(uuid.NAMESPACE_URL, msg_id).hex[:8]
    # If a draft for this msg_id already exists (re-send), do nothing
    # destructive — just reply with the existing /approve hint.
    existing = await _db_call(
        _telegram_receipt_existing_sync, msg_id
    )
    if existing:
        await update.message.reply_text(
            f"Already drafted run `{existing['run_id']}` for this photo. "
            f"Run `/approve {existing['run_id']}` to fill the CSV block, "
            f"or re-send a NEW photo (different image) for a fresh draft."
        )
        return
    # 1. Pick the largest PhotoSize variant (last in the list) and download.
    if not message.photo:
        logger.warning(
            f"A4 receipt-photo handler invoked with no .photo attribute "
            f"(message_id={telegram_msg_id}); ignoring."
        )
        return
    largest = message.photo[-1]
    RECEIPTS_DIR.mkdir(parents=True, exist_ok=True)
    photo_path = RECEIPTS_DIR / f"{now.strftime('%Y-%m-%d_%H%M%S')}_{run_id}.jpg"
    try:
        file_obj = await context.bot.get_file(largest.file_id)
        await file_obj.download_to_drive(custom_path=str(photo_path))
    except Exception as exc:
        logger.warning(
            f"A4 receipt-photo download failed (msg_id={msg_id}): {exc}"
        )
        await update.message.reply_text(
            f"I saved your RECEIPT caption but the photo download failed: "
            f"{exc}\n\nResend the photo and I'll try again."
        )
        return
    photo_rel = str(photo_path.relative_to(WORKSPACE_DIR))
    # 2. Parse the caption for amount/vendor/date.
    parsed = _parse_receipt_caption(caption, now)
    # 3. Write the draft + insert SQLite rows so /approve works.
    try:
        loop = asyncio.get_event_loop()
        draft_path = await loop.run_in_executor(
            None,
            lambda: _write_telegram_receipt_draft_sync(
                run_id, parsed, msg_id, photo_rel, caption, username, now,
            ),
        )
        await _db_call(
            _insert_expense_run_sync, run_id, parsed["date"], parsed["date"],
        )
        await _db_call(
            _update_expense_run_sync, run_id, {
                "draft_path": str(draft_path),
                "candidate_count": 1,
                "status": "drafted",
                "finished_at": now.isoformat(),
            },
        )
        await _db_call(
            _upsert_processed_msg_id_sync, msg_id, run_id, "drafted",
        )
    except Exception as exc:
        logger.exception(f"A4 receipt-photo draft write failed: {exc}")
        await update.message.reply_text(
            f"Photo saved at `{photo_rel}`, but the draft write failed: "
            f"{exc}\n\nThe photo is on disk — Cowork-Iris can file manually."
        )
        return
    # 4. NO TELEGRAM_CAPTURE.md append for photo receipts. The text-only
    # RECEIPT: path appends because Cowork-Iris routes those rows by hand
    # into Expense_Tracker.xlsx at her next desk session — but A4 has ALREADY
    # routed this one (draft + SQLite rows + /approve fills the CSV block).
    # An audit append here would risk Cowork double-filing the same receipt.
    # The draft markdown IS the audit trail; the SQLite expense_categorizer_runs
    # row is the run-state record. Skeptical-review finding #2 (2026-06-13).
    # 5. Ack with the next-step hint Steve already knows from the Sunday sweep.
    await update.message.reply_text(
        f"📸 Receipt logged.\n"
        f"• Photo: `{photo_rel}`\n"
        f"• Parsed: {parsed['date']} | {parsed['vendor']} | {parsed['amount']}\n"
        f"• Draft: `02_Finance/Expense_Tracker_Drafts/{draft_path.name}`\n\n"
        f"Run `/approve {run_id}` after eyeballing the parsed row "
        "(amount + vendor + date) — same flow as the Sunday Gmail sweep."
    )
    logger.info(
        f"A4 receipt-photo drafted: run_id={run_id} msg_id={msg_id} "
        f"photo={photo_rel} draft={draft_path}"
    )


def _telegram_receipt_existing_sync(msg_id: str) -> dict | None:
    """Look up whether this msg_id already has a drafted/approved row.
    Used for idempotency on a Telegram photo re-send (the user clicks the
    same photo again — common UX). Returns the matching processed_msg_id
    row's `run_id` + `status`, or None if unseen."""
    with _db() as con:
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT msg_id, run_id, status FROM "
            "expense_categorizer_processed_msg_ids WHERE msg_id = ?",
            (msg_id,),
        ).fetchone()
        return dict(row) if row else None


# --- Phase 5 (P5-12) — scheduled sweep + /approve handler ---

# Flipped 2026-05-31 (Session 17) from daily 09:00 → weekly Sunday; then moved
# 2026-05-31 (Session 19) from Sun 18:00 → Sun 04:00 per Steve's "all autonomous
# fires between 12am and 6am" directive. 9-day lookback gives ~2 days overlap
# insurance if a Sunday fire misfires; the dedup table
# (`expense_categorizer_processed_msg_ids`) handles repeats.
EXPENSE_CATEGORIZER_DAY_OF_WEEK = "sun"
EXPENSE_CATEGORIZER_HOUR = 4   # 04:00 ET — weekly Sunday early-morning receipt sweep
EXPENSE_CATEGORIZER_MINUTE = 0
EXPENSE_CATEGORIZER_LOOKBACK_DAYS = 9  # was 7 (daily); bumped for weekly cadence margin


async def fire_expense_categorizer_sweep(chat_id: int) -> None:
    """Daily 09:00 ET job: kick the expense-categorizer subagent in scheduled mode.

    Inserts a new run row, builds a brief naming run_id/since/until + the
    processed-msg_id dedup list, dispatches the agent via the standard
    dispatcher path. The deliverable notification + draft-parse + msg_id
    ingest happen in _finish_dispatch's post-completion hook.
    """
    try:
        run_id = uuid.uuid4().hex[:8]
        now_local = datetime.now(TIMEZONE)
        until_iso = now_local.strftime("%Y-%m-%d")
        since_iso = (now_local - timedelta(days=EXPENSE_CATEGORIZER_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
        await _db_call(_insert_expense_run_sync, run_id, since_iso, until_iso)
        processed_ids = await _db_call(_list_processed_msg_ids_sync, 500)
        processed_sample = ", ".join(processed_ids[:200])
        if len(processed_ids) > 200:
            processed_sample += f" ... (+{len(processed_ids) - 200} more)"
        brief = (
            f"Scheduled sweep. Operate in your **Scheduled mode** per your agent file.\n\n"
            f"run_id: {run_id}\n"
            f"since: {since_iso}\n"
            f"until: {until_iso}\n"
            f"already_processed_msg_ids ({len(processed_ids)} total — these MUST be skipped):\n"
            f"{processed_sample or '(none yet — first scheduled run)'}\n\n"
            "Write your draft to "
            f"`02_Finance/Expense_Tracker_Drafts/{until_iso}_{run_id}.md` "
            "per your Deliverable contract. After writing, return a one-sentence "
            "stdout summary (the daemon parses your draft to ingest msg_ids and "
            "ping Steve)."
        )
        result = await _start_dispatch(
            "expense-categorizer", brief, str(chat_id), expected=10
        )
        logger.info(
            f"Expense categorizer sweep fired: run_id={run_id}, "
            f"since={since_iso}, until={until_iso}, "
            f"dispatch_id={result['dispatch_id']}, "
            f"processed_dedup_count={len(processed_ids)}"
        )
    except Exception as exc:
        logger.exception(f"Expense categorizer sweep failed to fire: {exc}")


def _read_deliverable_status(deliverable_path: str | None) -> dict:
    """V1 status contract (2026-06-10): parse the `status:` YAML frontmatter
    field from an agent deliverable. Synchronous, best-effort, never raises.

    Returns a dict with keys `category` and `raw`:
      - category="ok"             — status starts with "ok" / any non-block/partial value
      - category="blocked"        — status starts with "blocked"
      - category="partial"        — status starts with "partial"
      - category="missing"        — frontmatter present but no status: field
      - category="no-frontmatter" — file does not open with a YAML `---` block
      - category="unreadable"     — file could not be read (raw=exception text)

    Caller surfaces `blocked` / `partial` / `missing` / `no-frontmatter` as a
    Telegram warning so dispatch-mechanical success cannot mask semantic
    failure (root cause of the 2026-06-07 expense-categorizer silent miss).
    """
    if not deliverable_path:
        return {"category": "unreadable", "raw": "no path"}
    try:
        text = Path(deliverable_path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"category": "unreadable", "raw": str(exc)}
    head = text.lstrip()
    if not head.startswith("---"):
        return {"category": "no-frontmatter", "raw": None}
    body = head[3:]
    end_idx = body.find("\n---")
    if end_idx == -1:
        return {"category": "no-frontmatter", "raw": None}
    fm = body[:end_idx]
    m = re.search(r"(?mi)^status:\s*(.+?)\s*$", fm)
    if not m:
        return {"category": "missing", "raw": None}
    raw = m.group(1).strip().strip('"').strip("'")
    low = raw.lower()
    if low.startswith("blocked"):
        return {"category": "blocked", "raw": raw}
    if low.startswith("partial"):
        return {"category": "partial", "raw": raw}
    return {"category": "ok", "raw": raw}


# A2 — Lint-gated calibration auto-pass (Workflow Autonomy Redesign Night 3,
# 2026-06-12). Spec: 06_CEO/Designs/2026-06-09_Workflow_Autonomy_Redesign.md.
# When an AUTONOMOUS dispatch completes with lint clean + status:ok + a
# recurring (not first-of-its-kind) deliverable, suppress the full-content
# Telegram surface — Steve gets a short audit line + path. 3-vids/wk agent
# throughput sustainable inside the 10-20 hr/wk budget depends on this. Steve's
# own dispatches always get the full surface (auto-pass only fires for
# autonomous_label is not None).
A2_STATE_FILE = Path("/Users/steve/iris_studio/state/a2_seen_kinds.tsv")


def _a2_compute_kind(agent_name: str, deliverable_path: str | None) -> str | None:
    """`kind` = agent_name + ':' + parent-dir of the deliverable.

    Most agents ship one kind (scriptwriter → Production_Scripts/); a few
    differentiate by subdir (scene-image-prompt-generator → Scene_Image_Prompts/,
    video-description-writer → Video_Descriptions/). Parent-dir is the stable
    discriminator that doesn't force per-agent config.
    """
    if not deliverable_path:
        return None
    try:
        parent = Path(deliverable_path).parent.name
    except Exception:
        return None
    return f"{agent_name}:{parent}"


def _a2_seen_kind(kind: str) -> bool:
    if not kind or not A2_STATE_FILE.exists():
        return False
    try:
        for line in A2_STATE_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            cols = line.split("\t")
            if cols and cols[0] == kind:
                return True
    except OSError:
        return False
    return False


def _a2_register_kind(kind: str, deliverable_path: str) -> None:
    if not kind:
        return
    try:
        A2_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        is_new = not A2_STATE_FILE.exists()
        today_iso = datetime.now().date().isoformat()
        with open(A2_STATE_FILE, "a", encoding="utf-8") as f:
            if is_new:
                f.write("# kind\tfirst_seen\tfirst_path\n")
            f.write(f"{kind}\t{today_iso}\t{deliverable_path}\n")
    except OSError as exc:
        logger.warning(f"A2 register failed for {kind!r}: {exc}")


async def _ingest_expense_draft(d_id: str, deliverable_path: str | None) -> None:
    """Post-completion hook for expense-categorizer dispatches.

    Reads the draft, parses candidate rows + msg_ids, inserts msg_ids into the
    dedup table with status='drafted', and updates the run row with
    candidate_count + status='drafted' + draft_path. Best-effort: a parse
    failure here is logged but doesn't break the standard dispatch path.
    """
    if not deliverable_path:
        return
    try:
        text = Path(deliverable_path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning(f"P5-12 ingest: could not read draft {deliverable_path}: {exc}")
        return
    # Pull run_id out of the draft's frontmatter; fall back to filename.
    run_id_match = re.search(r"^run_id:\s*(\S+)\s*$", text, re.MULTILINE)
    run_id = run_id_match.group(1).strip() if run_id_match else None
    if not run_id:
        stem = Path(deliverable_path).stem
        # Filename convention: <until_iso>_<run_id>.md → last underscore-segment
        if "_" in stem:
            run_id = stem.rsplit("_", 1)[-1]
    rows, msg_ids = _parse_candidate_rows(text)
    # A-26 (2026-06-10): if the agent reports a `blocked-*` semantic status in
    # the draft frontmatter, log a WARNING so the failure is visible in the
    # log digest even before the Telegram surface (V1 hook in _finish_dispatch)
    # catches it. The 6/7 incident: rows=1 came from the empty-table template,
    # not real receipts; status: blocked-gmail-oauth was the only honest signal.
    status_info = _read_deliverable_status(deliverable_path)
    if status_info.get("category") == "blocked":
        logger.warning(
            f"P5-12 ingest: draft reports blocked status "
            f"{status_info.get('raw')!r} — rows={len(rows)} may be a "
            f"blocker-template artifact, not real receipts. "
            f"draft={deliverable_path}"
        )
    if run_id:
        await _db_call(_update_expense_run_sync, run_id, {
            "draft_path": deliverable_path,
            "candidate_count": len(rows),
            "status": "drafted",
            "finished_at": datetime.now().isoformat(),
        })
    for msg_id in msg_ids:
        await _db_call(
            _upsert_processed_msg_id_sync, msg_id, run_id or "unknown", "drafted"
        )
    logger.info(
        f"P5-12 ingest: dispatch={d_id} run_id={run_id} "
        f"rows={len(rows)} new/updated msg_ids={len(msg_ids)} "
        f"draft={deliverable_path}"
    )


async def _handle_approve_command(update, chat_id: str, run_id: str) -> None:
    """Handle `/approve <run-id>`: fill the draft's Paste-ready CSV block,
    mark msg_ids as approved, mark the run as approved, ack to Steve.

    Idempotent — running /approve twice just re-fills the CSV with the same
    rows. The draft remains the source of truth; Steve still pastes manually
    into Expense_Tracker.xlsx (v1 design — no programmatic xlsx write).
    """
    drafts_dir = WORKSPACE_DIR / "02_Finance/Expense_Tracker_Drafts"
    if not drafts_dir.exists():
        await update.message.reply_text(
            f"No drafts dir at `{_vault_rel(str(drafts_dir))}`. "
            "Has expense-categorizer ever run?"
        )
        return
    candidates = list(drafts_dir.glob(f"*_{run_id}.md"))
    if not candidates:
        await update.message.reply_text(
            f"No draft found for run-id `{run_id}`. "
            f"Drafts live under `{_vault_rel(str(drafts_dir))}` — "
            f"send /dispatches to see recent runs."
        )
        return
    if len(candidates) > 1:
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    draft_path = candidates[0]
    try:
        text = draft_path.read_text(encoding="utf-8")
    except OSError as exc:
        await update.message.reply_text(f"Could not read draft `{draft_path.name}`: {exc}")
        return
    rows, msg_ids = _parse_candidate_rows(text)
    if not rows:
        await update.message.reply_text(
            f"No candidate rows parsed from `{draft_path.name}`. "
            "Open the draft and check the `## Candidate Rows` table is well-formed."
        )
        return
    csv_block = _build_paste_ready_csv(rows)
    when_local = datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M ET")
    new_text = _replace_paste_ready_block(text, csv_block, when_local)
    try:
        draft_path.write_text(new_text, encoding="utf-8")
    except OSError as exc:
        await update.message.reply_text(f"Could not write draft `{draft_path.name}`: {exc}")
        return
    for msg_id in msg_ids:
        await _db_call(
            _upsert_processed_msg_id_sync, msg_id, run_id, "approved"
        )
    await _db_call(_update_expense_run_sync, run_id, {
        "status": "approved",
        "finished_at": datetime.now().isoformat(),
        "candidate_count": len(rows),
    })
    await update.message.reply_text(
        f"✅ Approved {len(rows)} row{'s' if len(rows) != 1 else ''} from run `{run_id}` "
        f"({len(msg_ids)} msg_id{'s' if len(msg_ids) != 1 else ''} marked approved).\n\n"
        f"📂 CSV filled at: `{_vault_rel(str(draft_path))}`\n\n"
        "Open the draft, copy the block under `## Paste-ready CSV`, paste into "
        "the Expense_Tracker.xlsx Receipts tab, save."
    )


async def _handle_adapt_command(update, chat_id: str, subcommand: str, arg: str) -> None:
    """Handle `/adapt [list|show <id>|approve <id>|reject <id> [reason]]`.

    The ADAPTS-loop one-tap gate. The adaptation-proposer agent DRAFTS proposals
    into 06_CEO/Adaptations/queue/; Steve reviews here and approves/rejects. The
    actual file edit is performed by the deterministic apply core
    (scripts/adaptation.py) — never by the LLM proposer. This handler only routes
    Steve's decision to that core; the core re-validates the allowlist and the
    exact-once match at apply time.
    """
    if _adaptation is None:
        await update.message.reply_text(
            "⚠️ Adaptation apply core failed to load — /adapt is disabled. "
            "Check the daemon logs for the import error."
        )
        return

    loop = asyncio.get_running_loop()

    async def _run(fn, *args):
        return await loop.run_in_executor(None, lambda: fn(*args))

    AdaptError = _adaptation.AdaptError

    if subcommand in ("", "list"):
        try:
            props = await _run(_adaptation.list_proposals)
        except Exception as exc:  # noqa: BLE001
            await update.message.reply_text(f"Couldn't list proposals: {exc}")
            return
        if not props:
            await update.message.reply_text(
                "📭 No pending adaptation proposals. The queue is clean."
            )
            return
        lines = [f"📋 {len(props)} pending adaptation proposal(s):", ""]
        for p in props:
            tgt = _vault_rel(p.target) if p.target else "(no target)"
            conf = p.meta.get("confidence", "?")
            lines.append(f"• `{p.pid}` [{conf}] → {tgt}")
            lines.append(f"   {p.summary}")
        lines.append("")
        lines.append("Review one: `/adapt show <id>` · then `/adapt approve <id>` or `/adapt reject <id> [reason]`")
        await update.message.reply_text("\n".join(lines))
        return

    if subcommand == "show":
        if not arg:
            await update.message.reply_text("Usage: /adapt show <id>")
            return
        pid = arg.split(None, 1)[0]
        try:
            body = await _run(_adaptation.show_proposal, pid)
        except AdaptError as exc:
            await update.message.reply_text(f"⚠️ {exc}")
            return
        except Exception as exc:  # noqa: BLE001
            await update.message.reply_text(f"Couldn't show `{pid}`: {exc}")
            return
        await update.message.reply_text(body)
        return

    if subcommand == "approve":
        if not arg:
            await update.message.reply_text("Usage: /adapt approve <id>")
            return
        pid = arg.split(None, 1)[0]
        try:
            res = await _run(_adaptation.apply_proposal, pid)
        except AdaptError as exc:
            await update.message.reply_text(f"❌ Not applied: {exc}")
            return
        except Exception as exc:  # noqa: BLE001
            await update.message.reply_text(f"Apply failed for `{pid}`: {exc}")
            return
        await update.message.reply_text(
            f"✅ Applied `{res['pid']}`.\n"
            f"📝 {res['summary']}\n"
            f"📂 Target: `{_vault_rel(res['target'])}`\n"
            f"💾 Backup: `{Path(res['backup']).name}`\n"
            "The next dispatch of that agent/standard will use the new text."
        )
        return

    if subcommand == "reject":
        if not arg:
            await update.message.reply_text("Usage: /adapt reject <id> [reason]")
            return
        parts = arg.split(None, 1)
        pid = parts[0]
        reason = parts[1].strip() if len(parts) > 1 else ""
        try:
            res = await _run(_adaptation.reject_proposal, pid, reason)
        except AdaptError as exc:
            await update.message.reply_text(f"⚠️ {exc}")
            return
        except Exception as exc:  # noqa: BLE001
            await update.message.reply_text(f"Reject failed for `{pid}`: {exc}")
            return
        suffix = f" (reason: {reason})" if reason else ""
        await update.message.reply_text(
            f"🗑️ Rejected `{res['pid']}`{suffix}.\n📝 {res['summary']}"
        )
        return

    # --- Outcome tagging: close the learning loop (did the edit help?) --------
    if subcommand == "due":
        try:
            props = await _run(_adaptation.due_for_review)
        except Exception as exc:  # noqa: BLE001
            await update.message.reply_text(f"Couldn't list due reviews: {exc}")
            return
        if not props:
            await update.message.reply_text(
                "✅ No applied adaptations are due for an outcome review."
            )
            return
        lines = [f"🔎 {len(props)} applied adaptation(s) due for an outcome call:", ""]
        for p in props:
            tgt = _vault_rel(p.target) if p.target else "(no target)"
            eff = p.meta.get("expected_effect") or p.meta.get("metric") or "(no hypothesis)"
            lines.append(f"• `{p.pid}` (due {p.meta.get('evaluate_after', '?')}) → {tgt}")
            lines.append(f"   {p.summary}")
            lines.append(f"   expected: {eff}")
        lines.append("")
        lines.append("Record: `/adapt outcome <id> <improved|no-change|regressed|inconclusive> [note]`")
        await update.message.reply_text("\n".join(lines))
        return

    if subcommand == "score":
        try:
            sb = await _run(_adaptation.scoreboard)
        except Exception as exc:  # noqa: BLE001
            await update.message.reply_text(f"Couldn't build scoreboard: {exc}")
            return
        c = sb["counts"]
        if sb["hit_rate"] is None:
            hr = "n/a (no decisive outcomes yet)"
        else:
            hr = f"{sb['hit_rate']*100:.0f}% ({c['improved']}/{sb['decisive']} decisive)"
        await update.message.reply_text(
            "📊 Adaptation outcome scoreboard\n"
            f"• applied total: {sb['total']}\n"
            f"• improved: {c['improved']}  ·  no-change: {c['no-change']}  ·  "
            f"regressed: {c['regressed']}\n"
            f"• inconclusive: {c['inconclusive']}  ·  untagged: {sb['untagged']}\n"
            f"• hit-rate: {hr}"
        )
        return

    if subcommand == "outcome":
        parts = arg.split(None, 2) if arg else []
        if len(parts) < 2:
            await update.message.reply_text(
                "Usage: /adapt outcome <id> <improved|no-change|regressed|inconclusive> [note]"
            )
            return
        pid, verdict = parts[0], parts[1]
        note = parts[2].strip() if len(parts) > 2 else ""
        try:
            res = await _run(_adaptation.tag_outcome, pid, verdict, note)
        except AdaptError as exc:
            await update.message.reply_text(f"⚠️ {exc}")
            return
        except Exception as exc:  # noqa: BLE001
            await update.message.reply_text(f"Tag failed for `{pid}`: {exc}")
            return
        await update.message.reply_text(
            f"🏷️ Recorded `{res['pid']}` → *{res['outcome']}*.\n📝 {res['summary']}"
        )
        return

    await update.message.reply_text(
        "Usage: /adapt [list | show <id> | approve <id> | reject <id> [reason] | "
        "due | score | outcome <id> <verdict> [note]]"
    )


async def _update_dispatch(d_id, **fields):
    await _db_call(_update_dispatch_sync, d_id, fields)


# --- Telegram delivery (with deliver-on-reconnect fallback) ---

async def _send_telegram(chat_id, text) -> bool:
    """Send a Telegram message via the daemon bot, chunked. If the bot isn't
    ready or the send fails, queue the message to pending_notifications so it
    goes out on the next daemon boot."""
    if not chat_id:
        return False
    if _telegram_bot is None:
        await _db_call(_queue_notification_sync, str(chat_id), text)
        logger.warning("Dispatcher: bot not ready; notification queued to SQLite.")
        return False
    try:
        for i in range(0, len(text), TELEGRAM_MAX_MSG):
            await _telegram_bot.send_message(
                chat_id=int(chat_id), text=text[i:i + TELEGRAM_MAX_MSG]
            )
        return True
    except Exception as exc:
        logger.warning(f"Dispatcher: Telegram send failed ({exc}); queuing notification.")
        await _db_call(_queue_notification_sync, str(chat_id), text)
        return False


# --- Deliverable detection + synthesis ---

def _find_deliverable(agent_name: str, started_epoch: float,
                      finished_epoch: float | None = None,
                      allow_vault_wide: bool = True) -> str | None:
    """Best-effort: the file the subagent most likely produced. Prefer the
    newest file modified within this dispatch's [start, finish] window under the
    agent's configured deliverable_dir; else (only when no other dispatch is
    concurrently active) the newest such file anywhere in the vault; else None
    (caller falls back to stdout). Skips dotfiles/dot-dirs.

    The time-window upper bound + the `allow_vault_wide` gate exist to stop
    cross-dispatch mis-attribution (H3): without them, a concurrent dispatch's
    freshly written file (anywhere in the vault) could be returned as THIS
    agent's deliverable. When other dispatches are active the caller passes
    allow_vault_wide=False so we only trust the agent's own dir."""
    cfg = DISPATCH_AGENTS.get(agent_name, {})
    search_dirs = []
    if cfg.get("deliverable_dir"):
        search_dirs.append(WORKSPACE_DIR / cfg["deliverable_dir"])
    if allow_vault_wide:
        search_dirs.append(WORKSPACE_DIR)
    for base in search_dirs:
        if not base.exists():
            continue
        newest, newest_mtime = None, started_epoch - 1
        for p in base.rglob("*"):
            if not p.is_file() or any(part.startswith(".") for part in p.parts):
                continue
            try:
                m = p.stat().st_mtime
            except OSError:
                continue
            if m < started_epoch or (finished_epoch is not None and m > finished_epoch):
                continue
            if m > newest_mtime:
                newest, newest_mtime = p, m
        if newest is not None:
            return str(newest)
    return None


def _synthesize(stdout: str, deliverable: str | None) -> str:
    """Short synthesis for the Telegram notification: prefer the subagent's own
    stdout; if it's thin and a deliverable exists, peek at the file head."""
    text = (stdout or "").strip()
    if len(text) < 40 and deliverable:
        try:
            text = Path(deliverable).read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            pass
    if len(text) > 1500:
        text = text[:1500].rstrip() + "…"
    return text or "(subagent produced no stdout)"


# --- The dispatch lifecycle ---

def _save_partial_dispatch_output(d_id: str, agent_name: str, prompt: str,
                                  started_epoch: float, timeout_s: int,
                                  stdout: str, stderr: str) -> str | None:
    """On dispatch timeout, write whatever the subagent produced before being
    killed to a vault file, so the work isn't lost. The original behavior was to
    discard it (Telegram message even said so). Returns the vault-relative path
    string on success, None on error. Steve can open the file and finish the work
    with Cowork, or paste a chunk into a retry with a tighter ask."""
    try:
        out_dir = WORKSPACE_DIR / "_Iris_Memory" / "Dispatch_Partials"
        out_dir.mkdir(parents=True, exist_ok=True)
        date_s = datetime.now().strftime("%Y-%m-%d")
        path = out_dir / f"{date_s}_{agent_name}_{d_id}.md"
        elapsed = max(0, int(_now_epoch() - started_epoch))
        elapsed_str = f"{elapsed // 60}m {elapsed % 60}s"
        stderr_tail = (stderr or "")[-2000:]
        started_iso = datetime.fromtimestamp(started_epoch).isoformat(timespec="seconds")
        body = (
            f"# Partial output — {agent_name} dispatch `{d_id}` (timed out)\n\n"
            f"- **Dispatched:** {started_iso}\n"
            f"- **Timed out after:** {elapsed_str} (configured limit {timeout_s}s)\n"
            f"- **Prompt:**\n\n```\n{prompt}\n```\n\n"
            f"## Captured stdout ({len(stdout or '')} bytes)\n\n"
            f"```\n{stdout or '(empty)'}\n```\n\n"
            f"## Captured stderr (last 2000 chars)\n\n"
            f"```\n{stderr_tail or '(empty)'}\n```\n"
        )
        path.write_text(body, encoding="utf-8")
        return str(path.relative_to(WORKSPACE_DIR))
    except Exception as exc:
        logger.warning(f"Failed to save partial dispatch output for {d_id}: {exc}")
        return None


async def _finish_dispatch(d_id, agent_name, chat_id, started_epoch,
                           returncode, stdout, stderr, timed_out,
                           partial_path=None, autonomous_label=None):
    """Resolve terminal state, find the deliverable, persist, notify Steve.
    autonomous_label, if set, prefixes the Telegram notification with
    `🤖 Autonomous (<label>):` so scheduled fires are distinguishable from
    user-initiated / model-initiated dispatches."""
    elapsed = max(0, int(_now_epoch() - started_epoch))
    elapsed_str = f"{elapsed // 60}m {elapsed % 60}s"

    if timed_out:
        status = "timed_out"
    elif returncode == 0:
        status = "completed"
    else:
        status = "failed"

    deliverable = None
    if status == "completed" and agent_name != DISPATCH_ECHO_AGENT:
        finished_epoch = _now_epoch()
        # This dispatch is still 'running' here (flipped to terminal below), so a
        # count of 1 means it's the only active dispatch → the vault-wide fallback
        # is safe. >1 means a concurrent dispatch could own files outside this
        # agent's dir, so restrict to the agent's own deliverable_dir (H3).
        active_now = await _db_call(_count_active_dispatches_sync)
        loop = asyncio.get_event_loop()
        deliverable = await loop.run_in_executor(
            None,
            lambda: _find_deliverable(
                agent_name, started_epoch, finished_epoch, active_now <= 1
            ),
        )

    await _update_dispatch(
        d_id,
        status=status,
        deliverable_path=deliverable,
        error=(stderr[:2000] if status != "completed" else None),
        completed_at=datetime.now().isoformat(),
    )
    logger.info(
        f"Dispatch {d_id}: {status} after {elapsed_str} "
        f"(rc={returncode}, deliverable={deliverable})"
    )

    # Phase 5 (P5-12) — agent-specific post-completion hook: parse the
    # expense-categorizer draft + ingest msg_ids into the dedup table so
    # the next scheduled sweep skips them. Best-effort — never raises.
    if status == "completed" and agent_name == "expense-categorizer":
        try:
            await _ingest_expense_draft(d_id, deliverable)
        except Exception as exc:
            logger.warning(f"P5-12 ingest failed for dispatch {d_id}: {exc}")

    # Build 4 docx hook (2026-06-19): when scriptwriter ships a script .md,
    # auto-render a .docx beside it via pandoc so Steve's review copy exists
    # without a manual conversion step. Best-effort: a failure (no pandoc,
    # bad markdown) only logs — it never affects the dispatch outcome. Only the
    # mechanical .docx is generated here; the _REVIEW_PREP skim view is a content
    # derivation owned by the script-reviewer pair, not this hook.
    if (status == "completed" and agent_name == "scriptwriter"
            and deliverable and _script_to_docx is not None
            and str(deliverable).lower().endswith(".md")
            and "/_REVIEW_PREP/" not in str(deliverable)):
        try:
            loop = asyncio.get_event_loop()
            docx_path = await loop.run_in_executor(
                None, lambda: _script_to_docx.convert(deliverable)
            )
            logger.info(f"Build 4 docx hook: wrote {docx_path} for dispatch {d_id}")
        except Exception as exc:
            logger.warning(f"Build 4 docx hook failed for dispatch {d_id}: {exc}")

    # V1 — Semantic-status contract (2026-06-10): read the deliverable's
    # `status:` YAML frontmatter for ALL agents that produced a file.
    # blocked-*/partial-*/missing fields surface a Telegram warning so
    # dispatch-mechanical success (rc=0 + file exists + parse) cannot mask
    # semantic failure. Root cause: 2026-06-07 expense-categorizer blocked
    # by OAuth, dispatch reported "clean," Sessions 27+28 blessed it. The
    # only honest signal was the draft's `status: blocked-gmail-oauth`.
    draft_status_info = None
    if (status == "completed" and deliverable
            and agent_name != DISPATCH_ECHO_AGENT):
        try:
            loop = asyncio.get_event_loop()
            draft_status_info = await loop.run_in_executor(
                None, lambda: _read_deliverable_status(deliverable)
            )
            cat = draft_status_info.get("category")
            if cat == "blocked":
                logger.warning(
                    f"V1 status hook: dispatch {d_id} ({agent_name}) "
                    f"reports status={draft_status_info.get('raw')!r}. "
                    f"Deliverable: {deliverable}"
                )
            elif cat in ("partial", "missing", "no-frontmatter"):
                logger.info(
                    f"V1 status hook: dispatch {d_id} ({agent_name}) "
                    f"status category={cat} raw={draft_status_info.get('raw')!r}"
                )
        except Exception as exc:
            logger.warning(f"V1 status hook failed for dispatch {d_id}: {exc}")

    # A-23 — post-dispatch output lint: banned-vocab grep + monetary overview.
    # Read-only; writes a `<deliverable>_lint.md` report next to the deliverable
    # when worth reporting; returns a result the caller can use to extend the
    # Telegram notification. Skip echo (no deliverable) and expense-categorizer
    # (already has its own draft-parse hook above; the structured CSV is the
    # source of truth, not text patterns). Best-effort — never raises.
    lint_result = None
    if (status == "completed" and deliverable
            and agent_name not in (DISPATCH_ECHO_AGENT, "expense-categorizer")):
        try:
            loop = asyncio.get_event_loop()
            lint_result = await loop.run_in_executor(
                None, lambda: _agent_output_lint.lint_and_report(Path(deliverable))
            )
            if lint_result and lint_result.get("should_alert"):
                banned_n = len(lint_result.get("banned", []))
                logger.info(
                    f"A-23 lint flagged dispatch {d_id} ({agent_name}): "
                    f"{banned_n} banned-vocab occurrence(s). "
                    f"Report: {lint_result.get('report_path')}"
                )
        except Exception as exc:
            logger.warning(f"A-23 lint failed for dispatch {d_id}: {exc}")

    # A2 — Lint-gated calibration auto-pass (Redesign Night 3, 2026-06-12).
    # AUTONOMOUS fire + lint clean + status:ok + NOT first-of-kind → suppress
    # the full content surface; Steve sees a short audit line. Steve's own
    # dispatches (autonomous_label is None) ALWAYS get the full surface.
    # First-of-kind is registered (so subsequent fires auto-pass) but the
    # current fire still surfaces fully so Steve sees the baseline output.
    auto_pass = False
    if (status == "completed"
            and autonomous_label is not None
            and deliverable
            and agent_name not in (DISPATCH_ECHO_AGENT, "expense-categorizer")):
        lint_clean = not (lint_result and lint_result.get("should_alert"))
        status_cat = draft_status_info.get("category") if draft_status_info else None
        status_ok = status_cat == "ok"
        if lint_clean and status_ok:
            kind = _a2_compute_kind(agent_name, deliverable)
            if kind and _a2_seen_kind(kind):
                auto_pass = True
                logger.info(
                    f"A2 auto-pass: dispatch {d_id} ({agent_name}) — "
                    f"lint clean + status:ok + kind '{kind}' previously seen."
                )
            elif kind:
                _a2_register_kind(kind, deliverable)
                logger.info(
                    f"A2 first-of-kind: dispatch {d_id} ({agent_name}) — "
                    f"kind '{kind}' registered; Steve sees full surface."
                )

    if not chat_id:
        return

    # Prefix scheduled-fire notifications so Steve can tell them from his own /
    # the model's dispatches at a glance. Empty string for normal dispatches.
    prefix = f"🤖 Autonomous ({autonomous_label}) — " if autonomous_label else ""

    if status == "completed":
        if auto_pass:
            msg = (
                f"{prefix}✅ {agent_name} auto-passed ({elapsed_str}) — "
                f"lint clean + status:ok + recurring kind. Filed."
            )
            if deliverable:
                msg += f"\n\n📂 {_vault_rel(deliverable)}"
            await _send_telegram(chat_id, msg)
        else:
            msg = f"{prefix}📦 {agent_name} done after {elapsed_str}:\n\n{_synthesize(stdout, deliverable)}"
            if deliverable:
                msg += f"\n\n📂 Saved to: {_vault_rel(deliverable)}"
            if draft_status_info:
                sc = draft_status_info.get("category")
                sr = draft_status_info.get("raw")
                if sc == "blocked":
                    msg += f"\n\n⚠️ Draft status: {sr} — agent reports it could not complete. Read the deliverable for the remediation note."
                elif sc == "partial":
                    msg += f"\n\n⚠️ Draft status: {sr} — agent reports partial output."
                elif sc == "missing":
                    msg += "\n\n⚠️ Deliverable frontmatter has no `status:` field (V1 contract — agent file may predate the 2026-06-10 update)."
                elif sc == "no-frontmatter":
                    msg += "\n\n⚠️ Deliverable has no YAML frontmatter (V1 contract — cannot verify semantic status)."
            if lint_result and lint_result.get("should_alert"):
                banned_n = len(lint_result.get("banned", []))
                report = lint_result.get("report_path")
                msg += (
                    f"\n\n⚠️ Lint: {banned_n} banned-vocab occurrence(s). "
                    f"Report: {_vault_rel(report) if report else '(in-memory)'}"
                )
            await _send_telegram(chat_id, msg)
    elif status == "timed_out":
        if partial_path:
            partial_bytes = len(stdout or "")
            await _send_telegram(
                chat_id,
                f"{prefix}⏱️ {agent_name} timed out after {elapsed_str}. Partial "
                f"output captured ({partial_bytes} bytes) — open it and finish "
                f"with Cowork, or retry with a tighter ask.\n\n"
                f"📂 Partial: {partial_path}"
            )
        else:
            await _send_telegram(
                chat_id,
                f"{prefix}⏱️ {agent_name} timed out after {elapsed_str}. No "
                f"partial output was captured (subagent produced nothing before "
                f"the kill). Retry, simplify the ask, or hand it to Cowork."
            )
    else:
        tail = (stderr or stdout or "no output").strip()[-600:]
        await _send_telegram(
            chat_id,
            f"{prefix}⚠️ {agent_name} failed after {elapsed_str} (exit "
            f"{returncode}).\n\n{tail}\n\nRetry, dispatch a different subagent, "
            f"or hand to Cowork."
        )


async def _run_dispatch(d_id, agent_name, prompt, chat_id, expected_minutes,
                        autonomous_label=None):
    """Background task: acquire a slot, spawn the subagent, wait with timeout,
    detect the deliverable, notify Steve. Never raises (logs + notifies).
    autonomous_label, if set, is propagated to _finish_dispatch so the Telegram
    notification gets a `🤖 Autonomous (<label>):` prefix."""
    global _dispatch_semaphore
    if _dispatch_semaphore is None:
        _dispatch_semaphore = asyncio.Semaphore(DISPATCH_MAX_CONCURRENT)
    try:
        async with _dispatch_semaphore:
            started_epoch = _now_epoch()
            await _update_dispatch(d_id, status="running", started_epoch=started_epoch)

            # Debug echo: deterministic plumbing check, no subprocess / no quota.
            if agent_name == DISPATCH_ECHO_AGENT:
                logger.info(f"Dispatch {d_id}: echo short-circuit")
                await _finish_dispatch(
                    d_id, agent_name, chat_id, started_epoch, 0, prompt, "", False,
                    None, autonomous_label,
                )
                return

            _cfg = DISPATCH_AGENTS[agent_name]
            timeout = _cfg["timeout_seconds"]
            # Per-agent extra working dirs (e.g. decision-feeder needs read of the
            # second-brain vault, which lives outside WORKSPACE_DIR). Each extra dir
            # adds another `--add-dir`. Default [] = identical to prior behavior.
            _add_dir_args: list[str] = ["--add-dir", str(WORKSPACE_DIR)]
            for _extra in _cfg.get("extra_add_dirs", []):
                _add_dir_args += ["--add-dir", str(_extra)]
            logger.info(
                f"Dispatch {d_id}: spawning {agent_name} via {CLAUDE_CLI_PATH} "
                f"(timeout {timeout}s, add-dirs={_add_dir_args[1::2]})"
            )
            proc = await asyncio.create_subprocess_exec(
                CLAUDE_CLI_PATH,
                "--print",
                "--agent", agent_name,
                *_add_dir_args,
                "--dangerously-skip-permissions",
                "--", prompt,
                cwd=str(WORKSPACE_DIR),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await _update_dispatch(d_id, pid=proc.pid)
            timed_out = False
            try:
                out_b, err_b = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )
            except asyncio.TimeoutError:
                timed_out = True
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                # Re-draining communicate() after kill() can hang if the pipes are
                # half-consumed — bound it and reap the process so the task can't
                # block forever and the child can't linger as a zombie (M3).
                try:
                    out_b, err_b = await asyncio.wait_for(
                        proc.communicate(), timeout=5
                    )
                except Exception:
                    out_b, err_b = b"", b""
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=5)
                    except Exception:
                        pass
            out = (out_b or b"").decode("utf-8", "replace").strip()
            err = (err_b or b"").decode("utf-8", "replace").strip()
            partial_path = None
            if timed_out:
                partial_path = _save_partial_dispatch_output(
                    d_id, agent_name, prompt, started_epoch, timeout, out, err,
                )
            await _finish_dispatch(
                d_id, agent_name, chat_id, started_epoch,
                proc.returncode, out, err, timed_out, partial_path,
                autonomous_label,
            )
    except Exception as exc:
        logger.exception(f"Dispatch {d_id} crashed: {exc}")
        await _update_dispatch(
            d_id, status="failed", error=f"dispatcher-exception: {exc}",
            completed_at=datetime.now().isoformat(),
        )
        await _send_telegram(chat_id, f"⚠️ Dispatch to {agent_name} crashed: {exc}")


async def _start_dispatch(agent_name, prompt, chat_id, expected, *,
                          autonomous_label=None) -> dict:
    """Insert the row, spawn the background task, return a status payload.
    Shared by the model-facing MCP tool, the /agent debug command, and the
    APScheduler-driven autonomous dispatches. autonomous_label is set only by
    scheduled fires; it propagates to the Telegram notification as a
    `🤖 Autonomous (<label>):` prefix so Steve can tell scheduled runs apart."""
    d_id = uuid.uuid4().hex[:12]
    chat_id_str = str(chat_id) if chat_id else None
    await _db_call(
        _insert_dispatch_sync, d_id, agent_name, prompt, chat_id_str,
        expected, _now_epoch(),
    )
    active = await _db_call(_count_active_dispatches_sync)
    queued = active > DISPATCH_MAX_CONCURRENT
    # Fire-and-forget: the loop stays unblocked; the task captures the current
    # context (incl. chat_id) at creation.
    asyncio.create_task(
        _run_dispatch(d_id, agent_name, prompt, chat_id_str, expected, autonomous_label)
    )
    return {"dispatch_id": d_id, "queued": queued, "expected_turnaround_minutes": expected}


async def _fire_autonomous_dispatch(entry: dict, chat_id: int) -> None:
    """APScheduler callable. Fires one entry from AUTONOMOUS_DISPATCHES through
    the same _start_dispatch pipeline used by /agent and dispatch_subagent. The
    autonomous_label propagates → Telegram message gets a `🤖 Autonomous:` prefix
    so the user knows it wasn't requested. Never raises (logs + skips)."""
    try:
        cfg = DISPATCH_AGENTS.get(entry["agent_name"])
        if cfg is None:
            logger.warning(
                f"Autonomous dispatch '{entry['name']}' skipped: agent "
                f"'{entry['agent_name']}' is not in DISPATCH_AGENTS."
            )
            return
        expected_min = cfg["timeout_seconds"] // 60
        result = await _start_dispatch(
            entry["agent_name"],
            entry["prompt"],
            chat_id,
            expected_min,
            autonomous_label=entry["name"],
        )
        logger.info(
            f"Autonomous dispatch '{entry['name']}' fired: dispatch_id="
            f"{result['dispatch_id']}, queued={result['queued']}, "
            f"expected={expected_min}m."
        )
    except Exception as exc:
        logger.exception(f"Autonomous dispatch '{entry['name']}' crashed: {exc}")


# === Pipeline orchestrator bridge (2026-06-18) ===
# The /pipeline Telegram command shells the standalone deterministic sequencer
# scripts/pipeline_orchestrator.py. It is a FIXED-SHAPE, hardcoded-action handler
# (advance | status | spend-ok), NOT a DISPATCH_AGENTS entry — the cloud model can
# never reach it (same security posture as /agent). The orchestrator self-reports
# to Telegram via notify.sh; this helper additionally relays its one-line stdout
# back into the chat that triggered it. Backgrounded so a long stage run (an agent
# dispatch can take ~20 min) never blocks the daemon loop.
PIPELINE_SCRIPT = PROJECT_DIR / "scripts" / "pipeline_orchestrator.py"
_PIPELINE_FLAGS = {"advance": "--advance", "status": "--status", "spend-ok": "--spend-ok"}


async def _run_pipeline_command(video: int, action: str, chat_id) -> None:
    """Run the orchestrator subprocess for ONE fixed action and relay stdout.
    `action` is one of _PIPELINE_FLAGS (validated by the caller); no free-form
    input ever reaches the shell. Never raises (logs + notifies on crash)."""
    flag = _PIPELINE_FLAGS[action]
    cmd = [sys.executable, str(PIPELINE_SCRIPT), "--video", str(video), flag]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=str(PROJECT_DIR),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out_b, err_b = await proc.communicate()
        out = (out_b or b"").decode(errors="replace").strip()
        err = (err_b or b"").decode(errors="replace").strip()
        if proc.returncode == 0 and out:
            await _send_telegram(chat_id, out[:3500])
        elif out or err:
            await _send_telegram(
                chat_id,
                f"/pipeline {video} {action} (rc={proc.returncode}):\n{(out or err)[:3000]}",
            )
        else:
            await _send_telegram(
                chat_id,
                f"/pipeline {video} {action} finished (rc={proc.returncode}, no output).",
            )
    except Exception as exc:
        logger.exception(f"/pipeline {video} {action} crashed: {exc}")
        await _send_telegram(chat_id, f"⚠️ /pipeline {video} {action} crashed: {exc}")


# Fleet flags carry NO --video — they sweep every Video_NN_pipeline.json. Both
# route through the orchestrator's gated select_next, so --advance-all physically
# cannot cross a gate / spend / publish / record VO any more than a per-video
# advance can (the structural invariant lives in select_next). --supervise spawns
# no agents at all (read-only). Same fixed-shape, non-model-routable posture.
_PIPELINE_FLEET_FLAGS = {"advance": "--advance-all", "status": "--supervise"}


async def _run_pipeline_fleet_command(action: str, chat_id) -> None:
    """Run the orchestrator subprocess for ONE fixed fleet action (advance-all |
    supervise) and relay stdout. `action` is validated by the caller. Never raises."""
    flag = _PIPELINE_FLEET_FLAGS[action]
    cmd = [sys.executable, str(PIPELINE_SCRIPT), flag]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=str(PROJECT_DIR),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out_b, err_b = await proc.communicate()
        out = (out_b or b"").decode(errors="replace").strip()
        err = (err_b or b"").decode(errors="replace").strip()
        if proc.returncode == 0 and out:
            await _send_telegram(chat_id, out[:3500])
        elif out or err:
            await _send_telegram(
                chat_id,
                f"/pipeline all {action} (rc={proc.returncode}):\n{(out or err)[:3000]}",
            )
        else:
            await _send_telegram(
                chat_id,
                f"/pipeline all {action} finished (rc={proc.returncode}, no output).",
            )
    except Exception as exc:
        logger.exception(f"/pipeline all {action} crashed: {exc}")
        await _send_telegram(chat_id, f"⚠️ /pipeline all {action} crashed: {exc}")


async def _reconcile_orphaned_dispatches():
    """On boot, any dispatch left pending/running was orphaned when the prior
    daemon stopped (its subprocess was a child of that process and is gone).
    Mark them failed + notify, then flush queued notifications."""
    try:
        for status in ("running", "pending"):
            for row in await _db_call(_list_dispatches_by_status_sync, status):
                await _update_dispatch(
                    row["id"], status="failed", error="daemon-restart-orphaned",
                    completed_at=datetime.now().isoformat(),
                )
                if row.get("chat_id"):
                    await _send_telegram(
                        row["chat_id"],
                        f"⚠️ Dispatch to {row['agent_name']} was interrupted by a "
                        f"daemon restart and did not finish. Re-send if still needed."
                    )
        for row in await _db_call(_pop_pending_notifications_sync):
            await _send_telegram(row["chat_id"], row["text"])
    except Exception as exc:
        logger.warning(f"Dispatch reconciliation failed: {exc}")


# --- In-process MCP tool exposed to the cloud-tier model ---

@tool(
    "dispatch_subagent",
    "Spawn a specialist subagent to do scoped work (research, scriptwriting, "
    "thumbnail brief, sponsor outreach). Returns a dispatch_id immediately; the "
    "deliverable is sent to Steve via Telegram when the subagent finishes. Use "
    "for substantive work that takes minutes, not for quick answers.",
    {
        "agent_name": str,
        "prompt": str,
        "expected_turnaround_minutes": int,
    },
)
async def _dispatch_subagent_tool(args):
    agent_name = (args.get("agent_name") or "").strip()
    prompt = (args.get("prompt") or "").strip()
    expected = args.get("expected_turnaround_minutes")

    if agent_name not in DISPATCH_ALLOWED_AGENTS:
        return {
            "content": [{"type": "text", "text": (
                f"Error: '{agent_name}' is not an allowed subagent. "
                f"Choose one of: {', '.join(DISPATCH_ALLOWED_AGENTS)}."
            )}],
            "is_error": True,
        }
    if not prompt:
        return {
            "content": [{"type": "text", "text":
                "Error: prompt is required and must be non-empty."}],
            "is_error": True,
        }

    default_to = DISPATCH_AGENTS[agent_name]["timeout_seconds"] // 60
    if not isinstance(expected, int) or expected <= 0:
        expected = default_to

    chat_id = _current_dispatch_chat_id.get()
    result = await _start_dispatch(agent_name, prompt, chat_id, expected)
    payload = {
        "dispatch_id": result["dispatch_id"],
        "agent_name": agent_name,
        "status": "queued" if result["queued"] else "dispatched",
        "expected_turnaround_minutes": expected,
        "note": (
            "All dispatch slots busy; this starts when one frees up."
            if result["queued"]
            else "Running in the background. Tell Steve you'll ping him when it's done."
        ),
    }
    return {"content": [{"type": "text", "text": json.dumps(payload)}]}


_DISPATCHER_MCP = create_sdk_mcp_server(
    name="dispatcher",
    version="0.7.0",
    tools=[_dispatch_subagent_tool],
)


# ============================================================
# Phase 3 — Scheduled morning briefing
# ============================================================

MORNING_BRIEFING_PROMPT = (
    "Generate Steve's morning briefing as Iris. Use the workspace context "
    "loaded into your system prompt (INBOX, DAILY_BRIEFING, recent decisions, "
    "recent sessions, conversation history).\n\n"
    "BEFORE DRAFTING, do these two tool checks (one call each, then stop):\n"
    "- **Gmail (mcp__google-workspace):** check the studio@3skinternational.com "
    "  inbox for emails received in the last ~24 hours. Surface ONLY items that "
    "  change Steve's day — e.g., the Northwest WY LLC acknowledgment landing, "
    "  Mercury/IRS/NJ DORES correspondence, sponsor/vendor replies, or a flagged "
    "  security alert. Do NOT list every promotional or notification email. If "
    "  nothing new is meaningful, briefly say 'inbox quiet overnight' and move "
    "  on. Skip the call entirely if it would error (e.g., no MCP available).\n"
    "- **Calendar (mcp__google-workspace):** check today's calendar events. "
    "  Mention them in the brief only if there's something actionable (a call, "
    "  a deadline). If today's calendar is empty, omit the calendar mention.\n\n"
    "Use these tools sparingly — one call each, both read-only. Drafts and sends "
    "are out of scope for the brief.\n\n"
    "Format the briefing as concise mobile-friendly plain prose:\n"
    "1. One opening sentence on overall situation (use the current date)\n"
    "2. Inbox highlight (one line, only if something materially changed; "
    "   otherwise omit this section entirely)\n"
    "3. Today's calendar (one line, only if non-empty and actionable; otherwise omit)\n"
    "4. 1-3 top items active this week (from INBOX)\n"
    "5. Any pending external triggers (Northwest email, EIN application, etc.) firing soon\n"
    "6. One sentence on yesterday's tier usage if relevant (cite Iris usage stats)\n"
    "7. One recommended next move\n"
    "8. Sign off naturally with: — Iris\n\n"
    "Keep total length under 1800 chars (slightly higher cap to accommodate the "
    "inbox/calendar additions, but stay tight). No headers, no bullet lists unless "
    "essential. Lead with the situation, do not preamble.\n\n"
    "IMPORTANT formatting rules:\n"
    "- Do NOT auto-link filenames as markdown URLs (do not produce things like "
    "[iris.py](https://iris.py/) — just write the filename as code-fenced text "
    "like `iris.py` or plain text iris.py). Filenames are not websites.\n"
    "- Do NOT add markdown links to any text. Plain prose only.\n"
    "- Bold/italic emphasis is fine but use sparingly."
)


DAILY_BRIEFING_REGEN_PROMPT = (
    "You are Iris regenerating Steve's DAILY_BRIEFING.md for today. Use the "
    "workspace context loaded into your system prompt (INBOX, the PRIOR "
    "DAILY_BRIEFING.md from yesterday, recent decisions, recent sessions, "
    "conversation history).\n\n"
    "Output the COMPLETE markdown file content — no preamble, no code fences, "
    "no closing remarks. The output goes directly to disk as DAILY_BRIEFING.md. "
    "Target 80-120 lines (tight enough to generate in under 90 seconds, substantive "
    "enough to be a useful reference).\n\n"
    "Required structure (omit a section if there is genuinely nothing to put in it):\n\n"
    "# Daily Briefing — YYYY-MM-DD (Day name, optional context)\n\n"
    "_Auto-generated by Iris daemon scheduled job._\n\n"
    "## Today's Focus\n\n"
    "[1-2 short paragraphs on what matters most today. Pull from INBOX critical-path items.]\n\n"
    "## What Changed Since Yesterday\n\n"
    "[Recent shipped work and locked decisions. Be specific with dates/amounts.]\n\n"
    "## Financial Snapshot\n\n"
    "[Small markdown table with current spend metrics: YTD spend, recent receipts, next expected costs, reimbursement queue.]\n\n"
    "## Must-Dos (physical actions, sequenced)\n\n"
    "[Numbered list with 🧍 markers for human-required items. Today first, then upcoming.]\n\n"
    "## Flags & Attention Items\n\n"
    "[Bullet list of active risks, in-flight items.]\n\n"
    "## What Changed in the Library (last 24-48 hours)\n\n"
    "[Bullet list of files updated, decisions filed, sessions logged.]\n\n"
    "## Iris's One Recommendation\n\n"
    "[Single direct recommendation in 2-3 sentences.]\n\n"
    "— Iris\n\n"
    "---\n\n"
    "_This briefing regenerates automatically each morning. Edit by hand if needed — next scheduled run will replace it._\n\n"
    "BE SUBSTANTIVE BUT TIGHT. 80-120 lines target. Real numbers (spend, dates, amounts) — no generic placeholders. Same formatting rules apply: do NOT auto-link filenames as markdown URLs."
)
DAILY_BRIEFING_REGEN_TIMEOUT_SECONDS = 180.0


async def generate_morning_briefing() -> str:
    """Construct the morning brief using cloud tier with workspace context.

    Reuses query_cloud which already loads INBOX/DAILY_BRIEFING/etc. into
    the system prompt. No conversation history (briefing is a standalone
    output, not a continuation).
    """
    return await query_cloud(MORNING_BRIEFING_PROMPT, history=[])


def _preserve_evening_addendum() -> str | None:
    """Brain-audit fix #9 (2026-06-11): before DAILY_BRIEFING.md is overwritten
    by the morning regen, extract any `## 🌆 Evening addendum` section and
    append it to yesterday's daily note. Returns a short status line for the
    log, or None if nothing to preserve. Best-effort — never raises."""
    try:
        if not DAILY_BRIEFING_FILE.exists():
            return None
        text = DAILY_BRIEFING_FILE.read_text(encoding="utf-8", errors="replace")
        # Match "## 🌆 Evening addendum ..." up to the next "## " heading or EOF
        m = re.search(
            r"^##\s+🌆\s+Evening addendum.*?(?=^##\s|\Z)",
            text,
            re.MULTILINE | re.DOTALL,
        )
        if not m:
            return None
        addendum = m.group(0).rstrip() + "\n"
        # Yesterday's daily note
        yesterday = (datetime.now().date() - timedelta(days=1)).isoformat()
        daily_path = WORKSPACE_DIR / "_Iris_Memory" / "Daily" / f"{yesterday}.md"
        if not daily_path.exists():
            logger.warning(
                f"Evening addendum found in DAILY_BRIEFING but yesterday's daily note "
                f"({daily_path.name}) does not exist — skipping preservation."
            )
            return None
        header = (
            f"\n\n## 🌆 Evening addendum (carried from DAILY_BRIEFING on regen "
            f"{datetime.now().strftime('%Y-%m-%d %H:%M')})\n\n"
        )
        with daily_path.open("a", encoding="utf-8") as f:
            f.write(header + addendum)
        return f"preserved evening addendum to {daily_path.name} ({len(addendum)} chars)"
    except Exception as exc:
        logger.exception(f"_preserve_evening_addendum failed (non-fatal): {exc}")
        return None


def _archive_daily_briefing(timestamp: str) -> Path:
    """Move the existing DAILY_BRIEFING.md into the archive dir as a timestamped
    .bak. Returns the backup path. Keeps the .bak out of the vault root."""
    DAILY_BRIEFING_BAK_DIR.mkdir(parents=True, exist_ok=True)
    backup_path = DAILY_BRIEFING_BAK_DIR / f"DAILY_BRIEFING.md.bak-{timestamp}"
    DAILY_BRIEFING_FILE.rename(backup_path)
    return backup_path


def _prune_daily_briefing_baks() -> None:
    """Keep only the most recent DAILY_BRIEFING_BAK_RETENTION backups so the
    archive doesn't grow unbounded (matches the db-backup retention pattern)."""
    try:
        baks = sorted(
            DAILY_BRIEFING_BAK_DIR.glob("DAILY_BRIEFING.md.bak-*"),
            key=lambda p: p.name,
        )
        for stale in baks[:-DAILY_BRIEFING_BAK_RETENTION]:
            stale.unlink()
    except Exception as exc:
        logger.warning(f"Pruning daily-briefing baks failed (non-fatal): {exc}")


async def regenerate_daily_briefing_file() -> bool:
    """Regenerate DAILY_BRIEFING.md from current workspace state.

    Backs up the previous file as .bak-<timestamp> before overwriting.
    Returns True on success, False on any error (caller continues with
    whatever DAILY_BRIEFING content already exists on disk).
    """
    try:
        logger.info(
            f"Regenerating DAILY_BRIEFING.md from current workspace state "
            f"(timeout={DAILY_BRIEFING_REGEN_TIMEOUT_SECONDS}s)..."
        )
        new_content = await query_cloud(
            DAILY_BRIEFING_REGEN_PROMPT,
            history=[],
            timeout=DAILY_BRIEFING_REGEN_TIMEOUT_SECONDS,
        )
        if not new_content or len(new_content) < 500:
            logger.warning(
                f"DAILY_BRIEFING regen returned suspiciously short content ({len(new_content)} chars); "
                "skipping save to preserve the existing file."
            )
            return False
        # Brain-audit fix #9: rescue any evening addendum BEFORE we rename/overwrite.
        preserved = _preserve_evening_addendum()
        if preserved:
            logger.info(preserved)
        # Back up existing file with timestamp into the archive dir (not root).
        if DAILY_BRIEFING_FILE.exists():
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = _archive_daily_briefing(timestamp)
            logger.info(
                f"Previous DAILY_BRIEFING.md backed up to "
                f"{backup_path.relative_to(WORKSPACE_DIR)}"
            )
            _prune_daily_briefing_baks()
        DAILY_BRIEFING_FILE.write_text(new_content, encoding="utf-8")
        logger.info(f"DAILY_BRIEFING.md regenerated ({len(new_content)} chars)")
        await record_message_stat("cloud", cost_usd=0.0)
        return True
    except Exception as exc:
        logger.exception(f"DAILY_BRIEFING regen failed: {exc}")
        return False


# ============================================================
# Telegram media — shared send core + outbox queue + path guard
# ============================================================

def _is_url(s: str) -> bool:
    low = s.lower()
    return low.startswith("http://") or low.startswith("https://")


def _safe_vault_path(src: str) -> str:
    """Containment guard for UNTRUSTED senders (the cloud chat tool). Allows any
    http(s) URL (Telegram fetches it) or a local path that resolves INSIDE
    WORKSPACE_DIR — anything else raises PermissionError so the model can't
    exfiltrate arbitrary disk paths (e.g. /etc/passwd). The internal helper and
    the CLI are trusted local callers and bypass this guard."""
    if _is_url(src):
        return src
    rp = Path(src).resolve()
    if not rp.is_relative_to(WORKSPACE_DIR.resolve()):
        raise PermissionError(f"refused path outside vault: {src}")
    return str(rp)


async def send_media_to_chat(bot, chat_id: int, src: str, caption: str | None = None,
                             as_document: bool = False) -> None:
    """Send an image or file to a Telegram chat. `src` is an absolute local path
    or an http(s) URL. Images go as a photo unless oversize or as_document=True;
    everything else goes as a document. Raises on missing file / oversize doc."""
    cap = (caption or "")[:TELEGRAM_CAPTION_MAX] or None
    if _is_url(src):
        ext = Path(src.split("?")[0]).suffix.lower()
        if not as_document and ext in IMAGE_EXTS:
            await bot.send_photo(chat_id=chat_id, photo=src, caption=cap)
        else:
            await bot.send_document(chat_id=chat_id, document=src, caption=cap)
        return
    p = Path(src)
    if not p.is_file():
        raise FileNotFoundError(f"send_media_to_chat: not a file: {src}")
    size = p.stat().st_size
    is_img = p.suffix.lower() in IMAGE_EXTS
    send_as_photo = is_img and not as_document and size <= PHOTO_MAX_BYTES
    with p.open("rb") as fh:
        if send_as_photo:
            await bot.send_photo(chat_id=chat_id, photo=fh, caption=cap)
        else:
            if size > DOC_MAX_BYTES:
                raise ValueError(
                    f"File too large for Telegram ({size} bytes > {DOC_MAX_BYTES}): {src}"
                )
            await bot.send_document(chat_id=chat_id, document=fh, caption=cap,
                                    filename=p.name)


async def send_photo_to_steve(bot, src: str, caption: str | None = None) -> None:
    """Internal helper (trusted caller): push an image to Steve's private chat.
    In a private chat chat_id == user_id, so the first allowlisted id is the
    target — same trick _post_init uses for the briefing destination."""
    await send_media_to_chat(bot, next(iter(ALLOWED_USER_IDS)), src, caption,
                             as_document=False)


async def send_file_to_steve(bot, src: str, caption: str | None = None) -> None:
    """Internal helper (trusted caller): push a file (as a document) to Steve."""
    await send_media_to_chat(bot, next(iter(ALLOWED_USER_IDS)), src, caption,
                             as_document=True)


async def drain_outbox(bot) -> None:
    """APScheduler interval job: send every queued JSON job in OUTBOX_DIR, then
    delete it. Each job = {"src": ..., "caption": ..., "as_document": bool}.
    A failed job moves to OUTBOX_DIR/.failed/ with the exception logged — the
    loop NEVER raises (a crash here would kill the scheduler thread). Jobs come
    from trusted local scripts, so they bypass the vault-containment guard."""
    if not ALLOWED_USER_IDS:
        return
    target = next(iter(ALLOWED_USER_IDS))
    for job in sorted(OUTBOX_DIR.glob("*.json")):
        try:
            spec = json.loads(job.read_text(encoding="utf-8"))
            await send_media_to_chat(bot, target, spec["src"],
                                     spec.get("caption"),
                                     bool(spec.get("as_document", False)))
            job.unlink()
            logger.info(f"outbox: sent + cleared {job.name} (src={spec.get('src')})")
        except Exception as exc:
            logger.error(f"outbox job {job.name} failed: {exc}")
            try:
                failed = OUTBOX_DIR / ".failed"
                failed.mkdir(exist_ok=True)
                job.rename(failed / job.name)
            except Exception as move_exc:
                logger.error(f"outbox: could not quarantine {job.name}: {move_exc}")


def _extract_send_file_directives(text: str) -> tuple[str, list[str]]:
    """Pull every `[[SEND_FILE: <path|url>]]` sentinel out of a model reply.
    Returns (cleaned_text, [sources]). Cleaned text has the sentinels removed
    and collapsed blank lines tidied so the prose reads naturally."""
    # Cheap substring guard: skip the regex entirely for the ~always case where
    # the reply has no sentinel (also avoids any regex work on huge replies).
    if "SEND_FILE" not in text.upper():
        return text, []
    sources = [m.group(1).strip() for m in _SEND_FILE_RE.finditer(text)]
    if not sources:
        return text, []
    cleaned = _SEND_FILE_RE.sub("", text)
    # Tidy: collapse 3+ newlines left behind, trim trailing whitespace per line.
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, sources


async def send_morning_briefing(bot, chat_id: int) -> None:
    """Daily scheduled job: regenerate DAILY_BRIEFING.md from current state, then generate + send the Telegram brief."""
    logger.info(f"Morning briefing job firing for chat_id={chat_id}")
    try:
        # Step 1: regenerate DAILY_BRIEFING.md from current workspace state.
        # If this fails the brief still goes out using the existing (possibly stale)
        # DAILY_BRIEFING.md, so users always get SOMETHING.
        regen_ok = await regenerate_daily_briefing_file()
        if regen_ok:
            logger.info("DAILY_BRIEFING.md is fresh; generating Telegram brief from it.")
        else:
            logger.warning("DAILY_BRIEFING regen failed; brief will use existing file.")

        # Step 2: generate the Telegram brief. query_cloud reads the (now fresh) DAILY_BRIEFING.md.
        brief = await generate_morning_briefing()
        for i in range(0, len(brief), TELEGRAM_MAX_MSG):
            await bot.send_message(chat_id=chat_id, text=brief[i : i + TELEGRAM_MAX_MSG])
        await record_message_stat("cloud", cost_usd=0.0)
        logger.info(f"Morning briefing sent ({len(brief)} chars)")
    except Exception as exc:
        logger.exception(f"Morning briefing failed: {exc}")
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=f"Morning briefing failed: {exc}. Check daemon logs.",
            )
        except Exception:
            pass


async def _post_init(application) -> None:
    """Start the apscheduler once the bot is ready (called by Application post_init hook)."""
    global _scheduler, _telegram_bot, _dispatch_semaphore
    # Phase 5: expose the bot to the dispatcher so background subagent tasks can
    # deliver results, create the concurrency semaphore on this loop, and
    # reconcile any dispatches orphaned by the previous daemon stop.
    _telegram_bot = application.bot
    if _dispatch_semaphore is None:
        _dispatch_semaphore = asyncio.Semaphore(DISPATCH_MAX_CONCURRENT)
    # Telegram media outbox: ensure the local file queue dir exists so external
    # scripts (scripts/tg_send.sh) can drop jobs even before the first drain.
    try:
        OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        logger.warning(f"Could not create outbox dir {OUTBOX_DIR}: {exc}")
    await _reconcile_orphaned_dispatches()
    if not ALLOWED_USER_IDS:
        logger.warning(
            "Scheduler not starting — no authorized users. Morning briefing disabled."
        )
        return
    # In private chats, chat_id == user_id. ALLOWED_USER_IDS is keyed by
    # Telegram user IDs; use the first authorized user as the briefing target.
    target_chat_id = next(iter(ALLOWED_USER_IDS))
    # APScheduler defaults: tolerate up to 6 hours of missed-fire window (Mac
    # sleep / App Nap / kernel-recovery wake delay) and coalesce duplicate
    # missed runs into one. Without these, the default misfire_grace_time=1s
    # silently SKIPS jobs the instant the scheduler resumes — which is what
    # cost us the 2026-06-01 03:00 market-researcher-monthly + 05:30
    # project-manager-weekly + 06:00 morning-brief fires (APScheduler logged
    # "missed by 7:54:35" warnings then advanced next_run, never executing).
    _scheduler = AsyncIOScheduler(
        timezone=TIMEZONE,
        job_defaults={"misfire_grace_time": 21600, "coalesce": True},
    )
    _scheduler.add_job(
        send_morning_briefing,
        CronTrigger(
            hour=MORNING_BRIEFING_HOUR,
            minute=MORNING_BRIEFING_MINUTE,
            timezone=TIMEZONE,
        ),
        kwargs={"bot": application.bot, "chat_id": target_chat_id},
        id="morning_briefing",
        replace_existing=True,
        # Morning brief is time-sensitive (it's Steve's wake-up artifact); a
        # 2-hour grace is the right tradeoff — brief at 08:00 is still useful,
        # but a brief at noon would clobber the morning-fresh framing.
        misfire_grace_time=7200,
    )
    # Phase 5 (P5-12) — daily expense-categorizer sweep at 09:00 ET. Fires
    # the subagent in scheduled mode; the deliverable notification flows via
    # the standard dispatch path; Steve replies `/approve <run-id>` to fill
    # the draft's Paste-ready CSV block and mark msg_ids approved.
    _scheduler.add_job(
        fire_expense_categorizer_sweep,
        CronTrigger(
            day_of_week=EXPENSE_CATEGORIZER_DAY_OF_WEEK,
            hour=EXPENSE_CATEGORIZER_HOUR,
            minute=EXPENSE_CATEGORIZER_MINUTE,
            timezone=TIMEZONE,
        ),
        kwargs={"chat_id": target_chat_id},
        id="expense_categorizer_sweep",
        replace_existing=True,
    )
    # Phase 5 autonomous-dispatch cadences (project-manager daily, market-researcher
    # monthly, ...). Each AUTONOMOUS_DISPATCHES entry becomes one APScheduler job.
    for entry in AUTONOMOUS_DISPATCHES:
        _scheduler.add_job(
            _fire_autonomous_dispatch,
            CronTrigger(timezone=TIMEZONE, **entry["trigger_kwargs"]),
            kwargs={"entry": entry, "chat_id": target_chat_id},
            id=f"autonomous_{entry['name']}",
            replace_existing=True,
        )
    # Telegram media outbox drain — every 10s scan OUTBOX_DIR for queued JSON
    # send-jobs (dropped by scripts/tg_send.sh / any external script) and ship
    # them. drain_outbox never raises, so a bad job can't stall the scheduler.
    _scheduler.add_job(
        drain_outbox,
        IntervalTrigger(seconds=OUTBOX_DRAIN_INTERVAL_SECONDS, timezone=TIMEZONE),
        kwargs={"bot": application.bot},
        id="outbox_drain",
        replace_existing=True,
        # Don't let missed drains pile up into a burst; one catch-up is enough.
        max_instances=1,
        coalesce=True,
    )
    _scheduler.start()
    job = _scheduler.get_job("morning_briefing")
    next_run = job.next_run_time if job else None
    expense_job = _scheduler.get_job("expense_categorizer_sweep")
    expense_next = expense_job.next_run_time if expense_job else None
    logger.info(
        f"Scheduler started. Morning briefing: daily at "
        f"{MORNING_BRIEFING_HOUR:02d}:{MORNING_BRIEFING_MINUTE:02d} {TIMEZONE}. "
        f"Next fire: {next_run.isoformat() if next_run else 'unknown'} "
        f"(target chat_id={target_chat_id})."
    )
    logger.info(
        f"Phase 5 (P5-12) expense-categorizer sweep: weekly "
        f"{EXPENSE_CATEGORIZER_DAY_OF_WEEK} at "
        f"{EXPENSE_CATEGORIZER_HOUR:02d}:{EXPENSE_CATEGORIZER_MINUTE:02d} {TIMEZONE}. "
        f"Next fire: {expense_next.isoformat() if expense_next else 'unknown'}. "
        f"Lookback: {EXPENSE_CATEGORIZER_LOOKBACK_DAYS} days. "
        "Reply `/approve <run-id>` after each draft lands."
    )
    # Log each autonomous-dispatch entry with its next-fire time so boot logs
    # immediately show what cadences are armed.
    for entry in AUTONOMOUS_DISPATCHES:
        a_job = _scheduler.get_job(f"autonomous_{entry['name']}")
        a_next = a_job.next_run_time if a_job else None
        logger.info(
            f"Autonomous dispatch '{entry['name']}' ({entry['agent_name']}): "
            f"trigger={entry['trigger_kwargs']}, next fire: "
            f"{a_next.isoformat() if a_next else 'unknown'}."
        )


# ============================================================
# Router — Tier 1 (local) vs Tier 3 (cloud)
# ============================================================

_CLOUD_REQUIRED_KEYWORDS = frozenset([
    # Business specifics (legal/formation)
    "llc", "wyoming", "newark", "mercury", "northwest", "registered agent",
    "ein", "irs", "dba", "1583", "ups store", "ups mailbox", "pmb",
    # Financial — added 2026-05-25 evening after a "year to date spend" prompt
    # missed routing because none of these were on the list. Without them, any
    # question about money/budget/status routed to local where DAILY_BRIEFING
    # is not in context.
    "tax", "expense", "receipt", "purchase", "payment", "reimburse",
    "spend", "ytd", "year to date", "year-to-date", "budget", "money",
    "cost", "dollar", "$", "reimburse", "saas", "subscription", "recurring",
    "monthly", "annual", "yearly", "cap", "burn",
    # Iris system & files
    "blueprint", "decision", "inbox", "todo", "briefing", "steve_context",
    "pitch", "daemon", "iris", "cowork", "tier", "phase", "x9",
    "session", "memory graph", "obsidian",
    # 3SK Finance brand
    "three character", "character three", "video 1", "video 2", "video 3",
    "video 4", "script", "voiceover", "thumbnail", "channel", "youtube",
    "ebook", "brand bible", "elevenlabs", "scene prompt",
    # Complex tasks
    "summarize", "analyze", "explain", "draft", "write", "review",
    "compare", "research", "design", "implement", "debug",
    # Status / situational awareness — these almost always need INBOX or DAILY_BRIEFING
    "agenda", "status", "this week", "what is up", "whats up", "what is going on",
    "what should i", "what do i need", "next step", "next move",
    # Steve-specific identifiers
    "3skinternational", "3skfinance", "studio@", "@iris_studio_ai_bot",
    "3sk",
])

_FORCE_PREFIXES = ("/cloud ", "/local ", "/tier2 ", "!cloud ", "!local ", "!tier2 ")

# Hybrid router v2 — fast deterministic rules first; Haiku classifier only as
# a tiebreaker for genuinely ambiguous messages. Keeps cloud and greeting
# messages fast while letting borderline cases get smart routing.

ROUTER_CLASSIFIER_MODEL = "claude-haiku-4-5-20251001"
ROUTER_CLASSIFIER_TIMEOUT_SECONDS = 10.0

# Patterns for messages that are unambiguously local-appropriate — short,
# generic, no business context needed. These bypass the classifier.
_OBVIOUS_LOCAL_PATTERNS = [
    # Greetings and acknowledgments
    re.compile(
        r"^(hi|hey|hello|yo|sup|hiya|howdy|good morning|good night|good evening|good afternoon|thanks|thank you|ok|okay|yes|no|sure|cool|nice|wow|huh|lol|haha|nope|yep|yeah|right|exactly|true|maybe|perhaps|got it|understood|copy that|roger|aight|word)[\s.!?,]*$",
        re.IGNORECASE,
    ),
    # Simple arithmetic ("what is 2 plus 2", "what is 5 + 7")
    re.compile(
        r"^what (is|are|was|were|equals?)\s+[\d\+\-\*/\(\)\.x\s]+\??\s*$",
        re.IGNORECASE,
    ),
    # Time and date questions (date itself is already in the system prompt)
    re.compile(
        r"^(what time is it|what day is it|whats the date|what's the date|what is the date|what is today|whats today|what's today)\??\s*$",
        re.IGNORECASE,
    ),
    # Generic chitchat
    re.compile(
        r"^(how are you|hows it going|how's it going|what's up|whats up|how have you been|how was your day)\??\s*$",
        re.IGNORECASE,
    ),
]


def is_obvious_local(prompt: str) -> bool:
    """Return True if the message is clearly local-appropriate (greeting, math, time, chitchat)."""
    p = prompt.strip()
    return any(pat.match(p) for pat in _OBVIOUS_LOCAL_PATTERNS)


def _decide_tier_deterministic(prompt: str) -> str | None:
    """Fast deterministic routing rules. Return 'local', 'tier2', 'cloud', or None if ambiguous."""
    p_lower = prompt.lower().strip()
    if p_lower.startswith(("/cloud ", "!cloud ")):
        return "cloud"
    if p_lower.startswith(("/local ", "!local ")):
        return "local"
    if p_lower.startswith(("/tier2 ", "!tier2 ")):
        return "tier2"
    if _detect_quick_capture(prompt):
        return "cloud"
    if len(prompt.split()) > 25 or len(prompt) > 150:
        return "cloud"
    if any(kw in p_lower for kw in _CLOUD_REQUIRED_KEYWORDS):
        return "cloud"
    if prompt.count("?") > 1:
        return "cloud"
    if is_obvious_local(prompt):
        return "local"
    return None  # ambiguous — caller should classify


_CLASSIFIER_SYSTEM_PROMPT = (
    "You are a fast routing classifier for an AI assistant called Iris. "
    "Given a user message, respond with EXACTLY one word: LOCAL or CLOUD.\n\n"
    "Iris has two response tiers:\n"
    "- LOCAL (Llama 3.1 8B on a Mac Mini): handles general knowledge, simple math, "
    "time/date, casual chat. NO access to Steve's 3SK business data, workspace files, "
    "or memory beyond a few turns.\n"
    "- CLOUD (Claude Haiku 4.5): handles anything 3SK-specific (WY LLC, expenses, "
    "Mercury, Iris build, decisions, INBOX, daily briefing, character Three, video "
    "scripts) AND complex tasks (analysis, summarization, drafting, multi-step reasoning).\n\n"
    "Rules:\n"
    "- 3SK business specifics, references to canonical files, requests for status/data → CLOUD\n"
    "- General knowledge, math, greetings, time/date, casual chat → LOCAL\n"
    "- When in doubt → CLOUD (better to over-route to cloud than miss with local)\n\n"
    "Respond with exactly one word: LOCAL or CLOUD. No explanation."
)


async def classify_with_haiku(prompt: str) -> str:
    """Use a fast Haiku call to classify a borderline message as 'local' or 'cloud'.

    Returns 'local' or 'cloud'. On error returns 'cloud' (safe default).
    Times out after ROUTER_CLASSIFIER_TIMEOUT_SECONDS.
    """
    # Trim very long inputs — at this point we know prompt is short enough
    # for the keyword router not to route to cloud, but defensive trim anyway.
    user_input = f"Message to classify:\n\n{prompt[:500]}"

    parts: list[str] = []

    async def _collect():
        options = ClaudeAgentOptions(
            system_prompt=_CLASSIFIER_SYSTEM_PROMPT,
            model=ROUTER_CLASSIFIER_MODEL,
            allowed_tools=[],
        )
        async for msg in query(prompt=user_input, options=options):
            if type(msg).__name__ == "AssistantMessage":
                for block in msg.content:
                    text = getattr(block, "text", None)
                    if text:
                        parts.append(text)

    try:
        await asyncio.wait_for(_collect(), timeout=ROUTER_CLASSIFIER_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        logger.warning(
            f"Router classifier timed out after {ROUTER_CLASSIFIER_TIMEOUT_SECONDS}s; defaulting to cloud."
        )
        return "cloud"
    except Exception as exc:
        logger.warning(f"Router classifier failed: {exc}; defaulting to cloud.")
        return "cloud"

    response = "".join(parts).strip().lower()
    # Robust extraction — model may say "LOCAL" or "LOCAL." or include reasoning despite the system prompt.
    if "local" in response and "cloud" not in response:
        return "local"
    if "cloud" in response and "local" not in response:
        return "cloud"
    # Ambiguous classifier output → safe default cloud.
    logger.warning(
        f"Router classifier returned ambiguous response {response!r}; defaulting to cloud."
    )
    return "cloud"


async def decide_tier(prompt: str) -> str:
    """Hybrid router v2: fast deterministic rules + Haiku classifier for ambiguous cases.

    Most messages return in microseconds via the deterministic path. Only
    genuinely-ambiguous prompts incur the Haiku classifier roundtrip (~2-5 sec).
    """
    decision = _decide_tier_deterministic(prompt)
    if decision is not None:
        return decision
    logger.info(f"Router invoking Haiku classifier for ambiguous: {prompt[:80]!r}")
    classified = await classify_with_haiku(prompt)
    logger.info(f"Router classifier returned: {classified} for {prompt[:80]!r}")
    return classified


def _strip_force_prefix(prompt: str) -> str:
    p_lower = prompt.lower()
    for prefix in _FORCE_PREFIXES:
        if p_lower.startswith(prefix):
            return prompt[len(prefix):].strip()
    return prompt


# ============================================================
# Tier 1 — Local Llama 3.1 8B via MLX
# ============================================================

async def _ensure_local_model() -> None:
    global _local_model, _local_tokenizer, _local_load_lock
    if _local_model is not None:
        return
    if _local_load_lock is None:
        _local_load_lock = asyncio.Lock()
    async with _local_load_lock:
        if _local_model is not None:
            return
        logger.info(
            f"Loading local model {LOCAL_MODEL_PATH} from HF_HOME="
            f"{os.environ.get('HF_HOME', '~/.cache/huggingface')} "
            "(first use; takes ~5-10 sec)..."
        )
        loop = asyncio.get_event_loop()
        m, t = await loop.run_in_executor(None, lambda: mlx_load(LOCAL_MODEL_PATH))
        _local_model = m
        _local_tokenizer = t
        logger.info("Local model loaded and ready for inference.")


def _build_local_messages(prompt: str, history: list[dict]) -> list[dict]:
    """Build the multi-turn messages array for Llama chat template, including history."""
    system_prompt = TIER1_SYSTEM_PROMPT_TEMPLATE.format(date_block=_runtime_date_block())
    messages = [{"role": "system", "content": system_prompt}]
    for h in history:
        if h["role"] in ("user", "assistant") and h["content"]:
            messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": prompt})
    return messages


async def query_local(prompt: str, history: list[dict]) -> str:
    """Generate via local Llama 8B with multi-turn history."""
    if not MLX_AVAILABLE:
        raise RuntimeError(
            f"mlx_lm not installed; local tier disabled. Import error: {_MLX_IMPORT_ERROR}"
        )

    await _ensure_local_model()

    messages = _build_local_messages(prompt, history)
    chat_prompt = _local_tokenizer.apply_chat_template(
        messages, tokenize=False, add_generation_prompt=True
    )

    loop = asyncio.get_event_loop()
    text = await asyncio.wait_for(
        loop.run_in_executor(
            None,
            lambda: mlx_generate(
                _local_model,
                _local_tokenizer,
                prompt=chat_prompt,
                max_tokens=LOCAL_MAX_TOKENS,
                verbose=False,
            ),
        ),
        timeout=LOCAL_TIMEOUT_SECONDS,
    )
    return text.strip() or "(Local model returned no text)"


# ============================================================
# Tier 2 — Local Qwen 2.5 14B via MLX (W3 — 2026-05-27)
# ============================================================

async def _ensure_local_tier2_model() -> None:
    global _local_tier2_model, _local_tier2_tokenizer, _local_tier2_load_lock
    if _local_tier2_model is not None:
        return
    if _local_tier2_load_lock is None:
        _local_tier2_load_lock = asyncio.Lock()
    async with _local_tier2_load_lock:
        if _local_tier2_model is not None:
            return
        logger.info(
            f"Loading Tier 2 model {LOCAL_TIER2_MODEL_PATH} from HF_HOME="
            f"{os.environ.get('HF_HOME', '~/.cache/huggingface')} "
            "(first use; takes ~30-90 sec for 14B model)..."
        )
        loop = asyncio.get_event_loop()
        m, t = await loop.run_in_executor(None, lambda: mlx_load(LOCAL_TIER2_MODEL_PATH))
        _local_tier2_model = m
        _local_tier2_tokenizer = t
        logger.info("Tier 2 model (Qwen 14B) loaded and ready for inference.")


async def query_local_tier2(prompt: str, history: list[dict]) -> str:
    """Generate via local Qwen 14B with multi-turn history. Same TIER1 system
    prompt template (trimmed) — Tier 2 is for fast local inference on medium
    queries; if Iris needs full workspace awareness, escalate to cloud Tier 3."""
    if not MLX_AVAILABLE:
        raise RuntimeError(
            f"mlx_lm not installed; Tier 2 disabled. Import error: {_MLX_IMPORT_ERROR}"
        )

    await _ensure_local_tier2_model()

    # Reuse the Tier 1 messages-builder — same trimmed system prompt fits Qwen 14B too.
    system_prompt = TIER1_SYSTEM_PROMPT_TEMPLATE.format(date_block=_runtime_date_block())
    messages = [{"role": "system", "content": system_prompt}]
    for h in history:
        if h["role"] in ("user", "assistant") and h["content"]:
            messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": prompt})

    chat_prompt = _local_tier2_tokenizer.apply_chat_template(
        messages, tokenize=False, add_generation_prompt=True
    )

    loop = asyncio.get_event_loop()
    text = await asyncio.wait_for(
        loop.run_in_executor(
            None,
            lambda: mlx_generate(
                _local_tier2_model,
                _local_tier2_tokenizer,
                prompt=chat_prompt,
                max_tokens=LOCAL_TIER2_MAX_TOKENS,
                verbose=False,
            ),
        ),
        timeout=LOCAL_TIER2_TIMEOUT_SECONDS,
    )
    return text.strip() or "(Tier 2 model returned no text)"


# ============================================================
# Tier 3 — Claude Agent SDK via Max sub OAuth
# ============================================================

async def query_cloud(prompt: str, history: list[dict], timeout: float | None = None,
                      chat_id: str | None = None) -> str:
    """Call Agent SDK with OAuth auth and history-augmented system prompt.

    timeout: per-call override in seconds. Defaults to QUERY_TIMEOUT_SECONDS
    (60s) for typical chat. Long-output operations (DAILY_BRIEFING regen,
    ebook generation, etc.) should pass a longer timeout to avoid spurious
    timeout failures on multi-thousand-character generations.

    chat_id: when set (interactive Telegram turns), the Phase 5 dispatcher MCP
    is exposed so Iris can delegate to specialist subagents whose deliverables
    are returned to this chat. Briefing/regen calls pass chat_id=None and must
    NOT be able to dispatch.
    """
    effective_timeout = timeout if timeout is not None else QUERY_TIMEOUT_SECONDS
    system_prompt = load_system_prompt_cloud(history)
    # v0.4 (2026-05-26): WebSearch + WebFetch enabled for cloud tier.
    # The behavior prefix tells Iris to use these tools SPARINGLY — only when
    # Steve explicitly asks for research/lookup. Routine chat answers from
    # the loaded prompt context, no tool call.
    mcp_servers = MCP_SERVERS
    allowed_tools = ["WebSearch", "WebFetch", "mcp__obsidian", "mcp__google-workspace"]
    # Phase 5 (P5-2): expose dispatch_subagent ONLY on interactive chat turns.
    if chat_id is not None and _DISPATCHER_MCP is not None:
        mcp_servers = {**MCP_SERVERS, "dispatcher": _DISPATCHER_MCP}
        allowed_tools = allowed_tools + ["mcp__dispatcher"]
        system_prompt = system_prompt + DISPATCHER_MODE_SUFFIX
        _current_dispatch_chat_id.set(str(chat_id))
    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        model=ANTHROPIC_MODEL,
        mcp_servers=mcp_servers,
        allowed_tools=allowed_tools,
    )

    parts: list[str] = []
    result_was_error = False
    result_summary: str | None = None

    async def _collect() -> None:
        nonlocal result_was_error, result_summary
        async for msg in query(prompt=prompt, options=options):
            mtype = type(msg).__name__
            if mtype == "AssistantMessage":
                for block in msg.content:
                    text = getattr(block, "text", None)
                    if text:
                        parts.append(text)
            elif mtype == "ResultMessage":
                if getattr(msg, "is_error", False):
                    result_was_error = True
                    result_summary = repr(msg)
                else:
                    dur = getattr(msg, "duration_ms", None)
                    usage = getattr(msg, "model_usage", None)
                    logger.info(
                        f"Cloud result: ok duration_ms={dur} "
                        f"model_usage_keys={list(usage.keys()) if usage else None}"
                    )

    try:
        await asyncio.wait_for(_collect(), timeout=effective_timeout)
    except asyncio.TimeoutError:
        logger.error(
            f"Cloud query exceeded {effective_timeout}s timeout. "
            f"Captured partial text length: {sum(len(p) for p in parts)}"
        )
        raise RuntimeError(
            "Iris timed out waiting for a response. Try again, or send a "
            "shorter / simpler message."
        )

    if result_was_error:
        logger.warning(f"Cloud result indicated error: {result_summary}")

    return "".join(parts).strip() or "(Iris returned no text)"


# ============================================================
# Orchestrator
# ============================================================

async def ask_iris(prompt: str, chat_id: str, user_id: int) -> tuple[str, str]:
    """Decide tier, fetch history, call model, save both sides of the exchange.

    Returns (response_text, tier_actually_used) where tier is 'local',
    'cloud', or 'cloud_fallback' (local was tried first but failed)."""
    decided_tier = await decide_tier(prompt)
    stripped = _strip_force_prefix(prompt)

    # Save the user message FIRST so it appears in subsequent history queries
    # (handles back-to-back rapid messages correctly).
    await save_message(chat_id, user_id, "user", prompt)

    # Fetch history sized to the chosen tier
    if decided_tier in ("local", "tier2"):
        history = await get_history(chat_id, HISTORY_LIMIT_LOCAL)
        # Drop the just-saved user message from history since we append it
        # explicitly inside query_local / query_local_tier2.
        if history and history[-1]["role"] == "user":
            history = history[:-1]
    else:
        history = await get_history(chat_id, HISTORY_LIMIT_CLOUD)
        # For cloud, we put history in the system prompt and the current
        # message in the user prompt — so drop the duplicate trailing user msg.
        if history and history[-1]["role"] == "user":
            history = history[:-1]

    actual_tier: str

    if decided_tier == "tier2":
        if not MLX_AVAILABLE:
            logger.warning("Router chose tier2 but MLX is unavailable; falling through to cloud.")
            decided_tier = "cloud_fallback"
        else:
            try:
                text = await query_local_tier2(stripped, history)
                actual_tier = "tier2"
                await save_message(chat_id, user_id, "assistant", text, tier=actual_tier)
                await record_message_stat(actual_tier, cost_usd=0.0)
                return text, actual_tier
            except asyncio.TimeoutError:
                logger.warning(
                    f"Tier 2 timed out after {LOCAL_TIER2_TIMEOUT_SECONDS}s; falling back to cloud."
                )
                decided_tier = "cloud_fallback"
            except Exception as exc:
                logger.warning(
                    f"Tier 2 failed, falling back to cloud: {type(exc).__name__}: {exc}"
                )
                decided_tier = "cloud_fallback"
            # Re-fetch with cloud-sized history for the fallback call
            history = await get_history(chat_id, HISTORY_LIMIT_CLOUD)
            if history and history[-1]["role"] == "user":
                history = history[:-1]

    if decided_tier == "local":
        if not MLX_AVAILABLE:
            logger.warning("Router chose local but MLX is unavailable; falling through to cloud.")
            decided_tier = "cloud_fallback"
        else:
            try:
                text = await query_local(stripped, history)
                actual_tier = "local"
                await save_message(chat_id, user_id, "assistant", text, tier=actual_tier)
                await record_message_stat(actual_tier, cost_usd=0.0)
                return text, actual_tier
            except asyncio.TimeoutError:
                logger.warning(
                    f"Local tier timed out after {LOCAL_TIMEOUT_SECONDS}s; falling back to cloud."
                )
                decided_tier = "cloud_fallback"
            except Exception as exc:
                logger.warning(
                    f"Local tier failed, falling back to cloud: {type(exc).__name__}: {exc}"
                )
                decided_tier = "cloud_fallback"
            # Re-fetch with cloud-sized history for the fallback call
            history = await get_history(chat_id, HISTORY_LIMIT_CLOUD)
            if history and history[-1]["role"] == "user":
                history = history[:-1]

    text = await query_cloud(stripped, history, chat_id=chat_id)
    actual_tier = decided_tier if decided_tier == "cloud_fallback" else "cloud"
    await save_message(chat_id, user_id, "assistant", text, tier=actual_tier)
    await record_message_stat(actual_tier, cost_usd=0.0)
    return text, actual_tier


# ============================================================
# Telegram handlers
# ============================================================

async def _file_inbound_media(update: Update, tg_file, suffix: str, kind: str,
                              caption: str) -> tuple[Path, Path]:
    """Download an already-fetched Telegram File into INBOX_MEDIA_DIR and log a
    Quick Capture line so Cowork-Iris files it next session. Capture-only: the
    daemon is read-only on the vault except its own inbound dir. Returns
    (abs_dest, vault_relative_dest). Raises on download / write failure."""
    user = update.effective_user
    username = user.username or user.first_name
    INBOX_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    safe_suffix = suffix if suffix.startswith(".") else (f".{suffix}" if suffix else ".bin")
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    dest = INBOX_MEDIA_DIR / f"{stamp}_{kind}{safe_suffix}"
    if dest.exists():  # same-second collision — keep both
        dest = INBOX_MEDIA_DIR / f"{stamp}_{kind}_{uuid.uuid4().hex[:4]}{safe_suffix}"
    await tg_file.download_to_drive(custom_path=str(dest))
    rel = dest.relative_to(WORKSPACE_DIR)
    cap = (caption or "").strip()
    note = f"{cap} [file: {rel}]".strip() if cap else f"Inbound {kind} [file: {rel}]"
    # Honor a real Quick Capture prefix in the caption (e.g. a DECISION: pdf);
    # otherwise label it MEDIA: so Cowork-Iris routes by the file + note.
    qc_prefix = _detect_quick_capture(cap) or "MEDIA:"
    await save_quick_capture(qc_prefix, note, user.id, username)
    return dest, rel


async def handle_document_message(update: Update,
                                  context: ContextTypes.DEFAULT_TYPE) -> None:
    """Inbound document handler (2026-06-19). Any document Steve sends downloads
    into INBOX_MEDIA_DIR and logs a Quick Capture line. Capture-only — Cowork
    files it properly next session. Fail-closed on the allowlist, like text."""
    user = update.effective_user
    username = user.username or user.first_name
    if user.id not in ALLOWED_USER_IDS:
        logger.warning(f"BLOCKED unauthorized document: user_id={user.id} @{username}")
        return
    doc = update.message.document
    if not doc:
        return
    caption = (update.message.caption or "").strip()
    suffix = Path(doc.file_name or "").suffix or ".bin"
    logger.info(
        f"From @{username} (document): {doc.file_name!r} "
        f"({doc.file_size} bytes), caption={caption[:60]!r}"
    )
    try:
        tg_file = await context.bot.get_file(doc.file_id)
        _, rel = await _file_inbound_media(update, tg_file, suffix, "document", caption)
        await update.message.reply_text(
            f"📎 Saved `{rel.name}` and logged to Quick Capture — "
            f"Cowork-Iris files it properly next session."
        )
    except Exception as exc:
        logger.exception(f"inbound document capture failed: {exc}")
        await update.message.reply_text(
            f"Got the document but couldn't save it: {exc}\n"
            "It's still in this Telegram chat — Cowork-Iris can grab it from there."
        )


async def handle_photo_message(update: Update,
                               context: ContextTypes.DEFAULT_TYPE) -> None:
    """A4 (Redesign Night 4, 2026-06-13). Telegram photo handler — when the
    caption starts with `RECEIPT:`, route the photo through the receipt
    draft pipeline. Non-RECEIPT photo captions get a soft hint reply and
    are otherwise ignored (the daemon doesn't have a general image-routing
    path — photos without RECEIPT: are operator slips, not workflow)."""
    user_id = update.effective_user.id
    username = (update.effective_user.username
                or update.effective_user.first_name)
    if user_id not in ALLOWED_USER_IDS:
        logger.warning(
            f"BLOCKED unauthorized photo: user_id={user_id} @{username}"
        )
        return
    caption = (update.message.caption or "").strip()
    if not caption.upper().startswith("RECEIPT:"):
        # Non-RECEIPT photo: capture it to the inbound dir + Quick Capture
        # (2026-06-19) instead of rejecting. RECEIPT-captioned photos still go
        # through the richer expense-draft pipeline below.
        logger.info(
            f"Non-RECEIPT photo from @{username} → inbound capture "
            f"(caption preview: {caption[:60]!r})"
        )
        try:
            tg_file = await context.bot.get_file(update.message.photo[-1].file_id)
            _, rel = await _file_inbound_media(update, tg_file, ".jpg", "photo", caption)
            await update.message.reply_text(
                f"📸 Saved `{rel.name}` and logged to Quick Capture — "
                f"Cowork-Iris files it next session. "
                f"(Caption a photo `RECEIPT: $7.23 vendor` to auto-draft an expense.)"
            )
        except Exception as exc:
            logger.exception(f"inbound photo capture failed: {exc}")
            await update.message.reply_text(
                f"Got the photo but couldn't save it: {exc}\n"
                "It's still in this Telegram chat."
            )
        return
    logger.info(
        f"From @{username} (photo): RECEIPT capture, caption={caption!r}"
    )
    try:
        await _handle_receipt_photo(update, context, caption)
    except Exception as exc:
        logger.exception(f"A4 receipt-photo handler crashed: {exc}")
        await update.message.reply_text(
            f"Receipt-photo handler hit an error: {exc}\n"
            "Your photo is still in this Telegram chat — Cowork-Iris can "
            "file it from there if needed."
        )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Forward a Telegram message to the router and reply with the response."""
    user_id = update.effective_user.id
    chat_id = str(update.effective_chat.id)
    username = update.effective_user.username or update.effective_user.first_name
    # `.text` is None for non-text updates that still reach this handler (service
    # messages, some edited/forwarded updates) — `None[:80]`/`None.strip()` would
    # raise and drop the message with a false 🔴 alert (M1).
    text = update.message.text or ""

    if user_id not in ALLOWED_USER_IDS:
        logger.warning(
            f"BLOCKED unauthorized message: user_id={user_id}, "
            f"username=@{username}, text={text[:80]!r}"
        )
        return

    if not text.strip():
        return

    logger.info(f"From @{username}: {text}")

    # Built-in slash commands handled here BEFORE model routing.
    # Free, instant, do not invoke the model.
    stripped_text = text.strip()
    if stripped_text.lower() in ("/usage", "!usage"):
        stats = await get_today_stats()
        cap_pct = (stats["tier4_spend_usd"] / DAILY_TIER4_CAP_USD * 100) if DAILY_TIER4_CAP_USD > 0 else 0
        reply = (
            f"Iris usage today ({stats['date']}):\n\n"
            f"Local (Llama 8B, free): {stats['local']}\n"
            f"Cloud (Max sub Haiku 4.5, $0 marginal): {stats['cloud']}\n"
            f"Cloud fallback (after local error): {stats['cloud_fallback']}\n"
            f"Tier 4 (paid API): {stats['tier4']} msgs, "
            f"${stats['tier4_spend_usd']:.2f} / ${DAILY_TIER4_CAP_USD:.2f} cap ({cap_pct:.0f}%)\n"
            f"\nTotal: {stats['total']} messages"
        )
        logger.info(f"To @{username} (slash=/usage): served {stats['total']} total msgs today")
        await update.message.reply_text(reply)
        return

    if stripped_text.lower() in ("/briefing", "!briefing"):
        logger.info(f"From @{username} (slash=/briefing): on-demand brief requested")
        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id, action="typing"
        )
        try:
            brief = await generate_morning_briefing()
            for i in range(0, len(brief), TELEGRAM_MAX_MSG):
                await update.message.reply_text(brief[i : i + TELEGRAM_MAX_MSG])
            await record_message_stat("cloud", cost_usd=0.0)
            logger.info(f"To @{username} (slash=/briefing): served brief ({len(brief)} chars)")
        except Exception as exc:
            logger.exception("On-demand briefing failed")
            await update.message.reply_text(f"Briefing failed: {exc}")
        return

    if stripped_text.lower() in ("/refresh", "!refresh", "/refresh-briefing", "!refresh-briefing"):
        logger.info(f"From @{username} (slash=/refresh): manual DAILY_BRIEFING regen requested")
        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id, action="typing"
        )
        try:
            ok = await regenerate_daily_briefing_file()
            if ok:
                await update.message.reply_text(
                    "DAILY_BRIEFING.md regenerated from current workspace state. "
                    "Send /briefing to see the new content distilled, or open the file on either Mac."
                )
                logger.info(f"To @{username} (slash=/refresh): regen succeeded")
            else:
                await update.message.reply_text(
                    "DAILY_BRIEFING regen failed — see daemon logs. The existing file is preserved."
                )
                logger.warning(f"To @{username} (slash=/refresh): regen failed")
        except Exception as exc:
            logger.exception("Manual DAILY_BRIEFING regen failed")
            await update.message.reply_text(f"Refresh failed: {exc}")
        return

    # Phase 5 (P5-2) — dispatcher debug commands. Bypass the model: spawn a
    # subagent directly (or echo for a plumbing test) and list recent dispatches.
    if stripped_text.lower().startswith(("/agent ", "!agent ")):
        rest = stripped_text[len("/agent "):].strip()
        parts = rest.split(None, 1)
        if len(parts) < 2:
            await update.message.reply_text(
                "Usage: /agent <agent_name> <prompt>\n"
                f"Debug: echo. Real: {', '.join(DISPATCH_ALLOWED_AGENTS)}."
            )
            return
        agent_name, agent_prompt = parts[0], parts[1]
        if agent_name != DISPATCH_ECHO_AGENT and agent_name not in DISPATCH_ALLOWED_AGENTS:
            await update.message.reply_text(
                f"Unknown agent '{agent_name}'. Allowed: {DISPATCH_ECHO_AGENT}, "
                f"{', '.join(DISPATCH_ALLOWED_AGENTS)}."
            )
            return
        expected = (DISPATCH_AGENTS[agent_name]["timeout_seconds"] // 60
                    if agent_name in DISPATCH_AGENTS else 1)
        result = await _start_dispatch(agent_name, agent_prompt, chat_id, expected)
        logger.info(f"From @{username} (/agent {agent_name}): dispatch {result['dispatch_id']}")
        await update.message.reply_text(
            f"Dispatched {agent_name} (id {result['dispatch_id']}). "
            f"I'll send the result here when it's done."
        )
        return

    # Pipeline orchestrator — FIXED-SHAPE, hardcoded-action command. Mirrors
    # /agent's security posture: validate NN is an integer + the action token is
    # one of three literals, then shell pipeline_orchestrator.py via the daemon's
    # subprocess plumbing. No arbitrary shell, no free-form prompt — the cloud
    # model can never reach this surface. `spend-ok` is the ONLY billed path.
    if stripped_text.lower().startswith(("/pipeline ", "!pipeline ")):
        rest = stripped_text.split(None, 1)[1].strip() if len(stripped_text.split(None, 1)) > 1 else ""
        parts = rest.split()
        # Fleet path: "/pipeline all" advances every in-flight video to its next
        # gate; "/pipeline all status" is the read-only supervision sweep. Both
        # are gated through select_next — they can never spend/publish/record VO.
        if parts and parts[0].lower() == "all":
            fleet_action = "advance"
            if len(parts) == 2 and parts[1].lower() == "status":
                fleet_action = "status"
            elif len(parts) >= 2:
                await update.message.reply_text(
                    "Usage:\n"
                    "  /pipeline all          advance every in-flight video to its next gate\n"
                    "  /pipeline all status   read-only fleet supervision sweep"
                )
                return
            logger.info(f"From @{username} (/pipeline all {fleet_action})")
            ack = {
                "advance": "🌙 Advancing every in-flight video to its next gate…",
                "status": "📋 Reading the whole pipeline fleet…",
            }[fleet_action]
            await update.message.reply_text(ack + " I'll send the digest here when it's done.")
            asyncio.create_task(_run_pipeline_fleet_command(fleet_action, chat_id))
            return
        if not parts or not parts[0].isdigit():
            await update.message.reply_text(
                "Usage:\n"
                "  /pipeline <N>            advance video N one stage\n"
                "  /pipeline <N> status     show the pipeline state table\n"
                "  /pipeline <N> spend-ok   authorize the BILLED image batch (stage 5)\n"
                "  /pipeline all            advance every in-flight video to its next gate\n"
                "  /pipeline all status     read-only fleet supervision sweep\n"
                "N must be an integer."
            )
            return
        video = int(parts[0])
        action = "advance"
        if len(parts) == 2:
            tok = parts[1].lower()
            if tok == "status":
                action = "status"
            elif tok == "spend-ok":
                action = "spend-ok"
            else:
                await update.message.reply_text(
                    f"Unknown /pipeline action '{parts[1]}'. "
                    f"Allowed: status, spend-ok (or omit to advance)."
                )
                return
        elif len(parts) > 2:
            await update.message.reply_text(
                "Too many arguments. Use /pipeline <N> [status|spend-ok]."
            )
            return
        logger.info(f"From @{username} (/pipeline {video} {action})")
        ack = {
            "advance": f"🎬 Advancing video {video} one stage…",
            "status": f"📋 Reading video {video} pipeline state…",
            "spend-ok": f"💸 Authorizing the BILLED image batch for video {video}…",
        }[action]
        await update.message.reply_text(ack + " I'll send the result here when it's done.")
        asyncio.create_task(_run_pipeline_command(video, action, chat_id))
        return

    if stripped_text.lower() in ("/dispatches", "!dispatches"):
        rows = await _db_call(_list_recent_dispatches_sync, 10)
        if not rows:
            await update.message.reply_text("No dispatches yet.")
            return
        lines = ["Recent dispatches (newest first):"]
        for r in rows:
            lines.append(
                f"• [{r['status']}] {r['agent_name']} ({r['id']}) → "
                f"{_vault_rel(r.get('deliverable_path'))}"
            )
        await update.message.reply_text("\n".join(lines))
        return

    # Phase 5 (P5-12) — `/approve <run-id>` fills the expense-categorizer
    # draft's Paste-ready CSV block, marks msg_ids approved, marks the run
    # approved. Steve still pastes the CSV into Expense_Tracker.xlsx manually
    # (v1 design — no programmatic xlsx write).
    if stripped_text.lower().startswith(("/approve ", "!approve ")):
        rest = stripped_text[len("/approve "):].strip()
        if not rest:
            await update.message.reply_text(
                "Usage: /approve <run-id>\n"
                "Look up recent run-ids via /dispatches (the expense-categorizer "
                "rows show the dispatch id; the run-id is the 8-char suffix on "
                "the draft filename `02_Finance/Expense_Tracker_Drafts/...`)."
            )
            return
        run_id = rest.split(None, 1)[0]
        logger.info(f"From @{username} (/approve {run_id}): filling Paste-ready CSV")
        await _handle_approve_command(update, chat_id, run_id)
        return

    # ADAPTS loop one-tap gate — `/adapt [list|show <id>|approve <id>|reject <id>]`.
    # adaptation-proposer drafts proposals; Steve reviews + approves here; the
    # deterministic apply core (scripts/adaptation.py) performs the edit. The
    # proposer never edits a file directly — that separation is the safety model.
    if stripped_text.lower() in ("/adapt", "!adapt") or stripped_text.lower().startswith(("/adapt ", "!adapt ")):
        rest = stripped_text[len("/adapt"):].strip()
        parts = rest.split(None, 1)
        subcommand = parts[0].lower() if parts else "list"
        arg = parts[1].strip() if len(parts) > 1 else ""
        logger.info(f"From @{username} (/adapt {subcommand} {arg})")
        await _handle_adapt_command(update, chat_id, subcommand, arg)
        return

    # Second-brain capture (2026-06-16): `BRAIN: <thought>` (or NOTE:/ZK:) lands
    # a friction-free, timestamped note in the personal Zettelkasten's "00 - INBOX/"
    # for the nightly inbox-processor to triage. Dedicated fast-path: instant ack,
    # NO model call, and does not touch the business vault. Checked before the
    # business Quick-Capture prefixes so a second-brain thought can't be shadowed.
    brain_prefix = _detect_brain_capture(text)
    if brain_prefix:
        rel = await save_brain_capture(brain_prefix, text, user_id, username)
        if rel:
            await update.message.reply_text(
                f"🧠 Captured to your second brain → `{rel}`.\n"
                "The nightly inbox-processor will triage + file it."
            )
            logger.info(f"To @{username} (brain-capture): saved {rel}")
        else:
            await update.message.reply_text(
                "🧠 Couldn't write the capture to the second-brain inbox (see daemon logs). "
                "Your message wasn't filed — try again or capture manually."
            )
        return

    # Decision Feeder (2026-06-16): `/decision <decision>` dispatches the
    # decision-feeder subagent, which scans BOTH the business vault AND the
    # personal second brain for every note bearing on the decision and writes a
    # DECISION BRIEF to 06_CEO/Decision_Briefs/. (Also reachable via
    # `/agent decision-feeder <prompt>` and the cloud model's dispatcher.)
    if stripped_text.lower().startswith(("/decision ", "!decision ")):
        query = stripped_text[len("/decision "):].strip()
        if not query:
            await update.message.reply_text(
                "Usage: /decision <the decision to brief>\n"
                "e.g. /decision which ESP to pick\n"
                "I'll scan both vaults and write a decision brief to "
                "06_CEO/Decision_Briefs/, then send the summary here."
            )
            return
        expected = DISPATCH_AGENTS["decision-feeder"]["timeout_seconds"] // 60
        df_prompt = (
            "Mode A — on-demand single decision. Build a DECISION BRIEF for this "
            f"decision: {query}"
        )
        result = await _start_dispatch("decision-feeder", df_prompt, chat_id, expected)
        logger.info(f"From @{username} (/decision): dispatch {result['dispatch_id']} for {query!r}")
        await update.message.reply_text(
            f"🧭 Feeding the decision: \"{query}\". Scanning both vaults "
            f"(dispatch {result['dispatch_id']}); I'll send the brief summary here when it's done."
        )
        return

    # Quick Capture bridge: if message starts with RECEIPT/DECISION/etc.,
    # append the raw text to TELEGRAM_CAPTURE.md. Continue to model for a
    # natural acknowledgement reply. Save is best-effort — a failure here
    # does not block the reply.
    qc_prefix = _detect_quick_capture(text)
    if qc_prefix:
        saved = await save_quick_capture(qc_prefix, text, user_id, username)
        if not saved:
            logger.warning(f"Quick Capture for @{username} prefix={qc_prefix} did not persist; user reply will continue.")
        # A-1: also route markdown-safe prefixes to canonical homes inline.
        # xlsx-bound prefixes (RECEIPT/PURCHASE/PAYMENT/STATEMENT/MILESTONE) return None.
        await route_quick_capture(qc_prefix, text, user_id, username)

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action="typing"
    )

    try:
        response, tier = await ask_iris(text, chat_id=chat_id, user_id=user_id)
    except Exception as exc:
        logger.exception("Iris call failed")
        # OAuth-aware error reply (minimal Pitch #16) — when the cloud path
        # fails with auth-shaped error text, give Steve the recovery command
        # directly so a token expiry does not turn into a silent outage.
        exc_str = str(exc).lower()
        oauth_signals = (
            "oauth", "authenticate", "unauthorized", "401",
            "token expir", "credential", "auth failed", "auth error",
        )
        # A-5: detect sqlite / X9-unmount errors and give Steve the diskutil hint
        sqlite_signals = (
            "no such file", "unable to open database", "disk i/o error",
            "database is locked", "database disk image is malformed",
            "/volumes/ai_workspace",
        )
        if any(s in exc_str for s in oauth_signals):
            err_msg = (
                f"Iris auth/OAuth failure: {exc}\n\n"
                "On the Mac Mini Terminal, run: claude login\n"
                "Then text me again to verify."
            )
        elif any(s in exc_str for s in sqlite_signals):
            err_msg = (
                f"Iris SQLite/disk error: {exc}\n\n"
                "Looks like the X9 SSD may be unmounted. On the Mac Mini Terminal, run:\n"
                "diskutil mount /Volumes/AI_Workspace\n"
                "Then text me again to verify."
            )
        else:
            err_msg = f"Iris hit an error: {exc}"
        await update.message.reply_text(err_msg)
        return

    logger.info(f"To @{username} (tier={tier}): {response[:100]}")

    # Chat-tool file send (2026-06-19): honor any `[[SEND_FILE: <path|url>]]`
    # sentinels the cloud model emitted (taught only on interactive cloud turns
    # via DISPATCHER_MODE_SUFFIX). Only cloud-tier replies are parsed — the local
    # models were never taught the protocol, so skip them entirely. Strip the
    # sentinels from the prose, send the prose, then push each file through the
    # vault-containment guard — the model can only send files from inside
    # WORKSPACE_DIR or http(s) URLs.
    send_srcs: list[str] = []
    if tier.startswith("cloud"):
        response, send_srcs = _extract_send_file_directives(response)

    if response:
        for i in range(0, len(response), TELEGRAM_MAX_MSG):
            await update.message.reply_text(response[i : i + TELEGRAM_MAX_MSG])

    for src in send_srcs:
        try:
            safe = _safe_vault_path(src)
            await send_media_to_chat(context.bot, update.effective_chat.id, safe)
            logger.info(f"chat-tool SEND_FILE delivered: {src}")
        except Exception as exc:
            logger.warning(f"chat-tool SEND_FILE refused/failed for {src!r}: {exc}")
            await update.message.reply_text(f"⚠️ Couldn't send that file ({src}): {exc}")


# ============================================================
# Telegram error handler
# ============================================================

_NOTIFY_SCRIPT = Path(__file__).resolve().parent / "scripts" / "notify.sh"


def _alert_steve(text: str, wait: bool = False) -> None:
    """Push a Telegram alert via scripts/notify.sh. Deliberately decoupled from
    this daemon's own _send_telegram path so it still delivers when the bot /
    event loop is the thing that broke. Best-effort — never raises. Set
    wait=True only on the crash path (where the process is about to exit and a
    fire-and-forget child could be killed before curl finishes)."""
    try:
        if not _NOTIFY_SCRIPT.exists():
            logger.warning("notify.sh not found at %s; cannot alert Steve", _NOTIFY_SCRIPT)
            return
        proc = subprocess.Popen(
            [str(_NOTIFY_SCRIPT), text],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if wait:
            proc.wait(timeout=25)
    except Exception as exc:  # noqa: BLE001 — alerting must never crash the caller
        logger.warning("notify.sh alert failed: %s", exc)


async def _on_telegram_error(
    update: object, context: ContextTypes.DEFAULT_TYPE
) -> None:
    err = context.error
    # Transient network blips from Telegram's Bouncer / upstream — python-telegram-bot's
    # retry loop recovers on its own. Without a handler these surface as ERROR-level
    # stack traces in iris.err.log and trip the pre-brief Pass 3 `error|exception|traceback`
    # grep (Session 24 fix).
    if isinstance(err, (NetworkError, TimedOut, RetryAfter)):
        logger.warning(f"Telegram transient: {type(err).__name__}: {err}")
        return
    logger.exception("Unhandled Telegram error", exc_info=err)
    _alert_steve(
        f"🔴 Iris daemon — unhandled Telegram error: "
        f"{type(err).__name__}: {str(err)[:300]}"
    )


# ============================================================
# Main
# ============================================================

def main() -> None:
    if not ALLOWED_USER_IDS:
        logger.error(
            "ALLOWLIST EMPTY — IRIS_TELEGRAM_USER_IDS is not set in .env. "
            "Iris will SILENTLY IGNORE ALL incoming messages until this is "
            "fixed."
        )
    else:
        logger.info(
            f"Allowlist active: {len(ALLOWED_USER_IDS)} authorized user(s) "
            f"({sorted(ALLOWED_USER_IDS)})"
        )

    auth_hint = (
        "OAuth (Max sub via claude CLI)"
        if ANTHROPIC_API_KEY_FALLBACK is None
        else "OAuth (Max sub via claude CLI); API key available as Tier 4 fallback (currently stripped from env)"
    )
    local_hint = (
        f"Tier 1 ENABLED (Llama 3.1 8B local via MLX; model path {LOCAL_MODEL_PATH}; HF_HOME={os.environ.get('HF_HOME', 'unset')})"
        if MLX_AVAILABLE
        else f"Tier 1 DISABLED (mlx_lm import failed: {_MLX_IMPORT_ERROR})"
    )
    logger.info(f"Cloud (Tier 3): {auth_hint}. Model: {ANTHROPIC_MODEL}. Timeout: {QUERY_TIMEOUT_SECONDS}s.")
    logger.info(f"Local: {local_hint}. Max tokens: {LOCAL_MAX_TOKENS}. Timeout: {LOCAL_TIMEOUT_SECONDS}s.")
    logger.info(
        f"Conversation memory: SQLite at {DB_PATH}. History limits: local={HISTORY_LIMIT_LOCAL}, cloud={HISTORY_LIMIT_CLOUD}."
    )
    logger.info(
        f"Workspace awareness (cloud tier): Blueprint + Addendum + INBOX + DAILY_BRIEFING + last {RECENT_DECISIONS_LIMIT} Decisions + last {RECENT_SESSIONS_LIMIT} Session digests, freshly loaded each message."
    )
    logger.info(
        f"Quick Capture bridge: ENABLED. Prefixes {QUICK_CAPTURE_PREFIXES} append to {QUICK_CAPTURE_FILE} for Cowork-Iris routing."
    )
    logger.info(
        f"Second-brain capture: ENABLED. Prefixes {SECOND_BRAIN_PREFIXES} write a timestamped "
        f"capture note into {SECOND_BRAIN_INBOX} for the nightly inbox-processor (fast-path, no model call)."
    )
    logger.info(
        "Decision Feeder: ENABLED. '/decision <q>' (or /agent decision-feeder) scans both vaults → "
        "06_CEO/Decision_Briefs/; daily 03:40 ET deadline-watch auto-fires for near-deadline 💰/📤/⚖️ rows (skip-on-empty)."
    )
    logger.info(
        f"A4 receipt-photo capture: ENABLED. Photos with a `RECEIPT:` "
        f"caption → photo saved to {RECEIPTS_DIR.relative_to(WORKSPACE_DIR)}/ "
        f"+ single-row draft into {EXPENSE_DRAFTS_DIR.relative_to(WORKSPACE_DIR)}/ "
        f"+ /approve <run-id> fills CSV. Reuses expense-categorizer schema."
    )
    logger.info(
        f"Pipeline orchestrator: ENABLED. '/pipeline <N>' advances video N one stage; "
        f"'/pipeline <N> status' shows the state table; '/pipeline <N> spend-ok' is the "
        f"ONLY billed-image path. Fleet: '/pipeline all' advances every video to its next "
        f"gate, '/pipeline all status' is the read-only supervision sweep. "
        f"Fixed-shape, NOT model-routable. Script: {PIPELINE_SCRIPT}."
    )
    logger.info(
        f"Telegram media: ENABLED. Outbound via send_photo_to_steve/send_file_to_steve "
        f"(internal), {OUTBOX_DIR}/*.json file-queue drained every "
        f"{OUTBOX_DRAIN_INTERVAL_SECONDS}s (scripts/tg_send.sh enqueues), and a gated "
        f"`[[SEND_FILE: ...]]` chat sentinel (vault-contained). Inbound: non-RECEIPT "
        f"photos + documents → {INBOX_MEDIA_DIR.relative_to(WORKSPACE_DIR)}/ + Quick Capture."
    )
    logger.info(
        "OAuth-aware error reply: ENABLED. Auth failures surface a 'run claude login' hint to Steve via Telegram."
    )
    logger.info(
        f"Daily Tier 4 spend cap: ${DAILY_TIER4_CAP_USD:.2f}/day. "
        "Today's accumulated Tier 4 spend will show on startup once any Tier 4 calls fire (currently none do). "
        "Send '/usage' from Telegram to see tier-split message counts on demand."
    )
    logger.info(
        f"Phase 3 morning briefing: scheduled daily at "
        f"{MORNING_BRIEFING_HOUR:02d}:{MORNING_BRIEFING_MINUTE:02d} {TIMEZONE}. "
        "Job auto-regenerates DAILY_BRIEFING.md (with .bak-<timestamp> backup) before sending. "
        "Send '/briefing' for on-demand brief; '/refresh' to manually regenerate the file."
    )
    logger.info(f"Runtime date today: {_runtime_date_block()}")
    logger.info(
        f"Phase 5 dispatcher: ENABLED on interactive cloud turns. "
        f"Allowed agents: {DISPATCH_ALLOWED_AGENTS}. Max concurrent: {DISPATCH_MAX_CONCURRENT}. "
        f"claude CLI: {CLAUDE_CLI_PATH}. Debug via '/agent <name> <prompt>'; "
        f"list via '/dispatches'."
    )
    logger.info(
        f"Router v2 (hybrid): fast deterministic rules first; Haiku classifier "
        f"({ROUTER_CLASSIFIER_MODEL}, {ROUTER_CLASSIFIER_TIMEOUT_SECONDS}s timeout) "
        "only fires for ambiguous messages. Most messages route in microseconds "
        "via keyword/length/pattern rules. Force with /cloud or /local prefix."
    )

    # post_init hook starts the apscheduler AFTER the bot is ready so the
    # scheduler shares the same asyncio event loop as python-telegram-bot.
    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(_post_init)
        .build()
    )
    # filters.TEXT | filters.COMMAND lets slash-prefixed messages flow through too.
    app.add_handler(MessageHandler(filters.TEXT | filters.COMMAND, handle_message))
    # A4 (Redesign Night 4, 2026-06-13): photos with `RECEIPT:` captions
    # route through handle_photo_message → _handle_receipt_photo → draft +
    # /approve. Non-RECEIPT photos get a soft hint and are ignored.
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo_message))
    # Inbound documents (2026-06-19): download → _Iris_Memory/Telegram_Inbound/
    # + Quick Capture line. Capture-only; Cowork-Iris files it next session.
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document_message))
    app.add_error_handler(_on_telegram_error)
    logger.info(
        "Iris (Telegram daemon — Tier 1 local Llama 3.1 8B + Tier 3 cloud Haiku 4.5 via Max OAuth + "
        "SQLite memory + runtime date, allowlist enforced) starting. Ctrl+C to stop."
    )
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as _crash:  # noqa: BLE001 — alert before launchd restarts us
        import traceback as _tb
        _last = _tb.format_exc().strip().splitlines()[-1:]
        _alert_steve(
            "🔴 Iris daemon CRASHED — going down (launchd will restart it).\n"
            f"{type(_crash).__name__}: {str(_crash)[:300]}\n"
            f"{_last[0] if _last else ''}",
            wait=True,
        )
        raise
