#!/usr/bin/env python3
"""Guards for `derive_shots_from_hd_manifest` — the no-Shot_List fallback path.

WHY THIS EXISTS. This function had ZERO coverage while being the LIVE path for
V13 and V14 (the only two videos with no `Shot_List.md`). Its failure mode is
not a crash: it returns a plausible shot list that maps images to the WRONG VO
scenes, so the video renders fully, ships, and is wrong. Every case below was a
real code-review finding on 2026-07-27, not a hypothetical:

  * `_HD_SHOT_RE` required a scene-part letter, so it matched nothing on the
    letterless `Shot_02_3`, and its `\\b` terminator additionally failed on
    `Shot_04a_2` — between them, all 76 V14 shots went unmatched and the video
    assembled with "no images";
  * `id` composed from scene+sub produced `0203` for a shot named `02_3`, so all
    8 V14 card-overlay lookups missed;
  * scheme detection by `any()` let ONE V14-style entry appended to a V13-style
    manifest renumber every pre-existing shot onto a different VO scene;
  * kit ordinals are POSITIONAL, so a trimmed-away scene part shifts everything
    after it (manifests do get trimmed — V14 went 100 -> 76 entries). A MIDDLE or
    FRONT gap is caught structurally; a trimmed TAIL part is not, and needs the
    kit's own src-SCENE record as an oracle (verify_derived_ordinals).

The last two are silent-wrong-output, which a green suite does not see. Pin the
VALUES, not the counts: an earlier regression check compared shot COUNTS and
would have passed against a completely mis-ordered list.

Run: python3 tests/test_derive_shots.py
"""

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("bv", REPO / "build_video.py")
bv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bv)

# Loaded so the mutation-crash-guard check below can call mut._sidecar() instead
# of re-deriving the sidecar path. Three earlier rounds each shipped a check that
# recomputed a production path/formula locally, and each stayed green while the
# real thing regressed (HIGH-2, 2026-07-27) — this is the fix for the fourth one.
mut_spec = importlib.util.spec_from_file_location("mut_ds", REPO / "scripts" / "mutate.py")
mut = importlib.util.module_from_spec(mut_spec)
mut_spec.loader.exec_module(mut)

VAULT = pathlib.Path("~/Documents/3SK/outputs/BRANDS/3SK_Finance").expanduser()
FAILS = []
_n = [0]
# One dir per run: fixed names in a shared gettempdir() let two concurrent
# runs clobber each other mid-run, and the old teardown unlinked ds_*_hd.json
# globally.
TMP = pathlib.Path(tempfile.mkdtemp(prefix="ds_run_"))


def check(name, cond, detail=""):
    if cond:
        print(f"  [ok ] {name}")
    else:
        print(f"  [FAIL] {name} {detail}")
        FAILS.append(name)


def mk(names):
    """A manifest whose images are just these names."""
    _n[0] += 1
    p = TMP / f"ds_{_n[0]:03d}_hd.json"
    p.write_text(json.dumps({"images": [{"name": x, "prompt": "p"} for x in names]}))
    return p


def derive(names):
    vid = names[0].split("_Shot_")[0].split("_Thumbnail")[0]
    return bv.derive_shots_from_hd_manifest(mk(names), vid)


def dies(names):
    return dies_msg(names)[0]


def dies_msg(names):
    """(died, stderr). die() writes the message to stderr and raises SystemExit(1),
    so str(e) is just the exit code — the text is only on the stream."""
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            derive(names)
        return False, err.getvalue()
    except SystemExit:
        return True, err.getvalue()


