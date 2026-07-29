"""
model_allocator.py  —  iris_studio / vo_factory

Auto-picks the ElevenLabs TTS model (premium v2 vs cheap Flash) for each video
so the month's voiceovers spend the Creator allowance while keeping a safety
buffer. Decisions are made per-script from the script text (auto-classify) and
constrained by a stateful monthly budget.

Public API (the only things generate_vo.py needs):
    cfg   = load_config()                       -> dict
    state = load_state(cfg)                      -> dict   (persisted per billing cycle)
    d     = choose_model(script_text, state, cfg)-> Decision
    commit(state, d, cfg); save_state(state)     # after a SUCCESSFUL generation

`Decision` carries: model_key ("v2"/"flash"), model_id (pass to ElevenLabs),
credits_est, score, reason, and would_exceed_budget.

Offline planning:
    plan = allocate_batch([script1, script2, ...], cfg)   # pure, no state writes

CLI:
    python model_allocator.py decide --file script.txt        # decide + commit
    python model_allocator.py decide --file script.txt --dry  # decide only
    python model_allocator.py plan   --dir scripts/           # batch, no commit
    python model_allocator.py status                          # show cycle usage
    python model_allocator.py reset                           # force new cycle
"""
from __future__ import annotations

import argparse
import calendar
import json
import math
import os
import re
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "vo_budget_config.json"
STATE_PATH = HERE / "vo_budget_state.json"


# --------------------------------------------------------------------------- #
# Config + state
# --------------------------------------------------------------------------- #
def load_config(path: str | os.PathLike = CONFIG_PATH) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _period_start(cfg: dict, today: date | None = None) -> date:
    """First day of the current billing period, anchored to billing_cycle_day."""
    today = today or date.today()
    day = int(cfg.get("billing_cycle_day", 1))
    anchor_day = min(day, calendar.monthrange(today.year, today.month)[1])
    if today.day >= anchor_day:
        return today.replace(day=anchor_day)
    prev_last = today.replace(day=1) - timedelta(days=1)
    return prev_last.replace(
        day=min(day, calendar.monthrange(prev_last.year, prev_last.month)[1])
    )


def load_state(cfg: dict, path: str | os.PathLike = STATE_PATH) -> dict:
    start = _period_start(cfg).isoformat()
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            state = json.load(fh)
        if state.get("period_start") == start:
            return state
    # new or rolled-over cycle -> fresh state
    return {"period_start": start, "credits_used": 0, "v2_count": 0, "log": []}


def save_state(state: dict, path: str | os.PathLike = STATE_PATH) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)


def usable_budget(cfg: dict) -> float:
    return cfg["monthly_credits"] * (1.0 - cfg["buffer_pct"])


# --------------------------------------------------------------------------- #
# Classifier  (auto v2-worthiness from script text)
# --------------------------------------------------------------------------- #
def _count_terms(text_lower: str, terms: Iterable[str]) -> int:
    return sum(text_lower.count(t) for t in terms)


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


# Per-word/char densities are tiny fractions; scale them so they matter, then
# clamp so a degenerate input (e.g. "!!!") can't saturate the score.
_DENSITY_SCALE = 12.0
_DENSITY_CLAMP = 0.12
_DENSITY_KEYS = (
    "emotion_lexicon", "info_lexicon", "exclaim_density", "question_density",
    "ellipsis_emdash", "second_person_address", "digit_density",
)
# Unbounded raw counts -> squashed into ~0..1 via tanh so none can dominate.
_COUNT_KEYS = ("narrative_cues", "instruction_cues", "dialogue_quotes", "list_markers")


