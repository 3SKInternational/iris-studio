#!/usr/bin/env python3
"""Schedule already-uploaded PRIVATE videos to flip PUBLIC at a future instant.

upload_video.py only does videos().insert (a fresh upload → new video_id). Once a
video is already on the channel as private, the way to schedule its public release
is videos().update with status.privacyStatus=private + status.publishAt — YouTube
auto-flips it public at that instant. This script does exactly that, in place, for
the video_ids already recorded in the Production_Kits/*_youtube_upload.json receipts.

Usage (dry-run by default — prints the plan, calls nothing):
  python3 scripts/schedule_publish.py Video_05=2026-06-26T18:00:00Z
  python3 scripts/schedule_publish.py \
      Video_05=2026-06-26T18:00:00Z Video_06=2026-06-28T14:00:00Z \
      Video_07=2026-06-30T18:00:00Z --commit

Each arg is LABEL=ISO8601-UTC. Add --commit to actually call the API and write the
publish_at back into the receipt. Add --synthetic to also set the "altered or
synthetic media" disclosure (status.containsSyntheticMedia=True) in the same update
— correct for the AI-animated + TTS 3SK videos. Reuses youtube_client.py for auth.

# ponytail: GET-then-update merges publishAt into the LIVE status part so we don't
# clobber selfDeclaredMadeForKids / license / embeddable. Send only the writable
# status fields back (madeForKids is read-only-computed; dropping it is required).
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from youtube_client import (
    YouTubeAuthError,
    build_data_service,
    load_credentials,
)

DEFAULT_VAULT = "~/Documents/3SK/outputs/BRANDS/3SK_Finance"
# Poll the post-update verify GET through YouTube's read-after-write lag.
VERIFY_TRIES = 6
VERIFY_DELAY_S = 3
# status sub-fields that are writable and must be echoed back on an update so the
# API doesn't reset them to defaults. madeForKids is read-only (computed) — omit it.
_WRITABLE_STATUS = (
    "privacyStatus",
    "selfDeclaredMadeForKids",
    "license",
    "embeddable",
    "publicStatsViewable",
    "containsSyntheticMedia",
)


# Sentinel for "drop the scheduled publish" (distinct from None = preserve-mode).
CLEAR = object()


def die(msg: str, code: int = 1) -> None:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(code)


def vault() -> Path:
    return Path(os.path.expanduser(os.environ.get("SK_VAULT", DEFAULT_VAULT))).resolve()


def parse_arg(raw: str) -> tuple[str, str | None]:
    """'Video_05=2026-06-26T18:00:00Z' -> ('Video_05', RFC3339 publishAt).
    A bare 'Video_05' (no '=time') -> ('Video_05', None) = preserve-mode: touch
    only the flags requested (e.g. --synthetic), leaving privacy + any existing
    schedule untouched.
    'Video_05=none' (or =clear/=unschedule) -> ('Video_05', CLEAR) = drop any
    scheduled publish, leaving the video private (won't auto-flip public)."""
    label, _, when = raw.partition("=")
    label, when = label.strip(), when.strip()
    if not label.startswith("Video_"):
        die(f"bad label {label!r}; expected 'Video_NN'")
    if when.lower() in ("none", "clear", "unschedule"):
        return label, CLEAR  # unschedule-mode: stay private, no publishAt
    if "=" not in raw or not when:
        return label, None  # preserve-mode: no schedule change
    try:
        dt = datetime.fromisoformat(when.replace("Z", "+00:00"))
    except ValueError:
        die(f"publish time not ISO8601: {when!r} (e.g. 2026-06-26T18:00:00Z)")
    if dt.tzinfo is None:
        die(f"publish time {when!r} has no timezone; use a UTC instant ending in Z")
    dt = dt.astimezone(timezone.utc)
    if dt <= datetime.now(timezone.utc):
        die(f"publish time {when!r} is not in the future")
    return label, dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def write_receipt(path: Path, data: dict) -> None:
    """Atomic replace so an interrupt/full-disk can't truncate the only durable
    video_id↔schedule record (the vault is Drive-synced)."""
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def load_receipt(label: str) -> tuple[Path, dict]:
    path = vault() / "Production_Kits" / f"{label}_youtube_upload.json"
    if not path.is_file():
        die(f"no upload receipt for {label} at {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not data.get("video_id"):
        die(f"receipt {path.name} has no video_id — was {label} ever uploaded?")
    return path, data


def get_status(youtube, video_id: str) -> dict:
    resp = youtube.videos().list(part="status", id=video_id).execute()
    items = resp.get("items", [])
    if not items:
        die(f"video {video_id} not found on the channel (deleted? wrong account?)")
    return items[0]["status"]


def main() -> int:
    flags = {"--commit", "--synthetic"}
    args = [a for a in sys.argv[1:] if a not in flags]
    commit = "--commit" in sys.argv[1:]
    synthetic = "--synthetic" in sys.argv[1:]  # declare "altered or synthetic media"
    if not args:
        die("no videos given. Usage: schedule_publish.py Video_05=2026-06-26T18:00:00Z [...] [--commit] [--synthetic]")

    plan = [parse_arg(a) for a in args]
    if any(pa is None for _, pa in plan) and not synthetic:
        die("a bare 'Video_NN' (preserve-mode) needs --synthetic — otherwise there's nothing to change.")
    receipts = {label: load_receipt(label) for label, _ in plan}

    try:
        creds = load_credentials()
    except YouTubeAuthError as exc:
        die(str(exc))
    youtube = build_data_service(creds)

    def publish_matches(after_pa, want_pa) -> bool:
        # want_pa None = "no schedule" (after must also be unscheduled); else to-the-second.
        if want_pa is None:
            return not after_pa
        return (after_pa or "")[:19] == want_pa[:19]

    print(f"{'COMMIT' if commit else 'DRY-RUN'} — updating {len(plan)} video(s):\n")
    failures = 0
    for label, publish_at in plan:
        path, data = receipts[label]
        vid = data["video_id"]
        try:
            live = get_status(youtube, vid)
        except SystemExit:
            raise
        except Exception as exc:  # network / API blip — report, keep going.
            print(f"  ✗ {label} ({vid}): status read failed — {type(exc).__name__}: {exc}")
            failures += 1
            continue

        new_status = {k: live[k] for k in _WRITABLE_STATUS if k in live}
        # Floor matching upload_video.py: never let the COPPA flag go unset on update.
        new_status.setdefault("selfDeclaredMadeForKids", False)
        if publish_at is CLEAR:  # unschedule-mode: stay private, drop any scheduled publish
            want_privacy, want_publish = "private", None
            new_status["privacyStatus"] = "private"
            new_status.pop("publishAt", None)  # omitting publishAt on a status update clears it
        elif publish_at is not None:  # schedule-mode: flip to a scheduled public release
            want_privacy, want_publish = "private", publish_at
            new_status["privacyStatus"] = "private"  # required pairing for a scheduled publish
            new_status["publishAt"] = publish_at
        else:  # preserve-mode: leave privacy + any existing schedule exactly as-is
            want_privacy, want_publish = live.get("privacyStatus"), live.get("publishAt")
            if want_publish:  # re-send so an omission can't clear an existing schedule
                new_status["publishAt"] = want_publish
        if synthetic:
            new_status["containsSyntheticMedia"] = True
        print(f"  {label} ({vid}): {data.get('title','(untitled)')[:60]}")
        print(f"      was: privacyStatus={live.get('privacyStatus')} publishAt={live.get('publishAt')}"
              f" syntheticMedia={live.get('containsSyntheticMedia')}")
        print(f"      ->   privacyStatus={want_privacy} publishAt={want_publish}"
              + ("  syntheticMedia=True" if synthetic else ""))

        if not commit:
            continue
        try:
            resp = youtube.videos().update(part="status", body={"id": vid, "status": new_status}).execute()
        except Exception as exc:
            print(f"  ✗ {label} ({vid}): update FAILED — {type(exc).__name__}: {exc}")
            failures += 1
            continue
        rst = resp.get("status", {})

        # containsSyntheticMedia is confirmable ONLY via the update RESPONSE: videos.list
        # does NOT return it to the owner (verified — it reads back null even right after a
        # response that echoes True), so a list-based check false-negatives. The update
        # response is the server's post-write resource, so an echoed True is the persist proof.
        if synthetic and rst.get("containsSyntheticMedia") is not True:
            print(f"  ✗ {label} ({vid}): synthetic-media flag NOT accepted — update response "
                  f"status.containsSyntheticMedia={rst.get('containsSyntheticMedia')!r}")
            failures += 1
            continue

        # privacy + schedule DO read back via list (eventually — it's read-after-write lagged),
        # so poll-confirm those externally: this is the high-stakes public-flip guard.
        after, verify_err = {}, None
        for attempt in range(VERIFY_TRIES):
            if attempt:
                time.sleep(VERIFY_DELAY_S)
            try:
                after = get_status(youtube, vid)
            except SystemExit:
                raise
            except Exception as exc:
                verify_err = exc
                continue
            verify_err = None
            if (publish_matches(after.get("publishAt"), want_publish)
                    and after.get("privacyStatus") == want_privacy):
                break
        if verify_err is not None:
            print(f"  ✗ {label} ({vid}): update sent but verify GET failed — {type(verify_err).__name__}: {verify_err}")
            failures += 1
            continue
        if (not publish_matches(after.get("publishAt"), want_publish)
                or after.get("privacyStatus") != want_privacy):
            print(f"  ✗ {label} ({vid}): privacy/schedule did NOT take after {VERIFY_TRIES} checks — live status now "
                  f"privacyStatus={after.get('privacyStatus')} publishAt={after.get('publishAt')}")
            failures += 1
            continue

        if publish_at is CLEAR:
            data["privacy"] = "private"
            data["publish_at"] = None
        elif publish_at is not None:
            data["privacy"] = "private"
            data["publish_at"] = publish_at
        if synthetic:
            data["contains_synthetic_media"] = True
        write_receipt(path, data)
        print(f"  ✅ {label}: verified; receipt updated.")

    if not commit:
        print("\n(dry-run) re-run with --commit to apply.")
    if failures:
        print(f"\n{failures} failure(s).", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
