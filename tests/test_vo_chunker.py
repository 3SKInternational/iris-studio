#!/usr/bin/env python3
"""Regression tests for the VO long-scene sub-chunker (generate_vo.split_for_tts).

Locks the invariants the chunker must hold so a billed render stays correct:
  - short scenes are untouched (single chunk, identical text -> no behavior change)
  - long scenes split into <=MAX_TTS_CHARS pieces (sentence-bounded)
  - a <break .../> tag is never split mid-tag
  - no stray tail fragment shorter than MIN_TTS_CHARS (merged back)
  - rejoining the chunks reproduces the input (modulo single spaces at joins),
    so billed characters are conserved (no dropped/duplicated narration)
  - a single over-long sentence is kept whole rather than hard-cut

Stdlib unittest only -- no network, no API key. Run:
    python3 tests/test_vo_chunker.py
"""
import importlib.util
import pathlib
import re
import unittest

_GV_PATH = pathlib.Path(__file__).resolve().parent.parent / "vo_factory" / "generate_vo.py"
_spec = importlib.util.spec_from_file_location("generate_vo", _GV_PATH)
gv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gv)


def _rejoin(chunks):
    return " ".join(chunks)


class TestSplitForTTS(unittest.TestCase):
    def test_short_text_untouched(self):
        t = "A short scene. Two sentences only."
        self.assertEqual(gv.split_for_tts(t), [t])

    def test_long_text_splits_under_ceiling(self):
        # 40 sentences of ~40 chars each = ~1600 chars -> must split.
        sentences = [f"This is finance sentence number {i:02d} here." for i in range(40)]
        text = " ".join(sentences)
        self.assertGreater(len(text), gv.MAX_TTS_CHARS)
        chunks = gv.split_for_tts(text)
        self.assertGreater(len(chunks), 1)
        # Greedy packing keeps every non-final chunk within the ceiling. Only the
        # last chunk may exceed it, and only when a short tail was merged back into
        # it -- by strictly less than MIN_TTS_CHARS.
        for c in chunks[:-1]:
            self.assertLessEqual(len(c), gv.MAX_TTS_CHARS, f"non-final chunk over ceiling: {len(c)}")
        self.assertLess(len(chunks[-1]), gv.MAX_TTS_CHARS + gv.MIN_TTS_CHARS,
                        f"final chunk exceeds merge bound: {len(chunks[-1])}")

    def test_content_conserved(self):
        sentences = [f"Level {i} builds the next dollar of wealth." for i in range(60)]
        text = " ".join(sentences)
        chunks = gv.split_for_tts(text)
        # Rejoining reproduces the input exactly (single-space joins == original).
        self.assertEqual(_rejoin(chunks), text)
        # No characters lost or duplicated.
        self.assertEqual(sum(len(c) for c in chunks) + (len(chunks) - 1), len(text))

    def test_break_tag_not_split(self):
        # break tags carry "0.8s" -- the dot is followed by a digit, never split.
        unit = 'Save first. <break time="0.8s"/> Then invest the rest carefully here. '
        text = unit * 20  # well over the ceiling
        chunks = gv.split_for_tts(text)
        for c in chunks:
            # A chunk must never end or begin inside a break tag.
            self.assertEqual(c.count("<break"), c.count("/>"),
                             "a <break ...> tag was split across chunks")
            # No partial tag fragments.
            self.assertNotIn('time="0.8s"/>', c.replace('<break time="0.8s"/>', ""))

    def test_tail_merge_conserves_content(self):
        # Forces the tail-merge path (last pre-merge chunk < MIN) and verifies no
        # sentence is dropped or duplicated. Locks the mid-expression-pop bug where
        # the merge wrote to the wrong index (dropping one chunk, duplicating another).
        sentences = [f"This is finance sentence number {i:02d} here." for i in range(40)]
        text = " ".join(sentences)
        chunks = gv.split_for_tts(text)
        self.assertEqual(_rejoin(chunks), text)
        for s in sentences:
            self.assertEqual(text.count(s), _rejoin(chunks).count(s),
                             f"sentence count changed for: {s!r}")

    def test_no_tiny_tail_fragment(self):
        # Build text whose natural last chunk would be a short tail.
        body = " ".join(f"Sentence {i:02d} of the main body content here now." for i in range(30))
        text = body + " Short tail."
        chunks = gv.split_for_tts(text)
        if len(chunks) >= 2:
            self.assertGreaterEqual(len(chunks[-1]), gv.MIN_TTS_CHARS,
                                    "tiny tail fragment was not merged back")

    def test_single_over_long_sentence_kept_whole(self):
        # One sentence longer than the ceiling has no split point -> kept intact.
        sentence = "word " * 250 + "end."  # ~1250 chars, no internal . ! ?
        chunks = gv.split_for_tts(sentence)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0], sentence)


class TestConcatMp3(unittest.TestCase):
    def test_single_part_returned_asis(self):
        blob = b"\xff\xfb\x90fake-mp3-frame"
        self.assertEqual(gv._concat_mp3([blob], pathlib.Path(".")), blob)


if __name__ == "__main__":
    unittest.main()
