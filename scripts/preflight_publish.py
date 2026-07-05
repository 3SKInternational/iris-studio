#!/usr/bin/env python3
"""Deterministic pre-upload signoff gate for 3SK Finance videos.

The gap this closes shipped V6: its FINAL signoff was written PRE-render, then the
fix batch regenerated images and re-assembled the cut, and nothing re-verified the
final video before it was cleared for upload. No agent can *watch* a 20-minute mp4,
so this gate checks the two things that ARE machine-verifiable and were the actual
risk: is the cut COMPLETE, and is it FRESH (nothing regenerated after it was built).

Checks (each one FAILS the gate — fail-closed):
  1. the rendered mp4 exists;
  2. an edit manifest for this video exists and every shot image + VO clip it
     references exists on disk;
  3. NO content input (a shot image or a VO clip) is newer than the mp4 — a newer
     input means the mp4 is a STALE assembly of now-changed assets (the V6 gap).
     The manifest file's own mtime is deliberately ignored: build_video re-authors
     it on every invocation (even plan-only), so it's routinely newer than the mp4
     without the pixels having changed;
  4. the description pack exists;
  5. a thumbnail resolves (the file the uploader will actually set);
  6. every VO clip the cut references is at least as new as the VO kit that
     should have produced it (Voice_Files/<vid>/_VO_Session_B_Kit.md) — an mp3
     OLDER than its kit is retired narration a skip-existing VO run kept alive
     (the V7 gap 2026-07-04: stale VO is older than the mp4 too, so check 3
     can't see it). No kit on disk → warn-only (freshness unverifiable).
  7. no thumbnail overlay restates a dollar VALUE the locked title already
     carries — the Pairing Principle CTR-drop pattern (title + thumb answering
     the SAME dollar question). The overlay text lives machine-readably in
     image_factory/manifests/<vid>_thumbnail_overlay.json; the title mirrors
     upload_video's precedence (desc-pack `youtube_title:` frontmatter, else the
     NEWEST Packaging_<vid>*.md ⭐ recommended title). A violation on the
     THUMBNAIL THAT WILL BE UPLOADED (the one preflight resolves) FAILS; an
     A/B *alternate*
     only warns (Steve may intentionally test it). Spec or title unparseable →
     warn-only. This is the V8 gap 2026-07-04: B burned "$1,650 → $10M" against
     a title already carrying "($0–$10M)" and nothing between the packaging doc
     and the built thumbnail caught it. The check is deliberately narrow — a
     shared dollar VALUE (a title endpoint restated), NOT "the overlay has any
     dollar", so a distinct in-range stakes figure (e.g. "$340") stays clean.
Warn-only (never fails the gate): a missing .srt caption file — the uploader
already treats captions as best-effort.

On PASS it writes a dated signoff stamp under Footage_and_Edits/_preflight/ and
returns ok=True. Imported by upload_video.py (fail-closed before any network) and
runnable standalone:

  python3 scripts/preflight_publish.py Video_06            # check + stamp
  python3 scripts/preflight_publish.py Video_06 --json     # machine-readable
  python3 scripts/preflight_publish.py --selftest          # logic self-check
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MANIFEST_DIR = REPO / "video_factory" / "manifests"
THUMB_OVERLAY_DIR = REPO / "image_factory" / "manifests"
DEFAULT_VAULT = "~/Documents/3SK/outputs/BRANDS/3SK_Finance"

# A dollar figure, with an optional K/M/B magnitude suffix: $0, $1,650, $10M, $10.03M.
# The `\b` after the suffix keeps a following word from becoming a multiplier —
# "$500 MISTAKE" is $500, not $500M (overlays are all-caps by house style).
_DOLLAR_RE = re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)(?:\s?([KkMmBb])\b)?")
_MULT = {"k": 1e3, "m": 1e6, "b": 1e9}


def _dollar_values(text: str) -> set[int]:
    """Normalized integer dollar values in `text` ($10M and $10,000,000 → 10000000).

    Used to detect a thumbnail overlay RESTATING a value the title already owns.
    Magnitude-normalized so "$10M" (title) and "$10,000,000" (overlay) compare equal.
    """
    out: set[int] = set()
    for num, suf in _DOLLAR_RE.findall(text):
        val = float(num.replace(",", "")) * (_MULT[suf.lower()] if suf else 1)
        out.add(round(val))
    return out


def _pkg_version(path: Path) -> int:
    """Sort key for packaging docs: Packaging_Video_08_v4.md → 4; base or any
    non-numeric suffix (_repackage) → 1. Highest = newest recommended package."""
    m = re.search(r"_v(\d+)\.md$", path.name)
    return int(m.group(1)) if m else 1


def _title_from_packaging(pkg: Path) -> str | None:
    """The ⭐ recommended title in a packaging doc. Repackage (_vN) docs carry
    more than one `**Title:**` (recommended + alts), so anchor to the text AFTER
    the ⭐ marker; the variants TABLE uses `| # | Title |` so it can't be picked
    up. Fall back to the first `**Title:**` only if there's no ⭐ section."""
    text = pkg.read_text(encoding="utf-8")
    star = text.find("⭐")
    scope = text[star:] if star != -1 else text
    m = re.search(r'\*\*Title:\*\*\s*"([^"]+)"', scope)
    return m.group(1) if m else None


