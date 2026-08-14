"""price_spark.py と、/price/ /deals/ への spark 組み込みのテスト。

#5120 / #5135 で作った商品ページ用の描画ロジックを price_spark.py に切り出し、
一覧カード用の CARD_GEOM プリセットを足した際の回帰防止。商品ページ側の描画
そのものは test_build_post_price_history*.py が引き続き担保する。
"""
import json
import pathlib
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

THIS_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = THIS_DIR.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from price_spark import (  # noqa: E402
    ARTICLE_GEOM,
    CARD_GEOM,
    build_card_spark,
    build_spark,
)
from build_price_sparks import build_sparks  # noqa: E402


def _rec(days_ago: int, price: int) -> dict:
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    return {"ts": ts, "price": price}


def _hist(*pairs: tuple[int, int]) -> list[tuple[datetime, int]]:
    """(days_ago, price) の並びを load_merged_history と同じ形に変換する。"""
    base = datetime.now(timezone.utc)
    return [(base - timedelta(days=d), p) for d, p in pairs]


def _write_jsonl(root: pathlib.Path, asin: str, records: list[dict]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    with open(root / f"{asin.upper()}.jsonl", "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


class PriceSparkGeometryTests(unittest.TestCase):
    def test_card_geom_has_axes_area_and_now_marker(self):
        """#5167: カード版は「線だけ」ではなく、軸ラベル・面塗り・最新点を持つ。

        初版 (#5143) は軸ラベルを全て落としていたが、カード上では意味の分からない
        線が1本あるだけに見えて読者に何も伝わっていなかった。読める図に必要な
        要素が揃っていることを固定する。
        """
        spark = build_spark([_rec(20, 1000), _rec(10, 1200), _rec(0, 1500)],
                            1000, 1500, CARD_GEOM)
        self.assertEqual(len(spark["y_labels"]), 2, "価格帯の上下が読めること")
        self.assertEqual(len(spark["x_labels"]), 2, "期間の始点と終点が読めること")
        self.assertTrue(spark["area"], "折れ線の下が塗られること (線1本に見せない)")
        self.assertIsNotNone(spark["last_point"], "最新点のマーカーがあること")
        # 全観測点のドットは打たない (小さい図で潰れるため)。凡例も持たない。
        self.assertEqual(spark["dots"], [])
        self.assertIsNone(spark["legend"])
        self.assertEqual(spark["width"], 220)
        self.assertEqual(spark["height"], 72)

    def test_card_geom_uses_short_date_labels(self):
        """カードの x 軸は M/D。小さい図で ISO 日付は読めないし場所も食う。"""
        spark = build_spark([_rec(20, 1000), _rec(10, 1200), _rec(0, 1500)],
                            1000, 1500, CARD_GEOM)
        for label in spark["x_labels"]:
            self.assertRegex(label["text"], r"^\d{1,2}/\d{1,2}$")

    def test_card_geom_y_labels_show_price_range(self):
        spark = build_spark([_rec(20, 1000), _rec(10, 1200), _rec(0, 1500)],
                            1000, 1500, CARD_GEOM)
        texts = [label["text"] for label in spark["y_labels"]]
        self.assertIn("¥1,500", texts)
        self.assertIn("¥1,000", texts)

    def test_article_geom_keeps_iso_dates_and_no_area(self):
        """商品ページ側は従来どおり。目的が違う (Keepa の隣に置く観測の証跡)。"""
        spark = build_spark([_rec(20, 1000), _rec(10, 1200), _rec(0, 1500)],
                            1000, 1500, ARTICLE_GEOM)
        for label in spark["x_labels"]:
            self.assertRegex(label["text"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertEqual(spark["area"], "")
        self.assertIsNone(spark["last_point"])
        self.assertEqual([label["anchor"] for label in spark["y_labels"]], ["end", "end"])

    def test_article_geom_keeps_plot_bounds(self):
        """抽出リファクタで商品ページ側のプロット範囲がズレていないこと。

        最古の観測が x 左端 (52.0)、最新が右端 (296.0)、最高値が y 上端 (8.0)、
        最安値が y 下端 (56.0) に来るのが #5120 のジオメトリ。
        """
        spark = build_spark([_rec(20, 1000), _rec(10, 1200), _rec(0, 1500)],
                            1000, 1500, ARTICLE_GEOM)
        self.assertEqual(spark["width"], 300)
        self.assertEqual(spark["height"], 90)
        self.assertEqual(len(spark["dots"]), 3)
        self.assertEqual(len(spark["y_labels"]), 2)
        self.assertEqual(len(spark["x_labels"]), 2)
        self.assertAlmostEqual(spark["dots"][0]["x"], 52.0, places=1)
        self.assertAlmostEqual(spark["dots"][-1]["x"], 296.0, places=1)
        self.assertAlmostEqual(spark["dots"][-1]["y"], 8.0, places=1)   # 最高値
        self.assertAlmostEqual(spark["dots"][0]["y"], 56.0, places=1)   # 最安値

    def test_article_geom_returns_expected_keys(self):
        spark = build_spark([_rec(20, 1000), _rec(0, 1500)], 1000, 1500, ARTICLE_GEOM)
        self.assertEqual(
            set(spark),
            {"width", "height", "segments", "dots", "y_labels", "x_labels", "legend",
             "area", "last_point"},
        )

    def test_gap_over_14_days_produces_unobserved_segment(self):
        """14日超の観測間隔は破線 (observed=False) セグメントに分かれる。"""
        spark = build_spark([_rec(25, 1000), _rec(20, 1050), _rec(5, 1100), _rec(0, 1200)],
                            1000, 1200, CARD_GEOM)
        flags = [s["observed"] for s in spark["segments"]]
        self.assertIn(False, flags)
        self.assertIn(True, flags)

    def test_no_gap_history_is_all_observed(self):
        spark = build_spark([_rec(12, 1000), _rec(8, 1050), _rec(4, 1100), _rec(0, 1200)],
                            1000, 1200, CARD_GEOM)
        self.assertTrue(all(s["observed"] for s in spark["segments"]))


class BuildCardSparkTests(unittest.TestCase):
    def test_returns_none_when_too_few_points(self):
        self.assertIsNone(build_card_spark(_hist((10, 1000), (0, 1200))))

    def test_returns_none_when_price_never_changed(self):
        """価格の種類が1つだと水平な棒にしかならないので描かない。"""
        self.assertIsNone(build_card_spark(_hist((20, 1000), (10, 1000), (0, 1000))))

    def test_returns_none_for_empty_history(self):
        self.assertIsNone(build_card_spark([]))

    def test_returns_spark_with_price_range(self):
        spark = build_card_spark(_hist((20, 1000), (10, 1200), (0, 900)))
        self.assertIsNotNone(spark)
        self.assertEqual(spark["min_price"], 900)
        self.assertEqual(spark["max_price"], 1200)
        self.assertTrue(spark["segments"])
        self.assertEqual(spark["width"], CARD_GEOM.width)


class BuildPriceSparksTests(unittest.TestCase):
    """#5225: 全ページ共通の ASIN -> チャート辞書 (hugo/data/price_sparks.json)。

    /price/ や /deals/ の一覧 JSON に個別に埋め込むのをやめ、ここを SSOT にした。
    テンプレート側は partial "price_spark_for.html" で ASIN 引きする。
    """

    def _build(self, files: dict[str, list[dict]]) -> dict[str, dict]:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            pw, ph = tmp_path / "price_watch", tmp_path / "price_history"
            for asin, records in files.items():
                _write_jsonl(ph, asin, records)
            return build_sparks(pw, ph)

    def _iso(self, days_ago: int) -> str:
        return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()

    def _varying(self) -> list[dict]:
        return [
            {"ts": self._iso(20), "price": 1000, "source": "amazon"},
            {"ts": self._iso(10), "price": 1200, "source": "amazon"},
            {"ts": self._iso(0), "price": 900, "source": "amazon"},
        ]

    def _flat(self) -> list[dict]:
        return [
            {"ts": self._iso(20), "price": 1000, "source": "amazon"},
            {"ts": self._iso(10), "price": 1000, "source": "amazon"},
            {"ts": self._iso(0), "price": 1000, "source": "amazon"},
        ]

    def test_includes_asin_with_varying_price(self):
        sparks = self._build({"B0AAAAAAAA": self._varying()})
        self.assertIn("B0AAAAAAAA", sparks)
        self.assertTrue(sparks["B0AAAAAAAA"]["segments"])
        self.assertEqual(sparks["B0AAAAAAAA"]["width"], CARD_GEOM.width)

    def test_omits_asin_whose_price_never_changed(self):
        """描画に値しない ASIN はキーごと入れない (テンプレは引けたら描くだけ)。"""
        sparks = self._build({"B0AAAAAAAA": self._flat()})
        self.assertNotIn("B0AAAAAAAA", sparks)

    def test_omits_asin_with_too_few_points(self):
        sparks = self._build({"B0AAAAAAAA": [
            {"ts": self._iso(10), "price": 1000, "source": "amazon"},
            {"ts": self._iso(0), "price": 1200, "source": "amazon"},
        ]})
        self.assertNotIn("B0AAAAAAAA", sparks)

    def test_mixed_set_keeps_only_qualifying(self):
        sparks = self._build({"B0AAAAAAAA": self._varying(), "B0BBBBBBBB": self._flat()})
        self.assertEqual(sorted(sparks), ["B0AAAAAAAA"])

    def test_keys_are_uppercase_asins(self):
        """テンプレート側は upper した ASIN で引くので、キーも大文字で揃える。"""
        sparks = self._build({"b0aaaaaaaa": self._varying()})
        self.assertEqual(sorted(sparks), ["B0AAAAAAAA"])

    def test_no_history_dirs_returns_empty(self):
        """履歴ディレクトリが無くても落ちない (fail-soft)。"""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            self.assertEqual(build_sparks(tmp_path / "nope", tmp_path / "nada"), {})


if __name__ == "__main__":
    unittest.main()
