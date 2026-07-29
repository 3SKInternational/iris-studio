#!/usr/bin/env python3
"""Regression tests for the VO budget ledger (vo_factory/model_allocator.commit).

Locks the money-correctness invariants two real bugs broke (video-factory audit,
2026-06-22):

  H1  — UNDER-count (~94%): commit() reversed the FULL prior same-note booking on
        every same-note write, but a non-`--force` partial top-up only re-books the
        scenes it just rendered. Net spend was under-counted by the already-rendered
        remainder, defeating the monthly cap. Fixed with replace-vs-add semantics:
        `replace=True` (a --force full re-render) SUPERSEDES; `replace=False` (a
        partial top-up) ADDS only this run's scenes.

  H1b — OVER-count (~100%): on the `actual=None` reconciliation fallback, commit()
        booked d.credits_est (the WHOLE kit) additively, so two top-ups of one kit
        summed to 2x. Fixed with `est_override`: the caller passes THIS run's
        per-scene estimate (make_chars * rate); precedence is
        actual_credits -> est_override -> full-kit d.credits_est.

Plus the v2-slot accounting: a video consumes at most ONE v2 slot per cycle across
any number of top-ups, and a --force v2 re-render nets exactly one slot (reverse +
re-add), never drifting.

  2026-07-28 fix — credits_used monotonically additive on replace=True: ElevenLabs
  meters per SUBMITTED character, so a --force full re-render bills the ORIGINAL
  render's characters AGAIN (a second real charge), but commit() used to REVERSE
  the prior same-note booking before adding the new one -- so credits_used tracked
  only the LATEST render's cost, not real cumulative ElevenLabs consumption (a
  20-scene kit: real consumption 40, credits_used said 20). Fixed: credits_used is
  never reversed; only the v2_count SLOT (a cap on concurrent premium bookings,
  not a spend counter) is still reversed on a same-note supersede.

Stdlib unittest only — no network, no API keys, no real vault, no state file writes
(every commit runs against an in-memory dict). Run:
    python3 tests/test_vo_budget_ledger.py
or under the suite:
    python3 -m unittest discover -s tests
"""

import importlib.util
import math
import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
_MODEL_ALLOCATOR = _REPO / "vo_factory" / "model_allocator.py"


def _load_model_allocator():
    # NOTE: register in sys.modules BEFORE exec_module. model_allocator defines a
    # @dataclass (Decision); under Python 3.9 the dataclass machinery resolves
    # sys.modules[cls.__module__].__dict__, which raises AttributeError on None if
    # the dynamically-loaded module isn't registered first.
    spec = importlib.util.spec_from_file_location("model_allocator_under_test",
                                                  _MODEL_ALLOCATOR)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


ma = _load_model_allocator()

# Minimal cfg mirroring the real vo_budget_config.json shape: v2 = 1.0 cr/char,
# flash = 0.5 cr/char, 4 v2 slots/cycle, 10% buffer on 100k credits.
CFG = {
    "monthly_credits": 100000,
    "buffer_pct": 0.1,
    "billing_cycle_day": 1,
    "default_voice_id": "v",
    "models": {
        "v2": {"id": "eleven_multilingual_v2", "credits_per_char": 1.0},
        "flash": {"id": "eleven_flash_v2_5", "credits_per_char": 0.5},
    },
    "allocation": {
        "fallback_model": "flash",
        "v2_score_threshold": 0.5,
        "max_v2_per_cycle": 4,
    },
}


def _fresh():
    return {"period_start": "2026-06-01", "credits_used": 0, "v2_count": 0, "log": []}


def _dec(model_key, credits):
    return ma.Decision(
        model_key=model_key, model_id=CFG["models"][model_key]["id"], voice_id="v",
        score=0.9 if model_key == "v2" else 0.1, credits_est=credits,
        reason="test", would_exceed_budget=False, chars=credits)


def _est(chars, model_key):
    return int(math.ceil(chars * CFG["models"][model_key]["credits_per_char"]))


