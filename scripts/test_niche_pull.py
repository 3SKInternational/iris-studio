#!/usr/bin/env python3
"""Offline checks for niche_pull.py's pure logic — no network, no token.

Covers `_uploads_in_window`, the RFC3339 window filter that replaced the old
100-unit search.list channel path with the 1-unit playlistItems.list path (the
~100x quota cut). This is the one non-trivial branch in that change: get the
boundary or the drop-conditions wrong and the niche feed silently loses videos.
Gate-visible (scripts/test_*.py glob) so precheck's mutation pass has a killer
for this logic ([[feedback_selftest_not_gate_coverage]]).

Run: python3 scripts/test_niche_pull.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from niche_pull import (  # noqa: E402
    _quota_estimate,
    _uploads_in_window,
    recent_upload_ids,
    resolve_uploads_playlist,
)

AFTER = "2026-07-17T00:00:00Z"


class _FakeReq:
    def __init__(self, resp, raises=None):
        self._resp = resp
        self._raises = raises

    def execute(self):
        if self._raises is not None:
            raise self._raises
        return self._resp


def _quota_http_error():
    """A real googleapiclient HttpError that _is_quota_error() recognizes."""
    from googleapiclient.errors import HttpError

    class _Resp:
        status = 403
        reason = "Forbidden"
    # HttpError.__str__ surfaces error.message; _is_quota_error() greps the string
    # for "quotaexceeded", so the message must carry the reason.
    content = (b'{"error": {"message": "The request cannot be completed because you '
               b'have exceeded your quotaExceeded quota.", '
               b'"errors": [{"reason": "quotaExceeded"}]}}')
    return HttpError(_Resp(), content)


def _soft_http_error():
    """A non-quota HttpError (e.g. a transient 500) — must NOT be treated as quota."""
    from googleapiclient.errors import HttpError

    class _Resp:
        status = 500
        reason = "Internal Server Error"
    return HttpError(_Resp(), b'{"error": {"message": "Backend error", '
                             b'"errors": [{"reason": "backendError"}]}}')


class _FakeResource:
    """A googleapiclient-shaped resource that records the last .list(**kwargs)."""

    def __init__(self, resp, raises=None):
        self._resp = resp
        self._raises = raises
        self.last_kwargs = None

    def list(self, **kwargs):
        self.last_kwargs = kwargs
        return _FakeReq(self._resp, self._raises)


class _FakeService:
    def __init__(self, channels_resp=None, playlist_resp=None, playlist_raises=None):
        self._channels = _FakeResource(channels_resp or {})
        self._playlist = _FakeResource(playlist_resp or {}, raises=playlist_raises)

    def channels(self):
        return self._channels

    def playlistItems(self):
        return self._playlist


def _item(vid, pub):
    cd = {}
    if vid is not None:
        cd["videoId"] = vid
    if pub is not None:
        cd["videoPublishedAt"] = pub
    return {"contentDetails": cd}


def test_window_boundary_is_inclusive():
    # `>= after`: a video published exactly at the cutoff is IN-window (kept).
    assert _uploads_in_window([_item("edge", AFTER)], AFTER, 10) == ["edge"]
    # One second before the cutoff is dropped.
    assert _uploads_in_window([_item("just_before", "2026-07-16T23:59:59Z")], AFTER, 10) == []


def test_out_of_window_dropped():
    assert _uploads_in_window([_item("old", "2026-07-01T10:00:00Z")], AFTER, 10) == []
    assert _uploads_in_window([_item("new", "2026-07-25T10:00:00Z")], AFTER, 10) == ["new"]


def test_missing_id_or_date_dropped():
    assert _uploads_in_window([_item("", "2026-07-25T10:00:00Z")], AFTER, 10) == []
    assert _uploads_in_window([_item(None, "2026-07-25T10:00:00Z")], AFTER, 10) == []
    assert _uploads_in_window([_item("nopub", None)], AFTER, 10) == []
    assert _uploads_in_window([{}], AFTER, 10) == []  # no contentDetails at all


def test_per_query_cap_keeps_newest_first():
    # playlistItems arrives newest-first; the cap must preserve that order.
    items = [
        _item("a", "2026-07-25T10:00:00Z"),
        _item("b", "2026-07-24T10:00:00Z"),
        _item("c", "2026-07-23T10:00:00Z"),
    ]
    assert _uploads_in_window(items, AFTER, 2) == ["a", "b"]
    assert _uploads_in_window(items, AFTER, 10) == ["a", "b", "c"]


def test_empty_input():
    assert _uploads_in_window([], AFTER, 10) == []


def test_resolve_uploads_playlist_extracts_uploads_id():
    resp = {"items": [{"id": "UC123",
                       "contentDetails": {"relatedPlaylists": {"uploads": "UU123"}}}]}
    svc = _FakeService(channels_resp=resp)
    assert resolve_uploads_playlist(svc, "@somechannel") == "UU123"
    # channels.list must be asked for contentDetails (where uploads lives) and the
    # @ must be stripped from the handle.
    assert svc.channels().last_kwargs["part"] == "contentDetails"
    assert svc.channels().last_kwargs["forHandle"] == "somechannel"


def test_resolve_uploads_playlist_none_on_empty_or_missing():
    assert resolve_uploads_playlist(_FakeService(channels_resp={"items": []}), "@x") is None
    # Items present but no uploads playlist in the tree → None, not a crash.
    resp = {"items": [{"id": "UC1", "contentDetails": {}}]}
    assert resolve_uploads_playlist(_FakeService(channels_resp=resp), "@x") is None


def test_recent_upload_ids_lists_window_and_uses_cheap_call():
    resp = {"items": [
        {"contentDetails": {"videoId": "in", "videoPublishedAt": "2026-07-25T10:00:00Z"}},
        {"contentDetails": {"videoId": "out", "videoPublishedAt": "2026-07-01T10:00:00Z"}},
    ]}
    svc = _FakeService(playlist_resp=resp)
    assert recent_upload_ids(svc, "UU123", AFTER, 10) == ["in"]
    # The whole point of the change: playlistItems.list(maxResults=50), 1 unit.
    assert svc.playlistItems().last_kwargs["playlistId"] == "UU123"
    assert svc.playlistItems().last_kwargs["maxResults"] == 50


def test_recent_upload_ids_escalates_quota_error():
    # A quota 403 must become QuotaExceeded so the caller salvages partial data
    # instead of silently getting [] (the whole reason the branch exists).
    import niche_pull
    svc = _FakeService(playlist_raises=_quota_http_error())
    try:
        recent_upload_ids(svc, "UU123", AFTER, 10)
    except niche_pull.QuotaExceeded:
        pass
    else:
        raise AssertionError("quota 403 should raise QuotaExceeded, not return silently")


def test_recent_upload_ids_soft_fails_on_non_quota_error():
    # A transient 500 must degrade to [] (skip this channel), NOT abort the whole
    # run as if the quota were dead.
    import niche_pull
    svc = _FakeService(playlist_raises=_soft_http_error())
    assert recent_upload_ids(svc, "UU123", AFTER, 10) == []
    # And it must not have masqueraded as a quota stop.
    try:
        recent_upload_ids(svc, "UU123", AFTER, 10)
    except niche_pull.QuotaExceeded:
        raise AssertionError("a non-quota error must not raise QuotaExceeded")


def test_quota_estimate():
    # Channel-only path (keyword search off): 2 units/handle, the ~100x cut.
    assert _quota_estimate(9, 8, keyword_search=False) == 18
    assert _quota_estimate(0, 0, keyword_search=False) == 0
    # Keyword search opted in adds 100/query on top of the channel path.
    assert _quota_estimate(9, 8, keyword_search=True) == 18 + 800


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok {name}")
    print("test_niche_pull: OK")
