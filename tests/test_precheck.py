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


def _clear_test_files(d):
    """Remove any test_*.py left in `d` by a prior scenario -- but never touch
    scripts/precheck.sh itself (copied once, in main(), not test_*-named)."""
    if d.exists():
        for f in d.glob("test_*.py"):
            f.unlink()


def _scenario(tmp, files, extra_dirs=None):
    """files: {filename: source}, replaces tests/ wholesale. extra_dirs:
    {dirname: {filename: source}} for scripts/ and image_factory/ -- HIGH-1
    (round-2 review, 2026-07-28): precheck.sh's own comment claims
    'test_precheck.py pins the set' of discovery dirs, but _scenario only ever
    wrote into tests/, so a suite living in scripts/ or image_factory/ was
    never covered by this file at all -- the one line that stops the gate from
    silently skipping those dirs (`for t in tests/test_*.py scripts/test_*.py
    image_factory/test_*.py`) could be reverted to `tests/test_*.py` alone and
    this suite stayed green. Runs the real precheck.sh, returns (rc, combined
    stdout+stderr)."""
    tests_dir = tmp / "tests"
    if tests_dir.exists():
        shutil.rmtree(tests_dir)
    tests_dir.mkdir()
    for name, content in files.items():
        (tests_dir / name).write_text(content, encoding="utf-8")

    # Clear any leftover test_*.py from a PRIOR scenario in the shared
    # scripts/ and image_factory/ dirs (scripts/ also permanently holds the
    # precheck.sh copy from main() -- never rmtree it, only glob-delete
    # test_*.py).
    for dname in ("scripts", "image_factory"):
        _clear_test_files(tmp / dname)
    for dname, dfiles in (extra_dirs or {}).items():
        d = tmp / dname
        d.mkdir(parents=True, exist_ok=True)
        for name, content in dfiles.items():
            (d / name).write_text(content, encoding="utf-8")

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

        # --- I: a failing scripts/test_*.py must BLOCK -- HIGH-1 (round-2,
        # 2026-07-28). Reverting precheck.sh's discovery glob to `tests/test_*.py`
        # alone makes this scenario print a bogus CLEARED (rc 0): nothing in
        # scripts/ would even run.
        fail_src = "import sys\nprint('[FAIL] scripts-dir defect')\nsys.exit(1)\n"
        rc, out = _scenario(tmp, {"test_a.py": PASS_SRC},
                            extra_dirs={"scripts": {"test_bad.py": fail_src}})
        check("scripts/ discovery: a failing scripts/test_*.py BLOCKS",
              rc == 1 and "BLOCKED" in out and "CLEARED" not in out,
              f"rc={rc} out={out[-400:]}")

        # --- J: a failing image_factory/test_*.py must BLOCK too -- the other
        # dir the same comment claims is pinned.
        rc, out = _scenario(tmp, {"test_a.py": PASS_SRC},
                            extra_dirs={"image_factory": {"test_bad.py": fail_src}})
        check("image_factory/ discovery: a failing image_factory/test_*.py BLOCKS",
              rc == 1 and "BLOCKED" in out and "CLEARED" not in out,
              f"rc={rc} out={out[-400:]}")

        # --- K: no test_*.py in scripts/ or image_factory/ at all (image_factory/
        # doesn't even exist) -- must stay green, only the tests/ suite counted.
        # The `[ -e "$t" ] || continue` guard on a glob-with-no-match, exercised
        # explicitly rather than only implied by scenario A never populating
        # those dirs.
        rc, out = _scenario(tmp, {"test_a.py": PASS_SRC})
        check("no-match dirs: rc 0, exactly the one tests/ suite counted",
              rc == 0 and "ALL SUITES GREEN — 1 passed." in out,
              f"rc={rc} out={out[-300:]}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # --- discovery COMPLETENESS against the REAL tree ---------------------------
    # Every case above runs in a synthetic tree, so they can only pin the
    # directories precheck.sh ALREADY names — none of them can notice one that is
    # MISSING. That gap shipped twice: first `tests/` alone (9 suites never run,
    # 2026-07-28), then tests/+scripts/+image_factory/ with the REPO ROOT still
    # absent, so test_dual_form.py — the unique Video_05 VO/SRT dual-form guard —
    # had never once run in the mandatory gate. Fixing an instance is not fixing
    # the class, and a comment claiming "the set is pinned" is not a pin.
    #
    # So walk the REAL repo and fail if any directory holding a test_*.py is not
    # in the discovery line. Vendored/scratch trees are skipped — that is exactly
    # why the line is an explicit list instead of a repo-wide find.
    SKIP_PARTS = {".venv", "venv", ".git", "node_modules", "__pycache__",
                  "07_Archive", "site-packages", ".pytest_cache"}
    line = ""
    for raw in PRECHECK.read_text(encoding="utf-8").splitlines():
        if raw.lstrip().startswith("for t in ") and "test_*.py" in raw:
            line = raw
            break
    check("discovery: located the suite-discovery line in precheck.sh", bool(line),
          "no `for t in ... test_*.py` line found — this assertion is vacuous without it")

    real_dirs = set()
    for p in REPO.rglob("test_*.py"):
        if SKIP_PARTS & set(p.parts):
            continue
        rel = p.parent.relative_to(REPO).as_posix()
        real_dirs.add("." if rel == "" else rel)

    missing = sorted(d for d in real_dirs if f"{d}/test_*.py" not in line)
    check("discovery: every dir holding a test_*.py appears in the glob",
          bool(line) and not missing,
          f"these suites would NEVER run in the gate: "
          f"{[d + '/test_*.py' for d in missing]}")

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED: {FAILS}")
        return 1
    print("ALL OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
