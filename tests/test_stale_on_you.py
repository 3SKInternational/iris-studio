#!/usr/bin/env python3
"""Tests for scripts/stale_on_you.py — the C2 "Stale on you" surface generator.

Stdlib unittest only (repo convention). Run:
    python3 tests/test_stale_on_you.py
or under the suite:
    python3 -m unittest discover -s tests -v

Locks the three false-drop mechanisms found on 2026-08-25 (A-72), any of which
silently dropped an open Decision_Queue row from the single canonical
"awaiting Steve" list:

  1. Escaped pipes: `.split("|")` split on `\\|` inside wikilink aliases,
     shifting every later cell so a closed/HOLD row's status was mis-read as
     open (DQ-14/DQ-43 leaked in) and an open row's status was mis-read.
  2. Substring status match: `"closed" in status` dropped DQ-19 (prose said
     "app-CLOSED") and `"deferred" in status` dropped DQ-29 ("Deferred half").
  3. resolved_by_recent_decision over-match: a generic-token overlap dropped
     DQ-44 ({auto,build,nightly}); a DQ-id merely mentioned in a decision body
     dropped DQ-41/DQ-49.
"""
import contextlib
import datetime
import importlib.util
import io
import shutil
import tempfile
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "stale_on_you",
    Path(__file__).resolve().parent.parent / "scripts" / "stale_on_you.py",
)
soy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(soy)

HEADER = "| ID | Decision | Class | Default | Deadline | Days | Status |\n|---|---|---|---|---|---|---|\n"


def _parse(rows_md: str):
    """Point the module at a temp Decision_Queue holding rows_md, parse it."""
    orig = soy.DECISION_QUEUE
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as fh:
        fh.write(HEADER + rows_md)
        soy.DECISION_QUEUE = Path(fh.name)
    try:
        rows = soy.parse_decision_queue()
        skipped = soy.open_rows_skipped_by_parse()
    finally:
        soy.DECISION_QUEUE.unlink()
        soy.DECISION_QUEUE = orig
    return rows, skipped


class TestGlyphOpenDetection(unittest.TestCase):
    def test_open_prose_mentioning_closed_still_renders(self):
        # DQ-19 class: status leads with ⏳ but prose contains "app-CLOSED".
        rows, _ = _parse("| DQ-19 | Move the pulse | ENG | x | 2026-06-27 | 60 | ⏳ open — misses were app-CLOSED |\n")
        self.assertEqual([r["id"] for r in rows], ["DQ-19"])

    def test_open_prose_mentioning_deferred_still_renders(self):
        # DQ-29 class: status leads with ⏳ but prose contains "Deferred half".
        rows, _ = _parse("| DQ-29 | Arm triad-sync | ENG | x | 2026-07-04 | 52 | ⏳ open · 🔁 re-surfaced; Deferred half tracked |\n")
        self.assertEqual([r["id"] for r in rows], ["DQ-29"])

    def test_closed_row_does_not_render(self):
        rows, _ = _parse("| DQ-14 | Cancel ChatGPT | SPEND | x | trigger | 3 | ✅ CLOSED 6/18 |\n")
        self.assertEqual(rows, [])

    def test_hold_row_does_not_render(self):
        rows, _ = _parse("| DQ-43 | Quota decomposition | ENG | x | was 8/05 | 0 | ⏸ HOLD reopen-trigger only |\n")
        self.assertEqual(rows, [])

    def test_armed_row_does_not_render(self):
        rows, _ = _parse("| DQ-5 | Some armed item | ENG | x | none | 0 | 💤 armed |\n")
        self.assertEqual(rows, [])

    def test_bold_leading_glyph_still_open(self):
        rows, _ = _parse("| DQ-9 | Bold item | ENG | x | none | 1 | **⏳ open** still pending |\n")
        self.assertEqual([r["id"] for r in rows], ["DQ-9"])