def _locked_title(vlt: Path, vid: str, desc_pack: Path) -> str | None:
    """The title the thumbnail overlay is checked against, or None if none found.

    Mirrors upload_video's real precedence: the desc-pack frontmatter
    `youtube_title:` (what upload_video actually SETS as the title) wins; else the
    NEWEST packaging doc's ⭐ recommended title (a suggestion only, but the best
    pre-upload signal). Reading only base Packaging_<vid>.md would check a
    repackaged video against a RETIRED title — a silent fail-open (V5's base title
    is dollar-free while its _v4 carries "$100 to $1,000,000")."""
    if desc_pack.is_file():
        m = re.search(r'(?m)^youtube_title:\s*"?(.+?)"?\s*$',
                      desc_pack.read_text(encoding="utf-8"))
        if m and m.group(1).strip():
            return m.group(1).strip()
    pkgs = sorted((vlt / "Packaging").glob(f"Packaging_{vid}*.md"),
                  key=_pkg_version, reverse=True)
    for pkg in pkgs:
        t = _title_from_packaging(pkg)
        if t:
            return t
    return None


def _thumb_card_key(thumb_stem: str) -> str:
    """The overlay-spec card key for a resolved thumbnail file
    (Video_08_Thumbnail_A_FINAL → Video_08_Thumbnail_A)."""
    for suf in ("_FINAL", "_text"):
        if thumb_stem.endswith(suf):
            thumb_stem = thumb_stem[: -len(suf)]
    return thumb_stem


def vault() -> Path:
    return Path(os.path.expanduser(os.environ.get("SK_VAULT", DEFAULT_VAULT))).resolve()


def normalize_id(raw: str) -> str:
    m = re.search(r"(\d+)", raw)
    if not m:
        raise ValueError(f"could not parse a video number from '{raw}'")
    return f"Video_{int(m.group(1)):02d}"


def _select_edit_manifest(vid: str, mp4_stem: str) -> Path | None:
    """The edit manifest that built the mp4 on disk, or None.

    Prefer the manifest whose ``output_name`` matches the mp4 stem (e.g.
    ``Video_06_v2``) — that's the cut's identity, not a timestamp. mtime is only a
    tiebreaker among identity-matches, because the gate elsewhere (correctly)
    distrusts mtime: build_video re-authors manifests on every invocation. Fall
    back to newest-by-mtime only if none declares a matching output_name (older
    manifests predating the output_name field).
    """
    cands = sorted(MANIFEST_DIR.glob(f"{vid}_orchestrated_*.json"),
                   key=lambda p: p.stat().st_mtime)
    if not cands:
        return None
    matches = []
    for p in cands:
        try:
            if json.loads(p.read_text(encoding="utf-8")).get("output_name") == mp4_stem:
                matches.append(p)
        except (json.JSONDecodeError, OSError):
            continue
    return (matches or cands)[-1]  # newest identity-match, else newest overall