def main():
    print("derive_shots_from_hd_manifest")

    # --- V09-V13 scheme: the number IS the scene, the letter is shot order -----
    got = derive(["Video_13_Shot_01a", "Video_13_Shot_01b", "Video_13_Shot_02a"])
    check("V13 scheme: scene/sub/id exact",
          [(s["scene"], s["sub"], s["id"]) for s in got] ==
          [(1, "a", "01a"), (1, "b", "01b"), (2, "a", "02a")], f"got={got}")

    # A V09-V13 PARTIAL re-render — the real `video_09_hd_regen3.json` shape: one
    # shot at scene 17 plus thumbnails. It legitimately violates every V14+ invariant
    # (doesn't start at scene 1, single part 'a'), so it MUST pass untouched. Without
    # this fixture, forcing the `if lettered_scheme:` guard True LOOKS equivalent —
    # it is not; it kills 18 real in-tree manifests including the live V13 source.
    got = derive(["Video_09_Shot_17a", "Video_09_Thumbnail_A", "Video_09_Thumbnail_B"])
    check("V09-V13 partial re-render (scene 17 only) is untouched",
          [(s["scene"], s["sub"], s["id"]) for s in got] == [(17, "a", "17a")],
          f"got={got}")

    # --- V14+ scheme: the letter is a scene PART, remapped to a kit ordinal ----
    got = derive(["Video_14_Shot_01a_1", "Video_14_Shot_01b_1", "Video_14_Shot_02a_1"])
    check("V14 scheme: parts remap to sequential ordinals",
          [s["scene"] for s in got] == [1, 2, 3], f"got={[s['scene'] for s in got]}")

    # `id` must reproduce the NAME, not a composition of its parts. This is the
    # card_overlay lookup key — `0203` misses a shot actually named `02_3`.
    got = derive(["Video_14_Shot_01_3", "Video_14_Shot_02a_3"])
    check("id reproduces the raw name verbatim",
          [s["id"] for s in got] == ["01_3", "02a_3"], f"got={[s['id'] for s in got]}")

    # `sub` must sort correctly past 9 shots in one part — plain string sort puts
    # "a10" before "a2" without the zero-pad.
    got = derive([f"Video_14_Shot_01a_{i}" for i in (1, 2, 9, 10, 11)])
    subs = [s["sub"] for s in got]
    # (`part` is folded into the ordinal under this scheme, so `sub` is the
    # zero-padded index alone — what matters is that it string-sorts correctly.)
    check("sub zero-padded so _10 sorts after _9", subs == sorted(subs) ==
          ["01", "02", "09", "10", "11"], f"got={subs}")

    # --- the silent-wrong-output cases ----------------------------------------
    # Contiguous on purpose: an earlier cut used 01a/02a/04a_2, which also tripped
    # the scene-gap guard — so the fixture passed with the mixed-scheme check
    # deleted. Mutation testing caught it. Keep this the ONLY defect present.
    check("MIXED schemes die (not silently renumber)",
          dies(["Video_14_Shot_01a", "Video_14_Shot_02a", "Video_14_Shot_03a_2"]))

    check("missing scene PART dies (01b absent)",
          dies(["Video_14_Shot_01a_1", "Video_14_Shot_01c_1", "Video_14_Shot_02a_1"]))

    died, msg = dies_msg(["Video_14_Shot_01a_1", "Video_14_Shot_03a_1"])
    check("missing scene NUMBER dies (02 absent)", died)
    # Pin the MESSAGE's scene list, not just the die: it is what sends someone to
    # the right place, and a fatal message naming the wrong scenes is the same
    # defect class as the misordered-vs-missing-images mixup below.
    check("...and names the actual missing scene", "[2]" in msg, f"msg={msg!r}")

    # FRONT-truncation, not just a middle gap: kit ordinals start at 1, so a
    # manifest whose first scene is 4 is "contiguous" yet puts every shot 3 scenes
    # early. An earlier cut anchored the range at nums[0] and let this through.
    died, msg = dies_msg(["Video_14_Shot_04a_1", "Video_14_Shot_05a_1",
                          "Video_14_Shot_06a_1"])
    check("front-truncated run dies (starts at scene 4)", died)
    # The message must name scenes 1-3 as the missing ones. This is what pins the
    # range ANCHOR: with the range starting at 2 the list silently becomes [2,3],
    # and with it starting at nums[0] the guard does not fire at all.
    check("...and names scenes 1-3 as missing", "[1, 2, 3]" in msg, f"msg={msg!r}")
    # ...and states the run it checked against, so the reader can tell WHICH
    # range was scanned rather than inferring it.
    check("...and names the 1..6 run it checked", "1..6" in msg, f"msg={msg!r}")

    # OUT-OF-ORDER: the contiguity checks above both sort, so they are order-blind,
    # while ordinal assignment is order-DEPENDENT. Moving one part's entries to the
    # end passes every completeness check and still remaps almost everything —
    # on the real video_14_hd.json, moving Shot_04b_* to the end remaps 68 of 76
    # shots with no error and no kit-mismatch warning. Without this fixture, a
    # mutation replacing document order with sorted order leaves the suite green.
    check("out-of-order entries die (01b after 03a)",
          dies(["Video_14_Shot_01a_1", "Video_14_Shot_02a_1",
                "Video_14_Shot_03a_1", "Video_14_Shot_01b_1"]))

    # --- PORTED from the stale root-level test_derive_shots.py (2026-07-29) -----
    # That 88-line Jul-9 copy sat beside this 548-line one, both git-tracked and
    # both running in the gate under the same display name once suite discovery
    # was fixed. It is being deleted, but two of its assertions had ZERO coverage
    # here, so they move rather than vanish: the `no_char` flag and prompt
    # whitespace collapsing. Both are real production behaviour
    # (build_video.py:137 and :417 derive no_char from /\bno character\b/i).
    def _derive_with_prompts(entries, vid):
        _n[0] += 1
        q = TMP / f"ds_port_{_n[0]:03d}_hd.json"
        q.write_text(json.dumps({"images": entries}))
        return bv.derive_shots_from_hd_manifest(q, vid)

    _ported = _derive_with_prompts([
        {"name": "Video_99_Shot_01a", "prompt": "Three,  charcoal  suit."},
        {"name": "Video_99_Shot_01b", "prompt": "No character. A card."},
        {"name": "Video_99_Shot_02a", "prompt": "Three walking."},
        {"name": "Video_99_Thumbnail_A", "prompt": "thumb art"},   # must be excluded
        {"name": "Video_99_Thumbnail_B", "prompt": "thumb art"},
    ], "Video_99")
    check("ported: thumbnails excluded, shot ids in order",
          [x["id"] for x in _ported] == ["01a", "01b", "02a"],
          f"got={[x['id'] for x in _ported]}")
    check("ported: scene/sub parsed off the name",
          _ported[0]["scene"] == 1 and _ported[0]["sub"] == "a", str(_ported[0]))
    check("ported: prompt whitespace is collapsed",
          _ported[0]["prompt"] == "Three, charcoal suit.",
          f"got={_ported[0]['prompt']!r}")
    check("ported: no_char set from 'No character', and only there",
          _ported[1]["no_char"] is True and _ported[0]["no_char"] is False,
          f"01a={_ported[0]['no_char']} 01b={_ported[1]['no_char']}")

    # The final sort is the ONLY thing ordering shots within a scene — no guard
    # checks index order, so an appended re-render (Shot_01a_3 written before
    # Shot_01a_1) relies entirely on it. Deleting the sort survived mutation
    # testing because every other fixture was already in sorted order.
    got = derive(["Video_14_Shot_01a_3", "Video_14_Shot_01a_1", "Video_14_Shot_01a_2"])
    check("out-of-order shot INDEX is sorted, not rejected",
          [s["id"] for s in got] == ["01a_1", "01a_2", "01a_3"],
          f"got={[s['id'] for s in got]}")

    # Unpadded scene names must NOT false-die: the ordinal key is zero-padded
    # precisely so "10a" sorts after "1a" (a plain string sort puts "10a" first),
    # and a false die on the ascending-order guard would block a real build.
    try:
        got = derive([f"Video_14_Shot_{n}a_1" for n in range(1, 11)])
        check("unpadded scene names (1a..10a) do not false-die",
              [s["scene"] for s in got] == list(range(1, 11)),
              f"got={[s['scene'] for s in got]}")
    except SystemExit:
        check("unpadded scene names (1a..10a) do not false-die", False, "died")

    # Contiguous input must NOT die — a guard that blocks the live path is worse
    # than no guard, because the next person reaches for an override.
    try:
        derive(["Video_14_Shot_01a_1", "Video_14_Shot_01b_1", "Video_14_Shot_02a_1"])
        check("contiguous input does not die", True)
    except SystemExit:
        check("contiguous input does not die", False)

    # An unparseable shot-shaped name is DROPPED; that must be announced, not
    # silent. (`Video_13_Shot_01b2` has been dropped from every V13 assemble.)
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        got = derive(["Video_13_Shot_01a", "Video_13_Shot_01b2"])
    check("unparseable shot name warns by name",
          len(got) == 1 and "01b2" in err.getvalue(), f"err={err.getvalue()!r}")

    # Thumbnails are not shots and must not warn.
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        got = derive(["Video_14_Shot_01_1", "Video_14_Thumbnail_A"])
    check("thumbnail entries ignored silently",
          len(got) == 1 and err.getvalue() == "", f"err={err.getvalue()!r}")

    # --- _resolve_image north-star fallback ------------------------------------
    # The north-star asset is named by SCRIPT scene, but the caller passes the KIT
    # ORDINAL under the lettered scheme (5a -> 7). Indexing the filename by that
    # ordinal silently serves scene 7a's frame for shot 05a_1. Latent today only
    # because `Raw_Assets/Video_14/` doesn't exist — populate it and it ships.
    d = pathlib.Path(tempfile.mkdtemp(prefix="ns_"))
    (d / "Raw_Assets" / "Video_14").mkdir(parents=True)
    (d / "Raw_Assets" / "Video_14" / "Video_14_Scene_05.png").write_bytes(b"x")
    (d / "Raw_Assets" / "Video_14" / "Video_14_Scene_07.png").write_bytes(b"y")
    path_, _note = bv._resolve_image(d, "Raw_Assets/Video_14_HD", "Video_14",
                                     7, "05a_1", True)   # kit ordinal 7, script 5a
    check("north-star resolves by SCRIPT scene from sid, not the kit ordinal",
          path_.endswith("Video_14_Scene_05.png"), f"got={path_}")

    # The ABSENT case: with no north-star on disk it must fall through to `primary`
    # and report no note. Without this, forcing the is_file() check True survives —
    # i.e. nothing would notice the resolver returning a path that doesn't exist
    # while claiming in its note that a fallback happened.
    path2, note2 = bv._resolve_image(d, "Raw_Assets/Video_14_HD", "Video_14",
                                     9, "06a_1", True)   # no Scene_06.png written
    check("absent north-star falls through to primary, no note",
          path2.endswith("Video_14_Shot_06a_1.png") and note2 is None,
          f"got={path2!r} note={note2!r}")

    # --- verify_derived_ordinals: the TAIL-truncation hole ---------------------
    # The structural guards cannot catch a trimmed LAST part: a scene whose script
    # parts are a,b,c appearing as only a,b looks exactly like a scene that really
    # has two parts. Drop 04c and every later ordinal slides down one, silently.
    # Only the kit's own src-SCENE record closes this.
    kdir = pathlib.Path(tempfile.mkdtemp(prefix="kit_"))
    kfile = kdir / "_VO_Session_B_Kit.md"
    kfile.write_text(
        # Scenes 1-3 kept their numbers, so they carry NO annotation — the real
        # shape. A DOTALL regex would let scene 1 borrow scene 4's `src SCENE`.
        "## Scene 1 → `x.mp3` (COLD OPEN)\n"
        "## Scene 2 → `x.mp3` (PROMISE)\n"
        "## Scene 3 → `x.mp3` (TEASE)\n"
        "## Scene 4 → `x.mp3` (src SCENE 4a; A)\n"
        "## Scene 5 → `x.mp3` (src SCENE 4b; B)\n"
        "## Scene 6 → `x.mp3` (src SCENE 4c; C)\n"
        "## Scene 7 → `x.mp3` (src SCENE 5; D)\n", encoding="utf-8")

    full = derive(["Video_14_Shot_01_1", "Video_14_Shot_02_1", "Video_14_Shot_03_1",
                   "Video_14_Shot_04a_1", "Video_14_Shot_04b_1", "Video_14_Shot_04c_1",
                   "Video_14_Shot_05_1"])
    try:
        bv.verify_derived_ordinals(full, kfile, "video_14_hd.json")
        check("complete manifest passes the kit cross-check", True)
    except SystemExit:
        check("complete manifest passes the kit cross-check", False, "false die")

    # Same manifest with 04c trimmed away — passes every structural guard.
    trimmed = derive(["Video_14_Shot_01_1", "Video_14_Shot_02_1", "Video_14_Shot_03_1",
                      "Video_14_Shot_04a_1", "Video_14_Shot_04b_1", "Video_14_Shot_05_1"])
    check("tail-part truncation slips past the structural guards",
          [s["scene"] for s in trimmed] == [1, 2, 3, 4, 5, 6],
          f"got={[s['scene'] for s in trimmed]}")
    try:
        bv.verify_derived_ordinals(trimmed, kfile, "video_14_hd.json")
        check("tail-part truncation dies on the kit cross-check", False, "did NOT die")
    except SystemExit:
        check("tail-part truncation dies on the kit cross-check", True)

    # A kit scene with NO images is a DIFFERENT defect from a misordered manifest:
    # the timeline is built from shots, so that scene is dropped from the cut and
    # its narration never plays. It must die — and must NOT be described as
    # "misordered, shots would render over the wrong VO", which is false here.
    short = derive(["Video_14_Shot_01_1", "Video_14_Shot_02_1", "Video_14_Shot_03_1",
                    "Video_14_Shot_04a_1", "Video_14_Shot_04b_1", "Video_14_Shot_04c_1"])
    err = io.StringIO()
    try:
        # die() prints to stderr and raises SystemExit(1), so the message is on
        # the stream — str(e) is just the exit code.
        with contextlib.redirect_stderr(err):
            bv.verify_derived_ordinals(short, kfile, "video_14_hd.json")
        check("kit scene with no images dies", False, "did NOT die")
    except SystemExit:
        check("kit scene with no images dies", True)
        msg = err.getvalue()
        check("...and says images are MISSING, not misordered",
              "NO images" in msg and "wrong VO" not in msg, f"msg={msg!r}")

    # DISAGREEMENT with every kit ordinal present — the other die branch. Splitting
    # a scene the script does NOT split (03a/03b here) keeps the ordinal count up,
    # so nothing is `missing`; the mapping is just shifted. Without this fixture the
    # disagree branch is unreachable, because the tail-truncation case above now
    # dies at the `missing` check first — mutation testing caught exactly that.
    split = derive(["Video_14_Shot_01_1", "Video_14_Shot_02_1",
                    "Video_14_Shot_03a_1", "Video_14_Shot_03b_1",
                    "Video_14_Shot_04a_1", "Video_14_Shot_04b_1", "Video_14_Shot_04c_1"])
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            bv.verify_derived_ordinals(split, kfile, "video_14_hd.json")
        check("shifted-but-complete mapping dies", False, "did NOT die")
    except SystemExit:
        check("shifted-but-complete mapping dies", True)
        check("...and says MISORDERED, not missing images",
              "wrong VO" in err.getvalue() and "NO images" not in err.getvalue(),
              f"msg={err.getvalue()!r}")

    # Absent kit FILE -> warn and continue; the structural guards still stand.
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        bv.verify_derived_ordinals(full, kdir / "absent.md", "video_14_hd.json")
    check("absent kit warns, does not block", "absent" in err.getvalue(),
          f"err={err.getvalue()!r}")

    # But a kit that EXISTS with zero annotations while the manifest carries scene
    # PARTS is a CONTRADICTION, not a missing oracle: build_vo_kit annotates
    # whenever src != scene number, and a lettered src can never equal a digit
    # string — so any script with a lettered scene yields at least one annotation.
    # Parts in the manifest + none in the kit means the manifest splits scenes the
    # script does not have, which is exactly what shifts ordinals silently.
    bare = kdir / "bare.md"
    bare.write_text("## Scene 1 → `x.mp3` (A)\n## Scene 2 → `x.mp3` (B)\n", encoding="utf-8")
    try:
        bv.verify_derived_ordinals(full, bare, "video_14_hd.json")
        check("parts in manifest + no kit annotations dies", False, "did NOT die")
    except SystemExit:
        check("parts in manifest + no kit annotations dies", True)

    # `_PART_ID_RE` is the ONLY thing keeping every V09-V13 manifest out of this
    # verify path (has_parts gate at the top of verify_derived_ordinals). It
    # requires the trailing `_` precisely to tell a V14 scene PART (`04a_1`) apart
    # from a V09-V13 shot LETTER (`01a`) — drop that `_` and `01a` matches too, so
    # a real V13 build (whose kit never renumbers anything, so it correctly
    # carries NO src-SCENE annotation) hits the "parts present but kit annotates
    # nothing" contradiction branch and DIES. 22 of 138 real corpus manifests flip
    # to SystemExit(1) under that one-character regression.
    v13 = derive(["Video_13_Shot_01a", "Video_13_Shot_01b", "Video_13_Shot_02a"])
    k13dir = pathlib.Path(tempfile.mkdtemp(prefix="kit13_"))
    k13 = k13dir / "_VO_Session_B_Kit.md"
    # The real V13 shape: no scene was ever renumbered, so no `src SCENE` note
    # anywhere — that is correct, not a missing oracle.
    k13.write_text("## Scene 1 → `x.mp3` (COLD OPEN)\n"
                    "## Scene 2 → `x.mp3` (PROMISE)\n", encoding="utf-8")
    err = io.StringIO()
    died13 = False
    try:
        with contextlib.redirect_stderr(err):
            bv.verify_derived_ordinals(v13, k13, "video_13_hd.json")
    except SystemExit:
        died13 = True
    check("V13-scheme ids (01a/01b) do not trip the has_parts gate",
          not died13 and err.getvalue() == "", f"died={died13} err={err.getvalue()!r}")
    shutil.rmtree(k13dir, ignore_errors=True)

    # A letterless manifest needs no oracle at all — ordinal == scene number by
    # identity, nothing is inferred. It must be SILENT, not print a scary "cannot
    # verify" line about a risk that structurally cannot exist (V13 every build).
    flat = derive(["Video_14_Shot_01_1", "Video_14_Shot_02_1"])
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        bv.verify_derived_ordinals(flat, kdir / "absent.md", "video_14_hd.json")
    check("letterless manifest verifies silently", err.getvalue() == "",
          f"err={err.getvalue()!r}")
    shutil.rmtree(kdir, ignore_errors=True)

    # --- mutation-crash guard at the CLI entry --------------------------------
    # scripts/mutate.py rewrites this file in place; a SIGKILL leaves the mutant on
    # disk. The hourly pipeline-sweep invokes build_video.py, so the very next sweep
    # would render a real video from mutated code. The guard must refuse loudly.
    #
    # ISOLATED TREE (round 16, 2026-07-27): a prior cut wrote directly to the REAL
    # `.mutate.lock` / `build_video.py.mutate.orig` in the repo root — the exact
    # `if exists(): ... write()` TOCTOU shape mutate.py's own module docstring
    # forbids on those primitives (a real mutate.py run racing this fixture gets
    # its lock clobbered then unlinked, and its pristine sidecar deleted out from
    # under it — the corruption the lock exists to prevent). Copy only the guard's
    # dependency closure — the target file plus scripts/mutate.py, same relative
    # layout so each file's own __file__-derived paths line up the same way they
    # do in the real repo — into a private tmp_root instead. Nothing below ever
    # opens the real LOCK or the real sidecar, so N parallel instances of this
    # suite, or a real concurrent mutation run, cannot collide with it or with
    # each other: every instance owns its own tmp_root outright.
    guard_root = pathlib.Path(tempfile.mkdtemp(prefix="guard_tree_"))
    try:
        (guard_root / "scripts").mkdir()
        shutil.copy(REPO / "scripts" / "mutate.py", guard_root / "scripts" / "mutate.py")
        guard_target = guard_root / "build_video.py"
        shutil.copy(bv.__file__, guard_target)
        guard_lock = guard_root / ".mutate.lock"
        guard_sidecar = guard_target.with_suffix(guard_target.suffix + ".mutate.orig")
        # COPY the real source into the sidecar, don't write "x" — a byte-identical
        # sidecar keeps the printed `cp <sidecar> <target>` recovery advice a
        # no-op instead of destructive, matching what mutate.py itself writes.
        guard_sidecar.write_text(guard_target.read_text(encoding="utf-8"))

        # Explicitly WITHOUT SK_MUTATION_RUN — that env var is how the harness tells
        # the guard to stand down, and this check is about the production path (the
        # hourly sweep), which never sets it.
        env = {k: v for k, v in os.environ.items() if k != "SK_MUTATION_RUN"}
        # Point at a TEMP vault. Normally the guard fires before parse_args() and
        # nothing happens — but under the mutant that disables the guard, the full
        # plan path runs for real and rewrote live Video_14_orchestrated manifests
        # in the vault. Content-safe only by an unwritten invariant; one refactor
        # from not being.
        vault_td = tempfile.mkdtemp(prefix="guard_vault_")
        env["SK_VAULT"] = vault_td
        r = subprocess.run([sys.executable, str(guard_target), "Video_14"],
                           capture_output=True, text=True, env=env)
        shutil.rmtree(vault_td, ignore_errors=True)
        check("build refuses while a .mutate.orig sits beside it",
              r.returncode != 0 and "mutate.orig" in (r.stdout + r.stderr),
              f"rc={r.returncode}")
        # Round-12 mutation survivor (line 1667's if-test->True: forcing the
        # ACTIVE branch unconditionally): with NO lock at all this must still
        # say died-mid-flight, never ACTIVE. guard_lock is a private tmp path
        # nothing else can be holding, so — unlike the old fixture sharing the
        # real .mutate.lock with mutate.py's own outer pass — this is now
        # unconditionally true, no "only when nested outside a live run" caveat.
        check("...and does NOT say ACTIVE (no lock exists at all here)",
              "mutation-run-active" not in (r.stdout + r.stderr))

        # MEDIUM-1 (round 12): same sidecar, but a LIVE lock held by a running
        # pid — the ~100s window an 80-mutant gate run genuinely leaves open,
        # not a crash. The guard must say ACTIVE and must NOT print the cp/rm
        # crash-recovery advice, which would be actively wrong (and dangerous
        # if followed) mid-run. Writing our own pid is safe here: guard_lock is
        # a tmp file this fixture owns outright, not the shared production lock.
        guard_lock.write_text(f"{os.getpid()} selftest\n")
        vault_td2 = tempfile.mkdtemp(prefix="guard_vault_")
        env["SK_VAULT"] = vault_td2
        r2 = subprocess.run([sys.executable, str(guard_target), "Video_14"],
                            capture_output=True, text=True, env=env)
        shutil.rmtree(vault_td2, ignore_errors=True)
        out2 = r2.stdout + r2.stderr
        check("build refuses with ACTIVE wording under a live lock",
              r2.returncode != 0 and "mutation-run-active" in out2
              and "ACTIVE" in out2, f"rc={r2.returncode} out={out2[-300:]}")
        check("...and does NOT print the crash cp/rm recovery",
              "died mid-flight" not in out2, out2[-300:])

        # Round-12 mutation survivor (line 1657's int+1: '0' -> '1'): the
        # liveness probe must stay `os.kill(int(holder), 0)` — signal 0 is a
        # harmless no-op check; ANY other signal number is a real signal
        # delivered to whatever pid happens to be in the lock file (which can
        # be a real production process — the hourly sweep, or this very
        # mutation harness). That side effect is invisible to every assertion
        # above (both correct code and this mutant print identical ACTIVE
        # text — the only difference is what actually gets signaled), so it
        # cannot be caught behaviorally without deliberately signaling a real
        # process, which is unsafe to do from a test. Pin the literal instead.
        # Read from the REAL build_video.py (bv.__file__), not the tmp copy —
        # both are byte-identical at this point, but the real file is what a
        # reviewer expects this assertion to be about.
        guard_src = pathlib.Path(bv.__file__).read_text(encoding="utf-8")
        check("guard's liveness probe uses signal 0 (never a real signal)",
              "os.kill(int(holder), 0)" in guard_src)
        # Generator-safety companion (2026-07-27, post-fix): the check above pins
        # that THIS FILE's literal is the safe `0` — it says nothing about whether
        # mutate.py's generator would still be willing to mutate it if someone
        # changed that literal later. Call the PRODUCTION mutants() on the real
        # file and prove it excludes os.kill's signal arg here, not just in
        # test_mutate.py's synthetic fixture. Both checks are needed: this one
        # guards the generator, the one above guards the source.
        # Derive the line number from the source itself, never a hardcoded literal
        # — build_video.py is under active development and a hardcoded 1657 goes
        # vacuous the moment any line above the guard shifts (round-14 finding:
        # confirmed vacuous at shift>=2 by re-running this exact expression
        # against the file shifted by N leading lines).
        kill_ln = next(i for i, l in enumerate(guard_src.splitlines(), 1)
                       if "os.kill(int(holder)" in l)
        guard_labels = [lbl for lbl, ln, _new in mut.mutants(guard_src) if ln == kill_ln]
        check(f"mutate.py's generator emits NO mutant on the kill-call line ({kill_ln})",
              not guard_labels, f"labels={guard_labels}")

        # Round-12 mutation survivor (line 1667's flip-bool: 'and' -> 'or'):
        # `holder or alive` is indistinguishable from `holder and alive` in
        # BOTH scenarios exercised above — no-lock has both falsy, live-lock
        # has both truthy. Telling them apart needs a THIRD scenario (a real
        # lock file whose pid is confirmed dead: holder truthy, alive False),
        # which can only be fabricated safely with a lock this fixture owns —
        # true unconditionally now that guard_lock is private. Pin the literal
        # `and` instead — it also backstops the if-True/if-False mutants above
        # for the case where THEY are the ones lock-contended.
        check("guard's ACTIVE branch requires BOTH holder AND alive (not `or`)",
              "if holder and alive:" in guard_src)
    finally:
        shutil.rmtree(guard_root, ignore_errors=True)

    # --- EXTERNAL ORACLE: the real V14 manifest vs the real V14 VO kit --------
    # The kit annotates each ordinal with the script scene it came from
    # (`## Scene 7 -> ... (src SCENE 5a; ...)`). That mapping is authored
    # independently of this function, so agreeing with it is a genuine check and
    # not a restatement of the code. Skipped (not failed) if the vault is absent.
    man = VAULT / "Raw_Assets/Image_Factory/manifests/video_14_hd.json"
    kit = VAULT / "Voice_Files/Video_14/_VO_Session_B_Kit.md"
    kit = kit if kit.is_file() else None
    if man.is_file() and kit:
        shots = bv.derive_shots_from_hd_manifest(man, "Video_14")
        # Use the PRODUCTION regex, not a copy: an earlier cut reimplemented it
        # here, so adding re.DOTALL to build_video's version made the live V14
        # build false-die (scene 1 borrows scene 4's annotation) while this suite
        # stayed green — the one fixture that looks like it pins it, didn't.
        want = {int(o): s.lower() for o, s in
                bv._KIT_SRC_RE.findall(kit.read_text(encoding="utf-8"))}
        derived = {}
        for s in shots:
            part = re.match(r"(\d+)([a-z]?)", s["id"])
            derived.setdefault(s["scene"], f"{int(part.group(1))}{part.group(2)}")
        # The kit annotates `src SCENE` only where it RENUMBERED (scenes 1-3 kept
        # their numbers and carry no annotation), so compare on its own keys — and
        # require the annotated set to be substantial, or an empty/near-empty `want`
        # would make this pass vacuously.
        overlap = {k: derived.get(k) for k in want}
        check("V14 ordinals match the VO kit's own src-SCENE map",
              len(want) >= 20 and overlap == want,
              f"\n    derived={dict(sorted(overlap.items()))}\n    kit    ={dict(sorted(want.items()))}")
    else:
        print(f"  [skip] V14 vault oracle (manifest={man.is_file()} kit={bool(kit)})")

    shutil.rmtree(TMP, ignore_errors=True)
    shutil.rmtree(d, ignore_errors=True)

    if FAILS:
        print(f"\ntest_derive_shots: FAIL ({len(FAILS)}): {', '.join(FAILS)}")
        return 1
    print("\ntest_derive_shots: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
