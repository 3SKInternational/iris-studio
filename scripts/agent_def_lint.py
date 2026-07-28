#!/usr/bin/env python3
"""Lint the agent-definition fleet in ~/.claude/agents/.

Why this exists: the ADAPTS loop only ever APPENDS. Every approved adaptation
bolts a new dated block onto the end of an agent def and nothing ever merges a
rule into the section it belongs to or retires a superseded one. Measured
2026-07-27: scene-image-prompt-generator went 11KB (6/19) -> 50KB in five weeks,
image-reviewer 22.9KB -> 36KB, packaging-strategist 9.2KB -> 15.4KB. Growth is
monotonic. Every dispatch pays that prompt before it reads a single vault file.

Nothing was watching the size, so this is the watcher. REPORT-ONLY by design —
it never edits a def. A def over the threshold should get a compaction proposal
through the /adapt queue (where Steve approves each edit), which is the
mechanism that already governs those files.

Three checks, cheapest first:
  1. SIZE  (warn)  — def exceeds --max-kb. The growth gate.
  2. FRONT (error) — missing/!malformed frontmatter, or `name:` != filename stem.
                     A name/filename mismatch silently breaks dispatch-by-name.
  3. ROSTER(error) — org_chart.json names a stem with no def on disk, or a
                     business-fleet def is missing from the org chart. Catches
                     roster drift when agents are added/removed.

Usage:
  agent_def_lint.py                 # lint, human-readable
  agent_def_lint.py --max-kb 25     # different size threshold
  agent_def_lint.py --quiet         # only print problems (for cron)
  agent_def_lint.py --selftest      # offline logic check

Exit: 0 = clean or size-warnings only; 1 = hard error (FRONT/ROSTER); 2 = bad usage.
"""
from __future__ import annotations
import argparse
import json
import re
import sys
from pathlib import Path

DEFAULT_AGENTS_DIR = Path("/Users/steve/.claude/agents")
DEFAULT_ORG_CHART = Path("/Volumes/AI_Workspace/iris_studio/dashboard/org_chart.json")
DEFAULT_MAX_KB = 20.0

# Generic dev/tooling agents that intentionally live outside the business org
# chart (org_chart.json says so itself) — never flagged as roster drift.
NON_FLEET = frozenset({
    "echo", "claude", "claude-code-guide", "statusline-setup",
    "general-purpose", "Explore", "Plan",
})

_FM_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.DOTALL)
_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):\s*(.*)$")


def parse_frontmatter(text: str) -> dict[str, str] | None:
    """Return the frontmatter's top-level scalar keys, or None if absent/malformed.

    Deliberately not a YAML parser: agent frontmatter is flat `key: value` and
    the descriptions contain colons, braces and quotes that would make a real
    parser the fragile choice here. Only the first colon splits.
    """
    m = _FM_RE.match(text)
    if not m:
        return None
    out: dict[str, str] = {}
    for line in m.group(1).splitlines():
        # No blank/comment skip guard: _KEY_RE is anchored at ^[A-Za-z_], so a
        # blank line, an indented line, and any `#` comment already fail to match
        # (verified). A separate guard was provably an equivalent mutant — dead
        # code wearing the costume of a validation step. The behaviour it claimed
        # to provide is pinned by tests either way; don't add one back.
        km = _KEY_RE.match(line)
        if km:
            out[km.group(1)] = km.group(2).strip()
    return out or None


def load_org_stems(org_chart: Path) -> set[str]:
    """Every agent stem named anywhere in org_chart.json.

    The chart names agents under several keys at varying depths — `agent` (CEO),
    `lead` (department heads), and the `members` / `staff` lists — so walk the
    whole structure and collect all of them rather than hard-coding the shape.
    The chart is re-slotted freely, so a depth or key assumption would rot; an
    early version of this collector missed `lead`/`staff` and reported 11 false
    roster errors, which is exactly the failure mode being guarded against here.
    """
    SCALAR_KEYS = {"agent", "lead"}
    LIST_KEYS = {"members", "staff"}
    stems: set[str] = set()

    def walk(node) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if k in SCALAR_KEYS and isinstance(v, str):
                    stems.add(v)
                elif k in LIST_KEYS and isinstance(v, list):
                    stems.update(x for x in v if isinstance(x, str))
                    walk([x for x in v if not isinstance(x, str)])
                else:
                    walk(v)
        elif isinstance(node, list):
            for x in node:
                walk(x)

    walk(json.loads(org_chart.read_text()))
    return stems


