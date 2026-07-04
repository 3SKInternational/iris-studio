#!/usr/bin/env python3
"""
3SK Video Auto-Assembly Engine (E12).

Turns an edit manifest + an asset folder into a near-finished 1080p mp4:
per shot it applies a deterministic Ken Burns move to a still, lays the matching
VO on the audio spine (clips butt-to-butt), generates a soft SRT caption sidecar,
concatenates every shot, and exports a single H.264 mp4 + .srt.

Local, free, owned: ffmpeg only (no render-API fees). Deterministic and
re-runnable from the manifest. See schema.md for the manifest format.

Usage:
    python3 assemble.py manifests/video_01.json
    python3 assemble.py manifests/video_01.json --output /tmp/test.mp4
    python3 assemble.py manifests/proof_30s.json --keep-temp
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

# ---- Fixed render constants (the locked template) -------------------------
FPS = 30
OUT_W, OUT_H = 1920, 1080
# Ken Burns works on a high-res canvas so zoompan's whole-pixel crop stepping is
# sub-pixel relative to the 1080p output -> smooth, jitter-free motion. At 4x
# output (7680) each 1px crop step is ~0.25 output px, below perceptible judder
# even for very slow moves (the gentle ~2% Ken Burns default that exposed zoompan
# jitter at 2x canvas). Higher = smoother but slower; 4x is the sweet spot.
CANVAS_W, CANVAS_H = 7680, 4320
VIDEO_CODEC = ["-c:v", "libx264", "-preset", "medium", "-crf", "18",
               "-pix_fmt", "yuv420p"]
AUDIO_CODEC = ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]

# Bump this whenever the per-shot render math changes (compose_filter, zoompan,
# codecs, canvas/output dims, label burn-in). It is folded into every shot's
# cache key, so a bump invalidates ALL cached segments and forces a clean
# re-render — the safety valve that stops the cache serving stale pixels after a
# pipeline change. (The ffmpeg binary identity is ALSO folded into the key
# automatically, so an ffmpeg upgrade/keg-swap self-invalidates without a bump.)
CACHE_VERSION = 1


def _resolve_bin(name: str) -> str:
    # Prefer an explicit override, then Homebrew's keg-only ffmpeg-full (it
    # carries the libfreetype `drawtext` filter the lean `ffmpeg` formula drops
    # -> onscreen_label burn-in works), then whatever is on PATH.
    env = os.environ.get(name.upper())
    if env:
        return env
    keg = f"/opt/homebrew/opt/ffmpeg-full/bin/{name}"
    if os.path.exists(keg):
        return keg
    return name


FFMPEG = _resolve_bin("ffmpeg")
FFPROBE = _resolve_bin("ffprobe")
FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial.ttf",
]
VALID_MOTIONS = {"zoom_in", "zoom_out", "pan_left", "pan_right",
                 "pan_up", "pan_down", "hold"}
VALID_FITS = {"cover", "contain"}

# Caption chunking
CAP_MAX_CHARS = 42       # per line
CAP_MAX_LINES = 2        # per cue
CAP_MIN_CUE_S = 1.0      # never flash a cue shorter than this
CAP_MAX_WORDS = 9        # word-timed path: max words before a forced cue break
CAP_ALIGN_TRUST = 0.3    # >= this match_rate -> trust on-disk alignment for timing
CAP_ALIGN_REFRESH = 0.5  # under --align, below this (or stale) -> re-run alignment
SSML_BREAK_RE = re.compile(r"<break[^>]*/?>", re.IGNORECASE)
CAP_SENT_END_RE = re.compile(r"[.!?—]$")


# vo_factory lives one dir up (../vo_factory); import align_vo / generate_vo lazily
# so the normal free render never needs faster-whisper — only the --align refresh
# path (and even reading an existing align.json only needs align_vo, not whisper).
_VO_MODS: dict[str, object] = {}


def _vo_module(name: str):
    if name not in _VO_MODS:
        vf = str(Path(__file__).resolve().parent.parent / "vo_factory")
        if vf not in sys.path:
            sys.path.insert(0, vf)
        _VO_MODS[name] = __import__(name)
    return _VO_MODS[name]


def die(msg: str) -> None:
    sys.stderr.write(f"[assemble] ERROR: {msg}\n")
    sys.exit(1)


def run(cmd: list[str], desc: str) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write(f"[assemble] ffmpeg failed during {desc}:\n")
        sys.stderr.write("  cmd: " + " ".join(shlex.quote(c) for c in cmd) + "\n")
        sys.stderr.write(proc.stderr[-3000:] + "\n")
        sys.exit(1)


def ffprobe_duration(path: Path) -> float:
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True)
    if out.returncode != 0 or not out.stdout.strip():
        die(f"could not read duration of {path}")
    try:
        return float(out.stdout.strip())
    except ValueError:
        die(f"non-numeric duration for {path}: {out.stdout!r}")


def pick_font() -> str | None:
    for f in FONT_CANDIDATES:
        if os.path.exists(f):
            return f
    return None


def has_drawtext() -> bool:
    out = subprocess.run([FFMPEG, "-hide_banner", "-filters"],
                         capture_output=True, text=True)
    return bool(re.search(r"^\s*\S+\s+drawtext\b", out.stdout, re.MULTILINE))


# ---- Shot model -----------------------------------------------------------
@dataclass
class Shot:
    index: int
    image: Path
    audio: Path | None       # VO clip, or None for a silent shot
    audio_start: float       # input seek into the VO clip
    n_frames: int            # exact output frames -> seg duration = N/FPS
    motion: str
    zoom: float
    fit: str
    caption_text: str
    onscreen_label: str

    @property
    def seg_dur(self) -> float:
        return self.n_frames / FPS


def resolve_asset(asset_dir: Path, name: str, kind: str) -> Path:
    p = Path(name)
    cand = p if p.is_absolute() else asset_dir / name
    if not cand.exists():
        die(f"{kind} not found: {cand}")
    return cand


def build_shots(manifest: dict, asset_dir: Path) -> list[Shot]:
    defaults = manifest.get("defaults", {})
    def_zoom = float(defaults.get("zoom", 1.02))  # gentle Ken Burns; matches build_video default
    def_fit = defaults.get("fit", "cover")
    shots_in = manifest.get("shots")
    if not shots_in:
        die("manifest has no 'shots'")

    shots: list[Shot] = []
    for i, s in enumerate(shots_in, start=1):
        if "image" not in s:
            die(f"shot {i} missing 'image'")
        image = resolve_asset(asset_dir, s["image"], "image")

        audio: Path | None = None
        audio_start = 0.0
        # Duration resolution: explicit start/end on a vo_clip, full vo_clip,
        # or an explicit silent 'duration'.
        if s.get("vo_clip"):
            audio = resolve_asset(asset_dir, s["vo_clip"], "vo_clip")
            vo_dur = ffprobe_duration(audio)
            start = float(s.get("start", 0.0))
            end = float(s["end"]) if s.get("end") is not None else vo_dur
            if start < 0 or end <= start:
                die(f"shot {i}: bad start/end ({start}, {end})")
            if end > vo_dur + 0.05:
                die(f"shot {i}: end {end:.2f}s exceeds vo_clip length "
                    f"{vo_dur:.2f}s ({audio.name})")
            audio_start = start
            dur = end - start
        elif s.get("duration"):
            dur = float(s["duration"])
        else:
            die(f"shot {i}: needs 'vo_clip' or 'duration'")

        if dur <= 0:
            die(f"shot {i}: non-positive duration {dur}")
        n_frames = max(1, round(dur * FPS))

        motion = s.get("motion", "zoom_in")
        if motion not in VALID_MOTIONS:
            die(f"shot {i}: unknown motion '{motion}' "
                f"(valid: {sorted(VALID_MOTIONS)})")
        fit = s.get("fit", def_fit)
        if fit not in VALID_FITS:
            die(f"shot {i}: unknown fit '{fit}' (valid: {sorted(VALID_FITS)})")

        shots.append(Shot(
            index=i,
            image=image,
            audio=audio,
            audio_start=audio_start,
            n_frames=n_frames,
            motion=motion,
            zoom=float(s.get("zoom", def_zoom)),
            fit=fit,
            caption_text=(s.get("caption_text") or "").strip(),
            onscreen_label=(s.get("onscreen_label") or "").strip(),
        ))
    return shots


# ---- Filtergraph ----------------------------------------------------------
def compose_filter(shot: Shot, font: str | None, draw_ok: bool) -> str:
    """Build the per-shot video filtergraph producing [v] at 1920x1080."""
    n = shot.n_frames
    nm1 = max(1, n - 1)          # avoid div-by-zero on 1-frame shots
    z = shot.zoom

    # 1. Compose a CANVAS_W x CANVAS_H frame from the still.
    if shot.fit == "cover":
        compose = (
            f"scale={CANVAS_W}:{CANVAS_H}:force_original_aspect_ratio=increase,"
            f"crop={CANVAS_W}:{CANVAS_H}"
        )
    else:  # contain: blurred fill background + fitted foreground
        compose = (
            f"split=2[bg][fg];"
            f"[bg]scale={CANVAS_W}:{CANVAS_H}:force_original_aspect_ratio=increase,"
            f"crop={CANVAS_W}:{CANVAS_H},boxblur=24:2,eq=brightness=-0.12[bgb];"
            f"[fg]scale={CANVAS_W}:{CANVAS_H}:force_original_aspect_ratio=decrease[fgs];"
            f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2"
        )

    # 2. Ken Burns via zoompan. Expressions are linear in 'on' (output frame
    #    index) -> fully deterministic, no accumulating jitter.
    cx = "iw/2-(iw/zoom/2)"
    cy = "ih/2-(ih/zoom/2)"
    if shot.motion == "zoom_in":
        zexpr, xexpr, yexpr = f"1+({z}-1)*on/{nm1}", cx, cy
    elif shot.motion == "zoom_out":
        zexpr, xexpr, yexpr = f"{z}-({z}-1)*on/{nm1}", cx, cy
    elif shot.motion == "pan_right":
        zexpr, xexpr, yexpr = f"{z}", f"(iw-iw/zoom)*on/{nm1}", cy
    elif shot.motion == "pan_left":
        zexpr, xexpr, yexpr = f"{z}", f"(iw-iw/zoom)*(1-on/{nm1})", cy
    elif shot.motion == "pan_down":
        zexpr, xexpr, yexpr = f"{z}", cx, f"(ih-ih/zoom)*on/{nm1}"
    elif shot.motion == "pan_up":
        zexpr, xexpr, yexpr = f"{z}", cx, f"(ih-ih/zoom)*(1-on/{nm1})"
    else:  # hold
        zexpr, xexpr, yexpr = "1", cx, cy

    zoompan = (
        f"zoompan=z='{zexpr}':x='{xexpr}':y='{yexpr}':"
        f"d={n}:s={OUT_W}x{OUT_H}:fps={FPS},setsar=1,format=yuv420p"
    )

    graph = f"[0:v]{compose},{zoompan}"

    # 3. Optional burned onscreen label (e.g. an age "35"). Captions stay soft.
    if shot.onscreen_label and font and draw_ok:
        label = shot.onscreen_label.replace("\\", "").replace("'", "")
        graph += (
            f",drawtext=fontfile={font}:text='{label}':"
            f"fontcolor=white:fontsize=64:box=1:boxcolor=black@0.45:"
            f"boxborderw=18:x=72:y=64"
        )
    graph += "[v]"
    return graph


def render_shot(shot: Shot, font: str | None, draw_ok: bool,
                out_path: Path) -> None:
    seg_dur = shot.seg_dur
    cmd = [FFMPEG, "-y", "-loop", "1", "-i", str(shot.image)]
    if shot.audio is not None:
        cmd += ["-ss", f"{shot.audio_start:.6f}", "-i", str(shot.audio)]
        audio_map = "1:a"
        audio_filter = ["-af", "apad", "-t", f"{seg_dur:.6f}"]
    else:
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
        audio_map = "1:a"
        audio_filter = ["-t", f"{seg_dur:.6f}"]

    fg = compose_filter(shot, font, draw_ok)
    cmd += [
        "-filter_complex", fg,
        "-map", "[v]", "-map", audio_map,
        "-r", str(FPS), "-t", f"{seg_dur:.6f}",
        *audio_filter,
        *VIDEO_CODEC, *AUDIO_CODEC,
        "-movflags", "+faststart",
        str(out_path),
    ]
    run(cmd, f"shot {shot.index:02d} render")


# ---- Per-shot segment cache ----------------------------------------------
# Content-addressed: a shot's rendered .mp4 is keyed by a hash of EVERYTHING
# that determines its pixels + audio. Re-running after editing one image
# re-renders only that shot (its key changed) and reuses every other segment
# verbatim -> we never re-render the whole batch for a small fix.
def _file_digest(path: Path) -> str:
    """Streaming sha256 of a file's bytes (content, not path/mtime) so a swapped
    image at the same path correctly invalidates its cached segment."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _ffmpeg_identity() -> str:
    """Identity of the ffmpeg that will render — its resolved path + version
    banner. Folded into the cache key so a binary swap (lean<->ffmpeg-full keg,
    a brew upgrade that shifts x264/drawtext output) self-invalidates the cache
    instead of silently serving pixels the new binary wouldn't produce."""
    try:
        out = subprocess.run([FFMPEG, "-hide_banner", "-version"],
                             capture_output=True, text=True)
        first = (out.stdout or "").splitlines()[0] if out.stdout else ""
    except Exception:
        first = ""
    return f"{FFMPEG}|{FFPROBE}|{first}"


