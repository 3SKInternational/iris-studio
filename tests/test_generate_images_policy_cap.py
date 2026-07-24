#!/usr/bin/env python3
"""DQ-40: per-video cumulative image-spend policy cap in generate_images.py.

Pins: (1) video_label / canonical_video key normalization (the pre-fix
case-sensitive regex fragmented one video's spend across ledger keys, which is
exactly how 3 videos crossed the old $8 policy silently); (2) load_video_spend
summing across fragmented keys; (3) the live gate: over-cap blocks without
--over-cap-ok, proceeds with it, and stays quiet under the cap — all verified
by running the REAL script as a subprocess up to the pre-flight guard (no API
key needed: the guard fires before client init). IRIS_NOTIFY_DISABLE=1 keeps
Telegram out of test runs.

Run: python3 tests/test_generate_images_policy_cap.py
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SCRIPT = _HERE.parent / "image_factory" / "generate_images.py"
_spec = importlib.util.spec_from_file_location("generate_images", _SCRIPT)
gi = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gi)


class TestVideoKeyNormalization(unittest.TestCase):
    def test_video_label_matches_lowercase_manifest_names(self):
        # The pre-fix regex was case-sensitive and never matched real manifests.
        self.assertEqual(gi.video_label("video_13_hd.json"), "Video_13")
        self.assertEqual(gi.video_label("video_13_fixup_renders.json"), "Video_13")
        self.assertEqual(gi.video_label("Video_05_kit.json"), "Video_05")
        self.assertEqual(gi.video_label("video_9_regen.json"), "Video_09")

    def test_video_label_falls_back_to_stem(self):
        self.assertEqual(gi.video_label("thumb_pop_batch.json"), "thumb_pop_batch")

    def test_canonical_video_from_video_field(self):
        self.assertEqual(gi.canonical_video({"video": "video_13_hd"}), "Video_13")

    def test_canonical_video_falls_back_to_shot(self):
        # Rows like {'video': 'thumb_pop_v02', 'shot': 'Video_08_Thumbnail_B'}
        # must attribute to the real video.
        self.assertEqual(
            gi.canonical_video({"video": "v08_thumbB_regen",
                                "shot": "Video_08_Thumbnail_B"}), "Video_08")

    def test_canonical_video_passthrough(self):
        self.assertEqual(gi.canonical_video({"video": "CTA_Cards"}), "CTA_Cards")
        self.assertEqual(gi.canonical_video({}), "?")


class TestLoadVideoSpend(unittest.TestCase):
    def _ledger(self, rows):
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False, encoding="utf-8")
        for r in rows:
            tmp.write(json.dumps(r) + "\n")
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)
        return Path(tmp.name)

    def test_sums_across_fragmented_keys(self):
        # The V13 reality: hd batch + fixup renders under different raw keys.
        led = self._ledger([
            {"video": "video_13_hd", "shot": "Video_13_Shot_01", "cost_usd": 8.91},
            {"video": "video_13_fixup_renders", "shot": "Video_13_Shot_17b", "cost_usd": 0.23},
            {"video": "video_12_hd", "shot": "Video_12_Shot_01", "cost_usd": 11.39},
        ])
        self.assertAlmostEqual(gi.load_video_spend(led, "Video_13"), 9.14)
        self.assertAlmostEqual(gi.load_video_spend(led, "Video_12"), 11.39)

    def test_missing_ledger_is_zero(self):
        self.assertEqual(gi.load_video_spend(Path("/nonexistent/l.jsonl"), "Video_01"), 0.0)

    def test_tolerates_torn_line_and_null_cost(self):
        led = self._ledger([{"video": "video_07_hd", "shot": "Video_07_Shot_01",
                             "cost_usd": 1.0},
                            {"video": "video_07_hd", "shot": "Video_07_Shot_02",
                             "cost_usd": None}])
        with open(led, "a") as fh:
            fh.write('{"video": "video_07_hd", "cost_')  # torn mid-write
        self.assertAlmostEqual(gi.load_video_spend(led, "Video_07"), 1.0)


class TestPolicyCapGate(unittest.TestCase):
    """Drive the real script to the pre-flight guard (fires before client init,
    so no API key is needed to prove the gate's behavior)."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        d = Path(self.dir.name)
        self.manifest = d / "video_99_hd.json"
        self.manifest.write_text(json.dumps({
            "project": "cap-test",
            "images": [{"name": "Video_99_Shot_01", "prompt": "test shot",
                        "use_references": False}],
        }))
        self.ledger = d / "ledger.jsonl"
        self.out = d / "out"
        self.env = {**os.environ, "IRIS_NOTIFY_DISABLE": "1"}

    def _run(self, *extra):
        # --provider flux ALWAYS: flux is a NotImplementedError stub that dies
        # before any network call, so a test that gets PAST the policy gate can
        # never bill. (The obvious alternative — clearing OPENAI_API_KEY — does
        # NOT work: load_env_key falls back to image_factory/.env, which on the
        # production machine holds a LIVE key. Caught by the DQ-40 review; a
        # money-guard's tests must be $0 by construction, not by accident of
        # which interpreter lacks the openai package.)
        return subprocess.run(
            [sys.executable, str(_SCRIPT), str(self.manifest),
             "--output", str(self.out), "--ledger", str(self.ledger),
             "--provider", "flux", *extra],
            capture_output=True, text=True, env=self.env, timeout=60)

    def _seed_prior(self, amount):
        self.ledger.write_text(json.dumps(
            {"video": "video_99_hd", "shot": "Video_99_Shot_00",
             "cost_usd": amount}) + "\n")

    def test_over_cap_blocks_without_override(self):
        self._seed_prior(gi.POLICY_CAP_USD)  # any new estimate crosses
        p = self._run()
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("policy cap", p.stderr + p.stdout)
        self.assertIn("--over-cap-ok", p.stderr + p.stdout)

    def test_over_cap_ok_opens_the_gate(self):
        self._seed_prior(gi.POLICY_CAP_USD)
        p = self._run("--over-cap-ok")
        # The gate opened (override line printed); the run then dies at the flux
        # NotImplementedError stub — proving we got PAST the policy gate with a
        # provider that cannot bill.
        self.assertIn("Proceeding on --over-cap-ok", p.stdout)
        self.assertNotIn("policy cap:", p.stderr)

    def test_under_cap_passes_quietly(self):
        self._seed_prior(0.10)
        p = self._run()
        self.assertNotIn("--over-cap-ok", p.stderr)
        self.assertIn("spend guard: OK", p.stdout)

    def test_dry_run_reports_but_never_blocks(self):
        self._seed_prior(gi.POLICY_CAP_USD + 5)  # far over
        p = self._run("--dry-run")
        self.assertEqual(p.returncode, 0)
        self.assertIn("OVER", p.stdout)

    def test_fragmented_prior_spend_is_seen(self):
        # Prior spend recorded under a DIFFERENT manifest key must still count.
        self.ledger.write_text(json.dumps(
            {"video": "video_99_fixup_renders", "shot": "Video_99_Shot_00",
             "cost_usd": gi.POLICY_CAP_USD}) + "\n")
        p = self._run()
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("policy cap", p.stderr + p.stdout)

    def test_zero_render_run_never_blocks(self):
        # DQ-40 review HIGH: a run where every PNG already exists bills $0 and
        # must NOT block (or ping) even for a video already over cap — V10/11/12
        # are over TODAY, and their no-op reruns must stay green.
        self._seed_prior(gi.POLICY_CAP_USD + 5)
        self.out.mkdir(parents=True, exist_ok=True)
        (self.out / "Video_99_Shot_01.png").write_bytes(b"png")
        p = self._run()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn("BLOCKED", p.stderr + p.stdout)

    def test_batch_manifest_gates_per_video(self):
        # DQ-40 review MEDIUM: a multi-video batch manifest (thumb_pop_batch
        # class) must gate each image against ITS OWN video's cumulative spend,
        # not the batch stem's empty bucket.
        self.manifest.write_text(json.dumps({
            "project": "batch-test",
            "images": [{"name": "Video_98_Thumbnail_pop", "prompt": "pop",
                        "use_references": False}],
        }))
        # Manifest stem is video_99_hd (Video_99), but the image belongs to
        # Video_98, which is already over cap.
        self.ledger.write_text(json.dumps(
            {"video": "video_98_hd", "shot": "Video_98_Shot_00",
             "cost_usd": gi.POLICY_CAP_USD}) + "\n")
        p = self._run()
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("Video_98", p.stderr + p.stdout)

    def test_policy_gate_key_unit(self):
        self.assertEqual(gi.policy_gate_key("Video_98_Thumbnail_pop", "Video_99"),
                         "Video_98")
        self.assertEqual(gi.policy_gate_key("CTA_outro", "Video_99"), "Video_99")


if __name__ == "__main__":
    unittest.main(verbosity=1)