class TestEscapedPipes(unittest.TestCase):
    def test_one_escaped_pipe_keeps_columns_aligned(self):
        rows, _ = _parse("| DQ-42 | Grant FDA [[foo\\|bar]] | ENG | x | 2026-08-03 | 22 | ⏳ open · awaiting call |\n")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["deadline"], "2026-08-03")
        self.assertEqual(rows[0]["class"], "ENG")

    def test_three_escaped_pipes_closed_still_excluded(self):
        # Without escape-aware split this shifted so status read as an open-ish
        # cell and the closed row leaked in.
        rows, _ = _parse("| DQ-47 | Shorts [[a\\|b]] [[c\\|d]] [[e\\|f]] | ENG | x | 2026-08-18 | 6 | ✅ PROCEEDING default fired |\n")
        self.assertEqual(rows, [])

    def test_escaped_pipe_content_normalised(self):
        rows, _ = _parse("| DQ-8 | Beehiiv [[x\\|y]] scale | SPEND | x | trig | 72 | ⏳ open → brief |\n")
        # The stored text carries a logical pipe, not the backslash-escape.
        self.assertNotIn("\\|", rows[0]["text"])


class TestParseDropTripwire(unittest.TestCase):
    def test_malformed_open_row_is_reported_not_silent(self):
        # An open row with too few cells must be surfaced, never dropped silently.
        rows, skipped = _parse("| DQ-99 | broken row | ⏳ open missing columns |\n")
        self.assertEqual(rows, [])
        self.assertIn("DQ-99", skipped)

    def test_well_formed_open_row_not_flagged(self):
        rows, skipped = _parse("| DQ-8 | Beehiiv scale | SPEND | x | trig | 72 | ⏳ open → brief |\n")
        self.assertEqual(skipped, [])
        self.assertEqual([r["id"] for r in rows], ["DQ-8"])


class TestExclusionReason(unittest.TestCase):
    DECS = [
        ("2026-08-06_adapts_ledger", "adapts catch rate ledger auto build nightly", "body text"),
        ("2026-08-22_fleet_dark", "incident fleet dark window", "prose mentions dq-41 and dq-49"),
        ("2026-08-25_dq-44_shipped", "dq-44 selector durable fix shipped", "body"),
    ]

    def test_generic_token_overlap_does_not_exclude(self):
        # DQ-44 false-drop by generic {auto,build,nightly} overlap is gone.
        self.assertEqual(
            soy.exclusion_reason({"id": "DQ-14", "text": "auto build nightly selector"}, self.DECS), ""
        )

    def test_specific_topic_overlap_does_not_exclude(self):
        # DQ-49 false-drop: incident report shares {fleet,dark} but is not a
        # decision. Topic-word overlap must never exclude.
        self.assertEqual(
            soy.exclusion_reason({"id": "DQ-49", "text": "oauth token expired fleet dark"}, self.DECS), ""
        )

    def test_id_only_in_body_does_not_exclude(self):
        # id mentioned in a body, not a title (incident referencing DQ-41).
        self.assertEqual(
            soy.exclusion_reason({"id": "DQ-41", "text": "sudo pmset wake"}, self.DECS), ""
        )

    def test_id_in_title_excludes(self):
        self.assertTrue(soy.exclusion_reason({"id": "DQ-44", "text": "selector"}, self.DECS))

    def test_resolved_wrapper_matches_reason(self):
        item = {"id": "DQ-44", "text": "selector"}
        self.assertEqual(
            soy.resolved_by_recent_decision(item, self.DECS),
            bool(soy.exclusion_reason(item, self.DECS)),
        )

    def test_idless_item_never_excluded(self):
        # An empty item_id builds an empty-boundary regex that matches at pos 0
        # of any title starting with a non-word char, so without the id-guard an
        # id-less INBOX item is dropped the moment such a decision exists — a
        # silent obligation. The guard must short-circuit before the search.
        decs = [("s", "(fyi) fleet dark note", "body")]
        self.assertEqual(soy.exclusion_reason({"text": "no id here"}, decs), "")
        self.assertEqual(soy.exclusion_reason({"id": "", "text": "blank"}, decs), "")

    def test_shorter_id_not_excluded_by_longer_id_title(self):
        # "dq-4" is a substring of "dq-44"; an open DQ-4 (a real, currently-open
        # row) must NOT be suppressed by a DQ-44 decision. Word-boundary, not `in`.
        decs = [("2026-08-25_dq-44", "dq-44 selector durable fix shipped", "body")]
        self.assertEqual(soy.exclusion_reason({"id": "DQ-4", "text": "epidemic"}, decs), "")
        self.assertTrue(soy.exclusion_reason({"id": "DQ-44", "text": "x"}, decs))