class TestH1ReplaceVsAdd(unittest.TestCase):
    """H1: --force supersedes, non-force partial top-up adds (with REAL deltas)."""

    def test_partial_top_up_stacks_not_replaces(self):
        # Initial run books 2 scenes (2000 cr), top-up books the other 3 (3000 cr).
        # Net MUST be 5000 (the H1 bug netted 3000 — the top-up reversed the prior).
        s = _fresh()
        ma.commit(s, _dec("flash", 2000), CFG, note="V1", actual_credits=2000, replace=True)
        ma.commit(s, _dec("flash", 3000), CFG, note="V1", actual_credits=3000, replace=False)
        self.assertEqual(s["credits_used"], 5000)

    def test_force_re_render_supersedes(self):
        # --force re-renders the WHOLE kit -- ElevenLabs bills the resubmitted
        # characters AGAIN, a second real charge. credits_used must be additive
        # (2026-07-28 fix): 5000 then 4800 -> 9800, not a replace down to 4800
        # (which would under-count real cumulative spend by the first render).
        s = _fresh()
        ma.commit(s, _dec("flash", 5000), CFG, note="V2", actual_credits=5000, replace=True)
        ma.commit(s, _dec("flash", 4800), CFG, note="V2", actual_credits=4800, replace=True)
        self.assertEqual(s["credits_used"], 9800)

    def test_two_videos_accumulate_independently(self):
        s = _fresh()
        ma.commit(s, _dec("v2", 1000), CFG, note="V3", actual_credits=1000, replace=True)
        ma.commit(s, _dec("v2", 2000), CFG, note="V4", actual_credits=2000, replace=True)
        self.assertEqual(s["credits_used"], 3000)
        self.assertEqual(s["v2_count"], 2)

    def test_note_less_commits_always_append(self):
        # An empty note matches nothing, so note-less commits never supersede.
        s = _fresh()
        ma.commit(s, _dec("flash", 1000), CFG, note="", replace=True)
        ma.commit(s, _dec("flash", 1000), CFG, note="", replace=True)
        self.assertEqual(s["credits_used"], 2000)


class TestH1bEstimateFallback(unittest.TestCase):
    """H1b: on the actual=None fallback, an additive top-up must book only THIS
    run's per-scene estimate, never the full-kit estimate."""

    def test_partial_top_up_actual_none_books_per_run_not_full_kit(self):
        # 10000-char flash kit (full-kit est 5000). Run 1 renders 4000 chars
        # (est 2000); top-up renders the remaining 6000 chars (est 3000). With
        # actual=None both times, net MUST be 5000 — the H1b bug booked the
        # full-kit 5000 twice -> 10000.
        s = _fresh()
        dec = _dec("flash", 5000)  # full-kit estimate
        ma.commit(s, dec, CFG, note="V5", actual_credits=None, replace=True,
                  est_override=_est(4000, "flash"))
        ma.commit(s, dec, CFG, note="V5", actual_credits=None, replace=False,
                  est_override=_est(6000, "flash"))
        self.assertEqual(s["credits_used"], 5000)

    def test_retry_storm_is_bounded_not_full_kit_blowup(self):
        # A top-up that keeps failing+retrying books only its own small scene each
        # time — bounded linear growth, never the full-kit multiply the bug caused.
        s = _fresh()
        dec = _dec("flash", 5000)
        ma.commit(s, dec, CFG, note="V6", actual_credits=None, replace=True,
                  est_override=_est(8000, "flash"))  # 9 scenes land = 4000
        for _ in range(5):  # 5 replays of one 2000-char scene = 1000 each
            ma.commit(s, dec, CFG, note="V6", actual_credits=None, replace=False,
                      est_override=_est(2000, "flash"))
        self.assertEqual(s["credits_used"], 4000 + 5 * 1000)

    def test_force_actual_none_supersedes_to_full_kit(self):
        # On --force the est_override equals the full-kit estimate (renders all
        # scenes) each time; credits_used is additive across both force renders
        # (2026-07-28 fix) -> 5000 + 5000 = 10000, not a replace down to 5000.
        s = _fresh()
        dec = _dec("flash", 5000)
        ma.commit(s, dec, CFG, note="V7", actual_credits=None, replace=True,
                  est_override=_est(10000, "flash"))
        ma.commit(s, dec, CFG, note="V7", actual_credits=None, replace=True,
                  est_override=_est(10000, "flash"))
        self.assertEqual(s["credits_used"], 10000)

    def test_est_override_none_falls_back_to_full_kit(self):
        # Back-compat: with no est_override and no actual, book d.credits_est.
        s = _fresh()
        ma.commit(s, _dec("flash", 3333), CFG, note="V8", actual_credits=None, replace=True)
        self.assertEqual(s["credits_used"], 3333)

    def test_actual_credits_wins_over_est_override(self):
        # A real subscription delta is authoritative over any estimate.
        s = _fresh()
        ma.commit(s, _dec("flash", 5000), CFG, note="V9", actual_credits=1234,
                  replace=True, est_override=9999)
        self.assertEqual(s["credits_used"], 1234)