def lint(agents_dir: Path, org_chart: Path | None, max_kb: float) -> tuple[list[str], list[str]]:
    """Return (errors, warnings)."""
    errors: list[str] = []
    warnings: list[str] = []

    defs = sorted(p for p in agents_dir.glob("*.md") if p.is_file())
    if not defs:
        errors.append(f"no agent defs found in {agents_dir}")
        return errors, warnings

    on_disk: set[str] = set()
    for p in defs:
        stem = p.stem
        on_disk.add(stem)
        kb = p.stat().st_size / 1024
        fm = parse_frontmatter(p.read_text(errors="replace"))
        if fm is None:
            errors.append(f"FRONT {p.name}: missing or malformed frontmatter block")
        else:
            declared = fm.get("name")
            if declared is None:
                errors.append(f"FRONT {p.name}: frontmatter has no `name:` field")
            elif declared != stem:
                errors.append(
                    f"FRONT {p.name}: `name: {declared}` != filename stem `{stem}` "
                    f"(dispatch-by-name will not resolve)")
            for req in ("description", "model", "tools"):
                if req not in fm:
                    errors.append(f"FRONT {p.name}: frontmatter has no `{req}:` field")
        if kb > max_kb:
            warnings.append(
                f"SIZE  {p.name}: {kb:.1f}KB exceeds {max_kb:.0f}KB "
                f"(~{int(kb * 1024 / 4):,} tokens paid on every dispatch) "
                f"— queue a compaction proposal via /adapt")

    if org_chart is not None:
        if not org_chart.exists():
            errors.append(f"ROSTER org chart not found: {org_chart}")
        else:
            charted = load_org_stems(org_chart)
            for stem in sorted(charted - on_disk):
                errors.append(
                    f"ROSTER org_chart.json names `{stem}` but ~/.claude/agents/{stem}.md "
                    f"does not exist (dashboard will render a broken node)")
            for stem in sorted(on_disk - charted - NON_FLEET):
                errors.append(
                    f"ROSTER `{stem}.md` exists on disk but is absent from org_chart.json "
                    f"(add it, or add the stem to NON_FLEET if it is a tooling agent)")
    return errors, warnings


