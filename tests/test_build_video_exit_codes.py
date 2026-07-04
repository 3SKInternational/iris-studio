#!/usr/bin/env python3
"""Regression tests for build_video / generate_vo exit-code propagation.

Locks the invariant a scheduled runner depends on: a FATAL error in a stage must
make the process exit NON-ZERO. The bug this guards (2026-06-20): a VO stage that
failed (missing shot list, or a kit that parsed to zero usable narration) could
exit 0, so run_claude_job.sh / watchdog.sh / pipeline_orchestrator marked the
stage "done" with no artifact on disk.

Stdlib unittest + subprocess only (no network, no API keys, no real vault). Every
path tested here is fatal BEFORE any billed/network call, so the tests are
hermetic. Run:
    python3 tests/test_build_video_exit_codes.py
or under the suite:
    python3 -m unittest discover -s tests -v
"""

import contextlib
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
_BUILD_VIDEO = _REPO / "build_video.py"
_GENERATE_VO = _REPO / "vo_factory" / "generate_vo.py"


def _load_build_video():
    spec = importlib.util.spec_from_file_location("build_video", _BUILD_VIDEO)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(cmd, env=None):
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    return subprocess.run(
        cmd, capture_output=True, text=True, env=full_env, cwd=str(_REPO)
    )


class TestBuildVideoExitCodes(unittest.TestCase):
    def test_missing_shot_list_exits_nonzero(self):
        # The reported repro: --vo against a video with no shot list. The fatal
        # `die` fires before any manifest authoring or billed call, so an empty
        # vault is enough — and nothing is written into the repo.
        with tempfile.TemporaryDirectory() as td:
            r = _run(
                [sys.executable, str(_BUILD_VIDEO), "Video_77", "--vo"],
                env={"SK_VAULT": td},
            )
            self.assertNotEqual(r.returncode, 0, f"stdout={r.stdout}\nstderr={r.stderr}")
            self.assertIn("shot list not found", r.stdout + r.stderr)


class TestGenerateVoExitCodes(unittest.TestCase):
    def _write_kit(self, body: str) -> Path:
        td = tempfile.mkdtemp()
        kit = Path(td) / "_VO_Session_B_Kit.md"
        kit.write_text(textwrap.dedent(body), encoding="utf-8")
        return kit

    def test_empty_narration_kit_exits_nonzero(self):
        # Scene header present but no narration under it -> parse_kit returns [].
        # Before the fix this fell through to "nothing to do" + exit 0 with zero
        # mp3s. --dry-run is enough: the fatal guard runs before the dry-run branch.
        kit = self._write_kit(
            """\
            ## Scene 1 -> `Video_99_VO_Scene_01.mp3` (cold open)

            ---
            """
        )
        r = _run([sys.executable, str(_GENERATE_VO), str(kit), "--dry-run"])
        self.assertNotEqual(r.returncode, 0, f"stdout={r.stdout}\nstderr={r.stderr}")

    def test_no_scene_blocks_kit_exits_nonzero(self):
        kit = self._write_kit("just some prose, no scene headers at all\n")
        r = _run([sys.executable, str(_GENERATE_VO), str(kit), "--dry-run"])
        self.assertNotEqual(r.returncode, 0, f"stdout={r.stdout}\nstderr={r.stderr}")

    def test_valid_kit_dry_run_exits_zero(self):
        # Guards against a false-positive: a well-formed kit must still succeed.
        kit = self._write_kit(
            """\
            ## Scene 1 -> `Video_99_VO_Scene_01.mp3` (cold open)

            The median American household holds a few thousand dollars in savings.

            ---
            """
        )
        r = _run([sys.executable, str(_GENERATE_VO), str(kit), "--dry-run"])
        self.assertEqual(r.returncode, 0, f"stdout={r.stdout}\nstderr={r.stderr}")