class TestV2SlotAccounting(unittest.TestCase):
    """A video consumes at most ONE v2 slot per cycle, stable across top-ups and
    force re-renders."""

    def test_v2_slot_counted_once_across_top_ups(self):
        s = _fresh()
        ma.commit(s, _dec("v2", 1000), CFG, note="V10", actual_credits=1000, replace=True)
        ma.commit(s, _dec("v2", 1500), CFG, note="V10", actual_credits=1500, replace=False)
        self.assertEqual(s["v2_count"], 1)
        self.assertEqual(s["credits_used"], 2500)

    def test_force_v2_re_render_keeps_one_slot(self):
        # The SLOT stays at 1 (unchanged behavior); credits_used is additive
        # across both real charges (2026-07-28 fix) -> 1000 + 1200 = 2200.
        s = _fresh()
        ma.commit(s, _dec("v2", 1000), CFG, note="V11", actual_credits=1000, replace=True)
        ma.commit(s, _dec("v2", 1200), CFG, note="V11", actual_credits=1200, replace=True)
        self.assertEqual(s["v2_count"], 1)
        self.assertEqual(s["credits_used"], 2200)

    def test_v2_to_flash_force_flip_releases_slot(self):
        # Budget filled between runs -> a --force re-render flips v2->flash; the
        # superseded v2 entry's slot must be released back to 0.
        s = _fresh()
        ma.commit(s, _dec("v2", 1000), CFG, note="V12", actual_credits=1000, replace=True)
        ma.commit(s, _dec("flash", 800), CFG, note="V12", actual_credits=800, replace=True)
        self.assertEqual(s["v2_count"], 0)

    def test_v2_count_never_underflows_on_corrupt_log(self):
        # v2_count's reversal can still underflow on a hand-edited/corrupt state
        # (the log claims a v2 slot that live v2_count doesn't have) -- the
        # max(...,0) clamp must catch it. credits_used is NO LONGER reversed
        # (2026-07-28 fix) so it cannot underflow via this path at all -- it is
        # simply prior + booked.
        s = _fresh()
        s["log"] = [{"note": "V13", "model": "v2", "credits": 9999}]
        s["credits_used"] = 10
        s["v2_count"] = 0
        ma.commit(s, _dec("flash", 100), CFG, note="V13", actual_credits=100, replace=True)
        # v2_count reversal underflows to -1 before the max(...,0) clamp pins it at
        # exactly 0. Asserting the exact clamped value (not just >= 0) still fails
        # if the clamp is removed.
        self.assertEqual(s["v2_count"], 0)
        # credits_used = prior (10, untouched by the reversal loop) + booked (100).
        self.assertEqual(s["credits_used"], 110)

    def test_v2_count_key_entirely_missing_defaults_to_zero_not_one(self):
        # ROUND-2 (2026-07-28 review): pins the reversal's OWN default (the
        # first state.get("v2_count", 0) inside the once-per-note fix), not
        # just the final clamp. An even MORE corrupt shape than the test
        # above: v2_count is ABSENT entirely (never even initialized), not
        # merely 0 -- load_state() never produces this in production, but the
        # exact default used here compounds with the very next line's own
        # state.get("v2_count", 0) + 1 (a v2 re-add in the SAME commit call),
        # so a wrong default here silently changes the final clamped result.
        s = {"period_start": "2026-06-01", "credits_used": 0,
             "log": [{"note": "V25", "model": "v2", "credits": 500}]}
        assert "v2_count" not in s
        ma.commit(s, _dec("v2", 600), CFG, note="V25", actual_credits=600, replace=True)
        self.assertEqual(s["v2_count"], 0)

    def test_v2_slot_does_not_leak_after_multiple_top_ups_then_force(self):
        # ROUND-2 REGRESSION (2026-07-28 review). The reversal loop used to
        # decrement v2_count once per MATCHING LOG ENTRY, but a note holds at
        # most ONE slot. Two additive top-ups (replace=False) on an already-v2
        # note each append their own v2 log entry without claiming a second
        # slot (note_has_v2 short-circuits that) -- so 3 v2 entries end up
        # backing exactly 1 occupied slot. A subsequent --force must reverse
        # that ONE slot, not one-per-entry: reproduced exactly, this used to
        # take v2_count from 1 to 0 (a 3x-matching-entry over-reversal,
        # clamped at 0 by max(...,0) rather than going negative), silently
        # granting one extra 2x-cost v2 render past the per-cycle cap.
        s = _fresh()
        ma.commit(s, _dec("v2", 1000), CFG, note="V23", actual_credits=1000, replace=True)
        ma.commit(s, _dec("v2", 500), CFG, note="V23", actual_credits=500, replace=False)
        ma.commit(s, _dec("v2", 500), CFG, note="V23", actual_credits=500, replace=False)
        self.assertEqual(s["v2_count"], 1, "3 matching v2 entries must still back exactly 1 slot")
        ma.commit(s, _dec("v2", 900), CFG, note="V23", actual_credits=900, replace=True)
        self.assertEqual(s["v2_count"], 1,
                        "a --force on a note with 3 accumulated v2 entries must "
                        "reverse the slot ONCE (net back to 1), not underflow to 0")


