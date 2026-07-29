#!/usr/bin/env python3
"""Deterministic mutation tester: prove the suite actually SEES the code.

WHY THIS EXISTS (2026-07-27, Steve). A green suite says nothing about whether it
would notice the code being wrong. Every real defect found in that day's five
review rounds had a green suite sitting next to it, and hand-run mutations kept
finding checks that were decoration:

  * the whole `"brief" in name` discriminator on a $9 spend gate could be DELETED
    with both suites still green — no fixture had a non-brief file;
  * `if False:` at that gate's call site (the gate simply never firing) — green;
  * replacing document order with sorted order in the kit-ordinal derivation,
    the exact bug class that remaps 68 of 76 shots — green;
  * a fixture that "pinned" a mixed-scheme manifest was actually dying on a
    different guard, so deleting the one it named changed nothing.

A mutant that SURVIVES means: this line could be wrong in production and nothing
would tell you. That is a coverage hole, not a style note.

Operators are the standard set, chosen because each is unambiguous under `ast`
(no regex guessing about what is code vs. a string): negate a comparison, force
an `if` test to False/True, flip and/or, bump an integer literal.

  scripts/mutate.py build_video.py tests/test_derive_shots.py [more tests...]
  scripts/mutate.py --max 40 scripts/pipeline_orchestrator.py tests/*.py

Exit 0 = every mutant killed. Exit 1 = survivors (listed with file:line).

SAFETY. This rewrites the target file in place and restores it. Restoration is
belt-and-braces — try/finally + atexit + SIGINT/SIGTERM — and the SHA is verified
at exit. A lockfile prevents two agents mutating the same tree at once, which has
already happened once and can leave a mutated source on disk if either crashes.
"""

import argparse
import ast
import atexit
import hashlib
import os
import pathlib
import signal
import subprocess
import sys

LOCK = pathlib.Path(__file__).resolve().parent.parent / ".mutate.lock"


class HarnessError(RuntimeError):
    """The harness itself failed. Never scored as a kill — see run()."""


def _sidecar(target):
    """Pristine-copy path for THIS target. Keyed to the file, never a single shared
    path — a shared one cannot tell "this target is mutated" from "this is a
    different file", and restoring on that basis overwrites unrelated sources."""
    return target.with_suffix(target.suffix + ".mutate.orig")


def _atomic(path, text):
    """Write via tmp + os.replace. write_text truncates in place, so an interrupted
    write leaves a half-file where a source used to be.

    PRESERVES THE MODE. os.replace swaps in a freshly created file (default 0644),
    so every mutate/restore cycle silently stripped the exec bit off any 0755
    target — the mandatory gate quietly un-chmod'ing the repo one file per run,
    and re-dirtying `git diff --summary` with `100755 => 100644` after any manual
    fix. Nothing caught it: the integrity check at exit compares the sha, and the
    content is byte-identical — only the mode changed. Fixed here rather than in
    restore(), because the mutation loop restores through THIS function and never
    reaches restore()'s branch on a clean run. Round-7 review, 2026-07-29."""
    try:
        mode = path.stat().st_mode
    except OSError:
        mode = None
    tmp = path.with_suffix(path.suffix + ".mut")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    if mode is not None:
        os.chmod(path, mode)


def sha(p):
    return hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()


def span(src_lines, node):
    """(start, end) BYTE offsets of a node in the joined source.

    ast's col_offset is a UTF-8 BYTE offset, not a character index. Summing
    character lengths shifts every span on any line containing non-ASCII — and
    this codebase is full of `—`, `→`, `…` and emoji in f-strings and comments.
    The symptom is silent and ugly: a span slides, the extracted text is garbage
    (`'len(clipped) > 90'` came out as `'n(clipped) > 90 e'`), and the resulting
    mutant either fails to compile — scored as a KILL, inflating the coverage
    claim — or mutates a different token under a correct-looking label.
    """
    starts = [0]
    for ln in src_lines:
        starts.append(starts[-1] + len(ln.encode("utf-8")))
    return (starts[node.lineno - 1] + node.col_offset,
            starts[node.end_lineno - 1] + node.end_col_offset)


