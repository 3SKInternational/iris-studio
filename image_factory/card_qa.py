#!/usr/bin/env python3
"""
Card QA gate — the automated reviewer for composited cards.

WHY THIS EXISTS
---------------
`card_overlay.py` makes text *correct by construction* (perfect spelling,
deterministic placement for ladders/tiles/lines). But two things still need a
human-or-agent eye that no Python check can give:

  1. The AI BACKPLATE itself — is it on-brand (flat 2D, no rogue figures, the
     shape the prompt asked for)? Only a vision model can judge that.
  2. Did every expected string actually land legibly, on the right shape, not
     clipped or off-canvas?

This tool does the *deterministic* half of that gate so the vision pass is fast
and systematic instead of ad-hoc:

  - From the overlay spec it extracts, per card, the EXACT set of strings that
    should appear (text elements + ladder rungs + tile labels + headers). That
    is the answer key the reviewer checks each image against.
  - It verifies every expected composited PNG exists.
  - It builds a labelled CONTACT SHEET (montage) of all composites so the whole
    batch can be eyeballed in one image.
  - It writes `card_qa_packet.json` — the checklist a vision agent walks: for
    each card, the file, the expected strings, and a verdict slot to fill.

The vision pass itself (PASS/FAIL + issues per card) is done by an agent that
reads each composite against this packet — that's the half a script can't do.
Failures should be routed to Telegram via scripts/notify.sh by the caller.

USAGE
-----
  python3 card_qa.py overlay_spec.json --composites DIR
  python3 card_qa.py overlay_spec.json --composites DIR --suffix _text
  python3 card_qa.py overlay_spec.json --packet-only      # no montage
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tempfile
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
    _HAVE_PIL = True
except ModuleNotFoundError:
    _HAVE_PIL = False


def die(msg: str) -> None:
    sys.stderr.write(f"[card_qa] ERROR: {msg}\n")
    sys.exit(1)


def expected_strings(elements: list) -> list[str]:
    """Every string a viewer should see on the card, in spec order."""
    out: list[str] = []
    for el in elements:
        if not isinstance(el, dict):
            continue
        etype = el.get("type", "text")
        if etype == "text" and el.get("text"):
            out.append(str(el["text"]).replace("\\n", " ").strip())
        elif etype == "ladder":
            rungs = el.get("rungs")
            out.extend(str(r).strip() for r in (rungs if isinstance(rungs, list) else []))
        elif etype == "tiles":
            if el.get("header"):
                out.append(str(el["header"]).strip())
            tiles = el.get("tiles")
            out.extend(str(t).strip() for t in (tiles if isinstance(tiles, list) else []))
        # "line" carries no text
    # de-dupe while preserving order
    seen, uniq = set(), []
    for s in out:
        if s and s not in seen:
            seen.add(s)
            uniq.append(s)
    return uniq


def load_font(size: int):
    for p in ("/Library/Fonts/Inter-Regular.ttf",
              "/System/Library/Fonts/Supplemental/Arial.ttf",
              "/System/Library/Fonts/Helvetica.ttc"):
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


def build_contact_sheet(items: list[dict], out_path: Path, cols: int = 4) -> None:
    """Montage of all existing composites, each captioned with its card name."""
    present = [it for it in items if it["exists"]]
    if not present:
        sys.stderr.write("[card_qa] no composites exist yet — skipping contact sheet.\n")
        return
    cell_w, cell_h, cap_h, pad = 480, 270, 28, 10
    n = len(present)
    rows = math.ceil(n / cols)
    W = min(n, cols) * (cell_w + pad) + pad
    H = rows * (cell_h + cap_h + pad) + pad
    sheet = Image.new("RGB", (W, H), (32, 36, 40))
    draw = ImageDraw.Draw(sheet)
    font = load_font(20)
    for idx, it in enumerate(present):
        r, c = divmod(idx, cols)
        x = pad + c * (cell_w + pad)
        y = pad + r * (cell_h + cap_h + pad)
        try:
            thumb = Image.open(it["file"]).convert("RGB")
            thumb.thumbnail((cell_w, cell_h), Image.LANCZOS)
            sheet.paste(thumb, (x + (cell_w - thumb.width) // 2,
                                y + (cell_h - thumb.height) // 2))
        except Exception as e:
            draw.text((x + 8, y + 8), f"[load failed] {e}", fill=(255, 120, 120), font=font)
        draw.text((x + 4, y + cell_h + 4), it["name"], fill=(235, 235, 235), font=font)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".png", dir=str(out_path.parent))
    os.close(fd)
    try:
        sheet.save(tmp, "PNG")
        os.replace(tmp, out_path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    print(f"[card_qa] contact sheet -> {out_path}  ({len(present)}/{len(items)} cards)")


def main() -> None:
    ap = argparse.ArgumentParser(description="Build the QA packet + contact sheet for composited cards.")
    ap.add_argument("spec", help="overlay spec JSON (same one card_overlay.py consumes)")
    ap.add_argument("--composites", help="dir of composited PNGs (default: spec.out_dir)")
    ap.add_argument("--suffix", default="_text", help="output suffix used by card_overlay (default '_text')")
    ap.add_argument("--packet-only", action="store_true", help="skip the contact sheet")
    ap.add_argument("--out", help="packet path (default: <composites>/card_qa_packet.json)")
    args = ap.parse_args()

    spec_path = Path(args.spec).expanduser().resolve()
    if not spec_path.is_file():
        die(f"spec not found: {spec_path}")
    try:
        spec = json.loads(spec_path.read_text())
    except json.JSONDecodeError as e:
        die(f"spec is not valid JSON: {e}")

    cards = spec.get("cards", {})
    if not isinstance(cards, dict):
        die("spec 'cards' must be an object {name: [elements]}")
    if not cards:
        die("spec has no 'cards'")

    comp_candidate = args.composites or spec.get("out_dir") or str(spec_path.parent)
    if not isinstance(comp_candidate, str):
        die(f"composites dir must be a string path, got {comp_candidate!r} "
            "(check spec 'out_dir')")
    comp_dir = Path(os.path.expanduser(comp_candidate)).resolve()

    items = []
    for name, els in cards.items():
        f = comp_dir / f"{name}{args.suffix}.png"
        items.append({
            "name": name,
            "file": str(f),
            "exists": f.is_file(),
            "expected_strings": expected_strings(els if isinstance(els, list) else []),
            "verdict": "UNREVIEWED",   # vision agent fills: PASS | FAIL
            "issues": [],              # vision agent fills: e.g. ["label clipped on tile 3"]
        })

    n_present = sum(1 for it in items if it["exists"])
    packet = {
        "spec": str(spec_path),
        "composites_dir": str(comp_dir),
        "suffix": args.suffix,
        "total_cards": len(items),
        "present": n_present,
        "missing": [it["name"] for it in items if not it["exists"]],
        "cards": items,
        "instructions": (
            "VISION REVIEW: for each card, open 'file' and confirm (a) every "
            "string in 'expected_strings' appears, spelled exactly, legible and "
            "not clipped/off-canvas; (b) text sits on its intended shape; (c) the "
            "underlying backplate is on-brand (flat 2D, no rogue human figures). "
            "Set 'verdict' to PASS or FAIL and list any problems in 'issues'. "
            "Route any FAIL to Steve via scripts/notify.sh."
        ),
    }

    out_path = Path(os.path.expanduser(args.out)) if args.out else comp_dir / "card_qa_packet.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(packet, indent=2))
    print(f"[card_qa] packet -> {out_path}")
    print(f"[card_qa] {len(items)} card(s), {n_present} present, {len(packet['missing'])} missing")
    if packet["missing"]:
        print("[card_qa] missing:", ", ".join(packet["missing"]))

    if not args.packet_only:
        if not _HAVE_PIL:
            sys.stderr.write("[card_qa] Pillow not installed — packet written, "
                             "contact sheet skipped.\n")
        else:
            build_contact_sheet(items, comp_dir / "card_qa_contact_sheet.png")


if __name__ == "__main__":
    main()
