#!/usr/bin/env python3
r"""Batch voice-over generator for the 3SK video factory (Build E1).

VO kit (markdown) in -> one scene mp3 out per kit block, via the ElevenLabs
text-to-speech API. The upstream sibling of image_factory (prompts -> PNGs) and
the audio source for video_factory (mp3s -> rendered shots).

The kit is the input primitive: each `## Scene N -> \`Video_NN_VO_Scene_MM.mp3\``
block carries the reviewed, break-tagged narration. We keep the SSML `<break/>`
tags verbatim (ElevenLabs honors them on eleven_multilingual_v2) and strip only
markdown emphasis so nothing decorative gets read aloud.

Design mirrors image_factory deliberately: voice id / model / settings / output
dir are config values (env + kit-header + CLI), so a future voice swap is a flag
change, not a rewrite. Stdlib-only (urllib) to match video_factory -- no deps.

  python3 generate_vo.py <kit.md> --check            # verify key (free GET)
  python3 generate_vo.py <kit.md> --dry-run          # plan + credit estimate, no calls
  python3 generate_vo.py <kit.md>                     # generate into <kit-folder>_gen
  python3 generate_vo.py <kit.md> --output DIR        # generate into a chosen dir
  python3 generate_vo.py <kit.md> --limit 1           # smoke-test one clip
  python3 generate_vo.py <kit.md> --force             # re-render existing mp3s

Key: ELEVENLABS_API_KEY from the environment first, then the repo-root .env.
Never put the key in the vault -- it is git-tracked + synced.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

# VO model allocator (premium v2 vs cheap flash, per-video, budget-aware).
# Lives beside this file in vo_factory/. Best-effort import: if it's missing the
# run falls back to DEFAULT_MODEL rather than breaking.
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import model_allocator as ma
except Exception:  # noqa: BLE001 -- allocator is an optimization, not a hard dep
    ma = None

API_BASE = "https://api.elevenlabs.io/v1"

# Locked production defaults (Voice locked: Brian, 2026-06-11). All overridable
# by the kit header, env, or CLI -- a voice swap stays a config change.
DEFAULT_VOICE_ID = "nPczCjzI2devNBz1zQrb"  # Brian
DEFAULT_MODEL = "eleven_multilingual_v2"
# When the budget allocator is unavailable we must NOT silently default to the
# premium (expensive) v2 model -- a blind run falls back to cheap flash. An
# explicit --model still wins, so deliberate v2 use is unaffected.
FALLBACK_MODEL = "eleven_flash_v2_5"
DEFAULT_STABILITY = 0.5
DEFAULT_SIMILARITY = 0.75
DEFAULT_STYLE = 0.0
DEFAULT_SPEED = 1.0  # ElevenLabs voice_settings.speed; valid 0.7-1.2, 1.0 = native pace.

# ElevenLabs bills ~1 credit per character of the submitted text (break tags
# included). Used only for the offline --dry-run estimate.
CREDITS_PER_CHAR = 1.0


def die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(1)


def expand(path: str) -> Path:
    return Path(os.path.expanduser(path)).resolve()


def load_dotenv_key(script_dir: Path) -> str | None:
    """ELEVENLABS_API_KEY from env, else the nearest .env walking up from here."""
    key = os.environ.get("ELEVENLABS_API_KEY")
    if key:
        return key.strip()
    for parent in [script_dir, *script_dir.parents]:
        env_path = parent / ".env"
        if env_path.is_file():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("ELEVENLABS_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def load_dotenv_value(script_dir: Path, name: str) -> str | None:
    val = os.environ.get(name)
    if val:
        return val.strip()
    for parent in [script_dir, *script_dir.parents]:
        env_path = parent / ".env"
        if env_path.is_file():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith(f"{name}="):
                    v = line.split("=", 1)[1].strip().strip('"').strip("'")
                    return v or None
    return None


# --- kit parsing -----------------------------------------------------------

_BLOCK_RE = re.compile(
    r"^##\s+Scene\s+(\d+)\s*(?:->|→)\s*`([^`]+\.mp3)`",
    re.MULTILINE,
)

# Dual-form caption token {{spoken|caption}}: lets ONE kit carry the words the VO
# must SPEAK (left) and the digits the SRT should SHOW (right) for the same beat —
# e.g. {{one hundred thousand dollars|$100,000}}. The VO keeps the left form; the
# caption parser in build_video.py (build_video._DUAL_FORM_RE, identical pattern)
# keeps the right. Co-locating both forms makes the digits-vs-words split
# drift-proof; the two used to live in separate kit files that silently diverged.
# Absent -> both sides see the text unchanged (backward compatible).
_DUAL_RE = re.compile(r"\{\{\s*([^|{}]+?)\s*\|\s*([^{}]+?)\s*\}\}")


def clean_vo_text(raw: str) -> str:
    """Strip markdown emphasis + collapse whitespace; keep SSML <break/> tags."""
    text = raw.strip()
    # Drop markdown blockquote lines: in these kits a `>` line is always an author
    # note/aside (e.g. a "Slimmed by Steve" production note), never spoken VO. Run
    # this while newlines still exist, before the whitespace collapse below.
    text = re.sub(r"(?m)^[ \t]*>.*$", "", text)
    text = _DUAL_RE.sub(r"\1", text)  # dual-form token: VO speaks the left form
    # Drop markdown bold/italic markers (decorative; would not be spoken anyway,
    # but a stray '*' can confuse the engine). Leave punctuation and quotes.
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"(?<!\w)_([^_]+)_(?!\w)", r"\1", text)
    text = re.sub(r"\*([^*]+)\*", r"\1", text)
    # Collapse internal newlines / runs of whitespace to single spaces, but do
    # not touch the inside of <break .../> tags.
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def parse_kit(kit_path: Path) -> list[dict]:
    """Return ordered [{scene, filename, text}] from the VO kit markdown."""
    body = kit_path.read_text(encoding="utf-8")
    matches = list(_BLOCK_RE.finditer(body))
    if not matches:
        die(
            f"no scene blocks found in {kit_path.name}. Expected lines like "
            "'## Scene 1 -> `Video_01_VO_Scene_01.mp3` (...)'."
        )
    blocks: list[dict] = []
    for i, m in enumerate(matches):
        # Skip to the end of the header line so the trailing "(cold open, ...)"
        # label is not captured into the spoken narration.
        nl = body.find("\n", m.end())
        start = len(body) if nl == -1 else nl + 1
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        chunk = body[start:end]
        # The narration is everything up to a horizontal rule (the kit footer).
        chunk = re.split(r"^---\s*$", chunk, maxsplit=1, flags=re.MULTILINE)[0]
        text = clean_vo_text(chunk)
        if not text:
            continue
        fname = m.group(2).strip()
        # The filename is later used as `out_dir / fname` for a write — reject any
        # path-traversal so a kit can't escape out_dir (M3). Author-controlled today,
        # but it's an unvalidated write at a trust boundary.
        if "/" in fname or "\\" in fname or ".." in fname or fname.startswith("."):
            raise SystemExit(f"unsafe VO filename in kit: {fname!r} (no path separators or '..')")
        blocks.append(
            {"scene": int(m.group(1)), "filename": fname, "text": text}
        )
    return blocks


# --- ElevenLabs API --------------------------------------------------------

def _request(url: str, *, key: str, method: str = "GET", payload: dict | None = None,
             accept: str = "application/json") -> bytes:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("xi-api-key", key)
    req.add_header("Accept", accept)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def check_key(key: str) -> None:
    """Verify the key + report subscription tier/credits. Free GET, no spend."""
    try:
        raw = _request(f"{API_BASE}/user/subscription", key=key)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        die(f"key check failed: HTTP {e.code} {detail}")
    except urllib.error.URLError as e:
        die(f"key check failed: {e.reason}")
    sub = json.loads(raw)
    used = sub.get("character_count", 0)
    limit = sub.get("character_limit", 0)
    remaining = limit - used if isinstance(limit, int) else "?"
    print("ElevenLabs key OK.")
    print(f"  tier      : {sub.get('tier')}")
    print(f"  characters: {used} / {limit}  (remaining: {remaining})")
    print(f"  resets     : {sub.get('next_character_count_reset_unix')}")


def subscription_count(key: str) -> int | None:
    """Current billing-cycle usage (credits) from ElevenLabs, or None.

    Used for subscription-delta reconciliation: capture before + after the batch
    and book (after - before) as the REAL credits charged. Version-proof -- no
    dependence on a response header name ElevenLabs may rename."""
    try:
        sub = json.loads(_request(f"{API_BASE}/user/subscription", key=key))
        v = sub.get("character_count")
        return int(v) if isinstance(v, (int, float)) else None
    except Exception:  # noqa: BLE001 -- reconciliation is best-effort
        return None


@contextlib.contextmanager
def budget_lock(lock_path: Path):
    """Serialize the allocator commit (read-modify-write of vo_budget_state.json)
    across concurrent VO runs so a parallel run can't lose a counter increment.
    Best-effort: if locking is unavailable the run proceeds unlocked rather than
    failing. Held only around the fast commit, not the TTS batch, so it never
    serializes rendering."""
    fh = None
    try:
        fh = open(lock_path, "w")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX)
        except Exception:  # noqa: BLE001 -- proceed unlocked, but keep fh so finally closes it
            pass
    except Exception:  # noqa: BLE001 -- couldn't even open the lock file; run unlocked
        fh = None
    try:
        yield
    finally:
        if fh is not None:
            try:
                fcntl.flock(fh, fcntl.LOCK_UN)
            except Exception:  # noqa: BLE001 -- harmless if we never acquired it
                pass
            fh.close()


# Warn (don't block) when the run nears or exceeds the remaining balance. Steve's
# 2026-06-15 directive: warn me through Telegram for any/all alerts. The render
# loop already tolerates per-clip 402s, so this only adds a heads-up, never aborts.
LOW_CREDIT_SAFETY = 2000  # chars of headroom we want left AFTER a run


def preflight_numbers(kit_path: Path, *, notify: bool) -> None:
    """Warn (don't block) if the kit holds numbers ElevenLabs is known to
    mis-speak — non-round millions like $1,043,000, which it reads aloud as
    'one thousand forty-three thousand'. Runs the deterministic scripts/
    vo_number_lint.py gate; a hit prints the offenders + safe rewrites and pings
    Steve to fix the kit and re-render. Never aborts (matches preflight_credits'
    warn-not-block contract). Best-effort: a missing/failing linter must not break
    a VO run."""
    linter = Path(__file__).resolve().parent.parent / "scripts" / "vo_number_lint.py"
    if not linter.exists():
        return
    try:
        proc = subprocess.run(
            [sys.executable, str(linter), str(kit_path)],
            capture_output=True, text=True, timeout=30,
        )
    except Exception:  # noqa: BLE001 -- a flaky preflight must never stop a run
        return
    if proc.returncode == 1 and proc.stdout.strip():  # 1 = hazards (0 clean, 2 read error)
        msg = proc.stdout.strip()
        print("  preflight: ⚠️ TTS number hazard(s) — fix the kit before billing a render:\n"
              + msg, file=sys.stderr)
        if notify:
            notify_steve(f"🔴 VO number hazard in {kit_path.name} — ElevenLabs may "
                         f"mis-speak; fix the kit & re-render:\n{msg}")


def notify_steve(text: str) -> None:
    """Best-effort Telegram alert via the repo's scripts/notify.sh. Never raises."""
    notify = Path(__file__).resolve().parent.parent / "scripts" / "notify.sh"
    if not notify.exists():
        return
    try:
        subprocess.run([str(notify), text], timeout=25,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:  # noqa: BLE001 -- alerting must never break a VO run
        pass


def preflight_credits(key: str, need_chars: int) -> None:
    """Live balance check before spending. Prints the balance and fires a Telegram
    alert if this run would exceed it, or leave less than LOW_CREDIT_SAFETY headroom.
    Best-effort: a flaky balance check must not stop a run, so any error is non-fatal."""
    try:
        sub = json.loads(_request(f"{API_BASE}/user/subscription", key=key))
        used = sub.get("character_count")
        limit = sub.get("character_limit")
        if not isinstance(used, int) or not isinstance(limit, int):
            print("  preflight: balance unavailable (skipping credit check)", file=sys.stderr)
            return
        remaining = limit - used
        print(f"  preflight: {remaining} chars left; this run needs ~{need_chars}")
        if remaining < need_chars:
            notify_steve(
                f"🔴 ElevenLabs VO run may FAIL — need ~{need_chars} chars, only "
                f"{remaining} left (tier {sub.get('tier')}). Clips past the limit "
                "will 402. Top up or trim the batch."
            )
        elif remaining - need_chars < LOW_CREDIT_SAFETY:
            notify_steve(
                f"🟡 ElevenLabs credits LOW after this VO run — ~{remaining - need_chars} "
                f"chars would remain (tier {sub.get('tier')}). Heads up before the next batch."
            )
    except Exception as e:  # noqa: BLE001 -- never block a run on the pre-flight
        print(f"  preflight: credit check skipped ({e})", file=sys.stderr)


def synthesize(text: str, *, key: str, voice_id: str, model: str,
               stability: float, similarity: float, style: float,
               speed: float) -> bytes:
    url = f"{API_BASE}/text-to-speech/{voice_id}"
    payload = {
        "text": text,
        "model_id": model,
        "voice_settings": {
            "stability": stability,
            "similarity_boost": similarity,
            "style": style,
            "speed": speed,
            "use_speaker_boost": True,
        },
    }
    return _request(url, key=key, method="POST", payload=payload, accept="audio/mpeg")


# --- CLI -------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Batch VO generator (3SK video factory, E1).")
    p.add_argument("kit", help="Path to the Session-B VO kit markdown.")
    p.add_argument("--output", help="Output dir for the mp3s (default: a '<kit-folder>_gen' sibling, so a bare run never overwrites a hand-recorded set).")
    p.add_argument("--voice-id", help="ElevenLabs voice id (config value).")
    p.add_argument("--model", help="Model id (config value).")
    p.add_argument("--stability", type=float, help="Voice stability 0-1.")
    p.add_argument("--similarity", type=float, help="Similarity boost 0-1.")
    p.add_argument("--style", type=float, help="Style exaggeration 0-1.")
    p.add_argument("--speed", type=float, help="Speaking speed 0.7-1.2 (1.0 = native; ElevenLabs rejects outside this range).")
    p.add_argument("--only", help="Render only these scene numbers (comma-separated, e.g. 22,24). Add --force to overwrite existing mp3s. Model is still chosen from the WHOLE kit, so a single-scene redo keeps the same voice as the rest of the video.")
    p.add_argument("--limit", type=int, help="Generate at most N clips (smoke test).")
    p.add_argument("--force", action="store_true", help="Re-render mp3s that already exist.")
    p.add_argument("--dry-run", action="store_true", help="Plan + credit estimate; no API calls, no writes.")
    p.add_argument("--check", action="store_true", help="Verify the API key + print credits, then exit.")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    script_dir = Path(__file__).resolve().parent

    key = load_dotenv_key(script_dir)
    if args.check:
        if not key:
            die("no ELEVENLABS_API_KEY in env or .env.")
        check_key(key)
        return

    kit_path = expand(args.kit)
    if not kit_path.is_file():
        die(f"kit not found: {kit_path}")
    blocks = parse_kit(kit_path)
    # parse_kit dies on zero scene headers, but a kit whose headers all carry empty
    # narration parses to an empty list — which would otherwise fall through to the
    # "nothing to do" exit 0 below and silently produce no mp3s. Treat it as fatal.
    if not blocks:
        die(f"{kit_path.name} has scene headers but no narration text under any of them.")

    # Warn (never block) on numbers ElevenLabs mis-speaks (e.g. non-round millions).
    # Runs for dry-run too so a hazard surfaces before any spend; Telegram ping only
    # on a real run so dry-run experiments don't page Steve.
    preflight_numbers(kit_path, notify=not args.dry_run)

    voice_id = args.voice_id or load_dotenv_value(script_dir, "ELEVENLABS_VOICE_ID") or DEFAULT_VOICE_ID
    # Model: explicit --model wins; else the allocator auto-picks premium v2 vs
    # cheap flash for THIS video (one decision per kit, over all narration),
    # constrained by the monthly budget + the 3-v2/cycle cap.
    dec = cfg = state = None
    if args.model:
        model = args.model
    elif ma is not None:
        try:
            cfg = ma.load_config()
            state = ma.load_state(cfg)
            script_text = "\n\n".join(b["text"] for b in blocks)
            dec = ma.choose_model(script_text, state, cfg)
            model = dec.model_id
        except Exception as e:  # noqa: BLE001 -- fall back, never break a run
            model = FALLBACK_MODEL
            print(f"  allocator: unavailable ({e}); falling back to cheap {model}", file=sys.stderr)
    else:
        model = FALLBACK_MODEL
    stability = args.stability if args.stability is not None else DEFAULT_STABILITY
    similarity = args.similarity if args.similarity is not None else DEFAULT_SIMILARITY
    style = args.style if args.style is not None else DEFAULT_STYLE
    speed = args.speed if args.speed is not None else DEFAULT_SPEED
    if not 0.7 <= speed <= 1.2:
        die(f"--speed {speed} out of range; ElevenLabs accepts 0.7-1.2.")
    # Default to a '<kit-folder>_gen' sibling rather than the kit's own folder:
    # the kit lives beside the hand-recorded set (e.g. Voice_Files/Video_01/),
    # and a bare run must never overwrite those. An explicit --output overrides.
    out_dir = (expand(args.output) if args.output
               else kit_path.parent.parent / f"{kit_path.parent.name}_gen")

    # Full-batch cost (before --limit) so the dry-run estimate reflects the
    # whole run, not just the previewed slice.
    full_chars = sum(len(b["text"]) for b in blocks)
    full_count = len(blocks)
    # --only narrows WHICH scenes render (model already chosen from the full kit
    # above, so a one-scene redo keeps the same voice). Applied before --limit.
    if args.only:
        try:
            wanted = {int(s) for s in args.only.replace(",", " ").split()}
        except ValueError:
            die(f"--only must be scene numbers (e.g. 22,24); got {args.only!r}.")
        if not wanted:
            die(f"--only parsed to no scene numbers; got {args.only!r}.")
        missing = wanted - {b["scene"] for b in blocks}
        if missing:
            die(f"--only scene(s) not in {kit_path.name}: {sorted(missing)}")
        blocks = [b for b in blocks if b["scene"] in wanted]
    if args.limit is not None:
        blocks = blocks[: args.limit]

    print(f"kit      : {kit_path.name}   scenes: {len(blocks)}")
    print(f"voice    : {voice_id}   model: {model}   stab/sim/style/speed: {stability}/{similarity}/{style}/{speed}")
    print(f"output   : {out_dir}")
    if dec is not None:
        print(f"allocator: {dec.model_key} ({dec.model_id})  score={dec.score:.2f}  "
              f"est~{dec.credits_est} cr  -- {dec.reason}")
    print("-" * 64)

    total_chars = 0
    to_make: list[dict] = []
    for b in blocks:
        chars = len(b["text"])
        total_chars += chars
        dest = out_dir / b["filename"]
        exists = dest.exists() and not args.force
        flag = "skip" if exists else "gen "
        if not exists:
            to_make.append(b)
        print(f"  [{flag}] scene {b['scene']:>2}  {b['filename']:<32} {chars:>5} chars")

    est_credits = int(round(full_chars * CREDITS_PER_CHAR))
    print("-" * 64)
    if args.limit is not None:
        shown_credits = int(round(total_chars * CREDITS_PER_CHAR))
        print(f"  shown (--limit {args.limit}): {total_chars} chars ~= {shown_credits} credits")
    print(f"  full batch: {full_chars} chars  ~= {est_credits} credits ({full_count} scenes)")
    print(f"  to generate now: {len(to_make)} / {len(blocks)}")

    if args.dry_run:
        print("\n(dry run -- no API calls, no files written)")
        return
    if not to_make:
        print("\nnothing to do (all mp3s exist; use --force to re-render).")
        return
    if not key:
        die("no ELEVENLABS_API_KEY in env or .env.")

    # Pre-flight: warn (don't block) if this batch nears/exceeds the balance.
    make_chars = sum(len(b["text"]) for b in to_make)
    preflight_credits(key, make_chars)
    before = subscription_count(key)   # subscription-delta baseline

    out_dir.mkdir(parents=True, exist_ok=True)
    made = failed = 0
    for b in to_make:
        dest = out_dir / b["filename"]
        try:
            audio = synthesize(
                b["text"], key=key, voice_id=voice_id, model=model,
                stability=stability, similarity=similarity, style=style, speed=speed,
            )
            if not audio:
                raise RuntimeError("empty audio response")
            fd, tmp = tempfile.mkstemp(suffix=".mp3", dir=str(out_dir))
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(audio)
                os.replace(tmp, dest)
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)
            made += 1
            print(f"  [ok ] scene {b['scene']:>2}  {b['filename']}  ({len(audio)} bytes)")
        except urllib.error.HTTPError as e:
            failed += 1
            detail = e.read().decode("utf-8", "replace")[:200]
            print(f"  [FAIL] scene {b['scene']:>2}  HTTP {e.code} {detail}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001 -- one bad clip must not kill the batch
            failed += 1
            print(f"  [FAIL] scene {b['scene']:>2}  {e}", file=sys.stderr)

    print("-" * 64)
    print(f"done: {made} generated, {failed} failed, {len(blocks) - len(to_make)} skipped.")

    # Book spend against the monthly budget. Only on a full run that produced
    # audio: --limit is a smoke test and must not consume a cycle v2 slot. We
    # book whenever the allocator module is importable -- even on a fallback run
    # with no Decision -- so the ledger is never blind to real spend.
    # Bookkeeping must NEVER fail an otherwise-successful generation: the mp3s
    # are already on disk, so a budget-write error is logged and swallowed.
    if ma is not None and made > 0 and args.limit is None:
        try:
            after = subscription_count(key)
            actual = (max(after - before, 0)
                      if (before is not None and after is not None) else None)
            # A 0 (or negative) delta after a real billable batch means the
            # account-wide usage endpoint LAGGED or was skewed by a concurrent
            # run -- it does NOT mean the run was free. Treat it as unreliable and
            # fall back to the estimate (marked unreconciled in the log) so the
            # cap can't be defeated by a lagging endpoint reading zero.
            if actual is not None and actual <= 0 and make_chars > 0:
                actual = None
            video_id = kit_path.parent.name
            bcfg = cfg if cfg is not None else ma.load_config()
            with budget_lock(script_dir / ".vo_budget.lock"):
                # Re-read state INSIDE the lock so a parallel run's increment isn't
                # lost; commit is replace-by-note, so a re-render/top-up of this
                # video supersedes its prior cycle entry (no double-book, no extra
                # v2 slot) rather than stacking on top of it.
                state = ma.load_state(bcfg)
                # REPLACE the prior same-note entry only when THIS run covers the
                # whole kit -- i.e. a bare --force re-render. Any partial run is an
                # additive top-up that must ADD to the prior credits (which paid for
                # the scenes already on disk); replacing would erase that spend and
                # under-count. Two partial cases: a non-force run (rendered only the
                # missing scenes) and a --only run (rendered just the picked scenes,
                # even WITH --force) -- both add, never supersede.
                replace = bool(args.force) and not args.only
                if dec is not None:
                    # When there's no real delta (`actual is None`), fall back to
                    # an estimate for THIS run's scenes only -- `dec.credits_est`
                    # covers the WHOLE kit, so booking it on an additive top-up
                    # (replace=False) would over-count by the already-rendered
                    # remainder. make_chars is exactly the scenes rendered now
                    # (full kit on --force, only the missing ones otherwise), so
                    # it's the correct per-run estimate either way.
                    est_override = None
                    if actual is None:
                        rate = bcfg["models"][dec.model_key]["credits_per_char"]
                        est_override = int(math.ceil(make_chars * rate))
                    ma.commit(state, dec, bcfg, note=video_id, actual_credits=actual,
                              replace=replace, est_override=est_override)
                else:
                    # Allocator was unavailable for the DECISION (fell back to a
                    # fixed model); still book an estimate from real chars.
                    ma.commit_fallback(state, bcfg, model, make_chars,
                                       note=video_id, actual_credits=actual,
                                       replace=replace)
                ma.save_state(state)
            booked = actual if actual is not None else (state["log"][-1]["credits"] if state.get("log") else 0)
            src = "real" if actual is not None else "estimate"
            print(f"allocator: booked {booked} cr ({src}) for {video_id}; "
                  f"cycle {state['credits_used']:,}/{int(ma.usable_budget(bcfg)):,} usable, "
                  f"v2 {state['v2_count']}/{bcfg['allocation']['max_v2_per_cycle']}")
        except Exception as e:  # noqa: BLE001 -- budget write must not fail a good render
            print(f"  budget: booking skipped ({e}); mp3s are intact, ledger "
                  f"NOT updated -- reconcile manually", file=sys.stderr)

    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
