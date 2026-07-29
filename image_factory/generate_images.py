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
import collections
import difflib
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# Append-only spend ledger (realtime, one JSON line per billed render). Lives
# next to this script and is gitignored — it is local spend data, not code.
LEDGER_DEFAULT = Path(__file__).resolve().parent / "cost_ledger.jsonl"

# DQ-40 (2026-07-24, Steve set $10): per-VIDEO cumulative image-spend policy cap.
# Distinct from --max-cost, which is only a per-RUN plausibility guard the caller
# legitimately raises for big flagships — which is exactly how 3 videos crossed
# the old $8 policy number with zero signal. Crossing THIS cap blocks the run
# unless --over-cap-ok is passed, and pings Telegram either way, so a policy
# overage is never silent. Accepted residual: the gate compares prior ACTUALS +
# this run's ESTIMATE, so a single batch can finish over cap by the estimate
# error (measured: cents on the with-refs flagship path) — the next run sees
# the true total. LOCKED VALUE: changing it requires a Decisions_Log why-stub
# in the same session (CLAUDE.md rule, 2026-07-08).
POLICY_CAP_USD = 9.0
# Reprint budget, held back from the cap above (Steve, 2026-07-27). The FIRST batch
# for a video may spend up to POLICY_CAP_USD; any later run — a regen after the
# contact-sheet review, a fixup, a thumbnail redo — is a REPRINT and may add at most
# REPRINT_CAP_USD, with the absolute per-video ceiling at the sum ($10).
# WHY the split: a single $10 cap could be consumed entirely by pass 1, leaving zero
# budget to fix the shots that review then rejects — the money ran out exactly when
# it was needed most. V14 surfaced it: a 102-shot first batch estimated $12.65, and
# even trimmed to fit $10 it would have left nothing for re-renders. Reserving $1
# makes the fix pass affordable by construction rather than by luck.
# LOCKED VALUE: changing either figure requires a Decisions_Log why-stub in the same
# session (CLAUDE.md rule, 2026-07-08).
REPRINT_CAP_USD = 1.0

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


def preflight_interpreter(provider: str, dry_run: bool) -> None:
    """Fail FAST if THIS interpreter cannot actually render — before any planning.

    WHY (2026-07-27): run under a python without the `openai` package, this script
    printed the whole manifest header, the plan, and `spend guard: OK — 71 image(s)
    to render` before dying at client-init. Everything up to that point reads like a
    successful run, and the failure is one line of stderr under ~80 lines of stdout,
    so two consecutive V14 attempts were logged as "rendered" when nothing had been
    billed at all. The dependency is knowable in the first millisecond; checking it
    at the end is what made a hard failure look like success.

    Names the exact interpreter to re-run with, because the repo venv is the only one
    that carries the SDK and the system python3 is what a habit reaches for."""
    if dry_run or provider != "openai":
        return                          # neither path constructs an OpenAI client
    try:
        import openai  # noqa: F401
    except ImportError:
        venv = Path(__file__).resolve().parent.parent / ".venv" / "bin" / "python"
        if venv.exists():
            cmd = " ".join([str(venv), *sys.argv])
            die(f"this interpreter ({sys.executable}) has no `openai` package, so "
                f"nothing could be rendered. The repo venv has it — re-run:\n\n"
                f"  {cmd}\n")
        die("the `openai` package is not installed for this interpreter "
            f"({sys.executable}) — run: pip install -r requirements.txt")


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


# --- Cost ledger (realtime spend + re-render tracking) ----------------------


def video_label(manifest_name: str) -> str:
    """Stable per-video key for the ledger ('Video_05' from any V5 manifest).

    Case-insensitive + zero-padded (DQ-40, 2026-07-24): the old case-sensitive
    'Video_\\d+' never matched the actual lowercase manifest names, so one video's
    spend fragmented across keys ('video_13_hd', 'video_13_fixup_renders', …) and
    no cumulative per-video math was possible."""
    m = re.search(r"video[_\s-]?(\d+)", manifest_name, re.IGNORECASE)
    return f"Video_{int(m.group(1)):02d}" if m else Path(manifest_name).stem


