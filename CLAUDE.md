# CLAUDE.md — iris.py daemon repo (engineering side-car)

You're in the `iris.py` Telegram-daemon repo (`/Volumes/AI_Workspace/iris_studio/`, GitHub `3SKInternational/iris-studio`). This is a sub-surface of the larger 3SK / Iris engineering context.

## ⚡ Read the FULL context first

The master Claude Code context + cross-session memory lives in the vault, NOT here:

1. **`/Users/steve/Documents/3SK/outputs/CLAUDE.md`** — the master engineering context + boot sequence.
2. **`/Users/steve/Documents/3SK/outputs/_Iris_Memory/Sessions/CLAUDE_CODE_HANDOFF.md`** — the bridge file. Read the LATEST entry to see what the prior session did + what to pick up. **This is your cross-session memory.**

Read both before doing daemon work.

## This repo specifically

- **`iris.py`** — the daemon. ~1500 lines. Tier 1 local (Llama 8B / MLX) + Tier 2 local (Qwen 14B, `/tier2` prefix) + Tier 3 cloud (Haiku 4.5 via Max-sub OAuth). SQLite memory + MCPs (`obsidian`, `google-workspace`) + apscheduler morning brief + Quick Capture bridge.
- **`routines/*.prompt`** — the prompts the `com.iris.claude-code-*` launchd routines run.
- **`scripts/*.sh`** — ops helpers (drive-sync, log-rotate, remote-url, etc.).
- **`.env`** (chmod 600, gitignored) — TELEGRAM_BOT_TOKEN, OBSIDIAN_API_KEY, ANTHROPIC_API_KEY (Tier 4 fallback). `.env.test` for the shadow test bot.

## Hard rules (daemon-specific)

- **Backup before structural edits:** `cp iris.py iris.py.bak-pre-<change>` (then prefer a git commit too).
- **Always test after editing:** syntax check `.venv/bin/python -m py_compile iris.py`, then `launchctl kickstart -k gui/$(id -u)/com.iris.studio`, then tail `/Users/steve/iris_studio/logs/iris.err.log` for a clean boot.
- **Never commit `iris.db`, `.env*`, or any `*.bak*`** — all gitignored. The DB has conversation history (privacy).
- Don't push to GitHub without Steve's explicit go-ahead.

---

_Auto-loaded when a Claude Code session starts in this repo. The vault's CLAUDE.md + the bridge file are the real context — this is just the daemon-repo signpost._
