#!/usr/bin/env python3
"""Card backplate generator — the $0 half of the two-layer data card.

WHY THIS EXISTS
---------------
`card_overlay.py` already splits a data card into two layers: a text-free SHAPE
BACKPLATE, and the real text burned on top with PIL so the numbers are always
correct. But the backplate itself was still being BILLED to gpt-image-2 — about
8 shots per flagship at ~$0.10-0.13 each.

That spend buys nothing. A backplate is a flat colour field plus a few flat
shapes; the model can only APPROXIMATE #1F2A33, sometimes adds the texture or
gradient the prompt explicitly forbids, and occasionally hallucinates a
character or stray glyph into a frame whose entire spec is "No character.
ABSOLUTELY NO TEXT." Every one of those failure modes then has to be caught by
the PROMPTS/RENDERS review gates.

Rendering them here instead is exact, deterministic, free, and instantly
re-runnable — and it removes them from the review surface entirely, because a
shape that cannot contain text or people needs no audit for text or people.

WHAT THIS DOES NOT DO
---------------------
Anything with the "Three" character in it. Those need the image model plus the
reference sheet to stay on-model; that is the real cost of a batch and this tool
does not touch it. Solo-silhouette shots are also out of scope: they are figures
inside environments, not pure geometry, and hand-building them here would read
as off-style beside the rendered shots.

PALETTE
-------
Imported from card_overlay.PALETTE — deliberately NOT redefined. The backplate
and the text burned onto it must agree on what "charcoal" is; two copies of a
colour table is how they silently drift apart.

SPEC FORMAT (JSON) — mirrors card_overlay's shape
-------------------------------------------------
{
  "canvas": [2048, 1152],                  # optional; defaults to card_overlay's
  "out_dir": "~/.../Raw_Assets/Video_14_HD",
  "defaults": {"field": "charcoal", "accent": "amber"},
  "cards": [
    {"name": "Video_14_Shot_02_3",  "variant": "bars", "values": [1.0, 0.44]},
    {"name": "Video_14_Shot_07b_4", "variant": "line", "field": "blue"},
    {"name": "Video_14_Shot_13b_5", "variant": "plain", "accent": "amber"}
  ]
}

VARIANTS
  plain  flat field + card panel + accent block
  bars   N vertical bars, heights from `values` (fractions of the panel) — the
         two-outcome gap shape
  line   a rising polyline — the crossover/compounding curve
  dip    rise, drop, recover — the market-crash beat

Usage:
  card_backplate.py SPEC.json                 # render every card in the spec
  card_backplate.py SPEC.json --out-dir DIR   # override the destination
  card_backplate.py --demo DIR                # one of each variant, to eyeball
  card_backplate.py --selftest                # hermetic checks, no writes

Exit: 0 ok | 1 render/spec error | 2 bad usage.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
try:
    from card_overlay import PALETTE, DEFAULT_CANVAS  # single source of truth
except ImportError as e:                                          # pragma: no cover
    print(f"error: cannot import card_overlay ({e}); it must sit beside this file.",
          file=sys.stderr)
    raise SystemExit(1)

try:
    from PIL import Image, ImageDraw
except ImportError:                                               # pragma: no cover
    print("error: Pillow not available. Use the repo venv: "
          ".venv/bin/python image_factory/card_backplate.py ...", file=sys.stderr)
    raise SystemExit(1)

VARIANTS = ("plain", "bars", "line", "dip")

# Panel inset as a fraction of canvas. The panel is the lighter field the text
# lands on; card_overlay positions text in canvas fractions, so keeping the panel
# generous means a centred label never lands on the raw background.
_PANEL_X, _PANEL_Y = 0.11, 0.14
_ACCENT_W, _ACCENT_H = 0.055, 0.040      # the single accent block, lower right


def die(msg: str, code: int = 1):
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(code)


def _rgb(name: str) -> tuple[int, int, int]:
    """Palette name or #RRGGBB -> RGB triple (alpha dropped; backplates are opaque)."""
    if isinstance(name, str) and name.startswith("#") and len(name) == 7:
        try:
            return tuple(int(name[i:i + 2], 16) for i in (1, 3, 5))
        except ValueError:
            die(f"malformed hex colour {name!r} (expected #RRGGBB)")
    if name not in PALETTE:
        die(f"unknown colour {name!r} (palette: {sorted(PALETTE)} or #RRGGBB)")
    return PALETTE[name][:3]


