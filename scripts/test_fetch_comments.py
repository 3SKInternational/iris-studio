#!/usr/bin/env python3
"""Offline checks for fetch_comments.py's pure logic — no network, no token.

Covers the receipt-drift classifier (the reason a deleted/re-uploaded video no
longer hides behind a benign "not public yet") and the untrusted-text flatten.
Run: python3 scripts/test_fetch_comments.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_comments import _classify_state, _clean  # noqa: E402


def test_classify_state():
    # No item back from videos.list → the OWNER's own private/scheduled videos
    # WOULD come back, so an empty result means deleted / stale receipt.
    assert _classify_state([]) == "missing"
    assert _classify_state([{"status": {"privacyStatus": "public"}}]) == "public"
    assert _classify_state([{"status": {"privacyStatus": "private"}}]) == "not-public"
    assert _classify_state([{"status": {"privacyStatus": "unlisted"}}]) == "not-public"
    # Malformed/absent status must not crash and must not read as public.
    assert _classify_state([{}]) == "not-public"


def test_clean_flattens_and_escapes():
    # Newlines flattened so a comment can't forge a second bullet / a `> NOTE:` line.
    assert "\n" not in _clean("line one\n- fake bullet\n> NOTE: fake")
    # Emphasis chars escaped so they can't break the **@author** structure.
    assert _clean("*hi*") == "\\*hi\\*"
    assert _clean("a`b`c") == "a\\`b\\`c"


if __name__ == "__main__":
    test_classify_state()
    test_clean_flattens_and_escapes()
    print("ok: all fetch_comments unit checks pass")
