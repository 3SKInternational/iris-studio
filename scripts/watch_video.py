#!/usr/bin/env python3
"""watch_video.py SOURCE [flags] — let a Claude agent "watch" any video.

Turns a video URL (YouTube/IG/TikTok/anything yt-dlp supports) or a local file
into a WATCH PACK the research agents can Read: timestamped transcript +
uniformly-sampled JPEG frames + metadata + an INDEX.md. Transcript comes free
from native captions (yt-dlp); if a video has none (IG reels, raw clips) and a
GROQ_API_KEY is configured, audio is transcribed via Groq Whisper. Frames via
ffmpeg. Built 2026-07-03 for the "study in totality" mandate — transcripts
alone miss what's ON SCREEN (charts, text overlays, editing pace).

Usage:
  python3 scripts/watch_video.py "https://youtu.be/abc123"
  python3 scripts/watch_video.py "URL" --start 0:00 --end 1:00   # hook study
  python3 scripts/watch_video.py clip.mp4 --max-frames 20
  python3 scripts/watch_video.py "URL" --transcript-only

Flags:
  --start/--end T     window (SS, MM:SS or HH:MM:SS); frames + transcript both clip
  --max-frames N      frame cap (default 30; window mode densifies up to this)
  --width W           frame width px (default 512; 1024 to read on-screen text)
  --out-dir D         pack dir (default video_studies/<video-id>/ next to repo)
  --transcript-only   no video download, no frames
  --keep-video        keep the downloaded mp4 (default: deleted after frames)

Credentials: GROQ_API_KEY from the environment, else ~/.config/watch/.env
(NEVER from the vault or CWD — the vault is Drive-synced off-machine).

Exit: 0 = pack written (a caption-less keyless video still ships frames-only,
noted in INDEX.md) | 1 = real failure (download/extract error, bad args).
Prints the pack path + file list last so the calling agent knows what to Read.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_STUDIES = REPO / "video_studies"  # gitignored; NOT the vault (frames are third-party content)
GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
DL_TIMEOUT = 600   # ponytail: wall-clock caps so a stalled host can't wedge a cron agent
FF_TIMEOUT = 300
WHISPER_CHUNK_MB = 24  # Groq upload cap is 25MB; stay under


def die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def parse_time(t: str) -> float:
    """'90', '1:30' or '0:01:30' -> seconds."""
    parts = t.split(":")
    if not 1 <= len(parts) <= 3 or not all(p.strip() for p in parts):
        raise ValueError(f"bad time {t!r}")
    parts = [float(p) for p in parts]
    while len(parts) < 3:
        parts.insert(0, 0.0)
    h, m, s = parts
    return h * 3600 + m * 60 + s


def fmt_ts(sec: float) -> str:
    sec = int(sec)
    if sec >= 3600:
        return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"
    return f"{sec // 60}:{sec % 60:02d}"


def run(cmd: list[str], timeout: int, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, timeout=timeout, **kw)


def is_url(src: str) -> bool:
    return src.startswith(("http://", "https://"))


# ---------- transcript ----------

TAG_RE = re.compile(r"<[^>]+>")
CUE_RE = re.compile(r"(\d+:)?(\d+):(\d+)[.,](\d+)\s+-->")


def parse_vtt(text: str) -> list[tuple[float, str]]:
    """VTT -> [(start_sec, line)], rolling-caption duplicates collapsed."""
    out: list[tuple[float, str]] = []
    last = ""
    t = 0.0
    for raw in text.splitlines():
        m = CUE_RE.match(raw.strip())
        if m:
            h = float(m.group(1)[:-1]) if m.group(1) else 0.0
            frac = float(f"0.{m.group(4)}")
            t = h * 3600 + float(m.group(2)) * 60 + float(m.group(3)) + frac
            continue
        line = TAG_RE.sub("", raw).strip()
        if not line or line == "WEBVTT" or line.startswith(("Kind:", "Language:", "NOTE")):
            continue
        if line == last:  # auto-subs re-emit the previous line in each cue
            continue
        last = line
        out.append((t, line))
    return out


def window(cues: list[tuple[float, str]], start: float, end: float) -> list[tuple[float, str]]:
    return [(t, s) for t, s in cues if start <= t <= end]


def parse_json3(raw: bytes) -> list[tuple[float, str]]:
    """YouTube json3 caption events -> [(start_sec, line)]."""
    out = []
    for ev in json.loads(raw).get("events", []):
        text = "".join(seg.get("utf8", "") for seg in (ev.get("segs") or [])).strip()
        if text:
            out.append((ev.get("tStartMs", 0) / 1000.0, text))
    return out


def load_groq_key() -> str | None:
    key = os.environ.get("GROQ_API_KEY")
    if key:
        return key
    env = Path.home() / ".config/watch/.env"
    if env.is_file():
        for line in env.read_text().splitlines():
            if line.startswith("GROQ_API_KEY=") and line.split("=", 1)[1].strip():
                return line.split("=", 1)[1].strip()
    return None


def whisper_groq(audio: Path, key: str) -> list[tuple[float, str]]:
    """Groq whisper-large-v3, chunked under the 25MB cap. Partial chunks degrade, not die."""
    size_mb = audio.stat().st_size / 1e6
    dur = probe_duration(audio)
    n_chunks = max(1, int(size_mb // WHISPER_CHUNK_MB) + (1 if size_mb % WHISPER_CHUNK_MB else 0))
    chunk_sec = dur / n_chunks if n_chunks > 1 else dur
    cues: list[tuple[float, str]] = []
    for i in range(n_chunks):
        piece = audio
        off = i * chunk_sec
        if n_chunks > 1:
            piece = audio.with_name(f"chunk_{i:03d}.mp3")
            run(["ffmpeg", "-y", "-loglevel", "error", "-ss", str(off),
                 "-t", str(chunk_sec), "-i", str(audio), "-c", "copy", str(piece)],
                timeout=FF_TIMEOUT, check=True)
        try:
            for seg_start, seg_text in _groq_post(piece, key):
                cues.append((off + seg_start, seg_text))
        except Exception as e:  # noqa: BLE001 — partial transcript beats none in an unattended run
            print(f"warn: whisper chunk {i} failed: {e}", file=sys.stderr)
        finally:
            if piece != audio:
                piece.unlink(missing_ok=True)
    return cues


def _groq_post(audio: Path, key: str) -> list[tuple[float, str]]:
    boundary = uuid.uuid4().hex
    parts = []
    for name, val in (("model", "whisper-large-v3"), ("response_format", "verbose_json")):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{val}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                 f'filename="{audio.name}"\r\nContent-Type: audio/mpeg\r\n\r\n'.encode())
    body = b"".join(parts) + audio.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        GROQ_URL, data=body, method="POST",
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = json.loads(resp.read())
    return [(s["start"], s["text"].strip()) for s in data.get("segments", []) if s.get("text", "").strip()]


# ---------- media ----------

def probe_duration(path: Path) -> float:
    p = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)], timeout=60, capture_output=True, text=True)
    try:
        return float(p.stdout.strip())
    except ValueError:
        return 0.0


def ytdlp_meta(url: str) -> dict:
    p = run(["yt-dlp", "--no-warnings", "-J", "--skip-download", "--", url],
            timeout=DL_TIMEOUT, capture_output=True, text=True)
    if p.returncode != 0:
        die(f"yt-dlp metadata failed: {p.stderr.strip()[-400:]}")
    info = json.loads(p.stdout)
    if info.get("_type") == "playlist":  # a bare channel/playlist URL — refuse, we watch ONE video
        die("source is a playlist/channel, not a single video")
    return info


def fetch_captions(info: dict) -> tuple[list[tuple[float, str]], bool]:
    """(cues, throttled). Reads the caption-track URL straight out of the yt-dlp
    metadata we already fetched (same approach as scripts/transcript_pull.py) —
    no extra yt-dlp subprocess. Manual subs beat auto; json3 beats vtt. One
    30s-backoff retry on HTTP 429 — throttled is NOT 'video has no captions'."""
    track = None
    for src_key in ("subtitles", "automatic_captions"):
        tracks = info.get(src_key) or {}
        for lang in ("en", "en-US", "en-GB", "en-orig"):
            if tracks.get(lang):
                track = tracks[lang]
                break
        if track:
            break
    if not track:
        return [], False
    pick = next((t for t in track if t.get("ext") == "json3"),
                next((t for t in track if t.get("ext") == "vtt"), track[0]))
    for attempt in (1, 2):
        try:
            raw = urllib.request.urlopen(pick["url"], timeout=30).read()
            if pick.get("ext") == "json3":
                return parse_json3(raw), False
            return parse_vtt(raw.decode("utf-8", errors="replace")), False
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt == 1:
                import time
                time.sleep(30)
                continue
            print(f"warn: caption fetch failed (HTTP {e.code})", file=sys.stderr)
            return [], e.code == 429
        except Exception as e:  # noqa: BLE001 — degrade to frames-only, never die on captions
            print(f"warn: caption fetch failed: {e}", file=sys.stderr)
            return [], False
    return [], True


def fetch_video(url: str, work: Path) -> Path:
    p = run(["yt-dlp", "--no-warnings", "-f", "bv*[height<=720]+ba/b[height<=720]/b",
             "--merge-output-format", "mp4", "-o", str(work / "video.%(ext)s"), "--", url],
            timeout=DL_TIMEOUT, capture_output=True, text=True)
    vids = [f for f in work.iterdir() if f.stem == "video"]
    if p.returncode != 0 or not vids:
        die(f"video download failed: {p.stderr.strip()[-400:]}")
    return vids[0]


def extract_audio(video: Path, work: Path) -> Path:
    out = work / "audio.mp3"
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(video), "-vn",
         "-ac", "1", "-ar", "16000", "-b:a", "64k", str(out)],
        timeout=FF_TIMEOUT, check=True)
    return out


def extract_frames(video: Path, out: Path, start: float, end: float,
                   max_frames: int, width: int) -> list[tuple[float, Path]]:
    """Uniform sampling across [start,end], renamed with real timestamps."""
    out.mkdir(parents=True, exist_ok=True)
    span = max(end - start, 1.0)
    interval = max(span / max_frames, 0.5)  # ponytail: uniform only; scene-detect if it ever misses cuts
    tmp_pat = str(out / "raw_%04d.jpg")
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-ss", str(start), "-to", str(end),
           "-i", str(video), "-vf", f"fps=1/{interval},scale={width}:-2",
           "-frames:v", str(max_frames), "-q:v", "4", tmp_pat]
    run(cmd, timeout=FF_TIMEOUT, check=True)
    frames = []
    for i, f in enumerate(sorted(out.glob("raw_*.jpg"))):
        # +0.5: fps=1/interval emits the source frame nearest each slot CENTER, so the i-th
        # emitted frame's real content time is start + (i+0.5)*interval, not start + i*interval.
        # Ground-truthed 2026-07-04 by checksum-correlating emitted frames back to their true
        # decode time (content = label + interval/2 across whole-clip + window cases). Labeling
        # with i alone put frames ~interval/2 early (~23s on a default 30-frame ~23min watch) and
        # nearly caused a false assembly-QA flag. Guard: test_watch_video_frames.py.
        t = start + (i + 0.5) * interval
        dest = out / f"frame_{i + 1:03d}_t{fmt_ts(t).replace(':', 'm', 1).replace(':', 's')}.jpg"
        f.rename(dest)
        frames.append((t, dest))
    return frames


# ---------- main ----------

def main() -> None:
    ap = argparse.ArgumentParser(description="Make a video watchable by a Claude agent")
    ap.add_argument("source")
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--max-frames", type=int, default=30)
    ap.add_argument("--width", type=int, default=512)
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--transcript-only", action="store_true")
    ap.add_argument("--keep-video", action="store_true")
    args = ap.parse_args()

    src = args.source
    if src.startswith("-"):
        die("source may not start with '-'")
    try:
        start = parse_time(args.start) if args.start else 0.0
        end = parse_time(args.end) if args.end else None
    except ValueError as e:
        die(str(e))
    if args.max_frames < 1 or args.width < 64:
        die("--max-frames must be >=1 and --width >=64")

    # -- identify + pack dir
    local = not is_url(src)
    info: dict = {}
    if local:
        vid_path = Path(src)
        if not vid_path.is_file():
            die(f"no such file: {src}")
        meta = {"id": vid_path.stem, "title": vid_path.name, "source": str(vid_path.resolve())}
    else:
        info = ytdlp_meta(src)
        meta = {"id": info.get("id", "video"), "title": info.get("title"),
                "channel": info.get("channel") or info.get("uploader"),
                "duration_sec": info.get("duration"), "view_count": info.get("view_count"),
                "upload_date": info.get("upload_date"), "source": src}
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", str(meta["id"]))[:80]
    pack = Path(args.out_dir) if args.out_dir else DEFAULT_STUDIES / safe_id
    pack.mkdir(parents=True, exist_ok=True)
    (pack / "meta.json").write_text(json.dumps(meta, indent=2))

    # -- transcript: captions first, Whisper fallback, honest absence last
    cues: list[tuple[float, str]] = []
    tsource = "none"
    if not local:
        cues, throttled = fetch_captions(info)
        if cues:
            tsource = "captions"
        elif throttled:
            tsource = "captions-throttled"  # captions EXIST; YouTube 429'd us — retry later
            print("warn: caption fetch throttled (HTTP 429) — transcript missing but "
                  "the video may have captions; retry later", file=sys.stderr)

    video_file: Path | None = vid_path if local else None
    need_video = not args.transcript_only or (not cues and load_groq_key())
    if not local and need_video and video_file is None:
        video_file = fetch_video(src, pack)

    if not cues and video_file is not None:
        key = load_groq_key()
        if key:
            audio = extract_audio(video_file, pack)
            cues = whisper_groq(audio, key)
            tsource = "whisper-groq" if cues else "whisper-failed"
            audio.unlink(missing_ok=True)
        elif tsource == "none":
            print("note: no captions and no GROQ_API_KEY — frames-only pack "
                  "(key goes in ~/.config/watch/.env, never the vault)", file=sys.stderr)

    duration = meta.get("duration_sec") or (probe_duration(video_file) if video_file else 0.0)
    if local and not duration:
        die(f"cannot read media duration — corrupt or unsupported file? {vid_path}")
    end = end if end is not None else (duration or (cues[-1][0] + 10 if cues else 0))
    if end <= start:
        die(f"--end ({fmt_ts(end)}) must be after --start ({fmt_ts(start)})")
    if args.start or args.end:
        cues = window(cues, start, end)
    transcript = pack / "transcript.txt"
    transcript.write_text("\n".join(f"[{fmt_ts(t)}] {s}" for t, s in cues) or
                          f"(no transcript available — source: {tsource})\n")

    # -- frames
    frames: list[tuple[float, Path]] = []
    if not args.transcript_only and video_file is not None:
        frames = extract_frames(video_file, pack / "frames", start, end,
                                args.max_frames, args.width)
    if video_file is not None and not local and not args.keep_video:
        video_file.unlink(missing_ok=True)

    # -- index the pack for the agent
    lines = [f"# Watch pack — {meta.get('title')}", "",
             f"- source: {meta['source']}",
             f"- channel: {meta.get('channel', 'n/a')} | duration: {fmt_ts(duration) if duration else 'n/a'}"
             f" | views: {meta.get('view_count', 'n/a')} | uploaded: {meta.get('upload_date', 'n/a')}",
             f"- window: {fmt_ts(start)} → {fmt_ts(end)}",
             f"- transcript: transcript.txt ({tsource}, {len(cues)} lines)",
             f"- frames: {len(frames)} in frames/ (timestamps in filenames)", "",
             "Read transcript.txt and every frames/*.jpg (frames render as images; "
             "read them in one parallel batch, aligned to transcript timestamps)."]
    (pack / "INDEX.md").write_text("\n".join(lines) + "\n")

    print(f"pack: {pack}")
    print(f"  meta.json | transcript.txt ({tsource}) | {len(frames)} frames")
    for t, f in frames:
        print(f"  {f}")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as e:
        die(f"subprocess failed (exit {e.returncode}): {e.cmd[0]}")
    except subprocess.TimeoutExpired as e:
        die(f"subprocess timed out after {e.timeout}s: {e.cmd[0]}")
