#!/usr/bin/env python3
"""Local forced-alignment for 3SK VO clips — real word timestamps, $0, no API.

WHY THIS EXISTS
---------------
The video orchestrator (`build_video.py`) places each image's on-screen window
inside a scene by splitting the narration *by caption character-count* — an
estimate. Real speech has pauses, emphasis and varying pace, so inside a long
scene the picture drifts several seconds out of step with the words being said.
That is the "speech and video don't line up" feeling.

This module fixes the estimate at the source: it finds where each word is
ACTUALLY spoken in a clip and returns those word timestamps. `build_video.py`
then cuts images at real word boundaries instead of guessing from text length.

HOW (local, no network, no billing)
------------------------------------
We already have the EXACT transcript, so this is alignment, not transcription.
1. `faster-whisper` (CTranslate2 backend — no PyTorch) transcribes the audio
   locally with word-level timestamps. This is the only heavy step; the model
   is downloaded once and cached by huggingface under ~/.cache.
2. We align OUR transcript's words to whisper's hypothesis words with
   `difflib.SequenceMatcher`. Matched words inherit whisper's real timestamps
   (anchors); unmatched runs (e.g. "$74,000" vs "seventy four thousand") are
   linearly interpolated between surrounding anchors. The output is therefore
   one timestamp per word OF OUR TRANSCRIPT, in transcript order — exactly the
   shape a forced-alignment API would return, so `build_video.py` can map a
   shot boundary (a word index in the caption) straight to a real time.

Results are CACHED next to the clip as `<stem>.align.json` (keyed by a hash of
the transcript) so re-assembly never re-runs alignment for unchanged audio.

USAGE
  python3 align_vo.py CLIP.mp3 --text "the exact transcript"
  python3 align_vo.py CLIP.mp3 --text-file transcript.txt
  python3 align_vo.py CLIP.mp3 --text "..." --model small.en  # whisper size
  python3 align_vo.py CLIP.mp3 --text "..." --force            # ignore cache
  python3 align_vo.py CLIP.mp3 --text "..." --print            # dump, no cache write
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

# Whisper model size. base.en is fast and accurate enough for clean narration
# (we only need common words as alignment anchors — numbers are interpolated).
# Override with --model or ALIGN_MODEL env. small.en is more accurate, slower.
DEFAULT_MODEL = os.environ.get("ALIGN_MODEL", "base.en")
_SCHEMA = 2  # bump when the cache shape changes so stale caches re-align

_BREAK_RE = re.compile(r"<break[^>]*/?>", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")
_NORM_RE = re.compile(r"[^a-z0-9]+")


def die(msg: str) -> None:
    sys.stderr.write(f"[align_vo] ERROR: {msg}\n")
    raise SystemExit(1)


def _clean_text(text: str) -> str:
    """Drop SSML <break> tags (not spoken) and collapse whitespace."""
    return _WS_RE.sub(" ", _BREAK_RE.sub(" ", text or "")).strip()


def tokenize(text: str) -> list[str]:
    """Canonical whitespace tokenizer.

    MUST stay identical to how build_video.py counts caption words, because a
    shot boundary is expressed as a word index into this same token list.
    """
    return _clean_text(text).split()


def _norm(tok: str) -> str:
    return _NORM_RE.sub("", tok.lower())


def _text_hash(text: str) -> str:
    # Hash the canonical (cleaned) form so cosmetic whitespace/break edits that
    # don't change the spoken words still hit cache.
    return hashlib.sha256(_clean_text(text).encode("utf-8")).hexdigest()[:16]


def _transcribe(audio_path: Path, model_size: str):
    """Run faster-whisper; return (hyp_words, audio_duration).

    hyp_words: list of (norm_key, start, end) for each recognised word, in time
    order, skipping words whose normalised key is empty.
    """
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        die("faster-whisper not installed. Run: "
            "/usr/bin/python3 -m pip install --user faster-whisper")
    try:
        # int8 on CPU keeps memory + time low; deterministic greedy-ish decode.
        model = WhisperModel(model_size, device="cpu", compute_type="int8")
    except Exception as e:  # noqa: BLE001 - surface any load/download failure
        die(f"could not load whisper model '{model_size}': {e}")
    try:
        import warnings
        import numpy as np
        hyp = []
        # faster-whisper's mel-filter matmul emits harmless divide/overflow
        # warnings on silent padding (during feature extraction AND lazy
        # decoding); silence both numpy float-errors and the RuntimeWarnings —
        # the timings are valid.
        with np.errstate(divide="ignore", over="ignore", invalid="ignore"), \
                warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            segments, info = model.transcribe(
                str(audio_path),
                language="en",
                word_timestamps=True,
                beam_size=5,
                vad_filter=False,
            )
            for seg in segments:  # generator — consume fully (this does the work)
                for w in (seg.words or []):
                    key = _norm(w.word)
                    if not key:
                        continue
                    hyp.append((key, float(w.start), float(w.end)))
    except Exception as e:  # noqa: BLE001
        die(f"transcription failed for {audio_path.name}: {e}")
    duration = float(getattr(info, "duration", 0.0) or 0.0)
    if duration <= 0.0 and hyp:
        duration = hyp[-1][2]
    return hyp, duration


def _interp_times(n_tokens: int, anchors: dict[int, float], duration: float) -> list[float]:
    """Piecewise-linear time for each *boundary* position 0..n_tokens.

    anchors maps token-index -> known start time. We always pin position 0 to
    0.0 and position n_tokens to `duration`, then linearly interpolate every
    other boundary. Anchor times are clamped to be non-decreasing so the result
    is monotonic (boundaries never go backwards).
    """
    pts = {0: 0.0, n_tokens: max(duration, 0.0)}
    for idx, t in anchors.items():
        if 0 < idx < n_tokens:
            pts[idx] = t
    xs = sorted(pts)
    # Enforce monotonic, in-range times across the known points.
    ys = []
    run_max = 0.0
    for x in xs:
        y = min(max(pts[x], run_max), max(duration, 0.0))
        run_max = y
        ys.append(y)
    out = []
    for pos in range(n_tokens + 1):
        # locate bracketing known points
        if pos <= xs[0]:
            out.append(ys[0])
            continue
        if pos >= xs[-1]:
            out.append(ys[-1])
            continue
        # binary-ish linear scan (token counts are small)
        lo = 0
        for k in range(len(xs) - 1):
            if xs[k] <= pos <= xs[k + 1]:
                lo = k
                break
        x0, x1, y0, y1 = xs[lo], xs[lo + 1], ys[lo], ys[lo + 1]
        frac = (pos - x0) / (x1 - x0) if x1 > x0 else 0.0
        out.append(y0 + frac * (y1 - y0))
    return out


def align(audio_path: Path, text: str, *, model_size: str = DEFAULT_MODEL) -> dict:
    """Align `text` to `audio_path`; return word timings for OUR transcript."""
    toks = tokenize(text)
    if not toks:
        die(f"empty transcript for {audio_path.name}; cannot align.")
    if not audio_path.is_file():
        die(f"cannot read audio {audio_path}")

    hyp, duration = _transcribe(audio_path, model_size)

    anchors: dict[int, float] = {}
    matched = 0
    if hyp:
        import difflib
        tkeys = [_norm(t) for t in toks]
        hkeys = [h[0] for h in hyp]
        sm = difflib.SequenceMatcher(a=tkeys, b=hkeys, autojunk=False)
        for i, j, size in sm.get_matching_blocks():
            for k in range(size):
                anchors[i + k] = hyp[j + k][1]  # token i+k starts at hyp start
                matched += 1

    boundaries = _interp_times(len(toks), anchors, duration)
    words = []
    for i, surface in enumerate(toks):
        start = round(boundaries[i], 3)
        end = round(max(boundaries[i + 1], boundaries[i]), 3)
        words.append({"text": surface, "start": start, "end": end})

    match_rate = round(matched / len(toks), 4) if toks else 0.0
    return {
        "schema": _SCHEMA,
        "engine": "faster-whisper",
        "model": model_size,
        "words": words,
        "audio_duration": round(duration, 3),
        "match_rate": match_rate,         # 1.0 = every word anchored to real audio
        "loss": round(1.0 - match_rate, 4),  # familiar "lower is better" field
        "text_hash": _text_hash(text),
    }


def cache_path(audio_path: Path) -> Path:
    return audio_path.with_suffix(audio_path.suffix + ".align.json")


def load_or_align(audio_path: Path, text: str, *, model_size: str = DEFAULT_MODEL,
                  force: bool = False) -> dict:
    """Return word timings for a clip, using the on-disk cache when it matches."""
    cp = cache_path(audio_path)
    want = _text_hash(text)
    if not force and cp.is_file():
        try:
            cached = json.loads(cp.read_text(encoding="utf-8"))
            if (cached.get("text_hash") == want
                    and cached.get("schema") == _SCHEMA
                    and isinstance(cached.get("words"), list) and cached["words"]):
                return cached
        except (OSError, json.JSONDecodeError):
            pass  # fall through to a fresh alignment
    result = align(audio_path, text, model_size=model_size)
    tmp = cp.with_suffix(cp.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(result, indent=2), encoding="utf-8")
        os.replace(tmp, cp)
    finally:
        if tmp.exists():
            tmp.unlink()
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description="Local forced alignment for a VO clip.")
    ap.add_argument("audio", help="path to the VO clip (mp3/wav/...)")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--text", help="transcript string to align")
    src.add_argument("--text-file", help="file containing the transcript")
    ap.add_argument("--model", default=DEFAULT_MODEL,
                    help=f"faster-whisper model size (default: {DEFAULT_MODEL})")
    ap.add_argument("--force", action="store_true", help="ignore cache, re-align")
    ap.add_argument("--print", dest="do_print", action="store_true",
                    help="print word timings; do not write the cache")
    args = ap.parse_args()

    audio = Path(os.path.expanduser(args.audio)).resolve()
    if not audio.is_file():
        die(f"audio not found: {audio}")
    text = args.text if args.text is not None else \
        Path(os.path.expanduser(args.text_file)).read_text(encoding="utf-8")

    if args.do_print:
        result = align(audio, text, model_size=args.model)
        for w in result["words"]:
            print(f"  {w['start']:7.3f}–{w['end']:7.3f}  {w['text']}")
        print(f"[align_vo] {len(result['words'])} words, "
              f"match_rate={result.get('match_rate')}, model={result.get('model')}")
        return

    result = load_or_align(audio, text, model_size=args.model, force=args.force)
    print(f"[align_vo] {len(result['words'])} words -> {cache_path(audio)}  "
          f"(match_rate={result.get('match_rate')}, model={result.get('model')})")


if __name__ == "__main__":
    main()
