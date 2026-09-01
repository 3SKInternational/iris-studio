#!/usr/bin/env python3
"""Guards for render_short.py — the Shorts-pack -> 9:16 MP4 renderer.

The render itself (VO + ffmpeg) is owned by the two factories and covered by
their suites. What is NEW and untested here is the glue that decides WHAT gets
spoken and captioned:

  * parse_pack — pull each Short's VO body (a `> ...` blockquote) and its reuse
    PNG paths out of the markdown. A miss here renders the wrong words or no
    image.
  * tokenize_numbers — a TTS-hazard million must become a {{spoken|caption}}
    token (VO says the safe form, caption keeps the digits), and a
    hand-fix-only hazard must BLOCK, never ship mis-spoken money.
  * caption_form — the caption keeps the RIGHT (digits) side of the token.
  * split_caption — never strand a still with no group and never leave fewer
    sentences than the groups still to fill.

Run: python3 tests/test_render_short.py
"""
import contextlib
import importlib.util
import io
import os
import pathlib
import sys
import tempfile
import time
import unittest
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parent.parent


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rs = _load("render_short", "render_short.py")
vnl = _load("vo_number_lint", "scripts/vo_number_lint.py")

PACK = """# Video 99 — Shorts Production Pack

## Short 1 — "The Ten Million"

- **Hook (<=2s):** There is money.
- **VO (~40 words, <60s):**
  > There is $10,000,000 in the account. Not $10,000. Nobody knows.
  > The car has 183,000 miles on it.
- **On-screen text beats:**
  1. `nobody knows`
- **Image reuse:**
  - reuse `/abs/Video_99_Shot_01a.png` (from x) - crop 9:16
  - reuse `/abs/Video_99_Shot_02a.png` (from y) - crop 9:16
- **CTA:** Watch the full ladder.

## Short 2 — "The Twelve Hundred"

- **VO (~20 words):**
  > You put $1,200 in savings. The silence it buys is not small.
- **Image reuse:**
  - reuse `/abs/Video_99_Shot_05a.png` (from z) - crop 9:16
"""


class TestParse(unittest.TestCase):
    def test_splits_all_shorts(self):
        shorts = rs.parse_pack(PACK)
        self.assertEqual([s["n"] for s in shorts], [1, 2])

    def test_vo_body_joined_without_quote_marks(self):
        s1 = rs.parse_pack(PACK)[0]
        self.assertIn("There is $10,000,000 in the account.", s1["vo"])
        self.assertIn("183,000 miles", s1["vo"])
        self.assertNotIn(">", s1["vo"])

    def test_reuse_images_captured_in_order(self):
        s1 = rs.parse_pack(PACK)[0]
        self.assertEqual(s1["images"],
                         ["/abs/Video_99_Shot_01a.png", "/abs/Video_99_Shot_02a.png"])

    def test_hook_and_beats_are_not_treated_as_vo(self):
        # The VO must be the blockquote under **VO**, not the Hook line.
        s1 = rs.parse_pack(PACK)[0]
        self.assertNotIn("Nobody knows.\n1.", s1["vo"])
        self.assertNotIn("nobody knows", s1["vo"])  # that's a beat, not VO


class TestNumbers(unittest.TestCase):
    def test_million_becomes_dual_form_token(self):
        out = rs.tokenize_numbers("There is $10,000,000 here.", vnl)
        self.assertIn("{{$10 million|$10,000,000}}", out)

    def test_one_comma_figures_left_alone(self):
        # ElevenLabs reads these fine; do not touch them.
        out = rs.tokenize_numbers("You put $1,200 in. 183,000 miles.", vnl)
        self.assertEqual(out, "You put $1,200 in. 183,000 miles.")

    def test_hand_fix_hazard_blocks(self):
        # A sub-thousand-precision million the linter can only approximate must
        # fail-closed, not ship a wrong spoken amount.
        with self.assertRaises(SystemExit):
            rs.tokenize_numbers("He had $1,043,567 saved.", vnl)

    def test_clean_round_million_tokenizes_not_blocks(self):
        # $1.043 million round-trips (thousands preserved) -> tokenize, don't block.
        out = rs.tokenize_numbers("She hit $1,043,000 this year.", vnl)
        self.assertIn("{{$1.043 million|$1,043,000}}", out)

    def test_repeated_number_not_nested(self):
        # Regression: a global replace-per-finding double-tokenized a repeated
        # figure into {{safe|{{safe|raw}}}}. Single-pass must not nest.
        out = rs.tokenize_numbers("$10,000,000 and again $10,000,000.", vnl)
        self.assertEqual(out.count("{{"), 2)
        self.assertEqual(out.count("}}"), 2)
        self.assertNotIn("{{$10 million|{{", out)

    def test_preexisting_dual_form_token_left_untouched(self):
        # Regression: a figure the author already wrapped as {{spoken|caption}}
        # must be scanned around, not through — re-tokenizing it nested the token
        # ({{spoken|{{spoken|$X}}}}) and generate_vo then read the braces aloud.
        src = "You get {{$5 million|$5,000,000}} now."
        out = rs.tokenize_numbers(src, vnl)
        self.assertEqual(out, src)
        self.assertNotIn("{{$5 million|{{", out)

    def test_hand_fix_hazard_recovery_survives_rerun(self):
        # The die-message tells the author to wrap the hazard in a dual-form token.
        # That fix must then SATISFY the gate on re-run, not re-die on the same
        # figure sitting on the caption side.
        fixed = "He had {{one million forty-three thousand dollars|$1,043,567}} saved."
        out = rs.tokenize_numbers(fixed, vnl)  # must not raise
        self.assertEqual(out, fixed)

    def test_caption_keeps_digits(self):
        spoken = rs.tokenize_numbers("There is $10,000,000 here.", vnl)
        self.assertEqual(rs.caption_form(spoken), "There is $10,000,000 here.")


