#!/usr/bin/env python3
"""thumb_headroom_fix.py — deterministically place a thumbnail's head-top at the
locked >=28% down-frame band by shifting the frame, instead of re-rolling.

Why: gpt-image beat the head-position prompt lock FIVE consecutive times on V13
(18%, 28.6%, 19.3%, 20.1%, 21.4%) across three escalating prompt strategies —
bare percentage, compositional staging, head-size ceiling + eyebrow-to-chin
ruler. The model's "subject near the top" default wins at this composition
class. A billed re-roll is a coin flip; a pixel-measured shift is exact and $0.

Method: find the hair-top row (Three's brown curly-hair mass), then pad the TOP
with a replicated edge row (flat-color art makes this seamless) and crop the
same amount off the BOTTOM, placing the hair-top at exactly --target percent
down-frame. Canvas size is unchanged.

SAFETY: the bottom rows being cropped must be near-FLAT background. If the
subject (feet, props) reaches into the crop band, the tool REFUSES rather than
amputating content — that fired for real on V13 Thumbnail_B (full-body-on-
stairs composition; shoes+briefcase clipped, caught in review 2026-07-24).
Full-body compositions can't have both feet-in-frame and 30% headroom at >70%
character height; fix those by moving the text overlay off the top band, not
by cropping.

ORDERING: run on the RAW pre-burn render only (Raw_Assets), NEVER after
card_overlay has burned text — a shift would move burned overlays out of their
locked zones.

Usage:
  thumb_headroom_fix.py <in.png> [--target 30] [--floor 28] [--apply]
Exit codes: 0 = compliant already OR fix applied · 3 = would-fix (report-only,
no --apply/--out) · 4 = refused (no character found / bottom OR top band not
flat / degenerate pad / post-verify mismatch / non-PNG / Pillow missing);
argparse errors keep their own rc 2.
With --apply: writes in place, preserving the untouched input as <in>.orig.png
(never overwritten once created). --out writes elsewhere (must differ from the
input). --selftest runs the fixtures.

ponytail: hair-detector is tuned to Three's locked brown-curls-on-flat-field
look; if the character spec ever changes, retune HAIR_* below.
"""

import argparse
import os
import sys
import tempfile
from pathlib import Path

try:
    from PIL import Image
except ImportError:  # shebang resolves to system python (no PIL) — say so plainly
    sys.stderr.write("thumb_headroom_fix: Pillow missing — run with "
                     "/Volumes/AI_Workspace/iris_studio/.venv/bin/python\n")
    sys.exit(4)

# Brown hair-mass detector (Three's locked look: dark brown curls, R>G>B).
HAIR_RGB = dict(r_lo=50, r_hi=170, g_hi=140, rb_gap=25)
# COUNT semantics + a LOCALITY bound — deliberately NOT run/contiguity semantics.
# A contiguous-run detector reads the row where the hair mass gets WIDE, not the
# row where it STARTS: on a tapered curly crown that biased measurement +2-4pt
# (38px on the live V13 files) in the unsafe direction, silently certifying
# non-compliant frames (round-2 review catch). Count-of-hits matches the true
# top row; the compact-span + centered bound is what rejects scattered
# background specks and off-center warm-toned decoy props instead.
MIN_HITS = 5          # sampled hair hits in one CLUSTER (~20px of mass at stride 4)
SAMPLE_STRIDE = 4
MAX_SPAN_FRAC = 0.25  # a cluster must span <= this fraction of frame width
CENTER_BAND = (0.20, 0.80)  # cluster center must sit in the middle 60%
GAP_PX = 16           # max gap between neighbors within one cluster
FLAT_SPREAD = 8       # max per-channel min→max spread for a "flat background" band

RC_OK, RC_WOULD_FIX, RC_REFUSE = 0, 3, 4


def _is_hair(rgb) -> bool:
    r, g, b = rgb[:3]
    c = HAIR_RGB
    return (c["r_lo"] < r < c["r_hi"] and r > g > b
            and (r - b) > c["rb_gap"] and g < c["g_hi"])