def canonical_video(rec: dict) -> str:
    """Canonical 'Video_NN' for an EXISTING ledger row, tolerating the pre-fix
    fragmented keys: derive from the video field, else from the shot name
    ('Video_13_Shot_17b' → 'Video_13'), else pass the raw key through."""
    for cand in (rec.get("video"), rec.get("shot")):
        m = re.search(r"video[_\s-]?(\d+)", str(cand or ""), re.IGNORECASE)
        if m:
            return f"Video_{int(m.group(1)):02d}"
    return str(rec.get("video") or "?")


def policy_gate_key(image_name: str, manifest_video: str) -> str:
    """Which video's policy cap an image bills against: its OWN name's video
    index when it has one (so a multi-video batch manifest like thumb_pop_batch
    gates each video separately instead of bypassing the cap under the batch
    key), else the manifest's video."""
    m = re.search(r"video[_\s-]?(\d+)", image_name, re.IGNORECASE)
    return f"Video_{int(m.group(1)):02d}" if m else manifest_video


def load_render_counts(ledger_path: Path, video: str) -> dict:
    """shot -> times already billed for THIS video, so a repeat render is flagged."""
    counts: dict = {}
    if not ledger_path.exists():
        return counts
    for line in ledger_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue  # ponytail: tolerate a torn last line, never crash the batch on it
        # canonical_video, not raw rec['video']: a fixup/regen manifest's rows must
        # count as re-renders of the SAME video (pre-fix keys fragmented this).
        if canonical_video(rec) == video and rec.get("shot"):
            counts[rec["shot"]] = counts.get(rec["shot"], 0) + 1
    return counts


def load_video_spend(ledger_path: Path, video: str) -> float:
    """Total billed USD already on the ledger for THIS video, across ALL prior
    runs/manifests (hd batch, fixups, regens). Feeds the DQ-40 policy-cap gate."""
    total = 0.0
    if not ledger_path.exists():
        return total
    for line in ledger_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue  # tolerate a torn last line, same as load_render_counts
        if canonical_video(rec) == video and isinstance(rec.get("cost_usd"), (int, float)):
            total += rec["cost_usd"]
    return total


def load_video_batches(ledger_path: Path, video: str) -> dict:
    """{manifest: billed USD} for THIS video — per-BATCH totals, not one sum.

    The cap gate needs to know whether the video's real first batch has already
    happened, and a bare total cannot answer that: $0.10 of smoke test and $8.80
    of flagship look identical once summed.
    """
    batches: dict = {}
    if not ledger_path.exists():
        return batches
    for line in ledger_path.read_text().splitlines():
        line = line.strip()
        # mutequiv: if-test->False here is unobservable — an unskipped blank line
        # reaches json.loads(""), which raises JSONDecodeError, which the except
        # below already catches and continues on, landing on the identical result.
        # PROVEN by running both paths over ['', '  ', <real row>]: both yield
        # {'m': 1.0}. This is a fast path, not dead code, and it is the same idiom
        # four sibling loaders in this file use (:346, :367, :460) — so it stays.
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue  # tolerate a torn last line, same as load_video_spend
        if canonical_video(rec) == video and isinstance(rec.get("cost_usd"), (int, float)):
            key = rec.get("manifest") or "(unknown)"
            batches[key] = batches.get(key, 0.0) + rec["cost_usd"]
    return batches


def first_batch_done(ledger_path: Path, video: str) -> bool:
    """Has this video already had its REAL first batch?

    True iff some prior manifest billed MORE than the reprint cap. Anything at or
    under it (a --limit 1 smoke test, a single card re-render) is by definition
    not the bulk batch.

    Why not `prior_spend > 0` — the old test (2026-07-29). The `--limit 1` smoke
    test documented in this file's own usage costs ~$0.10, and that alone flipped
    the video into REPRINT mode, so the real flagship batch was then refused:
        prior $0.10 + add $8.80 = $8.90, blocked as "reprint adds $8.80 > $1.00"
    — $1.10 UNDER the $10 ceiling it was supposedly protecting. Six videos were
    already stuck that way. The only escape was --over-cap-ok, which pings
    "⚠️ over cap, proceeding" for a run that is not over cap, training the
    operator to treat the one flag that disables the real ceiling as routine.

    This restores the semantics the decision doc always stated ("First batch:
    video has $0 prior ledger spend ... bounded by POLICY_CAP_USD alone") without
    the smoke test counting as spend. The absolute ceiling is untouched:
    POLICY_CAP_USD + REPRINT_CAP_USD.
    """
    return any(v > REPRINT_CAP_USD for v in load_video_batches(ledger_path, video).values())


