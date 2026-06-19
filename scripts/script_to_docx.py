#!/usr/bin/env python3
"""Convert a 3SK Finance script Markdown file to .docx via pandoc.

This is the deterministic, reusable converter behind the daemon's scriptwriter
post-dispatch hook (iris.py `_finish_dispatch`). It exists as a standalone helper
so ANY channel can run it — the daemon, an interactive Claude Code session, or
Steve by hand — instead of anyone re-deriving the conversion. The daemon hook is
best-effort and never blocks a dispatch; this script owns the actual work.

It does ONE thing: pandoc-render a `.md` to a `.docx` beside it (same stem),
atomically. No summarizing, no _REVIEW_PREP skim view (that's a content
derivation, not a format conversion). pandoc is resolved from PATH with a
Homebrew fallback, because launchd jobs don't inherit an interactive PATH.

Usage:
  python3 scripts/script_to_docx.py path/to/Video_06_Script.md
  python3 scripts/script_to_docx.py path/to/script.md --out /somewhere/out.docx
  python3 scripts/script_to_docx.py path/to/script.md --check   # just verify pandoc

Exit codes: 0 ok; 2 bad input (missing/not-.md); 3 pandoc unavailable;
4 pandoc conversion failed. The daemon hook treats any non-zero as "skip,
log a warning" — never fatal.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# launchd jobs run with a minimal PATH, so an absolute Homebrew fallback is the
# difference between "works unattended" and "silently never converts".
PANDOC_FALLBACKS = ("/opt/homebrew/bin/pandoc", "/usr/local/bin/pandoc")


class DocxConvertError(RuntimeError):
    """Raised on any conversion failure so importers can catch one type."""


def find_pandoc() -> str | None:
    """Absolute pandoc path from PATH, then known Homebrew locations, else None."""
    found = shutil.which("pandoc")
    if found:
        return found
    for cand in PANDOC_FALLBACKS:
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def convert(md_path: str | os.PathLike, out_path: str | os.PathLike | None = None,
            pandoc: str | None = None) -> Path:
    """Render `md_path` (a .md) to .docx. Returns the output Path.

    Writes to a temp file in the destination dir then atomically replaces the
    target, so a crash mid-render never leaves a half-written .docx that looks
    valid. Raises DocxConvertError on any problem (missing input, no pandoc,
    pandoc non-zero).
    """
    src = Path(md_path).expanduser().resolve()
    if not src.is_file():
        raise DocxConvertError(f"input not found: {src}")
    if src.suffix.lower() != ".md":
        raise DocxConvertError(f"not a Markdown file (.md required): {src}")

    dst = (Path(out_path).expanduser().resolve() if out_path
           else src.with_suffix(".docx"))
    dst.parent.mkdir(parents=True, exist_ok=True)

    pandoc_bin = pandoc or find_pandoc()
    if not pandoc_bin:
        raise DocxConvertError(
            "pandoc not found on PATH or in Homebrew locations "
            f"({', '.join(PANDOC_FALLBACKS)}); install with `brew install pandoc`."
        )

    # Render to a temp file in the SAME dir (so os.replace is atomic, not a
    # cross-filesystem copy), then swap it in.
    fd, tmp_name = tempfile.mkstemp(suffix=".docx", dir=str(dst.parent))
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        proc = subprocess.run(
            [pandoc_bin, str(src), "-f", "markdown", "-t", "docx", "-o", str(tmp)],
            capture_output=True, text=True, timeout=120,
        )
        if proc.returncode != 0:
            raise DocxConvertError(
                f"pandoc exit {proc.returncode}: "
                f"{(proc.stderr or proc.stdout or '').strip()[:400]}"
            )
        if not tmp.is_file() or tmp.stat().st_size == 0:
            raise DocxConvertError("pandoc produced no output")
        os.replace(tmp, dst)
        # mkstemp creates 0600; a reviewable document should carry normal perms.
        try:
            os.chmod(dst, 0o644)
        except OSError:
            pass
    except subprocess.TimeoutExpired as exc:
        raise DocxConvertError(f"pandoc timed out after {exc.timeout}s") from exc
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return dst


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Convert a script .md to .docx via pandoc.")
    p.add_argument("md", nargs="?", help="Path to the script Markdown file.")
    p.add_argument("--out", help="Override output .docx path (default: same stem).")
    p.add_argument("--check", action="store_true",
                   help="Only check pandoc availability; convert nothing.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if args.check:
        pandoc_bin = find_pandoc()
        if pandoc_bin:
            print(f"pandoc: {pandoc_bin}")
            return 0
        print("pandoc: NOT FOUND", file=sys.stderr)
        return 3
    if not args.md:
        print("error: pass a path to a .md file (or --check).", file=sys.stderr)
        return 2
    try:
        out = convert(args.md, args.out)
    except DocxConvertError as exc:
        msg = str(exc)
        print(f"error: {msg}", file=sys.stderr)
        if "not found" in msg or ".md required" in msg:
            return 2
        if "pandoc not found" in msg:
            return 3
        return 4
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