def _lift(rgb: tuple[int, int, int], amount: int = 14) -> tuple[int, int, int]:
    """Panel tone: a touch off the field so the card reads as a distinct plane
    WITHOUT a border (the spec forbids borders, frames and outlines).

    Lightening clamps at 255, so on a near-white field (`white`, `paper` — both in
    the imported PALETTE, both reachable config) the panel became invisible and the
    "distinct plane" contract failed silently. Darken instead whenever lightening
    would clamp, so the separation exists at every point in the palette."""
    if max(rgb) + amount > 255:
        return tuple(max(0, c - amount) for c in rgb)
    return tuple(c + amount for c in rgb)


def render_card(card: dict, defaults: dict, canvas: tuple[int, int]) -> Image.Image:
    W, H = canvas
    field = _rgb(card.get("field", defaults.get("field", "charcoal")))
    accent = _rgb(card.get("accent", defaults.get("accent", "amber")))
    shape = _rgb(card.get("shape", defaults.get("shape", "blue")))
    variant = card.get("variant", "plain")
    if variant not in VARIANTS:
        die(f"{card.get('name','<unnamed>')}: unknown variant {variant!r} "
            f"(known: {', '.join(VARIANTS)})")

    im = Image.new("RGB", (W, H), field)
    d = ImageDraw.Draw(im)
    px, py = int(W * _PANEL_X), int(H * _PANEL_Y)
    panel = (px, py, W - px, H - py)
    d.rounded_rectangle(panel, radius=int(H * 0.024), fill=_lift(field))

    base = int(H * 0.74)                 # shared ground line for all geometry
    left, right = px + int(W * 0.09), W - px - int(W * 0.09)
    span = right - left
    top = py + int(H * 0.10)
    tall = base - top

    if variant == "bars":
        # `.get("values") or DEFAULT` was wrong: `or` treats an EMPTY list as absent,
        # so a spec that explicitly said "no bars" silently got the 2-bar default —
        # rendering something the author did not ask for and reporting success. Use a
        # sentinel so "key absent" (take the default) and "key present but empty" (an
        # error) are distinguishable. My first patch put the empty-check AFTER the
        # `or`, where it was unreachable; caught by actually running it.
        vals = card.get("values", [1.0, 0.44])
        if not isinstance(vals, list):
            die(f"{card.get('name')}: bars `values` must be a list, got {type(vals).__name__}")
        if not vals:
            die(f"{card.get('name')}: bars `values` is empty — nothing would be drawn")
        if not all(isinstance(v, (int, float)) and 0 < v <= 1 for v in vals):
            die(f"{card.get('name')}: bars `values` must be fractions in (0,1]")
        n = len(vals)
        bw = int(span / (n * 2 - 1))     # equal bar and gap widths
        if bw < 1:
            die(f"{card.get('name')}: {n} bars do not fit in {span}px — bars would be "
                f"invisible at this canvas size")
        for i, v in enumerate(vals):
            x0 = left + i * 2 * bw
            d.rectangle([x0, base - int(tall * v), x0 + bw, base], fill=shape)
    elif variant in ("line", "dip"):
        if variant == "line":
            pts = [(left, base), (left + span * 0.45, base - tall * 0.34),
                   (right, base - tall * 0.92)]
        else:
            pts = [(left, base - tall * 0.30), (left + span * 0.34, base - tall * 0.72),
                   (left + span * 0.55, base - tall * 0.18),
                   (right, base - tall * 0.86)]
        d.line([(int(x), int(y)) for x, y in pts], fill=shape,
               width=max(6, int(H * 0.014)), joint="curve")

    aw, ah = int(W * _ACCENT_W), int(H * _ACCENT_H)
    d.rectangle([W - px - aw, H - py - ah, W - px, H - py], fill=accent)
    return im


