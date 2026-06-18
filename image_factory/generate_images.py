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
import re
import sys
import tempfile
from pathlib import Path

# --- Config values (deliberately swappable) ---------------------------------

VALID_PROVIDERS = ("openai", "flux")
VALID_QUALITIES = ("low", "medium", "high", "auto")
# gpt-image `background` control. "transparent" emits a real alpha channel (PNG
# only — which is what this script always writes); "opaque"/"auto" are the
# normal solid-background modes. Per-image or default-level; omitted == provider
# default (auto). Only gpt-image models accept it.
VALID_BACKGROUNDS = ("transparent", "opaque", "auto")
# gpt-image-1's fixed menu (it accepts ONLY these). gpt-image-2 is far more
# flexible — see GPT2_SIZE_CONSTRAINTS + validate_size().
GPT1_SIZES = ("1024x1024", "1536x1024", "1024x1536", "auto")
# gpt-image-2 accepts any WIDTHxHEIGHT meeting these (OpenAI image-gen guide,
# verified 2026-06-16): both edges multiples of 16, max edge <=3840, total
# pixels in [655360, 8294400], long:short aspect ratio <=3:1. This is what
# unlocks native 16:9 (e.g. 2048x1152 = exact 16:9, 3840x2160 = 4K 16:9) so a
# 16:9 video frame fills edge-to-edge with zero crop.
GPT2_SIZE_CONSTRAINTS = dict(mult=16, max_edge=3840, min_px=655_360,
                             max_px=8_294_400, max_ratio=3.0)
VALID_FIDELITY = ("low", "high")

DEFAULT_PROVIDER = "openai"
DEFAULT_MODEL = "gpt-image-2"  # NOT gpt-image-1 (deprecated 2026-06-02, sunset 2026-12-01)
DEFAULT_QUALITY = "medium"
DEFAULT_SIZE = "2048x1152"  # native 16:9 (2K) — fills the 1920x1080 render with
                            # NO crop. (Was 1536x1024 3:2, which cover-cropped
                            # ~16% off top/bottom — exactly where on-image labels
                            # sit. gpt-image-2 supports true 16:9; gpt-image-1
                            # does not, so a gpt-image-1 manifest must override
                            # size back to one of GPT1_SIZES.)
# Reference fidelity for the edits endpoint. Only meaningful for gpt-image-1,
# where "high" preserves the character's face/features/style from the reference
# PNGs far more strictly than the "low" default. Ignored by the no-reference
# generate path.
DEFAULT_INPUT_FIDELITY = "high"
# input_fidelity is a gpt-image-1-only parameter. gpt-image-2 does NOT accept it
# because it already processes every image input at high fidelity automatically
# (OpenAI image-gen guide, verified 2026-06-16) — so gpt-image-2 holds the
# character from references WITHOUT this flag. (Supersedes the earlier
# "gpt-image-2 drifts / 400s on input_fidelity" note: the param is simply
# omitted; the V1 HD set generated on gpt-image-2 graded on-model.) gpt-image-1
# sunsets 2026-12-01; the permanent answer is the local Flux + LoRA provider.
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


def parse_size(size: str):
    """(w, h) for a 'WIDTHxHEIGHT' string, or None for 'auto'/unparseable."""
    if size == "auto":
        return None
    m = re.fullmatch(r"(\d+)x(\d+)", size.strip())
    return (int(m.group(1)), int(m.group(2))) if m else None


def validate_size(model: str, size: str) -> None:
    """Reject a size the chosen model can't render — fail BEFORE any spend."""
    if size == "auto":
        return
    wh = parse_size(size)
    if wh is None:
        die(f"size must be 'WIDTHxHEIGHT' or 'auto', got {size!r}")
    w, h = wh
    if model == "gpt-image-1":            # fixed menu, no custom sizes
        if size not in GPT1_SIZES:
            die(f"gpt-image-1 only supports {GPT1_SIZES}; got {size!r} "
                f"— use gpt-image-2 for arbitrary / native-16:9 sizes")
        return
    c = GPT2_SIZE_CONSTRAINTS             # gpt-image-2: arbitrary within bounds
    if w % c["mult"] or h % c["mult"]:
        die(f"{model}: {size} — both edges must be multiples of {c['mult']}")
    if max(w, h) > c["max_edge"]:
        die(f"{model}: {size} — max edge is {c['max_edge']}px")
    px = w * h
    if not (c["min_px"] <= px <= c["max_px"]):
        die(f"{model}: {size} — total pixels {px:,} outside "
            f"[{c['min_px']:,}, {c['max_px']:,}]")
    if max(w, h) / min(w, h) > c["max_ratio"] + 1e-9:
        die(f"{model}: {size} — aspect ratio exceeds {c['max_ratio']:g}:1")


