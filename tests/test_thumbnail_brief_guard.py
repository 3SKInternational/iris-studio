#!/usr/bin/env python3
"""Guard for `thumbnail_brief_paths` — the thumbnail-coordinator provenance gate.

WHY THIS EXISTS. Thumbnail art bills inside the stage-5 batch, so "was the
coordinator actually run?" has to be answered BEFORE money moves. On 2026-07-27
V14's thumbnails billed with no brief at all, and it surfaced only because Steve
asked (V14 was briefed the same day, retroactively). Briefs were missing for
V05-07, V09-11 at the time this landed and present for V01-04, V08, V12, V13 —
so the discriminator has to be real, not a function that returns truthy for
everything. Do not re-hardcode the live coverage list here; it changes, and the
real corpus is one call away. An earlier attempt to gate this by inspecting manifest
CONTENTS was premise-wrong (the art belongs in the billed batch) and reverted;
this one gates on the brief file, so its file-matching is the whole contract.

Run: python3 tests/test_thumbnail_brief_guard.py
"""

import importlib.util
import pathlib
import shutil
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("po", REPO / "scripts" / "pipeline_orchestrator.py")
po = importlib.util.module_from_spec(spec)
spec.loader.exec_module(po)

FAILS = []


def check(name, cond, detail=""):
    if cond:
        print(f"  [ok ] {name}")
    else:
        print(f"  [FAIL] {name} {detail}")
        FAILS.append(name)


def main():
    print("thumbnail-coordinator brief guard")

    root = pathlib.Path(tempfile.mkdtemp(prefix="tbg_"))
    thumbs = root / po.VAULT_REL / "Thumbnails"
    pack = root / po.VAULT_REL / "Packaging"
    thumbs.mkdir(parents=True)
    pack.mkdir(parents=True)

    real = po.WORKSPACE_DIR
    po.WORKSPACE_DIR = root
    try:
        # Absent -> empty LIST (not None). This is the blocking case; if it ever
        # returns truthy on an empty tree the gate is decorative, and if it returns
        # None the caller fails open and the gate never fires at all.
        check("no brief -> empty list (blocks)", po.thumbnail_brief_paths(14) == [])

        (thumbs / "Video_14_Thumbnail_Brief.md").write_text("x")
        check("canonical brief found", po.thumbnail_brief_paths(14) ==
              [f"{po.VAULT_REL}/Thumbnails/Video_14_Thumbnail_Brief.md"])

        # The real vault names briefs several ways — the glob must catch the
        # variants that already exist on disk, or videos that DID get a brief
        # would be blocked and Steve would learn to reach for --force.
        (thumbs / "Video_13_Thumbnail_Brief_v2.md").write_text("x")
        (thumbs / "Video_08_Thumbnail_B_regen_brief.md").write_text("x")
        (pack / "Video_03_Thumbnail_Brief_repackage.md").write_text("x")
        check("v2 suffix variant", po.thumbnail_brief_paths(13) != [])
        check("infix variant (_B_regen_brief)", po.thumbnail_brief_paths(8) != [])
        check("Packaging/ dir searched", po.thumbnail_brief_paths(3) ==
              [f"{po.VAULT_REL}/Packaging/Video_03_Thumbnail_Brief_repackage.md"])

        # Zero-padding must be exact: video 4 must not match Video_14's brief.
        # `Video_4_*` would glob nothing and `Video_1*` would over-match, and a
        # neighbouring video's brief satisfying the gate is a silent pass.
        check("no cross-video match (4 vs 14)", po.thumbnail_brief_paths(4) == [])

        # Both dirs at once, sorted + deduped by path.
        (pack / "Video_14_Thumbnail_Brief_repackage.md").write_text("x")
        got = po.thumbnail_brief_paths(14)
        check("both dirs collected", len(got) == 2 and got == sorted(got), f"got={got}")

        # NEGATIVE: a Video_NN_Thumbnail*.md that is NOT a brief must not satisfy a
        # spend gate. The real corpus has one (Packaging/Video_08_Thumbnail_AB_corrected.md);
        # without a fixture like this, deleting the "brief" filter outright survives.
        (thumbs / "Video_09_Thumbnail_AB_corrected.md").write_text("x")
        (thumbs / "Video_09_Thumbnail_Debrief.md").write_text("x")     # leading boundary
        (thumbs / "Video_09_Thumbnail_Briefing_notes.md").write_text("x")  # trailing boundary
        (thumbs / "Video_09_Thumbnail_Brief.md.bak").write_text("x")   # not a .md
        check("non-brief thumbnail docs do NOT satisfy the gate",
              po.thumbnail_brief_paths(9) == [], f"got={po.thumbnail_brief_paths(9)}")

        # Case-insensitive across the WHOLE name, not just "brief" — APFS is
        # case-insensitive but pathlib.glob is not, which is how the lowercase
        # `_regen_brief` was missed in the first cut.
        (thumbs / "Video_10_thumbnail_brief.md").write_text("x")
        check("all-lowercase name still matches", po.thumbnail_brief_paths(10) != [])

        # A missing subtree must not raise, and must report None ("could not
        # check") so the caller fails OPEN — returning [] here would block every
        # spend the moment the vault is unmounted.
        po.WORKSPACE_DIR = root / "nope"
        check("missing tree -> None (fail-open), no raise",
              po.thumbnail_brief_paths(14) is None)
    finally:
        po.WORKSPACE_DIR = real
        shutil.rmtree(root, ignore_errors=True)

    if FAILS:
        print(f"\ntest_thumbnail_brief_guard: FAIL ({len(FAILS)}): {', '.join(FAILS)}")
        return 1
    print("\ntest_thumbnail_brief_guard: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