def hair_top_row(im: Image.Image) -> int | None:
    """First row where SOME compact, centered hair cluster exists, or None.

    EXISTS-a-cluster, deliberately not ALL-hits-form-one-cluster: an off-side
    warm region in the same row must not widen the measured span and SUPPRESS
    detection at the true crown top — suppression under-shoots the floor
    silently (round-3 review catch). Governing principle: prefer mis-anchoring
    over mis-suppression — an early false anchor only ever OVER-shoots (lands
    the head below target, never below the floor, and the top-flat pad guard
    nets most such frames); a suppressed row is a silent floor violation.
    Hits are split into clusters at gaps > GAP_PX; each cluster must hold
    >= MIN_HITS, span <= MAX_SPAN_FRAC of width, and center in CENTER_BAND."""
    rgb = im.convert("RGB")
    px = rgb.load()
    W, H = rgb.size
    max_span = W * MAX_SPAN_FRAC
    lo, hi = W * CENTER_BAND[0], W * CENTER_BAND[1]
    for y in range(H):
        xs = [x for x in range(0, W, SAMPLE_STRIDE) if _is_hair(px[x, y])]
        if len(xs) < MIN_HITS:
            continue
        clusters = [[xs[0]]]
        for x in xs[1:]:
            if x - clusters[-1][-1] <= GAP_PX:
                clusters[-1].append(x)
            else:
                clusters.append([x])
        for c in clusters:
            if len(c) >= MIN_HITS and (c[-1] - c[0]) <= max_span \
                    and lo <= (c[0] + c[-1]) / 2 <= hi:
                return y
    return None


def band_is_flat(im: Image.Image, y0: int, y1: int) -> bool:
    """True when rows y0..y1 are near-uniform background (safe to crop away)."""
    if y0 >= y1:
        return True
    band = im.convert("RGB").crop((0, y0, im.size[0], y1))
    extrema = band.getextrema()  # ((rmin,rmax),(gmin,gmax),(bmin,bmax))
    return all(hi - lo <= FLAT_SPREAD for lo, hi in extrema)


def shift_down(im: Image.Image, pad: int) -> Image.Image:
    """Pad the top by replicating row 0, crop the same off the bottom.
    Mode-preserving; caller guarantees pad > 0 and a flat bottom band."""
    W, H = im.size
    top = im.crop((0, 0, W, 1)).resize((W, pad))
    out = Image.new(im.mode, (W, H))
    out.paste(top, (0, 0))
    out.paste(im.crop((0, 0, W, H - pad)), (0, pad))
    return out


def _atomic_save(im: Image.Image, dest: Path) -> None:
    fd, tmp = tempfile.mkstemp(suffix=".png", dir=str(dest.parent))
    os.close(fd)
    try:
        im.save(tmp, format="PNG")
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Deterministic head-top placement for thumbnails.")
    ap.add_argument("png", nargs="?", help="thumbnail PNG (raw pre-burn render)")
    ap.add_argument("--target", type=float, default=30.0, help="target head-top %% down-frame (default 30)")
    ap.add_argument("--floor", type=float, default=28.0, help="compliance floor %% (default 28)")
    ap.add_argument("--apply", action="store_true", help="write the fix (in place; keeps .orig.png)")
    ap.add_argument("--out", help="write fixed image here instead of in place")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()
    if not args.png:
        ap.error("png required (or --selftest)")
    if not (0 < args.floor <= args.target < 100):
        ap.error(f"need 0 < floor <= target < 100 (got floor={args.floor}, target={args.target})")

    p = Path(args.png)
    if args.out and Path(args.out).resolve() == p.resolve():
        ap.error("--out must differ from the input (use --apply for in-place)")
    if not p.exists():
        print(f"{p}: not found", file=sys.stderr)
        return RC_REFUSE
    im = Image.open(p)
    im.load()
    if (im.format or "PNG") != "PNG":
        print(f"{p.name}: {im.format} input — refusing (PNG only; re-encoding would lose data).",
              file=sys.stderr)
        return RC_REFUSE
    H = im.size[1]
    row = hair_top_row(im)
    if row is None:
        print(f"{p.name}: no hair mass found — not a Three character frame? refusing.",
              file=sys.stderr)
        return RC_REFUSE
    pct = row / H * 100
    if pct >= args.floor:
        print(f"{p.name}: head-top {pct:.1f}% — already >= {args.floor:.0f}% floor, no fix needed.")
        return RC_OK

    pad = round((args.target / 100) * H) - row
    if pad <= 0:  # arg validation makes this unreachable; belt for future edits
        print(f"{p.name}: degenerate pad {pad} — refusing.", file=sys.stderr)
        return RC_REFUSE
    if not band_is_flat(im, H - pad, H):
        print(f"{p.name}: head-top {pct:.1f}% needs a {pad}px shift, but the bottom {pad}px "
              f"band holds real content (feet/props) — REFUSING to amputate. Full-body "
              f"compositions can't fit {args.target:.0f}% headroom; move the text overlay "
              f"off the top band instead, or re-frame the shot.", file=sys.stderr)
        return RC_REFUSE
    if not band_is_flat(im, 0, pad):
        # Symmetric guard: the top pad replicates row 0 — content touching the
        # top edge would smear into a vertical streak.
        print(f"{p.name}: the top {pad}px band holds content at the frame edge — "
              f"row-replication padding would smear it. REFUSING.", file=sys.stderr)
        return RC_REFUSE
    print(f"{p.name}: head-top {pct:.1f}% < floor {args.floor:.0f}% — shift down {pad}px -> {args.target:.0f}%")
    if not args.apply and not args.out:
        return RC_WOULD_FIX

    fixed = shift_down(im, pad)
    check = hair_top_row(fixed)
    if check != row + pad:  # the landmark that moved must be the landmark we measured
        got = f"{check / H * 100:.1f}%" if check is not None else "none"
        print(f"{p.name}: post-fix verify FAILED (expected row {row + pad}, measured {got}) "
              f"— not writing.", file=sys.stderr)
        return RC_REFUSE
    if args.out:
        _atomic_save(fixed, Path(args.out))
        print(f"  wrote {args.out} (verified head-top {check / H * 100:.1f}%)")
    else:
        orig = p.with_suffix(".orig.png")
        if not orig.exists():
            p.rename(orig)
        _atomic_save(fixed, p)
        print(f"  wrote {p.name} in place (verified {check / H * 100:.1f}%); original kept at {orig.name}")
    return RC_OK