class TestLedgerReconciles(unittest.TestCase):
    """2026-07-28 round-2 audit finding: the purge that used to drop superseded
    same-note log entries deleted the itemization credits_used depends on for an
    audit trail, even after credits_used stopped being reversed. The log's own
    entries must always sum to credits_used -- an unauditable total (matching no
    itemization) can't be reconciled against the ElevenLabs dashboard, the one
    external check on it."""

    def test_log_sums_to_credits_used_after_force_rerender(self):
        s = _fresh()
        ma.commit(s, _dec("flash", 1000), CFG, note="V24", actual_credits=1000, replace=True)
        ma.commit(s, _dec("flash", 1100), CFG, note="V24", actual_credits=1100, replace=True)
        ma.commit(s, _dec("flash", 1200), CFG, note="V24", actual_credits=1200, replace=True)
        self.assertEqual(s["credits_used"], 3300)
        self.assertEqual(sum(e["credits"] for e in s["log"]), s["credits_used"],
                        "the log must itemize the total it explains, not just sum to it "
                        "by coincidence -- every commit's entry must survive on disk")
        self.assertEqual(len(s["log"]), 3, "superseded same-note entries must be RETAINED")


class TestV2SlotSurvivesRetention(unittest.TestCase):
    """ROUND-3 (2026-07-28 review): retention re-broke the slot count.

    Round 2 fixed the v2 slot leak, then — in the SAME round — stopped purging
    superseded log entries so the ledger could reconcile (TestLedgerReconciles
    above). Retention silently widened every `any(...)` scan over the log, and the
    slot predicate started matching bookings that had already been released:

        v2(replace)    -> 1   correct
        flash(replace) -> 0   correct, the slot is released
        v2(replace)    -> 0   WRONG (want 1) -- re-reversed an already-released slot

    `slot_open = v2_count < max_v2_per_cycle`, so the under-count grants an extra
    2x-cost v2 render past the per-cycle cap. Reachable in production because
    choose_model re-scores from script text every run, so an edit between --force
    runs genuinely flips v2 -> flash -> v2.

    The fix scopes both predicates to _note_window(): entries from the note's LAST
    replace=True onward. These cases fail if that window is removed."""

    def _seq(self, *models):
        s = _fresh()
        counts = []
        for m in models:
            ma.commit(s, _dec(m, 1000), CFG, note="V13", actual_credits=1000, replace=True)
            counts.append(s["v2_count"])
        return counts

    def test_v2_then_flash_then_v2_reclaims_the_slot(self):
        self.assertEqual(self._seq("v2", "flash", "v2"), [1, 0, 1],
                         "a released slot must be re-claimable; retained superseded "
                         "entries must not be scanned as if current")

    def test_repeated_v2_replaces_hold_exactly_one_slot(self):
        self.assertEqual(self._seq("v2", "v2", "v2"), [1, 1, 1],
                         "a note holds AT MOST one v2 slot no matter how many "
                         "replace re-renders it accumulates")

    def test_flash_only_never_books_a_slot(self):
        self.assertEqual(self._seq("flash", "flash"), [0, 0])

    def test_topup_after_supersede_books_the_slot(self):
        """The mirror bug at the additive branch: a top-up must judge occupancy
        from the CURRENT booking. Unwindowed it saw step 1's retained v2 and
        skipped booking a slot the note genuinely now holds."""
        s = _fresh()
        ma.commit(s, _dec("v2", 1000), CFG, note="V13", actual_credits=1000, replace=True)
        ma.commit(s, _dec("flash", 1000), CFG, note="V13", actual_credits=1000, replace=True)
        self.assertEqual(s["v2_count"], 0)
        ma.commit(s, _dec("v2", 500), CFG, note="V13", actual_credits=500, replace=False)
        self.assertEqual(s["v2_count"], 1,
                         "a v2 top-up after the note's v2 was superseded by flash "
                         "must book the slot it now occupies")

    def test_pure_topups_hold_one_slot_across_a_model_switch(self):
        """Pins the WINDOW BOUNDARY inside _note_window, not just its use.

        With no replace=True entry the window is the note's whole history. The
        mutant `if e.get("replace")` -> True collapses it to the LAST entry only,
        which survived every other case because they all contain a replace=True
        entry that lands on the same index. Pure top-ups are the one shape that
        distinguishes them:
            real   v2, flash, v2 (all replace=False) -> [1, 1, 1]
            mutant                                    -> [1, 1, 2]
        The mutant books a SECOND v2 slot for a single note, over-counting toward
        max_v2_per_cycle in the opposite direction from the leak above."""
        s = _fresh()
        counts = []
        for m in ("v2", "flash", "v2"):
            ma.commit(s, _dec(m, 500), CFG, note="V13", actual_credits=500, replace=False)
            counts.append(s["v2_count"])
        self.assertEqual(counts, [1, 1, 1],
                         "a note holds AT MOST one v2 slot across additive top-ups, "
                         "however its model choice flips between them")

    def test_retention_is_still_intact(self):
        """Guards the other half of the tension: this fix must not reintroduce the
        purge that TestLedgerReconciles forbids."""
        s = _fresh()
        for m in ("v2", "flash", "v2"):
            ma.commit(s, _dec(m, 1000), CFG, note="V13", actual_credits=1000, replace=True)
        self.assertEqual(len(s["log"]), 3, "superseded entries must still be RETAINED")
        self.assertEqual(sum(e["credits"] for e in s["log"]), s["credits_used"])


