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
import math
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


def _vo_generated_blocks(vo_kit: Path) -> list[dict]:
    """The exact [{scene, filename, text}] generate_vo would write for this kit.

    Loaded from generate_vo's own parser so the post-stage artifact check can never
    drift from what the generator actually produces (it skips empty-narration blocks
    and keeps break-only ones; a second local parser would diverge on both). Only
    called after a clean VO run, so its parse can't newly fail here."""
    import importlib.util
    path = REPO / "vo_factory" / "generate_vo.py"
    spec = importlib.util.spec_from_file_location("generate_vo", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.parse_kit(vo_kit)


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


# --- editorial cut-anchor directive (optional) -----------------------------
# A shot list may carry an explicit, machine-read cut-anchor line per multi-shot
# scene so picture cuts land on the editorially intended narrative beats instead
# of an even-sentence split. Format (anywhere in the file, one per scene):
#
#   > ✂️ Cut-anchors: 4 | "median American at" | "the market returns about" | …
#
# i.e. the scene number, then the k-1 boundary phrases (verbatim spoken
# substrings) for a k-shot scene, pipe-separated and double-quoted. Under
# --align the orchestrator maps each phrase to the moment it is actually spoken
# (forced alignment) and cuts there. The directive is OPTIONAL and SAFE: absent,
# unparseable, wrong count, or unresolved -> the scene keeps today's timing. The
# human-facing `@ "spoken line"` hints in the 🎬 Edit: prose remain the source of
# truth a person reads; this line is the same intent in a form a parser can trust
# (the prose markers are incomplete — final-shot cuts are often unmarked and
# tile-pans carry multiple @ per shot — so they can't be parsed reliably).
_CUT_ANCHOR_RE = re.compile(
    r"Cut-anchors:\s*(\d+)\s*((?:\|\s*\"[^\"]+\"\s*)+)", re.IGNORECASE)
_ANCHOR_PHRASE_RE = re.compile(r"\"([^\"]+)\"")


def parse_cut_anchors(text: str) -> dict[int, list[str]]:
    """scene_number -> [boundary phrases] from any Cut-anchors directive lines."""
    # Normalize smart/curly double quotes so an editor's autocorrected “…” (the
    # default in Obsidian/Docs) still parses instead of silently disabling the
    # directive — the phrases are matched on straight ASCII quotes.
    text = text.replace("“", '"').replace("”", '"')
    out: dict[int, list[str]] = {}
    for m in _CUT_ANCHOR_RE.finditer(text):
        scene = int(m.group(1))
        phrases = [p.strip() for p in _ANCHOR_PHRASE_RE.findall(m.group(2)) if p.strip()]
        if phrases:
            out[scene] = phrases
    # A line that says "Cut-anchors:" but whose scene didn't parse is a typo'd
    # directive (wrong quoting, missing scene number). Surface it instead of
    # silently falling back, so an editor doesn't believe a broken line took
    # effect. Gate on whether the scene actually landed in `out` (not a per-line
    # regex re-match) so a directive that legitimately wraps across lines — which
    # the full-text finditer above still parses — does not false-warn.
    for ln in text.splitlines():
        if "cut-anchors:" not in ln.lower():
            continue
        sm = re.search(r"cut-anchors:\s*(\d+)", ln, re.IGNORECASE)
        if sm and int(sm.group(1)) in out:
            continue  # this scene's directive parsed (possibly across lines)
        clipped = ln.strip()
        clipped = clipped[:90] + "…" if len(clipped) > 90 else clipped
        print(f'  ⚠ Cut-anchors line did not parse (need: N | "phrase" | …): {clipped}')
    return out


def lint_cut_anchors(shots: list[dict], edit_anchors: dict[int, list[str]]) -> list[str]:
    """Build-time diagnostics for the optional phrase-anchored cut directive.

    A multi-shot scene with NO `✂️ Cut-anchors:` directive silently falls back to
    even-sentence-split picture timing — the exact misalignment that defect
    existed to prevent (Video_02, 2026-06-18). Because shot lists are hand-authored
    and nothing emits the directive automatically, a new video's missing anchors
    were invisible until someone eyeballed the assembled cut. This surfaces them at
    build time so they are loud, not silent.

    Pure warning, never fatal: the even-split fallback is a valid, intentional
    default, so a missing/miscounted directive must not block a build. Returns a
    list of human-readable warning strings (no `⚠` prefix — the caller adds it,
    matching the other build warnings).
    """
    shots_per_scene: dict[int, int] = {}
    for s in shots:
        shots_per_scene[s["scene"]] = shots_per_scene.get(s["scene"], 0) + 1
    warns: list[str] = []
    for scene in sorted(shots_per_scene):
        k = shots_per_scene[scene]
        if k < 2:
            continue  # single-shot scene: no internal cut, no anchors needed
        phrases = edit_anchors.get(scene)
        if not phrases:
            warns.append(
                f"scene {scene} has {k} shots but no Cut-anchors directive — picture "
                f"cuts use even-sentence-split timing, not the editorial beats. Add: "
                f'> ✂️ Cut-anchors: {scene} | "phrase" | … ({k - 1} verbatim spoken phrases)')
        elif len(phrases) != k - 1:
            warns.append(
                f"scene {scene} Cut-anchors has {len(phrases)} phrase(s) but {k} shots "
                f"need {k - 1} — the directive will be rejected and the scene falls back "
                f"to even-split timing (fix the phrase count)")
    return warns


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


# Normalization for matching an editorial anchor phrase against the aligned word
# stream: strip everything but [a-z0-9] so "$6,400" / "8%" in a directive match
# the scripted word tokens "$6,400." / "8%" the aligner emits. (Mirrors the
# proven one-off scripts/realign_video02_cuts.py logic.)
_ANCHOR_NORM = re.compile(r"[^a-z0-9]+")


def _anorm(t: str) -> str:
    return _ANCHOR_NORM.sub("", t.lower())


def _atoks(phrase: str) -> list[str]:
    return [n for n in (_anorm(x) for x in phrase.split()) if n]


def _find_subseq(words_norm: list[str], anchor: list[str], start: int) -> int | None:
    """First index >= start where `anchor` matches as a contiguous run, else None."""
    n = len(anchor)
    if n == 0:
        return None
    for i in range(start, len(words_norm) - n + 1):
        if words_norm[i:i + n] == anchor:
            return i
    return None


def anchored_internal_cuts(caption: str, anchors: list[str], dur: float,
                           vo_path: Path) -> tuple[list[float], list[str]] | None:
    """Cut times + re-sliced captions from explicit editorial phrase anchors.

    For a k-shot scene with k-1 anchor phrases: map each phrase to the time it is
    actually spoken (forced-alignment word stream) and return the k-1 internal
    boundary times PLUS the caption re-sliced at those boundaries (so the soft SRT
    the assembler builds from caption_text stays matched to the new picture cuts).
    Returns None — the caller falls back to the even-split / proportional timing —
    on ANY failure: no VO, low-confidence alignment, an anchor that doesn't
    resolve, anchors out of order, or non-increasing cuts. So the directive can
    only ever sharpen timing, never break assembly.
    """
    if not anchors or dur is None or dur <= 0 or not vo_path.is_file():
        return None
    am = _align_module()
    try:
        result = am.load_or_align(vo_path, caption)
    except SystemExit:
        return None  # align_vo.die() on a bad clip must not abort the build
    words = result.get("words") or []
    if not words or result.get("match_rate", 0.0) < _MIN_MATCH_RATE:
        return None
    words_norm = [_anorm(w.get("text", "")) for w in words]
    cuts: list[float] = []
    idx = 1  # a boundary can't sit at word 0
    for ph in anchors:
        pos = _find_subseq(words_norm, _atoks(ph), idx)
        if pos is None:
            return None
        cuts.append(round(float(words[pos]["start"]), 3))
        idx = pos + 1
    bounds = [0.0, *cuts, dur]
    if any(bounds[j] >= bounds[j + 1] for j in range(len(bounds) - 1)):
        return None
    # Re-slice the caption: each scripted word joins the shot whose [lo, hi) window
    # holds its spoken start (the last window runs to clip end).
    captions: list[str] = []
    for j in range(len(bounds) - 1):
        lo, hi = bounds[j], bounds[j + 1]
        is_last = j == len(bounds) - 2
        chunk = [w.get("text", "") for w in words
                 if lo <= float(w["start"]) < hi or (is_last and float(w["start"]) >= hi)]
        captions.append(re.sub(r"\s+", " ", " ".join(chunk)).strip())
    return cuts, captions


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
        # GENERIC-CHARACTER GUARD (2026-06-18): we deliberately do NOT strip the
        # locked "Three" reference sheets, even on shots tagged "no character".
        # Withholding the references is exactly what let gpt-image-2 hallucinate a
        # GENERIC stranger onto Video_02's end screen (Shot_13a): a no-ref
        # end-screen "card" prompt still rendered a person, and with no reference
        # in context that person was off-model. Keeping the sheets attached costs
        # ~$0.006/image but GUARANTEES that any human the model draws is
        # conditioned on Three — so a generic character can't be generated. The
        # PROMPT text (not ref-stripping) is what controls whether a character
        # appears at all, so `use_references` is left at its default (True) for
        # every shot; nothing sets it False here. (The `no_char` flag is retained
        # in the shot model for reporting but no longer changes reference wiring.)
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
# The mid-roll bump is inserted right after this scene. The locked Universal Intro
# runs scenes 1-3 (cold open → promise → tease, ending ~0:35), so injecting after
# scene 3 drops the bump at the body's start — clear of the 0:15–0:30 high-attrition
# window and without breaking the intro. The outro CTA is always appended last.
CTA_MIDROLL_AFTER_SCENE = 3
CTA_SEGMENTS = {
    "midroll": {
        "image": f"{CTA_DIR}/CTA_midroll.png",
        "vo_clip": f"{CTA_DIR}/CTA_midroll.mp3",
        "motion": "zoom_in",
        "caption_text": ("Quick thing before we keep going — if this is landing, "
                         "subscribe. It's the easiest way to make sure the next one "
                         "reaches you. Okay, back to it."),
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
                         include_cta: bool = True,
                         scene_pause: float = 0.5,
                         edit_anchors: dict[int, list[str]] | None = None) -> tuple[dict, bool, list[str], list[int], list[str]]:
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

    `edit_anchors` (from parse_cut_anchors): optional per-scene editorial cut
    phrases. When --align is on and a scene supplies exactly k-1 anchors that all
    resolve, picture cuts land on those narrative beats (and the caption is
    re-sliced to match) instead of the even-sentence boundaries — falling back to
    the even-split/proportional timing on any miss.

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
    # Where the mid-roll CTA goes, captured as a real out_shots index (after the
    # CTA_MIDROLL_AFTER_SCENE scene + its pause). Counting out_shots directly — not
    # by_scene shot counts — keeps the insertion point correct once inter-scene
    # pause shots are interleaved below.
    midroll_after_idx: int | None = None
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
        # Cut timing, in order of preference (only when --align is on and k > 1):
        #   1. explicit editorial Cut-anchors mapped to real spoken time
        #      (+ caption re-sliced at those cuts);
        #   2. even-sentence word boundaries mapped to real spoken time;
        #   3. the caption character-proportion estimate.
        # Each step falls back to the next on any failure, so timing only sharpens.
        anchor_cuts = None      # k-1 boundary times from editorial anchors
        anchor_caps = None      # per-shot caption re-slice that matches anchor_cuts
        align_cuts = None       # k-1 boundary times from even-split alignment
        if align and dur is not None and k > 1:
            a = (edit_anchors or {}).get(scene)
            if a and len(a) == k - 1:
                res = anchored_internal_cuts(caption, a, dur, vo_path)
                if res is not None:
                    anchor_cuts, anchor_caps = res
            if anchor_cuts is not None:
                align_notes.append(f"scene {scene}: editorial cut-anchors ({k} shots)")
            else:
                align_cuts = aligned_internal_cuts(caption, cap_parts, dur, vo_path)
                align_notes.append(
                    f"scene {scene}: real-speech cut timing ({k} shots)" if align_cuts is not None
                    else f"scene {scene}: alignment unavailable/low-confidence — kept proportional timing")
        cum = 0.0  # cumulative fraction of the clip consumed by prior shots
        for i, s in enumerate(scene_shots):
            text, weight = cap_parts[i]
            if anchor_caps is not None:           # caption re-sliced at the anchors
                text = anchor_caps[i]
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
                fixed_cuts = anchor_cuts if anchor_cuts is not None else align_cuts
                if fixed_cuts is not None:
                    # Word-boundary times: [0, cut1, …, cut(k-1), clip end]. From
                    # editorial anchors when present, else even-split alignment.
                    bounds = [0.0, *fixed_cuts, dur]
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
        # Inter-scene breathing room: hold the scene's last frame, silent, so the
        # next scene doesn't start the instant this one's VO ends. A silent shot
        # (image + duration, no vo_clip) rides the assembler's existing anullsrc
        # path — no shared render-math change, no cache bump.
        if scene_pause > 0 and out_shots:
            out_shots.append({
                "image": out_shots[-1]["image"],
                "duration": round(scene_pause, 3),
                "motion": "hold",
                "caption_text": "",
            })
        # Capture the mid-roll insertion point as a real out_shots index, after
        # this scene's pause, so interleaved pauses don't shift it.
        if scene == CTA_MIDROLL_AFTER_SCENE:
            midroll_after_idx = len(out_shots)

    # Inject the shared reusable CTA segments (built once, never regenerated): a
    # mid-roll subscribe bump just after the Universal Intro, then the full
    # like/comment/subscribe beat at the very end. Both reference fixed assets, so
    # they add nothing to the billed image/VO stages. Insert the mid-roll first so
    # its index (counted from the pre-CTA scene shots) is unaffected by the outro
    # append.
    if include_cta:
        midroll = _cta_shot(asset_dir, "midroll", still=still)
        if midroll is not None:
            idx = midroll_after_idx
            if idx is not None and 0 < idx < len(out_shots):
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
    p.add_argument("--align", action=argparse.BooleanOptionalAction, default=True,
                   help="Cut multi-shot scenes at REAL spoken-word boundaries via local "
                        "forced alignment (fixes within-scene A/V drift). Local, free, no API. "
                        "ON BY DEFAULT (Steve, 2026-06-20: all shots align to the voice). "
                        "Pass --no-align to fall back to even-split proportional timing.")
    p.add_argument("--fit", choices=["cover", "contain"], default=None,
                   help="Override the edit-manifest default fit. 'cover' = crop-to-fill "
                        "(V1's original look); 'contain' = blurred-fill, no crop (default).")
    p.add_argument("--still", action=argparse.BooleanOptionalAction, default=True,
                   help="Lock every shot to a static frame (motion 'hold') — no Ken "
                        "Burns pan/zoom. ON BY DEFAULT (Steve, 2026-06-22: 3SK videos "
                        "ship as stills — no on-screen motion drift on the flat 2D art). "
                        "Pass --no-still to re-enable the gentle Ken Burns pan/zoom cycle.")
    p.add_argument("--no-cta", action="store_true",
                   help="Omit the shared reusable CTA segments (mid-roll subscribe "
                        "bump + outro like/comment/subscribe). On by default; they "
                        "reuse fixed assets and are never re-billed. STANDARD: pass "
                        "--no-cta when re-assembling an ALREADY-PUBLISHED video, so a "
                        "re-render doesn't retroactively change its cut/length.")
    p.add_argument("--scene-pause", type=float, default=0.5,
                   help="Seconds of silent held-frame breathing room between scenes "
                        "(default 0.5). Pass 0 to butt scenes together with no gap.")
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

    if not math.isfinite(args.scene_pause) or not 0 <= args.scene_pause <= 10:
        die(f"--scene-pause must be a finite value in [0, 10] (0 to disable); got {args.scene_pause}.")

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
    edit_anchors = parse_cut_anchors(shot_list.read_text(encoding="utf-8"))
    for w in lint_cut_anchors(shots, edit_anchors):
        print(f"  ⚠ {w}")
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
        scene_pause=args.scene_pause, edit_anchors=edit_anchors,
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

    # Trust-but-verify: a generation subprocess that exits 0 without writing its
    # artifacts would mark a stage "done" with nothing on disk, and a scheduled
    # runner (run_claude_job.sh, watchdog.sh, pipeline_orchestrator) trusts that
    # exit code to advance the pipeline. After any clean stage we confirm the
    # expected files actually exist; a missing artifact forces a non-zero exit.
    def _missing(label: str, paths: list[Path]) -> list[str]:
        gone = [p for p in paths if not (p.is_file() and p.stat().st_size > 0)]
        if gone:
            print(f"error: {label} stage exited 0 but {len(gone)} expected "
                  f"artifact(s) are missing/empty: {[str(p) for p in gone]}",
                  file=sys.stderr)
        return gone

    rc = 0
    if do_images:
        cmd = [sys.executable, str(img_gen), str(img_manifest_path)]
        if args.force:
            cmd.append("--force")
        img_rc = run(cmd, label="STAGE images (billed)")
        rc |= img_rc
        if img_rc == 0:
            want = [images_out / f"{img['name']}.png" for img in img_manifest["images"]]
            if want and _missing("images", want):
                rc |= 1
    if do_vo:
        if not vo_kit.is_file():
            die(f"VO kit not found: {vo_kit}")
        cmd = [sys.executable, str(vo_gen), str(vo_kit), "--output", str(vo_out)]
        if args.force:
            cmd.append("--force")
        vo_rc = run(cmd, label="STAGE vo (billed)")
        rc |= vo_rc
        if vo_rc == 0:
            # Verify against generate_vo's OWN parser so the expected set is exactly
            # the clips it writes — it skips empty-narration blocks but keeps
            # break-only ones, a divergence build_video's caption parser would miss.
            want = [vo_out / b["filename"] for b in _vo_generated_blocks(vo_kit)]
            if want and _missing("vo", want):
                rc |= 1
    if do_assemble:
        # Re-author the edit manifest now that VO clips should exist (exact
        # durations). With --align this is where local forced alignment runs, so
        # the cut times use real spoken-word boundaries instead of the estimate.
        edit_manifest, _, fallbacks, undetermined, align_notes = author_edit_manifest(
            shots, cadence, kit, vid, vlt, images_rel, vo_rel,
            vlt / "Footage_and_Edits", f"{vid}_v2",
            allow_image_fallback=bool(args.image_set),
            align=args.align, fit=args.fit, still=args.still,
            include_cta=not args.no_cta, scene_pause=args.scene_pause,
            edit_anchors=edit_anchors,
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
        asm_rc = run([sys.executable, str(assembler), str(edit_manifest_path)], label="STAGE assemble (free)")
        rc |= asm_rc
        if asm_rc == 0:
            out_mp4 = vlt / "Footage_and_Edits" / f"{vid}_v2.mp4"
            if _missing("assemble", [out_mp4]):
                rc |= 1

    if rc:
        raise SystemExit(1)
    print(f"\ndone. Draft video -> {vlt / 'Footage_and_Edits' / (vid + '_v2.mp4')}")


if __name__ == "__main__":
    main()
