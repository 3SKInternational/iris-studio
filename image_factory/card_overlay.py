#!/usr/bin/env python3
"""
Card text compositor — burns crisp, on-brand text onto generated card backplates.

WHY THIS EXISTS
---------------
Image models (gpt-image-2) reliably garble long/exact text: dollar figures,
multi-label diagrams, full sentences come back misspelled, dropped, or mislaid.
For a finance channel the numbers MUST be perfect. So the 3SK image pipeline
splits a data card into two layers, exactly like the thumbnail pipeline already
does (see thumbnail_overlay.py — this is its generalized sibling):

  1. image_factory generates a *text-free SHAPE BACKPLATE* (the ladder, the
     scale, the bars, the pegs) with "leave label areas clear" in the prompt.
  2. THIS tool composites the real text on top with PIL — guaranteed-correct,
     on-brand typography, and editable forever without re-billing the model.

Because data cards are static ``hold`` shots in the video, compositing onto the
still PNG is all that's needed — the video assembler (assemble.py) just consumes
the finished PNG like any other frame. No ffmpeg drawtext, no motion to fight.

SPEC FORMAT (JSON)
------------------
{
  "canvas": [2048, 1152],                       # backplate size (W,H); default 2048x1152
  "base_dir": "~/.../Raw_Assets/Video_02_HD",   # optional; --base-dir overrides
  "out_dir":  "~/.../Raw_Assets/Video_02_HD_text", # optional; --out-dir overrides
  "defaults": {"color":"charcoal","size":54,"anchor":"mm",
               "style":"plain","weight":"bold","align":"center","stroke":0},
  "cards": {
    "Video_02_Shot_01b": [
      {"text":"$10,000,000 APART","x":0.5,"y":0.60,"size":60,"color":"red","style":"stroked"}
    ],
    "Video_02_Shot_02b": [
      {"text":"L7  $10M+","x":0.5,"y":0.10,"size":40,"color":"red"},
      {"text":"L1  BELOW $0","x":0.5,"y":0.90,"size":40,"color":"charcoal"}
    ]
  }
}

Each element has a "type" (default "text"). x/y (and top/bottom/width/etc.) are
FRACTIONS of the canvas when a float in [0,1], else literal pixels.

  type "text"  — a single styled string. Fields: text (req), x/y (req), and
       optional size/color/anchor/style/weight/align/stroke/wrap/pill_color/
       underline_color.
         style:  plain | stroked | pill | underline
         color/pill_color/underline_color: palette name (white charcoal red
                 blue amber paper muted dark) or #RRGGBB.
         anchor: any PIL anchor (default "mm" = centered). wrap: px max width.

  --- STRUCTURAL types (DETERMINISTIC GEOMETRY) -------------------------------
  These DRAW their own shapes AND text at computed coordinates, so every label
  registers with its shape by construction — no eyeballing, identical every
  render. Use these for the dense cards (ladders, tile grids, comparisons) so
  the AI backplate only needs to supply a plain panel/atmosphere underneath.

  type "ladder" — N evenly-spaced rungs between two rails, one label per rung.
       Fields: rungs (req, >=2 labels), x (center), top, bottom, width (rail
       gap), order ("bottom_up" default = rungs[0] at the bottom | "top_down"),
       label_side (right|left|on), label_size, rail_color, rung_color,
       label_color, rail_thickness, rung_thickness, label_gap.

  type "tiles"  — N rounded tiles in a centered row (or grid via cols), each
       with a wrapped centered label, plus an optional header above. Fields:
       tiles (req), x, y (grid center), tile_w, tile_h, gap, cols (0=one row),
       tile_color, border_color, border_width, radius, label_color, label_size,
       header, header_y, header_size, header_color.

  type "line"   — a straight rule (dividers, baselines, brackets). Fields:
       x,y -> x2,y2 (req), color, thickness. A two-column comparison = one
       "line" divider + two "text" elements (left at x~0.25, right at x~0.75).

USAGE
-----
  python3 card_overlay.py spec.json --dry-run            # validate + plan, no writes
  python3 card_overlay.py spec.json                      # composite all cards
  python3 card_overlay.py spec.json --only Video_02_Shot_01b   # one card (repeatable)
  python3 card_overlay.py spec.json --base-dir DIR --out-dir DIR --suffix ""

Backplates are never modified in place: output goes to --out-dir (default
<base-dir>) as "<name><suffix>.png" (default suffix "_text"). If out-dir == base-dir
and suffix == "" the tool refuses (it would clobber the backplate). Atomic writes.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ModuleNotFoundError:
    sys.stderr.write("[card_overlay] ERROR: Pillow not installed "
                     "(pip install Pillow into the generation python).\n")
    sys.exit(1)

# ---- Brand palette (locked; mirrors thumbnail_overlay.py + Brand Bible) ----
PALETTE = {
    "white":    (255, 255, 255, 255),
    "charcoal": (31, 42, 51, 255),     # #1F2A33
    "dark":     (20, 26, 31, 255),     # #141A1F near-black body/stroke
    "red":      (200, 16, 46, 255),    # #C8102E brand-red accent
    "blue":     (42, 77, 110, 255),    # #2A4D6E
    "amber":    (229, 163, 56, 255),   # #E5A338
    "paper":    (247, 245, 240, 255),  # #F7F5F0
    "muted":    (91, 103, 112, 255),   # #5B6770
}
STROKE_FILL = PALETTE["dark"]          # default stroke colour for "stroked"

# Bold (display) and regular (body) font candidates, best -> fallback.
FONTS = {
    "bold": [
        "/Library/Fonts/Inter-Black.ttf",
        "/System/Library/Fonts/Supplemental/Arial Black.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/Library/Fonts/Arial Bold.ttf",
        "/System/Library/Fonts/HelveticaNeue.ttc",
    ],
    "regular": [
        "/Library/Fonts/Inter-Regular.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ],
}
VALID_STYLES = {"plain", "stroked", "pill", "underline"}
VALID_ALIGN = {"left", "center", "right"}
# PIL anchor grammar: horizontal {l,m,r} + vertical descriptor. multiline_text
# only accepts vertical a/m/d, so restrict to those to fail at validation not draw.
ANCHOR_H = {"l", "m", "r"}
ANCHOR_V = {"a", "m", "d"}
DEFAULT_CANVAS = (2048, 1152)

_FONT_CACHE: dict[tuple[str, int], "ImageFont.FreeTypeFont"] = {}


def die(msg: str) -> None:
    sys.stderr.write(f"[card_overlay] ERROR: {msg}\n")
    sys.exit(1)


def load_font(weight: str, size: int):
    key = (weight, size)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    for path in FONTS.get(weight, FONTS["bold"]):
        if os.path.exists(path):
            try:
                f = ImageFont.truetype(path, size)
                _FONT_CACHE[key] = f
                return f
            except Exception:
                continue
    # Last resort: PIL default (fixed size, low quality) — visible signal to
    # install a real font rather than a silent crash.
    sys.stderr.write(f"[card_overlay] WARN: no '{weight}' font found; "
                     f"using PIL default for size {size}.\n")
    f = ImageFont.load_default()
    _FONT_CACHE[key] = f
    return f


def resolve_color(name) -> tuple:
    if isinstance(name, (list, tuple)):
        t = tuple(int(c) for c in name)
        if len(t) not in (3, 4):
            die(f"colour tuple {name!r} must have 3 (RGB) or 4 (RGBA) values")
        if any(c < 0 or c > 255 for c in t):
            die(f"colour tuple {name!r} channels must be 0-255")
        return t if len(t) == 4 else t + (255,)
    s = str(name).strip()
    if s.startswith("#"):
        h = s.lstrip("#")
        if len(h) == 6:
            try:
                return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 255)
            except ValueError:
                die(f"bad hex colour {name!r} (non-hex digits; want #RRGGBB)")
        die(f"bad hex colour {name!r} (want #RRGGBB)")
    if s in PALETTE:
        return PALETTE[s]
    die(f"unknown colour {name!r} (palette: {sorted(PALETTE)} or #RRGGBB)")


def resolve_pos(v, span: int) -> int:
    """Fraction of the canvas edge when a float in [0,1], else literal pixels."""
    if isinstance(v, bool):
        die(f"position must be a number, got bool {v!r}")
    if isinstance(v, float) and 0.0 <= v <= 1.0:
        return round(v * span)
    return int(round(v))


def wrap_text(draw, text: str, font, max_w: int) -> str:
    """Greedy word-wrap to fit max_w px; preserves any explicit newlines."""
    out_lines: list[str] = []
    for para in text.split("\n"):
        words = para.split()
        if not words:
            out_lines.append("")
            continue
        line = words[0]
        for w in words[1:]:
            cand = f"{line} {w}"
            if draw.textlength(cand, font=font) <= max_w:
                line = cand
            else:
                out_lines.append(line)
                line = w
        out_lines.append(line)
    return "\n".join(out_lines)


def _check_anchor(card: str, i: int, anchor) -> None:
    s = str(anchor)
    if len(s) != 2 or s[0] not in ANCHOR_H or s[1] not in ANCHOR_V:
        die(f"{card} element {i}: bad anchor {anchor!r} "
            f"(want 2 chars: h in {sorted(ANCHOR_H)}, v in {sorted(ANCHOR_V)}, e.g. 'mm')")


def _check_pos_int(card: str, i: int, field: str, value, span: int) -> None:
    """Mirror draw-time coercion so a bad x/y/wrap/size fails at validation."""
    if isinstance(value, bool):
        die(f"{card} element {i}: '{field}' must be a number, got bool {value!r}")
    if not isinstance(value, (int, float)):
        die(f"{card} element {i}: '{field}' must be a number, got {value!r}")


# ---------------------------------------------------------------------------
# Element types. "text" is the default (a single styled string). The structural
# types — ladder / tiles / line — DRAW their own geometry AND text at known,
# computed coordinates, so the label always registers with its shape (the
# "deterministic geometry" path: nothing to eyeball, identical every render).
# ---------------------------------------------------------------------------
VALID_TYPES = {"text", "ladder", "tiles", "line"}


def _num(card, i, field, v, *, positive=False, nonneg=False):
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        die(f"{card} element {i}: '{field}' must be a number, got {v!r}")
    if positive and v <= 0:
        die(f"{card} element {i}: '{field}' must be > 0, got {v!r}")
    if nonneg and v < 0:
        die(f"{card} element {i}: '{field}' must be >= 0, got {v!r}")


def _opt_num(card, i, el, field, **kw):
    """Validate a field IFF present — so every value a drawer later coerces
    via int()/resolve_pos is checked up-front (mirror draw, fail-fast)."""
    if field in el:
        _num(card, i, field, el[field], **kw)


def _validate_text(card, i, el, canvas, defaults):
    if "text" not in el or not str(el["text"]).strip():
        die(f"{card} element {i}: missing non-empty 'text'")
    if "x" not in el or "y" not in el:
        die(f"{card} element {i}: needs 'x' and 'y'")
    _check_pos_int(card, i, "x", el["x"], canvas[0])
    _check_pos_int(card, i, "y", el["y"], canvas[1])
    style = el.get("style", defaults.get("style", "plain"))
    if style not in VALID_STYLES:
        die(f"{card} element {i}: unknown style {style!r} (valid: {sorted(VALID_STYLES)})")
    if el.get("align", defaults.get("align", "center")) not in VALID_ALIGN:
        die(f"{card} element {i}: unknown align {el.get('align')!r} (valid: {sorted(VALID_ALIGN)})")
    _check_anchor(card, i, el.get("anchor", defaults.get("anchor", "mm")))
    _num(card, i, "size", el.get("size", defaults.get("size", 54)), positive=True)
    _num(card, i, "stroke", el.get("stroke", defaults.get("stroke", 0)), nonneg=True)
    if "wrap" in el:
        _num(card, i, "wrap", el["wrap"], positive=True)
    # pill + underline extras the drawer coerces via int()
    for f in ("pad_x", "pad_y", "underline_thickness", "underline_gap"):
        _opt_num(card, i, el, f, nonneg=True)
    resolve_color(el.get("color", defaults.get("color", "charcoal")))
    if "pill_color" in el:
        resolve_color(el["pill_color"])
    if "underline_color" in el:
        resolve_color(el["underline_color"])


def _validate_ladder(card, i, el):
    rungs = el.get("rungs")
    if not isinstance(rungs, list) or len(rungs) < 2:
        die(f"{card} element {i}: ladder needs a 'rungs' list of >=2 labels")
    if el.get("order", "bottom_up") not in ("bottom_up", "top_down"):
        die(f"{card} element {i}: ladder 'order' must be 'bottom_up' or 'top_down'")
    if el.get("label_side", "right") not in ("left", "right", "on"):
        die(f"{card} element {i}: ladder 'label_side' must be left|right|on")
    for f in ("x", "top", "bottom"):
        _opt_num(card, i, el, f)
    _opt_num(card, i, el, "width", positive=True)
    # top must sit above bottom (smaller y); a swap silently flips the ladder.
    if "top" in el and "bottom" in el and el["top"] >= el["bottom"]:
        die(f"{card} element {i}: ladder 'top' ({el['top']}) must be < 'bottom' ({el['bottom']})")
    _num(card, i, "label_size", el.get("label_size", 36), positive=True)
    for f in ("rail_thickness", "rung_thickness"):
        _opt_num(card, i, el, f, positive=True)
    _opt_num(card, i, el, "label_gap", nonneg=True)
    resolve_color(el.get("rail_color", "charcoal"))
    resolve_color(el.get("rung_color", "charcoal"))
    resolve_color(el.get("label_color", "charcoal"))


def _validate_tiles(card, i, el):
    tiles = el.get("tiles")
    if not isinstance(tiles, list) or not tiles:
        die(f"{card} element {i}: tiles needs a non-empty 'tiles' list")
    cols = el.get("cols", 0)
    if not isinstance(cols, int) or isinstance(cols, bool) or cols < 0:
        die(f"{card} element {i}: tiles 'cols' must be an int >= 0 (0 = single row)")
    for f in ("x", "y"):
        _opt_num(card, i, el, f)
    # tile_w/tile_h must be > 0 or the rounded_rectangle box inverts and PIL raises.
    for f in ("tile_w", "tile_h"):
        _opt_num(card, i, el, f, positive=True)
    for f in ("gap", "gap_y", "radius", "border_width"):
        _opt_num(card, i, el, f, nonneg=True)
    _num(card, i, "label_size", el.get("label_size", 40), positive=True)
    resolve_color(el.get("tile_color", "blue"))
    resolve_color(el.get("label_color", "white"))
    if el.get("border_color") is not None:
        resolve_color(el["border_color"])
    if "header" in el and el["header"]:
        resolve_color(el.get("header_color", "charcoal"))
        _num(card, i, "header_size", el.get("header_size", 60), positive=True)
        _opt_num(card, i, el, "header_y")


def _validate_line(card, i, el):
    for f in ("x", "y", "x2", "y2"):
        if f not in el:
            die(f"{card} element {i}: line needs 'x','y','x2','y2'")
        _num(card, i, f, el[f])
    _num(card, i, "thickness", el.get("thickness", 6), positive=True)
    resolve_color(el.get("color", "charcoal"))


def validate_element(card: str, i: int, el: dict, canvas: tuple[int, int],
                     defaults: dict) -> None:
    if not isinstance(el, dict):
        die(f"{card} element {i}: must be an object")
    etype = el.get("type", "text")
    if etype not in VALID_TYPES:
        die(f"{card} element {i}: unknown type {etype!r} (valid: {sorted(VALID_TYPES)})")
    if etype == "text":
        _validate_text(card, i, el, canvas, defaults)
    elif etype == "ladder":
        _validate_ladder(card, i, el)
    elif etype == "tiles":
        _validate_tiles(card, i, el)
    elif etype == "line":
        _validate_line(card, i, el)


def _draw_text(draw, text, x, y, *, font, color, anchor="mm", align="center",
               style="plain", stroke=0, size=54, el=None):
    """Core single-string drawer (shared by 'text' elements and label sub-draws)."""
    el = el or {}
    text = str(text).replace("\\n", "\n")
    if style == "pill":
        pad_x = int(el.get("pad_x", 28))
        pad_y = int(el.get("pad_y", 14))
        pill_color = resolve_color(el.get("pill_color", "charcoal"))
        l, t, r, b = draw.multiline_textbbox((x, y), text, font=font,
                                             anchor=anchor, align=align)
        box = [l - pad_x, t - pad_y, r + pad_x, b + pad_y]
        radius = max(0, (box[3] - box[1]) // 2)
        draw.rounded_rectangle(box, radius=radius, fill=pill_color)
        draw.multiline_text((x, y), text, font=font, fill=color,
                            anchor=anchor, align=align)
        return
    stroke_kw = {}
    if style == "stroked":
        stroke_kw = {"stroke_width": stroke or 6, "stroke_fill": STROKE_FILL}
    elif stroke:
        stroke_kw = {"stroke_width": stroke, "stroke_fill": STROKE_FILL}
    draw.multiline_text((x, y), text, font=font, fill=color,
                        anchor=anchor, align=align, **stroke_kw)
    if style == "underline":
        ul_color = resolve_color(el.get("underline_color", "red"))
        l, t, r, b = draw.multiline_textbbox((x, y), text, font=font,
                                             anchor=anchor, align=align)
        thick = int(el.get("underline_thickness", max(3, size // 12)))
        gap = int(el.get("underline_gap", max(4, size // 8)))
        draw.rectangle([l, b + gap, r, b + gap + thick], fill=ul_color)


def draw_text_element(draw, el, defaults, canvas):
    W, H = canvas
    size = int(el.get("size", defaults.get("size", 54)))
    weight = el.get("weight", defaults.get("weight", "bold"))
    color = resolve_color(el.get("color", defaults.get("color", "charcoal")))
    anchor = el.get("anchor", defaults.get("anchor", "mm"))
    align = el.get("align", defaults.get("align", "center"))
    style = el.get("style", defaults.get("style", "plain"))
    stroke = int(el.get("stroke", defaults.get("stroke", 0)))
    font = load_font(weight, size)
    text = str(el["text"]).replace("\\n", "\n")
    if el.get("wrap"):
        text = wrap_text(draw, text, font, int(el["wrap"]))
    # pill text defaults to white unless overridden
    if style == "pill" and "color" not in el and "color" not in defaults:
        color = resolve_color("white")
    _draw_text(draw, text, resolve_pos(el["x"], W), resolve_pos(el["y"], H),
               font=font, color=color, anchor=anchor, align=align,
               style=style, stroke=stroke, size=size, el=el)


def draw_ladder(draw, el, defaults, canvas):
    W, H = canvas
    rungs = el["rungs"]
    n = len(rungs)
    cx = resolve_pos(el.get("x", 0.5), W)
    top = resolve_pos(el.get("top", 0.12), H)
    bottom = resolve_pos(el.get("bottom", 0.90), H)
    half = resolve_pos(el.get("width", 0.12), W) // 2
    rail_color = resolve_color(el.get("rail_color", "charcoal"))
    rung_color = resolve_color(el.get("rung_color", "charcoal"))
    rail_w = int(el.get("rail_thickness", 6))
    rung_w = int(el.get("rung_thickness", 6))
    label_color = resolve_color(el.get("label_color", "charcoal"))
    label_size = int(el.get("label_size", 36))
    label_side = el.get("label_side", "right")
    label_gap = int(el.get("label_gap", 24))
    weight = el.get("label_weight", defaults.get("weight", "bold"))
    font = load_font(weight, label_size)
    order = el.get("order", "bottom_up")

    # Two vertical rails.
    draw.line([(cx - half, top), (cx - half, bottom)], fill=rail_color, width=rail_w)
    draw.line([(cx + half, top), (cx + half, bottom)], fill=rail_color, width=rail_w)

    for k, label in enumerate(rungs):
        frac = k / (n - 1)
        # bottom_up: rungs[0] sits at the bottom (a wealth ladder rises L1->L7).
        y = bottom - frac * (bottom - top) if order == "bottom_up" else top + frac * (bottom - top)
        y = int(round(y))
        draw.line([(cx - half, y), (cx + half, y)], fill=rung_color, width=rung_w)
        if label_side == "on":
            _draw_text(draw, label, cx, y - label_size, font=font,
                       color=label_color, anchor="mm", size=label_size)
        elif label_side == "left":
            _draw_text(draw, label, cx - half - label_gap, y, font=font,
                       color=label_color, anchor="rm", size=label_size)
        else:
            _draw_text(draw, label, cx + half + label_gap, y, font=font,
                       color=label_color, anchor="lm", size=label_size)


def draw_tiles(draw, el, defaults, canvas):
    W, H = canvas
    tiles = el["tiles"]
    n = len(tiles)
    cols = el.get("cols", 0) or n
    rows = (n + cols - 1) // cols
    cx = resolve_pos(el.get("x", 0.5), W)
    cy = resolve_pos(el.get("y", 0.55), H)
    tw = resolve_pos(el.get("tile_w", 0.26), W)
    th = resolve_pos(el.get("tile_h", 0.30), H)
    gap_x = resolve_pos(el.get("gap", 0.03), W)
    gap_y = resolve_pos(el.get("gap_y", el.get("gap", 0.04)), H)
    radius = int(el.get("radius", 18))
    tile_color = resolve_color(el.get("tile_color", "blue"))
    border_color = resolve_color(el["border_color"]) if el.get("border_color") else None
    border_w = int(el.get("border_width", 0))
    label_color = resolve_color(el.get("label_color", "white"))
    label_size = int(el.get("label_size", 40))
    weight = el.get("label_weight", defaults.get("weight", "bold"))
    font = load_font(weight, label_size)

    # Optional header above the grid.
    header = el.get("header")
    if header:
        hsize = int(el.get("header_size", 60))
        hcolor = resolve_color(el.get("header_color", "charcoal"))
        hy = resolve_pos(el.get("header_y", 0.15), H)
        _draw_text(draw, header, cx, hy, font=load_font(weight, hsize),
                   color=hcolor, anchor="mm", size=hsize)

    grid_w = cols * tw + (cols - 1) * gap_x
    grid_h = rows * th + (rows - 1) * gap_y
    x0 = cx - grid_w // 2
    y0 = cy - grid_h // 2
    for idx, label in enumerate(tiles):
        r, c = divmod(idx, cols)
        left = x0 + c * (tw + gap_x)
        top = y0 + r * (th + gap_y)
        box = [left, top, left + tw, top + th]
        draw.rounded_rectangle(box, radius=radius, fill=tile_color,
                               outline=border_color, width=border_w if border_color else 0)
        # Label wrapped to fit inside the tile, centered.
        wrapped = wrap_text(draw, str(label), font, int(tw * 0.86))
        _draw_text(draw, wrapped, left + tw // 2, top + th // 2, font=font,
                   color=label_color, anchor="mm", align="center", size=label_size)


def draw_line(draw, el, defaults, canvas):
    W, H = canvas
    x1, y1 = resolve_pos(el["x"], W), resolve_pos(el["y"], H)
    x2, y2 = resolve_pos(el["x2"], W), resolve_pos(el["y2"], H)
    draw.line([(x1, y1), (x2, y2)], fill=resolve_color(el.get("color", "charcoal")),
              width=int(el.get("thickness", 6)))


_DRAWERS = {"text": draw_text_element, "ladder": draw_ladder,
            "tiles": draw_tiles, "line": draw_line}


def draw_element(draw, el: dict, defaults: dict, canvas: tuple[int, int]) -> None:
    _DRAWERS[el.get("type", "text")](draw, el, defaults, canvas)


def render_card(base_path: Path, elements: list, out_path: Path,
                canvas: tuple[int, int], defaults: dict) -> None:
    if not base_path.is_file():
        die(f"backplate not found: {base_path}")
    img = Image.open(base_path).convert("RGBA")
    if img.size != canvas:
        sys.stderr.write(f"[card_overlay] note: {base_path.name} is {img.size}, "
                         f"resizing to {canvas}.\n")
        img = img.resize(canvas, Image.LANCZOS)
    draw = ImageDraw.Draw(img)
    for el in elements:
        draw_element(draw, el, defaults, canvas)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write: temp in the same dir, then replace.
    fd, tmp = tempfile.mkstemp(suffix=".png", dir=str(out_path.parent))
    os.close(fd)
    try:
        img.convert("RGB").save(tmp, "PNG")
        os.replace(tmp, out_path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def main() -> None:
    ap = argparse.ArgumentParser(description="Composite crisp text onto card backplates.")
    ap.add_argument("spec", help="overlay spec JSON")
    ap.add_argument("--base-dir", help="dir of backplate PNGs (overrides spec.base_dir)")
    ap.add_argument("--out-dir", help="dir for composited PNGs (default: base-dir)")
    ap.add_argument("--suffix", default="_text",
                    help="appended to each output basename (default '_text'; "
                         "'' keeps the name — then --out-dir must differ from base)")
    ap.add_argument("--only", action="append", default=[],
                    help="composite only this card name (repeatable)")
    ap.add_argument("--dry-run", action="store_true",
                    help="validate spec + check backplates exist; no writes")
    args = ap.parse_args()

    spec_path = Path(args.spec).expanduser().resolve()
    if not spec_path.is_file():
        die(f"spec not found: {spec_path}")
    try:
        spec = json.loads(spec_path.read_text())
    except json.JSONDecodeError as e:
        die(f"spec is not valid JSON: {e}")

    canvas_raw = tuple(spec.get("canvas", DEFAULT_CANVAS))
    if len(canvas_raw) != 2:
        die(f"canvas must be [W,H], got {canvas_raw}")
    try:
        canvas = (int(canvas_raw[0]), int(canvas_raw[1]))
    except (TypeError, ValueError):
        die(f"canvas [W,H] must be integers, got {canvas_raw}")
    if canvas[0] <= 0 or canvas[1] <= 0:
        die(f"canvas dimensions must be > 0, got {canvas}")
    defaults = spec.get("defaults", {})
    cards = spec.get("cards", {})
    if not cards:
        die("spec has no 'cards'")

    base_dir = Path(os.path.expanduser(
        args.base_dir or spec.get("base_dir") or spec_path.parent)).resolve()
    out_dir = Path(os.path.expanduser(
        args.out_dir or spec.get("out_dir") or base_dir)).resolve()
    if out_dir == base_dir and args.suffix == "":
        die("out-dir == base-dir and suffix is empty → would overwrite backplates; "
            "set --suffix or a separate --out-dir.")

    names = list(cards)
    if args.only:
        unknown = [n for n in args.only if n not in cards]
        if unknown:
            die(f"--only names not in spec: {unknown}")
        names = [n for n in names if n in args.only]

    # Validate everything up front (cheap, before any image work) — including
    # that every backplate exists. Fail-fast before any writes: one missing or
    # malformed card aborts the whole batch so we never leave a partial set.
    missing = []
    for name in names:
        els = cards[name]
        if not isinstance(els, list) or not els:
            die(f"{name}: must map to a non-empty list of elements")
        for i, el in enumerate(els):
            validate_element(name, i, el, canvas, defaults)
        if not (base_dir / f"{name}.png").is_file():
            missing.append(name)
    if missing and not args.dry_run:
        die("backplate(s) not found (fix or --dry-run to plan): "
            + ", ".join(missing))

    print(f"[card_overlay] spec {spec_path.name}  canvas {canvas[0]}x{canvas[1]}  "
          f"{len(names)} card(s)")
    print(f"[card_overlay] base {base_dir}")
    print(f"[card_overlay] out  {out_dir}  (suffix {args.suffix!r})")

    n_ok = 0
    for name in names:
        base_path = base_dir / f"{name}.png"
        out_path = out_dir / f"{name}{args.suffix}.png"
        n_el = len(cards[name])
        if args.dry_run:
            status = "OK" if base_path.is_file() else "MISSING backplate"
            print(f"  [{status}] {name}  ({n_el} element(s)) -> {out_path.name}")
            continue
        render_card(base_path, cards[name], out_path, canvas, defaults)
        print(f"  wrote {out_path.name}  ({n_el} element(s))")
        n_ok += 1

    if args.dry_run:
        missing = [n for n in names if not (base_dir / f"{n}.png").is_file()]
        print(f"[card_overlay] dry-run: {len(names)} card(s) validated, "
              f"{len(missing)} missing backplate(s).")
        if missing:
            print("  missing:", ", ".join(missing))
    else:
        print(f"[card_overlay] done — {n_ok} card(s) composited into {out_dir}")


if __name__ == "__main__":
    main()
