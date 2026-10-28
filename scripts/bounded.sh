#!/bin/bash
# bounded.sh <secs> <cmd...> — run a probe with a hard kill -9 deadline (A-100).
#
# For audit probes under ~/Documents or iCloud CloudDocs, which can block in the
# kernel on dataless files and ignore TERM and ALRM (10-25 security audit). This
# Mac has no timeout/gtimeout.
#
# The command's output goes to a temp file, not to our stdout, so a stuck child
# (or a grandchild it left behind) cannot hold the caller's pipe open past the
# deadline. We poll instead of `wait`ing, so a child that kill -9 cannot reap
# yet does not block us either. No watchdog process is left behind.
#
# Prints the command's output. On the deadline also prints "TIMEOUT <cmd>" and
# exits 124; otherwise exits with the command's own code. Bad usage exits 2.
# ponytail: kills the command and its direct children only; use setsid-style
# process groups if a probe ever spawns deeper trees.
set -u
if [ $# -lt 2 ]; then echo "usage: bounded.sh <secs> <cmd...>" >&2; exit 2; fi
secs=$1; shift
case $secs in ''|*[!0-9]*) echo "bounded.sh: secs must be a whole number" >&2; exit 2;; esac
secs=$((10#$secs))   # "08" is not octal
if [ "$secs" -eq 0 ]; then echo "bounded.sh: secs must be above 0" >&2; exit 2; fi

out=$(mktemp "${TMPDIR:-/tmp}/bounded.XXXXXX") || exit 2
trap 'rm -f "$out"' EXIT

# </dev/null is belt and braces: non-interactive bash already gives & jobs /dev/null.
# mutequiv: deleting </dev/null keeps test_stdin_not_inherited green (bash's own default)
"$@" >"$out" 2>&1 </dev/null &
pid=$!
# If we are killed first (a caller's own timeout), take the probe down with us.
trap 'pkill -9 -P "$pid" 2>/dev/null; kill -9 "$pid" 2>/dev/null; exit 143' TERM INT HUP
# SECONDS counts whole seconds; -gt makes the deadline never earlier than secs.
end=$((SECONDS + secs))
while kill -0 "$pid" 2>/dev/null; do
  if [ "$SECONDS" -gt "$end" ]; then
    pkill -9 -P "$pid" 2>/dev/null
    kill -9 "$pid" 2>/dev/null
    cat "$out"
    [ -n "$(tail -c1 "$out")" ] && echo   # keep TIMEOUT on its own line
    echo "TIMEOUT $*"
    exit 124
  fi
  sleep 0.2
done
wait "$pid"; rc=$?
cat "$out"
exit "$rc"
