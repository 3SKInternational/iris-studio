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
        # Seed at the ABSOLUTE ceiling (cap + reprint). Seeding at POLICY_CAP alone no
        # longer crosses: since 2026-07-27 a video sitting at the cap is deliberately
        # still allowed a <=$1 reprint, so the old seed now passes by design.
        self._seed_prior(gi.POLICY_CAP_USD + gi.REPRINT_CAP_USD)
        p = self._run()
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("policy cap", p.stderr + p.stdout)
        self.assertIn("--over-cap-ok", p.stderr + p.stdout)

    def test_over_cap_ok_opens_the_gate(self):
        self._seed_prior(gi.POLICY_CAP_USD + gi.REPRINT_CAP_USD)
        p = self._run("--over-cap-ok")
        # The gate opened (override line printed); the run then dies at the flux
        # NotImplementedError stub — proving we got PAST the policy gate with a
        # provider that cannot bill.
        self.assertIn("Proceeding on --over-cap-ok", p.stdout)
        self.assertNotIn("policy cap:", p.stderr)

    def test_preflight_blocks_when_sdk_missing(self):
        """preflight_interpreter must refuse, IN-PROCESS — never via a live provider.

        The first cut of this test shelled out with `--provider openai` and tried to
        make the SDK unimportable via PYTHONPATH. PYTHONPATH PREPENDS; it does not
        remove site-packages — so under .venv/bin/python (the interpreter this repo
        renders with) `import openai` succeeded, load_env_key found the live key in
        image_factory/.env, and the test RENDERED FOR REAL. ~$0.10 per suite run, from
        the test file whose own _run() helper documents this exact trap and defends
        against it with --provider flux. A money-guard's tests must be $0 BY
        CONSTRUCTION. Masking sys.modules keeps it in-process and unbillable."""
        import unittest.mock
        with unittest.mock.patch.dict(sys.modules, {"openai": None}):
            with self.assertRaises(SystemExit) as cm:
                gi.preflight_interpreter("openai", dry_run=False)
        self.assertNotEqual(cm.exception.code, 0)

    def test_preflight_is_WIRED_before_any_planning_output(self):
        """The preflight must be CALLED from main(), ahead of the plan. $0, no network.

        The two in-process checks above prove the function refuses correctly, but
        neither says it is wired in — deleting the call from main() leaves them green
        while the V14 defect reproduces live. Ordering IS the premise here: the bug was
        never a bad exit code, it was ~80 lines of plan and a green spend guard printed
        first. So assert stdout is EMPTY.

        PYTHONPATH is used the way it actually works: it PRECEDES site-packages, so a
        stub module that raises ImportError wins even under the venv. The first cut of
        this test pointed PYTHONPATH at an empty dir and expected that to HIDE the real
        SDK — it does not, and the test billed ~$0.10 a run instead."""
        stub = Path(self.dir.name) / "stub"
        stub.mkdir(exist_ok=True)
        (stub / "openai.py").write_text('raise ImportError("stubbed for the $0 preflight test")\n')
        p = subprocess.run(
            [sys.executable, str(_SCRIPT), str(self.manifest), "--output", str(self.out),
             "--ledger", str(self.ledger), "--provider", "openai"],
            capture_output=True, text=True,
            env={**self.env, "PYTHONPATH": str(stub)}, timeout=60)
        self.assertEqual(p.returncode, 1)
        self.assertEqual(p.stdout.strip(), "",
                         "preflight must fire BEFORE any planning output")

    def test_preflight_exempts_dry_run_and_other_providers(self):
        """The preflight must not fire where no client is constructed."""
        import unittest.mock
        with unittest.mock.patch.dict(sys.modules, {"openai": None}):
            gi.preflight_interpreter("openai", dry_run=True)   # no raise
            gi.preflight_interpreter("flux", dry_run=False)    # no raise

    def test_dry_run_needs_no_sdk(self):
        """The preflight must NOT block --dry-run: it constructs no client, and cost
        checking from any interpreter is the habit we want to keep cheap."""
        self._widen_manifest(3)
        p = self._run("--dry-run")
        self.assertIn("dry run:", p.stdout)

    def test_cap_values_are_the_locked_figures(self):
        """Pin the LITERALS. Every other test references the symbols, so changing a
        constant would slide through silently — the value itself is the locked thing
        (Steve 2026-07-27: $9 first batch, $1 reprint). A change here must be
        deliberate and carry a Decisions_Log why-stub."""
        self.assertEqual(gi.POLICY_CAP_USD, 9.0)
        self.assertEqual(gi.REPRINT_CAP_USD, 1.0)

    def test_normal_first_batch_above_reprint_limit_passes(self):
        """THE primary production path: a first render with no prior spend, costing
        more than the $1 reprint limit but less than the $9 cap, must PASS.

        This pins the `p > 0.0` discriminator. Mutating it to `p >= 0.0` classifies
        every run as a reprint and caps normal first batches at $1 — and it survived
        all 20 previous tests, because every other fixture either seeds prior spend
        (making it a genuine reprint) or blocks for a reason that matches the reprint
        message too. 50 images ~= $4.75: over the reprint limit, well under the cap."""
        self._widen_manifest(50)
        p = self._run("--max-images", "150", "--max-cost", "99")
        out = p.stdout + p.stderr
        self.assertIn("spend guard: OK", out, "a normal first batch must not be gated")
        self.assertNotIn("--over-cap-ok", out)

    def test_reprint_limit_boundary_brackets(self):
        """Brackets the reprint limit from both sides: ~$0.95 passes, ~$1.05 blocks.

        HONEST LIMIT: this does NOT pin `>` vs `>=` at exactly $1.00 — image cost is
        ~$0.095/unit so no integer count lands on $1.0000, and mutating `>` to `>=`
        survives this fixture. Pinning that boundary would need the gate predicate
        extracted from main() into a callable. Economically irrelevant (a batch
        costing exactly $1.0000 does not occur), so it is documented, not chased."""
        self._seed_prior(0.50)
        self._widen_manifest(10)
        p = self._run("--max-images", "150", "--max-cost", "99")
        self.assertIn("spend guard: OK", p.stdout + p.stderr)
        self._widen_manifest(11)
        p2 = self._run("--max-images", "150", "--max-cost", "99")
        self.assertIn("reprint limit", p2.stdout + p2.stderr)

    def test_reprint_within_limit_passes(self):
        """A video already at the cap may still take a small reprint.

        This is the POINT of the 2026-07-27 split: a single $10 cap could be fully
        consumed by pass 1, leaving nothing to fix the shots review then rejects.
        $9 first batch + $1 reserved makes the fix pass affordable by construction."""
        self._seed_prior(gi.POLICY_CAP_USD)          # first batch spent the whole cap
        p = self._run()                              # ~$0.10 reprint
        self.assertIn("spend guard: OK", p.stdout)
        self.assertNotIn("--over-cap-ok", p.stderr)

    def _widen_manifest(self, n):
        """Rewrite the fixture manifest to n images (~$0.10 each) to drive a real
        dollar estimate. One image can never exceed the $1 reprint limit."""
        self.manifest.write_text(json.dumps({
            "project": "cap-test",
            "images": [{"name": f"Video_99_Shot_{i:02d}", "prompt": "test shot",
                        "use_references": False} for i in range(1, n + 1)],
        }))

    def test_reprint_over_limit_blocks_even_under_ceiling(self):
        """A reprint bigger than $1 blocks even when the TOTAL stays under the ceiling.

        Without this the reprint limit is decorative: a video with $0.50 of prior spend
        could bill another $8 and still sit below the $10 ceiling. 20 images ~= $2.00
        against $0.50 prior = $2.50 total — comfortably under the ceiling, and it must
        STILL block because the reprint half is what was exceeded."""
        self._seed_prior(0.50)
        self._widen_manifest(20)
        p = self._run("--max-images", "150", "--max-cost", "99")
        self.assertNotEqual(p.returncode, 0, "a >$1 reprint must block")
        out = p.stdout + p.stderr
        self.assertIn("reprint limit", out)
        self.assertIn("--over-cap-ok", out)

    def test_first_batch_over_cap_blocks_at_nine_not_ten(self):
        """With NO prior spend the ceiling is POLICY_CAP_USD alone — the $1 reprint
        reserve is not available to a first batch. ~95 images ~= $9.50: over $9,
        under $10, so it must block (it would have passed under the old single cap)."""
        self._widen_manifest(95)                     # ~$9.03: over $9, under $10
        p = self._run("--max-images", "150", "--max-cost", "99")
        out = p.stdout + p.stderr
        # NOT assertNotEqual(returncode, 0): the flux stub always exits non-zero, so
        # that passes whether the gate fired or not. NOT assertIn("policy cap") either
        # — that string also appears in the benign "no ledger" NOTE. Assert the one
        # phrase only a real cap BLOCK emits.
        self.assertIn("--over-cap-ok", out, "a first batch over $9 must block at the gate")
        self.assertIn("over the", out)

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
             "cost_usd": gi.POLICY_CAP_USD + gi.REPRINT_CAP_USD}) + "\n")
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
             "cost_usd": gi.POLICY_CAP_USD + gi.REPRINT_CAP_USD}) + "\n")
        p = self._run()
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("Video_98", p.stderr + p.stdout)

    def test_policy_gate_key_unit(self):
        self.assertEqual(gi.policy_gate_key("Video_98_Thumbnail_pop", "Video_99"),
                         "Video_98")
        self.assertEqual(gi.policy_gate_key("CTA_outro", "Video_99"), "Video_99")


if __name__ == "__main__":
    unittest.main(verbosity=1)
