#!/usr/bin/env python3
"""build_asset_index.py — deterministic catalog of already-generated 3SK Finance images.

Parses every REAL scene manifest under
`Raw_Assets/Image_Factory/manifests/*.json`, resolves each shot to its PNG on
disk, existence-checks it, and writes `asset_index.json`. Also ingests the 7
canonical Three character sheets from `Character_Reference/Reference_Manifest.md`.

Why this exists: ~140 PNGs already exist and every shot is already described in
text (each manifest carries a `name` + full `prompt`). Nothing indexed that
corpus, so upcoming videos paid to regenerate shots we may already own. This
script is the cheap, deterministic HALF of the reuse system — the `asset-librarian`
Haiku agent reads this index to suggest reuse candidates, instead of re-scanning
140 files per dispatch. Reuse is pure margin (saved API spend that compounds
across every future video + brand).

Filename convention (verified empirically against Video_01_HD/ + video_01_hd.json):
each manifest entry's `name` IS the full PNG stem, so the file resolves to
`<expanduser(output_dir)>/<name>.png`. No project/folder prefix is added.

Design constraints:
  - Stdlib only. No network. Read-only on all source manifests and PNGs.
  - Idempotent: same inputs -> same asset_index.json.
  - Atomic write (temp + os.replace) so a reader never sees a half-written index.
  - `exists` reflects the real filesystem so the librarian only ever suggests
    images actually on disk (some manifests reference not-yet-generated shots).
  - A malformed manifest is logged and skipped, never fatal.

Run on-demand or fold a call into an image-batch path to keep the index fresh:
  cd /Volumes/AI_Workspace/iris_studio && python3 scripts/build_asset_index.py
"""

from __future__ import annotations

import json
import os
import re
import struct
import sys
import tempfile
from pathlib import Path

VAULT = Path("/Users/steve/Documents/3SK/outputs")
IMAGE_FACTORY = VAULT / "BRANDS" / "3SK_Finance" / "Raw_Assets" / "Image_Factory"
MANIFEST_DIR = IMAGE_FACTORY / "manifests"
OUTPUT = IMAGE_FACTORY / "asset_index.json"

CHARACTER_REF_DIR = VAULT / "BRANDS" / "3SK_Finance" / "Character_Reference"
REFERENCE_MANIFEST = CHARACTER_REF_DIR / "Reference_Manifest.md"

# Manifests that are not real shot lists: backups, examples, templates.
def _is_real_manifest(path: Path) -> bool:
    name = path.name
    if name.endswith(".bak") or ".bak-" in name or ".bak." in name:
        return False
    if name.endswith(".example.json"):
        return False
    if name == "_TEMPLATE.json":
        return False
    return name.endswith(".json")


# Name tokens that mark a one-off text card / CTA / title overlay.
_CARD_TOKENS = ("card", "cta", "title")


def _name_is_card(name: str) -> bool:
    low = name.lower()
    return any(tok in low for tok in _CARD_TOKENS)


def _png_dimensions(path: Path) -> str | None:
    """Read a PNG's WxH from the IHDR chunk without any image library.

    PNG layout: 8-byte signature, then a length(4)+type(4) chunk header; the
    first chunk is IHDR whose data starts at byte 16 with width(4) then
    height(4), big-endian. Returns 'WxH' or None if unreadable / not a PNG.
    """
    try:
        with open(path, "rb") as fh:
            header = fh.read(24)
        if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
            return None
        if header[12:16] != b"IHDR":
            return None
        width, height = struct.unpack(">II", header[16:24])
        return f"{width}x{height}"
    except OSError:
        return None


def _resolve_output_dir(output_dir: str) -> Path:
    return Path(os.path.expanduser(output_dir))


