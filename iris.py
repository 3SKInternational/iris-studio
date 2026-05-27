"""Iris Studio v0.4 (2026-05-26) — Telegram bot with Tier 1 local + Tier 3 cloud + WebSearch/WebFetch for explicit research asks.

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
import logging
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

PROJECT_DIR = Path(__file__).parent
load_dotenv(PROJECT_DIR / ".env")

# Force Agent SDK to use the claude CLI OAuth (Max sub).
ANTHROPIC_API_KEY_FALLBACK = os.environ.pop("ANTHROPIC_API_KEY", None)

# Point HF cache at X9 so the local model is portable with the drive.
os.environ.setdefault("HF_HOME", "/Volumes/AI_Workspace/models")

# Import the Agent SDK AFTER the env strip.
from claude_agent_sdk import query, ClaudeAgentOptions  # noqa: E402

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
MORNING_BRIEFING_HOUR = 6  # Was 7, moved to 6 AM ET on 2026-05-25 per Steve
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

def _init_db_sync() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
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
    with sqlite3.connect(DB_PATH) as conn:
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
    with sqlite3.connect(DB_PATH) as conn:
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
    with sqlite3.connect(DB_PATH) as conn:
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
    with sqlite3.connect(DB_PATH) as conn:
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
        # Back up existing file with timestamp
        if DAILY_BRIEFING_FILE.exists():
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = DAILY_BRIEFING_FILE.with_name(
                f"DAILY_BRIEFING.md.bak-{timestamp}"
            )
            DAILY_BRIEFING_FILE.rename(backup_path)
            logger.info(f"Previous DAILY_BRIEFING.md backed up to {backup_path.name}")
        DAILY_BRIEFING_FILE.write_text(new_content, encoding="utf-8")
        logger.info(f"DAILY_BRIEFING.md regenerated ({len(new_content)} chars)")
        await record_message_stat("cloud", cost_usd=0.0)
        return True
    except Exception as exc:
        logger.exception(f"DAILY_BRIEFING regen failed: {exc}")
        return False


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
    global _scheduler
    if not ALLOWED_USER_IDS:
        logger.warning(
            "Scheduler not starting — no authorized users. Morning briefing disabled."
        )
        return
    # In private chats, chat_id == user_id. ALLOWED_USER_IDS is keyed by
    # Telegram user IDs; use the first authorized user as the briefing target.
    target_chat_id = next(iter(ALLOWED_USER_IDS))
    _scheduler = AsyncIOScheduler(timezone=TIMEZONE)
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
    )
    _scheduler.start()
    job = _scheduler.get_job("morning_briefing")
    next_run = job.next_run_time if job else None
    logger.info(
        f"Scheduler started. Morning briefing: daily at "
        f"{MORNING_BRIEFING_HOUR:02d}:{MORNING_BRIEFING_MINUTE:02d} {TIMEZONE}. "
        f"Next fire: {next_run.isoformat() if next_run else 'unknown'} "
        f"(target chat_id={target_chat_id})."
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

async def query_cloud(prompt: str, history: list[dict], timeout: float | None = None) -> str:
    """Call Agent SDK with OAuth auth and history-augmented system prompt.

    timeout: per-call override in seconds. Defaults to QUERY_TIMEOUT_SECONDS
    (60s) for typical chat. Long-output operations (DAILY_BRIEFING regen,
    ebook generation, etc.) should pass a longer timeout to avoid spurious
    timeout failures on multi-thousand-character generations.
    """
    effective_timeout = timeout if timeout is not None else QUERY_TIMEOUT_SECONDS
    system_prompt = load_system_prompt_cloud(history)
    # v0.4 (2026-05-26): WebSearch + WebFetch enabled for cloud tier.
    # The behavior prefix tells Iris to use these tools SPARINGLY — only when
    # Steve explicitly asks for research/lookup. Routine chat answers from
    # the loaded prompt context, no tool call.
    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        model=ANTHROPIC_MODEL,
        mcp_servers=MCP_SERVERS,
        allowed_tools=[
            "WebSearch",
            "WebFetch",
            "mcp__obsidian",
            "mcp__google-workspace",
        ],
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

    text = await query_cloud(stripped, history)
    actual_tier = decided_tier if decided_tier == "cloud_fallback" else "cloud"
    await save_message(chat_id, user_id, "assistant", text, tier=actual_tier)
    await record_message_stat(actual_tier, cost_usd=0.0)
    return text, actual_tier


# ============================================================
# Telegram handlers
# ============================================================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Forward a Telegram message to the router and reply with the response."""
    user_id = update.effective_user.id
    chat_id = str(update.effective_chat.id)
    username = update.effective_user.username or update.effective_user.first_name
    text = update.message.text

    if user_id not in ALLOWED_USER_IDS:
        logger.warning(
            f"BLOCKED unauthorized message: user_id={user_id}, "
            f"username=@{username}, text={text[:80]!r}"
        )
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

    for i in range(0, len(response), TELEGRAM_MAX_MSG):
        await update.message.reply_text(response[i : i + TELEGRAM_MAX_MSG])


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
    logger.info(
        "Iris (Telegram daemon — Tier 1 local Llama 3.1 8B + Tier 3 cloud Haiku 4.5 via Max OAuth + "
        "SQLite memory + runtime date, allowlist enforced) starting. Ctrl+C to stop."
    )
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
