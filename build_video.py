#!/usr/bin/env python3
"""Video orchestrator (Build 2) — one command per video.

Glue over three already-built engines:
  - image_factory/generate_images.py   (shot prompts -> scene PNGs)
  - vo_factory/generate_vo.py          (VO kit -> scene mp3s)
  - video_factory/assemble.py          (PNGs + mp3s + manifest -> mp4 + srt)

It collapses the two manual manifest hand-offs into one run: from a video's
**shot list** + **VO kit** it auto-authors the image manifest and the edit
manifest, then (on demand) fires each engine. The only real new logic is the
two manifest-authoring functions; everything else is orchestration.

Resolution from a video id (e.g. `Video_01` or `01`), under the 3SK Finance
vault (override with $SK_VAULT):
  shot list   : Scene_Image_Prompts/Video_NN_Shot_List.md
  VO kit      : Voice_Files/Video_NN/_VO_Session_B_Kit.md
  images out  : Raw_Assets/Video_NN_gen/
  VO out      : Voice_Files/Video_NN_gen/
  draft video : Footage_and_Edits/Video_NN_v2.mp4 (+ .srt)

Usage:
  python3 build_video.py Video_01                 # PLAN: author both manifests + cost, no spend
  python3 build_video.py Video_01 --assemble      # author + render (free) from existing assets
  python3 build_video.py Video_01 --vo            # author + generate VO (billed)
  python3 build_video.py Video_01 --images        # author + generate images (billed)
  python3 build_video.py Video_01 --run           # all three stages, each skip-if-exists
  python3 build_video.py Video_01 --vo-source Voice_Files/Video_01  # assemble off an existing VO set
  python3 build_video.py Video_01 --assemble --image-set Raw_Assets/Video_01_HD --vo-source Voice_Files/Video_01

Billed stages (images, VO) NEVER run without their explicit flag (or --run);
the default is a free dry plan. Each stage is independently runnable and
resumable (skip-if-output-exists). Same inputs -> same manifests -> same video.

Image-set names follow the shot list: each shot's frame is `<vid>_Shot_<id>.png`
in the chosen image dir (`--image-set`, default `Raw_Assets/<vid>_gen`). Only when
an explicit `--image-set` is given (a curated set where gaps are intentional) does
a missing shot frame fall back — with a logged ⚠ note — to the locked north-star
scene frame `Raw_Assets/<vid>/<vid>_Scene_NN.png` (e.g. the HD set reuses the north
star for shot 01a). In the default `_gen` flow there is NO fallback: a missing
frame hard-fails at assembly so a failed render can't silently become stills.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
DEFAULT_VAULT = "~/Documents/3SK/outputs/BRANDS/3SK_Finance"
# The locked "Three" character header (style preamble + reference sheet +
# gpt-image defaults) is brand-level and shared by every 3SK Finance video.
CANONICAL_IMG_HEADER = REPO / "image_factory" / "manifests" / "video_01_images.example.json"


def die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(1)


def vault() -> Path:
    return Path(os.path.expanduser(os.environ.get("SK_VAULT", DEFAULT_VAULT))).resolve()


def normalize_id(raw: str) -> tuple[str, str]:
    """'Video_01' | '01' | '1' -> ('Video_01', '01')."""
    m = re.search(r"(\d+)", raw)
    if not m:
        die(f"could not parse a video number from '{raw}'")
    nn = f"{int(m.group(1)):02d}"
    return f"Video_{nn}", nn


# --- shot-list parsing -----------------------------------------------------

_SCENE_RE = re.compile(r"^##\s+Scene\s+(\d+)\b.*$", re.MULTILINE)
# Tolerate descriptive lines between the "### Shot Na" header and its prompt
# fence (non-greedy to the first fence) so a stray note never silently drops a
# shot. The header-count cross-check in parse_shot_list catches any real miss.
_SHOT_RE = re.compile(r"^###\s+Shot\s+(\d+)([a-z])\b[^\n]*\n(?:[^\n]*\n)*?```[^\n]*\n(.*?)\n```", re.MULTILINE | re.DOTALL)
_SHOT_HEADER_RE = re.compile(r"^###\s+Shot\s+\d+[a-z]\b", re.MULTILINE)
# Cadence table rows: "| S1 | 11.4 | 1 | single |"
_CADENCE_RE = re.compile(r"^\|\s*S(\d+)\s*\|\s*([\d.]+)\s*\|", re.MULTILINE)


def parse_shot_list(path: Path) -> tuple[list[dict], dict[int, float]]:
    """Return (shots, cadence_seconds_by_scene).

    shots = [{scene:int, sub:str, id:str, prompt:str, no_char:bool}] in order.
    """
    text = path.read_text(encoding="utf-8")
    cadence = {int(s): float(sec) for s, sec in _CADENCE_RE.findall(text)}
    shots: list[dict] = []
    for m in _SHOT_RE.finditer(text):
        scene = int(m.group(1))
        sub = m.group(2)
        prompt = re.sub(r"\s+", " ", m.group(3)).strip()
        shots.append({
            "scene": scene,
            "sub": sub,
            "id": f"{scene:02d}{sub}",
            "prompt": prompt,
            "no_char": bool(re.search(r"\bno character\b", prompt, re.I)),
        })
    if not shots:
        die(f"no '### Shot Na' blocks found in {path.name}")
    n_headers = len(_SHOT_HEADER_RE.findall(text))
    if n_headers != len(shots):
        print(f"  ⚠ {n_headers} '### Shot' headers but only {len(shots)} parsed a "
              f"prompt fence in {path.name} — {n_headers - len(shots)} shot(s) "
              f"will be MISSING from the manifests (check for a malformed code fence).")
    return shots, cadence


# --- VO kit parsing (captions) --------------------------------------------

_KIT_BLOCK_RE = re.compile(r"^##\s+Scene\s+(\d+)\s*(?:->|→)\s*`([^`]+\.mp3)`[^\n]*\n", re.MULTILINE)


def parse_kit(path: Path) -> dict[int, dict]:
    """scene_number -> {caption, filename} from the VO kit (break tags stripped)."""
    if not path.is_file():
        return {}
    body = path.read_text(encoding="utf-8")
    heads = list(_KIT_BLOCK_RE.finditer(body))
    out: dict[int, dict] = {}
    for i, m in enumerate(heads):
        scene = int(m.group(1))
        filename = m.group(2).strip()
        start = m.end()
        end = heads[i + 1].start() if i + 1 < len(heads) else len(body)
        chunk = re.split(r"^---\s*$", body[start:end], maxsplit=1, flags=re.MULTILINE)[0]
        chunk = re.sub(r"<break[^>]*/>", " ", chunk)            # drop SSML
        chunk = re.sub(r"\*\*([^*]+)\*\*", r"\1", chunk)        # bold
        chunk = re.sub(r"(?<!\w)_([^_]+)_(?!\w)", r"\1", chunk)  # italic
        chunk = re.sub(r"\s+", " ", chunk).strip()
        out[scene] = {"caption": chunk, "filename": filename}
    return out


def split_scene(caption: str, n: int) -> list[tuple[str, float]]:
    """Split a caption into n contiguous groups, each with its audio time-weight.

    Caption text and audio cut-point are derived from the SAME sentence grouping
    so the on-screen caption matches the words heard under each shot. The weight
    is the group's share of total caption characters (weights sum to 1.0). Falls
    back to an even split when the caption can't be aligned — empty, or fewer
    sentences than shots — so audio still covers every shot.
    """
    if n <= 1:
        return [(caption, 1.0)]
    sentences = [s.strip() for s in re.findall(r".*?[.!?](?:\s+|$)", caption) if s.strip()] if caption else []
    if len(sentences) < n:
        # Can't give every shot its own sentence: keep audio even, front-load text.
        parts = [sentences[i] if i < len(sentences) else "" for i in range(n)]
        return [(p, 1.0 / n) for p in parts]
    per = len(sentences) / n
    groups: list[tuple[str, float]] = []
    for i in range(n):
        lo = round(i * per)
        hi = round((i + 1) * per)
        text = " ".join(sentences[lo:hi]).strip()
        groups.append((text, float(max(len(text), 1))))
    total = sum(w for _, w in groups)
    return [(t, w / total) for t, w in groups]


# --- real-speech alignment (optional) --------------------------------------

# Only require faster-whisper when --align is actually used; a normal run must
# not need it. Resolved lazily and cached on the module.
_ALIGN_MOD = None


def _align_module():
    global _ALIGN_MOD
    if _ALIGN_MOD is None:
        sys.path.insert(0, str(REPO / "vo_factory"))
        import align_vo  # noqa: E402  (intentional lazy import)
        _ALIGN_MOD = align_vo
    return _ALIGN_MOD


# Below this fraction of words anchored to real audio we don't trust the
# alignment for this clip and fall back to the character-proportion estimate.
_MIN_MATCH_RATE = 0.5


def aligned_internal_cuts(caption: str, cap_parts: list[tuple[str, float]],
                          dur: float, vo_path: Path) -> list[float] | None:
    """Real-speech cut times for the k-1 internal shot boundaries of a scene.

    Maps each shot boundary (a cumulative word count into the caption) to the
    time that word is actually spoken, via local forced alignment. Returns the
    k-1 boundary times, or None to signal "fall back to the proportional split"
    — when the clip can't be aligned, the alignment is low-confidence, a boundary
    word index is out of range, or the resulting cuts aren't strictly increasing
    inside (0, dur). The caller keeps its existing character-proportion math on
    None, so alignment can only ever improve timing, never break assembly.
    """
    k = len(cap_parts)
    if k <= 1 or dur is None or dur <= 0 or not vo_path.is_file():
        return None
    am = _align_module()
    try:
        result = am.load_or_align(vo_path, caption)
    except SystemExit:
        return None  # align_vo.die() on a bad clip must not abort the build
    words = result.get("words") or []
    if not words or result.get("match_rate", 0.0) < _MIN_MATCH_RATE:
        return None
    cuts: list[float] = []
    wc = 0
    for i in range(k - 1):  # k-1 internal boundaries; tail runs to clip end
        wc += len(am.tokenize(cap_parts[i][0]))
        if not (0 < wc < len(words)):
            return None
        cuts.append(float(words[wc]["start"]))
    # Strictly increasing and strictly inside (0, dur), else don't trust it.
    bounds = [0.0, *cuts, dur]
    if any(bounds[j] >= bounds[j + 1] for j in range(len(bounds) - 1)):
        return None
    return cuts


# --- duration probing ------------------------------------------------------

def ffprobe_seconds(mp3: Path) -> float | None:
    if not mp3.is_file():
        return None
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nokey=1:noprint_wrappers=1", str(mp3)],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        return float(out)
    except (subprocess.CalledProcessError, ValueError):
        return None


# --- manifest authoring ----------------------------------------------------

def author_image_manifest(shots: list[dict], vid: str, images_out: Path) -> dict:
    if not CANONICAL_IMG_HEADER.is_file():
        die(f"canonical image header not found: {CANONICAL_IMG_HEADER}")
    header = json.loads(CANONICAL_IMG_HEADER.read_text(encoding="utf-8"))
    images = []
    for s in shots:
        entry = {"name": f"{vid}_Shot_{s['id']}", "prompt": s["prompt"]}
        if s["no_char"]:
            entry["use_references"] = False
        images.append(entry)
    return {
        "project": vid,
        "output_dir": str(images_out),
        "reference_dir": header.get("reference_dir"),
        "reference_images": header.get("reference_images", []),
        "style_preamble": header.get("style_preamble", ""),
        "defaults": header.get("defaults", {}),
        "images": images,
    }


_MOTIONS = ["zoom_in", "pan_right", "zoom_out", "pan_left", "pan_up", "pan_down"]


# --- shared reusable CTA segments ------------------------------------------
# The subscribe/like/comment beat is identical in every video, so it is built
# ONCE as fixed assets and dropped straight into the edit manifest. It never
# enters the image manifest (no gpt-image-2 spend) or the VO kit (no TTS spend),
# so we never pay to regenerate it — only a one-time local ffmpeg render per
# video. Assets live under <vault>/Shared_Assets/CTA/ (vault-relative, so they
# resolve under the manifest asset_dir exactly like any per-shot image/clip).
# A missing asset is skipped with a warning, so a CTA that hasn't been generated
# yet degrades to the pre-CTA output instead of breaking a build.
CTA_DIR = "Shared_Assets/CTA"
# The mid-roll bump is inserted right after this scene. The Universal Intro ends
# ~scene 2 (cold-open hook + the "by the end of this video" promise), so the bump
# lands just past the hook while retention is still high. The outro CTA is always
# appended last.
CTA_MIDROLL_AFTER_SCENE = 2
CTA_SEGMENTS = {
    "midroll": {
        "image": f"{CTA_DIR}/CTA_midroll.png",
        "vo_clip": f"{CTA_DIR}/CTA_midroll.mp3",
        "motion": "zoom_in",
        "caption_text": ("Quick thing before we keep going — if this is landing, "
                         "subscribe. It's how the channel reaches the next person "
                         "who needs it. Okay, back to it."),
    },
    "outro": {
        "image": f"{CTA_DIR}/CTA_outro.png",
        "vo_clip": f"{CTA_DIR}/CTA_outro.mp3",
        "motion": "zoom_in",
        "caption_text": ("And before you go: like this video, leave a comment, and "
                         "subscribe so the next one finds you."),
    },
}


def _cta_shot(asset_dir: Path, key: str, still: bool = False) -> dict | None:
    """Build the edit-manifest shot for a shared CTA segment, or return None
    (with a warning) when either fixed asset is missing — so a CTA that hasn't
    been generated yet is silently omitted rather than hard-failing the
    assembler. Caption text feeds only the soft SRT; the on-screen
    subscribe/like/comment buttons are burned into the PNG itself."""
    seg = CTA_SEGMENTS[key]
    img = asset_dir / seg["image"]
    vo = asset_dir / seg["vo_clip"]
    missing = [p.name for p in (img, vo) if not p.is_file()]
    if missing:
        print(f"  ⚠ CTA {key} skipped — missing shared asset(s): "
              f"{', '.join(missing)} (generate them once under {CTA_DIR}/).")
        return None
    return {
        "image": seg["image"],
        "vo_clip": seg["vo_clip"],
        # Honor --still on the CTA beats too, so a "kill all motion drift" render
        # has no Ken Burns anywhere (the scene shots already use "hold").
        "motion": "hold" if still else seg["motion"],
        "caption_text": seg["caption_text"],
    }


def _resolve_image(asset_dir: Path, images_rel: str, vid: str,
                   scene: int, sid: str, allow_fallback: bool) -> tuple[str, str | None]:
    """Pick the vault-relative image path for a shot.

    Prefer the chosen image set's `<vid>_Shot_<id>.png`. Only when assembling
    from an explicit curated `--image-set` (`allow_fallback=True`) — where gaps
    are intentional, e.g. the HD set reuses the north star for shot 01a — fall
    back to the locked north-star scene frame `Raw_Assets/<vid>/<vid>_Scene_NN.png`.
    In the default `_gen` flow fallback is OFF: a missing frame keeps the primary
    path so the assembler hard-fails loudly (a failed render must not silently
    become repeated stills). Returns (rel_path, fallback_note); note is None
    unless a fallback was used.
    """
    primary = f"{images_rel}/{vid}_Shot_{sid}.png"
    if (asset_dir / primary).is_file():
        return primary, None
    if allow_fallback:
        north_star = f"Raw_Assets/{vid}/{vid}_Scene_{scene:02d}.png"
        if (asset_dir / north_star).is_file():
            return north_star, f"{vid}_Shot_{sid} absent from {images_rel} -> north-star {vid}_Scene_{scene:02d}.png"
    return primary, None


def author_edit_manifest(shots: list[dict], cadence: dict[int, float], kit: dict[int, dict],
                         vid: str, asset_dir: Path, images_rel: str, vo_rel: str,
                         output_dir: Path, output_name: str,
                         allow_image_fallback: bool = False,
                         align: bool = False,
                         fit: str | None = None,
                         still: bool = False,
                         include_cta: bool = True) -> tuple[dict, bool, list[str], list[int], list[str]]:
    """Author the video_factory edit manifest.

    `allow_image_fallback` (set only for an explicit `--image-set`) lets a shot
    missing from `images_rel` resolve to the locked north-star scene frame;
    otherwise a missing frame is left as-is for the assembler to hard-fail on.

    `align` (set by --align): for multi-shot scenes, derive each shot's audio
    window from where its words are ACTUALLY spoken (local forced alignment)
    instead of the caption character-proportion estimate. Any scene that can't be
    aligned cleanly silently keeps the proportional split, so --align only ever
    sharpens timing. `fit` overrides the manifest's default fit ("contain" when
    unset); pass "cover" to keep V1's original crop-to-fill look.

    Returns (manifest, all_vo_present, image_fallbacks, undetermined_scenes,
    align_notes). `undetermined_scenes` lists multi-shot scenes whose per-shot
    timing could NOT be computed (no VO mp3 AND no cadence entry) — assembling
    those would replay the scene's audio once per shot (M1), so the caller must
    refuse to assemble. `align_notes` are human-readable per-scene alignment
    outcomes (empty unless `align`).
    """
    by_scene: dict[int, list[dict]] = {}
    for s in shots:
        by_scene.setdefault(s["scene"], []).append(s)

    out_shots: list[dict] = []
    all_vo = True
    fallbacks: list[str] = []
    undetermined: list[int] = []
    align_notes: list[str] = []
    mi = 0  # global motion index, for visual variety across the whole video
    for scene in sorted(by_scene):
        scene_shots = by_scene[scene]
        k = len(scene_shots)
        # Use the filename the kit declares (what vo_factory actually writes);
        # fall back to the standard pattern when the kit is absent.
        vo_name = kit.get(scene, {}).get("filename") or f"{vid}_VO_Scene_{scene:02d}.mp3"
        vo_path = asset_dir / vo_rel / vo_name
        dur = ffprobe_seconds(vo_path)
        if dur is None:
            all_vo = False
            dur = cadence.get(scene)  # fallback: shot-list cadence table
        caption = kit.get(scene, {}).get("caption", "")
        cap_parts = split_scene(caption, k)
        # Real-speech cut times (k-1 internal boundaries) when --align is on and
        # the scene aligns cleanly; None means keep the proportional estimate.
        align_cuts = None
        if align and dur is not None and k > 1:
            align_cuts = aligned_internal_cuts(caption, cap_parts, dur, vo_path)
            align_notes.append(
                f"scene {scene}: real-speech cut timing ({k} shots)" if align_cuts is not None
                else f"scene {scene}: alignment unavailable/low-confidence — kept proportional timing")
        cum = 0.0  # cumulative fraction of the clip consumed by prior shots
        for i, s in enumerate(scene_shots):
            text, weight = cap_parts[i]
            image_rel, fb = _resolve_image(asset_dir, images_rel, vid, scene, s["id"], allow_image_fallback)
            if fb:
                fallbacks.append(fb)
            shot: dict = {
                "image": image_rel,
                "vo_clip": f"{vo_rel}/{vo_name}",
                # `still` locks every shot to a static frame (no Ken Burns) when
                # the caller wants zero motion drift; otherwise cycle for variety.
                "motion": "hold" if still else _MOTIONS[mi % len(_MOTIONS)],
                "caption_text": text,
            }
            mi += 1
            if dur is not None and k > 1:
                if align_cuts is not None:
                    # Real word-boundary times: [0, cut1, …, cut(k-1), clip end].
                    bounds = [0.0, *align_cuts, dur]
                    shot["start"] = round(bounds[i], 3)
                    if i < k - 1:                       # tail omits end -> clip end
                        shot["end"] = round(bounds[i + 1], 3)
                else:
                    shot["start"] = round(cum * dur, 3)
                    cum += weight
                    if i < k - 1:                       # omit end on the tail shot so
                        shot["end"] = round(cum * dur, 3)  # it runs to clip end
            out_shots.append(shot)
        if dur is None and k > 1:
            undetermined.append(scene)

    # Inject the shared reusable CTA segments (built once, never regenerated): a
    # mid-roll subscribe bump just after the Universal Intro, then the full
    # like/comment/subscribe beat at the very end. Both reference fixed assets, so
    # they add nothing to the billed image/VO stages. Insert the mid-roll first so
    # its index (counted from the pre-CTA scene shots) is unaffected by the outro
    # append.
    if include_cta:
        midroll = _cta_shot(asset_dir, "midroll", still=still)
        if midroll is not None:
            # Index of the first shot belonging to a scene past the threshold ==
            # count of shots in scenes <= the threshold.
            idx = sum(len(by_scene[s]) for s in by_scene
                      if s <= CTA_MIDROLL_AFTER_SCENE)
            if 0 < idx < len(out_shots):
                out_shots.insert(idx, midroll)
            else:
                print(f"  ⚠ CTA midroll skipped — no scene boundary after scene "
                      f"{CTA_MIDROLL_AFTER_SCENE} to place it (video too short?).")
        outro = _cta_shot(asset_dir, "outro", still=still)
        if outro is not None:
            out_shots.append(outro)

    manifest = {
        "video": f"{vid}_v2 (orchestrated)",
        "asset_dir": str(asset_dir),
        "output_dir": str(output_dir),
        "output_name": output_name,
        # zoom 1.02 = a gentle ~2% Ken Burns drift. 1.04 cropped too far into the
        # top/bottom of the contain-fitted stills (Steve, 2026-06-17); halved it.
        "defaults": {"zoom": 1.02, "fit": fit or "contain"},
        "shots": out_shots,
    }
    return manifest, all_vo, fallbacks, undetermined, align_notes


# --- engine invocation -----------------------------------------------------

def run(cmd: list[str], *, label: str) -> int:
    print(f"\n>>> {label}\n    $ {' '.join(cmd)}")
    return subprocess.run(cmd).returncode


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


# --- CLI -------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="3SK video orchestrator (Build 2).")
    p.add_argument("video", help="Video id, e.g. Video_01 or 01.")
    p.add_argument("--images", action="store_true", help="Run image_factory (BILLED).")
    p.add_argument("--vo", action="store_true", help="Run vo_factory (BILLED).")
    p.add_argument("--assemble", action="store_true", help="Run video_factory (free).")
    p.add_argument("--run", action="store_true", help="All three stages (images+vo+assemble).")
    p.add_argument("--vo-source", help="Vault-relative dir of existing VO mp3s to assemble from (e.g. Voice_Files/Video_01).")
    p.add_argument("--image-set", help="Vault-relative dir of existing image PNGs to assemble from (e.g. Raw_Assets/Video_01_HD). Mutually exclusive with --images.")
    p.add_argument("--force", action="store_true", help="Pass --force to the billed stages (re-render existing).")
    p.add_argument("--align", action="store_true",
                   help="Cut multi-shot scenes at REAL spoken-word boundaries via local "
                        "forced alignment (fixes within-scene A/V drift). Local, free, no API.")
    p.add_argument("--fit", choices=["cover", "contain"], default=None,
                   help="Override the edit-manifest default fit. 'cover' = crop-to-fill "
                        "(V1's original look); 'contain' = blurred-fill, no crop (default).")
    p.add_argument("--still", action="store_true",
                   help="Lock every shot to a static frame (motion 'hold') — no Ken "
                        "Burns pan/zoom. Use to kill on-screen motion drift.")
    p.add_argument("--no-cta", action="store_true",
                   help="Omit the shared reusable CTA segments (mid-roll subscribe "
                        "bump + outro like/comment/subscribe). On by default; they "
                        "reuse fixed assets and are never re-billed.")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    do_images = args.images or args.run
    do_vo = args.vo or args.run
    do_assemble = args.assemble or args.run
    plan_only = not (do_images or do_vo or do_assemble)

    if do_vo and args.vo_source:
        die("--vo writes to <video>_gen but --vo-source points assembly elsewhere; "
            "pick one (generate fresh, OR assemble from an existing set).")
    if do_images and args.image_set:
        die("--images writes to <video>_gen but --image-set points assembly at an existing set; "
            "pick one (generate fresh, OR assemble from an existing set).")

    vid, nn = normalize_id(args.video)
    vlt = vault()
    shot_list = vlt / "Scene_Image_Prompts" / f"{vid}_Shot_List.md"
    vo_kit = vlt / "Voice_Files" / vid / "_VO_Session_B_Kit.md"
    if not shot_list.is_file():
        die(f"shot list not found: {shot_list}")

    def _vault_rel(flag: str, raw: str) -> str:
        """Validate a user dir is vault-relative and stays inside the vault."""
        if Path(raw).is_absolute():
            die(f"{flag} must be vault-relative, not an absolute path.")
        rel = raw.strip("/")
        if not (vlt / rel).resolve().is_relative_to(vlt):
            die(f"{flag} escapes the vault ({raw!r}); must stay under {vlt}.")
        return rel

    images_rel = _vault_rel("--image-set", args.image_set) if args.image_set else f"Raw_Assets/{vid}_gen"
    vo_rel = _vault_rel("--vo-source", args.vo_source) if args.vo_source else f"Voice_Files/{vid}_gen"
    # Generation ALWAYS targets <vid>_gen — a curated `--image-set` is an input,
    # never an output, so a stale image manifest can never overwrite it.
    images_out = vlt / f"Raw_Assets/{vid}_gen"
    vo_out = vlt / f"Voice_Files/{vid}_gen"

    shots, cadence = parse_shot_list(shot_list)
    kit = parse_kit(vo_kit)

    # Warn if the kit and shot list disagree on which scenes exist — a mismatch
    # means a scene will silently fall back to the standard mp3 name / cadence
    # timing / empty caption, which is almost always an authoring error.
    shot_scenes = {s["scene"] for s in shots}
    kit_scenes = set(kit)
    if kit and shot_scenes != kit_scenes:
        only_shots = sorted(shot_scenes - kit_scenes)
        only_kit = sorted(kit_scenes - shot_scenes)
        warn = []
        if only_shots:
            warn.append(f"in shot list but not kit: {only_shots}")
        if only_kit:
            warn.append(f"in kit but not shot list: {only_kit}")
        print(f"  ⚠ scene mismatch — {'; '.join(warn)}")

    print(f"video      : {vid}")
    print(f"vault      : {vlt}")
    print(f"shot list  : {len(shots)} shots across {len(shot_scenes)} scenes")
    print(f"captions   : {len(kit)} scenes from {vo_kit.name if kit else '(kit missing)'}")
    print(f"images out : {images_out}")
    print(f"VO source  : {vlt / vo_rel}")

    # --- always author both manifests (free, deterministic) ---
    img_manifest = author_image_manifest(shots, vid, images_out)
    img_manifest_path = REPO / "image_factory" / "manifests" / f"{vid}_orchestrated.json"
    write_json(img_manifest_path, img_manifest)

    # The initial authoring is for the plan/preview + an interim manifest write;
    # keep it alignment-free so a plan (or any non-assemble run) never triggers
    # local transcription. Real-speech timing is applied in the assemble
    # re-author below, right before the render.
    edit_manifest, all_vo, fallbacks, _undetermined, _align_notes = author_edit_manifest(
        shots, cadence, kit, vid, vlt, images_rel, vo_rel,
        vlt / "Footage_and_Edits", f"{vid}_v2",
        allow_image_fallback=bool(args.image_set),
        fit=args.fit, still=args.still, include_cta=not args.no_cta,
    )
    # Key the edit manifest by BOTH its image set and its VO source so distinct
    # asset combinations (e.g. `_gen`+`_gen` vs `Video_01_HD`+hand-VO) never
    # overwrite each other's manifest. A flagless plan then can't silently break
    # a working assembly config (same inputs + same sources -> same path).
    edit_manifest_path = REPO / "video_factory" / "manifests" / f"{vid}_orchestrated_{Path(images_rel).name}_{Path(vo_rel).name}.json"
    write_json(edit_manifest_path, edit_manifest)

    print(f"\nauthored   : {img_manifest_path.relative_to(REPO)}  ({len(img_manifest['images'])} images)")
    print(f"authored   : {edit_manifest_path.relative_to(REPO)}  ({len(edit_manifest['shots'])} shots)")
    for fb in fallbacks:
        print(f"  ⚠ image fallback — {fb}")
    if not all_vo:
        print("  note: some VO clips not found yet — shot timing used the shot-list "
              "cadence table; it is re-probed exactly once the VO mp3s exist.")

    img_gen = REPO / "image_factory" / "generate_images.py"
    vo_gen = REPO / "vo_factory" / "generate_vo.py"
    assembler = REPO / "video_factory" / "assemble.py"

    if plan_only:
        print("\n--- PLAN ONLY (no spend). To execute: ---")
        print(f"  images (billed) : python3 {img_gen} {img_manifest_path} --dry-run   # then drop --dry-run")
        print(f"  VO     (billed) : python3 {vo_gen} {vo_kit} --output {vo_out} --dry-run")
        print(f"  assemble (free) : python3 {assembler} {edit_manifest_path}")
        print("  or all at once  : build_video.py {0} --run".format(vid))
        # Free cost preview for the billed stages.
        run([sys.executable, str(img_gen), str(img_manifest_path), "--dry-run"], label="image cost preview")
        if kit:
            run([sys.executable, str(vo_gen), str(vo_kit), "--output", str(vo_out), "--dry-run"], label="VO cost preview")
        return

    rc = 0
    if do_images:
        cmd = [sys.executable, str(img_gen), str(img_manifest_path)]
        if args.force:
            cmd.append("--force")
        rc |= run(cmd, label="STAGE images (billed)")
    if do_vo:
        if not vo_kit.is_file():
            die(f"VO kit not found: {vo_kit}")
        cmd = [sys.executable, str(vo_gen), str(vo_kit), "--output", str(vo_out)]
        if args.force:
            cmd.append("--force")
        rc |= run(cmd, label="STAGE vo (billed)")
    if do_assemble:
        # Re-author the edit manifest now that VO clips should exist (exact
        # durations). With --align this is where local forced alignment runs, so
        # the cut times use real spoken-word boundaries instead of the estimate.
        edit_manifest, _, fallbacks, undetermined, align_notes = author_edit_manifest(
            shots, cadence, kit, vid, vlt, images_rel, vo_rel,
            vlt / "Footage_and_Edits", f"{vid}_v2",
            allow_image_fallback=bool(args.image_set),
            align=args.align, fit=args.fit, still=args.still,
            include_cta=not args.no_cta,
        )
        for fb in fallbacks:
            print(f"  ⚠ image fallback — {fb}")
        for note in align_notes:
            print(f"  ◆ {note}")
        if undetermined:
            # Refuse rather than emit a video that replays each scene's VO once per
            # shot. Happens when a multi-shot scene still has no VO mp3 and no
            # shot-list cadence entry to derive timing from (M1).
            die("cannot assemble — these multi-shot scenes have no determinable "
                f"timing (missing VO mp3 AND no cadence entry): {undetermined}. "
                "Generate the VO clips (or add cadence entries) first.")
        write_json(edit_manifest_path, edit_manifest)
        rc |= run([sys.executable, str(assembler), str(edit_manifest_path)], label="STAGE assemble (free)")

    if rc:
        raise SystemExit(1)
    print(f"\ndone. Draft video -> {vlt / 'Footage_and_Edits' / (vid + '_v2.mp4')}")


if __name__ == "__main__":
    main()
