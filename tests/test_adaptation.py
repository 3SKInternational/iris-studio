#!/usr/bin/env python3
"""Tests for scripts/adaptation.py — the deterministic /adapt apply gate.

Stdlib unittest only (repo convention). Run:
    python3 tests/test_adaptation.py
or under the suite:
    python3 -m unittest discover -s tests -v

WHY THIS EXISTS. adaptation.py carries 36 tests in its own `--selftest`, but
precheck.sh only discovers tests/test_*.py — so the module that decides what an
assumed-hostile LLM proposer may rewrite had **zero** coverage at the gate.
Mutation on 2026-07-28 reported 11 survivors for exactly that reason: nothing was
running. This file puts the selftest inside the discovered suite and pins the
trust boundary directly, so a mutant in the allowlist or the frontmatter freeze
gets caught rather than shrugged at.

The boundary matters more since youtube-researcher joined the allowlist: it is the
first entry with Bash and web access, ingests untrusted content, and runs
unattended — see 06_CEO/Decisions_Log/2026-07-28_adapts_allowlist_youtube_researcher.md.
"""
import importlib.util
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("adaptation_under_test",
                                               REPO / "scripts" / "adaptation.py")
adapt = importlib.util.module_from_spec(_spec)
# Registered before exec: @dataclass resolves its module via sys.modules, and an
# unregistered name makes it raise 'NoneType has no attribute __dict__'. This is
# the same failure the daemon logs as "adaptation apply core unavailable".
sys.modules["adaptation_under_test"] = adapt
_spec.loader.exec_module(adapt)


class TestSelftest(unittest.TestCase):
    def test_module_selftest_passes(self):
        """Runs the module's own 36-case suite where precheck can see it."""
        self.assertEqual(adapt._selftest(), 0)


class TestAllowlistBoundary(unittest.TestCase):
    """WHICH files an assumed-hostile proposer may touch."""

    def _target(self, name):
        return str(Path(adapt.AGENTS_DIR) / name)

    def test_content_agents_are_editable(self):
        for name in ("scriptwriter.md", "image-reviewer.md", "youtube-researcher.md"):
            with self.subTest(name=name):
                self.assertIn(name, adapt.ALLOWED_AGENT_NAMES)

    def test_youtube_researcher_is_allowlisted(self):
        """Steve's 2026-07-28 decision — pinned so a refactor cannot drop it."""
        self.assertIn("youtube-researcher.md", adapt.ALLOWED_AGENT_NAMES)

    def test_safety_machinery_is_never_editable(self):
        """The invariant the allowlist exists for. No self-modification, and no
        proposal may rewrite the reviewers or the engineering agents that carry
        Edit/Write/Bash."""
        for name in ("adaptation-proposer.md", "skeptical-code-reviewer.md",
                     "senior-engineer.md", "senior-systems-architect.md",
                     "chief.md", "chief-of-staff.md", "canon-reviewer.md"):
            with self.subTest(name=name):
                self.assertNotIn(name, adapt.ALLOWED_AGENT_NAMES)

    def test_allowlist_has_not_silently_grown(self):
        """A bare count, so adding an entry forces a conscious edit here and a
        look at the Decisions_Log rationale."""
        self.assertEqual(len(adapt.ALLOWED_AGENT_NAMES), 8)


