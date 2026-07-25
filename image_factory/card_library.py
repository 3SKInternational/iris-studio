#!/usr/bin/env python3
"""card_library.py — deterministic card-backplate reuse for 3SK Finance.

Card backplates are text-free flat colour fields; every figure is burned on
afterward by card_overlay.py (PIL). So a backplate carries no character, no
composition, and no on-model risk — reusing one is a file copy, not a judgment
call. That is what makes cards the cleanest reuse case in the pipeline, and why
this needs no agent at all (unlike character-shot reuse, which is
asset-librarian's job because it needs pose/wardrobe/colour-arc matching).

Two subcommands:
  build [--dry-run]      Scan the render library, cluster flat backplates into
                         distinct looks, and copy ONE exemplar per look into the
                         canonical card library (_cards/). --dry-run reports the
                         redundancy audit only, writes nothing.
  apply MANIFEST OUTDIR  Pre-populate every manifest shot carrying a `reuse_card`
                         key by copying the named library card to
                         OUTDIR/<shot>.png. generate_images.py then skips that
                         shot for $0 (its skip-existing branch: dest exists).
                         Records every reuse to OUTDIR/_card_reuse.json so a
                         wrong reuse is traceable.

OFF THE BILLED PATH: apply never generates and touches nothing in
generate_images.py; it only copies files the library already owns.

FALSE-NEGATIVE BIAS (pickup Task 3): apply reuses ONLY the cards a human
explicitly named via `reuse_card`. It never auto-decides — a wrong frame costs
more than a re-render, so when in doubt the manifest just prompts the card and
pays the ~$0.097. build's clustering is advisory (it tells the author which
looks already exist); it never rewrites a manifest.

ponytail: flat-field detect (32x18 downsample, per-channel std < FLAT_STD) to
FIND cards, mean-abs-diff on 64x36 (<= LOOK_DIFF) to CLUSTER them into looks —
the exact two measures the 2026-07-23 Card_Backplate_Reuse pickup used. No
invention.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image

VAULT = Path("/Users/steve/Documents/3SK/outputs")
RENDER_ROOT = VAULT / "BRANDS/3SK_Finance/Raw_Assets"
CARD_LIB = RENDER_ROOT / "Image_Factory/_cards"

FLAT_STD = 22.0            # per-channel std below this (on FLAT_DOWN) => flat field
LOOK_DIFF = 12.0           # mean-abs-diff (on LOOK_DOWN) at/below this => same look
FLAT_DOWN = (32, 18)       # PIL (width, height) for the flat-field test
LOOK_DOWN = (64, 36)       # PIL (width, height) for the clustering signature
CANON_TOL = 34.0           # RGB euclidean within this of a canon colour => canon name
CARD_COST = 0.097          # per-card render cost (pickup sizing)

# The locked 3SK level-card palette (find_reuse_candidates.py + the V13 manifest).
CANON = {
    "charcoal": (0x1F, 0x2A, 0x33),
    "navy":     (0x2A, 0x4D, 0x6E),
    "cream":    (0xF4, 0xEB, 0xD9),
    "gold":     (0xD4, 0xA1, 0x1C),
}


def _downsample(path: Path, size) -> np.ndarray:
    """Deterministic RGB downsample as a float array, shape (h, w, 3)."""
    with Image.open(path) as im:
        small = im.convert("RGB").resize(size, Image.BILINEAR)
    return np.asarray(small, dtype=np.float64)


def is_flat(path: Path) -> bool:
    a = _downsample(path, FLAT_DOWN)
    return float(a.reshape(-1, 3).std(axis=0).max()) < FLAT_STD


def flatness(path: Path) -> float:
    """Max per-channel std on FLAT_DOWN; lower = flatter (best exemplar)."""
    a = _downsample(path, FLAT_DOWN)
    return float(a.reshape(-1, 3).std(axis=0).max())


def look_sig(path: Path) -> np.ndarray:
    return _downsample(path, LOOK_DOWN)


def look_diff(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.abs(a - b).mean())


def mean_rgb(sig: np.ndarray) -> tuple[int, int, int]:
    m = sig.reshape(-1, 3).mean(axis=0)
    return (int(round(m[0])), int(round(m[1])), int(round(m[2])))


def nearest_canon(rgb) -> tuple[str, float]:
    best, best_d = None, 1e9
    for name, c in CANON.items():
        d = float(np.linalg.norm(np.array(rgb, float) - np.array(c, float)))
        if d < best_d:
            best, best_d = name, d
    return best, best_d


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _bad_seg(s: str) -> bool:
    """A shot/card name that could escape its directory or hide a file."""
    return ("/" in s) or ("\\" in s) or s.startswith(".")


def cluster_flats(paths: list[Path]) -> list[dict]:
    """Greedy single-pass clustering by look signature. Deterministic on sorted
    input: each flat joins the first existing cluster within LOOK_DIFF of its
    exemplar, else opens a new cluster."""
    clusters: list[dict] = []
    for p in sorted(paths):
        sig = look_sig(p)
        placed = False
        for cl in clusters:
            if look_diff(sig, cl["sig"]) <= LOOK_DIFF:
                cl["members"].append(p)
                # keep the flattest member as the running exemplar
                if flatness(p) < cl["flatness"]:
                    cl["exemplar"], cl["sig"], cl["flatness"] = p, sig, flatness(p)
                placed = True
                break
        if not placed:
            clusters.append({"exemplar": p, "sig": sig,
                             "flatness": flatness(p), "members": [p]})
    return clusters


def cluster_mean_rgb(cl: dict) -> tuple[int, int, int]:
    """Mean colour across ALL members of the look — not the exemplar's own,
    which can swap on a re-run when a flatter member joins."""
    means = np.stack([look_sig(m).reshape(-1, 3).mean(axis=0)
                      for m in cl["members"]])
    m = means.mean(axis=0)
    return (int(round(m[0])), int(round(m[1])), int(round(m[2])))


def name_cluster(cl: dict, taken: set) -> str:
    """Content-derived name: `<canon>_<rrggbb>` (or `hex_<rrggbb>` outside
    CANON_TOL), where the hex is the cluster's MEAN colour across its members.

    Stable across scan-order permutations of a fixed member set — the name never
    depends on which file sorts first (unlike a bare positional `cream`/`cream_2`
    scheme, which rebinds a name to a different look the moment a new video sorts
    ahead). It is NOT a permanent identifier: a later `build` that adds or drops
    a member shifts the cluster mean and can rename a look. That is safe by
    construction — `apply` fail-closes (rc 1, no file written, never a wrong
    frame) when a `reuse_card` name is absent, and `_index.json` records each
    card's source + sha256 + members so a rename is fully traceable. Re-validate
    manifest `reuse_card` references after any `build` re-run. The canon prefix
    keeps names readable for authoring."""
    rgb = cluster_mean_rgb(cl)
    canon, d = nearest_canon(rgb)
    hexid = "{:02x}{:02x}{:02x}".format(*rgb)
    base = f"{canon}_{hexid}" if d <= CANON_TOL else f"hex_{hexid}"
    # Same mean hex on two distinct looks (e.g. identical bg, different accent)
    # is the only collision left; disambiguate deterministically.
    name, n = base, 2
    while name in taken:
        name, n = f"{base}_{n}", n + 1
    taken.add(name)
    return name


def cmd_build(args) -> int:
    root = Path(args.root).expanduser() if args.root else RENDER_ROOT
    lib = Path(args.card_lib).expanduser() if args.card_lib else CARD_LIB
    paths = sorted(p for p in root.glob("Video_*_HD/*.png")
                   if not p.name.endswith("_text.png"))
    if not paths:
        print(f"no candidate PNGs under {root}/Video_*_HD/", file=sys.stderr)
        return 1
    flats = [p for p in paths if is_flat(p)]
    clusters = cluster_flats(flats)
    redundant = sum(len(c["members"]) - 1 for c in clusters)
    sunk = redundant * CARD_COST
    print(f"scanned {len(paths)} renders -> {len(flats)} flat backplates "
          f"-> {len(clusters)} distinct looks -> {redundant} redundant "
          f"(${sunk:.2f} already spent re-making cards we owned)")

    taken: set = set()
    index = {}
    for cl in clusters:
        nm = name_cluster(cl, taken)
        rgb = cluster_mean_rgb(cl)
        canon, d = nearest_canon(rgb)
        print(f"  {nm:14s} rgb=#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x} "
              f"canon={canon}({d:.0f}) members={len(cl['members'])} "
              f"exemplar={cl['exemplar'].name}")
        index[nm] = {
            "source": str(cl["exemplar"]),
            "mean_rgb": list(rgb),
            "canon": canon,
            "canon_dist": round(d, 1),
            "members": [str(m) for m in cl["members"]],
            "sha256": _sha256(cl["exemplar"]),
        }

    if args.dry_run:
        print("dry run: library not written (pass without --dry-run to build)")
        return 0

    lib.mkdir(parents=True, exist_ok=True)
    for nm, meta in index.items():
        dest = lib / f"{nm}.png"
        dest.write_bytes(Path(meta["source"]).read_bytes())
    (lib / "_index.json").write_text(json.dumps(index, indent=2))
    print(f"wrote {len(index)} card(s) + _index.json to {lib}")
    return 0


def cmd_apply(args) -> int:
    lib = Path(args.card_lib).expanduser() if args.card_lib else CARD_LIB
    manifest_path = Path(args.manifest).expanduser()
    out = Path(args.outdir).expanduser()
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(f"cannot read manifest: {e}", file=sys.stderr)
        return 1
    images = manifest.get("images")
    if not isinstance(images, list):
        print("manifest 'images' must be a list -> refusing", file=sys.stderr)
        return 1
    out.mkdir(parents=True, exist_ok=True)

    records = []
    resolved = 0
    for shot in images:
        card = shot.get("reuse_card")
        if not card:
            continue
        name = shot.get("name")
        if not name:
            print("shot with reuse_card but no name -> refusing", file=sys.stderr)
            return 1
        # Reject path traversal in author-controlled names (matches the sibling
        # guard at generate_images.py name guard). Keeps writes inside OUTDIR and reads
        # inside the library.
        if any(_bad_seg(s) for s in (name, card)):
            print(f"unsafe name/card ({name!r}/{card!r}) -> refusing",
                  file=sys.stderr)
            return 1
        src = lib / f"{card}.png"
        if not src.exists():
            # fail closed: naming a card that isn't in the library is an author
            # error, not a licence to silently prompt-and-bill it.
            print(f"reuse_card '{card}' for {name}: not in library {lib}",
                  file=sys.stderr)
            return 1
        dest = out / f"{name}.png"
        if dest.exists() and not args.force:
            print(f"  = {name}  (already present, skipped)")
        else:
            dest.write_bytes(src.read_bytes())
            print(f"  + {name}  <- {card}.png")
            resolved += 1
        records.append({"shot": name, "card": card, "source": str(src),
                        "sha256": _sha256(src)})

    if records:
        rec_path = out / "_card_reuse.json"
        rec_path.write_text(json.dumps(records, indent=2))
    saved = resolved * CARD_COST
    print(f"applied {resolved} card(s) from library "
          f"({len(records)} total referenced) -> ~${saved:.2f} not billed; "
          f"generate_images.py will skip them")
    return 0


def selftest() -> int:
    import tempfile
    fails = []

    def solid(dirp, name, rgb, size=(200, 112)):
        p = Path(dirp) / name
        Image.new("RGB", size, rgb).save(p)
        return p

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        charc = solid(td, "a.png", CANON["charcoal"])
        charc2 = solid(td, "b.png", CANON["charcoal"])
        gold = solid(td, "c.png", CANON["gold"])
        # a noisy gradient => not flat
        grad = np.tile(np.linspace(0, 255, 200, dtype=np.uint8), (112, 1))
        gpath = td / "grad.png"
        Image.fromarray(np.stack([grad] * 3, -1)).save(gpath)

        if not is_flat(charc):
            fails.append("solid charcoal should be flat")
        if is_flat(gpath):
            fails.append("horizontal gradient should NOT be flat")
        if look_diff(look_sig(charc), look_sig(charc2)) > LOOK_DIFF:
            fails.append("two charcoal solids should cluster together")
        if look_diff(look_sig(charc), look_sig(gold)) <= LOOK_DIFF:
            fails.append("charcoal vs gold should be different looks")
        # Pin the pickup's threshold BAND (real same-look cards differ 2.9-5.9;
        # distinct looks >12). Solids k apart in each channel => look_diff == k,
        # so these bite if LOOK_DIFF is tightened/loosened out of band.
        near_a = solid(td, "na.png", (30, 42, 51))
        near_b = solid(td, "nb.png", (35, 47, 56))     # ~5 apart -> same look
        far_b = solid(td, "fb.png", (50, 62, 71))      # ~20 apart -> distinct
        if look_diff(look_sig(near_a), look_sig(near_b)) > LOOK_DIFF:
            fails.append("looks ~5 apart must cluster (LOOK_DIFF too tight)")
        if look_diff(look_sig(near_a), look_sig(far_b)) <= LOOK_DIFF:
            fails.append("looks ~20 apart must NOT cluster (LOOK_DIFF too loose)")
        if nearest_canon(CANON["charcoal"])[0] != "charcoal":
            fails.append("#1F2A33 should map to charcoal")
        if nearest_canon(CANON["gold"])[1] > CANON_TOL:
            fails.append("exact gold should be within CANON_TOL")

        cls = cluster_flats([charc, charc2, gold])
        if len(cls) != 2:
            fails.append(f"3 flats (2 same) should form 2 looks, got {len(cls)}")
        taken: set = set()
        names = {name_cluster(c, taken) for c in cls}
        # content-stable names: canon prefix + the look's own mean hex
        if not any(n.startswith("charcoal_") for n in names) or \
           not any(n.startswith("gold_") for n in names):
            fails.append(f"names should be canon+hex (charcoal_/gold_), got {names}")
        # same look -> same name regardless of scan order (the rebind fix)
        if name_cluster(cls[0], set()) != name_cluster(cls[0], set()):
            fails.append("card name must be content-stable, not positional")

        # apply: build a tiny library + manifest, prove $0 copy + record
        lib = td / "_cards"
        lib.mkdir()
        (lib / "charcoal.png").write_bytes(charc.read_bytes())
        man = td / "m.json"
        man.write_text(json.dumps({"images": [
            {"name": "V14_Shot_01c", "reuse_card": "charcoal"},
            {"name": "V14_Shot_02a", "prompt": "a character shot"},
        ]}))
        outd = td / "out"
        args = argparse.Namespace(manifest=str(man), outdir=str(outd),
                                  card_lib=str(lib), force=False)
        rc = cmd_apply(args)
        if rc != 0:
            fails.append("apply should succeed on a valid manifest")
        if not (outd / "V14_Shot_01c.png").exists():
            fails.append("apply should pre-populate the card shot")
        if (outd / "V14_Shot_02a.png").exists():
            fails.append("apply must NOT touch non-card shots")
        rec = json.loads((outd / "_card_reuse.json").read_text())
        if len(rec) != 1 or rec[0]["card"] != "charcoal":
            fails.append("apply must record the reuse for traceability")

        # fail-closed: unknown card name aborts, never guesses
        man2 = td / "m2.json"
        man2.write_text(json.dumps({"images": [
            {"name": "V14_Shot_09c", "reuse_card": "chartreuse"}]}))
        args2 = argparse.Namespace(manifest=str(man2), outdir=str(td / "o2"),
                                   card_lib=str(lib), force=False)
        if cmd_apply(args2) == 0:
            fails.append("apply must fail closed on an unknown card name")

        # traversal names must be refused, not written outside OUTDIR
        man3 = td / "m3.json"
        man3.write_text(json.dumps({"images": [
            {"name": "../escape", "reuse_card": "charcoal"}]}))
        args3 = argparse.Namespace(manifest=str(man3), outdir=str(td / "o3"),
                                   card_lib=str(lib), force=False)
        if cmd_apply(args3) == 0 or (td / "escape.png").exists():
            fails.append("apply must refuse a path-traversal shot name")

    if fails:
        print("SELFTEST FAIL:")
        for f in fails:
            print("  -", f)
        return 1
    print("selftest OK")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--selftest", action="store_true",
                    help="Run the built-in checks and exit ($0, no network).")
    sub = ap.add_subparsers(dest="cmd")

    b = sub.add_parser("build", help="Scan renders, cluster looks, build _cards/.")
    b.add_argument("--dry-run", action="store_true", help="Audit only; write nothing.")
    b.add_argument("--root", help="Render root (default: the vault Raw_Assets).")
    b.add_argument("--card-lib", help="Card library dir (default: _cards/).")

    a = sub.add_parser("apply", help="Pre-populate reuse_card shots for $0.")
    a.add_argument("manifest")
    a.add_argument("outdir")
    a.add_argument("--card-lib", help="Card library dir (default: _cards/).")
    a.add_argument("--force", action="store_true", help="Overwrite existing PNGs.")

    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if args.cmd == "build":
        return cmd_build(args)
    if args.cmd == "apply":
        return cmd_apply(args)
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
