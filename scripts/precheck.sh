#!/usr/bin/env bash
# Pre-review gate: run EVERY test suite in tests/ and report a single verdict.
#
# WHY THIS EXISTS (2026-07-27, Steve). Two separate review rounds that day were
# spent on defects the repo's own suites already caught in under two seconds:
# `raw_id` undefined in parse_shot_list (22 tests red), and a spend-gate change
# that broke 5 tests in a class unrelated to it. Neither was a code-quality
# problem — both were "shipped to a reviewer without running the tests." A
# reviewer round costs minutes and tokens; this costs seconds.
#
# THE RULE: do not dispatch a code reviewer, and do not commit, until this prints
# ALL SUITES GREEN. Attach its output to the review dispatch.
#
# Suites are discovered, not listed, so a new tests/test_*.py is covered the
# moment it lands — a hardcoded list silently stops covering what it doesn't name.
# Both harness styles are supported: unittest mains (exit 0 / "OK") and the
# check()-style scripts that exit non-zero on failure.
#
# A green suite is only half the gate: it says the code passes, not that the tests
# would NOTICE it being wrong. Pass --mutate <file> to also run scripts/mutate.py
# restricted to the lines you changed vs HEAD. Whole-file mutation reports
# survivors in code the change never touched, which trains you to ignore output.
#
# Usage: scripts/precheck.sh [-v] [--mutate <file.py> ...]
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2

# LOW-D (round 22): the env var only stops NEW .pyc creation -- CPython still
# LOADS a pre-existing .pyc if its mtime/size look fresh, so a suite phase run
# without this can silently execute stale bytecode left by an earlier ad-hoc
# `python3` invocation (demonstrated: same source, same size/mtime, changed
# return value, old .pyc still won). Exported once here so both the suite
# phase and the mutate phase (which build `env` from os.environ) inherit it.
export PYTHONDONTWRITEBYTECODE=1

# ...and PURGE what is already there. The export above stops NEW caches; by the
# comment's own words it does nothing about a cache that already exists, so the
# stated hazard was diagnosed but never actually closed. It bit for real on
# 2026-07-29: a review agent reverted a statement ordering in adaptation.py to
# check that a fixture bites, then restored it — within the same clock second,
# and a pure statement reorder is byte-length identical. CPython validates a
# .pyc on (mtime_seconds, size), so BOTH matched and the stale bytecode won.
# The gate then failed `test_adaptation` against source that was completely
# correct, and the executing code had the opposite ordering to the file on disk.
# ~20 minutes to diagnose, and it would have read as a real BLOCK to anyone who
# trusted it. Purging is cheap and unconditional; .venv is excluded because
# site-packages caches are not ours and are expensive to rebuild.
find . -name '__pycache__' -type d -not -path './.venv/*' -prune -exec rm -rf {} + 2>/dev/null || true