def estimate_cost(model: str, quality: str, size: str, prompt: str, n_refs: int) -> float:
    """Rough USD estimate for one image — dry-run planning only."""
    price = PRICING.get(model, PRICING[DEFAULT_MODEL])
    q = quality if quality in OUTPUT_TOKENS else "medium"
    # Exact table for the known presets; otherwise area-scale the output-token
    # count from the 1536x1024 anchor (this is a planning estimate only — the
    # real cost is read back from the API `usage` field after each live call).
    if size in OUTPUT_TOKENS[q]:
        out_tokens = OUTPUT_TOKENS[q][size]
    else:
        wh = parse_size(size)
        px = wh[0] * wh[1] if wh else 1536 * 1024
        out_tokens = round(px * OUTPUT_TOKENS[q]["1536x1024"] / (1536 * 1024))
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


def generate_openai(client, *, model, prompt, size, quality, ref_paths, input_fidelity,
                    background=None):
    """Return (png_bytes, usage). Uses the edits endpoint when refs are given."""
    kwargs = dict(model=model, prompt=prompt, size=size, quality=quality, n=1)
    if background and background != "auto":
        kwargs["background"] = background
        # Transparency lives in the alpha channel, which only PNG/WebP carry.
        # The API's output_format default is not contractually PNG, so a
        # "transparent" request can silently round-trip to opaque JPEG. Force
        # PNG (this script always writes .png) so the alpha actually survives.
        if background == "transparent":
            kwargs["output_format"] = "png"
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
    png = base64.b64decode(b64)
    if background == "transparent" and png[:8] != b"\x89PNG\r\n\x1a\n":
        raise RuntimeError("requested transparent background but provider did not "
                           "return PNG bytes — alpha channel would be lost")
    return png, getattr(result, "usage", None)


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
    p.add_argument("--size", help="Override size for the whole batch ('WIDTHxHEIGHT' or "
                   "'auto'). gpt-image-2 takes any 16-divisible size up to 3840px/edge, "
                   "ratio <=3:1 (e.g. 2048x1152 for native 16:9); gpt-image-1 is limited "
                   f"to {GPT1_SIZES}.")
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
    b_background = defaults.get("background")
    if b_background is not None and b_background not in VALID_BACKGROUNDS:
        die(f"background must be one of {VALID_BACKGROUNDS}; got {b_background!r}")
    validate_size(model, b_size)  # batch size — fail before any spend
    fidelity_active = model in MODELS_WITH_INPUT_FIDELITY
    if b_fidelity == "high" and not fidelity_active:
        print(f"note: {model} ignores input_fidelity — it already processes every "
              f"reference image at high fidelity automatically, so the character "
              f"holds without the flag.", file=sys.stderr)

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

    # Pre-validate every per-image size override BEFORE any spend. validate_size()
    # calls die()→sys.exit, so doing this inside the generation loop would abort the
    # run after earlier images were already billed (H1). Fail fast here instead.
    for _img in images:
        if isinstance(_img, dict) and _img.get("size") and _img["size"] != b_size:
            validate_size(model, str(_img["size"]))
        if isinstance(_img, dict) and _img.get("background") is not None \
                and _img["background"] not in VALID_BACKGROUNDS:
            die(f"background must be one of {VALID_BACKGROUNDS}; "
                f"got {_img['background']!r}")

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
        # Coerce to safe types before any membership/format check: a hand-authored
        # manifest can carry a non-str name (e.g. JSON number) or prompt, and
        # `"/" in <int>` would TypeError and abort the whole batch mid-spend (H1).
        name = str(img.get("name") or "")
        prompt = img.get("prompt")
        if not name or not prompt or not isinstance(prompt, str):
            print(f"  skip (missing/invalid name or prompt): {img}", file=sys.stderr)
            failed += 1
            continue
        if "/" in name or "\\" in name or name.startswith("."):
            print(f"  skip (unsafe name): {name!r}", file=sys.stderr)
            failed += 1
            continue

        quality = img.get("quality", b_quality)
        size = img.get("size", b_size)  # per-image overrides already validated pre-loop
        background = img.get("background", b_background)  # validated pre-loop
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
        if background and background != "auto":
            tag += f"/bg:{background}"
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
                background=background,
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
