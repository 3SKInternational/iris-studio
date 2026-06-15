---
date: 2026-06-15
type: engineering-spec
status: active
component: Image Factory (scene-image batch generator)
related:
  - "[[schema]]"
  - "[[2026-06-15_Video_Production_Pipeline]]"
tags:
  - engineering/video-factory
---

# Image-Manifest Schema + Generator Runbook

The **image manifest** is the upstream sibling of the video factory's edit
manifest. It decouples *prompts* (reviewable, diffable, regenerable) from
*generation*: a human authors the manifest, `generate_images.py` renders one PNG
per entry deterministically-ish through a swappable provider, and those PNGs
become the `image` inputs the video factory's `assemble.py` then animates.

One manifest = one video's still set. One **image** entry = one scene PNG.

## Why a manifest (and not just the ChatGPT chat)

Steve's hand workflow uploads the six "Three" reference PNGs into a fresh chat,
pastes a style preamble once, then pastes each scene block. That works because a
*chat* has memory. The image **API is stateless** — every call is independent —
so the manifest re-supplies the memory on every call:

- `style_preamble` is prepended to every image's prompt.
- `reference_images` are re-sent with every reference-anchored image (via the
  edits endpoint).

That is what holds the character consistent across a batch without a human in
the loop. This is the bridge to the V2 Flux + character-LoRA workflow, where the
reference becomes a true programmatic input instead of a chat upload.

## Running a batch

```bash
cd /Volumes/AI_Workspace/iris_studio/image_factory
pip install -r requirements.txt                 # one-time: the openai client
cp .env.example .env && chmod 600 .env          # one-time: add your OPENAI_API_KEY

python3 generate_images.py manifests/video_01_images.json --dry-run   # plan + cost, no calls
python3 generate_images.py manifests/video_01_images.json             # generate
python3 generate_images.py manifests/video_01_images.json --limit 1   # smoke-test one
python3 generate_images.py manifests/video_01_images.json --quality high --force
```

The API key is read from `OPENAI_API_KEY` (environment first, then a `.env` file
next to the script). **Never put the key in the vault** — it's git-tracked +
synced. Output: `<output_dir>/<name>.png` per image. Re-running **skips images
whose PNG already exists** (resumable) unless `--force`.

## Top-level fields

| Field | Required | Default | Meaning |
|---|---|---|---|
| `project` | no | `untitled` | Label, for humans. |
| `output_dir` | no | manifest dir | Where PNGs are written. `~` expanded. |
| `provider` | no | `openai` | Generation backend. `openai` (live) or `flux` (stub). |
| `reference_dir` | no | manifest dir | Base dir for relative `reference_images`. `~` expanded. |
| `reference_images` | no | `[]` | Character/style anchors re-sent with every ref-anchored image. |
| `style_preamble` | no | `""` | Prepended to every image's prompt (the stateless-API "paste once"). |
| `defaults` | no | `{}` | `model`, `quality`, `size`, `input_fidelity` — see below. |
| `images` | yes | — | Ordered list of image objects. |