def _vo_kit_for(vlt: Path, clip_rel: str) -> Path:
    """The VO kit that should have produced this clip: the clip's folder with any
    `_gen` suffix stripped is the kit folder (generate_vo renders Voice_Files/
    <vid>/_VO_Session_B_Kit.md into Voice_Files/<vid>_gen/; hand-recorded sets
    keep the kit in the clip folder itself)."""
    d = (vlt / clip_rel).parent.name
    kit_dir = d[: -len("_gen")] if d.endswith("_gen") else d
    return vlt / "Voice_Files" / kit_dir / "_VO_Session_B_Kit.md"


def _ts(mtime: float) -> str:
    return datetime.fromtimestamp(mtime).isoformat(timespec="minutes")


def resolve_thumbnail(vlt: Path, vid: str) -> Path | None:
    """Mirror upload_video.resolve_thumbnail's default search (no override).

    Kept as a local copy on purpose: preflight is imported BY upload_video, so it
    must not import back from it. When upload_video calls check_publish_ready it
    passes the thumb it already resolved (honoring --thumbnail); this is only the
    fallback for the standalone CLI.
    """
    for cand in sorted(vlt.glob(f"Thumbnails/{vid}*.png")) + sorted(
        vlt.glob(f"Thumbnails/{vid}*.jpg")
    ):
        if cand.is_file():
            return cand
    return None