def shot_cache_key(shot: Shot, font: str | None, draw_ok: bool,
                   env_id: str) -> str:
    """A digest over every input that changes the rendered segment. Caption text
    is deliberately excluded — it only feeds the soft SRT, never the pixels.
    `env_id` is the ffmpeg identity (see _ffmpeg_identity)."""
    # Whether a label will actually be burned in (affects pixels); if it won't,
    # the label text is irrelevant to the segment.
    label_burns = bool(shot.onscreen_label and font and draw_ok)
    payload = {
        "cache_version": CACHE_VERSION,
        "env": env_id,
        "fps": FPS, "out": [OUT_W, OUT_H], "canvas": [CANVAS_W, CANVAS_H],
        "vcodec": VIDEO_CODEC, "acodec": AUDIO_CODEC,
        "image_sha": _file_digest(shot.image),
        "audio_sha": (_file_digest(shot.audio) if shot.audio is not None else None),
        "audio_start": round(shot.audio_start, 6),
        "n_frames": shot.n_frames,
        "motion": shot.motion,
        "zoom": round(shot.zoom, 6),
        "fit": shot.fit,
        "label": (shot.onscreen_label if label_burns else ""),
        "label_font": (font if label_burns else ""),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()


# ---- Captions -------------------------------------------------------------
def chunk_caption(text: str) -> list[str]:
    """Split a scene's narration into readable 1-2 line cues."""
    text = SSML_BREAK_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []
    # Split on sentence-ish boundaries, keeping the terminator.
    parts = re.split(r"(?<=[.!?—])\s+", text)
    cues: list[str] = []
    for part in parts:
        words = part.split()
        line, lines = "", []
        for w in words:
            cand = (line + " " + w).strip()
            if len(cand) <= CAP_MAX_CHARS:
                line = cand
            else:
                if line:
                    lines.append(line)
                line = w
                if len(lines) == CAP_MAX_LINES:
                    cues.append("\n".join(lines))
                    lines = []
        if line:
            lines.append(line)
        if lines:
            cues.append("\n".join(lines))
    return [c for c in cues if c.strip()]


def srt_timestamp(t: float) -> str:
    # Integer-ms math so the carry cascades correctly through s/m/h
    # (avoids invalid ":60,000" seams at minute/hour boundaries).
    total_ms = max(0, int(round(t * 1000)))
    h, rem = divmod(total_ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _merge_to_max(cues: list[str], weights: list[int], max_cues: int):
    """Collapse adjacent cues so at most `max_cues` remain — used when a shot
    is too short to give every cue a readable dwell. Keeps cue count down
    instead of stretching cues past the shot (which would overlap)."""
    if max_cues >= len(cues):
        return cues, weights
    per = -(-len(cues) // max_cues)   # ceil division
    merged, mweights = [], []
    for i in range(0, len(cues), per):
        merged.append(" ".join(cues[i:i + per]))
        mweights.append(sum(weights[i:i + per]))
    return merged, mweights


def _wrap_lines(text: str) -> list[str]:
    """Greedy word-wrap: each line <= CAP_MAX_CHARS, except a single token longer
    than that (unbreakable — gets its own over-length line, as chunk_caption does).
    No line-count cap here; callers gate cue length via _cue_fits."""
    lines: list[str] = []
    line = ""
    for w in text.split():
        cand = (line + " " + w).strip()
        if not line or len(cand) <= CAP_MAX_CHARS:
            line = cand
        else:
            lines.append(line)
            line = w
    if line:
        lines.append(line)
    return lines


def _cue_fits(text: str) -> bool:
    """True if `text` word-wraps into at most CAP_MAX_LINES lines. (A line may
    exceed CAP_MAX_CHARS only for a single unbreakable token.)"""
    return len(_wrap_lines(text)) <= CAP_MAX_LINES


def _wrap_cue(text: str) -> str:
    """Render a cue's text as wrapped lines. Cue builders keep text within
    CAP_MAX_LINES via _cue_fits, so this never has to squash overflow."""
    return "\n".join(_wrap_lines(text))


def _valid_words(ws: object) -> bool:
    """A word list is usable only if every entry is a dict carrying text + numeric
    start/end. Guards against a valid-JSON-but-wrong-shape align.json (e.g. words
    as bare strings, or a dict missing 'start') crashing the word-timed path."""
    return (isinstance(ws, list) and bool(ws) and all(
        isinstance(w, dict) and isinstance(w.get("text"), str)
        and isinstance(w.get("start"), (int, float))
        and isinstance(w.get("end"), (int, float))
        for w in ws))


def _partition_clip_words(words: list[dict],
                          shots: list[Shot], shot_idxs: list[int]
                          ) -> dict[int, list[dict]]:
    """Assign each aligned word to EXACTLY ONE of the clip's shots, by spoken
    start time against the shots' audio_start boundaries (in order); the last
    shot takes the tail. Partitioning once per clip — rather than filtering each
    shot's [start,end) window independently — makes assignment exactly-once
    regardless of the ~10ms window overlaps/gaps that aligner jitter produces
    (which otherwise double-caption or drop boundary words)."""
    starts = [shots[j].audio_start for j in shot_idxs]
    buckets: dict[int, list[dict]] = {j: [] for j in shot_idxs}
    n = len(shot_idxs)
    k = 0
    for w in sorted(words, key=lambda x: float(x["start"])):
        ws = float(w["start"])
        while k + 1 < n and ws >= starts[k + 1]:
            k += 1
        buckets[shot_idxs[k]].append(w)
    return buckets


def _cue_join(a: str, b: str) -> str | None:
    """Merge two cue texts into one <= CAP_MAX_LINES block, or None if the result
    won't wrap that small (don't overflow a line just to avoid a short cue)."""
    combo = (a.replace("\n", " ") + " " + b.replace("\n", " ")).strip()
    return _wrap_cue(combo) if _cue_fits(combo) else None


def _merge_short_cues(cues: list[tuple[float, float, str]]
                      ) -> list[tuple[float, float, str]]:
    """Keep captions off sub-second flashes (as the proportional path's merging
    does). A short cue is folded into a neighbor when the combined text still
    fits; when it can't, its display is simply lengthened to the min instead of
    overflowing a caption line (the final monotonic pass re-resolves overlap)."""
    if len(cues) <= 1:
        return cues
    merged: list[tuple[float, float, str]] = []
    for s, e, txt in cues:
        if merged and (e - s) < CAP_MIN_CUE_S:
            ps, pe, pt = merged[-1]
            joined = _cue_join(pt, txt)
            if joined is not None:
                merged[-1] = (ps, e, joined)
                continue
            e = max(e, s + CAP_MIN_CUE_S)  # too long to merge; just don't flash
        merged.append((s, e, txt))
    if len(merged) >= 2 and (merged[0][1] - merged[0][0]) < CAP_MIN_CUE_S:
        (s0, _e0, t0), (s1, e1, t1) = merged[0], merged[1]
        joined = _cue_join(t0, t1)
        if joined is not None:
            merged[0:2] = [(s0, e1, joined)]
        else:
            merged[0] = (s0, max(_e0, s0 + CAP_MIN_CUE_S), t0)
    return merged


def _lengthen_short(cues: list[tuple[float, float, str]]
                    ) -> list[tuple[float, float, str]]:
    """Give any remaining sub-CAP_MIN_CUE_S cue more read time by extending its end
    into the gap before the next cue (never past it, so no overlap/cascade). Catches
    single-cue shots that the per-shot merge can't reach; genuinely packed cues with
    no gap stay short (unavoidable). `cues` is already start-ascending."""
    out: list[tuple[float, float, str]] = []
    for i, (s, e, t) in enumerate(cues):
        if e - s < CAP_MIN_CUE_S:
            nxt = cues[i + 1][0] if i + 1 < len(cues) else e + CAP_MIN_CUE_S
            e = min(max(e, s + CAP_MIN_CUE_S), max(nxt, e))
        out.append((s, e, t))
    return out


def _word_timed_cues(words: list[dict], offset: float, audio_start: float,
                     seg_dur: float) -> list[tuple[float, float, str]]:
    """Group a shot's aligned words into cues, timing each from its first/last
    word's real spoken time mapped into the assembled timeline
    (abs = offset + word_time - audio_start), clamped inside the shot window.

    A cue closes when the next word would exceed CAP_MAX_WORDS or no longer wrap
    into <= CAP_MAX_LINES lines (so a cue never overflows), and on a sentence-final
    token only once it has read for >= CAP_MIN_CUE_S — so a short sentence or a
    lone em-dash never becomes its own sub-second flash cue; a final merge pass
    absorbs any that still slip under the floor."""
    out: list[tuple[float, float, str]] = []
    end_t = offset + seg_dur
    buf: list[dict] = []

    def flush() -> None:
        if not buf:
            return
        s = offset + (float(buf[0]["start"]) - audio_start)
        e = offset + (float(buf[-1]["end"]) - audio_start)
        s = min(max(s, offset), end_t)
        e = min(max(e, s), end_t)
        out.append((s, e, _wrap_cue(" ".join(w["text"] for w in buf))))

    for w in words:
        # Would adding this word overflow the cue's word/line budget? If so, close
        # the current cue first so this word starts the next one (never overflow).
        joined = " ".join(x["text"] for x in buf + [w])
        if buf and (len(buf) >= CAP_MAX_WORDS or not _cue_fits(joined)):
            flush()
            buf = []
        buf.append(w)
        span = float(buf[-1]["end"]) - float(buf[0]["start"])
        if CAP_SENT_END_RE.search(w["text"]) and span >= CAP_MIN_CUE_S:
            flush()
            buf = []
    flush()
    return _merge_short_cues(out)


def _proportional_cues(caption_text: str, offset: float,
                       seg_dur: float) -> list[tuple[float, float, str]]:
    """Legacy fallback: distribute a shot's caption across [offset, offset+seg_dur]
    by character weight. Used when no trustworthy alignment exists for the shot."""
    cues = chunk_caption(caption_text)
    if not cues:
        return []
    weights = [max(len(c.replace("\n", " ")), 1) for c in cues]
    max_cues = max(1, int(seg_dur // CAP_MIN_CUE_S))
    cues, weights = _merge_to_max(cues, weights, max_cues)
    total_w = sum(weights)
    out: list[tuple[float, float, str]] = []
    cum = 0
    for cue, w in zip(cues, weights):
        start = offset + seg_dur * (cum / total_w)
        cum += w
        end = offset + seg_dur * (cum / total_w)
        out.append((start, end, cue))
    return out


def _clip_alignments(shots: list[Shot], clips: dict[Path, list[int]],
                     align: bool) -> dict[Path, dict | None]:
    """Resolve one trusted alignment (or None) per VO clip.

    Without --align: read an existing *.mp3.align.json, but DISTRUST it if the
    mp3 is newer than the cache (re-rendered audio -> stale word times) or its
    match_rate is very low -> those fall back to proportional timing.
    With --align: force-refresh a stale/weak clip by re-aligning against the
    spoken (clean_vo_text) form so the transcript matches the current audio."""
    out: dict[Path, dict | None] = {}
    align_mod = None
    gen_mod = None
    tried_gen = False
    for mp3, shot_idxs in clips.items():
        result: dict | None = None
        try:
            if align_mod is None:
                align_mod = _vo_module("align_vo")
            cp = align_mod.cache_path(mp3)
            cached = None
            if cp.is_file():
                try:
                    d = json.loads(cp.read_text(encoding="utf-8"))
                    if _valid_words(d.get("words")):
                        cached = d
                except (OSError, json.JSONDecodeError):
                    cached = None
            stale = (not cp.is_file()) or (
                mp3.is_file() and mp3.stat().st_mtime > cp.stat().st_mtime)
            weak = (cached is None
                    or cached.get("match_rate", 0.0) < CAP_ALIGN_REFRESH)
            if align and (stale or weak):
                if not tried_gen:
                    tried_gen = True
                    try:
                        gen_mod = _vo_module("generate_vo")
                    except Exception:
                        gen_mod = None
                scene_text = " ".join(
                    shots[j].caption_text for j in shot_idxs
                    if shots[j].caption_text).strip()
                spoken = (gen_mod.clean_vo_text(scene_text)
                          if gen_mod and scene_text else scene_text)
                if spoken:
                    try:
                        result = align_mod.load_or_align(mp3, spoken, force=True)
                    except SystemExit:
                        # Refresh failed: keep a fresh-but-weak cache, but never a
                        # stale one (its word times are for the OLD audio).
                        result = None if stale else cached
                else:
                    # Can't refresh a fully caption-less clip (no transcript to
                    # align against); keep the cache only if it isn't stale, same
                    # as the refresh-failure guard above.
                    result = None if stale else cached
            else:
                # Trust the cache only if it isn't stale-by-mtime.
                result = None if (stale and not align) else cached
        except Exception:
            result = None
        if result is not None and result.get("match_rate", 0.0) < CAP_ALIGN_TRUST:
            result = None  # too little anchored to real audio -> proportional
        out[mp3] = result
    return out


def build_srt(shots: list[Shot], *, align: bool = False) -> str:
    """Caption timeline on the rendered seg_dur (N/FPS) spine, so the SRT never
    drifts from the video regardless of raw VO clip lengths.

    Per shot: if a trustworthy forced-alignment (*.mp3.align.json) exists for its
    VO clip, each cue is placed at the ACTUAL spoken word times (offset +
    word.start/end) so captions track speech instead of an even character split.
    Shots with no/stale/low-match alignment fall back to the legacy proportional
    split over their [offset, offset+seg_dur] window. A final pass makes cues
    strictly monotonic + non-overlapping and clamps the last cue to end within
    the video."""
    offsets: list[float] = []
    off = 0.0
    for s in shots:
        offsets.append(off)
        off += s.seg_dur
    total = off

    # Group shots by their VO clip so a scene split over several shots shares one
    # alignment read and its words are partitioned across those shots exactly once.
    # Include EVERY audio-bearing shot (even caption-less ones): when one shot of a
    # scene carries the whole caption but a later shot plays the rest of the clip's
    # audio, that later shot must be a partition boundary and gets its own aligned
    # words — otherwise its speech is crushed into the caption-bearing shot's tail.
    clips: dict[Path, list[int]] = {}
    for i, s in enumerate(shots):
        if s.audio is not None:
            clips.setdefault(s.audio, []).append(i)

    alignments = _clip_alignments(shots, clips, align) if clips else {}

    # Per clip with a trusted alignment, assign each word to exactly one shot.
    clip_buckets: dict[Path, dict[int, list[dict]]] = {}
    for mp3, shot_idxs in clips.items():
        al = alignments.get(mp3)
        if al:
            clip_buckets[mp3] = _partition_clip_words(al["words"], shots, shot_idxs)

    cues: list[tuple[float, float, str]] = []
    for i, shot in enumerate(shots):
        offset, seg_dur = offsets[i], shot.seg_dur
        words = (clip_buckets.get(shot.audio, {}).get(i)
                 if shot.audio is not None else None)
        if words:
            # Cue text comes from the aligned words, so a caption-less shot that
            # plays real narration still gets correctly-timed captions.
            cues += _word_timed_cues(words, offset, shot.audio_start, seg_dur)
        elif shot.caption_text:
            cues += _proportional_cues(shot.caption_text, offset, seg_dur)

    cues = _lengthen_short(cues)  # floor sub-second cues that per-shot merge missed

    # Strictly monotonic, non-overlapping, every cue ends within the video. A cue
    # squeezed to zero at the tail has its TEXT folded into a preceding kept cue
    # rather than dropped, so aligned words are never silently lost — but only
    # while the fold still fits a readable 2-line block (_cue_join). If nothing
    # can absorb it (pathological tail bunching), we drop it with a loud WARN
    # rather than emit an unreadable 200-char line.
    kept: list[tuple[float, float, str]] = []
    prev_end = 0.0
    unplaced = 0
    for start, end, cue in cues:
        start = min(max(start, prev_end), total)
        end = min(max(end, start + 0.2), total)
        if end <= start:
            for j in (-1, -2):  # last kept cue, then the one before it
                if len(kept) >= -j:
                    joined = _cue_join(kept[j][2], cue)
                    if joined is not None:
                        ps, pe, _pt = kept[j]
                        kept[j] = (ps, pe, joined)
                        break
            else:
                unplaced += len(cue.split())
            continue
        prev_end = end
        kept.append((start, end, cue))
    if unplaced:
        sys.stderr.write(f"[assemble] WARN: {unplaced} tail caption word(s) could "
                         "not be placed within the video and were dropped\n")
    entries = [f"{n + 1}\n{srt_timestamp(s)} --> {srt_timestamp(e)}\n{t}\n"
               for n, (s, e, t) in enumerate(kept)]
    return "\n".join(entries) + ("\n" if entries else "")


# ---- Concat + orchestration ----------------------------------------------
def concat_shots(shot_files: list[Path], out_path: Path, tmp: Path) -> None:
    listfile = tmp / "concat.txt"
    listfile.write_text(
        "".join(f"file '{p.as_posix()}'\n" for p in shot_files))
    cmd = [FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", str(listfile),
           "-c", "copy", "-movflags", "+faststart", str(out_path)]
    run(cmd, "concat")


def main() -> None:
    ap = argparse.ArgumentParser(description="3SK video auto-assembly engine")
    ap.add_argument("manifest", help="path to the edit manifest JSON")
    ap.add_argument("--output", help="override output mp4 path")
    ap.add_argument("--keep-temp", action="store_true",
                    help="keep the per-shot temp files for inspection")
    ap.add_argument("--no-cache", action="store_true",
                    help="render every shot fresh; ignore + don't write the "
                         "segment cache (old all-shots behavior, for debugging)")
    ap.add_argument("--cache-dir",
                    help="override the per-shot segment cache dir "
                         "(default: ~/.cache/3sk_assemble/<output-stem>, "
                         "kept OUT of the git-tracked vault)")
    ap.add_argument("--align", action="store_true",
                    help="place captions at real spoken-word times from each "
                         "scene's *.mp3.align.json; force-refresh a scene's "
                         "alignment when the mp3 is newer than its cache or its "
                         "match_rate is weak (refresh needs faster-whisper)")
    args = ap.parse_args()

    for tool in (FFMPEG, FFPROBE):
        if not (os.path.isabs(tool) and os.path.exists(tool)) and shutil.which(tool) is None:
            die(f"{tool} not found (brew install ffmpeg, or ffmpeg-full for drawtext)")

    manifest_path = Path(args.manifest).resolve()
    if not manifest_path.exists():
        die(f"manifest not found: {manifest_path}")
    manifest = json.loads(manifest_path.read_text())

    asset_dir = Path(os.path.expanduser(manifest.get("asset_dir", "."))).resolve()
    if not asset_dir.is_dir():
        die(f"asset_dir is not a directory: {asset_dir}")

    if args.output:
        out_mp4 = Path(args.output).resolve()
    else:
        out_dir = Path(os.path.expanduser(
            manifest.get("output_dir", str(manifest_path.parent)))).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        out_mp4 = out_dir / (manifest.get("output_name", manifest_path.stem) + ".mp4")
    out_srt = out_mp4.with_suffix(".srt")

    font = pick_font()
    draw_ok = has_drawtext()
    shots = build_shots(manifest, asset_dir)
    wants_label = any(s.onscreen_label for s in shots)
    if wants_label and not (font and draw_ok):
        reason = "ffmpeg has no drawtext filter" if not draw_ok else "no usable font"
        print(f"[assemble] WARN: onscreen_label set but {reason} -> "
              f"labels skipped (captions are unaffected)")

    total = sum(s.seg_dur for s in shots)
    print(f"[assemble] {len(shots)} shots, {total:.1f}s "
          f"({total/60:.2f} min) -> {out_mp4}")

    # Per-shot segment cache: reuse unchanged shots, re-render only edited ones.
    use_cache = not args.no_cache
    cache_dir: Path | None = None
    env_id = ""
    lock_fd = None
    if use_cache:
        if args.cache_dir:
            cache_dir = Path(os.path.expanduser(args.cache_dir)).resolve()
        else:
            base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
            cache_dir = (Path(base) / "3sk_assemble" / out_mp4.stem).resolve()
        cache_dir.mkdir(parents=True, exist_ok=True)
        # Single-writer lock per cache namespace: the prune step below deletes
        # any segment this run doesn't reference, which would clobber a
        # concurrent run sharing the same output stem (and its just-staged,
        # not-yet-concatenated segments). Take an exclusive non-blocking lock;
        # if another run holds it, fall back to a cache-less fresh render (safe,
        # correct, just not incremental) rather than racing the prune.
        lock_fd = os.open(str(cache_dir / ".lock"), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            print("[assemble] WARN: another assemble run holds the cache lock "
                  f"({cache_dir}) -> rendering fresh without the cache this run")
            os.close(lock_fd)
            lock_fd = None
            use_cache = False

    tmp = Path(tempfile.mkdtemp(prefix="assemble_"))
    rendered = reused = 0
    try:
        if use_cache:
            env_id = _ffmpeg_identity()
        shot_files: list[Path] = []
        referenced: set[Path] = set()
        for shot in shots:
            if use_cache:
                key = shot_cache_key(shot, font, draw_ok, env_id)
                seg = cache_dir / f"shot_{shot.index:02d}_{key[:24]}.mp4"
                referenced.add(seg)
                if seg.exists() and seg.stat().st_size > 0:
                    print(f"[assemble]  shot {shot.index:02d}: {shot.image.name} "
                          f"{shot.motion} {shot.seg_dur:.1f}s  [cached]")
                    reused += 1
                    shot_files.append(seg)
                    continue
                print(f"[assemble]  shot {shot.index:02d}: {shot.image.name} "
                      f"{shot.motion} {shot.seg_dur:.1f}s  [render]")
                # Stage INSIDE the cache dir (same filesystem) so os.replace is
                # atomic and never raises EXDEV across volumes; the '.staging_'
                # prefix keeps it out of the 'shot_*.mp4' reuse + prune globs. A
                # killed render leaves only the staging file, cleaned up below.
                staged = cache_dir / f".staging_{shot.index:02d}_{key[:24]}.mp4"
                try:
                    render_shot(shot, font, draw_ok, staged)
                    os.replace(staged, seg)
                finally:
                    if staged.exists():
                        staged.unlink(missing_ok=True)
                rendered += 1
                shot_files.append(seg)
            else:
                sf = tmp / f"shot_{shot.index:02d}.mp4"
                print(f"[assemble]  shot {shot.index:02d}: {shot.image.name} "
                      f"{shot.motion} {shot.seg_dur:.1f}s")
                render_shot(shot, font, draw_ok, sf)
                shot_files.append(sf)

        concat_shots(shot_files, out_mp4, tmp)
        srt = build_srt(shots, align=args.align)
        out_srt.write_text(srt)

        if use_cache:
            # Prune segments in THIS output's cache namespace that this run no
            # longer references (plus any orphaned staging files from a killed
            # run), so the cache can't grow without bound across edits. The
            # single-writer lock above makes full-namespace pruning safe.
            pruned = 0
            for f in cache_dir.glob("shot_*.mp4"):
                if f not in referenced:
                    f.unlink(missing_ok=True)
                    pruned += 1
            for f in cache_dir.glob(".staging_*.mp4"):
                f.unlink(missing_ok=True)
            print(f"[assemble] cache: {rendered} rendered, {reused} reused"
                  + (f", {pruned} stale pruned" if pruned else "")
                  + f"  ({cache_dir})")
    finally:
        if lock_fd is not None:
            os.close(lock_fd)  # releases the flock
        if args.keep_temp:
            print(f"[assemble] temp kept at {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)

    final_dur = ffprobe_duration(out_mp4)
    print(f"[assemble] DONE  {out_mp4}  ({final_dur:.1f}s)")
    print(f"[assemble] SRT   {out_srt}")


def _selftest() -> None:
    """Assert the word-timed caption path: real word times, monotonic non-overlap,
    tail within video, and clean fallback to proportional when no alignment."""
    global _clip_alignments  # tests monkeypatch the resolver to inject alignments
    def mk(idx, seg, cap, audio=None, audio_start=0.0):
        return Shot(index=idx, image=Path("x.png"), audio=audio,
                    audio_start=audio_start, n_frames=round(seg * FPS),
                    motion="hold", zoom=1.0, fit="cover",
                    caption_text=cap, onscreen_label="")

    # One scene (mp3 "a") split across two shots of a 5s clip, plus a silent tail.
    mp3 = Path("/tmp/_selftest_a.mp3")
    words = [{"text": "Your", "start": 0.0, "end": 0.4},
             {"text": "account", "start": 0.4, "end": 1.0},
             {"text": "says.", "start": 1.0, "end": 2.0},
             {"text": "The", "start": 2.6, "end": 2.8},
             {"text": "number", "start": 2.8, "end": 3.4},
             {"text": "$290,000.", "start": 3.4, "end": 4.9}]
    shots = [mk(1, 2.5, "Your account says.", audio=mp3, audio_start=0.0),
             mk(2, 2.5, "The number $290,000.", audio=mp3, audio_start=2.5),
             mk(3, 0.5, "")]  # silent tail
    clips = {mp3: [0, 1]}
    al = {"words": words, "match_rate": 0.9}

    # Partition assigns each word to exactly one shot by audio_start boundary.
    buckets = _partition_clip_words(words, shots, [0, 1])
    assert [w["text"] for w in buckets[0]] == ["Your", "account", "says."], buckets
    assert [w["text"] for w in buckets[1]] == ["The", "number", "$290,000."], buckets

    # Boundary/jitter regression (the confirmed HIGH bug): a word starting just
    # BEFORE the split must land in exactly ONE shot, never both. Here "The"
    # starts at 2.49 (< shot 1's 2.5 audio_start) -> belongs to shot 0 only.
    jw = [{"text": "a", "start": 0.1, "end": 0.4},
          {"text": "The", "start": 2.49, "end": 2.7},
          {"text": "end.", "start": 2.7, "end": 3.0}]
    jb = _partition_clip_words(jw, shots, [0, 1])
    seen = [w["text"] for v in jb.values() for w in v]
    assert seen.count("The") == 1 and sorted(seen) == ["The", "a", "end."], jb

    c1 = _word_timed_cues(buckets[1], 2.5, 2.5, 2.5)
    # Cue for shot 1 starts at the word's real spoken time mapped to the timeline:
    # offset 2.5 + (2.6 - 2.5) = 2.6s, NOT a proportional guess.
    assert abs(c1[0][0] - 2.6) < 1e-6, c1
    assert "$290,000" in c1[-1][2]

    # Caption-less-audio-shot regression (the Video_08 crush): a shot playing the
    # clip's tail with an EMPTY caption still gets its own correctly-timed cues,
    # instead of its words being crushed into the caption-bearing shot's window.
    cl_shots = [mk(1, 2.5, "Your account says.", audio=mp3, audio_start=0.0),
                mk(2, 2.5, "", audio=mp3, audio_start=2.5)]  # empty cap, real audio
    orig_cl = _clip_alignments  # restored below
    try:
        _clip_alignments = lambda s, c, a: {mp3: al}  # noqa: E731
        cl_srt = build_srt(cl_shots)
    finally:
        _clip_alignments = orig_cl
    cl_cues = [(b.split("\n")[1], "\n".join(b.split("\n")[2:]))
               for b in cl_srt.strip().split("\n\n") if b.strip()]
    hit = [tm for tm, tx in cl_cues if "$290,000" in tx]
    assert hit, cl_srt                    # shot 2's words captioned, not dropped
    start_s = int(hit[0][6:8]) + int(hit[0][9:12]) / 1000  # MM:SS,mmm -> secs
    assert start_s >= 2.5 - 1e-6, (start_s, cl_srt)  # timed in shot 2, not crushed

    # No sub-second flash cues, and a lone em-dash never becomes its own cue.
    dash = [{"text": w, "start": i * 0.15, "end": i * 0.15 + 0.15}
            for i, w in enumerate(["So.", "—", "then.", "we.", "go.", "far."])]
    dc = _word_timed_cues(dash, 0.0, 0.0, 1.0)
    assert all(e - s >= CAP_MIN_CUE_S or k == len(dc) - 1
               for k, (s, e, _t) in enumerate(dc)), dc
    assert all(t.strip() != "—" for _s, _e, t in dc), dc

    # Wrong-shape align.json is refused by the validator (no crash downstream).
    assert not _valid_words(["hello", "world"])          # words as strings
    assert not _valid_words([{"text": "x", "start": 0.0}])  # missing 'end'
    assert _valid_words(words)

    # --align on a fully caption-less clip must NOT caption from a STALE cache
    # (the branch the all-audio-shot scoping made reachable): there's no
    # transcript to refresh against, so a stale-by-mtime cache -> None, exactly
    # like the refresh-failure guard. Uses the REAL _clip_alignments.
    td = Path(tempfile.mkdtemp(prefix="assemble_st_"))
    try:
        cmp3 = td / "clip.mp3"
        cmp3.write_bytes(b"x")
        cj = _vo_module("align_vo").cache_path(cmp3)
        cj.write_text(json.dumps({"match_rate": 0.9, "words": [
            {"text": "STALEGHOST.", "start": 0.0, "end": 1.0}]}))
        os.utime(cj, (1000, 1000))    # cache older than...
        os.utime(cmp3, (2000, 2000))  # ...the re-rendered mp3 -> stale
        cshots = [mk(1, 1.0, "", audio=cmp3, audio_start=0.0)]  # caption-less
        cclips = {cmp3: [0]}
        assert _clip_alignments(cshots, cclips, False)[cmp3] is None
        assert _clip_alignments(cshots, cclips, True)[cmp3] is None
        assert "STALEGHOST" not in build_srt(cshots, align=True)
    finally:
        shutil.rmtree(td, ignore_errors=True)

    # End-to-end build_srt with alignment injected (monkeypatch the resolver).
    orig = _clip_alignments
    try:
        _clip_alignments = lambda s, c, a: {mp3: al}
        srt = build_srt(shots, align=False)
    finally:
        _clip_alignments = orig
    # Parse cue times; assert monotonic, non-overlapping, within total (5.5s).
    times = re.findall(r"(\d\d):(\d\d):(\d\d),(\d\d\d) --> "
                       r"(\d\d):(\d\d):(\d\d),(\d\d\d)", srt)
    assert times, srt
    prev = 0.0
    total = sum(sh.seg_dur for sh in shots)
    for t in times:
        s = int(t[0]) * 3600 + int(t[1]) * 60 + int(t[2]) + int(t[3]) / 1000
        e = int(t[4]) * 3600 + int(t[5]) * 60 + int(t[6]) + int(t[7]) / 1000
        assert s >= prev - 1e-6 and e > s and e <= total + 1e-6, (s, e, total)
        prev = e

    # Fallback: no alignment for the clip -> proportional, still valid + within.
    try:
        _clip_alignments = lambda s, c, a: {mp3: None}
        srt2 = build_srt(shots, align=False)
    finally:
        _clip_alignments = orig
    assert srt2.strip() and "-->" in srt2

    # Tail crush: four ultra-short shots can't each hold a >=0.2s cue within the
    # total; the squeezed tail text must be FOLDED into the last cue, not dropped.
    tiny = [mk(1, 0.15, "alpha"), mk(2, 0.15, "bravo"),
            mk(3, 0.15, "charlie"), mk(4, 0.15, "zulu")]
    stiny = build_srt(tiny)
    for word in ("alpha", "bravo", "charlie", "zulu"):
        assert word in stiny, (word, stiny)

    # Heavy tail crush: many wordy cues squeezed at the end must NEVER overflow a
    # caption line (the fold is length-gated), even when some tail text is dropped.
    crush = [mk(i + 1, 0.15, f"crush {i} some longer filler words here")
             for i in range(8)]
    scrush = build_srt(crush)
    for blk in scrush.strip().split("\n\n"):
        for ln in blk.split("\n")[2:]:  # skip index + timing lines
            assert len(ln) <= CAP_MAX_CHARS, (len(ln), ln)

    # A lone short cue with a gap after it is lengthened to the readable floor;
    # a short cue butted against the next one can't extend and stays as-is.
    lz = _lengthen_short([(0.0, 0.3, "hi"), (5.0, 6.0, "there")])
    assert lz[0][1] - lz[0][0] >= CAP_MIN_CUE_S - 1e-9, lz
    lz2 = _lengthen_short([(0.0, 0.3, "hi"), (0.3, 1.5, "there")])
    assert abs(lz2[0][1] - 0.3) < 1e-9, lz2

    # A caption-less all-silent manifest yields an empty SRT (no crash).
    assert build_srt([mk(1, 1.0, "")]) == ""
    print("[assemble] selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        main()