def mutants(src):
    """[(label, lineno, new_source)] — one entry per mutation, deterministic order."""
    lines = src.splitlines(keepends=True)
    raw = src.encode("utf-8")          # spans are byte offsets — see span()
    tree = ast.parse(src)
    out = []

    # SIGNAL SAFETY (2026-07-27). int+1 on os.kill(pid, 0)'s signal argument turns
    # a liveness PROBE (signal 0 — a harmless "does this pid exist" no-op) into
    # os.kill(pid, 1) — a REAL SIGHUP delivered to whatever pid is in the lock
    # file. During a gate run that pid is the harness's own process tree, so the
    # mutant kills its own runner: atexit/finally never complete, the lock and
    # the mutated target are left on disk. This is not hypothetical — it happened
    # three times against this exact line (build_video.py's copy of this probe,
    # mutated while mutate.py tested build_video.py): twice in one agent's
    # session, once independently in a reviewer's re-run, each time leaving
    # build_video.py genuinely mutated on disk and requiring manual sidecar
    # recovery. An int argument to a call that can deliver a real OS signal is
    # not a test of program logic, it is a side effect on an unrelated process —
    # never generate a mutant there. Scoped narrowly: only the direct args/kwargs
    # of a call named `kill` (os.kill, os.killpg, Popen.kill, ...) or any
    # `signal.*` call (signal.signal, signal.alarm, signal.pthread_kill, ...).
    # Everything else int+1 touches keeps mutating normally.
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    def _is_signal_call(call):
        f = call.func
        if isinstance(f, ast.Attribute):
            # EXACT membership against the two literal names below, never a
            # prefix/substring test (round-16 finding, 2026-07-27: an earlier
            # version of this comment described the match loosely enough to be
            # misread as "anything named kill*"). `self.kill(3)` and any other
            # `.kill`-prefixed-but-different attr correctly do NOT match here;
            # only f.attr == "kill" or f.attr == "killpg" do. "kill" AND
            # "killpg" both matter: os.killpg(pgid, sig) signals a whole
            # process GROUP, strictly worse than the single-pid os.kill
            # incident this exclusion exists for. f.attr == "kill" alone
            # silently let killpg through despite an even earlier version of
            # this comment claiming otherwise (round-14 finding).
            return f.attr in ("kill", "killpg") or (isinstance(f.value, ast.Name)
                                                      and f.value.id == "signal")
        return isinstance(f, ast.Name) and f.id in ("kill", "killpg")

    def _is_signal_arg(node):
        parent = parents.get(node)
        if not isinstance(parent, ast.Call) or not _is_signal_call(parent):
            return False
        return node in parent.args or any(kw.value is node for kw in parent.keywords)

    def text(node):
        a, b = span(lines, node)
        return raw[a:b].decode("utf-8")

    def emit(node, replacement, label):
        a, b = span(lines, node)
        original = raw[a:b].decode("utf-8")
        if original == replacement:
            return
        new = (raw[:a] + replacement.encode("utf-8") + raw[b:]).decode("utf-8")
        try:
            compile(new, "<mutant>", "exec")   # a non-compiling mutant is a FALSE
        except SyntaxError:                    # kill: every suite errors out.
            return
        out.append((f"{label}: {original[:44]!r} -> {replacement[:44]!r}",
                    node.lineno, new))

    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            emit(node.test, "False", "if-test->False")
            emit(node.test, "True", "if-test->True")
        elif isinstance(node, ast.Compare):
            emit(node, f"(not ({text(node)}))", "negate-compare")
        elif isinstance(node, ast.BoolOp):
            txt = text(node)
            swap = (txt.replace(" and ", " or ") if isinstance(node.op, ast.And)
                    else txt.replace(" or ", " and "))
            emit(node, swap, "flip-bool")
        elif isinstance(node, ast.Constant) and isinstance(node.value, int) \
                and not isinstance(node.value, bool):
            if _is_signal_arg(node):   # see SIGNAL SAFETY comment above
                continue
            # LOW (round 14, noted not chased): the exclusion above is depth-1 —
            # it only catches a bare int literal passed DIRECTLY as a kill/killpg/
            # signal.* arg. `os.kill(pid, 0 if quiet else 9)`, `os.kill(pid, 0 + 0)`
            # or `os.kill(*args)` would still generate a live-signal mutant. No
            # in-tree call is shaped that way today. Same blind spot, same
            # decision: os.chmod(p, 0o600) -> os.chmod(p, 385) is still generated
            # (int+1 on an octal literal), and youtube_client.py chmods an OAuth
            # token file — harmless today (no suite imports it), not excluded,
            # because chasing every side-effecting call by name is an unbounded
            # list. If either shape appears in a call this generator can reach,
            # widen _is_signal_call / add a chmod exclusion then — don't add one
            # blind now for a call that doesn't exist yet.
            emit(node, str(node.value + 1), "int+1")
    # Sort for stability across runs, then dedupe identical resulting sources.
    seen, uniq = set(), []
    for label, lineno, new in sorted(out, key=lambda t: (t[1], t[0])):
        h = hashlib.sha256(new.encode()).hexdigest()
        if h not in seen:
            seen.add(h)
            uniq.append((label, lineno, new))
    return uniq


