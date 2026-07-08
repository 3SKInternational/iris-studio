"""Regression tests for build_video.clamp_scene_dwell — the standard shot-pacing
guard (Steve 2026-07-08: every image 8-12s, no flashes/over-holds).

Run: python3 tests/test_dwell_clamp.py
"""
import importlib.util
import unittest
from pathlib import Path

_BV = Path(__file__).resolve().parent.parent / "build_video.py"
_spec = importlib.util.spec_from_file_location("build_video", _BV)
bv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bv)


def _durs(cuts, dur, k):
    b = [0.0, *cuts, dur]
    return [round(b[i + 1] - b[i], 2) for i in range(k)]


class TestDwellClamp(unittest.TestCase):
    def test_in_band_unchanged(self):
        # scene 60s, 6 shots of 10s each — already in band → keep alignment.
        self.assertEqual(bv.clamp_scene_dwell([10, 20, 30, 40, 50], 60, 6),
                         ([10, 20, 30, 40, 50], False))

    def test_flash_to_even(self):
        cuts, did = bv.clamp_scene_dwell([2, 4, 20, 35, 50], 60, 6)  # 2s/2s flashes
        self.assertTrue(did)
        self.assertEqual(_durs(cuts, 60, 6), [10.0] * 6)

    def test_overhold_to_even(self):
        cuts, did = bv.clamp_scene_dwell([5, 10, 20, 30, 35], 60, 6)  # 25s tail hold
        self.assertTrue(did)
        self.assertTrue(all(8.0 <= d <= 12.0 for d in _durs(cuts, 60, 6)))

    def test_too_dense_kept(self):
        # 30s / 6 shots → eq=5s (<8): unfixable here, keep as-is (not force-broken).
        self.assertFalse(bv.clamp_scene_dwell([3, 8, 14, 20, 26], 30, 6)[1])

    def test_too_sparse_kept(self):
        # 40s / 2 shots → eq=20s (>12): keep as-is.
        self.assertFalse(bv.clamp_scene_dwell([5], 40, 2)[1])

    def test_boundary_inclusive(self):
        # shots of exactly 12s and 8s are in-band → not clamped.
        self.assertEqual(bv.clamp_scene_dwell([12], 20, 2), ([12], False))

    def test_guards(self):
        self.assertEqual(bv.clamp_scene_dwell([], 60, 1), ([], False))
        self.assertEqual(bv.clamp_scene_dwell(None, 60, 3), (None, False))
        self.assertEqual(bv.clamp_scene_dwell([10], 20, 1), ([10], False))  # k<2

    def test_clamped_cuts_strictly_increasing_in_range(self):
        cuts, did = bv.clamp_scene_dwell([1, 29], 30, 3)
        self.assertTrue(did)
        self.assertEqual(cuts, [10.0, 20.0])
        self.assertTrue(all(0 < c < 30 for c in cuts))
        self.assertTrue(all(cuts[i] < cuts[i + 1] for i in range(len(cuts) - 1)))


if __name__ == "__main__":
    unittest.main()
