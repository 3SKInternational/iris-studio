"""Self-check for the dual-form caption token {{spoken|caption}}.

Guards against the two parsers drifting: the VO must speak the LEFT form while the
SRT caption shows the RIGHT (digits) form, from the SAME kit text. This was the
Video_05 failure mode (two separate kit files silently diverged)."""
import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parent


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_dual_form_splits_vo_and_caption():
    gv = _load("generate_vo", "vo_factory/generate_vo.py")
    bv = _load("build_video", "build_video.py")
    raw = "You crossed {{one hundred thousand dollars|$100,000}} at Level {{three|3}}."
    vo = gv.clean_vo_text(raw)
    assert "one hundred thousand dollars" in vo and "$100,000" not in vo, vo
    assert "Level three" in vo and "Level 3" not in vo, vo
    cap = bv._DUAL_FORM_RE.sub(r"\2", raw)
    assert "$100,000" in cap and "one hundred thousand" not in cap, cap
    assert "Level 3" in cap and "Level three" not in cap, cap


def test_no_token_is_noop():
    gv = _load("generate_vo", "vo_factory/generate_vo.py")
    bv = _load("build_video", "build_video.py")
    raw = "Plain narration, $100,000 stays as written."
    assert "$100,000" in gv.clean_vo_text(raw)
    assert bv._DUAL_FORM_RE.sub(r"\2", raw) == raw


def test_regexes_are_identical():
    gv = _load("generate_vo", "vo_factory/generate_vo.py")
    bv = _load("build_video", "build_video.py")
    assert gv._DUAL_RE.pattern == bv._DUAL_FORM_RE.pattern


if __name__ == "__main__":
    test_dual_form_splits_vo_and_caption()
    test_no_token_is_noop()
    test_regexes_are_identical()
    print("ok — 3/3")
