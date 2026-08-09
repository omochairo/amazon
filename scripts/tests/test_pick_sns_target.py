"""pick_sns_target.py の #1580 スコアリング選定ロジックのテスト。

実行: amazon-clone 直下から `python -m unittest scripts.tests.test_pick_sns_target`
"""
import json
import math
import os
import pathlib
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import pick_sns_target as pst  # noqa: E402


def _article(asin, brand="レゴ", edu_domains=None, best_price=3000, date="2026-06-01"):
    """score_calculator が education 軸を算出できる最小記事 dict。"""
    return {
        "slug": f"{date}-{asin}",
        "date": f"{date}T10:00:00+09:00",
        "product": {
            "asin": asin,
            "name": f"商品 {asin}",
            "brand": brand,
            "edu_domains": edu_domains if edu_domains is not None else ["STEM", "想像"],
            "best_price": best_price,
        },
    }


class NormalizationTest(unittest.TestCase):
    def test_edu_norm_floor_and_ceil(self):
        self.assertEqual(pst._edu_norm(2.0), 0.0)
        self.assertEqual(pst._edu_norm(5.0), 1.0)
        self.assertAlmostEqual(pst._edu_norm(3.5), 0.5)

    def test_edu_norm_clamps_out_of_range(self):
        self.assertEqual(pst._edu_norm(1.0), 0.0)   # 軸下限割れ → 0
        self.assertEqual(pst._edu_norm(9.9), 1.0)   # 軸上限超え → 1
        self.assertEqual(pst._edu_norm(None), 0.0)  # 不明 → 0 (rankable)

    def test_price_norm_monotonic_and_saturating(self):
        self.assertEqual(pst._price_norm(None), 0.0)
        self.assertEqual(pst._price_norm(0), 0.0)
        self.assertEqual(pst._price_norm(pst.PRICE_MIN), 0.0)
        self.assertEqual(pst._price_norm(pst.PRICE_MAX), 1.0)
        self.assertEqual(pst._price_norm(99999), 1.0)  # 上端 saturate
        # 高いほど高得点 (高単価=アフィ報酬大)
        self.assertLess(pst._price_norm(2000), pst._price_norm(8000))

    def test_date_int(self):
        self.assertEqual(pst._date_int("2026-06-04-B0XXXXXXXX.json"), 20260604)
        self.assertEqual(pst._date_int("garbage.json"), 0)


class RankingTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self._tmp.name)
        self._orig_dir = pst.ARTICLES_DIR
        pst.ARTICLES_DIR = self.dir

    def tearDown(self):
        pst.ARTICLES_DIR = self._orig_dir
        self._tmp.cleanup()

    def _write(self, name, article):
        (self.dir / name).write_text(json.dumps(article, ensure_ascii=False), encoding="utf-8")

    def test_higher_edu_and_price_ranks_first(self):
        # 高知育・高価格
        self._write("2026-06-01-B000000001.json",
                    _article("B000000001", edu_domains=["STEM", "言語", "運動", "想像"], best_price=15000))
        # 低知育・低価格
        self._write("2026-06-01-B000000002.json",
                    _article("B000000002", edu_domains=[], best_price=800))
        ranked = pst.rank_candidates(set())
        self.assertEqual(ranked[0].asin, "B000000001")
        self.assertGreater(ranked[0].score, ranked[-1].score)

    def test_sidecar_files_excluded_and_do_not_shadow(self):
        # 本体記事 + 同 ASIN の quality サイドカー (product 無し)
        self._write("2026-06-01-B000000003.json",
                    _article("B000000003", best_price=12000))
        (self.dir / "2026-06-01-B000000003.quality.json").write_text(
            json.dumps({"slug": "x", "total_score": 88, "passed": True}), encoding="utf-8")
        ranked = pst.rank_candidates(set())
        # 候補は本体 1 件のみ。サイドカーが shadow して score 0 にしていない。
        self.assertEqual([c.asin for c in ranked], ["B000000003"])
        self.assertGreater(ranked[0].score, 0.0)

    def test_published_filtered_out(self):
        self._write("2026-06-01-B000000004.json", _article("B000000004"))
        self._write("2026-06-02-B000000005.json", _article("B000000005"))
        ranked = pst.rank_candidates({"B000000004"})
        self.assertEqual([c.asin for c in ranked], ["B000000005"])

    def test_no_hard_cutoff_low_score_still_returned(self):
        # 全件が低知育・低価格でも None にならず最上位を返す (枯渇回避)
        self._write("2026-06-01-B000000006.json",
                    _article("B000000006", edu_domains=[], best_price=300))
        self.assertIsNotNone(pst.pick_next(set()))

    def test_tiebreak_prefers_newer_date(self):
        # 同一スコア (同 brand/domains/price) は date 降順で新しい記事を優先
        self._write("2026-05-01-B000000007.json", _article("B000000007", date="2026-05-01"))
        self._write("2026-06-01-B000000008.json", _article("B000000008", date="2026-06-01"))
        ranked = pst.rank_candidates(set())
        self.assertAlmostEqual(ranked[0].score, ranked[1].score)
        self.assertEqual(ranked[0].asin, "B000000008")


