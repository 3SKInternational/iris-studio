#!/usr/bin/env python3
"""Selftest for scripts/precheck.sh — the gate that runs BEFORE this repo's own
tests can be trusted. (LOW-2, round 12 review, 2026-07-27.)

WHY THIS EXISTS. Every other piece of the pre-review gate now has a suite that
notices it regressing (tests/test_mutate.py guards the mutator). precheck.sh
itself had none, and that gap is exactly what let two real bugs ship:
  * HIGH-1: the env-skip branch checked only "exactly one 'No module named'
    line, and it's on the allowlist" — never whether the SAME suite ALSO
    printed real [FAIL] lines. A suite missing PIL with five real defects
    printed CLEARED.
  * MEDIUM-2: a live-mutation-lock skip (rc=75 + LOCK_SKIP) was folded into
    the "N env-skipped" banner text — indistinguishable, in the one line that
    survives a condensed relay into a review dispatch, from a genuine
    missing-module skip (different cause, different remedy).

This builds a scratch tree with synthetic tests/test_*.py fixtures and runs
the REAL precheck.sh against each scenario, asserting the BANNER TEXT and
exit code together — exit code alone is exactly what let both bugs above ship
past a green run (an unasserted claim can't see a wrong label next to a
correct verdict).

Run: python3 tests/test_precheck.py
"""

import pathlib
import shutil
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
PRECHECK = REPO / "scripts" / "precheck.sh"

FAILS = []


def check(name, cond, detail=""):
    if cond:
        print(f"  [ok ] {name}")
    else:
        print(f"  [FAIL] {name} {detail}")
        FAILS.append(name)


def _scenario(tmp, files):
    """files: {filename: source}. Replaces tests/ wholesale, runs the real
    precheck.sh against it, returns (rc, combined stdout+stderr)."""
    tests_dir = tmp / "tests"
    if tests_dir.exists():
        shutil.rmtree(tests_dir)
    tests_dir.mkdir()
    for name, content in files.items():
        (tests_dir / name).write_text(content, encoding="utf-8")
    proc = subprocess.run(["bash", "scripts/precheck.sh"], cwd=str(tmp),
                          capture_output=True, text=True, timeout=30)
    return proc.returncode, proc.stdout + proc.stderr


PASS_SRC = "import sys\nsys.exit(0)\n"
# Manually caught + re-printed, not a bare `raise`: an uncaught raise makes
# Python's default traceback echo the SOURCE LINE too, and that source line
# also contains the literal string "No module named 'PIL'" — doubling the
# grep -c count precheck.sh keys on and defeating the very scenario this is
# meant to simulate. A real `import PIL` failure never has this artifact.
ENV_SKIP_SRC = (
    "import sys\n"
    "print(\"ModuleNotFoundError: No module named 'PIL'\", file=sys.stderr)\n"
    "sys.exit(1)\n"
)
LOCK_SKIP_SRC = (
    "import sys\n"
    "print('LOCK_SKIP: a live mutation run held the lock')\n"
    "sys.exit(75)\n"
)


def main():
    print("precheck.sh")
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="pc_"))
    (tmp / "scripts").mkdir()
    shutil.copy(PRECHECK, tmp / "scripts" / "precheck.sh")
    try:
        # --- A: all pass, no skips ------------------------------------------
        rc, out = _scenario(tmp, {"test_a.py": PASS_SRC})
        check("all-pass: rc 0", rc == 0, f"rc={rc}")
        check("all-pass: banner has no skip mention",
              "ALL SUITES GREEN — 1 passed." in out and "skipped" not in out,
              out[-300:])

        # --- B: one genuine env-skip (allowlisted module, nothing else) -----
        rc, out = _scenario(tmp, {"test_a.py": PASS_SRC, "test_b.py": ENV_SKIP_SRC})
        check("env-skip: rc 0", rc == 0, f"rc={rc}")
        check("env-skip: banner says env-skipped, not lock-skipped",
              "1 env-skipped" in out and "lock-skipped" not in out, out[-300:])

        # --- C: HIGH-1 — allowlisted import gap ALONGSIDE real failures -----
        # Must NOT be excused as an env-skip: this is the exact defect class
        # the gate exists to catch, wearing the module-skip's clothes.
        mixed_src = (
            "import sys\n"
            "print('[FAIL] real defect 0')\n"
            "print('[FAIL] real defect 1')\n"
            "print(\"ModuleNotFoundError: No module named 'PIL'\", file=sys.stderr)\n"
            "sys.exit(1)\n"
        )
        rc, out = _scenario(tmp, {"test_a.py": PASS_SRC, "test_mixed.py": mixed_src})
        check("HIGH-1: real [FAIL]s + one import gap is BLOCKED, not excused",
              rc == 1 and "BLOCKED" in out and "CLEARED" not in out,
              f"rc={rc} out={out[-400:]}")

        # --- D: lock-skip (rc=75, LOCK_SKIP marker) — MED-6's rc-75 path ----
        rc, out = _scenario(tmp, {"test_a.py": PASS_SRC, "test_lock.py": LOCK_SKIP_SRC})
        check("lock-skip: rc 0", rc == 0, f"rc={rc}")
        check("MEDIUM-2: banner says lock-skipped, NEVER env-skipped",
              "1 lock-skipped" in out and "env-skipped" not in out, out[-300:])

        # --- E: an env-skip and a lock-skip together -------------------------
        rc, out = _scenario(tmp, {
            "test_a.py": PASS_SRC, "test_b.py": ENV_SKIP_SRC, "test_lock.py": LOCK_SKIP_SRC,
        })
        check("mixed skips: rc 0", rc == 0, f"rc={rc}")
        check("mixed skips: banner names both counts distinctly",
              "1 env-skipped" in out and "1 lock-skipped" in out, out[-300:])

        # --- F: a NON-allowlisted (internal) import gap is a real failure ---
        internal_src = "raise ImportError(\"No module named 'shot_ids_internal'\")\n"
        rc, out = _scenario(tmp, {"test_a.py": PASS_SRC, "test_internal.py": internal_src})
        check("non-allowlisted import gap: BLOCKED, not skipped",
              rc == 1 and "BLOCKED" in out, f"rc={rc} out={out[-300:]}")

        # --- G: an arbitrary non-zero, non-75 exit ("rc-3" path) -------------
        rc3_src = "import sys\nsys.exit(3)\n"
        rc, out = _scenario(tmp, {"test_a.py": PASS_SRC, "test_rc3.py": rc3_src})
        check("rc=3 suite: BLOCKED and the exit code is reported correctly",
              rc == 1 and "rc=3" in out, f"rc={rc} out={out[-300:]}")

        # --- H: everything env-skipped, nothing actually ran -----------------
        rc, out = _scenario(tmp, {"test_only_skip.py": ENV_SKIP_SRC})
        check("all-skipped: BLOCKED (nothing verified), not green",
              rc == 1 and "zero suites actually ran" in out, f"rc={rc} out={out[-300:]}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED: {FAILS}")
        return 1
    print("ALL OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
