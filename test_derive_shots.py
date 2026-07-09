"""Self-check for derive_shots_from_hd_manifest (build_video.py).

Guards the A-43 fallback: when a video never produced a `<vid>_Shot_List.md`
(Video_09 did this), assemble derives its shots from the HD render manifest
instead of dying. This locks the derivation to the by-hand method: scene/sub from
each shot NAME, thumbnails excluded, prompt verbatim, ordered."""
import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parent


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _write(tmp, obj):
    import json
    p = tmp / "video_99_hd.json"
    p.write_text(json.dumps(obj), encoding="utf-8")
    return p


def test_derives_shots_and_excludes_thumbnails(tmp_path):
    bv = _load("build_video", "build_video.py")
    manifest = {"images": [
        {"name": "Video_99_Shot_01a", "prompt": "Three,  charcoal  suit."},
        {"name": "Video_99_Shot_01b", "prompt": "No character. A card."},
        {"name": "Video_99_Shot_02a", "prompt": "Three walking."},
        {"name": "Video_99_Thumbnail_A", "prompt": "thumb art"},   # excluded
        {"name": "Video_99_Thumbnail_B", "prompt": "thumb art"},   # excluded
    ]}
    shots = bv.derive_shots_from_hd_manifest(_write(tmp_path, manifest), "Video_99")
    assert [s["id"] for s in shots] == ["01a", "01b", "02a"], shots
    assert shots[0]["scene"] == 1 and shots[0]["sub"] == "a"
    assert shots[0]["prompt"] == "Three, charcoal suit."          # whitespace collapsed
    assert shots[1]["no_char"] is True and shots[0]["no_char"] is False


def test_out_of_order_names_are_sorted(tmp_path):
    bv = _load("build_video", "build_video.py")
    manifest = {"images": [
        {"name": "Video_99_Shot_02a", "prompt": "b"},
        {"name": "Video_99_Shot_01a", "prompt": "a"},
    ]}
    shots = bv.derive_shots_from_hd_manifest(_write(tmp_path, manifest), "Video_99")
    assert [s["id"] for s in shots] == ["01a", "02a"]


def test_no_shot_entries_dies(tmp_path):
    bv = _load("build_video", "build_video.py")
    manifest = {"images": [{"name": "Video_99_Thumbnail_A", "prompt": "x"}]}
    try:
        bv.derive_shots_from_hd_manifest(_write(tmp_path, manifest), "Video_99")
    except SystemExit:
        return  # die() raises SystemExit — the wanted loud failure
    raise AssertionError("expected die() on a manifest with no shot entries")


def test_matches_hand_derived_v9_if_present():
    """Belt-and-suspenders: on the live vault, the auto-derivation must reproduce
    the hand-derived Video_09 shot list exactly (same ids, same order). Skips off
    the Mac where the vault isn't mounted."""
    bv = _load("build_video", "build_video.py")
    try:
        vlt = bv.vault()
    except SystemExit:
        return  # no vault here — hermetic tests above still ran
    hd = vlt / "Raw_Assets/Image_Factory/manifests/video_09_hd.json"
    sl = vlt / "Scene_Image_Prompts/Video_09_Shot_List.md"
    if not (hd.is_file() and sl.is_file()):
        return
    derived = [s["id"] for s in bv.derive_shots_from_hd_manifest(hd, "Video_09")]
    parsed = [s["id"] for s in bv.parse_shot_list(sl)[0]]
    assert derived == parsed, (derived, parsed)


if __name__ == "__main__":
    import tempfile
    for fn in (test_derives_shots_and_excludes_thumbnails,
               test_out_of_order_names_are_sorted, test_no_shot_entries_dies):
        with tempfile.TemporaryDirectory() as d:
            fn(Path(d))
    test_matches_hand_derived_v9_if_present()
    print("all derive-shots self-checks passed")
