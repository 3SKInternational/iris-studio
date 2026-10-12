"""Tests for scripts/premise_saturation.py — offline, fixture entries only.

Run: python3 tests/test_premise_saturation.py
"""
import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import premise_saturation as ps  # noqa: E402

TODAY = dt.date(2026, 10, 11)
Q = "POV richest person in your friend group"


def entry(days_ago, views=10, subs=5, title="POV: You Became the Richest Person in Your Friend Group"):
    d = TODAY - dt.timedelta(days=days_ago)
    return {"title": title, "upload_date": d.strftime("%Y%m%d"), "view_count": views,
            "channel_follower_count": subs, "channel": "Ch", "webpage_url": "u"}


class TestSamePremise(unittest.TestCase):
    def test_matching_title(self):
        self.assertTrue(ps.same_premise(Q, "POV: You're Secretly the Richest in Your Friend Group"))

    def test_unrelated_title(self):
        self.assertFalse(ps.same_premise(Q, "Billionaire Reborn as a Schooler Finds a Mystery Safe"))

    def test_one_shared_word_is_not_enough(self):
        self.assertFalse(ps.same_premise("paycheck cycle", "The paycheck nobody wants"))

    def test_half_of_keywords_is_enough(self):
        # keywords: richest, person, friend, group (4; "pov" is a format word)
        self.assertTrue(ps.same_premise(Q, "The richest group of planets"))      # 2 of 4
        self.assertFalse(ps.same_premise("richest person friend group money",
                                         "The richest group of planets"))      # 2 of 5

    def test_pov_is_not_a_premise_word(self):
        self.assertFalse(ps.same_premise(Q, "POV: Your Friend Moves Abroad"))  # only "friend"

    def test_plurals_fold(self):
        self.assertTrue(ps.same_premise(Q, "POV: You're the Richest of Your Friends"))
        self.assertEqual(ps.keywords("class bus cars"), {"class", "bus", "car"})

    def test_commas_in_numbers(self):
        self.assertIn("$10000", ps.keywords("Saving $10,000 fast"))

    def test_two_shared_keywords_is_enough(self):
        # 3 keywords, 2 shared: passes the 2-word minimum and the half rule
        self.assertTrue(ps.same_premise("paycheck cycle family", "The paycheck cycle"))

    def test_stopword_only_query(self):
        self.assertFalse(ps.same_premise("the of in", "the of in"))


class TestClassify(unittest.TestCase):
    def c(self, entries, q=Q):
        return ps.classify(entries, q, TODAY)

    def test_saturated_by_count_boundary(self):
        self.assertEqual(self.c([entry(5)] * 5)["verdict"], "CROWDED")
        self.assertEqual(self.c([entry(5)] * 6)["verdict"], "SATURATED")

    def test_crowded_boundary(self):
        self.assertEqual(self.c([entry(5)] * 2)["verdict"], "CLEAR")
        self.assertEqual(self.c([entry(5)] * 3)["verdict"], "CROWDED")

    def test_breakout_boundary(self):
        self.assertEqual(self.c([entry(80, views=99_999)])["verdict"], "CLEAR")
        self.assertEqual(self.c([entry(80, views=100_000)])["verdict"], "SATURATED")

    def test_recent_window_edges(self):
        self.assertEqual(self.c([entry(60)])["recent"], 1)
        self.assertEqual(self.c([entry(61)])["recent"], 0)

    def test_breakout_window_edges(self):
        self.assertEqual(self.c([entry(90, views=200_000)])["verdict"], "SATURATED")
        r = self.c([entry(91, views=200_000)])
        self.assertEqual((r["verdict"], r["top"]), ("CLEAR", None))

    def test_small_channel_count(self):
        r = self.c([entry(5, subs=999), entry(5, subs=1000), entry(5, subs=None)])
        self.assertEqual((r["recent"], r["small"]), (3, 1))

    def test_unrelated_titles_ignored(self):
        r = self.c([entry(5, title="Gold vs Silver — The Real Math")] * 10)
        self.assertEqual((r["recent"], r["verdict"]), (0, "CLEAR"))

    def test_same_day_upload_counts(self):
        self.assertEqual(self.c([entry(0)])["recent"], 1)

    def test_bad_and_future_dates_skipped(self):
        bad = entry(5); bad["upload_date"] = "2026-13-01"
        none = entry(5); none["upload_date"] = None
        r = self.c([bad, none, entry(-3)])
        self.assertEqual((r["recent"], r["matched"]), (0, []))

    def test_top_is_biggest_and_sorted(self):
        r = self.c([entry(5, views=10), entry(70, views=500), entry(3, views=40)])
        self.assertEqual(r["top"]["views"], 500)
        self.assertEqual([m["views"] for m in r["matched"]], [500, 40, 10])

    def test_missing_views_counts_as_zero(self):
        e = entry(5); e["view_count"] = None
        self.assertEqual(self.c([e])["top"]["views"], 0)


