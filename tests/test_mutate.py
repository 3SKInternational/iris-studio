#!/usr/bin/env python3
"""Guards for scripts/mutate.py — the mutation half of the pre-review gate.

WHY THIS EXISTS. mutate.py is now MANDATORY before any review dispatch or commit,
so a bug in it silently degrades every future review. That is not hypothetical:
on its very first real use it printed `CLEARED to dispatch review / commit` while
having skipped 14 of 74 mutants, two of which survived. Every case below is a
defect that actually shipped in it (2026-07-27), not a hypothetical:

  * `--max` used `len // max`, which floors to 1 whenever max < N < 2*max, turning
    a claimed "even stride" into a pure HEAD slice — and mutants are line-sorted,
    so the silently dropped tail was the newest, least-covered code;
  * truncation was not reported at all, so a partial run read as complete;
  * `changed_lines()` returned None on git failure and the caller tested `if not
    changed:`, so a bad --ref or an UNTRACKED target printed green having mutated
    nothing (all three files shipped that day were untracked);
  * `span()` summed character lengths while ast's col_offset is a UTF-8 BYTE
    offset, so any non-ASCII earlier on a line (this codebase is full of — → …)
    slid every span on it, producing garbage mutants that failed to compile and
    were scored as KILLS, inflating the coverage claim;
  * the lock was `if exists(): ... write()` — TOCTOU; two concurrent runs both
    proceeded, and one can bake the other's mutant in as "pristine".

It also rewrites a source file IN PLACE, and the hourly pipeline sweep invokes
build_video.py, so "leaves a mutant on disk" is a production risk, not a tidiness
one. Restoration is asserted here by SHA, not assumed.

Run: python3 tests/test_mutate.py
"""

import hashlib
import importlib.util
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
SRC_MUT = REPO / "scripts" / "mutate.py"

# ISOLATION (round 18, 2026-07-28 — overruling the round-17 scope call that this
# suite could write directly to the REAL lock because it owns the mechanism).
# This suite used to import and spawn REAL scripts/mutate.py, sharing its REAL
# lock: REPO/.mutate.lock. Every "if LIVE: skip" branch below was a
# snapshot-then-TOCTOU workaround for that sharing (`LIVE = mut.LOCK.exists()`
# taken once, ~0.4s before the write sites) — and a run landing between the
# snapshot and a write site clobbered, and in four spots UNLINKED, an external
# run's live lock. Reproduced: a real holder's lock ('16828 build_video.py') was
# overwritten within 0.08s and unlinked by 0.22s, a second run then acquired it,
# and 6 parallel runs of this suite left a mutant (`if True:` -> `if False:`)
# baked into a sibling target on disk. In this repo the files under that lock
# are build_video.py and pipeline_orchestrator.py, both hourly launchd targets.
#
# Fix: give this suite its OWN mutate.py, copied into a private mkdtemp tree, so
# `LOCK = pathlib.Path(__file__).resolve().parent.parent / ".mutate.lock"`
# resolves inside the tempdir, never REPO/.mutate.lock. That makes "another run
# might hold OUR lock" structurally impossible — there is no other process that
# has ever seen this path — so the LIVE snapshot, the four TOCTOU branches, the
# `"selftest" in read_text()` ownership sniffing, and the LOCK_SKIP/rc-75 exit
# are deleted rather than guarded. tests/test_precheck.py needs no change: its
# LOCK_SKIP scenario is a synthetic fixture, never a real mutate.py run.
tmp = pathlib.Path(tempfile.mkdtemp(prefix="mt_"))
PRIV_SCRIPTS = tmp / "scripts"
PRIV_SCRIPTS.mkdir()
MUT = PRIV_SCRIPTS / "mutate.py"
shutil.copy2(SRC_MUT, MUT)
spec = importlib.util.spec_from_file_location("mut", MUT)
mut = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mut)

FAILS = []


def check(name, cond, detail=""):
    if cond:
        print(f"  [ok ] {name}")
    else:
        print(f"  [FAIL] {name} {detail}")
        FAILS.append(name)