class TestCreditsUsedMonotonicOnForceReplace(unittest.TestCase):
    """2026-07-28 two-pass audit: ElevenLabs meters per SUBMITTED character, so a
    --force re-render bills TWICE but the old code booked once (it reversed the
    prior same-note booking before adding the new one). credits_used must be
    monotonically additive across any number of same-note replace=True commits;
    only the v2_count SLOT (concurrent-premium-booking cap, not a spend counter)
    is still reversed."""

    def test_force_rerender_adds_not_replaces(self):
        # The audit's own example, in miniature: two 20-credit submissions must
        # read as 40 (real cumulative ElevenLabs consumption), not 20 (only the
        # second render) -- the gap that produced a hard 402 mid-batch.
        s = _fresh()
        ma.commit(s, _dec("flash", 20), CFG, note="V20", actual_credits=20, replace=True)
        ma.commit(s, _dec("flash", 20), CFG, note="V20", actual_credits=20, replace=True)
        self.assertEqual(s["credits_used"], 40)

    def test_three_successive_force_rerenders_keep_accumulating(self):
        s = _fresh()
        for credits in (1000, 950, 1100):
            ma.commit(s, _dec("flash", credits), CFG, note="V21",
                      actual_credits=credits, replace=True)
        self.assertEqual(s["credits_used"], 1000 + 950 + 1100)

    def test_v2_slot_accounting_unchanged_by_this_fix(self):
        # Only credits_used stopped being reversed -- the slot logic (unaffected
        # by this fix) still nets exactly one slot for a v2->v2 force re-render.
        s = _fresh()
        ma.commit(s, _dec("v2", 500), CFG, note="V22", actual_credits=500, replace=True)
        ma.commit(s, _dec("v2", 600), CFG, note="V22", actual_credits=600, replace=True)
        self.assertEqual(s["v2_count"], 1)
        self.assertEqual(s["credits_used"], 1100)


class TestCommitFallback(unittest.TestCase):
    """commit_fallback (allocator-unavailable path) shares the replace/add
    semantics and books a per-run estimate from chars."""

    def test_fallback_top_up_stacks(self):
        s = _fresh()
        ma.commit_fallback(s, CFG, "eleven_flash_v2_5", chars=2000, note="V14",
                           actual_credits=None, replace=True)   # 1000 cr
        ma.commit_fallback(s, CFG, "eleven_flash_v2_5", chars=3000, note="V14",
                           actual_credits=None, replace=False)  # +1500 cr
        self.assertEqual(s["credits_used"], 2500)