def notify(msg: str) -> None:
    """Best-effort Telegram ping via scripts/notify.sh. Never blocks or raises.
    IRIS_NOTIFY_DISABLE=1 silences it (tests)."""
    if os.environ.get("IRIS_NOTIFY_DISABLE"):
        return
    ns = Path(__file__).resolve().parent.parent / "scripts" / "notify.sh"
    if not os.access(ns, os.X_OK):
        return
    try:
        import subprocess
        subprocess.run([str(ns), msg], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=30)  # > notify.sh's curl --max-time 20
    except Exception:
        pass


def ledger_append(ledger_path: Path, record: dict) -> None:
    """Append one render record NOW. fsync so a mid-batch crash still leaves the row."""
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with open(ledger_path, "a") as fh:
        fh.write(json.dumps(record) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def print_report(ledger_path: Path) -> None:
    """Per-video + grand-total spend and re-render counts from the ledger."""
    if not ledger_path.exists():
        print(f"no ledger yet at {ledger_path} — it fills as live renders run.")
        return
    agg: dict = {}
    grand = {"renders": 0, "rerenders": 0, "cost": 0.0, "dup_cost": 0.0, "unknown": 0, "unsaved": 0}
    for line in ledger_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        v = canonical_video(rec)  # merge pre-fix fragmented keys per video
        a = agg.setdefault(v, {"renders": 0, "rerenders": 0, "cost": 0.0, "dup_cost": 0.0, "unknown": 0, "unsaved": 0})
        cost = rec.get("cost_usd")
        rr = bool(rec.get("rerender"))
        # `saved` is absent on pre-fix rows → treat missing as saved (no false alarm).
        unsaved = rec.get("saved") is False
        for tgt in (a, grand):
            tgt["renders"] += 1
            if rr:
                tgt["rerenders"] += 1
            if unsaved:
                tgt["unsaved"] += 1
            if cost is None:
                tgt["unknown"] += 1
            else:
                tgt["cost"] += cost
                if rr:
                    tgt["dup_cost"] += cost
    hdr = f"{'video':<14}{'renders':>9}{'re-renders':>12}{'spend':>11}{'dup spend':>11}"
    print(f"cost ledger: {ledger_path}\n")
    print(hdr)
    print("-" * len(hdr))
    for v in sorted(agg):
        a = agg[v]
        print(f"{v:<14}{a['renders']:>9}{a['rerenders']:>12}"
              f"{'$' + format(a['cost'], '.2f'):>11}{'$' + format(a['dup_cost'], '.2f'):>11}")
    print("-" * len(hdr))
    print(f"{'TOTAL':<14}{grand['renders']:>9}{grand['rerenders']:>12}"
          f"{'$' + format(grand['cost'], '.2f'):>11}{'$' + format(grand['dup_cost'], '.2f'):>11}")
    if grand["unknown"]:
        print(f"\nnote: {grand['unknown']} render(s) returned no API usage — billed but "
              f"cost unknown, excluded from the $ totals.")
    if grand["unsaved"]:
        print(f"note: {grand['unsaved']} render(s) billed but the PNG did not save "
              f"(re-render needed, not duplicate spend).")


# --- Driver -----------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Batch scene-image generator (3SK video factory).")
    p.add_argument("manifest", nargs="?", help="Path to the image manifest JSON (omit with --report).")
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
    p.add_argument("--only", action="append", metavar="NAME[,NAME...]",
                   help="Render ONLY these shot names (repeatable, or comma-separated). "
                        "For a RENDERS-gate re-roll of specific failed shots. An "
                        "unmatched name is a fatal error, never a silent no-op. "
                        "Note: existing PNGs are skipped by default, so a re-roll "
                        "also needs --force (or delete those PNGs first).")
    p.add_argument("--force", action="store_true", help="Re-render images whose PNG already exists.")
    p.add_argument("--dry-run", action="store_true", help="Plan + cost estimate; no API calls, no writes.")
    # Runaway-spend guardrail. A real flagship batch is ~40-80 shots (~$8-9). A
    # malformed/duplicated manifest — or pointing the runner at the wrong file —
    # could silently bill hundreds. These caps abort BEFORE any API call when the
    # batch is implausibly large; raise them for a deliberate big run.
    p.add_argument("--max-images", type=int, default=150,
                   help="Refuse to bill more than N images in one run (spend guard; default 150).")
    p.add_argument("--max-cost", type=float, default=30.0,
                   help="Refuse to run if the estimated batch cost exceeds $N (spend guard; default 30).")
    p.add_argument("--ledger", help="Cost-ledger JSONL path (default: image_factory/cost_ledger.jsonl).")
    p.add_argument("--over-cap-ok", action="store_true",
                   help=f"Explicit per-video override: proceed past the "
                        f"${POLICY_CAP_USD:.0f}/video cumulative policy cap (DQ-40). "
                        f"Requires Steve's overage ok; the override itself pings Telegram.")
    p.add_argument("--report", action="store_true",
                   help="Print spend + re-render summary from the ledger and exit (no manifest needed).")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    script_dir = Path(__file__).resolve().parent
    ledger_path = Path(os.path.expanduser(args.ledger)) if args.ledger else LEDGER_DEFAULT

    if args.report:
        print_report(ledger_path)
        return
    if not args.manifest:
        die("manifest is required (or pass --report to summarize the ledger)")

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
    # FIRST thing after the provider is known, before any planning output: a run
    # that cannot possibly render must say so now, not after printing a plan and
    # a green spend guard. See preflight_interpreter's docstring.
    preflight_interpreter(provider, args.dry_run)

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

    # --only: restrict the batch to named shots (RENDERS-gate re-roll). Applied
    # HERE, once, before validation / cost estimate / spend guard / the billed
    # loop, so the dry-run estimate and the actual spend cannot diverge — on the
    # billed path that divergence would be a lying cost estimate under the $8 cap.
    #
    # An unmatched name is FATAL, never a silent no-op: the failure mode we
    # refuse is a typo'd shot quietly rendering nothing (Steve reads "done: 0
    # generated" as success and ships a stale PNG) or, worse, falling through to
    # the full batch and re-paying for shots we already own — which is the exact
    # spend DQ-33 exists to stop.
    if args.only:
        if args.limit is not None:
            # --limit slices by MANIFEST order, so it would silently bill a
            # re-roll of a shot you did not ask for (--only B,A --limit 1
            # renders A). Both are "render less"; requiring one is unambiguous.
            die("--only and --limit cannot be combined: --limit slices by manifest "
                "order, so it would silently drop or substitute shots you named. "
                "Use --only alone to pick exact shots.")
        wanted = [n.strip() for spec in args.only for n in spec.split(",") if n.strip()]
        if not wanted:
            # An empty/whitespace/comma-only value (classic unset `--only "$SHOTS"`)
            # must NOT fall through to "0 rendered, exit 0" — that reads as success
            # and ships stale PNGs, the exact failure this flag exists to prevent.
            die("--only: no shot names given (empty value). Omit --only to render "
                "the whole manifest.")
        have = [str(i.get("name") or "") for i in images if isinstance(i, dict)]
        have_set = set(have)
        missing = [n for n in wanted if n not in have_set]
        if missing:
            # Suggest for the first few missing names, not just the first — with
            # one typo among several, suggestions that silently cover only one of
            # them read as "the rest are fine".
            near = []
            for _m in missing[:3]:
                for _c in difflib.get_close_matches(_m, sorted(have_set), n=3, cutoff=0.5):
                    if _c not in near:
                        near.append(_c)
            if not near:
                near = sorted(h for h in have_set if any(n.lower() in h.lower() for n in missing))
            shown = ", ".join(missing[:10]) + (f" (+{len(missing) - 10} more)" if len(missing) > 10 else "")
            die(f"--only: shot(s) not in this manifest: {shown}"
                + (f"\n  did you mean: {', '.join(near[:8])}" if near else "")
                + f"\n  manifest has {len(have_set)} shot(s).")
        # Duplicate names are real in this corpus (Video_01_orchestrated.json has
        # 6), and they are NOT copy-paste dupes — the paired entries carry
        # DIFFERENT prompts (e.g. Shot_04c is both a dining-table scene and a
        # split-screen). A full batch renders both to the same dest, so os.replace
        # leaves the LAST entry on disk and the earlier render is money burned.
        #
        # Therefore --only must keep the LAST entry per name: that is the image
        # actually on disk, and a re-roll exists to replace what is on disk.
        # Keeping the FIRST would spend ~$0.13 to silently swap a shipped,
        # reviewed frame for a different scene, and report success doing it.
        keep, dropped = set(wanted), 0
        by_name = {}
        for i in images:
            if not isinstance(i, dict):
                continue
            nm = str(i.get("name") or "")
            if nm not in keep:
                continue
            if nm in by_name:
                dropped += 1
            by_name[nm] = i          # last wins, matching the full batch
        images = list(by_name.values())
        if dropped:
            print(f"--only  : WARNING — {dropped} duplicate manifest entr(ies) for the "
                  f"named shot(s); using the LAST of each (the one a full batch leaves "
                  f"on disk). Rename them in the manifest.")
        print(f"--only  : {len(images)} of {len(have_set)} shot(s) — {', '.join(wanted)}\n")

    _dups = sorted(n for n, c in collections.Counter(
        str(i.get("name") or "") for i in images if isinstance(i, dict)).items() if c > 1)
    if _dups:
        print(f"WARNING: duplicate shot name(s) in this batch: {', '.join(_dups[:8])}"
              f"{f' (+{len(_dups) - 8} more)' if len(_dups) > 8 else ''}\n"
              f"  Each duplicate BILLS TWICE and the later render overwrites the earlier "
              f"PNG. Rename them in the manifest.\n", file=sys.stderr)

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

    # --- Spend guardrail (pre-flight) ---------------------------------------
    # Count ONLY the images that would actually bill this run (skip those whose
    # PNG already exists unless --force), and sum their estimated cost. If either
    # the count or the estimate is implausibly large, abort before spending a cent.
    # Conservative by design: a real flagship (~76 shots, ~$9) clears comfortably;
    # a duplicated/wrong manifest (hundreds of shots / tens of dollars) is blocked.
    _mvideo = video_label(manifest_path.name)
    would_render, pre_est = 0, 0.0
    gate_adds: dict = {}   # policy-cap gate key -> this run's estimated add
    for _img in images:
        if not isinstance(_img, dict):
            continue
        _name = str(_img.get("name") or "")
        _prompt = _img.get("prompt")
        if not _name or not isinstance(_prompt, str) or not _prompt:
            continue
        if "/" in _name or "\\" in _name or _name.startswith("."):
            continue
        if (out_dir / f"{_name}.png").exists() and not args.force:
            continue  # would be skipped → bills nothing
        _q = _img.get("quality", b_quality)
        _s = _img.get("size", b_size)
        _full = f"{preamble}\n\n{_prompt}" if preamble else _prompt
        _refs = ref_paths if (_img.get("use_references", True) and ref_paths) else []
        would_render += 1
        _est = estimate_cost(model, _q, _s, _full, len(_refs))
        pre_est += _est
        _k = policy_gate_key(_name, _mvideo)
        gate_adds[_k] = gate_adds.get(_k, 0.0) + _est

    # DQ-40 policy-cap state, grouped by each image's OWN video index (falling
    # back to the manifest's video) — a multi-video batch manifest like
    # thumb_pop_batch must not bypass the per-video ceilings. Prior is ledger
    # ACTUALS; this run is the ESTIMATE, so one batch can finish over cap by the
    # estimate error (measured: cents on the with-refs flagship path); the next
    # run sees the true totals. A MISSING ledger legitimately sums to $0 — that
    # is a silent cap reset, so the live path prints a notice when it happens.
    gate_state = [(k, load_video_spend(ledger_path, k), gate_adds[k])
                  for k in sorted(gate_adds)]
    # A video with prior ledger spend is on a REPRINT run: the first batch is bounded
    # by POLICY_CAP_USD, later runs by REPRINT_CAP_USD each, and everything by the
    # absolute ceiling. Checked per-video, so a multi-video manifest cannot launder a
    # reprint through a fresh video's headroom.
    breaches = []
    for k, p, a in gate_state:
        # REPRINT is judged on whether the real first batch already happened, not
        # on `p > 0.0` — see first_batch_done(). A smoke test is not a first batch.
        _is_reprint = first_batch_done(ledger_path, k)
        if _is_reprint and a > REPRINT_CAP_USD:
            breaches.append((k, p, a, f"reprint run adds ~${a:.2f}, over the "
                                      f"${REPRINT_CAP_USD:.2f} reprint limit"))
        elif p + a > POLICY_CAP_USD + (REPRINT_CAP_USD if _is_reprint else 0.0):
            _ceil = POLICY_CAP_USD + (REPRINT_CAP_USD if _is_reprint else 0.0)
            breaches.append((k, p, a, f"would reach ${p + a:.2f}, over the "
                                      f"${_ceil:.2f} ceiling"))
    if not args.dry_run:
        if would_render > args.max_images:
            die(f"spend guard: {would_render} images would bill this run, over the "
                f"--max-images={args.max_images} cap. If this batch is intentional, "
                f"re-run with --max-images {would_render}. (A real flagship is ~40-80 "
                f"shots — a count this high usually means a duplicated or wrong manifest.)")
        if pre_est > args.max_cost:
            die(f"spend guard: estimated ${pre_est:.2f} for {would_render} image(s) "
                f"exceeds the --max-cost=${args.max_cost:.2f} cap. If intentional, "
                f"re-run with --max-cost {pre_est:.2f}.")
        # DQ-40 policy-cap gate: cumulative per-VIDEO spend vs the $-policy cap.
        # --max-cost above is per-RUN plausibility; THIS is the per-video ceiling
        # Steve owns. `breaches` is only ever non-empty when something would
        # actually bill (gate_adds fills per billing image), so a $0 no-op run —
        # every PNG already on disk — can never block or ping here, even for a
        # video already over cap.
        if would_render and not ledger_path.exists():
            print(f"policy cap: NOTE — no ledger at {ledger_path}; cumulative spend "
                  f"starts at $0. If this machine has billed before, the ledger "
                  f"moved or was deleted — check before a big spend.")
        if breaches:
            _det = "; ".join(f"{k}: {why} (${p:.2f} already billed + ~${a:.2f} this run)"
                             for k, p, a, why in breaches)
            _line = (f"policy cap: {_det} — caps are ${POLICY_CAP_USD:.2f}/video first "
                     f"batch + ${REPRINT_CAP_USD:.2f} reprint.")
            if not args.over_cap_ok:
                notify(f"🛑 {_line} BLOCKED — get Steve's explicit overage ok, then "
                       f"re-run with --over-cap-ok.")
                die(f"{_line} Get Steve's explicit ok for the overage, then re-run "
                    f"with --over-cap-ok. (An overage is never silent: this block "
                    f"and any override both ping Telegram.)")
            _msg = f"⚠️ {_line} Proceeding on --over-cap-ok (explicit override)."
            notify(_msg)
            print(_msg + "\n")
        if would_render:
            _wk, _wp, _wa = max(gate_state, key=lambda t: t[1] + t[2])
            print(f"spend guard: OK — {would_render} image(s) to render, "
                  f"~${pre_est:.2f} estimated (caps: {args.max_images} imgs / ${args.max_cost:.0f}; "
                  f"{_wk} cumulative ${_wp + _wa:.2f} of ${POLICY_CAP_USD:.0f}"
                  f"+${REPRINT_CAP_USD:.0f} policy).\n")
    else:
        # Dry-run: surface the policy-cap picture (the check-cost-first habit)
        # without blocking or pinging. Falls back to the manifest's video when
        # nothing would render, so the info line still appears.
        _rows = gate_state or [(_mvideo, load_video_spend(ledger_path, _mvideo), 0.0)]
        for _k, _p, _a in _rows:
            _isreprint = _p > 0.0
            _ceil = POLICY_CAP_USD + (REPRINT_CAP_USD if _isreprint else 0.0)
            _bad = (_a > REPRINT_CAP_USD) if _isreprint else False
            _over = " — OVER, a live run will need Steve's ok + --over-cap-ok" \
                if (_bad or _p + _a > _ceil) else ""
            _kind = f"REPRINT (limit ${REPRINT_CAP_USD:.2f}/run)" if _isreprint else "first batch"
            print(f"policy cap: {_k} ${_p:.2f} billed to date; this batch ~${_a:.2f} "
                  f"[{_kind}] → ${_p + _a:.2f} of ${_ceil:.0f}/video{_over}")
        print()

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
    reran = 0          # images billed this run whose shot was already in the ledger
    dup_cost = 0.0     # $ spent re-rendering those shots this run
    video = video_label(manifest_path.name)
    # Prior render counts for THIS video — so a shot rendered again is flagged a re-render.
    render_counts = load_render_counts(ledger_path, video) if not args.dry_run else {}

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

        # Dry-run honours skip-existing too. It used to ignore it, so the
        # estimate counted images the real run would skip — harmless when you
        # dry-ran a fresh batch, actively misleading now that --only exists,
        # since a re-roll targets shots whose PNGs already exist BY DEFINITION.
        # (The pre-flight spend guard always excluded them; only this display
        # path disagreed. Over-estimate, never a surprise bill — but a dry run
        # that says $0.25 for a run that bills $0.00 is a lying estimate, and
        # the $8 cap is enforced on the strength of these numbers.)
        if dest.exists() and not args.force:
            print(f"  = {name}  (exists, {'would skip — needs --force' if args.dry_run else 'skipped'})")
            skipped += 1
            continue

        # Counted only once the shot is known to actually bill, so the run total
        # can never exceed what the run spends (it fed the "estimated ~$X total"
        # summary that the $8/video cap is judged against).
        est_total += est

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
            # The money is spent the instant gen() returns. Record the ledger row
            # NOW — before the file save, which can still fail (disk full, perms)
            # or be cut short by a crash. Recording first means a billed image is
            # never lost from the spend log even if its PNG never lands; the
            # `saved` flag distinguishes "paid + on disk" from "paid, save failed".
            real = actual_cost(model, usage)
            if real is not None:
                actual_total += real
                saw_usage = True
            prior = render_counts.get(name, 0)
            render_counts[name] = prior + 1
            is_rerender = prior > 0
            if is_rerender:
                reran += 1
                if real is not None:
                    dup_cost += real
            saved = False
            try:
                fd, tmp = tempfile.mkstemp(suffix=".png", dir=out_dir)
                try:
                    with os.fdopen(fd, "wb") as fh:
                        fh.write(png)
                    os.replace(tmp, dest)
                    saved = True
                except BaseException:
                    if os.path.exists(tmp):
                        os.unlink(tmp)
                    raise
            finally:
                # Append in the finally so the spend is logged whether the save
                # succeeded or threw — the bill already happened either way.
                ledger_append(ledger_path, {
                    "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    # The shot's OWN video when its name carries one, so the row
                    # lands where the policy gate charged it (a cross-video batch
                    # image must not attribute to the batch manifest's video).
                    "video": policy_gate_key(name, video),
                    "manifest": manifest_path.name,
                    "shot": name,
                    "model": model,
                    "quality": quality,
                    "size": size,
                    "cost_usd": round(real, 4) if real is not None else None,
                    "render_count": prior + 1,
                    "rerender": is_rerender,
                    "saved": saved,
                })
            cost_str = f"${real:.3f}" if real is not None else f"~${est:.3f}"
            rr_tag = f"  [RE-RENDER #{prior + 1}]" if is_rerender else ""
            print(f"  + {name}  [{tag}]  {cost_str}  -> {dest.name}{rr_tag}")
            made += 1
        except NotImplementedError as e:
            die(str(e))
        except Exception as e:  # one bad image must not kill the batch
            # If gen() already returned, the bill happened and the finally above
            # logged it (saved=false); this path also covers a pre-bill API error.
            print(f"  ! {name}  FAILED (if it billed, it's logged in the ledger): {e}",
                  file=sys.stderr)
            failed += 1

    print()
    if args.dry_run:
        # Report what would BILL, not what was walked — "12 image(s), ~$0.00"
        # invites reading the count as the spend. Skipped shots are named above.
        print(f"dry run: {len(images) - skipped - failed} of {len(images)} image(s) would bill, "
              f"estimated ~${est_total:.2f} total"
              + (f" ({skipped} already exist — add --force to re-render)" if skipped else ""))
    else:
        line = f"done: {made} generated, {skipped} skipped, {failed} failed"
        if saw_usage:
            line += f"  |  billed ~${actual_total:.2f}"
        if reran:
            line += f"  |  {reran} re-render(s) (~${dup_cost:.2f} duplicate spend)"
        print(line)
        if made:
            print(f"ledger   : {ledger_path}  (run with --report for the running total)")
        if failed:
            sys.exit(1)


if __name__ == "__main__":
    main()
