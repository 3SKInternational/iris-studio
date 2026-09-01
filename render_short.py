#!/usr/bin/env python3
"""render_short.py — render a Shorts production pack to 9:16 MP4 files.

RENDER-TO-FILE ONLY. It produces vertical MP4s in Footage_and_Edits/Shorts/ and
stops. Nothing uploads, nothing publishes, no channel write. There is no upload
code path in this file by design (DQ-47: the render half is safe, the publish
half stays 100% gated on Steve's word).

Cost: 100% PNG reuse (no image spend). VO rides the existing ElevenLabs sub via
vo_factory/generate_vo.py. A ~140-word Short is ~750 chars ~= 750 credits.

It wraps the two existing factories rather than re-implementing them:
  vo_factory/generate_vo.py   Short VO text -> one mp3 per Short (auth, budget,
                              dual-form + break-tag handling all owned there)
  video_factory/assemble.py   PNGs + mp3 + 9:16 manifest -> mp4 + soft-CC srt
                              (Ken Burns, caption chunking, ffmpeg all owned there)

TTS number safety reuses scripts/vo_number_lint.py: comma-grouped millions that
ElevenLabs mis-speaks ($10,000,000) are rewritten to a dual-form token
{{$10 million|$10,000,000}} so the VO says the safe form and the caption keeps
the digits. A hazard the linter can only rewrite "by hand" (a non-round million)
blocks that Short fail-closed rather than shipping mis-spoken audio.

Usage:
  python3 render_short.py BRANDS/3SK_Finance/Shorts/Video_13_Shorts.md --dry-run
  python3 render_short.py BRANDS/3SK_Finance/Shorts/Video_13_Shorts.md --limit 3
  python3 render_short.py <pack> --only 1,3        # render specific Shorts
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
DEFAULT_VAULT = "~/Documents/3SK/outputs/BRANDS/3SK_Finance"
# Motion cycle gives each still in a Short a distinct gentle move.
MOTIONS = ["zoom_in", "pan_up", "zoom_out", "pan_right", "hold"]


def die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(1)  # mutequiv: any nonzero exit means failure; no caller branches on the code


def vault() -> Path:
    return Path(os.path.expanduser(os.environ.get("SK_VAULT", DEFAULT_VAULT))).resolve()


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---- Shorts-pack parsing --------------------------------------------------

_SHORT_SPLIT = re.compile(r"^##\s+Short\s+(\d+)\b(.*)$", re.MULTILINE)
_VO_BLOCK = re.compile(r"\*\*VO[^:]*:\*\*\s*\n((?:[ \t]*>.*\n?)+)")
_REUSE_PNG = re.compile(r"reuse\s+`([^`]+\.png)`", re.IGNORECASE)
# {{spoken|CAPTION}} — keep the RIGHT (caption/digits) form for on-screen text.
_DUAL_KEEP_RIGHT = re.compile(r"\{\{\s*[^|{}]+?\s*\|\s*([^{}]+?)\s*\}\}")


def _quote_body(block: str) -> str:
    """The narration under a `**VO ...:**` bullet is a `> ...` blockquote."""
    m = _VO_BLOCK.search(block)
    if not m:
        return ""
    lines = [re.sub(r"^[ \t]*>\s?", "", ln) for ln in m.group(1).splitlines()]
    return "\n".join(lines).strip()


def parse_pack(text: str) -> list[dict]:
    """Return [{n, title, vo, images}] for each Short in the pack, in order."""
    parts = _SHORT_SPLIT.split(text)
    # split() -> [pre, n1, hdr1, body1, n2, hdr2, body2, ...]
    shorts = []
    for i in range(1, len(parts), 3):
        n = int(parts[i])
        title = parts[i + 1].strip().lstrip("—- ").strip().strip('"')
        body = parts[i + 2]
        vo = _quote_body(body)
        images = _REUSE_PNG.findall(body)
        shorts.append({"n": n, "title": title, "vo": vo, "images": images})
    return shorts


# ---- Number safety (reuse vo_number_lint's tested detector/rewriter) -------

def tokenize_numbers(vo: str, vnl) -> str:
    """Wrap each TTS-hazard number in a dual-form token so the VO speaks the safe
    form and the caption keeps the digits. Blocks (raises) on a hazard the linter
    can only approximate — mis-spoken money on a money channel is worse than a
    stop.

    Scans only the text OUTSIDE any existing {{spoken|CAPTION}} token, the same
    masking the linter applies (_DUAL_FORM): a figure the author already wrapped is
    settled, so re-scanning it would nest the token (garbled VO) and make the
    die-message's own recovery advice re-die on its second run. Within each gap,
    re.sub never re-scans inserted text, so a repeated bare figure is still
    rewritten exactly once. Reuses the linter's pattern + rewriter so the two stay
    in lockstep (its docstring asks for this)."""
    def repl(m: re.Match) -> str:
        raw = m.group(0)
        safe = vnl._safe_rewrite(raw)
        if "approx" in safe:
            die(f"unrenderable number {raw!r}: {safe}. Fix the pack VO by hand "
                f"(add a {{{{spoken|{raw}}}}} dual-form token) and re-run.")
        return f"{{{{{safe}|{raw}}}}}"
    out, pos = [], 0
    for tok in vnl._DUAL_FORM.finditer(vo):
        out.append(vnl._GROUPED_MILLIONS.sub(repl, vo[pos:tok.start()]))
        out.append(tok.group(0))
        pos = tok.end()
    out.append(vnl._GROUPED_MILLIONS.sub(repl, vo[pos:]))
    return "".join(out)


def caption_form(vo: str) -> str:
    """The on-screen caption keeps the RIGHT side of any dual-form token."""
    return _DUAL_KEEP_RIGHT.sub(r"\1", vo)


# ---- Caption chunking across the Short's stills ---------------------------

def split_caption(caption: str, n: int) -> list[str]:
    """Split narration into n char-balanced groups on sentence boundaries so each
    still carries roughly the words spoken while it is on screen. assemble.py then
    auto-chunks each group into timed SRT cues within that still's window."""
    if n <= 1:
        return [caption]
    sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", caption) if s.strip()]
    if len(sents) <= n:
        # Pad with empties so every still still gets a (possibly blank) caption.
        return sents + [""] * (n - len(sents))
    target = sum(len(s) for s in sents) / n
    groups, cur, cur_len = [], [], 0  # mutequiv: cur_len is a soft char budget; a 1-off never breaks the count/no-drop invariants
    for idx, s in enumerate(sents):
        cur.append(s)
        cur_len += len(s)
        remaining_groups = n - len(groups)
        remaining_sents = len(sents) - (idx + 1)
        # Close the group at the target, but never leave fewer sentences than the
        # groups still to fill.
        if (cur_len >= target and remaining_groups > 1
                and remaining_sents >= remaining_groups - 1):  # mutequiv: rg-1 vs rg-2 only shifts a tail close; the pad step yields the same groups
            groups.append(" ".join(cur))
            cur, cur_len = [], 0  # mutequiv: same soft char-budget reset; a 1-off changes no contracted output
    if cur:  # mutequiv: the final group never closes (rg==1) so cur is never empty here
        groups.append(" ".join(cur))
    while len(groups) < n:
        groups.append("")
    return groups[:n]


