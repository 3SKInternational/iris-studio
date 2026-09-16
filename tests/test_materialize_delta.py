"""Tests for scripts/materialize_delta.py (DQ-52 backup self-heal).

unittest, NOT pytest: the repo's precheck runner executes each tests/test_*.py
as a plain script (there is no pytest on the box), so a pytest-fixture file runs
zero tests and reads as a false-green — the exact class precheck exists to catch.
No real IO: readable/disk_free/run_brctl/os.path.exists are patched so the
disk-budget bound and the materialize -> re-probe flow are exercised
deterministically.
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import materialize_delta as md  # noqa: E402


# --- parse_dryrun ---------------------------------------------------------

class ParseDryrun(unittest.TestCase):
    def test_sizes_and_units(self):
        text = (
            "2026/09/05 03:45:40 NOTICE: INBOX.md: Skipped copy as --dry-run is set (size 28.904Ki)\n"
            "2026/09/05 03:45:40 NOTICE: a/b/Video.mp4: Skipped copy as --dry-run is set (size 700.5Mi)\n"
            "2026/09/05 03:45:40 NOTICE: big.bin: Skipped copy as --dry-run is set (size 2Gi)\n"
        )
        got = dict(md.parse_dryrun(text))
        self.assertEqual(got["INBOX.md"], int(28.904 * 1024))
        self.assertEqual(got["a/b/Video.mp4"], int(700.5 * 1024 ** 2))
        self.assertEqual(got["big.bin"], 2 * 1024 ** 3)

    def test_ignores_deletes_and_noise(self):
        text = (
            "2026/09/05 NOTICE: gone.md: Skipped delete as --dry-run is set\n"
            "2026/09/05 INFO  : x.md: Copied (new)\n"
            "2026/09/05 NOTICE: kept.md: Skipped copy as --dry-run is set (size 1Ki)\n"
        )
        self.assertEqual(md.parse_dryrun(text), [("kept.md", 1024)])

    def test_handles_missing_size(self):
        # Empty files can log without a "(size ...)" suffix.
        text = "2026/09/05 NOTICE: empty.md: Skipped copy as --dry-run is set\n"
        self.assertEqual(md.parse_dryrun(text), [("empty.md", 0)])

    def test_relpath_with_spaces(self):
        text = "2026/09/05 NOTICE: a dir/my note.md: Skipped copy as --dry-run is set (size 10Ki)\n"
        self.assertEqual(md.parse_dryrun(text), [("a dir/my note.md", 10 * 1024)])

    def test_bare_number_and_byte_and_large_units(self):
        # A bare number (no unit) uses the None:1 factor; "B" uses B:1; and the
        # rarely-hit large units Ti/Pi must scale by their exact power of 1024.
        text = (
            "2026/09/05 NOTICE: bare.md: Skipped copy as --dry-run is set (size 42)\n"
            "2026/09/05 NOTICE: byte.md: Skipped copy as --dry-run is set (size 8B)\n"
            "2026/09/05 NOTICE: big.ti: Skipped copy as --dry-run is set (size 2Ti)\n"
            "2026/09/05 NOTICE: big.pi: Skipped copy as --dry-run is set (size 1Pi)\n"
        )
        got = dict(md.parse_dryrun(text))
        self.assertEqual(got["bare.md"], 42)
        self.assertEqual(got["byte.md"], 8)
        self.assertEqual(got["big.ti"], 2 * 1024 ** 4)
        self.assertEqual(got["big.pi"], 1 * 1024 ** 5)


# --- materialize ----------------------------------------------------------

def _wire(tc, *, evicted, free, brctl_fixes=None, exists=None):
    """evicted: set of relpaths that read as unreadable.
    brctl_fixes: relpaths that become readable after brctl (default: all evicted).
    exists: relpaths that exist on disk (default: all in the entry list).
    Patches are started here and torn down via tc.addCleanup(mock.patch.stopall)."""
    if brctl_fixes is None:
        brctl_fixes = set(evicted)
    state = {"unreadable": set(evicted), "brctl_calls": []}

    if exists is None:
        mock.patch.object(md.os.path, "exists", lambda p: True).start()
    else:
        mock.patch.object(md.os.path, "exists",
                          lambda p: os.path.relpath(p, "/vault") in exists).start()

    def fake_readable(p):
        return os.path.relpath(p, "/vault") not in state["unreadable"]

    def fake_brctl(p):
        rel = os.path.relpath(p, "/vault")
        state["brctl_calls"].append(rel)
        if rel in brctl_fixes:
            state["unreadable"].discard(rel)

    mock.patch.object(md, "readable", fake_readable).start()
    mock.patch.object(md, "disk_free", lambda p: free).start()
    mock.patch.object(md, "run_brctl", fake_brctl).start()
    tc.addCleanup(mock.patch.stopall)
    return state


class Materialize(unittest.TestCase):
    def test_nothing_evicted_is_noop(self):
        state = _wire(self, evicted=set(), free=50 * 1024 ** 3)
        c = md.materialize([("a.md", 100), ("b.md", 200)], "/vault")
        self.assertEqual(c["evicted"], 0)
        self.assertEqual(c["materialized"], 0)
        self.assertEqual(state["brctl_calls"], [])

    def test_small_delta_all_materialized(self):
        state = _wire(self, evicted={"a.md", "b.md"}, free=50 * 1024 ** 3)
        c = md.materialize([("a.md", 100), ("b.md", 200)], "/vault")
        self.assertEqual(c["materialized"], 2)
        self.assertEqual(c["failed"], 0)
        self.assertEqual(c["skipped_too_big"], 0)
        self.assertEqual(set(state["brctl_calls"]), {"a.md", "b.md"})

    def test_oversized_file_skipped_by_budget(self):
        # free - margin(3Gi) = 1Gi budget; the 5Gi video must be skipped, the small md kept.
        free = md.DEFAULT_MARGIN + 1 * 1024 ** 3
        state = _wire(self, evicted={"small.md", "huge.mp4"}, free=free)
        c = md.materialize([("small.md", 10 * 1024), ("huge.mp4", 5 * 1024 ** 3)], "/vault")
        self.assertEqual(c["materialized"], 1)
        self.assertEqual(c["skipped_too_big"], 1)
        self.assertEqual(state["brctl_calls"], ["small.md"])  # huge never brctl'd

    def test_near_full_disk_materializes_nothing(self):
        # free < margin -> budget 0 -> refuse to download anything (protect the disk).
        state = _wire(self, evicted={"a.md"}, free=md.DEFAULT_MARGIN - 1)
        c = md.materialize([("a.md", 100)], "/vault")
        self.assertEqual(c["materialized"], 0)
        self.assertEqual(c["skipped_too_big"], 1)
        self.assertEqual(state["brctl_calls"], [])

    def test_still_unreadable_after_brctl_counts_failed(self):
        # brctl runs but does not fix it (Instance-4 caveat) -> failed, not materialized.
        state = _wire(self, evicted={"stuck.md"}, free=50 * 1024 ** 3, brctl_fixes=set())
        c = md.materialize([("stuck.md", 100)], "/vault")
        self.assertEqual(c["failed"], 1)
        self.assertEqual(c["materialized"], 0)
        self.assertEqual(state["brctl_calls"], ["stuck.md"])

    def test_smallest_first_budget_greedy(self):
        # budget fits the two small files but not the big one; entries are big-first.
        free = md.DEFAULT_MARGIN + 300
        state = _wire(self, evicted={"big", "s1", "s2"}, free=free)
        c = md.materialize([("big", 1000), ("s1", 100), ("s2", 150)], "/vault")
        self.assertEqual(c["materialized"], 2)
        self.assertEqual(c["skipped_too_big"], 1)
        self.assertEqual(state["brctl_calls"], ["s1", "s2"])  # smallest first, big skipped

    def test_missing_file_not_materialized(self):
        # A would-copy path that no longer exists is not probed/downloaded.
        state = _wire(self, evicted={"a.md"}, free=50 * 1024 ** 3, exists={"a.md"})
        c = md.materialize([("a.md", 100), ("ghost.md", 100)], "/vault")
        self.assertEqual(c["evicted"], 1)
        self.assertEqual(c["materialized"], 1)
        self.assertEqual(state["brctl_calls"], ["a.md"])


# --- main() anchor guard --------------------------------------------------

class MainAnchorGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.log = os.path.join(self.tmp, "dryrun.log")
        with open(self.log, "w") as f:
            f.write("2026/09/05 NOTICE: a.md: Skipped copy as --dry-run is set (size 1Ki)\n")

    def test_skips_when_anchor_unreadable(self):
        calls = []
        with mock.patch.object(md.os.path, "exists", lambda p: True), \
             mock.patch.object(md, "readable", lambda p: False), \
             mock.patch.object(md, "run_brctl", lambda p: calls.append(p)), \
             mock.patch.object(md, "disk_free", lambda p: 50 * 1024 ** 3):
            self.assertEqual(md.main(["prog", self.log, "/vault"]), 0)
        self.assertEqual(calls, [])  # never tried to materialize anything

    def test_runs_when_anchor_readable(self):
        calls = []
        reads = {"/vault/CLAUDE.md": True, "/vault/a.md": False}  # anchor ok, a.md evicted

        def brctl(p):
            calls.append(p)
            reads[p] = True  # rehydrated

        with mock.patch.object(md.os.path, "exists", lambda p: True), \
             mock.patch.object(md, "readable", lambda p: reads.get(p, False)), \
             mock.patch.object(md, "run_brctl", brctl), \
             mock.patch.object(md, "disk_free", lambda p: 50 * 1024 ** 3):
            self.assertEqual(md.main(["prog", self.log, "/vault"]), 0)
        self.assertEqual(calls, ["/vault/a.md"])


# --- main() return paths (all must exit 0 so the real sync stays authoritative) ---

class MainReturnPaths(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def test_wrong_argc_returns_zero_and_never_materializes(self):
        # Bad arg count must no-op with exit 0 (never block the sync on our own
        # arg error) and must not touch brctl.
        calls = []
        with mock.patch.object(md, "run_brctl", lambda p: calls.append(p)):
            self.assertEqual(md.main(["prog"]), 0)
            self.assertEqual(md.main(["prog", "only-one-arg"]), 0)
        self.assertEqual(calls, [])

    def test_unreadable_dryrun_log_returns_zero(self):
        # A dry-run log we can't open -> skip, exit 0 (anchor readable so we get
        # past the anchor guard to the log-open path).
        missing = os.path.join(self.tmp, "does-not-exist.log")
        with mock.patch.object(md.os.path, "exists", lambda p: p != missing), \
             mock.patch.object(md, "readable", lambda p: True):
            self.assertEqual(md.main(["prog", missing, "/vault"]), 0)

    def test_empty_dryrun_returns_zero_and_short_circuits_materialize(self):
        # A dry-run log with no would-copy lines -> "nothing to do", exit 0, and
        # materialize() is never entered (asserting the return code alone can't
        # tell the early-return from a fall-through that materializes []).
        log = os.path.join(self.tmp, "empty.log")
        with open(log, "w") as f:
            f.write("2026/09/05 NOTICE: gone.md: Skipped delete as --dry-run is set\n")
        with mock.patch.object(md.os.path, "exists", lambda p: True), \
             mock.patch.object(md, "readable", lambda p: True), \
             mock.patch.object(md, "materialize") as mat:
            self.assertEqual(md.main(["prog", log, "/vault"]), 0)
        mat.assert_not_called()


if __name__ == "__main__":
    unittest.main()