class TestParseTripwireEdgeCases(unittest.TestCase):
    def test_missing_queue_file_is_empty_not_crash(self):
        orig = soy.DECISION_QUEUE
        soy.DECISION_QUEUE = Path(tempfile.gettempdir()) / "stale_on_you_absent_test.md"
        try:
            self.assertEqual(soy.parse_decision_queue(), [])
            self.assertEqual(soy.open_rows_skipped_by_parse(), [])
        finally:
            soy.DECISION_QUEUE = orig

    def test_dq_prefix_without_number_does_not_crash(self):
        # Starts with "| DQ-" but has no DQ-\d+ id; the tripwire must skip it,
        # never crash on m.group(1).
        rows, skipped = _parse("| DQ- not a real id | ⏳ open |\n")
        self.assertEqual(rows, [])
        self.assertEqual(skipped, [])

    def test_malformed_non_open_row_not_flagged(self):
        # A too-few-cell row with NO ⏳ is not an awaiting-Steve drop; the loud
        # tripwire is only for open rows it could not read.
        rows, skipped = _parse("| DQ-71 | closed short row | ✅ done |\n")
        self.assertEqual(rows, [])
        self.assertEqual(skipped, [])

    def test_seven_cell_closed_row_with_incidental_hourglass_not_flagged(self):
        # DQ-45 class (real corpus): a well-formed 7-cell CLOSED row whose status
        # PROSE contains "⏳ open" ("Was: ⏳ open …"). It parses fine (just not as
        # open), so the tripwire must stay silent — a `len(cells) < 8` off-by-one
        # would falsely flag it as a silently-dropped open row.
        rows, skipped = _parse(
            "| DQ-45 | Approve footer | ENG | x | 2026-08-13 | 8 | ✅ PROCEEDING (default) — Was: ⏳ open surfaced |\n"
        )
        self.assertEqual(rows, [])
        self.assertEqual(skipped, [])


class TestMainReconciliation(unittest.TestCase):
    """main() end-to-end: the loud reconcile line + silent-drop tripwire that
    are the whole point of A-72 — an open row is rendered or excluded WITH a
    reason, never silently dropped."""

    def _run_main(self, dq_md, decisions):
        tmp = Path(tempfile.mkdtemp())
        orig = (soy.DECISION_QUEUE, soy.DECISIONS_LOG, soy.INBOX, soy.STATE_FILE)
        (tmp / "dq.md").write_text(HEADER + dq_md, encoding="utf-8")
        dlog = tmp / "Decisions_Log"
        dlog.mkdir()
        for name, body in decisions:
            (dlog / name).write_text(body, encoding="utf-8")
        (tmp / "INBOX.md").write_text("## 🧍 TODAY\n\n## 🧍 THIS WEEK\n", encoding="utf-8")
        soy.DECISION_QUEUE = tmp / "dq.md"
        soy.DECISIONS_LOG = dlog
        soy.INBOX = tmp / "INBOX.md"
        soy.STATE_FILE = tmp / "seen.tsv"
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = soy.main()
        finally:
            soy.DECISION_QUEUE, soy.DECISIONS_LOG, soy.INBOX, soy.STATE_FILE = orig
            shutil.rmtree(tmp, ignore_errors=True)
        return rc, out.getvalue(), err.getvalue()

    def test_excluded_row_dropped_kept_row_rendered(self):
        today = datetime.date.today().isoformat()
        rc, out, err = self._run_main(
            "| DQ-77 | Ship the thing | ENG | x | 2026-08-01 | 5 | ⏳ open awaiting call |\n"
            "| DQ-99 | Keep me here | ENG | x | 2026-08-02 | 3 | ⏳ open still pending |\n",
            [(f"{today}_dq-77_decided.md", "# DQ-77 selector shipped\n\nbody")],
        )
        self.assertEqual(rc, 0)
        self.assertIn("1 excluded", err)              # reconcile is loud
        self.assertNotIn("Ship the thing", out)       # DQ-77 excluded (if-False mutant fails here)
        self.assertIn("Keep me here", out)            # DQ-99 kept (if-True mutant fails here)
        self.assertNotIn("could not read open row", out)  # no tripwire when nothing malformed

    def test_malformed_open_row_warns_loudly(self):
        rc, out, err = self._run_main("| DQ-88 | broken open | ⏳ open missing cols |\n", [])
        self.assertEqual(rc, 0)
        self.assertIn("DQ-88", err)                   # BUG line to stderr
        self.assertIn("could not read open row", out)  # warning prepended to the section


if __name__ == "__main__":
    unittest.main()
