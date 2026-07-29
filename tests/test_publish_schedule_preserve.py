#!/usr/bin/env python3
"""Regression: publish_video.build_status must not silently cancel a schedule.

THE DEFECT (two-pass codebase audit, lane C, 2026-07-28). videos.update REPLACES
the status part wholesale. publish_video rebuilt `status` from a whitelist and wrote
`publishAt` ONLY when --publish-at was passed, never reading the live one back. So the
documented metadata-refresh form:

    publish_video.py Video_09 --privacy private     # fix a description typo

on a SCHEDULED video dropped its publishAt — the video went private with no schedule
and never published. Silently: `going_public` is False so the release gate never fires,
and the plan block printed nothing about the schedule it was about to destroy.

It was also self-concealing. The receipt write stored `publish_at: None`, destroying
the local record, and youtube_reality_check then compared private-vs-private and
reported CLEAN — the same command rewrote the receipt the tripwire diffs against.
9 of 13 in-tree receipts carry a publish_at.

Run: python3 tests/test_publish_schedule_preserve.py   (exit 0 = pass)
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import types
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "publish_video", Path(__file__).resolve().parents[1] / "scripts" / "publish_video.py"
)
pv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pv)

SCHEDULED = {"privacyStatus": "private", "publishAt": "2026-10-01T13:00:00Z",
             "selfDeclaredMadeForKids": False, "license": "youtube"}


# --- no-network main() wiring harness ---------------------------------------
# main()'s `if conflict_msg: die(conflict_msg)` (round-2 fix) only becomes
# reachable AFTER the live videos().list() fetch -- unlike upload_video.py's
# guard, which fires before any network call. A subprocess --dry-run run
# returns before the fetch and can't exercise it (see the file's own May-28
# docstring note). This mocks the YouTube client chain (no real network, no
# live channel, no billed API -- pure in-memory objects) so the wiring is
# directly testable, same "extract a seam, then test the seam" pattern
# tests/test_lint_notice_branches.py already uses for iris.py.

class _FakeRequest:
    def __init__(self, result=None, forbid=None):
        self._result = result
        self._forbid = forbid  # message to raise on if this must never execute

    def execute(self):
        if self._forbid:
            raise AssertionError(self._forbid)
        return self._result


class _FakeVideos:
    def __init__(self, list_result):
        self._list_result = list_result

    def list(self, **kw):
        return _FakeRequest(self._list_result)

    def update(self, **kw):
        return _FakeRequest(forbid="videos().update must NOT be called -- "
                                    "the conflict guard should have died() first")


class _FakeYouTube:
    def __init__(self, list_result):
        self._videos = _FakeVideos(list_result)

    def videos(self):
        return self._videos


def _stub_googleapiclient():
    """googleapiclient isn't installed in this env (test_caption_error_bucket
    env-skips on it too) -- main() does `from googleapiclient.errors import
    HttpError` unconditionally past the dry-run return, so it must be stubbed
    for ANY no-dry-run main() call, not just the conflict path."""
    if "googleapiclient" in sys.modules and hasattr(
            sys.modules.get("googleapiclient.errors", None), "HttpError"):
        return
    pkg = types.ModuleType("googleapiclient")
    errors = types.ModuleType("googleapiclient.errors")
    errors.HttpError = type("HttpError", (Exception,), {})
    sys.modules["googleapiclient"] = pkg
    sys.modules["googleapiclient.errors"] = errors


def _run_main_no_network(argv, list_result):
    """Run the REAL pv.main() with load_credentials/build_data_service mocked
    to a fake in-memory YouTube client -- no network, no live channel, no
    billed API. Returns None normally or re-raises SystemExit."""
    _stub_googleapiclient()
    old_argv = sys.argv
    old_load_creds, old_build_svc = pv.load_credentials, pv.build_data_service
    sys.argv = ["publish_video.py"] + argv
    pv.load_credentials = lambda token: object()
    pv.build_data_service = lambda creds: _FakeYouTube(list_result)
    try:
        pv.main()
    finally:
        sys.argv = old_argv
        pv.load_credentials, pv.build_data_service = old_load_creds, old_build_svc


def _make_vault(video="Video_09", video_id="DY2RVnuUb64"):
    d = tempfile.mkdtemp(prefix="pv_novnet_")
    vlt = Path(d)
    (vlt / "Production_Kits").mkdir(parents=True)
    (vlt / "Production_Kits" / f"{video}_youtube_upload.json").write_text(
        json.dumps({"video_id": video_id, "privacy": "private"}))
    (vlt / "Video_Descriptions").mkdir(parents=True)
    (vlt / "Video_Descriptions" / f"{video}_Description.md").write_text(
        "---\nyoutube_title: A Title\n---\n## Description\nSome description body.\n")
    return vlt


def test_main_dies_before_update_when_unlisted_conflicts_with_schedule():
    """THE wiring test for the round-2 fix: unlisted + a live schedule must
    die() BEFORE videos().update() is ever called -- kills the :370
    if-test->False/True survivors (nothing in the suite previously reached
    main() this far)."""
    vlt = _make_vault()
    old_env = os.environ.get("SK_VAULT")
    os.environ["SK_VAULT"] = str(vlt)
    live = {"items": [{"snippet": {"categoryId": "27"},
                       "status": dict(SCHEDULED)}]}
    try:
        try:
            _run_main_no_network(["Video_09", "--privacy", "unlisted"], live)
            assert False, "must have died() on the unlisted+schedule conflict"
        except SystemExit as exc:
            assert exc.code == 1, exc.code
    finally:
        if old_env is None:
            os.environ.pop("SK_VAULT", None)
        else:
            os.environ["SK_VAULT"] = old_env


def test_main_reaches_update_when_privacy_private_no_conflict():
    """Control: private (no unlisted+schedule conflict) must NOT die here --
    it reaches videos().update(), which this fixture's fake DOES allow to
    execute (no `forbid`), proving the guard is not over-firing."""
    vlt = _make_vault()
    old_env = os.environ.get("SK_VAULT")
    os.environ["SK_VAULT"] = str(vlt)
    live = {"items": [{"snippet": {"categoryId": "27"},
                       "status": dict(SCHEDULED)}]}

    class _AllowingVideos(_FakeVideos):
        def update(self, **kw):
            # Echo containsSyntheticMedia, as the real API does when it ACCEPTS the
            # declaration. main() now verifies persistence from this response — the
            # update response is the only place the flag is observable, since
            # videos.list never returns it to the owner. A mock that stays silent
            # here is indistinguishable from an API that rejected the disclosure.
            return _FakeRequest({"status": {"privacyStatus": "private",
                                            "containsSyntheticMedia": True}})

    class _AllowingYouTube(_FakeYouTube):
        def __init__(self, list_result):
            self._videos = _AllowingVideos(list_result)

    try:
        _stub_googleapiclient()
        old_argv = sys.argv
        old_load_creds, old_build_svc = pv.load_credentials, pv.build_data_service
        sys.argv = ["publish_video.py", "Video_09", "--privacy", "private", "--no-captions"]
        pv.load_credentials = lambda token: object()
        pv.build_data_service = lambda creds: _AllowingYouTube(live)
        try:
            pv.main()  # must NOT raise
        finally:
            sys.argv = old_argv
            pv.load_credentials, pv.build_data_service = old_load_creds, old_build_svc
    finally:
        if old_env is None:
            os.environ.pop("SK_VAULT", None)
        else:
            os.environ["SK_VAULT"] = old_env


def test_refresh_preserves_existing_schedule():
    """THE regression. A metadata refresh on a scheduled video keeps publishAt."""
    st = pv.build_status("private", SCHEDULED, publish_at=None, clear_schedule=False)
    assert st.get("publishAt") == "2026-10-01T13:00:00Z", \
        f"refresh silently cancelled the scheduled release: {st}"


def test_explicit_publish_at_wins():
    st = pv.build_status("private", SCHEDULED, publish_at="2026-11-05T09:00:00Z",
                         clear_schedule=False)
    assert st["publishAt"] == "2026-11-05T09:00:00+00:00", st


def test_clear_schedule_drops_it():
    """Unscheduling must still be possible — but only when asked for explicitly."""
    st = pv.build_status("private", SCHEDULED, publish_at=None, clear_schedule=True)
    assert "publishAt" not in st, f"--clear-schedule should drop the schedule: {st}"


def test_going_public_does_not_carry_publish_at():
    """publishAt is meaningless once privacy is public; an immediate publish is a
    deliberate supersede of the schedule, not an accident."""
    st = pv.build_status("public", SCHEDULED, publish_at=None, clear_schedule=False)
    assert "publishAt" not in st, f"public must not carry publishAt: {st}"


def test_unscheduled_video_stays_unscheduled():
    """No schedule in, no schedule out — the preserve branch must not invent one."""
    live = {"privacyStatus": "private", "selfDeclaredMadeForKids": False}
    st = pv.build_status("private", live, publish_at=None, clear_schedule=False)
    assert "publishAt" not in st, st


def test_other_writable_fields_still_carried():
    """The whitelist carry-over must keep working (this is why status is rebuilt)."""
    live = dict(SCHEDULED, embeddable=True, publicStatsViewable=False, madeForKids=True)
    st = pv.build_status("private", live, publish_at=None, clear_schedule=False)
    assert st["license"] == "youtube" and st["embeddable"] is True, st
    assert st["publicStatsViewable"] is False, st
    # selfDeclaredMadeForKids present on the live resource wins over madeForKids echo
    assert st["selfDeclaredMadeForKids"] is False, st


def test_made_for_kids_falls_back_to_readonly_echo():
    live = {"privacyStatus": "private", "madeForKids": True}
    st = pv.build_status("private", live, publish_at=None, clear_schedule=False)
    assert st["selfDeclaredMadeForKids"] is True, st


def test_unlisted_does_not_preserve_a_schedule():
    """ROUND-2 REGRESSION (2026-07-28 review). The preserve condition used to be
    `target_privacy != "public"`, which also matches "unlisted" -- but YouTube
    pairs a scheduled publishAt with privacyStatus=private ONLY
    (schedule_publish.py:170 records this as a required pairing). Sending
    publishAt alongside unlisted would send an illegal combination. build_status
    itself must never construct it (main() separately die()s before reaching
    here -- see test_main_dies_on_unlisted_with_existing_schedule below)."""
    st = pv.build_status("unlisted", SCHEDULED, publish_at=None, clear_schedule=False)
    assert "publishAt" not in st, f"unlisted must never carry a schedule: {st}"


def test_unlisted_schedule_conflict_message_blocks():
    """The pure guard main() calls before the API: unlisted + a live schedule
    (no --clear-schedule) must refuse, naming both the schedule and the escape
    hatch."""
    msg = pv.unlisted_schedule_conflict_message(
        "unlisted", False, SCHEDULED, "Video_09")
    assert msg is not None, "unlisted with a live schedule must block"
    assert "2026-10-01T13:00:00Z" in msg, msg
    assert "--clear-schedule" in msg, msg


def test_unlisted_schedule_conflict_message_exempts_clear_schedule():
    """--clear-schedule is how you escape the conflict -- must not block."""
    assert pv.unlisted_schedule_conflict_message(
        "unlisted", True, SCHEDULED, "Video_09") is None


def test_unlisted_schedule_conflict_message_ignores_unscheduled_video():
    """No live schedule -> unlisted is perfectly fine, must not block."""
    live = {"privacyStatus": "private"}
    assert pv.unlisted_schedule_conflict_message(
        "unlisted", False, live, "Video_09") is None


def test_unlisted_schedule_conflict_message_ignores_private_target():
    """The conflict is specific to unlisted; private legitimately preserves
    the schedule (see test_refresh_preserves_existing_schedule above)."""
    assert pv.unlisted_schedule_conflict_message(
        "private", False, SCHEDULED, "Video_09") is None


def test_synthetic_disclosure_is_reasserted_by_default():
    """THE compliance regression. videos.update REPLACES the status part, and the
    API reference is explicit: a property omitted from the request is DELETED.
    publish_video.py never set containsSyntheticMedia at all, so every metadata
    refresh silently stripped the AI-disclosure flag — undetectably, because
    videos.list does not return the property to the owner (None for all 12 live
    videos). Six receipts record published_via: publish_video.py."""
    st = pv.build_status("private", SCHEDULED, publish_at=None, clear_schedule=False)
    assert st.get("containsSyntheticMedia") is True, \
        f"the disclosure must be re-asserted on EVERY update, else it is deleted: {st}"


def test_synthetic_disclosure_survives_every_schedule_path():
    """It must not depend on which schedule branch runs."""
    for kwargs in (dict(publish_at=None, clear_schedule=False),
                   dict(publish_at="2030-05-05T09:00:00Z", clear_schedule=False),
                   dict(publish_at=None, clear_schedule=True)):
        st = pv.build_status("private", SCHEDULED, **kwargs)
        assert st.get("containsSyntheticMedia") is True, (kwargs, st)
    st = pv.build_status("public", SCHEDULED, publish_at=None, clear_schedule=False)
    assert st.get("containsSyntheticMedia") is True, \
        "going public is exactly when the disclosure matters most"


def test_no_synthetic_opt_out_is_respected():
    """A deliberate non-AI upload can still opt out — but only explicitly."""
    st = pv.build_status("private", SCHEDULED, publish_at=None, clear_schedule=False,
                         synthetic=False)
    assert "containsSyntheticMedia" not in st, st


def test_main_dies_if_the_disclosure_is_not_echoed_back():
    """A response that does NOT confirm the disclosure must abort, not carry on.

    videos.update DELETES any status property the request omits, and videos.list
    never returns containsSyntheticMedia to the owner — so the update RESPONSE is
    the only evidence the flag actually landed. If it comes back None, the video
    may be public with no AI-disclosure label and nothing downstream could tell.
    Silence here would be a fail-open on a compliance control."""
    vlt = _make_vault()
    old_env = os.environ.get("SK_VAULT")
    os.environ["SK_VAULT"] = str(vlt)
    live = {"items": [{"snippet": {"categoryId": "27"}, "status": dict(SCHEDULED)}]}

    class _SilentVideos(_FakeVideos):
        def update(self, **kw):
            # accepted the update but did NOT confirm the disclosure
            return _FakeRequest({"status": {"privacyStatus": "private"}})

    class _SilentYouTube(_FakeYouTube):
        def __init__(self, list_result):
            self._videos = _SilentVideos(list_result)

    try:
        _stub_googleapiclient()
        old_argv = sys.argv
        old_load_creds, old_build_svc = pv.load_credentials, pv.build_data_service
        sys.argv = ["publish_video.py", "Video_09", "--privacy", "private", "--no-captions"]
        pv.load_credentials = lambda token: object()
        pv.build_data_service = lambda creds: _SilentYouTube(live)
        try:
            raised = False
            try:
                pv.main()
            except SystemExit:
                raised = True
            assert raised, ("an unconfirmed synthetic-media disclosure must die(), "
                            "not pass silently — the video could be public with no "
                            "AI-disclosure label")
        finally:
            sys.argv = old_argv
            pv.load_credentials, pv.build_data_service = old_load_creds, old_build_svc
    finally:
        if old_env is None:
            os.environ.pop("SK_VAULT", None)
        else:
            os.environ["SK_VAULT"] = old_env


def test_no_synthetic_does_not_demand_an_echo():
    """With --no-synthetic the flag is never SENT, so the response has nothing to
    echo — demanding confirmation there would die() on a legitimate run.

    Guards the other side of the verification conditional: forcing it to always
    verify passes every other case (they all send the flag AND get it echoed),
    and only this one distinguishes them."""
    vlt = _make_vault()
    old_env = os.environ.get("SK_VAULT")
    os.environ["SK_VAULT"] = str(vlt)
    live = {"items": [{"snippet": {"categoryId": "27"}, "status": dict(SCHEDULED)}]}

    class _SilentVideos(_FakeVideos):
        def update(self, **kw):
            return _FakeRequest({"status": {"privacyStatus": "private"}})

    class _SilentYouTube(_FakeYouTube):
        def __init__(self, list_result):
            self._videos = _SilentVideos(list_result)

    try:
        _stub_googleapiclient()
        old_argv = sys.argv
        old_load_creds, old_build_svc = pv.load_credentials, pv.build_data_service
        sys.argv = ["publish_video.py", "Video_09", "--privacy", "private",
                    "--no-captions", "--no-synthetic"]
        pv.load_credentials = lambda token: object()
        pv.build_data_service = lambda creds: _SilentYouTube(live)
        try:
            pv.main()  # must NOT raise — nothing was declared, nothing to confirm
        finally:
            sys.argv = old_argv
            pv.load_credentials, pv.build_data_service = old_load_creds, old_build_svc
    finally:
        if old_env is None:
            os.environ.pop("SK_VAULT", None)
        else:
            os.environ["SK_VAULT"] = old_env


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
    print(f"test_publish_schedule_preserve: {n}/{n} pass")
