---
date: 2026-06-15
type: engineering-spec
status: active
component: VO Factory (E1 ElevenLabs voice-over pipeline)
related:
  - "[[2026-06-15_Video_Production_Pipeline]]"
  - "[[2026-06-15_YouTube_Autonomy_Roadmap]]"
  - "[[2026-06-09_Claude_Code_Execution_Directions]]"
tags:
  - engineering/video-factory
---

# VO Factory — kit-in → scene-mp3s-out (Build E1)

The **VO kit** is the input primitive: each `## Scene N → \`Video_NN_VO_Scene_MM.mp3\``
block in a Session-B kit carries the reviewed, break-tagged narration. The
generator parses the kit, calls the ElevenLabs TTS API once per scene, and
writes one mp3 per block. Those mp3s become the `vo_clip` inputs the video
factory's `assemble.py` lays on the audio spine.

It is the missing upstream sibling of `image_factory` (prompts → PNGs) and the
audio source for `video_factory` (mp3s → rendered shots). Same design ethos:
voice id / model / settings / output dir are **config values** (env + CLI), so a
future voice swap is a flag change, not a rewrite. **Stdlib-only** (urllib) to
match `video_factory` — no pip install.

## Why parse the kit, not the script

The Session-B kit is the *reviewed, expanded, break-tagged* narration (Steve's
veto already applied, SSML `<break/>` pauses tuned per line). The raw script's
`**VO:**` blocks are the un-tuned source. We keep the kit's `<break/>` tags
verbatim — ElevenLabs honors them on `eleven_multilingual_v2` — and strip only
markdown emphasis and the header's `(label, timestamp)` so nothing decorative is
read aloud.

## Running

```bash
cd /Volumes/AI_Workspace/iris_studio/vo_factory

# 1. Verify the key first (free GET, no spend) — reports tier + remaining credits
python3 generate_vo.py <kit.md> --check

# 2. Plan + credit estimate, no API calls, no writes
python3 generate_vo.py <kit.md> --dry-run --output <fresh_dir>

# 3. Generate (billed — Steve authorizes)
python3 generate_vo.py <kit.md> --output <fresh_dir>
python3 generate_vo.py <kit.md> --limit 1            # smoke-test one clip
python3 generate_vo.py <kit.md> --force              # re-render existing mp3s
```

Example (regenerate V1 into a fresh dir, never overwriting the existing set):

```bash
python3 generate_vo.py \
  ~/Documents/3SK/outputs/BRANDS/3SK_Finance/Voice_Files/Video_01/_VO_Session_B_Kit.md \
  --output ~/Documents/3SK/outputs/BRANDS/3SK_Finance/Voice_Files/Video_01_gen
```

## Config values

| Source (lowest→highest precedence) | Keys |
|---|---|
| Built-in defaults | voice `nPczCjzI2devNBz1zQrb` (Brian) · model `eleven_multilingual_v2` · stability 0.5 · similarity 0.75 · style 0.0 |
| Repo-root `.env` | `ELEVENLABS_API_KEY` (required) · `ELEVENLABS_VOICE_ID` (optional override) |
| CLI flags | `--voice-id --model --stability --similarity --style --output` |

The API key is read from the `ELEVENLABS_API_KEY` environment variable first,
then the nearest `.env` walking up from the script (the repo-root
`/Volumes/AI_Workspace/iris_studio/.env`). **Never put the key in the vault** —
it is git-tracked + synced.

## Output

`<output_dir>/<filename-from-kit>.mp3` per scene (e.g.
`Video_01_VO_Scene_01.mp3`). Default `output_dir` is a **`<kit-folder>_gen`
sibling** (e.g. `Voice_Files/Video_01/` → `Voice_Files/Video_01_gen/`) so a bare
run never overwrites a hand-recorded set; pass `--output` to target a specific
dir. Re-running **skips scenes whose mp3 already exists** (resumable) unless
`--force`.

## Cost / safety

- ElevenLabs bills ~1 credit per character of submitted text (break tags
  included). `--dry-run` prints the per-scene char count + a total credit
  estimate **without calling the API**. V1's full 12-scene kit ≈ **12,200
  chars ≈ 12.2k credits** — fits a Starter month with room for one selective
  retake pass; watch the meter (`--check`) before a full run.
- **Resumable**: skip-if-mp3-exists; a failed clip doesn't kill the batch
  (per-clip try/except); re-run to fill gaps.
- **Atomic writes**: each mp3 is written to a temp file and `os.replace`d into
  place, so an interrupted run never leaves a half-written clip the
  skip-if-exists check would later trust.

## Divergence from the original E1 spec (noted, intentional)

The 2026-06-09 E1 spec named the script `scripts/generate_vo.py` and parsed the
*script's* `**VO:**` blocks. This build instead lives in `vo_factory/` (matching
the `image_factory/` + `video_factory/` sibling pattern established 2026-06-15)
and parses the *Session-B kit* (the reviewed narration, per the 2026-06-15
orchestrator pickup). Same behavior, better-aligned source + a consistent
factory layout for the orchestrator (`build_video.py`) to drive.

## Consumed by the orchestrator

`build_video.py <video_id>` runs this as its VO stage (skip-if-exists keeps it
idempotent). See `../build_video.py` and the pipeline design
([[2026-06-15_Video_Production_Pipeline]]).