def check_publish_ready(
    vlt: Path,
    vid: str,
    *,
    video_file: Path,
    desc_pack: Path,
    thumb: Path | None,
    srt: Path | None,
) -> tuple[bool, list[str], list[str]]:
    """Return (ok, failures, warnings). Pure — no writes, no network."""
    failures: list[str] = []
    warnings: list[str] = []

    if not video_file.is_file():
        # Nothing else is meaningful without the cut; short-circuit.
        return False, [f"rendered video not found: {video_file}"], warnings
    mp4_mtime = video_file.stat().st_mtime

    manifest = _select_edit_manifest(vid, video_file.stem)
    if manifest is None:
        failures.append(
            f"no edit manifest {vid}_orchestrated_*.json in {MANIFEST_DIR} — "
            "cannot verify the cut's inputs; re-run build_video to author one."
        )
    else:
        try:
            shots = json.loads(manifest.read_text(encoding="utf-8")).get("shots", [])
        except (json.JSONDecodeError, OSError) as e:
            failures.append(f"edit manifest {manifest.name} unreadable: {e}")
            shots = []
        inputs: set[str] = set()
        for s in shots:
            for key in ("image", "vo_clip"):
                if s.get(key):
                    inputs.add(s[key])
        if not inputs:
            # Fail closed: an empty/zero-input manifest verifies NOTHING. A green
            # pass here would be the exact false-confidence the gate exists to kill
            # (truncated write, schema drift renaming `shots`, unreadable→[] above).
            failures.append(
                f"edit manifest {manifest.name} references zero shot inputs — "
                "cannot verify the cut is complete or fresh; re-author it."
            )
        missing = []
        stale = []
        for rel in sorted(inputs):
            p = vlt / rel
            if not p.is_file():
                missing.append(rel)
            # ponytail: strict `>` is deliberate — assembly reads inputs THEN writes
            # the mp4, so a legit input is strictly older; equal-tick ties (coarse FS,
            # mtime-preserving copy) read as fresh rather than false-failing the cut.
            elif p.stat().st_mtime > mp4_mtime:
                stale.append(rel)
        if missing:
            failures.append(
                f"{len(missing)} input(s) referenced by the cut are missing: "
                + ", ".join(missing[:6]) + (" …" if len(missing) > 6 else "")
            )
        if stale:
            failures.append(
                f"STALE CUT: {len(stale)} input(s) were regenerated AFTER the mp4 "
                f"was built ({', '.join(stale[:6])}{' …' if len(stale) > 6 else ''}) "
                "— re-assemble before upload."
            )
        # Check 6 — VO freshness vs the kit (the V7 gap): a skip-existing VO run
        # keeps mp3s that PREDATE the kit that should have produced them; they're
        # older than the mp4 too, so the stale-input check above is blind to them.
        # Same tie policy as above: equal mtimes read fresh.
        stale_vo: list[str] = []
        no_kit: set[str] = set()
        for rel in sorted(inputs):
            p = vlt / rel
            if not rel.endswith(".mp3") or not p.is_file():
                continue  # non-VO input, or already reported missing
            kit = _vo_kit_for(vlt, rel)
            if not kit.is_file():
                no_kit.add(str(kit.parent.relative_to(vlt)))
                continue
            if p.stat().st_mtime < kit.stat().st_mtime:
                stale_vo.append(f"{rel} (mp3 {_ts(p.stat().st_mtime)} < "
                                f"kit {_ts(kit.stat().st_mtime)})")
        if stale_vo:
            failures.append(
                f"STALE VO: {len(stale_vo)} clip(s) are OLDER than the VO kit that "
                f"should have produced them — retired narration; re-render VO "
                f"(generate_vo --force) and re-assemble: "
                + "; ".join(stale_vo[:4]) + (" …" if len(stale_vo) > 4 else "")
            )
        for d in sorted(no_kit):
            warnings.append(
                f"no VO kit at {d}/_VO_Session_B_Kit.md — VO freshness unverified."
            )

    if not desc_pack.is_file():
        failures.append(f"description pack not found: {desc_pack}")

    if thumb is None:
        failures.append(
            f"no thumbnail resolves for {vid} — the uploader would ship with no "
            f"custom thumbnail (tanks CTR). Place one under {vlt}/Thumbnails/{vid}* "
            "or pass --thumbnail."
        )
    elif not Path(thumb).is_file():
        failures.append(f"thumbnail not found: {thumb}")

    # Check 7 — thumbnail overlay vs locked-title pairing (the V8 gap): an overlay
    # that restates a dollar VALUE the title already carries answers the same
    # question the title does — the documented CTR-drop pattern. The overlay text
    # is machine-readable (the thumbnail_overlay spec); the title from packaging.
    title = _locked_title(vlt, vid, desc_pack)
    spec_path = THUMB_OVERLAY_DIR / f"{vid}_thumbnail_overlay.json"
    if title is None:
        warnings.append(f"no parseable locked title (desc-pack youtube_title or "
                        f"Packaging/Packaging_{vid}*.md) — thumbnail↔title pairing "
                        "unverified.")
    elif not spec_path.is_file():
        warnings.append(f"no thumbnail overlay spec {spec_path.name} — "
                        "thumbnail↔title pairing unverified.")
    else:
        title_vals = _dollar_values(title)
        resolved_key = _thumb_card_key(Path(thumb).stem) if thumb else None
        try:
            loaded = json.loads(spec_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            warnings.append(f"thumbnail overlay spec {spec_path.name} unreadable: {e}")
            loaded = {}
        cards = loaded.get("cards", {}) if isinstance(loaded, dict) else {}
        if not isinstance(cards, dict):
            warnings.append(f"thumbnail overlay spec {spec_path.name} has no valid "
                            "'cards' object — pairing unverified.")
            cards = {}
        for key, els in cards.items():
            if not isinstance(els, list):
                continue  # malformed card; a real burn would have failed already
            # str() coerces a numeric "text" (e.g. 10000000) so join never raises;
            # a bare number without a "$" isn't a dollar token anyway.
            overlay_text = " ".join(str(el.get("text", "")) for el in els
                                    if isinstance(el, dict))
            shared = title_vals & _dollar_values(overlay_text)
            if not shared:
                continue
            shared_str = ", ".join(f"${v:,}" for v in sorted(shared))
            msg = (f"THUMBNAIL PAIRING: overlay '{key}' ({overlay_text.strip()!r}) "
                   f"restates dollar value(s) {shared_str} the locked title already "
                   f"carries ({title!r}) — the Pairing Principle CTR-drop pattern. "
                   "Rebuild the overlay with a figure ORTHOGONAL to the title's range.")
            # Fail only if it's the thumbnail that will actually be uploaded; a
            # mispaired A/B alternate only warns (Steve may intentionally test it).
            if key == resolved_key:
                failures.append(msg)
            else:
                warnings.append(msg)
        # If the to-be-uploaded thumbnail matches no card, a violation on it could
        # only ever warn — surface that the severity attribution failed.
        if resolved_key is not None and cards and resolved_key not in cards:
            warnings.append(
                f"resolved thumbnail '{resolved_key}' matched no overlay card in "
                f"{spec_path.name} — pairing severity unattributable; verify the "
                "thumbnail↔spec naming.")

    if srt is None or not Path(srt).is_file():
        warnings.append(
            f"no caption .srt for {vid} — captions will be skipped (upload continues)."
        )

    return (not failures), failures, warnings


def write_signoff(vlt: Path, vid: str, video_file: Path,
                  thumb: Path | None, srt: Path | None) -> Path:
    """Record a dated stamp that this cut passed the freshness+completeness gate."""
    out_dir = vlt / "Footage_and_Edits" / "_preflight"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{vid}_preflight_signoff.md"
    now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    out.write_text(
        f"# Preflight signoff — {vid}\n\n"
        f"- verified: {now}\n"
        f"- mp4: {video_file}  ({video_file.stat().st_size / 1e6:.1f} MB)\n"
        f"- thumbnail: {thumb if thumb else '(none)'}\n"
        f"- captions: {srt if (srt and Path(srt).is_file()) else '(none)'}\n\n"
        "Deterministic checks passed: cut is COMPLETE (all referenced images + VO "
        "present, description pack + thumbnail present) and FRESH (no input "
        "regenerated after the mp4 was built). This gate does NOT watch the video — "
        "playback review remains the human step (the private upload).\n",
        encoding="utf-8",
    )
    return out


# --- standalone CLI --------------------------------------------------------

def _cli() -> int:
    p = argparse.ArgumentParser(description="Deterministic pre-upload signoff gate.")
    p.add_argument("video", nargs="?", help="Video id, e.g. Video_06 or 06.")
    p.add_argument("--json", action="store_true", help="Machine-readable result.")
    p.add_argument("--no-stamp", action="store_true", help="Don't write the signoff file on pass.")
    p.add_argument("--selftest", action="store_true", help="Run the logic self-check and exit.")
    args = p.parse_args()

    if args.selftest:
        return _selftest()
    if not args.video:
        p.error("video id required (or --selftest)")

    vid = normalize_id(args.video)
    vlt = vault()
    video_file = vlt / "Footage_and_Edits" / f"{vid}_v2.mp4"
    desc_pack = vlt / "Video_Descriptions" / f"{vid}_Description.md"
    srt = vlt / "Footage_and_Edits" / f"{vid}_v2.srt"
    thumb = resolve_thumbnail(vlt, vid)

    ok, failures, warnings = check_publish_ready(
        vlt, vid, video_file=video_file, desc_pack=desc_pack, thumb=thumb, srt=srt
    )
    if args.json:
        print(json.dumps({"video": vid, "ok": ok, "failures": failures,
                          "warnings": warnings}, indent=2))
    else:
        for w in warnings:
            print(f"  ⚠ {w}")
        if ok:
            print(f"✅ {vid}: preflight PASS — cut is fresh + complete.")
        else:
            print(f"❌ {vid}: preflight FAIL:")
            for f in failures:
                print(f"  - {f}")
    if ok and not args.no_stamp:
        stamp = write_signoff(vlt, vid, video_file, thumb, srt)
        if not args.json:
            print(f"  signoff → {stamp}")
    return 0 if ok else 1


def _selftest() -> int:
    import tempfile
    global MANIFEST_DIR, THUMB_OVERLAY_DIR
    real_manifest_dir = MANIFEST_DIR
    real_thumb_overlay_dir = THUMB_OVERLAY_DIR
    with tempfile.TemporaryDirectory() as td:
        vlt = Path(td)
        MANIFEST_DIR = vlt / "manifests"  # isolate: never touch the real repo dir
        MANIFEST_DIR.mkdir()
        THUMB_OVERLAY_DIR = vlt / "thumb_manifests"
        THUMB_OVERLAY_DIR.mkdir()
        vid = "Video_99"
        stem = f"{vid}_v2"
        (vlt / "Footage_and_Edits").mkdir(parents=True)
        (vlt / "Video_Descriptions").mkdir()
        (vlt / "Thumbnails").mkdir()
        (vlt / "Raw_Assets" / f"{vid}_gen").mkdir(parents=True)
        (vlt / "Voice_Files" / f"{vid}_gen").mkdir(parents=True)
        img = f"Raw_Assets/{vid}_gen/{vid}_Shot_01a.png"
        vo = f"Voice_Files/{vid}_gen/{vid}_VO_Scene_01.mp3"
        for rel in (img, vo):
            (vlt / rel).write_text("x")
        (vlt / "Voice_Files" / vid).mkdir()
        kit_file = vlt / "Voice_Files" / vid / "_VO_Session_B_Kit.md"
        kit_file.write_text("## Scene 1 -> `x.mp3`\n")
        kit_old = datetime(2019, 1, 1).timestamp()
        os.utime(kit_file, (kit_old, kit_old))  # older than the 2020 mp3s → fresh
        desc = vlt / "Video_Descriptions" / f"{vid}_Description.md"
        desc.write_text("## Description\nx\n")
        thumb = vlt / "Thumbnails" / f"{vid}_A.jpg"
        thumb.write_text("x")
        man = MANIFEST_DIR / f"{vid}_orchestrated_selftest.json"
        man.write_text(json.dumps(
            {"output_name": stem, "shots": [{"image": img, "vo_clip": vo}]}))
        mp4 = vlt / "Footage_and_Edits" / f"{stem}.mp4"
        try:
            # Inputs older than the mp4 → PASS.
            old = datetime(2020, 1, 1).timestamp()
            for rel in (img, vo):
                os.utime(vlt / rel, (old, old))
            mp4.write_bytes(b"video")
            ok, fails, _ = check_publish_ready(
                vlt, vid, video_file=mp4, desc_pack=desc, thumb=thumb, srt=None)
            assert ok, f"expected PASS, got {fails}"

            # Empty-input manifest → FAIL (vacuous-pass guard).
            man.write_text(json.dumps({"output_name": stem, "shots": []}))
            ok, fails, _ = check_publish_ready(
                vlt, vid, video_file=mp4, desc_pack=desc, thumb=thumb, srt=None)
            assert not ok and any("zero shot inputs" in f for f in fails), \
                f"expected empty-manifest fail, got {fails}"
            man.write_text(json.dumps(
                {"output_name": stem, "shots": [{"image": img, "vo_clip": vo}]}))

            # VO mp3 OLDER than its kit → STALE VO → FAIL (the V7 gap: the mp3 is
            # older than the mp4 too, so the stale-input check can't catch it).
            kit_new = datetime(2021, 1, 1).timestamp()
            os.utime(kit_file, (kit_new, kit_new))
            ok, fails, _ = check_publish_ready(
                vlt, vid, video_file=mp4, desc_pack=desc, thumb=thumb, srt=None)
            assert not ok and any("STALE VO" in f for f in fails), \
                f"expected stale-VO fail, got {fails}"
            os.utime(kit_file, (kit_old, kit_old))

            # Kit missing entirely → warn-only, still PASS.
            kit_file.unlink()
            ok, fails, warns = check_publish_ready(
                vlt, vid, video_file=mp4, desc_pack=desc, thumb=thumb, srt=None)
            assert ok and any("VO kit" in w for w in warns), \
                f"expected missing-kit warn + PASS, got fails={fails} warns={warns}"
            kit_file.write_text("## Scene 1 -> `x.mp3`\n")
            os.utime(kit_file, (kit_old, kit_old))

            # Touch an input newer than the mp4 → STALE → FAIL.
            future = datetime(2099, 1, 1).timestamp()
            os.utime(vlt / img, (future, future))
            ok, fails, _ = check_publish_ready(
                vlt, vid, video_file=mp4, desc_pack=desc, thumb=thumb, srt=None)
            assert not ok and any("STALE" in f for f in fails), f"expected STALE fail, got {fails}"

            # --- Check 7: thumbnail↔title pairing ---
            for rel in (img, vo):  # prior case left img at 2099 → reset to fresh
                os.utime(vlt / rel, (old, old))
            pkg_dir = vlt / "Packaging"
            pkg_dir.mkdir()
            pkg = pkg_dir / f"Packaging_{vid}.md"
            pkg.write_text('## ⭐ Recommended package\n'
                           '- **Title:** "POV: Your House ($0–$10M)"\n')
            spec = THUMB_OVERLAY_DIR / f"{vid}_thumbnail_overlay.json"
            # Resolved thumb (Video_99_A.jpg) is clean "$340"; alternate B restates
            # "$10M" (a title endpoint) → alternate WARNS, gate still PASSES.
            spec.write_text(json.dumps({"cards": {
                f"{vid}_A": [{"text": "BALANCE: $340"}],
                f"{vid}_B": [{"text": "$1,650"}, {"text": "→ $10M"}],
            }}))
            ok, fails, warns = check_publish_ready(
                vlt, vid, video_file=mp4, desc_pack=desc, thumb=thumb, srt=None)
            assert ok, f"expected PASS (clean resolved thumb), got {fails}"
            assert any("THUMBNAIL PAIRING" in w and f"{vid}_B" in w for w in warns), \
                f"expected mispaired-alternate warning, got {warns}"

            # Now the RESOLVED thumb itself restates $10M → FAIL.
            spec.write_text(json.dumps({"cards": {
                f"{vid}_A": [{"text": "$1,650 → $10M"}],
            }}))
            ok, fails, _ = check_publish_ready(
                vlt, vid, video_file=mp4, desc_pack=desc, thumb=thumb, srt=None)
            assert not ok and any("THUMBNAIL PAIRING" in f for f in fails), \
                f"expected resolved-thumb pairing FAIL, got {fails}"

            # $10M and $10,000,000 must compare equal (magnitude normalization).
            assert _dollar_values("$10M") == _dollar_values("$10,000,000") == {10_000_000}
            # A distinct in-range figure ($340) is NOT a restatement → clean.
            assert not (_dollar_values("POV ($0–$10M)") & _dollar_values("BALANCE: $340"))

            # --- check-7 hardening (skeptical-code-reviewer 2026-07-04) ---
            # Malformed-but-valid-JSON specs must WARN, never crash (warn-only contract).
            for bad in ([], {"cards": None}, {"cards": [1]}, {"cards": {"K": None}},
                        {"cards": {"K": 5}}, {"cards": {"K": [{"text": 10_000_000}]}}):
                spec.write_text(json.dumps(bad))
                ok, fails, _ = check_publish_ready(  # must not raise
                    vlt, vid, video_file=mp4, desc_pack=desc, thumb=thumb, srt=None)
                assert ok, f"malformed spec {bad} should not FAIL, got {fails}"

            # Regex: a trailing word must NOT become a magnitude multiplier.
            assert _dollar_values("$500 MISTAKE") == {500}, _dollar_values("$500 MISTAKE")
            assert _dollar_values("$700K") == {700_000}
            assert _dollar_values("$10M+") == {10_000_000}
            assert _dollar_values("$10 M") == {10_000_000}

            # Title precedence: desc-pack youtube_title wins over packaging.
            desc.write_text('---\nyoutube_title: "Clean Title No Dollars"\n---\nx\n')
            spec.write_text(json.dumps({"cards": {f"{vid}_A": [{"text": "$10M"}]}}))
            ok, fails, _ = check_publish_ready(
                vlt, vid, video_file=mp4, desc_pack=desc, thumb=thumb, srt=None)
            assert ok, f"clean youtube_title → overlay $10M is not a restatement, got {fails}"
            desc.write_text('---\nyoutube_title: "Ladder To $10M"\n---\nx\n')
            ok, fails, _ = check_publish_ready(
                vlt, vid, video_file=mp4, desc_pack=desc, thumb=thumb, srt=None)
            assert not ok and any("THUMBNAIL PAIRING" in f for f in fails), \
                f"youtube_title $10M vs overlay $10M → FAIL, got {fails}"
            desc.write_text("## Description\nx\n")  # restore no-frontmatter

            # Newest packaging doc wins over the base (repackage-staleness fix).
            pkg.write_text('## ⭐ Recommended package\n- **Title:** "Base No Dollars"\n')
            pkg_v2 = pkg_dir / f"Packaging_{vid}_v2.md"
            pkg_v2.write_text('## ⭐ Recommended package\n'
                              '- **Title:** "V2 Ladder ($0–$10M)"\n'
                              '## Alt\n- **Title:** "decoy $999"\n')
            spec.write_text(json.dumps({"cards": {f"{vid}_A": [{"text": "$10M"}]}}))
            ok, fails, _ = check_publish_ready(
                vlt, vid, video_file=mp4, desc_pack=desc, thumb=thumb, srt=None)
            assert not ok and any("THUMBNAIL PAIRING" in f for f in fails), \
                f"newest packaging (_v2, $10M) should catch overlay $10M, got {fails}"
            pkg_v2.unlink()
            pkg.write_text('## ⭐ Recommended package\n'
                           '- **Title:** "POV: Your House ($0–$10M)"\n')

            # Resolved thumb matching no card → warn (severity unattributable).
            spec.write_text(json.dumps({"cards": {f"{vid}_Zzz": [{"text": "$999"}]}}))
            ok, fails, warns = check_publish_ready(
                vlt, vid, video_file=mp4, desc_pack=desc, thumb=thumb, srt=None)
            assert ok and any("unattributable" in w for w in warns), \
                f"resolved-key mismatch should warn, got warns={warns}"

            # Spec present but no title parseable → warn-only, still PASS.
            spec.write_text(json.dumps({"cards": {f"{vid}_A": [{"text": "$10M"}]}}))
            pkg.write_text("no title field here\n")
            ok, fails, warns = check_publish_ready(
                vlt, vid, video_file=mp4, desc_pack=desc, thumb=thumb, srt=None)
            assert ok and any("locked title" in w for w in warns), \
                f"expected no-title warn + PASS, got fails={fails} warns={warns}"
            pkg.unlink()
            spec.unlink()

            # Missing thumbnail → FAIL.
            ok, fails, _ = check_publish_ready(
                vlt, vid, video_file=mp4, desc_pack=desc, thumb=None, srt=None)
            assert not ok and any("thumbnail" in f for f in fails), f"expected thumb fail, got {fails}"

            # Missing mp4 → FAIL (short-circuit).
            ok, fails, _ = check_publish_ready(
                vlt, vid, video_file=vlt / "nope.mp4", desc_pack=desc, thumb=thumb, srt=None)
            assert not ok, "expected missing-mp4 fail"
        finally:
            MANIFEST_DIR = real_manifest_dir
            THUMB_OVERLAY_DIR = real_thumb_overlay_dir
    print("selftest ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