def select(all_m, cap):
    """(selected, skipped) — an EVEN stride over the mutant list, never a head slice.

    `len // cap` floors to 1 whenever cap < N < 2*cap, which silently turns this
    into `all_m[:cap]`. Mutants are line-sorted, so the dropped tail is the newest,
    least-covered code — that shipped, and skipped 14 of 74 with 2 survivors in the
    gap. Exposed as a function so its own suite pins THIS arithmetic rather than a
    copy of it (a test that recomputes the formula locally stays green when the
    formula regresses).
    """
    if cap is None or len(all_m) <= cap:
        return all_m, 0
    # Ceil stride: samples the whole range but UNDER-fills the cap (77/60 -> step 2
    # -> 39 selected). That is fine — a capped run now blocks rather than
    # certifying, so this only decides what a diagnostic partial run looks at.
    step = -(-len(all_m) // cap)
    sel = all_m[::step][:cap]
    return sel, len(all_m) - len(sel)


def changed_lines(target, ref):
    """Line numbers in `target` added/modified vs `ref`, from git's own hunk headers."""
    out = subprocess.run(["git", "diff", "-U0", ref, "--", str(target)],
                         capture_output=True, text=True)
    if out.returncode != 0:
        print(f"warning: git diff vs {ref} failed — mutating the whole file instead.",
              file=sys.stderr)
        return None
    lines = set()
    for h in out.stdout.splitlines():
        if h.startswith("@@"):
            # @@ -old,n +new,m @@   — the +side is what exists now.
            plus = h.split("+")[1].split("@@")[0].strip()
            start, _, count = plus.partition(",")
            lines.update(range(int(start), int(start) + int(count or 1)))
    return lines


_EQUIV = "mutequiv:"


def equiv_reason(src, lineno):
    """The justification on an accepted-equivalent marker at/above `lineno`, else "".

    An EQUIVALENT MUTANT is a change with no observable behaviour — a message
    truncation cap, a guard on a map nothing reads. No fixture can kill it, so
    without an escape hatch the gate stays red forever and gets bypassed, which
    is worse than no gate. The escape is deliberately awkward: the reason has to
    live on the line itself, in the diff, where a reviewer sees it —

        preview = ...[:5]   # mutequiv: message truncation only, no behaviour

    A marker with no reason after it is not accepted. Accepted ones are always
    PRINTED, so a wrong "equivalent" call stays visible instead of disappearing.
    """
    lines = src.splitlines()
    # The code line itself, then upward through the contiguous comment block
    # directly above it — a justification usually needs more than a trailing
    # fragment, and scanning a fixed one or two lines silently ignored it.
    idx = lineno - 1
    if not (0 <= idx < len(lines)):
        return ""
    candidates = [lines[idx]]
    n = idx - 1
    while n >= 0 and lines[n].lstrip().startswith("#"):
        candidates.append(lines[n])
        n -= 1
    for ln in candidates:
        if _EQUIV in ln:
            reason = ln.split(_EQUIV, 1)[1].strip()
            if len(reason) >= 12:
                return reason
    return ""


def run(tests, timeout):
    """True if EVERY suite passes (i.e. the mutant survived).

    PYTHONDONTWRITEBYTECODE is not optional. The suites import their target via
    spec_from_file_location, which writes a .pyc that CPython validates on
    int(mtime) + byte SIZE. A length-preserving mutant ("5"->"6", "[:5]"->"[:6]")
    written in the same integer second as the cache is then loaded from STALE
    bytecode and reported as SURVIVED though the suite never saw it — the
    instrument returning different answers for identical input based on invisible
    timing. Reproduced: three such mutants scored SURVIVED with caching on and
    killed with it off."""
    for t in tests:
        try:
            if subprocess.run([sys.executable, t], capture_output=True,
                              timeout=timeout,
                              env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
                                       SK_MUTATION_RUN="1"),
                              ).returncode != 0:
                return False
        except subprocess.TimeoutExpired:
            return False       # a hang is a kill: the mutant changed behaviour
        except OSError as e:
            # "the suite could not RUN" is NOT "the suite noticed the mutant".
            # EMFILE / fork failure / disk-full during an 80-mutant loop would
            # otherwise silently score every affected mutant as killed and print
            # CLEARED — the same false-green shape as the head-slice, in the one
            # function whose entire job is to be trustworthy.
            raise HarnessError(f"could not run {t}: {e}") from e
    return True


