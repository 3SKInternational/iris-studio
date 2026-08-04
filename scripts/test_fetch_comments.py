#!/usr/bin/env python3
"""Offline checks for fetch_comments.py's pure logic — no network, no token.

Covers the receipt-drift classifier (the reason a deleted/re-uploaded video no
longer hides behind a benign "not public yet") and the untrusted-text flatten.
Run: python3 scripts/test_fetch_comments.py
"""
import contextlib
import io
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch_comments  # noqa: E402
from fetch_comments import _classify_state, _clean, _http_reason  # noqa: E402


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


def test_http_reason():
    class _E(Exception):
        pass
    e = _E("<HttpError 403>")
    e.error_details = [{"reason": "quotaExceeded"}]
    assert _http_reason(e) == "quotaExceeded"
    # No error_details → falls back to the string form, never raises.
    assert _http_reason(RuntimeError("boom")) == "boom"
    # Empty message → class name, never an empty status reason.
    assert _http_reason(_E("")) == "_E"
    # error_details present but the reason is EMPTY/missing → must fall back to the
    # message, NEVER return "" (an empty status reason would print `blocked ()`).
    e2 = _E("fallback-msg"); e2.error_details = [{"reason": ""}]
    assert _http_reason(e2) == "fallback-msg"
    e3 = _E("fallback-msg2"); e3.error_details = [{"code": 403}]  # no "reason" key
    assert _http_reason(e3) == "fallback-msg2"
    # error_details holds a NON-dict first element → guard must skip, not crash.
    e4 = _E("fallback-msg3"); e4.error_details = ["not-a-dict"]
    assert _http_reason(e4) == "fallback-msg3"


class _FakeExec:
    def __init__(self, result=None, raises=None):
        self._result, self._raises = result, raises

    def execute(self):
        if self._raises is not None:
            raise self._raises
        return self._result


class _FakeVideos:
    def __init__(self, result=None, raises=None):
        self._result, self._raises = result, raises

    def list(self, **_kw):
        return _FakeExec(self._result, self._raises)


class _FakeYouTube:
    """Minimal youtube stub: only .videos().list().execute() is exercised — the
    blocked test raises before fetch_threads; the ok test returns not-public so
    fetch_threads is never reached."""
    def __init__(self, result=None, raises=None):
        self._v = _FakeVideos(result, raises)

    def videos(self):
        return self._v


def _run_main(monkey_youtube, receipts, argv=("fetch_comments.py",)):
    """Drive fetch_comments.main() fully offline. Returns (stdout, exit_code)."""
    tmp = Path(tempfile.mkdtemp())
    saved = {k: getattr(fetch_comments, k) for k in
             ("load_credentials", "build_data_service", "resolve_channel",
              "iter_upload_receipts", "vault")}
    saved_argv = sys.argv
    fetch_comments.load_credentials = lambda *_a, **_k: object()
    fetch_comments.build_data_service = lambda *_a, **_k: monkey_youtube
    fetch_comments.resolve_channel = lambda *_a, **_k: {"id": "OWNER"}
    fetch_comments.iter_upload_receipts = lambda *_a, **_k: list(receipts)
    fetch_comments.vault = lambda *_a, **_k: tmp
    sys.argv = list(argv)
    buf = io.StringIO()
    code = 0
    try:
        with contextlib.redirect_stdout(buf):
            fetch_comments.main()
    except SystemExit as se:
        code = se.code if isinstance(se.code, int) else 1
    finally:
        for k, v in saved.items():
            setattr(fetch_comments, k, v)
        sys.argv = saved_argv
    return buf.getvalue(), code


def test_main_blocked_on_status_check_403():
    """The 2026-07-29 masked failure: a quota 403 on the status check for every
    video must surface as `status: blocked (...)` + exit 2 — NEVER `ok (0)`."""
    class _E(Exception):
        pass
    err = _E("<HttpError 403 quotaExceeded>")
    err.error_details = [{"reason": "quotaExceeded"}]
    receipts = [("Video_01", "vid1", Path("/x"), {}),
                ("Video_02", "vid2", Path("/x"), {})]
    out, code = _run_main(_FakeYouTube(raises=err), receipts)
    assert code == 2, f"expected exit 2, got {code}\n{out}"
    last_status = [ln for ln in out.splitlines() if ln.startswith("status:")][-1]
    assert last_status.startswith("status: blocked ("), last_status
    assert "status: ok" not in out, f"MASKED: emitted an ok line on a blocked run\n{out}"
    assert "quotaExceeded" in out  # the real reason is surfaced, not swallowed


def test_main_ok_zero_on_genuine_empty_day():
    """A real run where every video is private/not-public → honest `ok (0)` + exit 0.
    Guards against the fix over-firing (blocking a genuine zero)."""
    result = {"items": [{"status": {"privacyStatus": "private"}}]}
    receipts = [("Video_01", "vid1", Path("/x"), {})]
    out, code = _run_main(_FakeYouTube(result=result), receipts)
    assert code == 0, f"expected exit 0, got {code}\n{out}"
    assert out.strip().splitlines()[-1] == "status: ok (0 comments)", out
    assert "status: blocked" not in out


if __name__ == "__main__":
    test_classify_state()
    test_clean_flattens_and_escapes()
    test_http_reason()
    test_main_blocked_on_status_check_403()
    test_main_ok_zero_on_genuine_empty_day()
    print("ok: all fetch_comments unit checks pass")
