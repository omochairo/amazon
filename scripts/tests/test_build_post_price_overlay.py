"""Unit tests for #4007 — build_post の Amazon 価格日次上書き。

``_apply_price_overlay`` は記事 JSON に凍結している ``product.prices.amazon.price``
を price_watch (日次) → per_asin (週次) の観測で上書きし、乖離が大きいときは
``data["price_body_stale"]`` を立てる (本文中の価格リテラルとの矛盾を注記させる)。
``_attach_price_freshness`` は上書きに使った観測の時刻を表示日付に採用する
(「新鮮な日付 + 凍結価格」という矛盾表示を避ける)。
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

THIS_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = THIS_DIR.parent.parent  # amazon-clone/
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import price_overlay  # type: ignore[import-not-found]  # noqa: E402
from build_post import (  # type: ignore[import-not-found]  # noqa: E402
    _apply_amazon_unpriced,
    _apply_price_overlay,
    _attach_price_freshness,
    _recompute_best_price,
)


def _article(asin: str = "B00000030", price: int = 2518) -> dict:
    return {
        "slug": f"2026-06-09-{asin.lower()}",
        "product": {
            "asin": asin,
            "name": "テスト商品",
            "best_price": price,
            "best_platform": "Amazon",
            "prices": {
                "amazon": {"price": price, "url": f"https://www.amazon.co.jp/dp/{asin}/"},
            },
        },
    }


def _obs(asin: str = "B00000030", price: int = 7927, **kw) -> price_overlay.PriceObservation:
    return price_overlay.PriceObservation(
        asin=asin,
        price=price,
        savings_percentage=kw.get("savings_percentage", 26),
        availability=kw.get("availability", "在庫あり。"),
        observed_at=kw.get("observed_at", "2026-07-25T21:11:27+00:00"),
        source=kw.get("source", "price_watch"),
    )


def _write_per_asin(root: pathlib.Path, asin: str, *, price: int, fetched_at: str) -> None:
    d = root / asin
    d.mkdir(parents=True, exist_ok=True)
    (d / "amazon.json").write_text(
        json.dumps({"asin": asin, "fetched_at": fetched_at,
                    "item": {"price": price, "savings_percentage": 0}}),
        encoding="utf-8",
    )


class ApplyPriceOverlayTest(unittest.TestCase):
    def test_frozen_price_is_overwritten_from_watch_index(self):
        with tempfile.TemporaryDirectory() as td:
            data = _article()
            src = _apply_price_overlay(
                data, {"B00000030": _obs()}, pathlib.Path(td)
            )
            amazon = data["product"]["prices"]["amazon"]

            self.assertEqual(src, "price_watch")
            self.assertEqual(amazon["price"], 7927)
            self.assertEqual(amazon["savings_percentage"], 26)
            self.assertEqual(amazon["price_source"], "price_watch")
            self.assertEqual(amazon["price_observed_at"], "2026-07-25T21:11:27+00:00")

    def test_best_price_follows_after_recompute(self):
        """最安バッジ・front matter・JSON-LD が同じ観測を出すことの担保。"""
        with tempfile.TemporaryDirectory() as td:
            data = _article()
            data["product"]["prices"]["rakuten"] = {"price": 3000, "url": "https://r.invalid/"}
            _apply_price_overlay(data, {"B00000030": _obs()}, pathlib.Path(td))
            _recompute_best_price(data["product"])

            self.assertEqual(data["product"]["best_price"], 3000)
            self.assertEqual(data["product"]["best_platform"], "楽天市場")

    def test_falls_back_to_per_asin_when_watch_index_empty(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            # _apply_price_overlay は now を取らない (実行時刻基準の鮮度ガード) ため、
            # fetched_at は実行時刻からの相対で書く。固定日付だと 21 日後に腐る。
            fresh = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
            _write_per_asin(root, "B00000030", price=3149, fetched_at=fresh)
            data = _article()

            src = _apply_price_overlay(data, {}, root)
            self.assertEqual(src, "per_asin")
            self.assertEqual(data["product"]["prices"]["amazon"]["price"], 3149)

    def test_per_asin_beyond_stale_guard_is_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
            _write_per_asin(root, "B00000030", price=3149, fetched_at=old)
            data = _article()

            self.assertIsNone(_apply_price_overlay(data, {}, root))
            self.assertEqual(data["product"]["prices"]["amazon"]["price"], 2518)

    def test_no_observation_leaves_article_untouched(self):
        with tempfile.TemporaryDirectory() as td:
            data = _article()
            before = json.dumps(data, ensure_ascii=False, sort_keys=True)
            src = _apply_price_overlay(data, {}, pathlib.Path(td))

            self.assertIsNone(src)
            self.assertEqual(json.dumps(data, ensure_ascii=False, sort_keys=True), before)

    def test_large_divergence_flags_body_stale(self):
        with tempfile.TemporaryDirectory() as td:
            data = _article(price=2518)
            _apply_price_overlay(data, {"B00000030": _obs(price=7927)}, pathlib.Path(td))
            self.assertTrue(data.get("price_body_stale"))

    def test_small_divergence_does_not_flag_body_stale(self):
        with tempfile.TemporaryDirectory() as td:
            data = _article(price=2518)
            # +4% は本文の価格帯記述と矛盾しないので注記しない。
            _apply_price_overlay(data, {"B00000030": _obs(price=2618)}, pathlib.Path(td))
            self.assertFalse(data.get("price_body_stale"))

    def test_missing_asin_is_noop(self):
        with tempfile.TemporaryDirectory() as td:
            data = {"product": {"name": "asin なし"}}
            self.assertIsNone(_apply_price_overlay(data, {}, pathlib.Path(td)))


class PriceFreshnessSourceTest(unittest.TestCase):
    def test_price_checked_at_follows_the_observation_actually_used(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            # per_asin の fetched_at (7/20) より overlay の観測 (7/25) を優先する。
            _write_per_asin(root, "B00000030", price=3149,
                            fetched_at="2026-07-20T10:00:00+00:00")
            data = _article()
            _apply_price_overlay(data, {"B00000030": _obs()}, root)
            _attach_price_freshness(data, root)

            self.assertEqual(data["price_checked_at"], "2026-07-25")

    def test_no_overlay_means_no_date_at_all(self):
        """overlay が無いときに snapshot の fetched_at を代用しない (2026-08-09)。

        旧実装はここで per_asin の fetched_at を表示日付にしていた。だが overlay が
        観測を採用できなかったということは「その取得で価格が得られなかった」であり、
        表示される価格は記事作成時の凍結値のまま。新しい日付を貼ると、古い価格が
        当日検証済みに見える。実データ 1900 件中 87 件が該当していた。
        """
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            _write_per_asin(root, "B00000030", price=0,
                            fetched_at="2026-08-09T10:00:00+00:00")
            data = _article()
            _attach_price_freshness(data, root)

            self.assertNotIn("price_checked_at", data)


class AmazonUnpricedTest(unittest.TestCase):
    """最新 snapshot が価格を返さない出品の凍結価格を落とす (2026-08-09)。"""

    def test_out_of_stock_snapshot_clears_frozen_price(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            _write_per_asin(root, "B00000030", price=0,
                            fetched_at="2026-08-09T10:00:00+00:00")
            data = _article(price=12685)
            self.assertTrue(_apply_amazon_unpriced(data, root))

            self.assertEqual(data["product"]["prices"]["amazon"]["price"], 0)
            self.assertEqual(data["product"]["best_price"], 0)

    def test_other_platform_becomes_best_price(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            _write_per_asin(root, "B00000030", price=0,
                            fetched_at="2026-08-09T10:00:00+00:00")
            data = _article(price=12685)
            data["product"]["prices"]["rakuten"] = {"price": 10810, "url": "https://r/"}
            _apply_amazon_unpriced(data, root)

            self.assertEqual(data["product"]["best_price"], 10810)

    def test_url_and_availability_survive(self):
        """在庫切れバッジと「再入荷を確認」CTA は今回の snapshot 由来の事実なので残す。"""
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            _write_per_asin(root, "B00000030", price=0,
                            fetched_at="2026-08-09T10:00:00+00:00")
            data = _article(price=12685)
            data["product"]["prices"]["amazon"]["availability"] = "現在在庫切れです。"
            _apply_amazon_unpriced(data, root)

            amazon = data["product"]["prices"]["amazon"]
            self.assertEqual(amazon["availability"], "現在在庫切れです。")
            self.assertTrue(amazon["url"])

    def test_priced_snapshot_is_left_alone(self):
        """価格が取れている (stale guard で弾かれただけ) 場合は据え置く。"""
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            _write_per_asin(root, "B00000030", price=3149,
                            fetched_at="2026-06-01T10:00:00+00:00")
            data = _article(price=12685)
            self.assertFalse(_apply_amazon_unpriced(data, root))
            self.assertEqual(data["product"]["prices"]["amazon"]["price"], 12685)

    def test_overlay_applied_is_left_alone(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            _write_per_asin(root, "B00000030", price=0,
                            fetched_at="2026-08-09T10:00:00+00:00")
            data = _article()
            _apply_price_overlay(data, {"B00000030": _obs()}, root)
            self.assertFalse(_apply_amazon_unpriced(data, root))
            self.assertEqual(data["product"]["prices"]["amazon"]["price"], 7927)

    def test_missing_snapshot_is_noop(self):
        """snapshot が無いなら現況を知らない。凍結価格を消す根拠がない。"""
        with tempfile.TemporaryDirectory() as td:
            data = _article(price=12685)
            self.assertFalse(_apply_amazon_unpriced(data, pathlib.Path(td)))
            self.assertEqual(data["product"]["prices"]["amazon"]["price"], 12685)

    def test_discontinued_is_left_to_its_own_handler(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            _write_per_asin(root, "B00000030", price=0,
                            fetched_at="2026-08-09T10:00:00+00:00")
            data = _article(price=12685)
            data["product"]["prices"]["amazon"]["discontinued"] = True
            self.assertFalse(_apply_amazon_unpriced(data, root))


if __name__ == "__main__":
    unittest.main()