class ChannelGateTest(unittest.TestCase):
    """#4783 — 全チャネル失敗で mark すると SNS 露出が恒久ゼロになる。

    成り立つべき条件:
      1. 1 つも success が無ければ state を一切書き換えず exit 2 (翌日 cron が再試行)。
      2. 1 つでも success があれば従来どおり mark し、失敗チャネルを記録に残す。
      3. --channel 無指定は従来の無条件 mark (後方互換)。
    """

    def test_parse_channel_args(self):
        self.assertEqual(
            pst.parse_channel_args(["x=success", " Threads = Failure "]),
            {"x": "success", "threads": "failure"},
        )

    def test_parse_channel_args_drops_empty_outcome(self):
        # step 自体が skip されると ${{ steps.foo.outcome }} は空文字になる
        self.assertEqual(pst.parse_channel_args(["x=success", "bluesky="]), {"x": "success"})

    def test_parse_channel_args_rejects_malformed(self):
        with self.assertRaises(ValueError):
            pst.parse_channel_args(["x"])
        with self.assertRaises(ValueError):
            pst.parse_channel_args(["=success"])

    def test_any_delivered(self):
        self.assertTrue(pst.any_delivered({"x": "failure", "threads": "success"}))
        self.assertFalse(pst.any_delivered({"x": "failure", "threads": "skipped"}))
        self.assertFalse(pst.any_delivered({}))

    def test_channel_results_trimmed_to_published(self):
        state = {"published": ["A2", "A3"], "channel_results": {"A1": {"x": "success"}}}
        pst.record_channel_results(state, "A3", {"x": "success"})
        # published から溢れた A1 は捨て、A3 だけが残る
        self.assertEqual(state["channel_results"], {"A3": {"x": "success"}})


class MarkCliTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_file = pathlib.Path(self._tmp.name) / "sns_published.json"
        self._orig = pst.STATE_FILE
        pst.STATE_FILE = self.state_file
        self.state_file.write_text(
            json.dumps({"published": ["B0OLD00001"], "updated": None}), encoding="utf-8")
        self._orig_argv = sys.argv

    def tearDown(self):
        pst.STATE_FILE = self._orig
        sys.argv = self._orig_argv
        self._tmp.cleanup()

    def _run(self, *argv):
        sys.argv = ["pick_sns_target.py", *argv]
        return pst.main()

    def _state(self):
        return json.loads(self.state_file.read_text(encoding="utf-8"))

    def test_all_channels_failed_does_not_mark(self):
        before = self.state_file.read_text(encoding="utf-8")
        rc = self._run("--mark", "B0NEW00001",
                       "--channel", "x=failure",
                       "--channel", "threads=failure",
                       "--channel", "bluesky=failure")
        self.assertEqual(rc, 2)
        # state はバイト単位で不変 = 翌日の cron が同じ ASIN を再選定できる
        self.assertEqual(self.state_file.read_text(encoding="utf-8"), before)

    def test_all_channels_skipped_does_not_mark(self):
        rc = self._run("--mark", "B0NEW00001",
                       "--channel", "x=skipped", "--channel", "threads=cancelled")
        self.assertEqual(rc, 2)
        self.assertNotIn("B0NEW00001", self._state()["published"])

    def test_partial_success_marks_and_records_failures(self):
        rc = self._run("--mark", "B0NEW00001",
                       "--channel", "x=failure",
                       "--channel", "threads=success",
                       "--channel", "bluesky=failure")
        self.assertEqual(rc, 0)
        state = self._state()
        self.assertIn("B0NEW00001", state["published"])
        self.assertEqual(
            state["channel_results"]["B0NEW00001"],
            {"x": "failure", "threads": "success", "bluesky": "failure"},
        )

    def test_no_channel_args_keeps_legacy_behaviour(self):
        rc = self._run("--mark", "B0NEW00002")
        self.assertEqual(rc, 0)
        state = self._state()
        self.assertIn("B0NEW00002", state["published"])
        self.assertNotIn("channel_results", state)

    def test_already_published_is_still_a_no_op(self):
        rc = self._run("--mark", "B0OLD00001", "--channel", "x=success")
        self.assertEqual(rc, 0)
        self.assertEqual(self._state()["published"], ["B0OLD00001"])

    def test_limit_trim_also_trims_channel_results(self):
        self.state_file.write_text(json.dumps({
            "published": ["A1", "A2"],
            "channel_results": {"A1": {"x": "success"}, "A2": {"x": "success"}},
        }), encoding="utf-8")
        rc = self._run("--mark", "A3", "--channel", "x=success", "--limit", "2")
        self.assertEqual(rc, 0)
        state = self._state()
        self.assertEqual(state["published"], ["A2", "A3"])
        self.assertEqual(sorted(state["channel_results"]), ["A2", "A3"])


if __name__ == "__main__":
    unittest.main()
