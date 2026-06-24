#!/usr/bin/env python3
# ponytail: smoke test only — proves kokoro TTS + faster-whisper STT work locally end to end.
import sys, soundfile as sf
from kokoro import KPipeline
from faster_whisper import WhisperModel

PHRASE = "Iris two way local voice is working."

# TTS: kokoro -> wav (24kHz)
tts = KPipeline(lang_code="a")
audio = next(chunk[2] for chunk in tts(PHRASE, voice="af_heart"))
sf.write("/tmp/iris_voice_test.wav", audio, 24000)

# STT: faster-whisper (CPU int8; CTranslate2 has no MPS) -> text
stt = WhisperModel("base.en", device="cpu", compute_type="int8")
segs, _ = stt.transcribe("/tmp/iris_voice_test.wav")
heard = " ".join(s.text for s in segs).strip()

print("said: ", PHRASE)
print("heard:", heard)
# loose check: at least half the words round-tripped
said_w = set(PHRASE.lower().strip(".").split())
heard_w = set(heard.lower().strip(".").split())
hit = len(said_w & heard_w) / len(said_w)
assert hit >= 0.5, f"round-trip too lossy: {hit:.0%}"
print(f"OK round-trip {hit:.0%}")
