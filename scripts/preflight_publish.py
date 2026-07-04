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
DEFAULT_VAULT = "~/Documents/3SK/outputs/BRANDS/3SK_Finance"


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
    global MANIFEST_DIR
    real_manifest_dir = MANIFEST_DIR
    with tempfile.TemporaryDirectory() as td:
        vlt = Path(td)
        MANIFEST_DIR = vlt / "manifests"  # isolate: never touch the real repo dir
        MANIFEST_DIR.mkdir()
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
    print("selftest ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
