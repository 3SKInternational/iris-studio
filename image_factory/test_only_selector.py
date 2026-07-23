#!/usr/bin/env python3
"""Regression pins for generate_images.py --only (DQ-33).

Both cases here are bugs that actually shipped into review and were caught by
the binary gate, on the BILLED image path. Neither is hypothetical:

  1. `--only ""` (an unset `--only "$SHOTS"` in a wrapper) rendered 0 images and
     exited 0 — "done" reads as success, so the operator ships stale PNGs.
  2. `--only` on a duplicated shot name kept the FIRST manifest entry, but a full
     batch leaves the LAST one on disk (os.replace). The duplicated pairs in
     Video_01_orchestrated.json carry DIFFERENT prompts, so a re-roll would have
     paid ~$0.13 to silently swap a shipped frame for a different scene.

Runs the real CLI as a subprocess in --dry-run. No API calls, no spend, no network.
    python3 image_factory/test_only_selector.py
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
CLI = HERE / "generate_images.py"

# The two entries share a name but differ in quality, so the dry-run's per-shot
# "[quality/size/...]" tag tells us WHICH entry was selected — that is the whole
# assertion for keep-last, and it needs no provider stub.
MANIFEST = {
    "project": "dup-fixture",
    "images": [
        {"name": "Shot_dup", "prompt": "FIRST entry — a full batch discards this one.",
         "quality": "low", "use_references": False},
        {"name": "Shot_dup", "prompt": "LAST entry — this is what ends up on disk.",
         "quality": "high", "use_references": False},
        {"name": "Shot_solo", "prompt": "An ordinary un-duplicated shot.",
         "quality": "low", "use_references": False},
    ],
}


def run(manifest_path, out_dir, *args):
    p = subprocess.run(
        [sys.executable, str(CLI), str(manifest_path), "--output", str(out_dir),
         "--dry-run", *args],
        capture_output=True, text=True,
    )
    return p.returncode, p.stdout + p.stderr


def main():
    checks = 0

    def ck(cond, msg):
        nonlocal checks
        if not cond:
            print(f"FAIL: {msg}", file=sys.stderr)
            sys.exit(1)
        checks += 1

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        mf, out = td / "m.json", td / "out"
        mf.write_text(json.dumps(MANIFEST))

        # 1. empty --only must be FATAL, never a silent 0-image success
        for empty in ("", ",", "   ", " , "):
            rc, o = run(mf, out, "--only", empty, "--force")
            ck(rc != 0, f"--only {empty!r} must exit non-zero (got {rc})")
            ck("no shot names given" in o, f"--only {empty!r} must explain itself")

        # 2. --only on a duplicated name selects the LAST entry (what a full
        #    batch leaves on disk), not the first.
        rc, o = run(mf, out, "--only", "Shot_dup", "--force")
        ck(rc == 0, f"valid --only should succeed (got {rc}): {o}")
        ck("high/" in o, "keep-LAST: must select the 'high' entry (#2), not 'low' (#1)")
        ck("low/" not in o, "keep-LAST: must NOT render the discarded first entry")
        ck("1 of 1 image(s) would bill" in o, "duplicates must collapse to ONE billed render")
        ck("using the LAST of each" in o, "the dedupe must announce which entry it kept")

        # 3. a normal (un-duplicated) selection is unaffected
        rc, o = run(mf, out, "--only", "Shot_solo", "--force")
        ck(rc == 0 and "1 of 1 image(s) would bill" in o, "solo shot should bill exactly once")
        ck("using the LAST of each" not in o, "no dedupe warning when there are no duplicates")

        # 4. an unknown name is fatal, not a silent no-op
        rc, o = run(mf, out, "--only", "Shot_nope", "--force")
        ck(rc != 0 and "not in this manifest" in o, "unknown shot must be fatal")

        # 5. --only + --limit is refused (limit slices by manifest order)
        rc, o = run(mf, out, "--only", "Shot_solo", "--limit", "1", "--force")
        ck(rc != 0 and "cannot be combined" in o, "--only + --limit must be refused")

        # 6. skip-existing is honoured in dry-run: an existing PNG bills $0.00
        out.mkdir(parents=True, exist_ok=True)
        (out / "Shot_solo.png").write_bytes(b"stub")
        rc, o = run(mf, out, "--only", "Shot_solo")          # no --force
        ck(rc == 0, "existing-PNG dry run should succeed")
        ck("0 of 1 image(s) would bill" in o and "~$0.00" in o,
           "an existing PNG must estimate $0.00, not a phantom charge")

    print(f"only-selector self-check: PASS ({checks} assertions)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