def _relevance_ordered(tests, target):
    """Suites most likely to kill a mutant in `target`, first.

    run() short-circuits on the first FAILING suite, so ordering decides how much
    wall clock a killed mutant costs — not whether it is killed. Three tiers:
      1. name match  — test_<stem>.py for the target's stem (the usual killer)
      2. content match — the suite's source mentions the target's module name
      3. everything else, in the caller's original order

    Ordering is STABLE within each tier, so a run is reproducible.

    This never changes a verdict: a mutant is killed if ANY suite fails (an
    order-independent property), and a SURVIVOR still runs every suite before it
    is reported alive. Pinned by tests/test_mutate.py.
    """
    stem = pathlib.Path(target).stem
    named, mentions, rest = [], [], []
    for t in tests:
        if pathlib.Path(t).stem in (f"test_{stem}", f"{stem}_test"):
            named.append(t)
            continue
        try:
            body = pathlib.Path(t).read_text(encoding="utf-8", errors="replace")
        except OSError:
            # Unreadable here is NOT a reason to drop a suite — run() must still
            # execute it and fail loudly. Order it last rather than losing it.
            rest.append(t)
            continue
        (mentions if stem in body else rest).append(t)
    return named + mentions + rest


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("target")
    ap.add_argument("tests", nargs="+")
    ap.add_argument("--no-reorder", action="store_true",
                    help="Keep the caller's suite order (diagnostic; slower). "
                         "Reordering never changes the verdict, only the wall clock.")
    # Default None = NO CAP. --diff-only already scopes the run to the changed
    # lines, and a cap there is the difference between certifying coverage and
    # certifying a sample. A cap now BLOCKS rather than silently truncating.
    ap.add_argument("--max", type=int, default=None,
                    help="cap mutants (even stride). A capped run exits 2: it cannot "
                         "certify coverage.")
    ap.add_argument("--timeout", type=int, default=120)
    # store_true + a separate --ref, NOT nargs="?": an optional-value flag silently
    # swallows the following positional, so `--diff-only build_video.py tests/x.py`
    # parsed as ref=build_video.py, target=tests/x.py and mutated the wrong file.
    ap.add_argument("--diff-only", action="store_true",
                    help="only mutate lines changed vs --ref. The right mode for a "
                         "pre-review gate: whole-file runs report survivors in code "
                         "the change never touched, which trains you to ignore output.")
    ap.add_argument("--ref", default="HEAD", help="baseline for --diff-only")
    args = ap.parse_args()

    target = pathlib.Path(args.target)
    if target.suffix != ".py" or not target.is_file():
        print(f"error: target {target} is not an existing .py file", file=sys.stderr)
        return 2
    # LOCK FIRST, THEN READ. Reading the baseline before taking the lock means a
    # concurrent run's mutant can BE our "pristine" source, and our restore then
    # bakes it in permanently. O_CREAT|O_EXCL because `if exists(): ... write()`
    # is TOCTOU — two runs both cleared that check in testing.
    try:
        fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        # The lock records its pid — READ IT BACK. atexit and the signal handlers
        # cannot run on SIGKILL/OOM/power-loss, so the lock outlives the process and
        # every later run on ANY target gets "another mutation run is active" while
        # nothing is. precheck maps that to BLOCKED, and the gate is mandatory, so
        # the tree wedges — and the message never mentioned the lock file or `rm`.
        # Report, never auto-clear: a live holder must stay protected.
        holder, alive, unreadable = "", True, False
        try:
            holder = LOCK.read_text().split()[0]
            os.kill(int(holder), 0)
        except PermissionError:
            # The process EXISTS but we can't signal it (root-owned, another
            # user — e.g. pid 1/launchd). That is still ALIVE, not stale: the
            # tuple below caught this as OSError and reported a live lock holder
            # as "STALE... Clear it: rm ..." — telling the operator to rm a lock
            # a running process holds, which is the exact two-runs-on-one-tree
            # corruption the lock exists to prevent.
            pass
        except (ValueError, IndexError):
            # LOW-1 (2026-07-27): main() writes the lock in two syscalls — open,
            # THEN write the pid — so an empty file (`.split()[0]` -> IndexError)
            # is the state of a healthy, JUST-acquired live lock, not a dead one.
            # Same for whitespace-only or a garbled `notapid target` (`int()` ->
            # ValueError). That is "holder unknown", not "confirmed not running" —
            # the same bug class as the MED-3 fix, so it gets the same non-committal
            # remedy instead of a confident STALE/rm.
            alive, unreadable = False, True
        except (ProcessLookupError, OSError):
            alive = False
        stale = _sidecar(pathlib.Path(args.target))
        hint = ""
        if stale.is_file():
            hint = (f"\n  Its target may still be MUTATED — a pristine copy is at "
                    f"{stale.name}. Compare before doing anything else.")
        if alive:
            print(f"error: {LOCK.name} is held by pid {holder}, which is RUNNING — "
                  f"another mutation run is active. Wait for it.{hint}",
                  file=sys.stderr)
        elif unreadable:
            print(f"error: {LOCK.name} content is unreadable ({holder!r} is not a "
                  f"parseable pid) — this can be a lock in the middle of being "
                  f"written, not a dead one. Holder unknown; rm only if you are "
                  f"certain nothing is running.{hint}", file=sys.stderr)
        else:
            print(f"error: {LOCK.name} is held by pid {holder or '?'}, which is NOT "
                  f"running — a previous run was killed. STALE lock.{hint}\n"
                  f"  Clear it:  rm {LOCK}", file=sys.stderr)
        return 2
    os.write(fd, f"{os.getpid()} {target}\n".encode())
    os.close(fd)

    # NO AUTO-RESTORE. Two rounds of trying produced two criticals and then dead
    # code: lock acquisition above already returned 2 if a stale lock exists, and if
    # the operator clears the lock to proceed there is no longer any evidence that
    # the difference is a crash rather than their own edit. Restoring on "the file
    # differs from a sidecar" overwrote an unrelated target with another file's
    # source, and silently discarded legitimate post-crash work.
    #
    # Refuse instead, and name the exact recovery. A leftover per-target sidecar is
    # unambiguous evidence that SOME run died mid-mutation on THIS file.
    sidecar_pre = _sidecar(target)
    if sidecar_pre.is_file():
        print(f"error: {sidecar_pre.name} exists — a previous run died mid-mutation "
              f"on {target.name}, so it may still be MUTATED on disk. Compare them, "
              f"restore if needed, then delete the sidecar:\n"
              f"    diff {sidecar_pre} {target}\n"
              f"    cp {sidecar_pre} {target}   # only if the diff is a mutation\n"
              f"    rm {sidecar_pre}", file=sys.stderr)
        # RELEASE the lock we just took. This check has to sit BELOW acquisition —
        # during a healthy run the sidecar exists by design, so checking above it
        # would report a live run as a crash — but returning here without
        # unlinking left a dead-pid lock behind, and the recovery this very message
        # prints does not mention it. That wedged the whole tree: every later run,
        # on ANY target, hit "another mutation run is active" and precheck mapped
        # it to BLOCKED, so no commit could pass the mandatory gate.
        LOCK.unlink(missing_ok=True)
        return 2

    src = target.read_text(encoding="utf-8")
    before = sha(target)
    # Capture the mode alongside the sha — restore() re-creates the file via
    # os.replace, which would otherwise drop the exec bit (see restore()).
    try:
        _mode_before = target.stat().st_mode
    except OSError:
        _mode_before = None
    # Sidecar pristine copy: signal handlers cannot run on SIGKILL or power loss,
    # so the only recovery for "mutant left on disk" is a copy that outlives the
    # process. The hourly pipeline sweep invokes build_video.py, so a mutated file
    # left behind would be used to render a real video.
    sidecar = _sidecar(target)
    sidecar.write_text(src, encoding="utf-8")

    def restore():
        # ponytail: no test drives this via the atexit/signal crash path (round
        # 20 mutate.py gate run: all 4 mutants at this function survive) — the
        # happy path restores inline at the bottom of run_gate/main instead, so
        # every real gate run exercises the read/write/unlink logic here anyway,
        # just not through atexit.register or the SIGINT/SIGTERM handler below.
        # Verified by hand instead of by fixture: SIGTERM/SIGINT during a run
        # restore cleanly (atexit + the handler's sys.exit(130) both fire this);
        # SIGKILL cannot run any Python handler at all, so it leaves the sidecar
        # + lock behind BY DESIGN (that's what the sidecar is for — see the
        # comment above). Add a subprocess-kill fixture if this path regresses.
        if target.exists() and sha(target) != before:
            # tmp + os.replace: write_text truncates in place, so an interrupted
            # write during RESTORE would destroy the source it is recovering.
            tmp = target.with_suffix(target.suffix + ".mutrestore")
            tmp.write_text(src, encoding="utf-8")
            os.replace(tmp, target)
            # Carry the ORIGINAL mode across. os.replace swaps in a fresh file
            # created at the default 0644, so restore silently stripped the exec
            # bit off every executable it mutated — the gate quietly un-chmod'ing
            # the repo, one file per run, and re-dirtying `git diff --summary`
            # with `100755 => 100644` after any manual fix. Nothing caught it
            # because the integrity assert below compares CONTENT (sha) only:
            # byte-identical, mode changed. Found 2026-07-29, round-7 review.
            if _mode_before is not None:
                os.chmod(target, _mode_before)
        sidecar.unlink(missing_ok=True)
        LOCK.unlink(missing_ok=True)

    atexit.register(restore)
    for s in (signal.SIGINT, signal.SIGTERM):
        signal.signal(s, lambda *_: sys.exit(130))

    # Order suites most-likely-killer-first. run() short-circuits on the FIRST
    # failing suite, so a mutant in commit_expense.py that its own suite would
    # kill in 0.1s instead burned ~20s of unrelated suites, because precheck
    # passes them in glob order (all of tests/ before scripts/). Measured: one
    # full pass is ~23s, dominated by test_quota_infra_classification (8.0s) and
    # test_derive_shots (5.3s); commit_expense's 43 mutants spent ~14 minutes
    # almost entirely on suites that could never have killed them.
    #
    # This CANNOT change any verdict: "killed by at least one suite" is
    # order-independent, and a SURVIVOR still runs every suite before it is
    # declared alive. Only the detection order — and therefore the wall clock —
    # changes. tests/test_mutate.py pins that reordering leaves results identical.
    if not args.no_reorder:
        args.tests = _relevance_ordered(args.tests, target)

    try:
      try:
        if not run(args.tests, args.timeout):
            print("error: the suite is RED before any mutation — fix that first; "
                  "mutation results are meaningless against a failing baseline.",
                  file=sys.stderr)
            return 2

        all_m = mutants(src)
        scope = len(src.splitlines())
        if args.diff_only:
            changed = changed_lines(target, args.ref)
            if changed is None:
                # git couldn't answer. changed_lines already warned that we would
                # mutate the whole file — so actually DO that. An earlier cut tested
                # `if not changed:` and returned 0, i.e. printed green having mutated
                # nothing, on a bad --ref or a detached HEAD.
                pass
            elif not changed:
                # An untracked or unchanged target under --diff-only is NOT a pass:
                # it means this run verified nothing. All three files shipped today
                # were untracked, and each would have reported a clean green.
                print(f"error: {target.name} has no lines changed vs {args.ref} "
                      f"(untracked, or identical) — --diff-only verified NOTHING. "
                      f"Drop --diff-only to mutate the whole file.", file=sys.stderr)
                return 2
            else:
                all_m = [m for m in all_m if m[1] in changed]
                scope = len(changed)
                print(f"restricted to {len(changed)} line(s) changed vs {args.ref}")
            if not all_m:
                print(f"\nNo mutable sites on the changed lines (comments, strings, "
                      f"or plain statements only). Mutation cannot speak to this "
                      f"change — rely on the suites.")
                return 0
        cap = args.max
        sel, skipped = select(all_m, cap)
        print(f"{target.name}: {len(all_m)} mutants, testing {len(sel)} "
              f"against {len(args.tests)} suite(s)")
        if skipped:
            # A partial run must BLOCK, not merely warn. An earlier cut printed this
            # note and then still returned 0 with "the suite genuinely covers the
            # changed lines" — a false green wearing a warning label. rc 2 means
            # "could not fully run", which is exactly what happened.
            print(f"\033[31mBLOCKED: {skipped} of {len(all_m)} mutant(s) NOT tested "
                  f"(--max {cap}). A partial run cannot certify coverage. Re-run with "
                  f"--max {len(all_m)} (or no cap) — do NOT report this as green.\033[0m",
                  file=sys.stderr)
            return 2
        print()

        survivors, accepted = [], []
        for i, (label, lineno, new) in enumerate(sel, 1):
            _atomic(target, new)
            alive = run(args.tests, args.timeout)
            _atomic(target, src)
            excused = alive and equiv_reason(src, lineno)
            mark = ("\033[33mSURVIVED(equivalent)\033[0m" if excused
                    else "\033[31mSURVIVED\033[0m" if alive else "\033[32mkilled\033[0m")
            print(f"  [{i:>3}/{len(sel)}] {mark}  {target.name}:{lineno}  {label}")
            if excused:
                # Record the LABEL too: the marker is matched by line, so one
                # comment excuses every surviving mutant on that line. Printing
                # which mutant was excused is what makes that visible.
                accepted.append((lineno, f"{label}  <- {excused}"))
            elif alive:
                survivors.append((lineno, label))
      except HarnessError as e:
        print(f"error: {e} — aborting. Mutation results would be meaningless.",
              file=sys.stderr)
        return 2
    finally:
        restore()

    assert sha(target) == before, "target NOT restored — restore it from git before anything else"
    print()
    if accepted:
        print(f"{len(accepted)} accepted as unobservable — the justification is "
              f"in the source, and the mutant it excused is named here:")
        for lineno, reason in accepted:
            print(f"  {target}:{lineno}  {reason}")
        print()
    if survivors:
        print(f"{len(survivors)} SURVIVOR(S) — these lines could be wrong in "
              f"production and no suite would tell you:")
        for lineno, label in survivors:
            print(f"  {target}:{lineno}  {label}")
        print("\nEach is either a missing fixture or an equivalent mutant (a change "
              "with no observable effect). Decide which, in writing, before review.")
        return 1
    killed = len(sel) - len(accepted)
    print(f"{killed} MUTANT(S) KILLED"
          f"{f', {len(accepted)} accepted equivalent' if accepted else ''}"
          f" in {target.name}.")
    # Say what was NOT covered. Mutation only reaches lines with a mutable AST site:
    # regex literals, string content and bare `Expr` call statements have NO mutant,
    # so "all killed" said nothing about `_HD_SHOT_RE` (the whole point of this
    # change) or about the verify_derived_ordinals CALL SITE. Both turned out to be
    # fixture-guarded, but the summary claimed a coverage it had not measured.
    print(f"Mutable sites found on {len(set(ln for _l, ln, _n in all_m))} of "
          f"{scope} line(s). Mutation cannot see regex literals, string content, or "
          f"bare call statements — those need fixtures, not mutants.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # An uncaught crash (a real bug in this harness, not a mutation
        # survivor) fell through to Python's default uncaught-exception exit,
        # which is 1 — identical to "N SURVIVOR(S) found". precheck.sh then
        # printed "classify every survivor IN WRITING", the wrong remedy for a
        # harness crash. rc 3 keeps it out of the survivor branch (see
        # precheck.sh's `elif $mrc -ne 0`, which already reads any non-{0,1}
        # code as "could not run" and says so).
        import traceback
        traceback.print_exc()
        print("error: mutate.py crashed — this is a harness bug, not a mutation "
              "survivor. Fix the harness; do not treat this as BLOCKED-on-survivors.",
              file=sys.stderr)
        sys.exit(3)
