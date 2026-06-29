"""Guards the caption-sweep error bucketing: a gone video (404) must NOT page,
a transient blip must NOT page, only a real hard error pages. Regression test for
the 2026-06-29 fix (V4/V6/V7 404 videoNotFound red-alerting every sweep)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from googleapiclient.errors import HttpError  # noqa: E402
from upload_video import _caption_error_bucket  # noqa: E402


class _Resp:
    def __init__(self, status):
        self.status = status
        self.reason = "test"


def _http(status):
    return HttpError(_Resp(status), b"{}")


def test_buckets():
    assert _caption_error_bucket(_http(404)) == "gone"       # video deleted/stale id
    assert _caption_error_bucket(_http(503)) == "transient"  # retriable 5xx
    assert _caption_error_bucket(_http(429)) == "transient"  # rate limit
    assert _caption_error_bucket(_http(403)) == "errors"     # quota/scope — real, page
    assert _caption_error_bucket(_http(401)) == "errors"     # auth — real, page
    assert _caption_error_bucket(OSError("conn reset")) == "transient"
    assert _caption_error_bucket(ValueError("bad")) == "errors"


if __name__ == "__main__":
    test_buckets()
    print("OK")
