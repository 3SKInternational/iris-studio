"""Guards the caption servability fix (2026-07-06, V9 stuck-track bug).

Root cause: a caption track inserted while the video is still processing reports
'serving' in captions.list but is permanently non-servable (the player renders
nothing). The fixes: (1) never attach captions until processingStatus=succeeded,
(2) the sweep re-inserts a present-but-untrusted (stuck-suspect) track instead of
skipping it, and an in-place update does NOT un-stick a track — only delete+insert.

These tests use fakes only; no network / no real YouTube calls.
"""
import atexit
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from upload_video import (  # noqa: E402
    _caption_trusted,
    sweep_captions,
    upsert_captions,
    video_processing_status,
)

_SRT = "1\n00:00:00,000 --> 00:00:02,000\nHello world.\n"


class _Req:
    def __init__(self, result):
        self._r = result

    def execute(self):
        return self._r


class FakeVideos:
    """videos().list(part='processingDetails,status', id=...) stub."""

    def __init__(self, processing_status="succeeded", use_upload_status=False):
        self.processing_status = processing_status
        self.use_upload_status = use_upload_status

    def list(self, part=None, id=None):
        if self.processing_status == "gone":
            return _Req({"items": []})  # deleted / not-owned id → HTTP 200 empty items
        upload = "processed" if self.processing_status == "succeeded" else "uploaded"
        item = {"status": {"uploadStatus": upload}}
        if not self.use_upload_status:
            item["processingDetails"] = {"processingStatus": self.processing_status}
        return _Req({"items": [item]})


class FakeCaptions:
    """captions().list/insert/update/delete stub with a shared track list + op log."""

    def __init__(self, tracks):
        self.tracks = list(tracks)  # each {"id","snippet":{language,name,trackKind}}
        self.ops = []
        self._n = 0

    def list(self, part=None, videoId=None):
        return _Req({"items": self.tracks})

    def insert(self, part=None, body=None, media_body=None, sync=None):
        self._n += 1
        tid = f"new{self._n}"
        self.ops.append(("insert", tid))
        snip = dict(body["snippet"])
        snip.setdefault("trackKind", "standard")
        self.tracks.append({"id": tid, "snippet": snip})
        return _Req({"id": tid})

    def update(self, part=None, body=None, media_body=None, sync=None):
        self.ops.append(("update", body["id"]))
        return _Req({"id": body["id"]})

    def delete(self, id=None):
        self.ops.append(("delete", id))
        self.tracks = [t for t in self.tracks if t["id"] != id]
        return _Req({})


class FakeYouTube:
    def __init__(self, processing_status="succeeded", tracks=None, use_upload_status=False):
        self._videos = FakeVideos(processing_status, use_upload_status)
        self._captions = FakeCaptions(tracks or [])

    def videos(self):
        return self._videos

    def captions(self):
        return self._captions


def _track(tid="old", lang="en", name="English"):
    return {"id": tid, "snippet": {"language": lang, "name": name, "trackKind": "standard"}}


# One real on-disk SRT so MediaFileUpload (constructed inside upsert_captions) has a
# file to stat. Created once at import — works under both pytest and __main__.
_SRT_FILE = tempfile.NamedTemporaryFile("w", suffix=".srt", delete=False)
_SRT_FILE.write(_SRT)
_SRT_FILE.close()
_SRT_PATH = _SRT_FILE.name
atexit.register(lambda: os.path.exists(_SRT_PATH) and os.unlink(_SRT_PATH))


# --- unit: processing status parsing ---------------------------------------

def test_processing_status():
    assert video_processing_status(FakeYouTube("succeeded"), "v") == "succeeded"
    assert video_processing_status(FakeYouTube("processing"), "v") == "processing"
    # falls back to status.uploadStatus when processingDetails is absent.
    assert video_processing_status(FakeYouTube("succeeded", use_upload_status=True), "v") == "succeeded"
    assert video_processing_status(FakeYouTube("processing", use_upload_status=True), "v") == "uploaded"
    # deleted / not-owned id → HTTP 200 empty items → terminal 'gone'.
    assert video_processing_status(FakeYouTube("gone"), "v") == "gone"


# --- unit: the stuck-track discriminator -----------------------------------

def test_caption_trusted_discriminator():
    # V9-like: captioned only by the old inline path → NOT trusted (stuck-suspect).
    assert _caption_trusted({"captions_set": True}) is False
    # legacy captions_updated_at is NOT trusted — an update doesn't prove servability,
    # so legacy videos get re-inserted once (deterministic fleet heal).
    assert _caption_trusted({"captions_set": True, "captions_updated_at": "2026-06-19T..."}) is False
    # only the explicit post-processing flag confers trust.
    assert _caption_trusted({"captions_post_processing": True}) is True
    assert _caption_trusted({}) is False