class TestGenerateVoBooksRealChars(unittest.TestCase):
    """The reconciliation fix (2026-07-06 money-integrity bug): generate_vo books
    the REAL billable characters of the scenes it rendered (1 char-count/char),
    NOT the eventually-consistent /user/subscription character_count delta, which
    read right after a batch under-reported ~19x (V6 booked 1,124 of 21,289 real
    chars). This exercises generate_vo.main end-to-end with the network mocked —
    the commit-level tests above can't catch it because commit() faithfully books
    whatever number the caller computes; the bug was in the number the caller fed.
    """

    def _run_and_get_state(self, scene_chars, force=True):
        import os
        import sys
        import contextlib as _ctx
        import tempfile as _tmp
        from pathlib import Path as _P

        sys.path.insert(0, str(_REPO / "vo_factory"))
        import generate_vo as gv

        holder = {}
        flash_id = CFG["models"]["flash"]["id"]

        def fake_choose_model(text, state, cfg):
            return gv.ma.Decision(
                model_key="flash", model_id=flash_id, voice_id="testvoice",
                score=0.1, credits_est=99999, reason="test",
                would_exceed_budget=False, chars=len(text))

        # Isolate all allocator I/O + the network + repo side effects. These patch
        # SHARED module objects (gv, gv.ma), so restore every one in `finally` below
        # or a later in-process test would silently inherit a mock (e.g. a save_state
        # that writes nowhere).
        patches = {
            (gv.ma, "load_config"): lambda *a, **k: CFG,
            (gv.ma, "load_state"): lambda *a, **k: _fresh(),
            (gv.ma, "save_state"): lambda state, *a, **k: holder.__setitem__("state", state),
            (gv.ma, "choose_model"): fake_choose_model,
            (gv, "synthesize_scene"): lambda *a, **k: b"FAKEMP3",
            (gv, "preflight_credits"): lambda *a, **k: None,
            (gv, "preflight_numbers"): lambda *a, **k: None,
            (gv, "budget_lock"): lambda *a, **k: _ctx.nullcontext(),
        }
        originals = {(obj, name): getattr(obj, name) for (obj, name) in patches}
        for (obj, name), fn in patches.items():
            setattr(obj, name, fn)

        try:
            return self._run_main(gv, scene_chars, force, holder, _tmp, _P)
        finally:
            for (obj, name), orig in originals.items():
                setattr(obj, name, orig)

    def _run_main(self, gv, scene_chars, force, holder, _tmp, _P):
        import os
        import sys
        with _tmp.TemporaryDirectory() as td:
            kit_dir = _P(td) / "Video_TT"
            kit_dir.mkdir()
            lines = []
            for i, n in enumerate(scene_chars, 1):
                # Body of exactly n chars (ASCII, no markdown/dual-form) so the
                # parsed b["text"] length is exactly n — the billable unit.
                lines.append(f"## Scene {i} -> `Video_TT_VO_Scene_{i:02d}.mp3`")
                lines.append("x" * n)
                lines.append("")
            (kit_dir / "kit.md").write_text("\n".join(lines), encoding="utf-8")

            argv = ["generate_vo.py", str(kit_dir / "kit.md"), "--voice-id", "testvoice"]
            if force:
                argv.append("--force")
            old_argv, old_key = sys.argv, os.environ.get("ELEVENLABS_API_KEY")
            sys.argv = argv
            os.environ["ELEVENLABS_API_KEY"] = "dummy-key-for-test"
            try:
                gv.main()
            finally:
                sys.argv = old_argv
                if old_key is None:
                    os.environ.pop("ELEVENLABS_API_KEY", None)
                else:
                    os.environ["ELEVENLABS_API_KEY"] = old_key
        return holder.get("state")

    def test_books_full_submitted_chars_not_collapsed_delta(self):
        # Two scenes of 5000 + 4000 real chars. The ledger MUST book 9000 (1/char),
        # the true metered spend — regressing to a collapsed subscription delta
        # (the ~19x-too-low bug) would book a fraction of this and fail here.
        state = self._run_and_get_state([5000, 4000])
        self.assertIsNotNone(state, "generate_vo never committed a booking")
        self.assertEqual(state["credits_used"], 9000)
        self.assertEqual(len(state["log"]), 1)
        entry = state["log"][0]
        self.assertEqual(entry["credits"], 9000)
        self.assertTrue(entry["reconciled"], "booked chars must be marked reconciled (real)")
        self.assertEqual(entry["note"], "Video_TT")


if __name__ == "__main__":
    unittest.main()