def _selftest() -> int:
    # mutequiv: mutants inside this function survive because the only assertion on
    # it is `_selftest() == 0` — a weakened check still returns 0, so the selftest
    # cannot catch its own weakening.
    #
    # An earlier version of this marker claimed every property here was
    # independently pinned in tests/test_agent_def_lint.py. That was FALSE and a
    # 2026-07-28 review proved it: both roster error-emission loops in lint()
    # could be deleted with all 15 tests green, because every other test called
    # lint() with org_chart=None. The marker was hiding a real hole in the ROSTER
    # tier — the one efficiency-steward routes as a defect. TestRosterTier now
    # pins both loops through lint() with a real chart (verified: removing them
    # fails 4 tests), so the claim is finally true.
    #
    # Lesson worth keeping: an equivalence marker is a claim about COVERAGE
    # ELSEWHERE, and it decays silently when that coverage was never there. Before
    # trusting one, delete the code it excuses and confirm something goes red.
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        d = Path(td) / "agents"
        d.mkdir()

        def write(stem: str, fm: str, body: str = "body\n") -> Path:
            p = d / f"{stem}.md"
            p.write_text(f"---\n{fm}\n---\n\n{body}")
            return p

        good_fm = "name: alpha\ndescription: d\nmodel: sonnet\ntools: Read"
        write("alpha", good_fm)
        write("mismatch", "name: WRONG\ndescription: d\nmodel: sonnet\ntools: Read")
        write("nomodel", "name: nomodel\ndescription: d\ntools: Read")
        (d / "nofm.md").write_text("no frontmatter here\n")
        write("fat", "name: fat\ndescription: d\nmodel: sonnet\ntools: Read", "x" * 40000)

        errs, warns = lint(d, None, max_kb=20.0)
        joined = " | ".join(errs)

        # Mirrors the real chart's shape: `agent` at the CEO, a `staff` list, and
        # departments keyed by `lead` + `members`. All four must be collected.
        chart = Path(td) / "org.json"
        chart.write_text(json.dumps({
            "ceo": {"agent": "alpha", "staff": ["nomodel"]},
            "departments": [
                {"key": "d1", "lead": "mismatch", "members": ["fat", "phantom"]}]}))
        errs2, _ = lint(d, chart, max_kb=20.0)
        j2 = " | ".join(errs2)

        # description containing a colon must not break the flat parser
        colon = parse_frontmatter(
            "---\nname: x\ndescription: Uses this: and that. Distinct from y\n"
            "model: sonnet\ntools: Read\n---\n\nbody\n")

        checks = {
            "flags name != filename stem": any("mismatch.md" in e and "!=" in e for e in errs),
            "flags missing model field": any("nomodel.md" in e and "`model:`" in e for e in errs),
            "flags missing frontmatter": any("nofm.md" in e and "malformed" in e for e in errs),
            "clean def produces no error": "alpha.md" not in joined,
            "size warns, does not error": (any("fat.md" in w for w in warns)
                                           and not any("fat.md" in e for e in errs)),
            "size stays a warning, not an error tier": len(warns) == 1,
            "roster: charted stem with no def": any("phantom" in e for e in errs2),
            "roster: def absent from chart": any("nofm" in e and "absent" in e for e in errs2),
            "roster: charted-and-present not flagged": "`alpha`" not in j2,
            # regression guard: an earlier collector read only `agent`/`members`
            # and reported every department lead + CEO staffer as missing.
            "roster: collects `lead` key (not just `agent`)":
                not any("mismatch" in e and "absent" in e for e in errs2),
            "roster: collects `staff` list (not just `members`)":
                not any("nomodel" in e and "absent" in e for e in errs2),
            # Two entries, not one `and`: an and->or slip in a compound check
            # short-circuits past the half that matters and the selftest still
            # returns 0 — it cannot catch its own weakening.
            "frontmatter with colons in a value still parses":
                colon is not None,
            "frontmatter splits on the FIRST colon only":
                (colon or {}).get("description", "").startswith("Uses this:"),
        }
        for name, ok in checks.items():
            print(f"  [{'ok ' if ok else 'FAIL'}] {name}")
        allok = all(checks.values())
        print("selftest:", "PASS" if allok else "FAIL")
        return 0 if allok else 1


def main() -> None:
    ap = argparse.ArgumentParser(description="Lint ~/.claude/agents/ defs for size + frontmatter + roster drift.")
    ap.add_argument("--agents-dir", type=Path, default=DEFAULT_AGENTS_DIR)
    ap.add_argument("--org-chart", type=Path, default=DEFAULT_ORG_CHART,
                    help="org_chart.json to cross-check (pass --no-roster to skip)")
    ap.add_argument("--no-roster", action="store_true", help="skip the org-chart cross-check")
    ap.add_argument("--max-kb", type=float, default=DEFAULT_MAX_KB,
                    help=f"warn above this def size (default {DEFAULT_MAX_KB:.0f})")
    ap.add_argument("--quiet", action="store_true", help="print only problems")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        sys.exit(_selftest())
    if not a.agents_dir.is_dir():
        ap.error(f"agents dir not found: {a.agents_dir}")

    errors, warnings = lint(a.agents_dir, None if a.no_roster else a.org_chart, a.max_kb)
    for e in errors:
        print(f"✗ {e}")
    for w in warnings:
        print(f"⚠ {w}")
    if not errors and not warnings:
        if not a.quiet:
            print(f"agent_def_lint: clean — all defs under {a.max_kb:.0f}KB, "
                  f"frontmatter valid, roster in sync.")
    elif not a.quiet:
        print(f"\nagent_def_lint: {len(errors)} error(s), {len(warnings)} size warning(s).")
    sys.exit(1 if errors else 0)


# mutequiv: the __main__ guard cannot be killed by an in-process suite — the test
# imports this module (so the guard is False by construction) and the CLI tests
# invoke it as a subprocess (so it is True by construction). No fixture can
# observe it being wrong. Standard, and the only accepted equivalent here.
if __name__ == "__main__":
    main()
