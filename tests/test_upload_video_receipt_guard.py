#!/usr/bin/env python3
"""Tests for upload_video.py's receipt guard (no network / no live API calls).

THE DEFECT (two-pass codebase audit, lane C, 2026-07-28). main() went straight
from the preflight signoff to videos().insert() with no check of the receipt it
was about to overwrite. A human re-run duplicates a LIVE video, and the
unconditional receipt write that followed clobbered the only pointer to the
FIRST video's id in the same act that duplicated it. No programmatic caller
exists (grep -c upload_video scripts/pipeline_orchestrator.py -> 0), so this is
purely a human-rerun path.

Fix: reupload_guard_message() refuses (main() die()s) when the receipt already
carries a live video_id, unless --allow-reupload; write_receipt_preserving_dead_id()
never drops a DIFFERENT existing video_id — it moves it to `deleted_video_id`
(the schema receipt_surface_id_lint.py already reads) instead of overwriting it.

Run: python3 tests/test_upload_video_receipt_guard.py
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location(
    "upload_video_under_test", REPO / "scripts" / "upload_video.py"
)
uv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(uv)


class TestReuploadGuardMessage(unittest.TestCase):
    def test_no_receipt_at_all_is_safe(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "Video_05_youtube_upload.json"
            self.assertIsNone(uv.reupload_guard_message(p, allow_reupload=False))

    def test_receipt_with_null_video_id_is_safe(self):
        """A deleted-from-channel receipt (video_id: null) must not block a
        legitimate first real upload."""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "Video_04_youtube_upload.json"
            p.write_text(json.dumps({"video_id": None, "deleted_video_id": "ClJVIUtwsVE"}))
            self.assertIsNone(uv.reupload_guard_message(p, allow_reupload=False))

    def test_receipt_with_live_video_id_blocks_by_default(self):
        """THE regression case: a live video_id must refuse without an override."""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "Video_09_youtube_upload.json"
            p.write_text(json.dumps({"video_id": "DY2RVnuUb64"}))
            msg = uv.reupload_guard_message(p, allow_reupload=False)
            self.assertIsNotNone(msg, "a live video_id must block a re-run")
            self.assertIn("DY2RVnuUb64", msg)
            self.assertIn("--allow-reupload", msg)

    def test_allow_reupload_overrides_the_block(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "Video_09_youtube_upload.json"
            p.write_text(json.dumps({"video_id": "DY2RVnuUb64"}))
            self.assertIsNone(uv.reupload_guard_message(p, allow_reupload=True))

    def test_whitespace_only_video_id_treated_as_absent(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "Video_02_youtube_upload.json"
            p.write_text(json.dumps({"video_id": "   "}))
            self.assertIsNone(uv.reupload_guard_message(p, allow_reupload=False))


class TestWriteReceiptPreservingDeadId(unittest.TestCase):
    def test_no_prior_receipt_writes_cleanly(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "Video_05_youtube_upload.json"
            uv.write_receipt_preserving_dead_id(p, {"video": "Video_05", "video_id": "NEW11111111"})
            data = json.loads(p.read_text())
            self.assertEqual(data["video_id"], "NEW11111111")
            self.assertNotIn("deleted_video_id", data)

    def test_same_video_id_rewrite_does_not_invent_a_dead_id(self):
        """Re-writing the SAME id (e.g. the captions/thumbnail follow-up write in
        the same run) must not fabricate a deleted_video_id."""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "Video_05_youtube_upload.json"
            p.write_text(json.dumps({"video_id": "SAME1111111"}))
            uv.write_receipt_preserving_dead_id(
                p, {"video": "Video_05", "video_id": "SAME1111111", "captions_set": True})
            data = json.loads(p.read_text())
            self.assertNotIn("deleted_video_id", data)

    def test_different_existing_video_id_is_preserved_not_destroyed(self):
        """THE regression case: pre-fix this silently overwrote the receipt,
        destroying the only pointer to the first video. Now the old id must
        survive as `deleted_video_id`."""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "Video_09_youtube_upload.json"
            p.write_text(json.dumps({"video": "Video_09", "video_id": "OLD11111111"}))
            uv.write_receipt_preserving_dead_id(
                p, {"video": "Video_09", "video_id": "NEW22222222"})
            data = json.loads(p.read_text())
            self.assertEqual(data["video_id"], "NEW22222222")
            self.assertEqual(data["deleted_video_id"], "OLD11111111",
                            "the destroyed-pointer regression: old id must be preserved")

    def test_explicit_deleted_video_id_leads_but_does_not_destroy_history(self):
        """A caller-supplied deleted_video_id JOINS the union; it does not replace it.

        ASSERTION CHANGED 2026-07-28 (round-3 review, finding M2). This previously
        asserted the caller's value won OUTRIGHT — `== "EXPLICIT0001"` — which
        encoded the short-circuit as intended behaviour. It was not: an explicit
        value wrote straight through and every dead id already on disk was
        destroyed (a receipt holding ["D1","D2","D3"] plus an explicit "OLDZ" kept
        one of four). That is the exact data loss this function exists to prevent,
        re-entering through the "explicit caller wins" door.

        The original INTENT — the caller's choice is not overridden — still holds
        and is asserted below: their id is present and LEADS. What changed is that
        the ids it used to silently drop now survive alongside it."""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "Video_09_youtube_upload.json"
            p.write_text(json.dumps({"video_id": "OLD11111111"}))
            uv.write_receipt_preserving_dead_id(
                p, {"video_id": "NEW22222222", "deleted_video_id": "EXPLICIT0001"})
            got = json.loads(p.read_text())["deleted_video_id"]
            self.assertEqual(got[0], "EXPLICIT0001",
                             "the caller's explicit id must lead — their ordering choice")
            self.assertIn("OLD11111111", got,
                          "the id being retired must NOT be destroyed by an explicit value")

    def test_explicit_value_does_not_destroy_a_disk_list(self):
        """The M2 reproduction itself: 3 ids on disk + an explicit 4th, none lost."""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "Video_04_youtube_upload.json"
            p.write_text(json.dumps({"video_id": "LIVE1111111",
                                     "deleted_video_id": ["D1aaaaaaaaa", "D2bbbbbbbbb",
                                                          "D3ccccccccc"]}))
            uv.write_receipt_preserving_dead_id(
                p, {"video_id": "NEW22222222", "deleted_video_id": "OLDZzzzzzzz"})
            got = json.loads(p.read_text())["deleted_video_id"]
            for want in ("OLDZzzzzzzz", "D1aaaaaaaaa", "D2bbbbbbbbb", "D3ccccccccc",
                         "LIVE1111111"):
                self.assertIn(want, got, f"{want} was destroyed by the explicit value")
            self.assertEqual(len(got), len(set(got)), "no duplicates in the union")

    def test_inherited_deleted_video_id_survives_a_reupload(self):
        """ROUND-2 REGRESSION (2026-07-28 review). Reproduces the exact real
        Video_04 receipt shape: video_id is null (so the guard passes with no
        --allow-reupload needed) but deleted_video_id ClJVIUtwsVE is already on
        disk. v1's fix only carried forward an id THIS write displaces
        (_existing_video_id reads video_id, which is null here) -- so it never
        looked at disk's own deleted_video_id and the fresh receipt dict wiped
        it. That downgrades receipt_surface_id_lint.py from a paging FLAG to a
        non-paging WARN. The inherited id must survive."""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "Video_04_youtube_upload.json"
            p.write_text(json.dumps({
                "video": "Video_04", "video_id": None,
                "deleted_video_id": "ClJVIUtwsVE", "privacy": "public",
            }))
            uv.write_receipt_preserving_dead_id(
                p, {"video": "Video_04", "video_id": "NEWID000001", "privacy": "private"})
            data = json.loads(p.read_text())
            self.assertEqual(data["video_id"], "NEWID000001")
            self.assertEqual(data["deleted_video_id"], "ClJVIUtwsVE",
                            "the inherited dead id must survive a re-upload")

    def test_prior_list_shaped_dead_ids_all_survive_another_write(self):
        """disk's deleted_video_id is ALREADY a list (this fix's own prior
        output, once a receipt has accumulated >1 dead id) -- every entry must
        be carried forward on a FURTHER write, not dropped because the
        isinstance(prior, list) branch never fires."""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "Video_13_youtube_upload.json"
            p.write_text(json.dumps({
                "video": "Video_13", "video_id": "LIVE3333333",
                # garbage entries (None, empty, whitespace) mixed in -- must be
                # skipped, not just the 2 real ids picked up.
                "deleted_video_id": ["DEAD1111111", None, "", "   ", "DEAD2222222"],
            }))
            uv.write_receipt_preserving_dead_id(
                p, {"video": "Video_13", "video_id": "NEW44444444"})
            data = json.loads(p.read_text())
            self.assertEqual(sorted(data["deleted_video_id"]),
                            sorted(["DEAD1111111", "DEAD2222222", "LIVE3333333"]))

    def test_a_duplicate_dead_id_across_sources_is_not_repeated(self):
        """The SAME id can appear both as disk's existing deleted_video_id AND
        as the id this write is retiring (video_id == deleted_video_id on
        disk) -- must dedup to ONE entry, not survive twice in a list."""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "Video_14_youtube_upload.json"
            p.write_text(json.dumps(
                {"video_id": "SAMEID00001", "deleted_video_id": "SAMEID00001"}))
            uv.write_receipt_preserving_dead_id(p, {"video_id": "NEW55555555"})
            data = json.loads(p.read_text())
            self.assertEqual(data["deleted_video_id"], "SAMEID00001",
                            "a duplicate id across sources must collapse to ONE "
                            "entry, not survive twice as a list")

    def test_a_third_distinct_dead_id_is_not_dropped(self):
        """disk already carries one dead id (ClJVIUtwsVE) AND a live video_id
        this write is about to retire (LIVE22222222, different from the new
        id) -> BOTH must survive, as a list -- none silently dropped."""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "Video_04_youtube_upload.json"
            p.write_text(json.dumps({
                "video": "Video_04", "video_id": "LIVE22222222",
                "deleted_video_id": "ClJVIUtwsVE",
            }))
            uv.write_receipt_preserving_dead_id(
                p, {"video": "Video_04", "video_id": "NEWID000003"})
            data = json.loads(p.read_text())
            self.assertEqual(sorted(data["deleted_video_id"]),
                            sorted(["ClJVIUtwsVE", "LIVE22222222"]))


class TestMainWiresTheGuard(unittest.TestCase):
    """main() must actually ACT on reupload_guard_message()'s result, not just
    compute it. Both cases die() before any network call (guard check sits
    ahead of the video-file existence check), so this is still no-network."""

    def _run(self, vault_dir):
        env = dict(os.environ, SK_VAULT=str(vault_dir))
        return subprocess.run(
            [sys.executable, str(REPO / "scripts" / "upload_video.py"),
             "Video_05", "--title", "X", "--dry-run"],
            capture_output=True, text=True, timeout=30, env=env,
        )

    def test_live_receipt_dies_with_the_guard_message_before_file_check(self):
        """Kills the :857 if-test->False mutant, which would skip straight past
        a live-video_id receipt to the (here, also-failing) video-file check —
        the wrong error entirely, and in production the wrong outcome: upload."""
        with tempfile.TemporaryDirectory() as d:
            kits = Path(d) / "Production_Kits"
            kits.mkdir(parents=True)
            (kits / "Video_05_youtube_upload.json").write_text(
                json.dumps({"video_id": "LIVE1111111"}))
            proc = self._run(d)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("already carries a live video_id", proc.stderr)
        self.assertNotIn("video file not found", proc.stderr)

    def test_no_receipt_does_not_die_on_the_guard(self):
        """Kills the :857 if-test->True mutant, which would die() on the guard
        message even when there is none (guard_msg is None/falsy) -- the run
        must instead reach (and fail on) the NEXT real check, video-file
        existence, proving the guard branch was correctly skipped."""
        with tempfile.TemporaryDirectory() as d:
            proc = self._run(d)  # no Production_Kits dir at all -> no receipt
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("video file not found", proc.stderr)
        self.assertNotIn("already carries a live video_id", proc.stderr)


if __name__ == "__main__":
    unittest.main()