class TestFrontmatterFreeze(unittest.TestCase):
    """WHAT PART of an allowed file may change. Capability lines: never."""

    AGENT = ("---\nname: scriptwriter\ndescription: d\n"
             "tools: Read, Write, Grep\nmodel: sonnet\n---\n\nBody.\n")

    def _assert_refused(self, before, after):
        with self.assertRaises(adapt.AdaptError):
            adapt._assert_no_capability_change(before, after, Path("scriptwriter.md"))

    def test_adding_a_tool_is_refused(self):
        """The headline case: an approved edit must never hand a new capability
        to an agent that runs unattended."""
        self._assert_refused(
            self.AGENT, self.AGENT.replace("tools: Read, Write, Grep",
                                           "tools: Read, Write, Grep, Bash"))

    def test_removing_a_tool_is_refused(self):
        """Both directions — silently dropping a tool breaks the agent as surely
        as adding one endangers it."""
        self._assert_refused(
            self.AGENT, self.AGENT.replace("tools: Read, Write, Grep", "tools: Read"))

    def test_model_tier_change_is_refused(self):
        """`model:` is a locked value needing a Decisions_Log entry; an
        auto-apply would move a spend tier with no record of why."""
        self._assert_refused(self.AGENT, self.AGENT.replace("model: sonnet", "model: opus"))

    def test_name_rewrite_is_refused(self):
        self._assert_refused(
            self.AGENT, self.AGENT.replace("name: scriptwriter", "name: other"))

    def test_body_only_edit_is_allowed(self):
        """The guard must not block the legitimate case it sits beside."""
        adapt._assert_no_capability_change(
            self.AGENT, self.AGENT.replace("Body.", "Better body."),
            Path("scriptwriter.md"))

    def test_file_without_frontmatter_is_allowed(self):
        """Content-standard docs in ALLOWED_STANDARDS have no frontmatter and
        must stay editable."""
        adapt._assert_no_capability_change("plain body\n", "plain body v2\n",
                                           Path("Standard.md"))

    def test_bare_keyword_line_in_frontmatter_is_refused(self):
        """A bare `tools` line is exactly the continuation shape that granted Bash
        — under the wholesale freeze it refuses because the BLOCK changed, with no
        need to decide what YAML makes of it."""
        after = self.AGENT.replace("description: d", "description: d\ntools")
        self._assert_refused(self.AGENT, after)

    def test_diagnostic_reader_does_not_invent_a_field_from_a_bare_line(self):
        """`_frozen_fields` is diagnostics-only now, but it still must not report
        a phantom `tools` change — a wrong explanation on a real refusal sends the
        approver hunting the wrong thing."""
        fields = adapt._frozen_fields(
            "---\nname: x\ntools\ntools: Read, Bash\nmodel: sonnet\n---\n\nbody\n")
        self.assertEqual(fields.get("tools"), ["Read, Bash"])

    def test_a_real_tools_line_is_captured_past_a_bare_one(self):
        """The genuine field must be recorded, whatever noise precedes it."""
        fields = adapt._frozen_fields(
            "---\nname: x\ntools\ntools: Read, Bash\nmodel: sonnet\n---\n\nbody\n")
        self.assertEqual(fields.get("tools"), ["Read, Bash"])

    # --- duplicate-key escalation ------------------------------------------
    # The shape a hostile proposal would actually take: ADD a second capability
    # line instead of editing the first. An earlier guard compared first-wins
    # while every loader resolves last-wins, so these all APPLIED and escalated.
    # Confirmed exploitable end-to-end through apply_proposal, 2026-07-28.

    def test_appended_duplicate_tools_line_is_refused(self):
        after = self.AGENT.replace(
            "model: sonnet", "tools: Read, Write, Grep, Bash\nmodel: sonnet")
        self._assert_refused(self.AGENT, after)

    def test_prepended_duplicate_tools_line_is_refused(self):
        """Order must not matter — the original only refused this one by accident."""
        after = self.AGENT.replace(
            "tools: Read, Write, Grep", "tools: Bash\ntools: Read, Write, Grep")
        self._assert_refused(self.AGENT, after)

    def test_indented_duplicate_tools_line_is_refused(self):
        after = self.AGENT.replace(
            "model: sonnet", "  tools: Read, Write, Grep, Bash\nmodel: sonnet")
        self._assert_refused(self.AGENT, after)

    def test_tab_prefixed_duplicate_tools_line_is_refused(self):
        after = self.AGENT.replace(
            "model: sonnet", "\ttools: Read, Write, Grep, Bash\nmodel: sonnet")
        self._assert_refused(self.AGENT, after)

    def test_appended_duplicate_model_line_is_refused(self):
        after = self.AGENT.replace("model: sonnet", "model: sonnet\nmodel: opus")
        self._assert_refused(self.AGENT, after)

    def test_duplicate_escalation_is_refused_with_crlf_frontmatter(self):
        crlf = self.AGENT.replace("\n", "\r\n")
        after = crlf.replace("model: sonnet", "tools: Bash\r\nmodel: sonnet")
        self._assert_refused(crlf, after)

    def test_refusal_message_names_the_changed_field_when_it_can(self):
        """The refusal text is part of the control, not decoration — an approver
        who cannot tell WHY a plausible proposal was refused learns to distrust
        the freeze."""
        after = self.AGENT.replace("tools: Read, Write, Grep", "tools: Bash")
        with self.assertRaises(adapt.AdaptError) as cm:
            adapt._assert_no_capability_change(self.AGENT, after, Path("scriptwriter.md"))
        self.assertIn("`tools:`", str(cm.exception))

    def test_refusal_says_so_when_no_frozen_field_parses_as_changed(self):
        """The important case: a YAML continuation changes the effective value
        while every line-based read sees the same fields. The message must flag
        that explicitly rather than implying a benign diff — that gap IS the
        bypass class this guard exists to stop."""
        after = self.AGENT.replace(
            "tools: Read, Write, Grep", "tools: Read, Write, Grep\n  , Bash")
        with self.assertRaises(adapt.AdaptError) as cm:
            adapt._assert_no_capability_change(self.AGENT, after, Path("scriptwriter.md"))
        self.assertIn("cannot see", str(cm.exception))

    def test_frozen_fields_records_every_occurrence(self):
        """The property the refusals rest on: no occurrence is silently dropped,
        so the guard cannot disagree with a last-wins loader."""
        fields = adapt._frozen_fields(
            "---\nname: x\ntools: Read\nmodel: sonnet\ntools: Read, Bash\n---\n\nb\n")
        self.assertEqual(fields.get("tools"), ["Read", "Read, Bash"])

    # --- YAML continuation: the round-5 bypass -----------------------------
    # `tools:` is a plain scalar, so YAML folds a more-indented following line
    # into its value. A line-based reader skips that line (no colon) and sees the
    # `tools:` line byte-identical — so did the human approver. Hence the freeze
    # now compares the raw block and never parses to decide.

    def test_yaml_continuation_comma_form_is_refused(self):
        after = self.AGENT.replace(
            "tools: Read, Write, Grep", "tools: Read, Write, Grep\n  , Bash")
        self._assert_refused(self.AGENT, after)

    def test_yaml_continuation_bare_form_is_refused(self):
        after = self.AGENT.replace(
            "tools: Read, Write, Grep", "tools: Read, Write, Grep\n  Bash")
        self._assert_refused(self.AGENT, after)

    def test_any_frontmatter_change_is_refused_even_a_harmless_one(self):
        """Deliberate over-refusal, and the reason it costs nothing: NONE of the
        81 adaptations applied to date has ever edited frontmatter. Freezing the
        block wholesale removes the entire parser-disagreement bug class instead
        of patching the two shapes we happened to find."""
        after = self.AGENT.replace("description: d", "description: d\nnote: extra")
        self._assert_refused(self.AGENT, after)

    def test_whitespace_only_frontmatter_change_is_refused(self):
        """Byte-exact: even a reflow is a commit-level act, not an adaptation."""
        self._assert_refused(self.AGENT, self.AGENT.replace("model: sonnet",
                                                            "model:  sonnet"))

    def test_body_edit_is_unaffected_by_the_wholesale_freeze(self):
        """The freeze must not spill into the body — that is the whole job."""
        adapt._assert_no_capability_change(
            self.AGENT, self.AGENT.replace("Body.", "Much better body."),
            Path("scriptwriter.md"))

    def test_adding_frontmatter_to_a_file_that_had_none_is_refused_cleanly(self):
        """Standards docs carry no frontmatter, so a proposal can try to ADD a
        block. That must raise AdaptError, not crash: the diagnostic reader gets
        called on frontmatter-less text in this path, and an AttributeError there
        would lose the refusal message and read as a bug rather than a block.
        """
        with self.assertRaises(adapt.AdaptError) as cm:
            adapt._assert_no_capability_change(
                "plain standards body\n",
                "---\ntools: Bash\n---\n\nplain standards body\n",
                Path("Standard.md"))
        self.assertIn("frontmatter", str(cm.exception))

    def test_removing_frontmatter_entirely_is_refused_cleanly(self):
        """The inverse, same crash surface — diagnostics run on the AFTER text."""
        with self.assertRaises(adapt.AdaptError) as cm:
            adapt._assert_no_capability_change(self.AGENT, "Body.\n", Path("x.md"))
        self.assertIn("frontmatter", str(cm.exception))

    # --- fail-closed on unidentifiable frontmatter (round-6 finding) --------
    # `_FRONTMATTER_RE` is anchored \A---\r?\n. A BOM, leading whitespace or
    # CR-only endings defeat that anchor while a YAML loader may still read a
    # block — and returning "" for both sides compared ""=="" and ALLOWED a
    # tools: edit. A guard must not go quiet exactly when it cannot identify its
    # input. Not attacker-reachable (the loop writes utf-8/newline="" and cannot
    # introduce a BOM; prepending one in a proposal refuses) — hardening.

    def _fm_variant(self, text):
        return adapt._frontmatter_block(text)

    def test_bom_prefixed_frontmatter_fails_closed(self):
        bom = "﻿" + self.AGENT
        self._assert_refused(bom, bom.replace("tools: Read, Write, Grep", "tools: Bash"))

    def test_cr_only_line_endings_fail_closed(self):
        cr = self.AGENT.replace("\n", "\r")
        self._assert_refused(cr, cr.replace("tools: Read, Write, Grep", "tools: Bash"))

    def test_leading_blank_line_fails_closed(self):
        nl = "\n" + self.AGENT
        self._assert_refused(nl, nl.replace("tools: Read, Write, Grep", "tools: Bash"))

    def test_unidentifiable_block_is_not_the_empty_string(self):
        """The property the refusals rest on: an unparsable head must NOT collapse
        to "", or it compares equal to a genuinely frontmatter-less file."""
        self.assertNotEqual(self._fm_variant("﻿" + self.AGENT), "")
        self.assertNotEqual(self._fm_variant(self.AGENT.replace("\n", "\r")), "")

    def test_body_horizontal_rule_is_not_mistaken_for_frontmatter(self):
        """The discriminator is 'first real content is a fence', not 'a fence
        appears near the top' — 14 of 44 live defs carry `---` rules in their
        bodies, and those files must stay editable."""
        doc = "# Title\n\nintro\n\n---\n\nsection two\n"
        self.assertEqual(self._fm_variant(doc), "")
        adapt._assert_no_capability_change(
            doc, doc.replace("section two", "section 2"), Path("Standard.md"))

    def test_head_window_is_8192_bytes(self):
        """A deliberate tuning value in a security path — raising it refuses more
        legitimate body edits, lowering it risks a fence past the window. Pinned
        so it cannot drift; changing it should be a conscious edit here too."""
        self.assertEqual(adapt._UNIDENTIFIABLE_HEAD_BYTES, 8192)

    def test_change_inside_the_head_window_is_refused(self):
        """The window is what makes the fail-closed path meaningful: a change near
        the fence of an unidentifiable file must still refuse."""
        bom = "﻿" + self.AGENT + "x" * 200 + "\n"
        self._assert_refused(bom, bom.replace("model: sonnet", "model: opus"))

    def test_deep_body_edit_on_a_bom_file_still_applies(self):
        """Comparing a bounded head, not the whole file, keeps an edit far below
        the fence applyable — over-refusing everything would be its own failure."""
        bom = "﻿" + self.AGENT + "x" * 9000 + "\nTAIL\n"
        adapt._assert_no_capability_change(
            bom, bom.replace("TAIL", "TAIL2"), Path("scriptwriter.md"))

    def test_frontmatter_block_extraction_is_verbatim(self):
        """The decision rests on this being the raw block, not a normalisation."""
        self.assertEqual(adapt._frontmatter_block(self.AGENT),
                         self.AGENT[:self.AGENT.index("\n\nBody.")] + "\n")
        self.assertEqual(adapt._frontmatter_block("no frontmatter\n"), "")

    def test_frozen_keys_cover_all_three(self):
        self.assertEqual(set(adapt.FROZEN_FRONTMATTER_KEYS), {"tools", "model", "name"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
