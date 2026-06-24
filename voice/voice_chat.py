#!/usr/bin/env python3
"""Local two-way voice for Iris. Mini-only.

  mic → faster-whisper (local STT) → router → kokoro (local TTS) → speaker

Hybrid brain:
  - local LLM (Ollama) for quick chatter
  - claude CLI (full agent fleet + MCPs + skills) when the turn names an agent
    or a hard task — escalation keyword router.

Wake word: say "iris ..." (or "hey iris ...") to address her; bare speech is ignored.

ponytail: deliberately one file, stdlib HTTP for Ollama, keyword router (no ML
classifier), energy-VAD record-until-silence (no wake-word engine). Upgrade
paths marked inline. Run --selftest for an audio-free check of the brain path.
"""
from __future__ import annotations
import os, sys, json, re, time, uuid, subprocess, urllib.request
from pathlib import Path

import numpy as np

# --- config ---------------------------------------------------------------
SR = 16000                      # mic sample rate (whisper wants 16k)
TTS_SR = 24000                  # kokoro output rate
WHISPER_MODEL = os.environ.get("IRIS_WHISPER", "base.en")
# Kokoro voice. First letter = accent (a=US, b=UK), second = gender (f/m).
# Default bf_emma = UK female. Other UK women: bf_isabella, bf_alice, bf_lily.
VOICE = os.environ.get("IRIS_VOICE", "bf_emma")
OLLAMA_MODEL = os.environ.get("IRIS_OLLAMA", "llama3.2:3b")
OLLAMA_URL = "http://127.0.0.1:11434/api/chat"
CLAUDE_CLI = os.environ.get("CLAUDE_CLI_PATH", "/opt/homebrew/bin/claude")
WAKE = ("iris", "hey iris")     # turn must start with one of these
STOP_PHRASES = ("stop listening", "go to sleep", "goodbye iris")  # exits the loop
SILENCE_RMS = float(os.environ.get("IRIS_SILENCE_RMS", "0.012"))  # ponytail: tune per mic/room
SILENCE_HANG = 0.8              # seconds of quiet that ends an utterance
MAX_UTTER = 15.0               # hard cap on one utterance (s)

# Escalate to the full Claude brain when the turn invokes an agent or a hard
# task; otherwise the local LLM handles chatter.
# ponytail: keyword heuristic. Ceiling = misroutes on paraphrase; upgrade to a
# tiny intent classifier only if that actually bites.
# Dropped bare ambiguous singles (find/plan/code/build) — they over-escalated on
# normal chatter ("can't find my keys", "weekend plans"), spending a Claude turn.
ESCALATE_VERBS = {
    "dispatch", "research", "write", "draft", "review", "analyze",
    "analyse", "audit", "script", "generate", "outline", "fix",
    "refactor", "summarize", "summarise", "search", "look up", "pull",
}

SYSTEM = ("You are Iris talking out loud to Steve. Reply in 1-3 spoken "
          "sentences, plain words, no markdown, no lists, no code blocks.")


def agent_names() -> set[str]:
    """The 38-agent fleet on disk — any name spoken forces escalation."""
    d = Path.home() / ".claude" / "agents"
    out = set()
    for p in d.glob("*.md"):
        out.add(p.stem.replace("-", " "))
        out.add(p.stem)
    return out


def should_escalate(text: str, agents: set[str]) -> bool:
    t = text.lower()
    if any(a in t for a in agents):
        return True
    return any(v in t for v in ESCALATE_VERBS)


# --- brains ---------------------------------------------------------------
def ask_ollama(text: str, history: list[dict]) -> str:
    msgs = [{"role": "system", "content": SYSTEM}, *history,
            {"role": "user", "content": text}]
    body = json.dumps({"model": OLLAMA_MODEL, "messages": msgs,
                       "stream": False}).encode()
    req = urllib.request.Request(OLLAMA_URL, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())["message"]["content"].strip()


def ask_claude(text: str, session: str, first: bool) -> str | None:
    """Full Iris brain: all agents + MCPs + skills, via the claude CLI.

    Returns the reply, or None on failure (so the caller does NOT mark the
    session as established — otherwise a failed first turn would leave every
    later --resume pointed at a session that was never created)."""
    flag = ["--session-id", session] if first else ["--resume", session]
    cmd = [CLAUDE_CLI, "-p", text, "--output-format", "text",
           "--append-system-prompt", SYSTEM, *flag]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired:
        return None
    if p.returncode != 0:
        print(f"[claude rc={p.returncode}] {p.stderr.strip()[:200]}")
        return None
    return p.stdout.strip()