class TestCaptionSplit(unittest.TestCase):
    def test_every_still_gets_a_group(self):
        for n in (1, 2, 3, 4, 5):
            groups = rs.split_caption("One. Two. Three. Four.", n)
            self.assertEqual(len(groups), n)

    def test_fewer_sentences_than_stills_pads_not_crashes(self):
        groups = rs.split_caption("Only one sentence here.", 3)
        self.assertEqual(len(groups), 3)
        self.assertEqual(groups[0], "Only one sentence here.")
        self.assertEqual(groups[1:], ["", ""])

    def test_no_group_starved_when_balanced(self):
        text = "A A A. B B B. C C C. D D D. E E E. F F F."
        groups = rs.split_caption(text, 3)
        self.assertTrue(all(g.strip() for g in groups),
                        f"a still was left with no caption: {groups}")
        # Every sentence survives the split (nothing dropped).
        self.assertEqual(" ".join(groups).count("."), text.count("."))


class TestParseMore(unittest.TestCase):
    def test_title_captured(self):
        shorts = rs.parse_pack(PACK)
        # The title is the header tail with the em-dash + quotes stripped —
        # not the Short number and not the body.
        self.assertEqual(shorts[0]["title"], "The Ten Million")
        self.assertEqual(shorts[1]["title"], "The Twelve Hundred")

    def test_short_without_vo_block_is_empty_not_crash(self):
        pack = ('## Short 1 — "No VO here"\n\n'
                "- **Image reuse:**\n  - reuse `/a/x.png` (from q) - crop 9:16\n")
        s = rs.parse_pack(pack)[0]
        self.assertEqual(s["vo"], "")
        self.assertEqual(s["images"], ["/a/x.png"])


class TestCaptionBalance(unittest.TestCase):
    def test_split_caption_balances_across_groups(self):
        # 6 equal sentences into 3 groups must land 2 per group. Pins the
        # target/close-the-group arithmetic (an off-by-one unbalances it).
        text = "A A. B B. C C. D D. E E. F F."
        groups = rs.split_caption(text, 3)
        self.assertEqual([g.count(".") for g in groups], [2, 2, 2])

    def test_single_group_returns_caption_verbatim(self):
        # n<=1 returns the caption untouched (the fast path, no re-join).
        self.assertEqual(rs.split_caption("A.  B.", 1), ["A.  B."])

    def test_two_short_sentences_pad_not_merge(self):
        # 2 sentences into 3 groups: one per group plus a pad, never merged.
        self.assertEqual(rs.split_caption("A. B.", 3), ["A.", "B.", ""])

    def test_uneven_partition_keeps_a_sentence_for_the_last_group(self):
        # The lookahead must not sweep every sentence into group 1.
        self.assertEqual(rs.split_caption("A. BB. CCC. DDDD.", 2),
                         ["A. BB. CCC.", "DDDD."])


