#!/usr/bin/env bash
# tg_send.sh — enqueue a Telegram media send for the iris.py daemon.
#
# Drops a JSON job into the daemon outbox; the daemon's drain_outbox job (every
# ~10s) sends it to Steve and deletes the job. No network listener, no daemon
# import — just a file in a watched directory.
#
# Usage:
#   scripts/tg_send.sh <path-or-url> [caption] [--doc]
#
# Examples:
#   scripts/tg_send.sh /Users/steve/Documents/3SK/outputs/thumb.png "V1 thumb"
#   scripts/tg_send.sh https://example.com/report.pdf "the report" --doc
#
# --doc forces sending as a document (otherwise images send as photos).
set -euo pipefail
SRC="${1:?usage: tg_send.sh <path-or-url> [caption] [--doc]}"
CAP="${2:-}"
ASDOC="false"; [ "${3:-}" = "--doc" ] && ASDOC="true"
OUT="/Users/steve/iris_studio/outbox"
mkdir -p "$OUT"
python3 - "$SRC" "$CAP" "$ASDOC" "$OUT" <<'PY'
import json, os, sys, time, pathlib
src, cap, asdoc, out = sys.argv[1:5]
job = {"src": src, "caption": cap or None, "as_document": asdoc == "true"}
out = pathlib.Path(out)
final = out / f"{int(time.time()*1000)}.json"
# Write to a temp name then atomically rename into place, so the daemon's drain
# loop (which globs *.json) never reads a half-written job.
tmp = out / f".{final.name}.tmp"
tmp.write_text(json.dumps(job))
os.rename(tmp, final)
print(final)
PY