# --- audio ----------------------------------------------------------------
def record_utterance(sd) -> np.ndarray | None:
    """Record from mic until ~SILENCE_HANG of quiet. None if nothing spoken."""
    block = int(SR * 0.1)               # 100ms blocks
    hang_blocks = int(SILENCE_HANG / 0.1)
    max_blocks = int(MAX_UTTER / 0.1)
    buf, quiet, started, peak = [], 0, False, 0.0
    with sd.InputStream(samplerate=SR, channels=1, dtype="float32") as stream:
        for _ in range(max_blocks):
            chunk, _ = stream.read(block)
            chunk = chunk[:, 0]
            rms = float(np.sqrt(np.mean(chunk ** 2)))
            peak = max(peak, rms)
            if rms >= SILENCE_RMS:
                started, quiet = True, 0
                buf.append(chunk)
            elif started:
                quiet += 1
                buf.append(chunk)
                if quiet >= hang_blocks:
                    break
    if not started:
        # Sound present but under threshold = mic too quiet / threshold too high.
        # Surface it so a misconfigured IRIS_SILENCE_RMS is diagnosable, not "dead".
        if peak >= SILENCE_RMS * 0.4:
            print(f"…heard sound below threshold (peak {peak:.3f} < "
                  f"{SILENCE_RMS}); lower IRIS_SILENCE_RMS to talk.")
        return None
    return np.concatenate(buf)


def speak(sd, tts, text: str) -> None:
    for chunk in tts(text, voice=VOICE):
        sd.play(chunk[2], TTS_SR)
        sd.wait()


def is_stop(text: str) -> bool:
    """Kill phrase — works with or without the wake word."""
    t = text.lower()
    return any(p in t for p in STOP_PHRASES)


def strip_wake(text: str) -> str | None:
    t = text.strip()
    low = t.lower()
    for w in WAKE:
        if low.startswith(w):
            return t[len(w):].lstrip(" ,.").strip()
    return None


# --- self-test (no audio) -------------------------------------------------
def selftest() -> int:
    agents = agent_names()
    assert "scriptwriter" in agents, "agent fleet not found on disk"
    assert should_escalate("iris write the script for video 12", agents)
    assert should_escalate("ask the channel analyst about retention", agents)
    assert not should_escalate("how are you doing today", agents)
    assert strip_wake("Hey Iris, what's up") == "what's up"
    assert strip_wake("random noise") is None
    assert is_stop("stop listening") and is_stop("okay iris, stop listening")
    assert not is_stop("i'm listening to music")
    print("selftest OK:", len(agents), "agent names; router + wake/stop gating pass")
    return 0


def main() -> int:
    if "--selftest" in sys.argv:
        return selftest()

    import sounddevice as sd
    from faster_whisper import WhisperModel
    from kokoro import KPipeline

    print(f"Loading STT={WHISPER_MODEL} TTS=kokoro brain={OLLAMA_MODEL}/claude …")
    stt = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    tts = KPipeline(lang_code=VOICE[0])     # 'b' = British phonemization
    agents = agent_names()
    session = str(uuid.uuid4())
    first_claude = True
    history: list[dict] = []        # local-chatter memory only

    print('Ready. Say "Iris …". Ctrl-C to quit.')
    while True:
        # One guard for the whole turn: a single bad mic/STT/LLM/TTS turn must
        # not kill the session. ponytail: shared guard beats five call-site ones.
        try:
            audio = record_utterance(sd)
            if audio is None:
                continue
            segs, _ = stt.transcribe(audio, language="en")
            heard = " ".join(s.text for s in segs).strip()
            if not heard:
                continue
            if is_stop(heard):
                print("stop phrase heard — exiting")
                speak(sd, tts, "Going quiet. Bye.")
                break
            cmd = strip_wake(heard)
            if cmd is None:
                continue                # not addressed to Iris
            if not cmd:
                speak(sd, tts, "Yes?")
                continue
            print(f"you: {cmd}")

            if should_escalate(cmd, agents):
                reply = ask_claude(cmd, session, first_claude)
                if reply is not None:   # rc==0 created the session, even if empty
                    first_claude = False
                if not reply:           # None (failed) or "" (empty success)
                    reply = "Sorry, the full brain didn't answer that one."
            else:
                reply = ask_ollama(cmd, history)
                history += [{"role": "user", "content": cmd},
                            {"role": "assistant", "content": reply}]
                history[:] = history[-8:]      # keep it short
            print(f"iris: {reply}")
            speak(sd, tts, reply)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(f"[turn error] {type(e).__name__}: {e}")
            try:
                speak(sd, tts, "Sorry, I hit a snag.")
            except Exception:
                pass
            continue


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nbye")
