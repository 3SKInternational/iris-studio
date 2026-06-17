#!/usr/bin/env bash
#
# run_card_batch.sh — one-command card pipeline for the 3SK video factory.
#
# WHY THIS EXISTS
# ---------------
# A card batch is four steps that always run in the same order:
#   1. GENERATE   text-free shape backplates from the image manifest (BILLED — gpt-image-2)
#   2. COMPOSITE  the exact text + deterministic geometry onto each backplate (card_overlay.py, $0)
#   3. QA PACKET  build the answer-key + labelled contact sheet for the vision gate (card_qa.py, $0)
#   4. NOTIFY     push the result (or any failure) to Steve's Telegram (scripts/notify.sh)
#
# The point: the operator's ONLY action is authorizing the spend by running this
# (or dispatching it). Everything downstream chains automatically — no relaying
# tasks between agents, no hand-running each step. A failure at any step aborts
# the rest and fires a 🔴 Telegram alert instead of failing silently.
#
# The vision gate itself (PASS/FAIL per card) is the one genuinely-human-or-agent
# step a script can't do; this runner hands it the packet + contact sheet and
# tells the reviewer where they are. Wire that as an agent dispatch if/when ready.
#
# USAGE
#   ./run_card_batch.sh <image_manifest.json> <overlay_spec.json> [extra generate_images flags...]
#   ./run_card_batch.sh manifests/video_02_hd.json manifests/video_02_overlay.json
#   DRY_RUN=1 ./run_card_batch.sh ... manifests/...   # validate every step, generate NOTHING (no spend)
#
# ENV
#   PY        python interpreter (default: /usr/bin/python3 — the gen env with openai + Pillow)
#   DRY_RUN   set to 1 to pass --dry-run to generation and skip notify (rehearsal, $0)
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
PY="${PY:-/usr/bin/python3}"
NOTIFY="$ROOT/scripts/notify.sh"
GEN="$ROOT/image_factory/generate_images.py"
OVERLAY="$ROOT/image_factory/card_overlay.py"
QA="$ROOT/image_factory/card_qa.py"

die() { echo "[run_card_batch] ERROR: $*" >&2; exit 1; }

notify() {
    # Never let a notify failure mask the real exit status.
    if [ "${DRY_RUN:-0}" = "1" ]; then
        echo "[run_card_batch] (dry-run, notify suppressed): $*"
        return 0
    fi
    [ -x "$NOTIFY" ] && "$NOTIFY" "$*" || echo "[run_card_batch] notify skipped: $*" >&2
}

[ "$#" -ge 2 ] || die "usage: run_card_batch.sh <image_manifest.json> <overlay_spec.json> [extra generate flags...]"
MANIFEST="$1"; shift
SPEC="$1"; shift
EXTRA=("$@")

[ -f "$MANIFEST" ] || die "image manifest not found: $MANIFEST"
[ -f "$SPEC" ]     || die "overlay spec not found: $SPEC"
[ -f "$GEN" ]      || die "generator not found: $GEN"
[ -f "$OVERLAY" ]  || die "card_overlay.py not found: $OVERLAY"
[ -f "$QA" ]       || die "card_qa.py not found: $QA"

# Resolve the composites dir + QA artifact paths UP FRONT — before any billed step.
# Why here: (1) a malformed spec / bad out_dir fails at $0 instead of after the bill;
# (2) doing it after QA succeeded would let a heredoc error fire a FALSE 🔴 "FAILED"
# alert on a run that actually spent money and produced the packet. We mirror
# card_qa.py's resolution EXACTLY (out_dir or spec's own dir, expanduser + resolve)
# so the paths in the notify message match where card_qa actually wrote.
OUT_DIR="$("$PY" - "$SPEC" <<'PY'
import json, os, sys
from pathlib import Path
spec = json.load(open(os.path.expanduser(sys.argv[1])))
d = spec.get("out_dir")
if d is not None and not isinstance(d, str):
    sys.stderr.write("spec 'out_dir' must be a string path\n"); sys.exit(1)
if not d:
    d = os.path.dirname(os.path.abspath(sys.argv[1]))
print(str(Path(os.path.expanduser(d)).resolve()))
PY
)" || die "could not resolve out_dir from spec (bad JSON or non-string out_dir?): $SPEC"
SHEET="$OUT_DIR/card_qa_contact_sheet.png"
PACKET="$OUT_DIR/card_qa_packet.json"

# Fire a 🔴 alert if we abort anywhere after this point (covers every step below).
trap 'notify "🔴 card batch FAILED (see logs) — manifest=$(basename "$MANIFEST")"' ERR

DRY_FLAG=()
if [ "${DRY_RUN:-0}" = "1" ]; then
    DRY_FLAG=(--dry-run)
    echo "[run_card_batch] DRY_RUN=1 — generation will validate only, nothing is billed."
fi

echo "[run_card_batch] 1/4 GENERATE backplates  ($MANIFEST)"
# ${arr[@]+"${arr[@]}"} = safe empty-array expansion under `set -u` (macOS bash 3.2).
"$PY" "$GEN" "$MANIFEST" --size 2048x1152 --quality medium \
    ${DRY_FLAG[@]+"${DRY_FLAG[@]}"} ${EXTRA[@]+"${EXTRA[@]}"}

if [ "${DRY_RUN:-0}" = "1" ]; then
    echo "[run_card_batch] 2/4 COMPOSITE (dry-run validate spec)"
    "$PY" "$OVERLAY" "$SPEC" --dry-run
    echo "[run_card_batch] 3/4 QA PACKET (dry-run skipped — needs real composites)"
    echo "[run_card_batch] dry-run complete — every step validated, nothing billed."
    exit 0
fi

echo "[run_card_batch] 2/4 COMPOSITE text + geometry  ($SPEC)"
"$PY" "$OVERLAY" "$SPEC"

echo "[run_card_batch] 3/4 QA PACKET + contact sheet  ($SPEC)"
"$PY" "$QA" "$SPEC"

echo "[run_card_batch] 4/4 NOTIFY"
notify "✅ card batch composited — manifest=$(basename "$MANIFEST"). Review the contact sheet ($SHEET) and run the vision gate on the QA packet ($PACKET) before assembly."
trap - ERR
echo "[run_card_batch] done. Contact sheet: $SHEET"
echo "[run_card_batch] NEXT: vision-gate the packet ($PACKET) → set PASS/FAIL per card → route FAILs to Telegram."