def classify_script(text: str, cfg: dict) -> dict:
    """Return {score: 0..1, signals: {...}}. Higher score => more v2-worthy.

    Sign of each feature's contribution comes ONLY from its configured weight;
    feature values are always non-negative, so flipping a weight in the config
    cleanly flips that signal's direction.
    """
    c = cfg["classifier"]
    w = c["weights"]
    low = text.lower()
    words = max(len(re.findall(r"\b\w+\b", text)), 1)

    sig = {
        "emotion_lexicon": _count_terms(low, c["emotion_lexicon"]) / words,
        "narrative_cues": _count_terms(low, c["narrative_cues"]),
        "info_lexicon": _count_terms(low, c["info_lexicon"]) / words,
        "instruction_cues": _count_terms(low, c["instruction_cues"]),
        "dialogue_quotes": text.count('"') / 2 + text.count("“"),
        "exclaim_density": text.count("!") / words,
        "question_density": text.count("?") / words,
        "ellipsis_emdash": (text.count("...") + text.count("—")) / words,
        "second_person_address": len(re.findall(r"\byou\b", low)) / words,
        "digit_density": len(re.findall(r"\d", text)) / max(len(text), 1),
        "list_markers": len(re.findall(r"(?m)^\s*([-*•]|\d+[.)])\s", text)),
    }

    feat = {}
    for k in _DENSITY_KEYS:
        feat[k] = min(sig[k], _DENSITY_CLAMP) * _DENSITY_SCALE
    for k in _COUNT_KEYS:
        feat[k] = math.tanh(sig[k] / 2.0)

    z = float(c.get("bias", 0.0)) + sum(w.get(k, 0.0) * feat.get(k, 0.0) for k in w)
    return {"score": round(_sigmoid(z), 4), "signals": sig}


# --------------------------------------------------------------------------- #
# Cost + decision
# --------------------------------------------------------------------------- #
def estimate_credits(text: str, model_key: str, cfg: dict) -> int:
    rate = cfg["models"][model_key]["credits_per_char"]
    return int(math.ceil(len(text) * rate))


@dataclass
class Decision:
    model_key: str
    model_id: str
    voice_id: str
    score: float
    credits_est: int
    reason: str
    would_exceed_budget: bool
    chars: int


def choose_model(text: str, state: dict, cfg: dict) -> Decision:
    a = cfg["allocation"]
    fb = a["fallback_model"]
    score = classify_script(text, cfg)["score"]
    v2_cost = estimate_credits(text, "v2", cfg)
    budget = usable_budget(cfg)
    used = state["credits_used"]

    wants_v2 = score >= a["v2_score_threshold"]
    slot_open = state["v2_count"] < a["max_v2_per_cycle"]
    v2_fits = (used + v2_cost) <= budget

    if wants_v2 and slot_open and v2_fits:
        key, reason = "v2", f"score {score:.2f} >= {a['v2_score_threshold']}; v2 slot {state['v2_count']+1}/{a['max_v2_per_cycle']}; fits budget"
    else:
        key = fb
        if not wants_v2:
            reason = f"score {score:.2f} < {a['v2_score_threshold']} -> {fb}"
        elif not slot_open:
            reason = f"v2-worthy but {a['max_v2_per_cycle']} v2 slots already used -> {fb}"
        else:
            reason = f"v2-worthy but v2 cost {v2_cost} would breach buffered budget -> {fb}"

    cost = estimate_credits(text, key, cfg)
    return Decision(
        model_key=key,
        model_id=cfg["models"][key]["id"],
        voice_id=cfg.get("default_voice_id", ""),
        score=score,
        credits_est=cost,
        reason=reason,
        would_exceed_budget=(used + cost) > cfg["monthly_credits"],
        chars=len(text),
    )


def model_key_for_id(cfg: dict, model_id: str) -> str | None:
    """Reverse-lookup a config model_key ('v2'/'flash') from an ElevenLabs id."""
    for k, v in cfg.get("models", {}).items():
        if v.get("id") == model_id:
            return k
    return None


def _note_window(log, note):
    """The entries for `note` that describe its CURRENT booking.

    A `replace=True` commit supersedes everything booked for that note before it,
    so the window starts at the note's LAST replace=True entry (inclusive). With
    no such entry the whole note history is current (pure top-ups).

    This exists because round-2 stopped purging superseded log entries — needed so
    credits_used reconciles against its own itemization — which silently widened
    the scope of every `any(...)` scan over the log. Retention and unscoped scans
    cannot both be right: see the two call sites for the slot leak it caused.
    """
    entries = [e for e in log if e.get("note") == note]
    last_replace = None
    for i, e in enumerate(entries):
        if e.get("replace"):
            last_replace = i
    return entries if last_replace is None else entries[last_replace:]