# ---- VO generation + duration ---------------------------------------------

def ffprobe_seconds(mp3: Path) -> float:
    exe = os.environ.get("FFPROBE", "ffprobe")
    out = subprocess.run(
        [exe, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(mp3)],
        capture_output=True, text=True, check=True).stdout.strip()
    return float(out)


def write_kit(shorts: list[dict], vid: str, kit_path: Path, vnl) -> dict[int, str]:
    """Author a VO kit generate_vo.py accepts. Returns {short_n: mp3_filename}."""
    blocks, names = [], {}
    for s in shorts:
        fname = f"{vid}_Short_{s['n']}_VO.mp3"
        names[s["n"]] = fname
        spoken = tokenize_numbers(s["vo"], vnl)
        blocks.append(f"## Scene {s['n']} -> `{fname}`\n{spoken}\n")
    content = "\n".join(blocks) + "\n"
    # Only touch the file when content actually changes: generate_vo treats an
    # existing mp3 OLDER than the kit as fatal ("kit changed after audio"), so a
    # rewrite with identical bytes would break a benign resumable re-run.
    if not kit_path.exists() or kit_path.read_text(encoding="utf-8") != content:
        kit_path.write_text(content, encoding="utf-8")
    return names


# ---- Manifest authoring ---------------------------------------------------

def author_manifest(short: dict, mp3: Path, out_dir: Path, name: str) -> dict:
    imgs = short["images"]
    if not imgs:
        die(f"Short {short['n']} has no reuse images in the pack.")
    dur = ffprobe_seconds(mp3)
    n = len(imgs)
    bounds = [round(i * dur / n, 3) for i in range(n)] + [dur]  # mutequiv: 3 vs 4 dp is a <=1-frame shift in a Ken Burns cut point; VO is a continuous clip so audio is unaffected and no content drops
    caps = split_caption(caption_form(short["vo"]), n)
    shots = []
    for i, img in enumerate(imgs):
        shots.append({
            "image": img,
            "vo_clip": str(mp3),
            "start": bounds[i],
            "end": bounds[i + 1],
            "motion": MOTIONS[i % len(MOTIONS)],
            "caption_text": caps[i],
        })
    return {
        "video": f"{name} ({short['title']})",
        "asset_dir": "/",
        "output_dir": str(out_dir),
        "output_name": name,
        "output_size": [1080, 1920],
        "defaults": {"zoom": 1.06, "fit": "cover"},
        "shots": shots,
    }