# --- selftest ----------------------------------------------------------------
BG = (31, 42, 51)
HAIR = (90, 64, 48)


def _synth(head_pct: float, feet: bool = False) -> Image.Image:
    im = Image.new("RGB", (512, 288), BG)
    im.paste(Image.new("RGB", (120, 60), HAIR), (196, round(head_pct / 100 * 288)))
    if feet:  # content reaching the bottom edge (the Thumbnail_B class)
        im.paste(Image.new("RGB", (60, 40), (10, 10, 10)), (220, 248))
    return im


def selftest() -> int:
    import hashlib
    checks = []

    def ok(name, cond):
        checks.append((name, bool(cond)))

    # detection accuracy (exact-row)
    for pct in (10, 18, 28, 40):
        want = round(pct / 100 * 288)
        ok(f"detect@{pct}%", hair_top_row(_synth(pct)) == want)
    # blank frame + sub-run speck must both refuse detection
    ok("blank->None", hair_top_row(Image.new("RGB", (512, 288), BG)) is None)
    speck = Image.new("RGB", (512, 288), BG)
    for x in range(0, 512, 64):  # scattered non-contiguous brown pixels
        speck.putpixel((x, 100), HAIR)
    ok("speck->None", hair_top_row(speck) is None)
    # TAPERED + irregularly-textured crown (the round-2 bias class): the hair
    # narrows toward its top and highlight gaps interrupt rows irregularly. The
    # detector must land within 1 row of the TRUE top (where >= MIN_HITS of
    # mass first exists), not the row where the mass gets wide — a run-semantics
    # detector reads 20-40px low here and certifies non-compliant frames.
    tap = Image.new("RGB", (512, 288), BG)
    trow = round(0.20 * 288)
    for dy in range(40):                       # widths taper 24px -> 180px
        half = 12 + dy * 2
        for x in range(256 - half, 256 + half):
            if (x * 7 + dy * 3) % 5 != 0:      # irregular texture gaps
                tap.putpixel((x, trow + dy), HAIR)
    ok("tapered-crown top-row accuracy", abs(hair_top_row(tap) - trow) <= 1)
    # off-center warm decoy (wood shelf at the frame edge) must NOT anchor
    dec = Image.new("RGB", (512, 288), BG)
    dec.paste(Image.new("RGB", (100, 20), (150, 110, 70)), (0, 20))   # edge prop
    dec.paste(Image.new("RGB", (120, 60), HAIR), (196, round(0.20 * 288)))
    ok("edge-decoy ignored", hair_top_row(dec) == round(0.20 * 288))
    # wide scattered warm band (dappled background) must NOT anchor: span bound
    wide = Image.new("RGB", (512, 288), BG)
    for x in range(0, 512, 24):
        wide.paste(Image.new("RGB", (8, 8), HAIR), (x, 30))
    wide.paste(Image.new("RGB", (120, 60), HAIR), (196, round(0.20 * 288)))
    ok("wide-band ignored", hair_top_row(wide) == round(0.20 * 288))
    # top-edge content refuses (symmetric pad guard)
    ok("top-band-not-flat", not band_is_flat(dec, 0, 60))
    # SUPPRESSION class (round-3): warm content above AND beside the crown must
    # not widen the row span and suppress detection at the true crown top
    sup = Image.new("RGB", (512, 288), BG)
    sup.paste(Image.new("RGB", (80, 120), (150, 110, 70)), (0, 40))  # side panel
    strow = round(0.20 * 288)
    sup.paste(Image.new("RGB", (120, 60), HAIR), (196, strow))
    ok("side-panel no suppression", hair_top_row(sup) == strow)
    # a single compact stray blob below MIN_HITS must not anchor (pins MIN_HITS)
    stray = Image.new("RGB", (512, 288), BG)
    stray.paste(Image.new("RGB", (8, 8), HAIR), (250, 30))  # 2 samples < MIN_HITS
    stray.paste(Image.new("RGB", (120, 60), HAIR), (196, strow))
    ok("tiny-stray ignored", hair_top_row(stray) == strow)
    # shift correctness — EXACT row equality, both off-by-one mutants die here
    im = _synth(18)
    row = hair_top_row(im)
    pad = round(0.30 * 288) - row
    ok("shift-exact", hair_top_row(shift_down(im, pad)) == row + pad)
    # flat-band guard
    ok("flat-band", band_is_flat(_synth(18), 288 - pad, 288))
    ok("feet-band-not-flat", not band_is_flat(_synth(18, feet=True), 288 - 60, 288))

    # main() round-trips in a tmpdir
    import tempfile as tf
    with tf.TemporaryDirectory() as d:
        pth = Path(d) / "t.png"
        _synth(18).save(pth)
        ok("report-only rc3", main([str(pth)]) == RC_WOULD_FIX)
        ok("apply rc0", main([str(pth), "--apply"]) == RC_OK)
        orig = pth.with_suffix(".orig.png")
        ok("orig created", orig.exists())
        h1 = hashlib.md5(orig.read_bytes()).hexdigest() if orig.exists() else None
        ok("2nd apply rc0 (floor guard)", main([str(pth), "--apply"]) == RC_OK)
        ok("orig untouched", orig.exists()
           and hashlib.md5(orig.read_bytes()).hexdigest() == h1)
        # compliant frame: pure no-op
        c = Path(d) / "c.png"
        _synth(35).save(c)
        ok("compliant rc0", main([str(c), "--apply"]) == RC_OK)
        ok("compliant no orig", not c.with_suffix(".orig.png").exists())
        # feet in the crop band → REFUSE, file untouched
        f = Path(d) / "f.png"
        _synth(18, feet=True).save(f)
        before = hashlib.md5(f.read_bytes()).hexdigest()
        ok("feet refuse rc4", main([str(f), "--apply"]) == RC_REFUSE)
        ok("feet file untouched", hashlib.md5(f.read_bytes()).hexdigest() == before)
        ok("feet no orig", not f.with_suffix(".orig.png").exists())
        # blank frame → refuse
        blank = Path(d) / "b.png"
        Image.new("RGB", (512, 288), BG).save(blank)
        ok("blank refuse rc4", main([str(blank), "--apply"]) == RC_REFUSE)
        # content touching the TOP edge → refuse (pins the top-flat guard in main)
        tg = Path(d) / "tg.png"
        timg = _synth(18)
        timg.paste(Image.new("RGB", (60, 20), (212, 175, 55)), (220, 0))  # top badge
        timg.save(tg)
        tbefore = hashlib.md5(tg.read_bytes()).hexdigest()
        ok("top-edge refuse rc4", main([str(tg), "--apply"]) == RC_REFUSE)
        ok("top-edge file untouched", hashlib.md5(tg.read_bytes()).hexdigest() == tbefore)
        # RGBA mode preserved through an apply
        ra = Path(d) / "a.png"
        _synth(18).convert("RGBA").save(ra)
        ok("rgba apply rc0", main([str(ra), "--apply"]) == RC_OK)
        ok("rgba preserved", Image.open(ra).mode == "RGBA")

    bad = [n for n, c in checks if not c]
    print(f"thumb_headroom_fix --selftest: {'PASS' if not bad else 'FAIL'} "
          f"({len(checks) - len(bad)}/{len(checks)})" + (f" failed: {bad}" if bad else ""))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
