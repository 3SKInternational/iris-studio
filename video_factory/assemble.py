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
SSML_BREAK_RE = re.compile(r"<break[^>]*/?>", re.IGNORECASE)


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


def build_srt(shots: list[Shot]) -> str:
    """Caption timeline aligned to the rendered seg_durs (N/FPS), so the SRT
    never drifts from the video regardless of raw VO clip lengths.

    Cue boundaries are cumulative proportional cut-points within each shot's
    [offset, offset+seg_dur] window: starts are strictly monotonic, every cue
    ends within the shot, and the last cue ends exactly at the shot boundary
    (== the next shot's offset). So cues never overlap or run backwards, and
    there is no cross-shot drift. CAP_MIN_CUE_S caps how MANY cues a short shot
    gets (via merging) rather than stretching any single cue past the shot."""
    entries: list[str] = []
    idx = 1
    offset = 0.0
    for shot in shots:
        seg_dur = shot.seg_dur
        cues = chunk_caption(shot.caption_text)
        if cues:
            weights = [max(len(c.replace("\n", " ")), 1) for c in cues]
            max_cues = max(1, int(seg_dur // CAP_MIN_CUE_S))
            cues, weights = _merge_to_max(cues, weights, max_cues)
            total_w = sum(weights)
            cum = 0
            for cue, w in zip(cues, weights):
                start = offset + seg_dur * (cum / total_w)
                cum += w
                end = offset + seg_dur * (cum / total_w)
                entries.append(
                    f"{idx}\n{srt_timestamp(start)} --> {srt_timestamp(end)}\n{cue}\n")
                idx += 1
        offset += seg_dur
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
        srt = build_srt(shots)
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


if __name__ == "__main__":
    main()