class TestGenerateVoStaleGuard(unittest.TestCase):
    """V7 incident (2026-07-04): a skip-existing run kept 12 mp3s rendered from a
    RETIRED kit (mp3s older than the kit) and exited 0 — build_video printed
    "done." with zero generated. The guard must make any skip loud, and a
    kit-newer-than-skipped-mp3 skip fatal. It fires before the dry-run branch,
    so --dry-run exercises it hermetically (no key, no billed call)."""

    def _setup(self):
        td = Path(tempfile.mkdtemp())
        kit = td / "_VO_Session_B_Kit.md"
        kit.write_text(
            textwrap.dedent(
                """\
                ## Scene 1 -> `Video_99_VO_Scene_01.mp3` (cold open)

                The median American household holds a few thousand dollars in savings.

                ---
                """
            ),
            encoding="utf-8",
        )
        out = td / "gen"
        out.mkdir()
        mp3 = out / "Video_99_VO_Scene_01.mp3"
        mp3.write_bytes(b"\x00")
        return kit, out, mp3

    def _gv(self, kit, out, *extra):
        return _run([sys.executable, str(_GENERATE_VO), str(kit),
                     "--output", str(out), "--dry-run", *extra])

    def test_skipped_mp3_older_than_kit_exits_nonzero(self):
        kit, out, mp3 = self._setup()
        old = 1_600_000_000  # 2020 — well before the kit's just-written mtime
        os.utime(mp3, (old, old))
        r = self._gv(kit, out)
        self.assertNotEqual(r.returncode, 0, f"stdout={r.stdout}\nstderr={r.stderr}")
        self.assertIn("STALE VO", r.stdout + r.stderr)
        self.assertIn("skipped (existing)", r.stdout + r.stderr)

    def test_allow_stale_overrides(self):
        kit, out, mp3 = self._setup()
        old = 1_600_000_000
        os.utime(mp3, (old, old))
        r = self._gv(kit, out, "--allow-stale")
        self.assertEqual(r.returncode, 0, f"stdout={r.stdout}\nstderr={r.stderr}")
        self.assertIn("skipped (existing)", r.stdout + r.stderr)

    def test_fresh_skip_exits_zero_but_warns(self):
        # mp3 written AFTER the kit → a legitimate skip: loud summary, exit 0.
        kit, out, mp3 = self._setup()
        r = self._gv(kit, out)
        self.assertEqual(r.returncode, 0, f"stdout={r.stdout}\nstderr={r.stderr}")
        self.assertIn("skipped (existing)", r.stdout + r.stderr)

    def test_force_run_has_no_skip_summary(self):
        kit, out, mp3 = self._setup()
        old = 1_600_000_000
        os.utime(mp3, (old, old))
        r = self._gv(kit, out, "--force")  # nothing skipped → guard silent
        self.assertEqual(r.returncode, 0, f"stdout={r.stdout}\nstderr={r.stderr}")
        self.assertNotIn("skipped (existing)", r.stdout + r.stderr)


class TestBuildVideoArtifactVerify(unittest.TestCase):
    """White-box: the headline of the fix is that a stage which exits 0 WITHOUT
    writing its artifact must still fail the build. Stub the gen subprocess to
    return 0 and write nothing, then assert build_video exits non-zero."""

    def _vault(self, td: Path):
        (td / "Scene_Image_Prompts").mkdir(parents=True)
        (td / "Voice_Files" / "Video_99").mkdir(parents=True)
        (td / "Scene_Image_Prompts" / "Video_99_Shot_List.md").write_text(
            textwrap.dedent(
                """\
                # Video_99 Shot List

                ### Shot 1a — establishing
                ```text
                A calm study, Three at a desk reviewing a ledger.
                ```
                """
            ),
            encoding="utf-8",
        )
        (td / "Voice_Files" / "Video_99" / "_VO_Session_B_Kit.md").write_text(
            textwrap.dedent(
                """\
                # Video_99 VO Kit

                ## Scene 1 -> `Video_99_VO_Scene_01.mp3` (cold open)

                The median household saves little. <break time="0.5s"/> Here is why.

                ---
                """
            ),
            encoding="utf-8",
        )

    def test_vo_stage_exit0_no_mp3_fails_build(self):
        bv = _load_build_video()
        with tempfile.TemporaryDirectory() as tds:
            td = Path(tds)
            self._vault(td)
            orig_run, orig_write = bv.run, bv.write_json
            bv.run = lambda cmd, *, label: 0          # subprocess "succeeds"...
            bv.write_json = lambda *a, **k: None       # ...but no manifest/repo writes
            argv = sys.argv
            sys.argv = ["build_video.py", "Video_99", "--vo"]
            os.environ["SK_VAULT"] = str(td)
            try:
                with self.assertRaises(SystemExit) as cm, \
                        contextlib.redirect_stdout(io.StringIO()), \
                        contextlib.redirect_stderr(io.StringIO()):
                    bv.main()
                # SystemExit(1) (truthy/non-zero) — the missing mp3 forced the fail.
                self.assertNotIn(cm.exception.code, (0, None))
            finally:
                bv.run, bv.write_json = orig_run, orig_write
                sys.argv = argv
                os.environ.pop("SK_VAULT", None)

    def test_vo_stage_exit0_with_mp3_passes(self):
        # Same path, but the artifact IS present -> the verify must NOT false-fail.
        bv = _load_build_video()
        with tempfile.TemporaryDirectory() as tds:
            td = Path(tds)
            self._vault(td)
            vo_out = td / "Voice_Files" / "Video_99_gen"
            vo_out.mkdir(parents=True)
            (vo_out / "Video_99_VO_Scene_01.mp3").write_bytes(b"\x00\x01")
            orig_run, orig_write = bv.run, bv.write_json
            bv.run = lambda cmd, *, label: 0
            bv.write_json = lambda *a, **k: None
            argv = sys.argv
            sys.argv = ["build_video.py", "Video_99", "--vo"]
            os.environ["SK_VAULT"] = str(td)
            try:
                with contextlib.redirect_stdout(io.StringIO()), \
                        contextlib.redirect_stderr(io.StringIO()):
                    bv.main()  # no SystemExit -> clean success
            finally:
                bv.run, bv.write_json = orig_run, orig_write
                sys.argv = argv
                os.environ.pop("SK_VAULT", None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
