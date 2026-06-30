"""Tests for agent_output_lint.py — A-32 VO-density check + A-23 no-regression.

Run: python scripts/test_agent_output_lint.py  (exit 0 = all pass)
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "agent_output_lint", Path(__file__).with_name("agent_output_lint.py")
)
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)


def test_vo_density():
    # 1: V05-shape — target 12 min, ~1,786 words → ratio ~0.99 → clean
    f1 = "Runtime target: 12 min\n\n**VO:**\n" + ("hello world " * 893) + "\n\n**B-roll:** x\n"
    r1 = m.check_vo_density(f1)
    assert r1["applicable"] and not r1["should_alert"], r1

    # 2: V01-shape — target 15 min, 700 words → ratio ~0.31 → flag
    f2 = "Runtime target: 15:00\n\n**VO:**\n" + ("word " * 700) + "\n"
    r2 = m.check_vo_density(f2)
    assert r2["applicable"] and r2["should_alert"], r2
    assert r2["word_count"] == 700, r2

    # 3: no runtime target — skip silent (applicable False)
    r3 = m.check_vo_density("**VO:**\n" + ("word " * 50))
    assert not r3["applicable"] and not r3["should_alert"], r3

    # 4: runtime target present but zero VO blocks — applicable, must NOT flag
    r4 = m.check_vo_density("Runtime target: 10 min\n\nJust prose, no VO markers.\n")
    assert r4["applicable"] and not r4["should_alert"], r4
    assert r4["word_count"] == 0, r4

    # 5: multiple VO blocks summed; MM:SS target parsed
    f5 = ("Runtime target: 2:00\n\n**VO:**\n" + ("a " * 120) + "\n\n"
          "**Image:** x\n\n**VO:**\n" + ("b " * 120) + "\n")
    r5 = m.check_vo_density(f5)
    assert r5["applicable"] and r5["vo_block_count"] == 2 and r5["word_count"] == 240, r5
    assert not r5["should_alert"], r5  # 240/150=1.6min vs 2min target = 0.8 > 0.7

    # 6: INLINE VO — live scriptwriter shape "**VO:** narration on same line",
    # interleaved with **B-roll:**/**Image:** markers (which must terminate the
    # block, not be counted as VO). 3 VO lines of 100 words each = 300 words.
    f6 = (
        "Runtime target: 3 min\n\n"
        "**VO:** " + ("w " * 100) + "\n"
        "**B-roll:** a busy office, lots of detail words here\n\n"
        "**VO:** " + ("w " * 100) + "\n"
        "**Image:** prompt text describing a scene\n\n"
        "**VO:** " + ("w " * 100) + "\n"
    )
    r6 = m.check_vo_density(f6)
    assert r6["vo_block_count"] == 3 and r6["word_count"] == 300, r6
    # 300/150=2min vs 3min target = 0.667 < 0.70 → flag
    assert r6["should_alert"], r6

    # 7: live markdown target form "- **Runtime target:** 19:49" must be detected,
    # and a frontmatter "runtime_estimate:" / mid-line "| Runtime: ~19:49" must NOT
    # false-match.
    assert m.check_vo_density("- **Runtime target:** 19:49\n\n**VO:** " + ("w " * 100))["applicable"]
    assert m.check_vo_density('runtime_estimate: "19:49"\n')["applicable"] is False
    assert m.check_vo_density("Formula: X | Runtime: ~19:49 here\n")["applicable"] is False

    print("VO density: 7/7 pass")


def test_full_lint_and_render(tmp: Path):
    # Under-dense script routes should_alert + report has the VO section.
    f = "Runtime target: 15:00\n\n**VO:**\n" + ("word " * 700) + "\n"
    p = tmp / "Video_99_Script.md"
    p.write_text(f, encoding="utf-8")
    res = m.lint(p)
    assert res["should_alert"] and res["worth_reporting"], res
    report = m.format_report(res)
    assert "VO density check" in report and "Low density" in report, report

    # A-23 no-regression: a banned word still flags; clean prose stays clean.
    assert m.check_banned_vocab("Use cinematic lighting here."), "banned regression"
    assert not m.check_banned_vocab("A flat 2D chibi character."), "false positive"
    print("full lint + A-23 regression: pass")


if __name__ == "__main__":
    import tempfile
    test_vo_density()
    with tempfile.TemporaryDirectory() as d:
        test_full_lint_and_render(Path(d))
    print("ALL PASS")
