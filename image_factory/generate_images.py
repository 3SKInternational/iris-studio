#!/usr/bin/env python3
"""Batch scene-image generator for the 3SK video factory.

Reads an *image manifest* (JSON) and renders one PNG per `image` entry through a
pluggable provider. Today the provider is OpenAI's gpt-image line (the same
ChatGPT image lineage Steve already prompts by hand); the design keeps `model`,
`quality`, `size`, and `provider` as plain config values so the eventual swap to
a local Flux + character-LoRA pipeline is a provider module, not a rewrite.

Stateless-API note: a ChatGPT *chat* lets you upload the character references
and paste the style preamble once, then every later prompt inherits them. The
image API has no such memory — each call is independent. So the manifest carries
a top-level `style_preamble` (prepended to every prompt) and `reference_images`
(re-sent with every reference-anchored image via the edits endpoint). That is
what holds "Three" consistent across a batch.

Usage:
    python3 generate_images.py manifests/video_01_images.json
    python3 generate_images.py manifests/video_01_images.json --dry-run
    python3 generate_images.py manifests/video_01_images.json --quality high --force
    python3 generate_images.py manifests/video_01_images.json --limit 1

Only the stdlib + `openai` are required. The API key is read from
OPENAI_API_KEY (environment, or a `.env` file next to this script — never the
vault, which is git-tracked + synced).
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import tempfile
from pathlib import Path

# --- Config values (deliberately swappable) ---------------------------------

VALID_PROVIDERS = ("openai", "flux")
VALID_QUALITIES = ("low", "medium", "high", "auto")
VALID_SIZES = ("1024x1024", "1536x1024", "1024x1536", "auto")
VALID_FIDELITY = ("low", "high")

DEFAULT_PROVIDER = "openai"
DEFAULT_MODEL = "gpt-image-2"  # NOT gpt-image-1 (deprecated 2026-06-02, sunset 2026-12-01)
DEFAULT_QUALITY = "medium"
DEFAULT_SIZE = "1536x1024"  # 3:2 landscape, feeds the 1920x1080 video render
# Reference fidelity for the edits endpoint. "high" preserves the character's
# face/features/style from the reference PNGs far more strictly than the "low"
# default — required to hold "Three"'s dot-eyes + chibi proportions. Costs more
# image-input tokens. Ignored by the no-reference generate path.
DEFAULT_INPUT_FIDELITY = "high"
# input_fidelity is a gpt-image-1 parameter; gpt-image-2 rejects it with a 400
# (verified live 2026-06-15). gpt-image-1 + high fidelity is the only config
# tested that holds the character — but gpt-image-1 sunsets 2026-12-01, so this
# is a launch bridge; the permanent answer is the local Flux + LoRA provider.
MODELS_WITH_INPUT_FIDELITY = ("gpt-image-1",)

# gpt-image token pricing, USD per 1M tokens. Approximate — verify against
# OpenAI's pricing page. Kept here so a price change is a one-line edit.
PRICING = {
    "gpt-image-2": {"text_input": 5.0, "image_input": 10.0, "image_output": 40.0},
    "gpt-image-1": {"text_input": 5.0, "image_input": 10.0, "image_output": 40.0},
}
# Rough OUTPUT-token counts by quality x size — for the dry-run estimate only.
# The real cost is read back from the API `usage` field after each live call.
OUTPUT_TOKENS = {
    "low": {"1024x1024": 272, "1536x1024": 408, "1024x1536": 408},
    "medium": {"1024x1024": 1056, "1536x1024": 1584, "1024x1536": 1584},
    "high": {"1024x1024": 4160, "1536x1024": 6240, "1024x1536": 6240},
}


def die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def load_env_key(script_dir: Path) -> str | None:
    """OPENAI_API_KEY from the environment, falling back to a local .env."""
    key = os.environ.get("OPENAI_API_KEY")
    if key:
        return key.strip()
    env_path = script_dir / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            if name.strip() == "OPENAI_API_KEY":
                return value.strip().strip('"').strip("'")
    return None


def expand(path: str) -> Path:
    return Path(os.path.expanduser(path)).resolve()


def estimate_cost(model: str, quality: str, size: str, prompt: str, n_refs: int) -> float:
    """Rough USD estimate for one image — dry-run planning only."""
    price = PRICING.get(model, PRICING[DEFAULT_MODEL])
    q = quality if quality in OUTPUT_TOKENS else "medium"
    s = size if size in OUTPUT_TOKENS[q] else "1536x1024"
    out_tokens = OUTPUT_TOKENS[q][s]
    text_tokens = max(1, len(prompt) // 4)
    # Each reference image runs ~300-1000 input tokens; use a flat midpoint.
    img_in_tokens = n_refs * 600
    return (
        out_tokens * price["image_output"]
        + text_tokens * price["text_input"]
        + img_in_tokens * price["image_input"]
    ) / 1_000_000


def actual_cost(model: str, usage) -> float | None:
    """USD from the API usage object, if the provider returned one."""
    if usage is None:
        return None
    price = PRICING.get(model, PRICING[DEFAULT_MODEL])
    text_in = getattr(usage, "input_tokens", 0) or 0
    out = getattr(usage, "output_tokens", 0) or 0
    # gpt-image splits input into text + image; if the breakdown is present use it.
    details = getattr(usage, "input_tokens_details", None)
    img_in = getattr(details, "image_tokens", 0) if details else 0
    text_only = max(0, text_in - img_in)
    return (
        text_only * price["text_input"]
        + img_in * price["image_input"]
        + out * price["image_output"]
    ) / 1_000_000


# --- Provider: OpenAI gpt-image ---------------------------------------------


def generate_openai(client, *, model, prompt, size, quality, ref_paths, input_fidelity):
    """Return (png_bytes, usage). Uses the edits endpoint when refs are given."""
    kwargs = dict(model=model, prompt=prompt, size=size, quality=quality, n=1)
    if ref_paths:
        if model in MODELS_WITH_INPUT_FIDELITY:
            kwargs["input_fidelity"] = input_fidelity
        handles = [open(p, "rb") for p in ref_paths]
        try:
            result = client.images.edit(image=handles, **kwargs)
        finally:
            for h in handles:
                h.close()
    else:
        result = client.images.generate(**kwargs)
    b64 = result.data[0].b64_json
    if not b64:
        raise RuntimeError("provider returned no image data")
    return base64.b64decode(b64), getattr(result, "usage", None)


def generate_flux(*_args, **_kwargs):
    raise NotImplementedError(
        "flux provider is a stub. Planned: local Flux + 'Three' character LoRA "
        "for locked consistency once channel revenue funds the GPU time "
        "(see schema.md 'Swapping providers')."
    )


PROVIDERS = {"openai": generate_openai, "flux": generate_flux}


# --- Driver -----------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Batch scene-image generator (3SK video factory).")
    p.add_argument("manifest", help="Path to the image manifest JSON.")
    p.add_argument("--provider", choices=VALID_PROVIDERS, help="Override manifest/default provider.")
    p.add_argument("--model", help="Override the model id (config value).")
    p.add_argument("--quality", choices=VALID_QUALITIES, help="Override quality for the whole batch.")
    p.add_argument("--size", choices=VALID_SIZES, help="Override size for the whole batch.")
    p.add_argument("--input-fidelity", choices=VALID_FIDELITY, help="Reference fidelity (edits endpoint). 'high' holds the character.")
    p.add_argument("--output", help="Override the manifest's output_dir.")
    p.add_argument("--limit", type=int, help="Generate at most N images (smoke test).")
    p.add_argument("--force", action="store_true", help="Re-render images whose PNG already exists.")
    p.add_argument("--dry-run", action="store_true", help="Plan + cost estimate; no API calls, no writes.")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    script_dir = Path(__file__).resolve().parent

    manifest_path = Path(args.manifest)
    if not manifest_path.exists():
        die(f"manifest not found: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text())
    except json.JSONDecodeError as e:
        die(f"manifest is not valid JSON: {e}")

    provider = args.provider or manifest.get("provider", DEFAULT_PROVIDER)
    if provider not in VALID_PROVIDERS:
        die(f"unknown provider: {provider}")

    defaults = manifest.get("defaults", {})
    model = args.model or defaults.get("model", DEFAULT_MODEL)
    b_quality = args.quality or defaults.get("quality", DEFAULT_QUALITY)
    b_size = args.size or defaults.get("size", DEFAULT_SIZE)
    b_fidelity = args.input_fidelity or defaults.get("input_fidelity", DEFAULT_INPUT_FIDELITY)
    fidelity_active = model in MODELS_WITH_INPUT_FIDELITY
    if b_fidelity == "high" and not fidelity_active:
        print(f"note: {model} does not support input_fidelity — references will be "
              f"loose hints, expect character drift (gpt-image-1 holds it).", file=sys.stderr)

    images = manifest.get("images")
    if not images:
        die("manifest has no `images`")

    out_dir = expand(args.output or manifest.get("output_dir") or str(manifest_path.parent))
    preamble = (manifest.get("style_preamble") or "").strip()

    # Resolve reference images once (relative to reference_dir, else manifest dir).
    ref_dir = manifest.get("reference_dir")
    ref_base = expand(ref_dir) if ref_dir else manifest_path.parent
    ref_paths: list[Path] = []
    for r in manifest.get("reference_images", []):
        rp = Path(r)
        rp = rp if rp.is_absolute() else ref_base / r
        if not rp.exists():
            die(f"reference image not found: {rp}")
        ref_paths.append(rp)

    if args.limit is not None:
        images = images[: args.limit]

    print(f"manifest : {manifest_path.name}  ({manifest.get('project', 'untitled')})")
    print(f"provider : {provider}   model: {model}   quality: {b_quality}   size: {b_size}   fidelity: {b_fidelity}")
    print(f"output   : {out_dir}")
    print(f"refs     : {len(ref_paths)} reference image(s)")
    print(f"images   : {len(images)}{'  (DRY RUN)' if args.dry_run else ''}\n")

    client = None
    if not args.dry_run and provider == "openai":
        key = load_env_key(script_dir)
        if not key:
            die("OPENAI_API_KEY not set (export it or add it to image_factory/.env)")
        try:
            from openai import OpenAI
        except ImportError:
            die("the `openai` package is not installed — run: pip install -r requirements.txt")
        client = OpenAI(api_key=key)

    if not args.dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)

    gen = PROVIDERS[provider]
    est_total = 0.0
    actual_total = 0.0
    saw_usage = False
    made = skipped = failed = 0

    for img in images:
        name = img.get("name")
        prompt = img.get("prompt")
        if not name or not prompt:
            print(f"  skip (missing name/prompt): {img}")
            failed += 1
            continue
        if "/" in name or "\\" in name or name.startswith("."):
            print(f"  skip (unsafe name): {name!r}", file=sys.stderr)
            failed += 1
            continue

        quality = img.get("quality", b_quality)
        size = img.get("size", b_size)
        use_refs = img.get("use_references", True) and bool(ref_paths)
        these_refs = ref_paths if use_refs else []
        full_prompt = f"{preamble}\n\n{prompt}" if preamble else prompt

        dest = out_dir / f"{name}.png"
        est = estimate_cost(model, quality, size, full_prompt, len(these_refs))
        est_total += est

        if dest.exists() and not args.force and not args.dry_run:
            print(f"  = {name}  (exists, skipped)")
            skipped += 1
            continue

        ref_tag = f"/{len(these_refs)}ref:{b_fidelity}" if fidelity_active else f"/{len(these_refs)}ref"
        tag = f"{quality}/{size}" + (ref_tag if these_refs else "/no-ref")
        if args.dry_run:
            print(f"  + {name}  [{tag}]  ~${est:.3f}")
            continue

        try:
            png, usage = gen(
                client,
                model=model,
                prompt=full_prompt,
                size=size,
                quality=quality,
                ref_paths=these_refs,
                input_fidelity=b_fidelity,
            )
            fd, tmp = tempfile.mkstemp(suffix=".png", dir=out_dir)
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(png)
                os.replace(tmp, dest)
            except BaseException:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise
            real = actual_cost(model, usage)
            if real is not None:
                actual_total += real
                saw_usage = True
            cost_str = f"${real:.3f}" if real is not None else f"~${est:.3f}"
            print(f"  + {name}  [{tag}]  {cost_str}  -> {dest.name}")
            made += 1
        except NotImplementedError as e:
            die(str(e))
        except Exception as e:  # one bad image must not kill the batch
            print(f"  ! {name}  FAILED: {e}", file=sys.stderr)
            failed += 1

    print()
    if args.dry_run:
        print(f"dry run: {len(images)} image(s), estimated ~${est_total:.2f} total")
    else:
        line = f"done: {made} generated, {skipped} skipped, {failed} failed"
        if saw_usage:
            line += f"  |  billed ~${actual_total:.2f}"
        print(line)
        if failed:
            sys.exit(1)


if __name__ == "__main__":
    main()