def commit(state: dict, d: Decision, cfg: dict, note: str = "",
           actual_credits: int | None = None, replace: bool = True,
           est_override: int | None = None) -> dict:
    """Record a SUCCESSFUL generation against the monthly budget.

    Pass `actual_credits` (the real usage ElevenLabs returns in the TTS
    response/headers, e.g. the 'character-cost' header or the subscription
    usage endpoint) to book the true cost and keep the cycle total exact.
    If omitted, the booked amount falls back to an estimate.

    `est_override` is the estimate-fallback for THIS booking when no real
    `actual_credits` reading is available. It exists because `d.credits_est`
    is computed once over the WHOLE kit, but a PARTIAL top-up (`replace=False`)
    only rendered the still-missing scenes -- booking the full-kit estimate on
    an additive top-up would OVER-count real spend by the already-rendered
    remainder (the mirror of the under-count bug `replace` fixed). The caller
    that knows how many chars it actually rendered this run passes that run's
    estimate here; if None, the full-kit `d.credits_est` is used (correct for a
    `--force` full re-render, where this run IS the whole kit).

    NOTE (the video id) is the per-cycle idempotency key. How a same-note booking
    combines with prior ones depends on `replace`:

      replace=True  (a FULL re-render -- generate_vo's `--force`, which re-renders
                    EVERY scene): SUPERSEDE the SLOT, never the CREDITS.
                    * credits_used is ADDITIVE and never reversed. ElevenLabs
                      meters per SUBMITTED character, so a --force re-render is a
                      SECOND REAL CHARGE, not an edit of the first. Reversing it
                      made the ledger read lower than actual consumption and showed
                      headroom right up to a hard 402 mid-batch.
                    * only v2_count is reversed -- it counts distinct videos
                      occupying a premium slot this cycle, not spend, and a note
                      holds at most one slot however many times it re-renders.
                    CORRECTED 2026-07-29 (round-5 review): this said "the old
                    same-note entries' credits AND any v2 slot are reversed", the
                    exact opposite of the credits half and of the inline comment in
                    commit() below. A maintainer trusting it would "restore" the
                    reversal and re-open the 402 bug. The `max(credits_used, 0)`
                    clamp further down is likewise vestigial for this path --
                    nothing can drive credits_used negative any more.

      replace=False (a PARTIAL top-up -- a non-`--force` run that renders ONLY the
                    scenes whose mp3s are still missing): ADD. The new booking
                    covers only the just-rendered scenes, so it must STACK on top
                    of the prior same-note credits (which paid for the scenes that
                    already existed) -- reversing them would under-count real spend
                    by the size of the already-rendered remainder. The v2 slot is
                    counted at most ONCE per note: a top-up does not consume a
                    second slot if this note already holds one.

    A top-up never double-books a scene: non-`--force` runs skip existing mp3s, so
    a scene is rendered (and booked) exactly once across the original run + top-ups.
    An empty note never matches, so note-less commits always append.
    """
    if actual_credits is not None:
        booked = int(actual_credits)
    elif est_override is not None:
        booked = int(est_override)
    else:
        booked = d.credits_est
    log = state.setdefault("log", [])
    note_has_v2 = False
    if note and replace:
        # credits_used is DELIBERATELY NOT reversed here. ElevenLabs meters per
        # SUBMITTED character, so a --force full re-render bills the original
        # render's characters AGAIN -- it is a second real charge, not an edit
        # of the first. Reversing the old booking made credits_used LOWER than
        # actual cumulative ElevenLabs consumption (a 20-scene kit: real
        # consumption 40, credits_used said 20 -- generate_vo.py's own
        # stale-VO guard instructs "Re-render with --force", making this the
        # COMMON path, not an edge case), so the ledger showed headroom right
        # up to a hard 402 mid-batch (2026-07-28 two-pass audit). credits_used
        # must be monotonically additive, matching the real meter it exists to
        # track.
        #
        # v2_count IS still reversed here: unlike credits_used it counts
        # DISTINCT videos currently occupying a v2 slot this cycle (a cap on
        # concurrent premium bookings), not cumulative spend -- a re-render of
        # the SAME video must not double-claim (or leak) a slot. A note holds
        # AT MOST ONE slot regardless of how many v2 log entries it has
        # accumulated (repeated top-ups can leave >1 same-note v2 entry), so
        # this reverses ONCE PER NOTE, not once per matching entry.
        # ROUND-2 REGRESSION (2026-07-28 review): this used to decrement once
        # per matching log entry inside the purge loop below -- 2 v2 top-ups
        # + one --force reversed v2_count twice for a single occupied slot,
        # under-counting it and letting an extra 2x-cost v2 render past the
        # per-cycle cap.
        #
        # ROUND-3 REGRESSION (2026-07-28 review): scoped to _note_window, NOT the
        # note's whole history. Round-2 stopped PURGING superseded entries (so the
        # ledger reconciles), which silently widened every `any(...)` over the log
        # and reintroduced the very leak the same round fixed:
        #     v2(replace) -> 1   flash(replace) -> 0   v2(replace) -> 0, want 1
        # The third call re-reversed a slot the flash run had ALREADY released,
        # because step 1's retained v2 entry still matched. `slot_open` is then
        # v2_count < max_v2_per_cycle, so that under-count grants an extra
        # 2x-cost v2 render past the per-cycle cap.
        if any(e.get("model") == "v2" for e in _note_window(log, note)):
            state["v2_count"] = state.get("v2_count", 0) - 1
        # Prior same-note entries are RETAINED (not purged): the purge existed
        # only to avoid double-reversing credits_used, and credits_used is no
        # longer reversed at all above -- so purging now only deletes the
        # itemization that explains the total, leaving credits_used unable to
        # reconcile against its own log (ROUND-2 audit finding, 2026-07-28).
        # note_has_v2 stays False here: this branch always books a fresh v2
        # slot above when the NEW booking is v2 (the just-reversed slot, if
        # any, is re-claimed by this run's own commit below).
    elif note and not replace:
        # Additive top-up: keep prior same-note entries; only flag whether this
        # note already consumed a v2 slot so we don't book a second one.
        # Same window as the replace branch: a top-up must judge slot occupancy
        # from the note's CURRENT booking, not a superseded one. Unwindowed, the
        # sequence v2(replace) -> flash(replace) -> v2(top-up) saw step 1's
        # retained v2 and skipped booking a slot the note genuinely now holds.
        note_has_v2 = any(e.get("model") == "v2" for e in _note_window(log, note))
    state["credits_used"] = state.get("credits_used", 0) + booked
    if d.model_key == "v2" and not note_has_v2:
        state["v2_count"] = state.get("v2_count", 0) + 1
    # Reversing superseded entries must never push a counter below zero.
    state["credits_used"] = max(state["credits_used"], 0)
    state["v2_count"] = max(state["v2_count"], 0)
    log.append(
        {"ts": datetime.now().isoformat(timespec="seconds"),
         "model": d.model_key, "credits": booked,
         "estimated": d.credits_est, "reconciled": actual_credits is not None,
         "replace": bool(replace),
         "score": d.score, "chars": d.chars, "note": note}
    )
    return state


