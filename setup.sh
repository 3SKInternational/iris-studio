#!/usr/bin/env bash
# Iris / V.A.U.L.T. one-command bootstrap.
#   git clone … && cd iris_studio
#   cp .env.example .env   # fill in your keys
#   ./setup.sh             # installs deps, links the fleet, brings the stack online
#
# Idempotent: safe to re-run. Use --dry-run to print the plan without touching anything.
set -euo pipefail
cd "$(dirname "$0")"

DRY=0
[[ "${1:-}" == "--dry-run" || "${1:-}" == "-n" ]] && DRY=1

c_ok=$'\033[32m'; c_warn=$'\033[33m'; c_err=$'\033[31m'; c_dim=$'\033[2m'; c_end=$'\033[0m'
ok()   { printf '%s✓%s %s\n' "$c_ok" "$c_end" "$*"; }
warn() { printf '%s⚠%s  %s\n' "$c_warn" "$c_end" "$*"; }
die()  { printf '%s✗%s %s\n' "$c_err" "$c_end" "$*" >&2; exit 1; }
step() { printf '\n%s→ %s%s\n' "$c_dim" "$*" "$c_end"; }
run()  { if [[ $DRY == 1 ]]; then printf '   would run: %s\n' "$*"; else eval "$*"; fi; }

[[ $DRY == 1 ]] && warn "DRY RUN — no changes will be made"

# ── 1. .env ──────────────────────────────────────────────────────────────────
step ".env"
if [[ ! -f .env ]]; then
  if [[ $DRY == 1 ]]; then
    warn "would copy .env.example → .env (then you must fill in keys)"
  else
    cp .env.example .env
    warn "created .env from template — FILL IN YOUR KEYS, then re-run ./setup.sh"
    exit 0
  fi
else
  ok ".env present"
fi

# ── 2. system deps (Homebrew) ──────────────────────────────────────────────────
step "system deps (espeak-ng, portaudio, ollama — for local voice)"
if command -v brew >/dev/null 2>&1; then
  for pkg in espeak-ng portaudio ollama; do
    if brew list --formula "$pkg" >/dev/null 2>&1; then
      ok "$pkg"
    else
      warn "installing $pkg"
      run "brew install $pkg"
    fi
  done
else
  warn "Homebrew not found — install espeak-ng, portaudio, ollama yourself for local voice"
fi

# ── 3. local LLM model (Ollama) ──────────────────────────────────────────────────
step "local chatter model (ollama llama3.2:3b)"
OLLAMA_MODEL="${IRIS_OLLAMA:-llama3.2:3b}"
if command -v ollama >/dev/null 2>&1; then
  if ollama list 2>/dev/null | grep -q "${OLLAMA_MODEL%%:*}"; then
    ok "$OLLAMA_MODEL"
  else
    warn "pulling $OLLAMA_MODEL (one-time, ~2GB)"
    run "ollama pull $OLLAMA_MODEL"
  fi
else
  warn "ollama not installed — voice chatter will escalate everything to the claude CLI"
fi

# ── 4. Python venv + deps ──────────────────────────────────────────────────────
step "python venv + deps"
if [[ ! -d .venv ]]; then
  warn "creating .venv"
  run "python3 -m venv .venv"
else
  ok ".venv present"
fi
run ".venv/bin/pip install --quiet --upgrade pip"
run ".venv/bin/pip install --quiet -r requirements.txt"
[[ -f voice/requirements.txt ]] && run ".venv/bin/pip install --quiet -r voice/requirements.txt"
ok "python deps installed"

# ── 5. agent + skill fleet ──────────────────────────────────────────────────────
step "agent + skill fleet (~/.claude)"
# ponytail: || true — find exits 1 on an absent ~/.claude dir (fresh machine), which
# pipefail+errexit would otherwise turn into a silent mid-script abort.
agents=$(find ~/.claude/agents -maxdepth 1 -name '*.md' 2>/dev/null | wc -l | tr -d ' ' || true)
skills=$(find ~/.claude/skills -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l | tr -d ' ' || true)
if [[ "$agents" -gt 0 ]]; then ok "$agents agents linked"; else warn "no agents in ~/.claude/agents — the fleet is empty"; fi
if [[ "$skills" -gt 0 ]]; then ok "$skills skills linked"; else warn "no skills in ~/.claude/skills"; fi
command -v claude >/dev/null 2>&1 && ok "claude CLI on PATH" || warn "claude CLI not found — install it + run 'claude login'"

# ── 6. vault mount ──────────────────────────────────────────────────────────────
step "vault"
VAULT="${SK_VAULT:-$HOME/Documents/3SK/outputs}"
VAULT="${VAULT/#\~/$HOME}"
if [[ -d "$VAULT" ]]; then ok "vault mounted: $VAULT"; else warn "vault not found at $VAULT — set SK_VAULT in .env"; fi

# ── done ─────────────────────────────────────────────────────────────────────
printf '\n%s════ ready ════%s\n' "$c_ok" "$c_end"
ok "skills linked · vault mounted"
ok "to bring the stack online:"
cat <<'EOF'
   voice      .venv/bin/python voice/voice_chat.py        # local two-way voice
   dashboard  .venv/bin/python dashboard/server.py        # V.A.U.L.T. cockpit → http://127.0.0.1:8765
   daemon     ./run_iris.sh                               # Telegram operator daemon
   → reskin per client: see 06_CEO/Designs/<dated>_Per_Client_Reskin.md in the vault
EOF