`defaults` keys: `model` (default `gpt-image-1` — see *Model choice* below),
`quality` (`low`|`medium`|`high`|`auto`, default `medium`), `size`
(`1024x1024`|`1536x1024`|`1024x1536`|`auto`, default `1536x1024` — 3:2 landscape
that feeds the 1920×1080 video render), `input_fidelity` (`low`|`high`, default
`high` — only applies to gpt-image-1's edits endpoint; silently skipped on
models that don't support it).

## Image fields

| Field | Required | Default | Meaning |
|---|---|---|---|
| `name` | yes | — | Output basename (no extension). Match the video manifest's `image` names. |
| `prompt` | yes | — | Scene description. `style_preamble` is prepended automatically. |
| `quality` | no | `defaults.quality` | Per-image override. |
| `size` | no | `defaults.size` | Per-image override. |
| `use_references` | no | `true` | Set `false` for character-free scenes (e.g. a CTA card). |

## CLI flags (all override the manifest)

| Flag | Effect |
|---|---|
| `--provider` | `openai` \| `flux`. |
| `--model` | Model id — a config value, so a deprecation is a one-flag change. |
| `--quality` / `--size` | Apply to the whole batch. |
| `--input-fidelity` | `low` \| `high`. Reference fidelity (gpt-image-1 edits only). |
| `--output` | Override `output_dir`. |
| `--limit N` | Generate at most N images (smoke test). |
| `--force` | Re-render images whose PNG already exists. |
| `--dry-run` | Print the plan + a cost estimate; no API calls, no writes. |

## Cost

gpt-image is **token-priced** (text input + image input + image output tokens).
`--dry-run` prints a rough estimate from a per-quality token table; a live run
reads the **actual** cost back from each call's `usage` field and reports the
billed total. Order-of-magnitude for the 12-shot Video 01 at `medium` + 5
references: a few dollars. Drivers, biggest first: **quality** (`high` is ~4×
`medium` output tokens), then the **per-image reference inputs**. The price table
lives at the top of `generate_images.py` — a price change is a one-line edit.

## Swapping providers

`model`, `quality`, `size`, and `provider` are all config values; provider
backends are functions in a `PROVIDERS` dict. The `flux` entry is a deliberate
`NotImplementedError` stub. The planned swap — local Flux + a "Three" character
LoRA for locked consistency, once channel revenue funds the GPU time — adds a
`generate_flux()` body and (optionally) a new default model; the manifest format
and the rest of the pipeline stay identical. Local generation was benchmarked on
the M4 Mini (2026-06-15) and shelved as a *someday*: ~10–11 min/image at
dev-quality, not viable for production yet.

## Model choice (settled by a live A/B on 2026-06-15)

Counter-intuitively, the **deprecated** model is the right one for now:

- **gpt-image-1 + `input_fidelity: high` — the production config through launch.**
  In a same-prompt A/B on Scene 01, this was the only config that **held the
  character**: solid black dot-eyes, chibi proportions, flat 2D style — matching
  the locked reference. `input_fidelity: high` strictly preserves the look of the
  reference PNGs. Cost ~$0.33/image at `medium`. Caveat: gpt-image-1 **sunsets
  2026-12-01**, ~2 months past the Oct launch — so this is a *bridge*, not the end
  state.
- **gpt-image-2 — rejected for character work.** It does **not** support
  `input_fidelity` (returns a 400), and without it the same prompt drifted off
  model: realistic eyes instead of dots, normal instead of chibi proportions.
  Cheaper (~$0.13) but it loses "Three." Fine only for character-free frames.
- **Local Flux + "Three" LoRA — the permanent endgame.** A character LoRA locks
  the look independent of OpenAI's model churn (which the gpt-image-1 sunset makes
  a concrete risk). The Dec-2026 sunset is the natural deadline to have it built.

The lesson that held: keep the model id (and now `input_fidelity`) as config
values — this whole pivot was a manifest edit, not a code rewrite.

## Cowork-Iris access (author-side bridge)

Cowork-Iris can't see or run this tool — her filesystem connector is scoped to
the vault and she has no shell. The division of labor: **Cowork authors a
manifest** in the vault drop-folder
`BRANDS/3SK_Finance/Raw_Assets/Image_Factory/manifests/`, **Claude Code (or
Steve) runs** `generate_images.py <that-manifest>`, and the PNGs land back in
the manifest's vault `output_dir` so Cowork can review them. The Cowork-facing
guide + a `_TEMPLATE.json` + a copy of the example manifest live in that
`Image_Factory/` folder. Execution is deliberately **kept manual** (not a
watch-folder auto-runner) so every billed batch has a human cost gate.

## Design notes

- **Resumable by default.** A failed image doesn't kill the batch (per-image
  try/except); re-run to fill the gaps, `--force` to redo.
- **Atomic writes.** Each PNG is written to a temp file and `os.replace`d into
  place, so an interrupted run never leaves a half-written image that the
  skip-if-exists check would later trust.
- **No vault secrets.** Key in `image_factory/.env` (chmod 600, gitignored).