def run_spec(spec: dict, out_dir: Path | None) -> list[Path]:
    canvas = tuple(spec.get("canvas") or DEFAULT_CANVAS)
    defaults = spec.get("defaults") or {}
    cards = spec.get("cards") or []
    if not cards:
        die("spec has no `cards`")
    dest = out_dir or Path(os.path.expanduser(spec.get("out_dir", "."))).resolve()
    dest.mkdir(parents=True, exist_ok=True)
    written = []
    for c in cards:
        name = c.get("name")
        if not name:
            die("every card needs a `name` (the manifest shot name)")
        p = dest / f"{name}.png"
        render_card(c, defaults, canvas).save(p)
        written.append(p)
        print(f"  [card] {name:28} {c.get('variant','plain'):6} -> {p}")
    return written


def _selftest() -> int:
    import tempfile
    fails = []

    def chk(n, ok, d=""):
        print(f"  [{'ok ' if ok else 'FAIL'}] {n}{'' if ok else '  ' + str(d)}")
        if not ok:
            fails.append(n)

    canvas = (256, 144)
    im = render_card({"name": "t", "variant": "plain"}, {}, canvas)
    chk("renders at the requested size", im.size == canvas, im.size)

    # EXACT palette, pinned to the LITERAL #1F2A33. Comparing the pixel to
    # PALETTE["charcoal"] took both sides from the same table and asserted nothing —
    # it would pass if the palette drifted to any other colour. The literal is the
    # whole point over a generative model, which only approximates a hex value.
    chk("field colour is exactly #1F2A33", im.getpixel((0, 0)) == (31, 42, 51),
        im.getpixel((0, 0)))

    a = render_card({"name": "t", "variant": "bars", "values": [1.0, 0.44]}, {}, canvas)

    # The _lift contract: the panel must be VISIBLY distinct from the field with no
    # border. Uncovered before, and it silently failed on near-white fields where
    # lightening clamped at 255. Check both a dark and a near-white field.
    for fld in ("charcoal", "white"):
        c = render_card({"name": "t", "variant": "plain", "field": fld}, {}, canvas)
        corner, middle = c.getpixel((0, 0)), c.getpixel((canvas[0] // 2, canvas[1] // 2))
        chk(f"panel visible against '{fld}' field", corner != middle, f"{corner} == {middle}")

    # Geometry actually differs by variant (guards a stub that ignores `variant`).
    plain = render_card({"name": "t", "variant": "plain"}, {}, canvas)
    chk("bars differ from plain", a.tobytes() != plain.tobytes())
    line = render_card({"name": "t", "variant": "line"}, {}, canvas)
    dip = render_card({"name": "t", "variant": "dip"}, {}, canvas)
    chk("line differs from plain", line.tobytes() != plain.tobytes())
    chk("dip differs from line", dip.tobytes() != line.tobytes())

    # Bar heights must track `values`, not be fixed decoration.
    tallbar = render_card({"name": "t", "variant": "bars", "values": [1.0, 1.0]}, {}, canvas)
    chk("bar heights follow values", tallbar.tobytes() != a.tobytes())

    # SHARED palette, asserted by IDENTITY. `"charcoal" in PALETTE` passed just as
    # happily against a local copy-paste, which is the exact drift this guards.
    import card_overlay as _co
    chk("palette IS card_overlay's object (not a copy)", PALETTE is _co.PALETTE)

    # Bad input fails loudly rather than rendering something wrong.
    for bad, label in (({"name": "t", "variant": "nope"}, "unknown variant"),
                       ({"name": "t", "variant": "bars", "values": [0, 2]}, "out-of-range values"),
                       # An EXPLICIT empty list must error, not silently take the default.
                       # `.get("values") or DEFAULT` reintroduces that verbatim and used to
                       # stay green: the guard was written but never guarded.
                       ({"name": "t", "variant": "bars", "values": []}, "explicitly empty values"),
                       ({"name": "t", "variant": "bars", "values": "x"}, "non-list values"),
                       # Too many bars for the canvas -> 0px wide, invisible, success.
                       # 200 bars across the 256px selftest canvas -> bw floors to 0.
                       # (40 gives bw=1 here — a real but non-zero bar — so the earlier
                       # count did not exercise the guard. Sized to the check, not to a
                       # number that merely looked large.)
                       ({"name": "t", "variant": "bars", "values": [0.5] * 200}, "bars that cannot fit"),
                       ({"name": "t", "variant": "plain", "field": "#ZZZZZZ"}, "malformed hex colour")):
        try:
            render_card(bad, {}, canvas)
            chk(f"{label} rejected", False, "no SystemExit")
        except SystemExit:
            chk(f"{label} rejected", True)

    # The empty-values guard overlaps with the bw<1 guard (an empty list makes n=0,
    # so bw goes negative and the size check fires anyway). Both dying is correct
    # defence in depth, but "it raised" cannot tell them apart — removing the empty
    # check stayed green. Assert the MESSAGE so each guard is pinned to its own case.
    import contextlib, io
    _err = io.StringIO()
    with contextlib.redirect_stderr(_err):
        try:
            render_card({"name": "t", "variant": "bars", "values": []}, {}, canvas)
        except SystemExit:
            pass
    chk("empty values names the RIGHT error", "is empty" in _err.getvalue(), _err.getvalue().strip())

    # Two more guards that fall through to a SIBLING when removed, so "it raised"
    # cannot tell which fired: a bad hex falls to the unknown-colour die, and a
    # non-list `values` falls to the range check. Both stay loud and correct — but
    # unpinned. Assert the message, same as above.
    for bad, needle, label in (
            ({"name": "t", "variant": "plain", "field": "#ZZZZZZ"}, "malformed hex", "hex"),
            ({"name": "t", "variant": "bars", "values": "x"}, "must be a list", "non-list values")):
        _e = io.StringIO()
        with contextlib.redirect_stderr(_e):
            try:
                render_card(bad, {}, canvas)
            except SystemExit:
                pass
        chk(f"{label} names the RIGHT error", needle in _e.getvalue(), _e.getvalue().strip())

    # End-to-end spec write.
    with tempfile.TemporaryDirectory() as td:
        out = run_spec({"canvas": list(canvas), "cards": [
            {"name": "Video_99_Shot_01_c", "variant": "bars", "values": [1.0, 0.44]}]},
            Path(td))
        chk("spec run writes a PNG", out and out[0].exists() and out[0].stat().st_size > 0)

    if fails:
        print(f"\ncard_backplate selftest: FAIL ({len(fails)}): {', '.join(fails)}")
        return 1
    print("\ncard_backplate selftest: PASS")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Generate flat card backplates ($0, deterministic).")
    ap.add_argument("spec", nargs="?", help="Path to a backplate spec JSON.")
    ap.add_argument("--out-dir", help="Override the spec's out_dir.")
    ap.add_argument("--demo", metavar="DIR", help="Write one of each variant to DIR.")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        return _selftest()
    if args.demo:
        return 0 if run_spec({"cards": [
            {"name": f"demo_{v}", "variant": v,
             **({"values": [1.0, 0.44]} if v == "bars" else {})} for v in VARIANTS]},
            Path(args.demo).expanduser().resolve()) else 1
    if not args.spec:
        ap.print_usage(sys.stderr)
        return 2
    p = Path(args.spec).expanduser()
    if not p.exists():
        die(f"spec not found: {p}")
    try:
        spec = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        die(f"spec is not valid JSON: {e}")
    run_spec(spec, Path(args.out_dir).expanduser().resolve() if args.out_dir else None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