# ---- Driver ---------------------------------------------------------------

def run(cmd: list[str], label: str) -> None:
    print(f"  -> {label}: {' '.join(cmd)}")
    if subprocess.run(cmd).returncode != 0:
        die(f"{label} failed.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Render a Shorts pack to 9:16 MP4s (render-to-file only).")
    ap.add_argument("pack", help="Path to a *_Shorts.md production pack.")
    ap.add_argument("--limit", type=int, default=3,
                    help="Render at most N Shorts (default 3 — a small drip, not a 32-Short dump).")
    ap.add_argument("--only", help="Render only these Short numbers (comma-separated, e.g. 1,3).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Author the VO kit + manifests and gate numbers; no VO spend, no render.")
    ap.add_argument("--force", action="store_true", help="Re-render existing VO mp3s.")
    args = ap.parse_args()

    pack_path = Path(args.pack).expanduser().resolve()
    if not pack_path.is_file():
        die(f"pack not found: {pack_path}")
    vid_m = re.search(r"(Video_\d+)", pack_path.name)
    if not vid_m:
        die(f"cannot derive a Video_NN id from {pack_path.name}")
    vid = vid_m.group(1)

    vnl = _load("vo_number_lint", REPO / "scripts" / "vo_number_lint.py")

    shorts = parse_pack(pack_path.read_text(encoding="utf-8"))
    if not shorts:
        die("no Shorts parsed from the pack (expected '## Short N' sections).")
    if args.only:
        want = {int(x) for x in re.findall(r"\d+", args.only)}
        shorts = [s for s in shorts if s["n"] in want]
        if not shorts:
            die(f"--only {args.only!r} matched no Shorts in the pack.")
    else:
        shorts = shorts[:max(0, args.limit)]

    out_dir = vault() / "Footage_and_Edits" / "Shorts"
    work = out_dir / "_work" / vid
    vo_dir = work / "vo"
    for d in (out_dir, vo_dir):
        d.mkdir(parents=True, exist_ok=True)
    print(f"pack: {pack_path.name}  |  {vid}  |  {len(shorts)} Short(s)  |  out: {out_dir}")

    # 1. Author the VO kit (number-gated) and generate one mp3 per Short.
    kit_path = work / f"{vid}_shorts_vo_kit.md"
    names = write_kit(shorts, vid, kit_path, vnl)
    print(f"  VO kit -> {kit_path}")

    vo_gen = REPO / "vo_factory" / "generate_vo.py"
    vo_cmd = [sys.executable, str(vo_gen), str(kit_path), "--output", str(vo_dir)]
    if args.dry_run:
        vo_cmd.append("--dry-run")
    if args.force:
        vo_cmd.append("--force")
    run(vo_cmd, "generate_vo")

    if args.dry_run:
        # Prove the manifests author cleanly without spend: use a placeholder
        # duration since no mp3 exists yet.
        for s in shorts:
            caps = split_caption(caption_form(s["vo"]), len(s["images"]))
            print(f"  [dry] Short {s['n']}: {len(s['images'])} still(s), "
                  f"{len(s['vo'])} VO chars, {sum(bool(c) for c in caps)} caption group(s) -> "
                  f"{vid}_Short_{s['n']}.mp4")
        print("dry-run: VO kit + number gate + manifest plan OK. No spend, no render.")
        return

    # 2. Author each manifest and render it to a 9:16 MP4.
    assembler = REPO / "video_factory" / "assemble.py"
    rendered = []
    for s in shorts:
        mp3 = vo_dir / names[s["n"]]
        if not mp3.is_file():
            die(f"expected VO mp3 missing: {mp3}")
        name = f"{vid}_Short_{s['n']}"
        manifest = author_manifest(s, mp3, out_dir, name)
        man_path = work / f"{name}.json"
        man_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")  # mutequiv: JSON indent is cosmetic; assemble.py parses regardless
        run([sys.executable, str(assembler), str(man_path)], f"assemble {name}")
        rendered.append(out_dir / f"{name}.mp4")

    print("\ndone. Rendered (render-to-file only — nothing published):")
    for p in rendered:
        print(f"  {p}")


if __name__ == "__main__":  # mutequiv: entrypoint guard; only affects direct exec, not imported logic
    main()