class TestWriteKit(unittest.TestCase):
    def test_returns_mp3_name_per_short(self):
        shorts = rs.parse_pack(PACK)
        with tempfile.TemporaryDirectory() as d:
            names = rs.write_kit(shorts, "Video_99", pathlib.Path(d) / "k.md", vnl)
            self.assertEqual(names[1], "Video_99_Short_1_VO.mp3")
            self.assertEqual(names[2], "Video_99_Short_2_VO.mp3")

    def test_no_rewrite_when_content_unchanged(self):
        # generate_vo treats a kit NEWER than its mp3 as fatal, so an identical
        # re-run must not touch the file. Guard against a rewrite-every-time.
        shorts = rs.parse_pack(PACK)
        with tempfile.TemporaryDirectory() as d:
            kit = pathlib.Path(d) / "kit.md"
            rs.write_kit(shorts, "Video_99", kit, vnl)
            first_mtime = kit.stat().st_mtime_ns
            first_bytes = kit.read_bytes()
            time.sleep(0.01)
            rs.write_kit(shorts, "Video_99", kit, vnl)
            self.assertEqual(kit.stat().st_mtime_ns, first_mtime)
            self.assertEqual(kit.read_bytes(), first_bytes)


class TestAuthorManifest(unittest.TestCase):
    def _short(self, images):
        return {"n": 1, "title": "T", "vo": "One. Two. Three. Four.",
                "images": images}

    def test_manifest_bounds_motion_size_captions(self):
        s = self._short(["/a/x1.png", "/a/x2.png", "/a/x3.png"])
        with mock.patch.object(rs, "ffprobe_seconds", return_value=9.0):
            man = rs.author_manifest(s, pathlib.Path("/tmp/x.mp3"),
                                     pathlib.Path("/out"), "Video_99_Short_1")
        self.assertEqual(man["output_size"], [1080, 1920])
        self.assertEqual(len(man["shots"]), 3)
        # bounds tile [0..dur] with no gap or overlap
        self.assertEqual(man["shots"][0]["start"], 0.0)
        self.assertEqual(man["shots"][-1]["end"], 9.0)
        for a, b in zip(man["shots"], man["shots"][1:]):
            self.assertEqual(a["end"], b["start"])
        # motions cycle through MOTIONS in order
        self.assertEqual([sh["motion"] for sh in man["shots"]], rs.MOTIONS[:3])
        self.assertTrue(all(sh["vo_clip"].endswith("x.mp3") for sh in man["shots"]))
        self.assertTrue(all("caption_text" in sh for sh in man["shots"]))

    def test_no_images_dies(self):
        with mock.patch.object(rs, "ffprobe_seconds", return_value=9.0):
            with self.assertRaises(SystemExit):
                rs.author_manifest(self._short([]), pathlib.Path("/tmp/x.mp3"),
                                   pathlib.Path("/out"), "Video_99_Short_1")