class TestIds(unittest.TestCase):
    def test_split_id(self):
        self.assertEqual(ps.split_id("B22: friend group"), ("B22", "friend group"))
        self.assertEqual(ps.split_id("  B7 :x y"), ("B7", "x y"))
        self.assertEqual(ps.split_id("plain phrase"), ("", "plain phrase"))
        self.assertEqual(ps.split_id("b22: lower"), ("B22", "lower"))


class TestClassifyFailures(unittest.TestCase):
    def test_none_entries_is_error(self):
        r = ps.classify(None, Q, TODAY)
        self.assertEqual((r["verdict"], r["recent"], r["small"], r["top"]), ("ERROR", 0, 0, None))

    def test_none_items_skipped(self):
        self.assertEqual(ps.classify([None, entry(5)], Q, TODAY)["recent"], 1)


class TestRender(unittest.TestCase):
    def test_cells_escaped(self):
        e = entry(5, title="POV: Richest [Friend] Group | #rich")
        e["channel"] = "A|B"
        r = ps.classify([e], Q, TODAY); r["id"] = "B22"; r["query"] = "q|x"
        md = ps.render([r], TODAY)
        self.assertIn("[POV: Richest (Friend) Group / rich](u)", md)
        self.assertIn("| A/B (5) |", md)
        self.assertIn("| B22 | q/x |", md)
        self.assertNotIn("#rich", md)

    def test_detail_rows(self):
        a = entry(5, views=166_820, subs=24_200)
        b = entry(6, views=9, subs=None); del b["channel"]; del b["webpage_url"]
        md = ps.render([ps.classify([a, b], Q, TODAY)], TODAY)
        self.assertIn("| 166,820 | [POV: You Became the Richest Person in Your Friend Group](u) "
                      "| Ch (24,200) | 2026-10-06 |", md)
        self.assertIn("| 9 | [POV: You Became the Richest Person in Your Friend Group]() "
                      "| ? (n/a) | 2026-10-05 |", md)
        self.assertNotIn("No same-premise uploads", md)

    def test_detail_capped_at_12(self):
        r = ps.classify([entry(5, views=v) for v in range(13)], Q, TODAY)
        md = ps.render([r], TODAY)
        self.assertEqual(md.count("](u) | Ch (5) |"), 12)

    def test_summary_and_detail(self):
        r = ps.classify([entry(5, views=166_820, subs=24_200)] + [entry(5)] * 6, Q, TODAY)
        md = ps.render([r, ps.classify([], "first in your family", TODAY)], TODAY)
        self.assertIn("type: premise-saturation", md)
        self.assertIn(f"| - | {Q} | **SATURATED** | 7 | 6 | 166,820 views:", md)
        self.assertIn("| - | first in your family | **CLEAR** | 0 | 0 | none |", md)
        self.assertIn("status: ok", md)
        self.assertIn("No same-premise uploads in the last 90 days.", md)

    def test_pipes_in_titles_escaped(self):
        r = ps.classify([entry(5, title="POV | Richest Person | Friend Group")], Q, TODAY)
        md = ps.render([r], TODAY)
        self.assertNotIn("POV | Richest", md)