# --- unit: replace does delete+insert, not update --------------------------

def test_upsert_replace_deletes_then_inserts():
    fy = FakeYouTube(tracks=[_track("old")])
    assert upsert_captions(fy, "vid", _SRT_PATH, replace=True) == "replace"
    ops = fy.captions().ops
    assert ("delete", "old") in ops, ops
    assert any(op == "insert" for op, _ in ops), ops
    # order: delete BEFORE insert (else the fresh track is the one deleted).
    assert ops.index(("delete", "old")) < next(i for i, (o, _) in enumerate(ops) if o == "insert")


def test_upsert_no_replace_updates_in_place():
    fy = FakeYouTube(tracks=[_track("old")])
    assert upsert_captions(fy, "vid", _SRT_PATH, replace=False) == "update"
    assert ("update", "old") in fy.captions().ops
    assert not any(op == "delete" for op, _ in fy.captions().ops)


# --- integration: the sweep repairs stuck, skips servable, defers processing -

def _vault(tmp, receipts):
    vlt = Path(tmp)
    (vlt / "Production_Kits").mkdir(parents=True)
    (vlt / "Footage_and_Edits").mkdir(parents=True)
    for label, data in receipts.items():
        (vlt / "Production_Kits" / f"{label}_youtube_upload.json").write_text(
            json.dumps(data), encoding="utf-8")
        (vlt / "Footage_and_Edits" / f"{label}_v2.srt").write_text(_SRT, encoding="utf-8")
    return vlt


def test_sweep_repairs_untrusted_skips_trusted():
    with tempfile.TemporaryDirectory() as tmp:
        vlt = _vault(tmp, {
            # trusted (has the post-processing flag) → skip, no re-insert.
            "Video_01": {"video": "Video_01", "video_id": "v1",
                         "captions_set": True, "captions_post_processing": True},
            # V9-like stuck-suspect (no flag) → delete+re-insert.
            "Video_09": {"video": "Video_09", "video_id": "v9", "captions_set": True},
        })
        fy = FakeYouTube(processing_status="succeeded", tracks=[_track("old")])
        s = sweep_captions(fy, vlt)
        assert s["checked"] == 2, s
        assert s["skipped"] == 1, s          # Video_01 trusted
        assert s["repaired"] == 1, s         # Video_09 stuck → repaired
        assert s["added"] == 0 and s["updated"] == 0, s
        # the repair actually deleted the stale track and inserted a fresh one.
        ops = fy.captions().ops
        assert ("delete", "old") in ops, ops
        assert any(op == "insert" for op, _ in ops), ops
        # and V9's receipt is now stamped trusted so the next sweep skips it.
        r = json.loads((vlt / "Production_Kits" / "Video_09_youtube_upload.json").read_text())
        assert r.get("captions_post_processing") is True
        assert _caption_trusted(r) is True


def test_sweep_defers_while_processing():
    with tempfile.TemporaryDirectory() as tmp:
        vlt = _vault(tmp, {
            "Video_09": {"video": "Video_09", "video_id": "v9", "captions_set": True},
        })
        fy = FakeYouTube(processing_status="processing", tracks=[_track("old")])
        s = sweep_captions(fy, vlt)
        assert s["processing"] == 1, s
        assert s["repaired"] == 0 and s["added"] == 0 and s["updated"] == 0, s
        # nothing touched the caption track while the video was still processing.
        assert fy.captions().ops == [], fy.captions().ops


def test_sweep_buckets_gone_video():
    with tempfile.TemporaryDirectory() as tmp:
        vlt = _vault(tmp, {
            "Video_09": {"video": "Video_09", "video_id": "v9", "captions_set": True},
        })
        # deleted / not-owned id → empty items → 'gone' bucket, never 'processing'.
        fy = FakeYouTube(processing_status="gone", tracks=[_track("old")])
        s = sweep_captions(fy, vlt)
        assert s["gone"] == 1, s
        assert s["processing"] == 0 and s["repaired"] == 0 and s["errors"] == 0, s
        assert fy.captions().ops == [], fy.captions().ops


if __name__ == "__main__":
    test_processing_status()
    test_caption_trusted_discriminator()
    test_upsert_replace_deletes_then_inserts()
    test_upsert_no_replace_updates_in_place()
    test_sweep_repairs_untrusted_skips_trusted()
    test_sweep_defers_while_processing()
    test_sweep_buckets_gone_video()
    print("OK")
