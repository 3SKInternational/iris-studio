#!/usr/bin/env python3
"""contact_sheet.py VIDEO [--open] [--output PATH]

Build a labeled contact sheet (grid thumbnail montage) of a 3SK Finance video's
rendered image batch, so a finished billed run can be eyeballed at a glance.

WHY: after a billed image spend completes, Steve wants to SEE all the renders in
one frame (and have it pop open in Preview) instead of opening 30-48 PNGs by hand.
build_video.py's image stage calls this on a clean finish; it is also runnable by
hand any time (needs a PIL-capable interpreter, e.g. the repo .venv/bin/python or
/usr/bin/python3): `.venv/bin/python scripts/contact_sheet.py 6 --open`.

Shots are laid out in narrative order, filtered to the renders that ACTUALLY
exist, each cell labeled with its short shot name (e.g. "05e"). A stale manifest
listing shots later cut from the shot list will NOT paint phantom "missing" cells.

Exit codes: 0 = sheet written (and opened if --open) | 3 = no renders found
            (quiet no-op — nothing to sheet) | 1 = a real error (bad args, PIL
            missing, manifest unreadable in a way we can't fall back from).
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

VAULT = Path("/Users/steve/Documents/3SK/outputs")
BRAND_REL = "BRANDS/3SK_Finance"


def nn(video: int) -> str:
    return f"{video:02d}"


def manifest_path(video: int) -> Path:
    # New orchestrated layout first, then the legacy _hd manifest.
    base = VAULT / BRAND_REL / "Raw_Assets" / "Image_Factory" / "manifests"
    orch = base / f"Video_{nn(video)}_orchestrated.json"
    return orch if orch.exists() else base / f"video_{nn(video)}_hd.json"


def renders_dir(video: int) -> Path:
    # Current pipeline renders to Video_NN_gen; older batches used Video_NN_HD.
    base = VAULT / BRAND_REL / "Raw_Assets"
    gen = base / f"Video_{nn(video)}_gen"
    return gen if gen.is_dir() else base / f"Video_{nn(video)}_HD"


def default_output(video: int) -> Path:
    return (VAULT / BRAND_REL / "Raw_Assets" / "Image_Factory" / "_REVIEW" /
            f"Video_{nn(video)}_contact_sheet.png")


def shot_order(video: int, rdir: Path) -> list[str]:
    """Show exactly the shots that actually rendered, in narrative order.

    Ordered by the manifest when it agrees with the renders, else by a sorted
    glob (3SK shot names are zero-padded, so they sort into narrative order).
    We deliberately filter to PRESENT renders so a stale manifest listing shots
    that were later cut from the shot list doesn't paint phantom 'missing' cells
    (the V6 hd manifest still lists 42 pre-consolidation shots; only 30 exist)."""
    present = sorted(p.stem for p in rdir.glob("*.png"))
    present_set = set(present)
    try:
        data = json.loads(manifest_path(video).read_text(encoding="utf-8"))
        names = [im["name"] for im in data.get("images", []) if im.get("name")]
        names_set = set(names)
        ordered = [n for n in names if n in present_set]
        ordered += [n for n in present if n not in names_set]  # renders absent from manifest
        if ordered:
            return ordered
    except Exception:
        pass
    return present


def build(video: int, out: Path) -> int:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception as e:  # pragma: no cover
        print(f"contact_sheet: PIL unavailable: {e}", file=sys.stderr)
        return 1

    rdir = renders_dir(video)
    if not rdir.is_dir():
        print(f"contact_sheet: no renders dir {rdir}", file=sys.stderr)
        return 3
    order = shot_order(video, rdir)
    if not order:
        print(f"contact_sheet: no renders found under {rdir}", file=sys.stderr)
        return 3

    COLS = 6
    CELL_W, CELL_H, LABEL_H, PAD, HEADER = 320, 200, 26, 6, 40
    cell_total_h = CELL_H + LABEL_H
    rows = (len(order) + COLS - 1) // COLS
    W = COLS * (CELL_W + PAD) + PAD
    H = rows * (cell_total_h + PAD) + PAD + HEADER

    sheet = Image.new("RGB", (W, H), (24, 24, 28))
    d = ImageDraw.Draw(sheet)
    try:
        f = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 16)
        fh = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 22)
    except Exception:
        f = ImageFont.load_default()
        fh = f

    present = sum(1 for n in order if (rdir / f"{n}.png").exists())
    d.text((PAD, 10), f"Video {nn(video)} — {present}/{len(order)} rendered",
           fill=(235, 235, 235), font=fh)

    for i, name in enumerate(order):
        r, c = divmod(i, COLS)
        x = PAD + c * (CELL_W + PAD)
        y = HEADER + PAD + r * (cell_total_h + PAD)
        path = rdir / f"{name}.png"
        if path.exists():
            try:
                im = Image.open(path).convert("RGB")
                im.thumbnail((CELL_W, CELL_H))
                sheet.paste(im, (x + (CELL_W - im.width) // 2,
                                 y + (CELL_H - im.height) // 2))
            except Exception:
                d.rectangle([x, y, x + CELL_W, y + CELL_H], fill=(60, 40, 40))
        else:
            d.rectangle([x, y, x + CELL_W, y + CELL_H], fill=(60, 40, 40))
        d.rectangle([x, y, x + CELL_W, y + CELL_H], outline=(90, 90, 96), width=1)
        short = name.replace(f"Video_{nn(video)}_Shot_", "").replace(f"Video_{nn(video)}_", "")
        d.rectangle([x, y + CELL_H, x + CELL_W, y + CELL_H + LABEL_H], fill=(40, 40, 46))
        d.text((x + 6, y + CELL_H + 4), short, fill=(255, 255, 255), font=f)

    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out)
    print(f"contact_sheet: wrote {out} ({sheet.size[0]}x{sheet.size[1]}, "
          f"{present}/{len(order)} present)")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Build a contact sheet of a video's renders.")
    p.add_argument("video", type=int, help="Video number (e.g. 3).")
    p.add_argument("--open", action="store_true", dest="open_",
                   help="Open the sheet in Preview after writing (macOS).")
    p.add_argument("--output", help="Override the output PNG path.")
    args = p.parse_args()

    out = Path(args.output) if args.output else default_output(args.video)
    rc = build(args.video, out)
    if rc == 0 and args.open_:
        try:
            subprocess.run(["open", "-a", "Preview", str(out)],
                           check=False, timeout=20)
        except Exception as e:
            print(f"contact_sheet: open failed (non-fatal): {e}", file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