class TestMain(unittest.TestCase):
    def test_missing_ytdlp_exits_2(self):
        with mock.patch.object(ps.shutil, "which", return_value=None), \
             mock.patch.object(ps, "Path", side_effect=lambda p: Path("/nonexistent/yt-dlp")):
            self.assertEqual(ps.main(["q"]), 2)

    def test_writes_report(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "r.md"
            with mock.patch.object(ps.shutil, "which", return_value=sys.executable), \
                 mock.patch.object(ps, "search", return_value=[entry(5)] * 6):
                self.assertEqual(ps.main([Q, "--out", str(out)]), 0)
            self.assertIn("**SATURATED**", out.read_text())

    def test_search_returns_entries(self):
        with mock.patch.object(ps.subprocess, "run",
                               return_value=mock.Mock(stdout='{"entries": [{"title": "t"}]}')) as run:
            self.assertEqual(ps.search("/x/yt-dlp", "q"), [{"title": "t"}])
            self.assertEqual(run.call_args[0][0][:2], ["/x/yt-dlp", "ytsearch40:q"])

    def test_main_uses_ytdlp_on_path(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(ps.shutil, "which", return_value=sys.executable), \
                 mock.patch.object(ps, "search", return_value=[]) as srch:
                ps.main(["q", "--out", str(Path(td) / "r.md")])
            self.assertEqual(srch.call_args[0][0], sys.executable)

    def test_search_failures_are_none(self):
        for out in ("not json", "", "null\n", "[]"):
            with mock.patch.object(ps.subprocess, "run", return_value=mock.Mock(stdout=out)):
                self.assertIsNone(ps.search("yt-dlp", "q"), out)

    def test_search_timeout_is_none(self):
        with mock.patch.object(ps.subprocess, "run",
                               side_effect=ps.subprocess.TimeoutExpired("yt-dlp", 600)):
            self.assertIsNone(ps.search("yt-dlp", "q"))

    def test_search_drops_failed_videos(self):
        out = '{"entries": [null, {"title": "t"}, null]}'
        with mock.patch.object(ps.subprocess, "run", return_value=mock.Mock(stdout=out)):
            self.assertEqual(ps.search("yt-dlp", "q"), [{"title": "t"}])

    def test_search_no_entries_is_empty_not_failed(self):
        with mock.patch.object(ps.subprocess, "run",
                               return_value=mock.Mock(stdout='{"entries": null}')):
            self.assertEqual(ps.search("yt-dlp", "q"), [])

    def test_failed_search_reports_error_and_partial_status(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "r.md"
            with mock.patch.object(ps.shutil, "which", return_value=sys.executable), \
                 mock.patch.object(ps, "search", side_effect=[None, [entry(5)] * 6]):
                self.assertEqual(ps.main(["B22: " + Q, "B33: paycheck cycle", "--out", str(out)]), 0)
            md = out.read_text()
            self.assertIn("status: partial-search-failed", md)
            self.assertIn(f"| B22 | {Q} | **ERROR** |", md)
            self.assertIn("do not read as clear", md)

    def test_report_written_after_each_query(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "r.md"
            def boom_second(_y, _q, calls=[0]):
                calls[0] += 1
                if calls[0] == 2:
                    raise KeyboardInterrupt
                return [entry(5)] * 6
            with mock.patch.object(ps.shutil, "which", return_value=sys.executable), \
                 mock.patch.object(ps, "search", side_effect=boom_second):
                with self.assertRaises(KeyboardInterrupt):
                    ps.main(["B22: " + Q, "B33: paycheck cycle", "--out", str(out)])
            self.assertIn("| B22 |", out.read_text())


if __name__ == "__main__":
    unittest.main()