class TestMainDriver(unittest.TestCase):
    """Drive main() end-to-end with the two subprocess factories + ffprobe
    stubbed, so the orchestration logic (selection, dry-run gate, die-guards)
    is exercised in-process without spend."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.pack = self.root / "Video_99_Shorts.md"
        self.pack.write_text(PACK, encoding="utf-8")
        self._env = os.environ.get("SK_VAULT")
        os.environ["SK_VAULT"] = str(self.root / "vault")

    def tearDown(self):
        if self._env is None:
            os.environ.pop("SK_VAULT", None)
        else:
            os.environ["SK_VAULT"] = self._env
        self._tmp.cleanup()

    def _fake_run(self):
        """Stub subprocess.run: a generate_vo call drops the expected mp3s into
        --output; an assemble call is a no-op. Both return rc 0."""
        calls = []

        def run(cmd, *a, **k):
            calls.append([str(c) for c in cmd])
            joined = " ".join(str(c) for c in cmd)
            if "generate_vo" in joined and "--dry-run" not in joined:
                out = pathlib.Path(cmd[cmd.index("--output") + 1])
                vid = out.parent.name  # .../_work/<vid>/vo
                out.mkdir(parents=True, exist_ok=True)
                for n in range(1, 9):
                    (out / f"{vid}_Short_{n}_VO.mp3").write_bytes(b"x")

            class R:
                returncode = 0

            return R()

        return run, calls

    def _drive(self, argv):
        old = sys.argv
        sys.argv = ["render_short.py"] + argv
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                rs.main()
        finally:
            sys.argv = old

    def _assembles(self, calls):
        return [c for c in calls if "assemble" in " ".join(c)]

    def test_dry_run_gates_without_assemble(self):
        run, calls = self._fake_run()
        with mock.patch.object(rs.subprocess, "run", run):
            self._drive([str(self.pack), "--dry-run"])
        gen = [c for c in calls if "generate_vo" in " ".join(c)]
        self.assertTrue(gen)
        self.assertTrue(all("--dry-run" in " ".join(c) for c in gen))
        self.assertEqual(self._assembles(calls), [])

    def test_full_render_assembles_each_short(self):
        run, calls = self._fake_run()
        with mock.patch.object(rs.subprocess, "run", run), \
                mock.patch.object(rs, "ffprobe_seconds", return_value=6.0):
            self._drive([str(self.pack)])
        self.assertEqual(len(self._assembles(calls)), 2)  # the pack has 2 Shorts
        # Without --force, the flag must NOT leak into the generate_vo call.
        gen = [c for c in calls if "generate_vo" in " ".join(c)]
        self.assertTrue(gen and not any("--force" in " ".join(c) for c in gen))

    def test_only_selects_named_short(self):
        run, calls = self._fake_run()
        with mock.patch.object(rs.subprocess, "run", run), \
                mock.patch.object(rs, "ffprobe_seconds", return_value=6.0):
            self._drive([str(self.pack), "--only", "2"])
        asm = self._assembles(calls)
        self.assertEqual(len(asm), 1)
        self.assertTrue(any("Video_99_Short_2.json" in " ".join(c) for c in asm))

    def test_limit_one(self):
        run, calls = self._fake_run()
        with mock.patch.object(rs.subprocess, "run", run), \
                mock.patch.object(rs, "ffprobe_seconds", return_value=6.0):
            self._drive([str(self.pack), "--limit", "1"])
        asm = self._assembles(calls)
        self.assertEqual(len(asm), 1)
        self.assertTrue(any("Video_99_Short_1.json" in " ".join(c) for c in asm))

    def test_limit_zero_renders_nothing(self):
        run, calls = self._fake_run()
        with mock.patch.object(rs.subprocess, "run", run):
            self._drive([str(self.pack), "--limit", "0"])
        self.assertEqual(self._assembles(calls), [])

    def test_default_limit_caps_at_three(self):
        four = "\n".join(
            f'## Short {n} — "S{n}"\n\n- **VO:**\n  > Line {n} here.\n\n'
            f"- **Image reuse:**\n  - reuse `/a/x{n}.png` (from q) - crop 9:16\n"
            for n in (1, 2, 3, 4))
        p = self.root / "Video_97_Shorts.md"
        p.write_text(four, encoding="utf-8")
        run, calls = self._fake_run()
        with mock.patch.object(rs.subprocess, "run", run), \
                mock.patch.object(rs, "ffprobe_seconds", return_value=6.0):
            self._drive([str(p)])
        self.assertEqual(len(self._assembles(calls)), 3)  # default drip, not 4

    def test_missing_mp3_dies(self):
        def run(cmd, *a, **k):  # generate_vo produces no mp3
            class R:
                returncode = 0
            return R()
        with mock.patch.object(rs.subprocess, "run", run):
            with self.assertRaises(SystemExit):
                self._drive([str(self.pack)])

    def test_subprocess_failure_dies(self):
        def run(cmd, *a, **k):
            class R:
                returncode = 1
            return R()
        with mock.patch.object(rs.subprocess, "run", run):
            with self.assertRaises(SystemExit):
                self._drive([str(self.pack)])

    def test_bad_pack_path_dies(self):
        with self.assertRaises(SystemExit):
            self._drive([str(self.root / "Video_99_Shorts_nope.md")])

    def test_filename_without_video_id_dies(self):
        p = self.root / "Shorts.md"
        p.write_text(PACK, encoding="utf-8")
        with self.assertRaises(SystemExit):
            self._drive([str(p)])

    def test_force_flag_passed_through(self):
        run, calls = self._fake_run()
        with mock.patch.object(rs.subprocess, "run", run), \
                mock.patch.object(rs, "ffprobe_seconds", return_value=6.0):
            self._drive([str(self.pack), "--force"])
        gen = [c for c in calls if "generate_vo" in " ".join(c)]
        self.assertTrue(gen and all("--force" in " ".join(c) for c in gen))

    def test_empty_pack_dies(self):
        p = self.root / "Video_98_Shorts.md"
        p.write_text("# nothing parseable here\n", encoding="utf-8")
        # Stub the factories so the SystemExit can ONLY come from the empty-pack
        # guard, not from a real generate_vo failing on an empty kit.
        run, _ = self._fake_run()
        with mock.patch.object(rs.subprocess, "run", run):
            with self.assertRaises(SystemExit):
                self._drive([str(p)])

    def test_only_matching_nothing_dies(self):
        run, _ = self._fake_run()
        with mock.patch.object(rs.subprocess, "run", run):
            with self.assertRaises(SystemExit):
                self._drive([str(self.pack), "--only", "9"])


if __name__ == "__main__":
    unittest.main()