def commit_fallback(state: dict, cfg: dict, model_id: str, chars: int,
                    note: str = "", actual_credits: int | None = None,
                    replace: bool = True) -> dict:
    """Book a budget entry when no allocator Decision was available.

    Used when generate_vo fell back to a fixed model (allocator import/classify
    failed) but still produced billable audio -- so the ledger isn't blind.
    Credits are estimated from `chars` at the model's configured rate unless a
    real `actual_credits` reading is supplied. Goes through `commit`, so it shares
    the same replace-vs-add semantics (`replace`) as any other booking.
    """
    key = model_key_for_id(cfg, model_id) or cfg["allocation"]["fallback_model"]
    rate = cfg["models"][key]["credits_per_char"]
    est = int(math.ceil(max(int(chars), 0) * rate))
    d = Decision(
        model_key=key, model_id=model_id,
        voice_id=cfg.get("default_voice_id", ""),
        score=0.0, credits_est=est,
        reason="allocator-unavailable fallback",
        would_exceed_budget=False, chars=int(chars),
    )
    return commit(state, d, cfg, note=note, actual_credits=actual_credits,
                  replace=replace)


# --------------------------------------------------------------------------- #
# Offline batch planner (pure - no state file writes)
# --------------------------------------------------------------------------- #
def allocate_batch(scripts: list[str], cfg: dict) -> list[dict]:
    """
    Plan a whole month at once. v2 goes to the highest-scoring v2-worthy
    scripts, capped by max_v2_per_cycle and the buffered budget; everything
    else is flash. Returns one row per input script (original order).
    """
    a = cfg["allocation"]
    budget = usable_budget(cfg)
    scored = [
        {"idx": i, "text": s, "score": classify_script(s, cfg)["score"],
         "v2_cost": estimate_credits(s, "v2", cfg),
         "fb_cost": estimate_credits(s, a["fallback_model"], cfg)}
        for i, s in enumerate(scripts)
    ]
    eligible = sorted(
        [r for r in scored if r["score"] >= a["v2_score_threshold"]],
        key=lambda r: r["score"], reverse=True,
    )
    v2_ids, used = set(), 0
    for r in eligible:
        if len(v2_ids) >= a["max_v2_per_cycle"]:
            break
        if used + r["v2_cost"] <= budget:
            v2_ids.add(r["idx"]); used += r["v2_cost"]
    rows = []
    for r in scored:
        key = "v2" if r["idx"] in v2_ids else a["fallback_model"]
        rows.append({
            "idx": r["idx"], "model_key": key,
            "model_id": cfg["models"][key]["id"],
            "score": r["score"],
            "credits_est": r["v2_cost"] if key == "v2" else r["fb_cost"],
        })
    return rows


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def main(argv=None) -> int:
    cfg = load_config()
    ap = argparse.ArgumentParser(description="ElevenLabs model allocator for iris_studio")
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("decide", help="decide model for one script (commits unless --dry)")
    d.add_argument("--file", required=True)
    d.add_argument("--dry", action="store_true")

    pl = sub.add_parser("plan", help="batch-plan a directory of .txt scripts (no commit)")
    pl.add_argument("--dir", required=True)

    sub.add_parser("status", help="show current cycle usage")
    sub.add_parser("reset", help="force a fresh billing cycle")

    args = ap.parse_args(argv)

    if args.cmd == "decide":
        state = load_state(cfg)
        dec = choose_model(_read(args.file), state, cfg)
        if not args.dry:
            commit(state, dec, cfg, note=os.path.basename(args.file)); save_state(state)
        print(json.dumps({**asdict(dec),
                          "committed": (not args.dry),
                          "cycle_credits_used": state["credits_used"],
                          "cycle_v2_count": state["v2_count"]}, indent=2))
        return 0

    if args.cmd == "plan":
        files = sorted(Path(args.dir).glob("*.txt"))
        rows = allocate_batch([_read(str(f)) for f in files], cfg)
        total = sum(r["credits_est"] for r in rows)
        for f, r in zip(files, rows):
            print(f"{f.name:30s} {r['model_key']:5s} score={r['score']:.2f} "
                  f"~{r['credits_est']} cr")
        print(f"\nPlanned credits: {total:,} / usable {int(usable_budget(cfg)):,} "
              f"(cap {cfg['monthly_credits']:,}, buffer {int(cfg['buffer_pct']*100)}%)")
        return 0

    if args.cmd == "status":
        state = load_state(cfg)
        print(json.dumps({**state,
                          "usable_budget": usable_budget(cfg),
                          "monthly_credits": cfg["monthly_credits"]}, indent=2))
        return 0

    if args.cmd == "reset":
        save_state({"period_start": _period_start(cfg).isoformat(),
                    "credits_used": 0, "v2_count": 0, "log": []})
        print("cycle reset")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