VERBOSE=""
declare -a MUTATE=()
while [ $# -gt 0 ]; do
    case "$1" in
        -v) VERBOSE=1; shift ;;
        --mutate)
            [ $# -ge 2 ] || { echo "--mutate needs a file argument" >&2; exit 2; }
            MUTATE+=("$2"); shift 2 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done
# Third-party modules a dev box may legitimately lack. Anything NOT on this list
# — especially an internal module — is a FAILURE, not a skip.
SKIPPABLE='googleapiclient|google|PIL|moviepy|openai|requests|numpy|yaml|dotenv'

pass=0 fail=0 envskip=0 lockskip=0
declare -a FAILED=() SKIPPED=() LOCKSKIPPED=() PASSED=()

# Discover suites in EVERY directory that holds them, not just tests/. This globbed
# `tests/test_*.py` alone until 2026-07-28, while 7 suites lived in scripts/ and 2 in
# image_factory/ — so the gate printed "ALL SUITES GREEN" with 9 suites never run,
# including the regression tests guarding the redaction fail-open and the
# crashed-linter-reads-clean defects. That is the same false-green class this gate
# exists to catch, in the gate itself. Kept as an explicit directory list rather than a
# repo-wide find so a stray test_*.py in .venv or a scratch dir can't join the run;
# adding a directory here is the one manual step.
#
# The REPO ROOT was still missing until 2026-07-28 (round-3 review) — the same
# false-green, one directory further out: `test_dual_form.py` (the Video_05 VO/SRT
# dual-form guard, unique, nowhere else) had never run in the gate. Fixing an
# instance of this does not fix the class, so `test_precheck.py` now WALKS the real
# tree and fails if any directory containing a `test_*.py` is absent from this line.
# That assertion, not this comment, is what pins the set.
for t in ./test_*.py tests/test_*.py scripts/test_*.py image_factory/test_*.py; do
    [ -e "$t" ] || continue          # glob with no match expands to itself
    name=$(basename "$t" .py)
    out=$(python3 "$t" 2>&1); rc=$?
    if [ $rc -eq 0 ]; then
        printf '  \033[32m✓\033[0m %-38s\n' "$name"; pass=$((pass + 1)); PASSED+=("$t")
    elif [ $rc -eq 75 ] && grep -q "LOCK_SKIP" <<<"$out"; then
        # Reserved convention: any suite may exit 75 + print LOCK_SKIP to report a
        # partial run held back by a live mutation lock. No suite currently does
        # this for real — test_mutate.py's own live-lock skip was removed when
        # its self-test fixtures went PID-scoped instead of sharing state with a
        # concurrent mutate.py run — tests/test_precheck.py's D/E cases exercise
        # this path synthetically so the gate still recognizes it if a future
        # suite needs it. Counted separately from envskip (MEDIUM-2, 2026-07-27):
        # the banner used to fold this into "N env-skipped", which is a DIFFERENT
        # reason a relay-condensed dispatch cannot tell apart from a genuine env gap.
        printf '  \033[33m~\033[0m %-38s lock: partially skipped (live mutation run held it)\n' "$name"
        LOCKSKIPPED+=("$name (mutation lock held)"); lockskip=$((lockskip + 1))
    elif [ "$(grep -c "No module named" <<<"$out")" = "1" ] \
         && grep -qE "No module named '($SKIPPABLE)'" <<<"$out" \
         && ! grep -qE "^(FAIL|ERROR)[: ]|\[FAIL\]" <<<"$out"; then
        # An absent THIRD-PARTY import is an environment gap, not a defect in the
        # change under review, and failing the gate on it guarantees the gate gets
        # routinely bypassed. But the module must be on the allowlist and be the
        # ONLY failure: a typo'd INTERNAL import presents identically, and that is
        # the exact defect class this gate exists to catch (`raw_id` undefined).
        # A suite with one import gap and twenty real failures must not be excused
        # (HIGH-1, 2026-07-27): the two conditions above only look at the "No
        # module named" line count/allowlist match, never at whether the SAME
        # output also carries real failure markers — a suite missing PIL AND
        # printing 5 [FAIL] lines used to fall into this branch clean. Reuse the
        # exact marker line 75 (below) already fails a suite on.
        mod=$(grep -o "No module named '[^']*'" <<<"$out" | head -1)
        printf '  \033[33m~\033[0m %-38s env: %s\n' "$name" "$mod"
        SKIPPED+=("$name ($mod)"); envskip=$((envskip + 1))
    else
        printf '  \033[31m✗\033[0m %-38s rc=%d\n' "$name" "$rc"
        FAILED+=("$name"); fail=$((fail + 1))
        [ -n "$VERBOSE" ] && sed 's/^/      /' <<<"$out"
        grep -E "^(FAIL|ERROR)[: ]|\[FAIL\]" <<<"$out" | head -5 | sed 's/^/      /'
    fi
done

echo
if [ $fail -gt 0 ]; then
    echo "BLOCKED — $fail suite(s) failing: ${FAILED[*]}"
    echo "Fix before dispatching a reviewer or committing. Re-run with -v for full output."
    exit 1
fi
[ $envskip -gt 0 ] && echo "note: $envskip suite(s) skipped on missing modules: ${SKIPPED[*]}"
[ $lockskip -gt 0 ] && echo "note: $lockskip suite(s) lock-skipped (live mutation run held them): ${LOCKSKIPPED[*]}"
if [ $pass -eq 0 ]; then
    # Zero failures AND zero passes is not green — it is a run that executed no
    # tests, e.g. every suite env-skipped on a box missing numpy/PIL/moviepy. Same
    # class as the --max head-slice: a run that verified nothing reading as
    # complete. (Also guards "${PASSED[@]}" below, which is an unbound-variable
    # abort on macOS bash 3.2 when the array is empty.)
    echo "BLOCKED — zero suites actually ran ($envskip env-skipped, $lockskip lock-skipped). Nothing was verified."
    exit 1
fi
# $envskip/$lockskip are the STRING "0", which is non-null, so ${x:+...} always
# expanded and a fully-provisioned box read "0 env-skipped".
# MEDIUM-2 (2026-07-27): this banner is the one line that survives a condensed
# relay into a review dispatch — it must not attribute a lock-skip to "env",
# a DIFFERENT reason with a different remedy (wait vs. install a module).
if [ $envskip -gt 0 ] && [ $lockskip -gt 0 ]; then
    echo "ALL SUITES GREEN — $pass passed, $envskip env-skipped, $lockskip lock-skipped."
elif [ $envskip -gt 0 ]; then
    echo "ALL SUITES GREEN — $pass passed, $envskip env-skipped."
elif [ $lockskip -gt 0 ]; then
    echo "ALL SUITES GREEN — $pass passed, $lockskip lock-skipped."
else
    echo "ALL SUITES GREEN — $pass passed."
fi

# Mutation pass: only the suites that actually ran are handed to the mutator, so
# an env-skipped suite can't be mistaken for coverage.
mrc=0
if [ ${#MUTATE[@]} -gt 0 ]; then
    # RAN was rebuilt by re-running every suite a second time — pure waste. PASSED
    # is collected by the verdict loop above; just filter it.
    # test_mutate.py is EXCLUDED: it invokes mutate.py, which is holding the lock
    # during this pass. It ran in the suite phase above; this only drops it from
    # the per-mutant inner loop.
    declare -a RAN=()
    for t in "${PASSED[@]}"; do
        [ "$(basename "$t")" = "test_mutate.py" ] && continue
        RAN+=("$t")
    done
    if [ ${#RAN[@]} -eq 0 ]; then
        # bash 3.2 aborts on "${RAN[@]}" when empty under `set -u`, printing a bash
        # internal error directly beneath a green banner. Say what is actually wrong.
        echo "BLOCKED — no suites left to mutate against (only test_mutate.py passed)."
        exit 1
    fi
    for f in "${MUTATE[@]}"; do
        echo
        echo "--- mutation pass: $f (changed lines only) ---"
        echo "    (do NOT edit $f while this runs — it is rewritten in place"
        echo "     and restored, so a concurrent save is silently reverted)"
        # Capture the REAL rc — `|| mrc=1` would collapse rc=2 (could not run) into
        # rc=1 (survivors) and print the wrong remedy.
        python3 scripts/mutate.py --diff-only "$f" "${RAN[@]}"; rc2=$?
        [ $rc2 -ne 0 ] && mrc=$rc2
    done
fi

echo
if [ $mrc -eq 1 ]; then
    echo "BLOCKED — mutation survivors above. Each is a missing fixture or an"
    echo "equivalent mutant; classify every one IN WRITING before dispatching review."
    echo "Do NOT mark one equivalent without proving it: on the first real use, 1 of"
    echo "3 accepted markers was wrong and hid a guard that kills 18 real manifests."
    exit 1
elif [ $mrc -ne 0 ]; then
    # rc=2 is mutate.py refusing to run at all (stale lock, red baseline, untracked
    # target under --diff-only). Zero survivors, but zero verification either.
    echo "BLOCKED — the mutation pass could not RUN (see its error above). That is"
    echo "not a pass: nothing was verified."
    exit 1
fi
echo "CLEARED to dispatch review / commit. Attach this output to the dispatch."
