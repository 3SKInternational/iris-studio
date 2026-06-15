---
date: 2026-06-15
type: engineering-spec
status: active
component: E12 Auto-Assembly Engine
related:
  - "[[2026-06-15_Video_Production_Pipeline]]"
tags:
  - engineering/video-factory
---

# Edit-Manifest Schema + Engine Runbook (E12)

The **edit manifest** is the key primitive of the video factory: it decouples
*content* (reviewable, diffable, regenerable) from *rendering*. A human edits
the manifest; the engine (`assemble.py`) renders it deterministically. An
upstream agent can author the manifest from a script + VO kit.

One manifest = one video. One **shot** = one still image + one Ken Burns move +
(optionally) one VO clip + caption text. The locked template is: *image + VO +
slow move + caption*, repeated per shot.

## Running a render

```bash
cd /Volumes/AI_Workspace/iris_studio/video_factory
python3 assemble.py manifests/video_01.json          # render to manifest's output_dir
python3 assemble.py manifests/video_01.json --output /tmp/test.mp4
python3 assemble.py manifests/proof_30s.json --keep-temp   # keep per-shot files
```

Requires `ffmpeg` + `ffprobe` (`brew install ffmpeg`). No Python deps beyond the
stdlib. Output: `<output_name>.mp4` (H.264 1080p, AAC stereo) plus a soft-CC
`<output_name>.srt` sidecar next to it. Re-running on the same manifest produces
the same video (deterministic).

**Binary resolution:** the engine picks its ffmpeg/ffprobe in this order —
`$FFMPEG`/`$FFPROBE` env override → keg-only `/opt/homebrew/opt/ffmpeg-full/bin`
→ PATH. The lean homebrew `ffmpeg` formula has **no `drawtext` filter** (it
dropped libfreetype), so `onscreen_label` burn-in needs the fuller build:
`brew install ffmpeg-full` (keg-only — does not shadow the PATH `ffmpeg`). The
engine auto-prefers it. Without a drawtext-capable build, labels are skipped
with a warning (captions are unaffected).

## Top-level fields

| Field | Required | Default | Meaning |
|---|---|---|---|
| `video` | no | — | Label, for humans. |
| `asset_dir` | yes | `.` | Base dir for relative `image`/`vo_clip` paths. `~` is expanded. |
| `output_dir` | no | manifest dir | Where the mp4 + srt are written. `~` expanded. |
| `output_name` | no | manifest filename | Output basename (no extension). |
| `defaults` | no | `{}` | Per-shot defaults — see below. |
| `shots` | yes | — | Ordered list of shot objects. Render order = list order. |

`defaults` keys: `zoom` (float, default `1.04` — a gentle ~4% move; raise for
more dramatic Ken Burns), `fit` (`cover`|`contain`, default `cover`). Any shot
can override.

## Shot fields

| Field | Required | Default | Meaning |
|---|---|---|---|
| `image` | yes | — | Path to the still (relative to `asset_dir`, or absolute). |
| `vo_clip` | one of vo_clip / duration | — | Path to the VO audio for this shot. |
| `start` | no | `0` | Seconds into `vo_clip` to begin (trim head). |
| `end` | no | clip length | Seconds into `vo_clip` to stop (trim tail). |
| `duration` | one of vo_clip / duration | — | For a **silent** shot (no VO): shot length in seconds. |
| `motion` | no | `zoom_in` | Ken Burns move — see table. |
| `zoom` | no | `defaults.zoom` | Zoom factor (e.g. `1.04` = 4%). Also the pan headroom — a pan glides across this much of the frame. |
| `fit` | no | `defaults.fit` | `cover` (fill+crop) or `contain` (fit + blurred-fill bg). |
| `caption_text` | no | `""` | Narration for this shot. Auto-chunked into soft SRT cues. SSML `<break/>` tags are stripped. |
| `onscreen_label` | no | `""` | Short burned-in label (e.g. an age `"35"`). Requires a freetype/drawtext-enabled ffmpeg; skipped with a warning if unavailable. Captions are unaffected. |

### Duration resolution (per shot)

1. If `vo_clip` is set: shot length = `end - start` (defaults: whole clip).
   The engine validates `end` does not exceed the clip's real length.
2. Else if `duration` is set: a silent shot of that length.
3. Each shot's video length is `round(length * 30) / 30` (frame-exact at 30fps),
   and the VO is padded/trimmed to that exact length. This locks audio↔video per
   shot and prevents cumulative drift across a long video. The SRT timeline is
   built from these same frame-exact lengths, so captions never drift.

### Motion values

| `motion` | Effect |
|---|---|
| `zoom_in` | Slow push in to center (1.0 → `zoom`). |
| `zoom_out` | Slow pull out from center (`zoom` → 1.0). |
| `pan_left` / `pan_right` | Hold `zoom`, glide horizontally. |
| `pan_up` / `pan_down` | Hold `zoom`, glide vertically. |
| `hold` | Static frame, no move. |

Motion is linear in the output frame index (deterministic, jitter-free). Ken
Burns is computed on a 3840×2160 canvas then downscaled to 1920×1080, so the
move is sub-pixel smooth.

## Minimal example

```json
{
  "asset_dir": "~/Documents/3SK/outputs/BRANDS/3SK_Finance",
  "output_dir": "~/Documents/3SK/outputs/BRANDS/3SK_Finance/Footage_and_Edits",
  "output_name": "Video_01_v2",
  "defaults": { "zoom": 1.04, "fit": "cover" },
  "shots": [
    {
      "image": "Raw_Assets/Video_01/Video_01_Scene_01.png",
      "vo_clip": "Voice_Files/Video_01/Video_01_VO_Scene_01.mp3",
      "motion": "zoom_in",
      "onscreen_label": "4 AM",
      "caption_text": "It's 4 AM. You can't sleep. You open your bank app."
    }
  ]
}
```

## Design notes

- **No music bed** (brand choice 2026-06-15). VO is the only audio.
- **Soft SRT captions, not burned-in** (matches Willie Finance's soft-CC style).
- **Audio spine** is butt-to-butt: shot N's VO follows shot N-1's with no gap.
  (Crossfade is intentionally not implemented — the VO clips carry their own
  natural lead/trail silence, and butt-to-butt keeps the timeline exact.)
- The engine is `ffmpeg`-only by deliberate choice over MoviePy/Remotion: free,
  owned, lowest-dependency, fast (~8× realtime), and runs on the always-on Mini.
