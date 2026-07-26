#!/usr/bin/env python3
"""One-shot repair of the 11_analyze stages parked by the quota misclassification.

Context: before fa795b1, a CLI usage/session-limit notice (STDOUT, exit 1, empty
stderr) was billed as a genuine task failure, so 3 strikes parked the video. The
classifier is fixed, but the already-written state files still carry the bogus
fail_count/park_reason and will never retry on their own.

Two DIFFERENT wrong states are repaired differently, because the videos are not
in the same situation:

  * V8/V9/V12 — their analyses exist on disk and carry real analyze-reviewer
    REVISE verdicts. The park reason is wrong (quota, not 3 content failures),
    but the work genuinely is not done. Reset the counters so the pipeline's own
    review->fix->re-review loop can run.

  * V13 — published 2026-07-26 14:30Z. Its only analysis is a PRE-LAUNCH doc, so
    there is no post-publish data to analyse yet. It is not failed, it is
    premature: park it as awaiting_data, the same posture V1 and V4 already use.

Idempotent: re-running changes nothing once each stage is in its target state.
Dry-run by default; pass --apply to write.
"""
import argparse
import json
import pathlib
import sys

KITS = pathlib.Path("/Users/steve/Documents/3SK/outputs/BRANDS/3SK_Finance/Production_Kits")
STAGE = "11_analyze"

# video -> (target_status, park_reason, note)
TARGETS = {
    8:  ("ready", None, "unparked 2026-07-26: parked by the pre-fa795b1 quota "
                        "misclassification, not 3 content failures. Analysis exists "
                        "(2026-07-26_video08-analysis.md) with an analyze-reviewer "
                        "REVISE — the review->fix->re-review loop still needs to run."),
    9:  ("ready", None, "unparked 2026-07-26: same quota misclassification. Analysis "
                        "exists (2026-07-26_video09-analysis.md), verdict REVISE."),
    12: ("ready", None, "unparked 2026-07-26: same quota misclassification. Analysis "
                        "exists (2026-07-25_video12-performance.md), verdict REVISE."),
    13: ("needs-steve", "awaiting_data",
         "re-parked 2026-07-26: NOT a failure. Published 2026-07-26 14:30Z; the only "
         "analysis on disk is the pre-launch doc, so there is no post-publish data to "
         "read yet. Same posture as V1/V4. Revisit ~7 days post-publish. (Note: the "
         "48h velocity read for the V14 trigger is a separate manual read, not this "
         "stage.)"),
}


def repair(video: int, apply: bool) -> bool:
    path = KITS / f"Video_{video:02d}_pipeline.json"
    if not path.exists():
        print(f"  V{video}: NO STATE FILE at {path} — skipped")
        return False
    data = json.loads(path.read_text())
    s = data.get("stages", {}).get(STAGE)
    if s is None:
        print(f"  V{video}: no {STAGE} stage — skipped")
        return False
    status, park_reason, note = TARGETS[video]
    before = (s.get("status"), s.get("fail_count"), s.get("infra_count"), s.get("park_reason"))
    if before == (status, 0, 0, park_reason):
        print(f"  V{video}: already {status}/{park_reason} — no change")
        return False
    s["status"] = status
    s["fail_count"] = 0
    s["infra_count"] = 0
    s["park_reason"] = park_reason
    s["note"] = note
    s["pid"] = None
    s["pid_start_token"] = None
    s["started_at"] = None
    print(f"  V{video}: {before} -> ({status}, 0, 0, {park_reason})")
    if apply:
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        tmp.replace(path)  # atomic, matches the orchestrator's own write discipline
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    args = ap.parse_args()
    print(f"{'APPLYING' if args.apply else 'DRY-RUN'} — {STAGE} quota-park repair")
    changed = sum(repair(v, args.apply) for v in sorted(TARGETS))
    print(f"{changed} stage(s) {'repaired' if args.apply else 'would change'}")
    if not args.apply and changed:
        print("re-run with --apply to write")
    return 0


if __name__ == "__main__":
    sys.exit(main())
