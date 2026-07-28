#!/usr/bin/env python3
"""Tests for scripts/sweep_baks.py — the stale-.bak archiver.

Stdlib unittest only (repo convention). Run:
    python3 tests/test_sweep_baks.py
or under the suite:
    python3 -m unittest discover -s tests -v

WHY THIS EXISTS. sweep_baks.py carries a thorough --selftest, but it lives INSIDE
the script and precheck.sh only discovers tests/test_*.py — so none of it ran at
the gate. When --archive-root was added on 2026-07-27 (to sweep ~/.claude/agents
into the vault archive), the new routing was therefore invisible to the gate. This
file makes the existing selftest part of the discovered suite and pins the routing
directly, so both are mutation-visible.
"""
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "sweep_baks", REPO / "scripts" / "sweep_baks.py")
sb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sb)


class TestArchiveDestRoot(unittest.TestCase):
    """Where swept baks land. The default must stay byte-identical to the
    pre-2026-07-27 behaviour — a vault sweep that silently relocated its archive
    would scatter backups across two trees."""

    def setUp(self):
        self.vault = Path("/tmp/some-vault")
        self.other = Path("/tmp/other-tree")

    def test_default_is_the_swept_trees_own_archive(self):
        self.assertEqual(
            sb.archive_dest_root(self.vault, None),
            self.vault / sb.ARCHIVE_SUBDIR)

    def test_archive_root_equal_to_vault_is_the_same_as_default(self):
        """Passing --archive-root pointing at the swept tree itself must not
        nest an extra subdir."""
        self.assertEqual(
            sb.archive_dest_root(self.vault, self.vault),
            sb.archive_dest_root(self.vault, None))

    def test_foreign_archive_root_nests_under_the_swept_tree_name(self):
        """A foreign root parks under a subdir named for the swept tree, so a
        vault sweep and an agents sweep can never collide on the same rel-path."""
        self.assertEqual(
            sb.archive_dest_root(Path("/Users/x/.claude/agents"), self.other),
            self.other / sb.ARCHIVE_SUBDIR / "agents")

    def test_two_different_trees_never_share_a_destination(self):
        a = sb.archive_dest_root(Path("/Users/x/.claude/agents"), self.other)
        b = sb.archive_dest_root(Path("/Users/x/.claude/skills"), self.other)
        self.assertNotEqual(a, b)


class TestSweepEndToEnd(unittest.TestCase):
    """sweep() must actually USE archive_dest_root.

    The wiring line is a plain assignment, so mutation is blind to it: a review on
    2026-07-28 reverted it to `vault / ARCHIVE_SUBDIR` with all tests green, and
    the agents sweep then created 07_Archive/ INSIDE ~/.claude/agents and dumped
    the backups there. The pure-helper tests could not see it — only a real move
    can.
    """

    def _tree_with_bak(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        vault = root / "agents"
        vault.mkdir()
        (vault / "scriptwriter.md.bak-pre-adapt").write_text("old\n")
        other = root / "thevault"
        other.mkdir()
        return vault, other

    def test_foreign_archive_root_receives_the_file(self):
        vault, other = self._tree_with_bak()
        # min_age_days=-1 so the fresh fixture is eligible without sleeping.
        sb.sweep(vault, -1, apply=True, archive_root=other)
        landed = other / sb.ARCHIVE_SUBDIR / vault.name / "scriptwriter.md.bak-pre-adapt"
        self.assertTrue(landed.is_file(), f"expected the bak at {landed}")
        self.assertFalse((vault / "scriptwriter.md.bak-pre-adapt").exists(),
                         "the source must have MOVED, not been copied")

    def test_foreign_archive_root_does_not_write_inside_the_swept_tree(self):
        """The exact regression: an archive dir appearing in the agents dir."""
        vault, other = self._tree_with_bak()
        sb.sweep(vault, -1, apply=True, archive_root=other)
        self.assertFalse((vault / "07_Archive").exists(),
                         "sweep must not create an archive inside the swept tree")

    def test_default_still_archives_in_place(self):
        """Byte-identical default behaviour — the vault sweep must be unaffected."""
        vault, _ = self._tree_with_bak()
        sb.sweep(vault, -1, apply=True)
        self.assertTrue((vault / sb.ARCHIVE_SUBDIR / "scriptwriter.md.bak-pre-adapt").is_file())

    def test_dry_run_moves_nothing(self):
        vault, other = self._tree_with_bak()
        sb.sweep(vault, -1, apply=False, archive_root=other)
        self.assertTrue((vault / "scriptwriter.md.bak-pre-adapt").is_file(),
                        "dry-run must leave the source in place")
        self.assertFalse((other / sb.ARCHIVE_SUBDIR).exists())


class TestArchiveRootValidation(unittest.TestCase):
    """The --archive-root guard in main(). Validation at a CLI trust boundary:
    a typo'd archive root must fail loudly, never silently sweep backups into a
    path that gets created on the fly and then forgotten."""

    SCRIPT = REPO / "scripts" / "sweep_baks.py"

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, str(self.SCRIPT), *args],
            capture_output=True, text=True)

    def test_nonexistent_archive_root_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            r = self._run("--vault", td,
                          "--archive-root", "/nonexistent/definitely/not/here")
        self.assertNotEqual(r.returncode, 0, "a bogus --archive-root must not proceed")
        self.assertIn("archive-root not found", r.stderr)

    def test_valid_archive_root_is_accepted(self):
        """The guard must not reject a good root — an always-error branch would
        break the agents sweep entirely."""
        with tempfile.TemporaryDirectory() as v, tempfile.TemporaryDirectory() as a:
            r = self._run("--vault", v, "--archive-root", a)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("archive-root not found", r.stderr)

    def test_also_tree_is_swept_into_the_vault_archive(self):
        """--also is what keeps ~/.claude/agents from re-accumulating baks; without
        it the scheduled job covers the vault only and the pile rebuilds."""
        with tempfile.TemporaryDirectory() as root:
            v, extra = Path(root) / "vault", Path(root) / "agents"
            v.mkdir(); extra.mkdir()
            (extra / "scriptwriter.md.bak-pre-adapt").write_text("old\n")
            r = self._run("--vault", str(v), "--also", str(extra),
                          "--min-age-days", "-1", "--apply")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue(
                (v / "07_Archive" / "bak_sweep" / "agents"
                 / "scriptwriter.md.bak-pre-adapt").is_file(), r.stdout)

    def test_missing_also_tree_is_skipped_not_fatal(self):
        """One absent path must not abort the whole scheduled sweep."""
        with tempfile.TemporaryDirectory() as v:
            r = self._run("--vault", v, "--also", "/nonexistent/nope", "--apply")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("skipped", r.stdout)

    def test_archive_root_omitted_is_accepted(self):
        """Default path (None) must skip the existence check entirely."""
        with tempfile.TemporaryDirectory() as v:
            r = self._run("--vault", v)
        self.assertEqual(r.returncode, 0, r.stderr)


class TestSelftest(unittest.TestCase):
    def test_module_selftest_passes(self):
        """Runs the script's own selftest (bak detection, the ctime freshness
        guard, skip-dirs, the no-clobber collision loop) under the discovered
        suite, where precheck can actually see it."""
        self.assertEqual(sb._selftest(), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