def index_manifest(path: Path) -> list[dict]:
    """Return one record per images[] entry in a manifest. Never raises."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        sys.stderr.write(f"WARN  skipping unparseable manifest {path.name}: {exc}\n")
        return []
    if not isinstance(data, dict):
        sys.stderr.write(f"WARN  skipping non-object manifest {path.name}\n")
        return []

    images = data.get("images")
    if not isinstance(images, list) or not images:
        # e.g. video_02_overlay.json has no `images` array — not a shot list.
        return []

    output_dir = data.get("output_dir")
    if not output_dir or not isinstance(output_dir, str):
        sys.stderr.write(
            f"WARN  manifest {path.name} has images but no output_dir — skipping\n"
        )
        return []
    out_dir = _resolve_output_dir(output_dir)

    defaults = data.get("defaults") or {}
    default_size = defaults.get("size") if isinstance(defaults, dict) else None

    video = path.stem
    records = []
    for entry in images:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not name or not isinstance(name, str):
            sys.stderr.write(
                f"WARN  manifest {path.name} has an image entry with no name — skipping that entry\n"
            )
            continue
        use_references = entry.get("use_references")
        size = entry.get("size") or default_size
        png_path = out_dir / f"{name}.png"
        exists = png_path.is_file()
        if exists:
            # Ground-truth the aspect ratio from the actual pixels when the PNG
            # is on disk — the agent's same-aspect reuse rule keys off `size`, so
            # a mis-declared manifest size must not let it offer a wrong-aspect shot.
            actual = _png_dimensions(png_path)
            if actual:
                size = actual
        name_card = _name_is_card(name)
        is_card = (use_references is False) or name_card
        records.append(
            {
                "video": video,
                "name": name,
                "prompt": entry.get("prompt", ""),
                "use_references": use_references,
                "size": size,
                "png_path": str(png_path),
                "exists": exists,
                "is_card": is_card,
                # One-off text cards (by name) are not worth reusing; an
                # infographic backplate (use_references False, no card token)
                # still is, so it stays reusable.
                "reusable_hint": not name_card,
            }
        )
    return records


# Match backtick-wrapped PNG filenames in the Reference_Manifest table rows.
_REF_PNG_RE = re.compile(r"`([^`]+\.png)`")


def index_canonical_references() -> list[dict]:
    """Ingest the canonical Three character sheets from Reference_Manifest.md."""
    try:
        text = REFERENCE_MANIFEST.read_text(encoding="utf-8")
    except OSError as exc:
        sys.stderr.write(f"WARN  cannot read Reference_Manifest.md: {exc}\n")
        return []

    seen: set[str] = set()
    records = []
    for match in _REF_PNG_RE.finditer(text):
        filename = match.group(1)
        if filename in seen:
            continue
        seen.add(filename)
        png_path = CHARACTER_REF_DIR / filename
        exists = png_path.is_file()
        size = _png_dimensions(png_path) if exists else None
        name = Path(filename).stem
        records.append(
            {
                "video": "_canonical_reference",
                "name": name,
                "prompt": "",
                "use_references": None,
                "size": size,
                "png_path": str(png_path),
                "exists": exists,
                "is_card": False,
                "reusable_hint": True,
            }
        )
    return records


def build_index() -> list[dict]:
    records: list[dict] = []
    manifests = sorted(p for p in MANIFEST_DIR.glob("*.json") if _is_real_manifest(p))
    for path in manifests:
        records.extend(index_manifest(path))
    records.extend(index_canonical_references())
    return records


def _atomic_write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".asset_index.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.chmod(tmp, 0o644)  # mkstemp is 0600; the index is non-secret + read by the agent
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main() -> int:
    records = build_index()
    total = len(records)
    # Multiple manifests can point at the SAME PNG (e.g. video_01_full + video_01_hd
    # share Video_01_HD/), so the record count overstates how many images we own.
    # exist_on_disk is the UNIQUE-file count (the honest "images we own" headline the
    # agent's STOP gate and the avoided-generations math should key off); exist_records
    # is the raw per-manifest-entry tally.
    exist_records = sum(1 for r in records if r["exists"])
    exist_on_disk = len({r["png_path"] for r in records if r["exists"]})
    missing = total - exist_records
    payload = {
        "generated_by": "scripts/build_asset_index.py",
        "manifest_dir": str(MANIFEST_DIR),
        "total_shots": total,
        "exist_on_disk": exist_on_disk,
        "exist_records": exist_records,
        "missing": missing,
        "shots": records,
    }
    _atomic_write_json(OUTPUT, payload)
    sys.stdout.write(
        f"asset-index: {total} shot records — {exist_on_disk} unique PNGs on disk "
        f"({exist_records} records), {missing} records missing -> {OUTPUT}\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