def main():
    print("mutate.py")
    # git may be absent (an export, a tarball, a worktree-less CI): --diff-only then
    # falls back to whole-file mode, so that check is about git, not about mutate.py.
    HAS_GIT = subprocess.run(["git", "rev-parse", "--git-dir"], cwd=str(REPO),
                             capture_output=True).returncode == 0
    try:
        # --- span(): BYTE offsets, not character offsets ----------------------
        # The em-dash before the comparison is the whole point: with character
        # offsets the extracted text slides right and comes out as garbage.
        src = 'x = 1\n# note — here\nif len(src) > 90:  # dash — inside\n    pass\n'
        got = {lbl.split(":")[0]: lbl for lbl, _ln, _new in mut.mutants(src)}
        texts = [lbl for lbl in got.values()]
        check("non-ASCII line: extracted text is the real token",
              any("'len(src) > 90'" in t for t in texts), f"got={texts}")

        # Every generated mutant must COMPILE. A non-compiling mutant makes every
        # suite error out and is scored as a kill — a false kill inflating coverage.
        bad = [lbl for lbl, _ln, new in mut.mutants(src)
               if _nocompile(new)]
        check("no mutant fails to compile", not bad, f"bad={bad}")

        # --- SIGNAL SAFETY: os.kill/signal.* int args must never be mutated ---
        # int+1 on the `0` in os.kill(pid, 0) turns a liveness PROBE into a REAL
        # SIGHUP delivered to whatever pid is in the lock file. During a gate run
        # that pid is the harness's own process tree -- this killed the harness
        # three times against this exact code (build_video.py:1657, mutated
        # while mutate.py tested build_video.py): twice in one session, once in
        # a reviewer's independent re-run, each time leaving build_video.py
        # genuinely mutated on disk. Assert on the GENERATED MUTANT LIST only --
        # never execute a mutant here, that is the exact side effect this test
        # exists to prevent.
        sig_src = ("import os, signal\n"
                   "def probe(holder):\n"
                   "    os.kill(int(holder), 0)\n"
                   "    signal.alarm(5)\n"
                   "    foo(300)\n"
                   "    a.b.c(7)\n"
                   "    return holder + 1\n")
        sig_labels = [lbl for lbl, _ln, _new in mut.mutants(sig_src)]
        check("no mutant touches os.kill's signal arg (the '0')",
              not any(lbl.startswith("int+1: '0'") for lbl in sig_labels),
              f"labels={sig_labels}")
        check("no mutant touches signal.alarm's int arg (the '5')",
              not any(lbl.startswith("int+1: '5'") for lbl in sig_labels),
              f"labels={sig_labels}")
        check("an unrelated int literal is still mutated (exclusion isn't over-broad)",
              any(lbl.startswith("int+1: '1'") for lbl in sig_labels),
              f"labels={sig_labels}")
        # MEDIUM-3 (round 20): a bare-name call that is NOT kill/killpg (e.g.
        # `foo(300)`) must still have its int literal mutated. `_is_signal_call`'s
        # last branch is `isinstance(f, ast.Name) and f.id in ("kill", "killpg")`
        # -- flipping that `and` to `or` makes EVERY bare-name call match (since
        # isinstance(f, ast.Name) alone is True for any `name(...)` call),
        # silently excluding every such int arg from mutation with no error and
        # no drop in the printed "N mutants" count (the mutants are never
        # GENERATED, so the N-vs-N cross-check can't see it either). `holder + 1`
        # is a BinOp, not a call argument, so it cannot exercise this branch --
        # this is the one fixture in this file that can.
        check("an int literal in an unrelated bare-name call is still mutated",
              any(lbl.startswith("int+1: '300'") for lbl in sig_labels),
              f"labels={sig_labels}")
        # LOW-C (round 22): `isinstance(f.value, ast.Name) and f.value.id ==
        # "signal"` -- flipping that `and` to `or` survived every fixture above
        # because they're all single-level Attribute calls (os.kill, os.killpg,
        # signal.alarm) or bare Names, so f.value is always a Name and the
        # isinstance check is always True -- the `or` mutation is never
        # observably different there. `a.b.c(7)`'s f.value (the `b` of `a.b`)
        # is itself an Attribute, not a Name, so isinstance(...) is False here
        # -- the only fixture that puts a non-Name on that branch. Under the
        # `or` mutation this forces evaluation of `f.value.id` on an Attribute
        # node (AttributeError, no `.id` field), crashing mutate.py instead of
        # silently passing; on a real target this shows up as precheck BLOCKED,
        # not a false green.
        check("an int literal in a nested-attribute call is still mutated",
              any(lbl.startswith("int+1: '7'") for lbl in sig_labels),
              f"labels={sig_labels}")

        # --- round-14 finding: os.killpg was NOT covered despite the comment ---
        # claiming it was. killpg signals a whole process GROUP -- strictly worse
        # than the single-pid os.kill incident above. Same non-execution method:
        # assert on the generated mutant list only.
        killpg_src = ("import os\n"
                      "def reap(pgid):\n"
                      "    os.killpg(pgid, 9)\n"
                      "    return pgid + 1\n")
        killpg_labels = [lbl for lbl, _ln, _new in mut.mutants(killpg_src)]
        check("no mutant touches os.killpg's signal arg (the '9')",
              not any(lbl.startswith("int+1: '9'") for lbl in killpg_labels),
              f"labels={killpg_labels}")
        check("...and an unrelated int literal near killpg is still mutated",
              any(lbl.startswith("int+1: '1'") for lbl in killpg_labels),
              f"labels={killpg_labels}")

        # --- the head-slice bug ----------------------------------------------
        # Call mut.select — an earlier cut recomputed the formula LOCALLY and
        # asserted on its own copy, so reverting mutate.py to floor division left
        # this suite green. The headline guard pinned a copy, not the code.
        n, mx = 74, 60                       # the exact shape that shipped broken
        items = list(range(n))
        sel, skipped = mut.select(items, mx)
        check("stride samples the TAIL, not a head slice",
              max(sel) > mx - 1, f"max_selected={max(sel)} of {n}")
        check("skipped count is reported", skipped == n - len(sel) and skipped > 0,
              f"skipped={skipped} len(sel)={len(sel)}")
        # The floor version, for contrast: it would have stopped dead at index 59.
        check("floor stride WOULD have head-sliced (the shipped bug)",
              max(items[::max(1, n // mx)][:mx]) == mx - 1)
        check("no cap -> everything selected, nothing skipped",
              mut.select(items, None) == (items, 0))
        check("cap >= N -> nothing skipped", mut.select(items, n)[1] == 0)

        # --- --diff-only on an untracked target must NOT report success -------
        # It verified nothing. All three files shipped that day were untracked.
        # The victim must live INSIDE the repo: for a path outside it, git diff
        # errors, changed_lines returns None, and mutate.py correctly falls back to
        # whole-file mode. The case being pinned is the other one — a file git
        # tracks nothing about, where --diff-only verifies NOTHING.
        # PID-scoped name: the victim must live INSIDE the repo (see comment above)
        # so it can't move into the private mkdtemp — but a fixed name still races
        # two CONCURRENT test_mutate.py processes against each other's fixture
        # (unrelated to the HIGH-1 lock bug, caught while stress-testing this fix).
        victim = REPO / f"_mutate_selftest_victim_{os.getpid()}.py"
        victim.write_text("def f(x):\n    if x > 1:\n        return 1\n    return 0\n")
        t_ok = tmp / "t_ok.py"
        t_ok.write_text("import sys\nsys.exit(0)\n")
        if not HAS_GIT:
            print("  [skip] untracked --diff-only check (no git)")
        else:
            r = subprocess.run([sys.executable, str(MUT), "--diff-only",
                                str(victim), str(t_ok)],
                               capture_output=True, text=True, cwd=str(REPO))
            check("untracked target under --diff-only exits 2, not 0",
                  r.returncode == 2, f"rc={r.returncode} out={(r.stdout+r.stderr)[-200:]}")

        # A CAPPED run must refuse to certify: it exits 2, never 0. The shipped
        # bug printed the truncation note and still returned 0 with "genuinely
        # covers the changed lines" — a false green wearing a warning label.
        big = tmp / "big.py"
        big.write_text("".join(f"def f{i}(x):\n    if x > {i}:\n        return {i}\n"
                               for i in range(40)))
        r_cap = subprocess.run([sys.executable, str(MUT), "--max", "3",
                                str(big), str(t_ok)],
                               capture_output=True, text=True, cwd=str(REPO))
        check("a capped (partial) run exits 2, not 0", r_cap.returncode == 2,
              f"rc={r_cap.returncode}")
        check("...and does not claim coverage",
              "genuinely covers" not in r_cap.stdout, f"out={r_cap.stdout[-160:]}")

        # --- restore + lock hygiene on a NORMAL run ---------------------------
        before = hashlib.sha256(victim.read_bytes()).hexdigest()
        r = subprocess.run([sys.executable, str(MUT), str(victim), str(t_ok)],
                           capture_output=True, text=True, cwd=str(REPO))
        after = hashlib.sha256(victim.read_bytes()).hexdigest()
        check("target restored byte-identical after a run", before == after)
        check("lockfile removed after a run", not mut.LOCK.exists())
        # Use the PRODUCTION helper, never a re-derived path: this asserted
        # `<repo>/.mutate.orig`, which nothing has created since the sidecar
        # became per-target — so it was vacuously true in every run and stayed
        # green while the sidecar cleanup was deleted outright. Post-guard a
        # leaked sidecar makes build_video.py hard-refuse on EVERY invocation
        # (sweep, orchestrator, human shell), and *.mutate.orig is gitignored,
        # so nothing else would surface it.
        check("sidecar removed after a run", not mut._sidecar(victim).exists())
        # _atomic writes a sibling of the TARGET, and the target is in the repo
        # root. An earlier cut globbed the mkdtemp (which only ever holds t_ok.py)
        # and printed [ok] unconditionally. A later cut globbed the whole REPO
        # (`*.mut`) instead — which is this process's OWN sibling check done as a
        # blanket scan, so a second test_mutate.py process's in-flight victim.mut
        # (mid-_atomic-write on ITS OWN pid-scoped victim) transiently satisfied
        # it and failed this run for a file it doesn't own. Check the two exact
        # sibling paths for THIS run's own victim instead.
        check("no .mut/.mutrestore temp left beside the target",
              not victim.with_suffix(victim.suffix + ".mut").exists()
              and not victim.with_suffix(victim.suffix + ".mutrestore").exists())
        # A suite that always passes means every mutant survives -> rc 1, never 0.
        check("all-surviving run exits 1", r.returncode == 1, f"rc={r.returncode}")

        # --- lock is exclusive (was TOCTOU against a SHARED lock) --------------
        # This is now OUR private lock, in a mkdtemp no other process has ever
        # seen — so unlike the pre-isolation version, there is nothing to check
        # for "a live run holds it" and nothing to sniff before unlinking.
        assert not mut.LOCK.exists(), "private lock pre-held — isolation broken"
        mut.LOCK.write_text(f"{os.getpid()} selftest\n")
        try:
            r2 = subprocess.run([sys.executable, str(MUT), str(victim), str(t_ok)],
                                capture_output=True, text=True, cwd=str(REPO))
            check("a held lock refuses the run", r2.returncode == 2,
                  f"rc={r2.returncode}")
            # A LIVE holder must read as live — never tell someone to rm it.
            check("...and a live pid reads as RUNNING, with no rm advice",
                  "RUNNING" in (r2.stdout + r2.stderr)
                  and "rm " not in (r2.stdout + r2.stderr),
                  f"out={(r2.stdout + r2.stderr)[:200]}")
            check("...and the refusal does NOT delete the holder's lock",
                  mut.LOCK.exists())
        finally:
            mut.LOCK.unlink(missing_ok=True)

        # --- post-crash residue: REFUSE, never restore (the round-7 critical) --
        # A run that dies mid-mutation leaves <target>.mutate.orig. An earlier cut
        # "helpfully" auto-restored from it — but the sidecar was one shared path
        # with no record of its target, so a crashed run on alpha followed by a run
        # on beta OVERWROTE BETA WITH ALPHA'S SOURCE (reproduced), left alpha still
        # mutated, and deleted alpha's only recovery copy. It also discarded
        # legitimate uncommitted post-crash edits. The gate must refuse and point
        # at the file, never write.
        alpha = REPO / f"_mutate_selftest_alpha_{os.getpid()}.py"
        beta = REPO / f"_mutate_selftest_beta_{os.getpid()}.py"
        try:
            beta_src = "BETA_MUST_SURVIVE = 'irreplaceable'\nif True:\n    pass\n"
            beta.write_text(beta_src)
            alpha.write_text("ALPHA = 1\nif ALPHA > 0:\n    pass\n")
            # simulate a crash on alpha: its sidecar survives, alpha left mutated
            mut._sidecar(alpha).write_text(alpha.read_text())
            alpha.write_text("ALPHA = 1\nif False:  # MUTATED\n    pass\n")

            r3 = subprocess.run([sys.executable, str(MUT), str(beta), str(t_ok)],
                                capture_output=True, text=True, cwd=str(REPO))
            check("alpha's crash residue does NOT touch an unrelated target beta",
                  beta.read_text() == beta_src, f"beta={beta.read_text()!r}")
            check("...and beta's own run is unaffected by it", r3.returncode == 1,
                  f"rc={r3.returncode}")
            check("...and alpha's recovery copy survives", mut._sidecar(alpha).is_file())

            r4 = subprocess.run([sys.executable, str(MUT), str(alpha), str(t_ok)],
                                capture_output=True, text=True, cwd=str(REPO))
            check("a run on the CRASHED target refuses (rc 2), does not auto-write",
                  r4.returncode == 2, f"rc={r4.returncode}")
            check("...and leaves the mutated file exactly as found, for the operator",
                  "MUTATED" in alpha.read_text())
            check("...naming the sidecar in the error",
                  "mutate.orig" in (r4.stdout + r4.stderr))
            # THE WEDGE: the refusal takes the lock, so returning without
            # releasing it left a dead-pid lock that blocked every later run on
            # EVERY target — and the recovery the message prints never mentions
            # the lock. Since the gate is mandatory, that stopped all commits.
            check("...and RELEASES the lock it took (else the tree wedges)",
                  not mut.LOCK.exists())
            mut._sidecar(alpha).unlink(missing_ok=True)
            r5 = subprocess.run([sys.executable, str(MUT), str(alpha), str(t_ok)],
                                capture_output=True, text=True, cwd=str(REPO))
            check("...so the documented recovery actually unblocks the gate",
                  r5.returncode == 1, f"rc={r5.returncode}")
        finally:
            mut.LOCK.unlink(missing_ok=True)
            for f in (alpha, beta, mut._sidecar(alpha), mut._sidecar(beta)):
                f.unlink(missing_ok=True)

        # --- STALE lock after SIGKILL: diagnose it, and say `rm` ---------------
        # atexit and the signal handlers cannot run on SIGKILL/OOM/power-loss, so
        # the lock outlives the process. Every later run on ANY target then got
        # "another mutation run is active" while nothing was, precheck mapped that
        # to BLOCKED, and since the gate is mandatory the whole tree wedged — with
        # no message ever naming the lock file or `rm`.
        mut.LOCK.write_text("999999 selftest\n")   # a pid that cannot exist
        try:
            r6 = subprocess.run([sys.executable, str(MUT), str(victim), str(t_ok)],
                                capture_output=True, text=True, cwd=str(REPO))
            out6 = r6.stdout + r6.stderr
            check("a DEAD-pid lock is reported as STALE", "STALE" in out6,
                  f"out={out6[:200]}")
            check("...and the message names `rm <lock>` so the tree unwedges",
                  "rm " in out6 and ".mutate.lock" in out6, f"out={out6[:200]}")
        finally:
            mut.LOCK.unlink(missing_ok=True)

        # --- PermissionError lock holder: LIVE, not STALE (round-N critical) ---
        # os.kill(pid, 0) on a process you don't own (root, another user) raises
        # PermissionError, which IS an OSError subclass — the original tuple
        # caught it as "process gone" and reported a running holder as STALE with
        # `rm` advice, which lets a second run mutate the tree concurrently with
        # the first. pid 1 (launchd) is unambiguously alive and unsignalable by a
        # non-root user on every box this runs on.
        mut.LOCK.write_text("1 selftest\n")   # pid 1 = launchd, always alive
        try:
            r9 = subprocess.run([sys.executable, str(MUT), str(victim), str(t_ok)],
                                capture_output=True, text=True, cwd=str(REPO))
            out9 = r9.stdout + r9.stderr
            check("a PermissionError (root-owned pid) reads as RUNNING, not STALE",
                  "RUNNING" in out9 and "STALE" not in out9, f"out={out9[:200]}")
            check("...and gives no `rm` advice for a live holder",
                  "rm " not in out9, f"out={out9[:200]}")
        finally:
            mut.LOCK.unlink(missing_ok=True)

        # --- unreadable lock (empty / garbled): unknown, NOT confidently STALE --
        # main() writes the lock in two syscalls — open, THEN write the pid — so
        # an EMPTY lock file is the state of a healthy, JUST-acquired live lock,
        # not a dead one. Same for whitespace-only or a garbled "notapid target".
        # Before this fix (LOW-1) both landed in the same except tuple as a
        # confirmed-dead pid and printed "STALE... Clear it: rm" — the exact
        # confident-wrong-remedy bug MED-3 fixed for the PermissionError case,
        # for a DIFFERENT unreadable-state input.
        mut.LOCK.write_text("")   # empty: the just-acquired, healthy-live state
        try:
            r7 = subprocess.run([sys.executable, str(MUT), str(victim), str(t_ok)],
                                capture_output=True, text=True, cwd=str(REPO))
            out7 = r7.stdout + r7.stderr
            check("an EMPTY lock is reported unreadable, not confidently STALE",
                  "STALE" not in out7 and "unreadable" in out7, f"out={out7[:200]}")
        finally:
            mut.LOCK.unlink(missing_ok=True)
        mut.LOCK.write_text("notapid target\n")   # garbled: unparseable pid
        try:
            r8 = subprocess.run([sys.executable, str(MUT), str(victim), str(t_ok)],
                                capture_output=True, text=True, cwd=str(REPO))
            out8 = r8.stdout + r8.stderr
            check("a GARBLED lock is reported unreadable, not confidently STALE",
                  "STALE" not in out8 and "unreadable" in out8, f"out={out8[:200]}")
        finally:
            mut.LOCK.unlink(missing_ok=True)

        # --- red baseline must ABORT, never be scored as mutants killed --------
        # Reverting `if not run(...): ... return 2` to `if False:` leaves this
        # unfixtured: a suite that fails regardless of the code then scores every
        # mutant "killed" for free (`6 MUTANT(S) KILLED`, rc=0).
        always_red = tmp / "always_red.py"
        always_red.write_text("import sys\nsys.exit(1)\n")
        r10 = subprocess.run([sys.executable, str(MUT), str(victim), str(always_red)],
                             capture_output=True, text=True, cwd=str(REPO))
        check("a red baseline aborts (rc 2), never scored as mutants killed",
              r10.returncode == 2, f"rc={r10.returncode}")
        check("...and names the reason",
              "RED before any mutation" in (r10.stdout + r10.stderr))

        # --- HarnessError: a real spawn-level OSError must abort, never score --
        # Reverting `raise HarnessError(...)` to `return False` (MED-5) makes a
        # broken SK_MUTATION_RUN or an EMFILE mid-loop score every mutant KILLED
        # instead of aborting. Call the PRODUCTION run() directly (not a
        # subprocess re-invocation — a huge argv here would blow ARG_MAX on the
        # test's OWN spawn too, before it ever reaches mutate.py) with a path so
        # long that the exec syscall inside it genuinely raises OSError E7
        # (Argument list too long) — a real spawn-level failure, not a mock, of
        # exactly the class run() exists to distinguish from "the suite ran and
        # failed": ProcessLookupError/E2BIG/EMFILE all arrive here as OSError.
        huge_path = "x" * (2 * 1024 * 1024)
        try:
            mut.run([huge_path], 5)
            check("run() raises HarnessError on a real spawn-level OSError", False,
                  "no exception raised — the mutant is scored as a pass silently")
        except mut.HarnessError:
            check("run() raises HarnessError on a real spawn-level OSError", True)
        except Exception as e:
            check("run() raises HarnessError on a real spawn-level OSError", False,
                  f"wrong exception: {type(e).__name__}: {e}")

        # --- equiv marker requires a real reason ------------------------------
        s2 = "# mutequiv:\nif x:\n    pass\n"
        check("bare marker with no reason is not accepted", mut.equiv_reason(s2, 2) == "")
        s3 = "# mutequiv: message truncation only, no behaviour\nif x:\n    pass\n"
        check("marker with a reason is accepted", mut.equiv_reason(s3, 2) != "")
    finally:
        # Re-derive, don't reference the local `victim` — an exception raised
        # before that assignment would make this a NameError that masks the
        # real one, and skips the tmp cleanup below it.
        (REPO / f"_mutate_selftest_victim_{os.getpid()}.py").unlink(missing_ok=True)
        shutil.rmtree(tmp, ignore_errors=True)

    if FAILS:
        print(f"\ntest_mutate: FAIL ({len(FAILS)}): {', '.join(FAILS)}")
        return 1
    print("\ntest_mutate: PASS")
    return 0


def _nocompile(src):
    try:
        compile(src, "<m>", "exec")
        return False
    except SyntaxError:
        return True


if __name__ == "__main__":
    sys.exit(main())
