#!/usr/bin/env python3
"""E10 — File_Index auto-regen.

Walks the vault tree, writes `_MAP/File_Index.md` as the canonical file index
(one row per file: relative path, size, mtime, top-level folder). Excludes the
.git, .obsidian, .venv, _archive, and temp/backup files. Idempotent.

Brain-audit fix #8 (2026-06-10): the hand-maintained File_Index tab of
3SK_File_Map.xlsx can't keep up with a 320+ file vault. Per the audit, MOC +
folder READMEs are the live index for humans/agents; this regen quarterly
keeps the inventory honest without ongoing manual work.

Deviation from the audit: ships as markdown (`_MAP/File_Index.md`) instead of
overwriting the xlsx tab — the daemon venv doesn't carry openpyxl, and adding
a deps install for a quarterly chore is wrong-sized. The xlsx other-7-tabs are
hand-curated rules anyway; the File_Index tab can be either deprecated in
favor of this .md or refreshed via one-time paste of the .md table.

Schedule: not hooked into launchd by default — quarterly cadence is too coarse
to warrant a dedicated job. Run on-demand or fold into the Sunday hygiene
routine on the first Sunday of each quarter (Jan / Apr / Jul / Oct).

Stdlib-only.
"""

from __future__ import annotations

import datetime
import os
import sys
from pathlib import Path

VAULT = Path("/Users/steve/Documents/3SK/outputs")
OUTPUT = VAULT / "_MAP" / "File_Index.md"

# Skip these paths entirely (substring match against relative path).
EXCLUDE_DIR_SUBSTRINGS = {
    "/.git/",
    "/.obsidian/workspace",
    "/.venv/",
    "/__pycache__/",
    "/_archive/",
    "/07_Archive/",
}

# Skip these by filename.
EXCLUDE_NAMES = {".DS_Store"}

# Skip these by suffix (backup/temp files).
EXCLUDE_SUFFIXES = (".bak", ".tmp", ".pyc")


def _is_excluded(rel_path_str: str, name: str) -> bool:
    if name in EXCLUDE_NAMES:
        return True
    if name.endswith(EXCLUDE_SUFFIXES):
        return True
    # Common backup-name patterns: foo.bak-* or foo.bak.YYYY-MM-DD
    if ".bak-" in name or ".bak." in name:
        return True
    for sub in EXCLUDE_DIR_SUBSTRINGS:
        if sub in "/" + rel_path_str:
            return True
    return False


def _top_folder(rel_path: Path) -> str:
    parts = rel_path.parts
    if not parts:
        return "(root)"
    if len(parts) == 1:
        return "(root)"
    return parts[0]


def _human_size(n: int) -> str:
    for unit in ("B", "K", "M", "G"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024  # type: ignore[assignment]
    return f"{n:.1f}T"


def walk_vault() -> list[dict[str, str]]:
    entries = []
    for dirpath, dirnames, filenames in os.walk(VAULT):
        # In-place prune to skip whole excluded directories early
        dirnames[:] = [
            d for d in dirnames
            if not _is_excluded(str(Path(dirpath, d).relative_to(VAULT)), d)
            and d != ".git"
            and d != ".venv"
            and d != "__pycache__"
        ]
        for fname in filenames:
            full = Path(dirpath) / fname
            try:
                rel = full.relative_to(VAULT)
            except ValueError:
                continue
            rel_str = str(rel)
            if _is_excluded(rel_str, fname):
                continue
            try:
                st = full.stat()
            except OSError:
                continue
            entries.append(
                {
                    "path": rel_str,
                    "name": fname,
                    "size": _human_size(st.st_size),
                    "size_bytes": str(st.st_size),
                    "mtime": datetime.date.fromtimestamp(st.st_mtime).isoformat(),
                    "folder": _top_folder(rel),
                    "ext": (full.suffix or "(none)").lower(),
                }
            )
    entries.sort(key=lambda e: (e["folder"].lower(), e["path"].lower()))
    return entries


def render(entries: list[dict[str, str]]) -> str:
    today = datetime.date.today().isoformat()
    by_folder: dict[str, int] = {}
    by_ext: dict[str, int] = {}
    total_bytes = 0
    for e in entries:
        by_folder[e["folder"]] = by_folder.get(e["folder"], 0) + 1
        by_ext[e["ext"]] = by_ext.get(e["ext"], 0) + 1
        total_bytes += int(e["size_bytes"])

    out = [
        "---",
        f"date: {today}",
        "type: index",
        "status: auto-generated",
        "purpose: Inventory of every tracked file in the 3SK vault — regenerated quarterly by `scripts/regen_file_index.py` (E10).",
        "tags:",
        "  - vault/index",
        "  - auto-generated",
        "---",
        "",
        "# File Index — vault inventory",
        "",
        f"_Auto-generated {today} by `/Volumes/AI_Workspace/iris_studio/scripts/regen_file_index.py` (E10 — brain-audit fix #8). DO NOT hand-edit; re-run the script to refresh. Cadence: first Sunday of each quarter, or on-demand._",
        "",
        f"**Total files indexed:** {len(entries):,}  ·  **Total size:** {_human_size(total_bytes)}",
        "",
        "## By top-level folder",
        "",
        "| Folder | File count |",
        "|---|---|",
    ]
    for folder, count in sorted(by_folder.items(), key=lambda kv: kv[1], reverse=True):
        out.append(f"| {folder} | {count:,} |")

    out.extend([
        "",
        "## By extension (top 12)",
        "",
        "| Extension | File count |",
        "|---|---|",
    ])
    for ext, count in sorted(by_ext.items(), key=lambda kv: kv[1], reverse=True)[:12]:
        out.append(f"| `{ext}` | {count:,} |")

    out.extend([
        "",
        "## Full file list",
        "",
        "| Folder | Path | Size | Modified |",
        "|---|---|---|---|",
    ])
    for e in entries:
        path = e["path"].replace("|", "\\|")
        out.append(f"| {e['folder']} | `{path}` | {e['size']} | {e['mtime']} |")
    out.append("")
    return "\n".join(out)


def main() -> int:
    entries = walk_vault()
    content = render(entries)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(content, encoding="utf-8")
    # Status line for log capture
    sys.stdout.write(
        f"E10 regen: wrote {OUTPUT.relative_to(VAULT)} — {len(entries):,} files indexed.\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
