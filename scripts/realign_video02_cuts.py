#!/usr/bin/env python3
"""One-off: re-time Video_02's picture cuts to the editorial @ "phrase" markers.

WHY: build_video.py's author_edit_manifest splits each scene's narration into
evenly-sized SENTENCE groups (split_scene) and cuts the picture there. That
ignores the editorial cut markers documented in Video_02_Shot_List.md
(`🎬 Edit: … Cuts: Xa "…" → Xb @ "spoken line" → …`), so picture cuts land at
mathematically-even points instead of the intended narrative beats. Proven on
Scene 11: the first cut landed at 7.82s ("Write it down") instead of 14.62s
("Skipping levels always fails"), showing 11c ~7s early.

WHAT: for each multi-shot scene (4–11), anchor each internal shot boundary to
the time its editorial phrase is actually spoken, via the existing per-scene
forced-alignment word timings (*.align.json), and patch the manifest's per-shot
start/end. Single-shot scenes and scenes 1–3/12 (no editorial Cuts markers) are
left untouched. Motion, image order, captions are NOT changed — only timing.

This is a surgical, isolated fix for the one shipped video. The durable fix is
to teach author_edit_manifest to honor these markers; tracked separately.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

MANIFEST = Path("/Volumes/AI_Workspace/iris_studio/video_factory/manifests/"
                "Video_02_orchestrated_Video_02_assembly_Video_02.json")
ALIGN_DIR = Path("/Users/steve/Documents/3SK/outputs/BRANDS/3SK_Finance/"
                 "Voice_Files/Video_02")

# Editorial boundary phrases per scene, in EDITORIAL shot order, one phrase per
# internal boundary (k-1 phrases for a k-shot scene). Transcribed verbatim from
# the *.align.json word stream so they are guaranteed to match what Whisper heard
# (anchors avoid raw numbers/symbols where the transcription is unreliable).
# Source: Video_02_Shot_List.md `🎬 Edit:` Cuts notes.
ANCHORS: dict[int, list[str]] = {
    4:  ["median american at", "market returns about", "kill the debt",
         "return on your money", "no market crash"],
    5:  ["400 surprise", "1,000 emergency fund",
         "car repair is an inconvenience", "automate 25"],
    6:  ["doubles your money", "stay at level 3 upgrade", "over thirty years"],
    7:  ["households climb while most plateau", "rental next door",
         "300,000 invested", "first income-producing"],
    8:  ["roth conversion", "hire a", "stop optimizing"],
    9:  ["160,000 a year", "single lawsuit", "climbers build walls",
         "trust so assets"],
    10: ["because the problem is interesting", "wakes up on a tuesday",
         "not the balcony", "turns outward"],
    11: ["skipping levels always fails", "strategy above your level"],
}

_NORM = re.compile(r"[^a-z0-9]+")


def norm(t: str) -> str:
    return _NORM.sub("", t.lower())


def toks(phrase: str) -> list[str]:
    return [n for n in (norm(x) for x in phrase.split()) if n]


def find(words_norm: list[str], anchor: list[str], start: int) -> int | None:
    n = len(anchor)
    for i in range(start, len(words_norm) - n + 1):
        if words_norm[i:i + n] == anchor:
            return i
    return None


def scene_cuts(scene: int) -> tuple[list[float], list[str]]:
    """Resolve a scene's k-1 internal cut times AND the re-sliced per-shot caption.

    Returns (cuts, captions). `captions` has one entry per shot: the scripted
    words whose aligned start falls inside that shot's [start, end) window, so the
    soft SRT the assembler builds from caption_text stays matched to both the VO
    and the new picture cuts (the align `words` are the scripted caption tokens
    with real spoken times, so this is the original caption re-split at the cuts).
    """
    align = json.loads((ALIGN_DIR / f"Video_02_VO_Scene_{scene:02d}.mp3.align.json")
                       .read_text())
    words = align["words"]
    wn = [norm(w["text"]) for w in words]
    dur = float(align["audio_duration"])
    cuts: list[float] = []
    idx = 1  # boundary can't sit at word 0
    for ph in ANCHORS[scene]:
        pos = find(wn, toks(ph), idx)
        if pos is None:
            sys.exit(f"scene {scene}: anchor not found in alignment: {ph!r}")
        cuts.append(round(float(words[pos]["start"]), 3))
        idx = pos + 1
    # Must be strictly increasing and strictly inside (0, dur).
    bounds = [0.0, *cuts, dur]
    if any(bounds[j] >= bounds[j + 1] for j in range(len(bounds) - 1)):
        sys.exit(f"scene {scene}: cuts not strictly increasing in (0,{dur}): {cuts}")
    # Re-slice the caption: each word goes to the shot whose window holds its
    # start time. Windows are [bounds[n], bounds[n+1]); the last runs to dur.
    captions: list[str] = []
    for n in range(len(bounds) - 1):
        lo, hi = bounds[n], bounds[n + 1]
        is_last = n == len(bounds) - 2
        chunk = [w["text"] for w in words
                 if lo <= float(w["start"]) < hi or (is_last and float(w["start"]) >= hi)]
        captions.append(re.sub(r"\s+", " ", " ".join(chunk)).strip())
    return cuts, captions


def main(apply: bool) -> None:
    manifest = json.loads(MANIFEST.read_text())
    shots = manifest["shots"]

    # Group manifest shot indices by scene via the vo_clip scene number.
    scene_re = re.compile(r"_VO_Scene_(\d+)\.mp3$")
    by_scene: dict[int, list[int]] = {}
    for i, s in enumerate(shots):
        m = scene_re.search(s.get("vo_clip", ""))
        if m:
            by_scene.setdefault(int(m.group(1)), []).append(i)

    for scene in sorted(ANCHORS):
        idxs = by_scene.get(scene, [])
        k = len(idxs)
        if k != len(ANCHORS[scene]) + 1:
            sys.exit(f"scene {scene}: manifest has {k} shots but "
                     f"{len(ANCHORS[scene])} anchors (expected {k-1})")
        cuts, captions = scene_cuts(scene)
        bounds = [0.0, *cuts]  # one start per shot; tail end omitted -> clip end
        print(f"scene {scene}: {[s['image'].split('/')[-1] for i in idxs for s in [shots[i]]]}")
        for n, i in enumerate(idxs):
            old_s, old_e = shots[i].get("start"), shots[i].get("end")
            shots[i]["start"] = round(bounds[n], 3)
            if n < k - 1:
                shots[i]["end"] = round(bounds[n + 1], 3)
            else:
                shots[i].pop("end", None)  # tail runs to clip end
            shots[i]["caption_text"] = captions[n]
            new_e = shots[i].get("end")
            print(f"    shot{n}: start {old_s}->{shots[i]['start']}  "
                  f"end {old_e}->{new_e}")
            print(f"           cap: {captions[n][:90]}")

    if apply:
        bak = MANIFEST.with_suffix(".json.bak-pre-realign")
        if not bak.exists():
            bak.write_text(json.dumps(json.loads(MANIFEST.read_text()), indent=2))
        MANIFEST.write_text(json.dumps(manifest, indent=2))
        print(f"\nPATCHED {MANIFEST.name} (backup: {bak.name})")
    else:
        print("\nDRY RUN — pass --apply to write the manifest")


if __name__ == "__main__":
    main(apply="--apply" in sys.argv)
