"""Unit tests for build_feature_lists.py (Issue #590 PR-1).

Coverage:
1. load_articles  - parses article json, skips .quality.json / .enrichment.json / .seo.json,
                    skips records missing asin / ivs_score / best_price.
2. attach_amazon_meta - fills savings_percentage + fetched_at from per_asin/<ASIN>/amazon.json,
                        silently skips missing files.
3. build_cospa    - low_ivs / price band / sort order / top_n / dedupe by asin.
4. build_deals    - stale guard / missing savings data / sort by savings then ivs / top_n.
5. serialize_*    - url_internal is lowercased; cospa includes score_cospa; deals includes
                    savings_percentage + fetched_at.
6. run (end-to-end on a tmp fixture) - writes both hugo outputs + manifest.
7. deleted article regen - re-running after an article json is removed drops it
   from the next generation (#3364: /products/{asin}/ has no stale-alias 404,
   unlike legacy /posts/{slug}/ whose Hugo alias disappears with the article).
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.dirname(THIS_DIR)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import build_feature_lists as bfl  # noqa: E402
import price_overlay  # noqa: E402


# build_feature_lists.load_articles() は brand_normalizer + score_calculator で
# 知育スコアを再計算する (session 58: list / 詳細ページの score 乖離解消)。
# 本テストは集計ロジック単体を検証するのが目的なので、テスト中は recalc を
# 「JSON 上の ivs_score / ivs_detail.total_100 をそのまま返す」スタブに差し替える。
_ZERO_BREAKDOWN = {
    "brand_tier": 0, "safety_cert": 0, "age_fit": 0, "edu_value": 0,
    "media_exposure": 0, "multi_market": 0, "price_value": 0,
}


class _StubScoreResult:
    def __init__(self, total_100: int, ivs_score: float) -> None:
        self.total_100 = total_100
        self.ivs_score = ivs_score
        # compute_ivs_axes() needs a full breakdown; zeros make every axis 2.0
        # which is fine for tests that only care about ranking / serialization.
        self.breakdown = dict(_ZERO_BREAKDOWN)


def _stub_calculate_score(article, brand, asin=None):
    prod = article.get("product") or {}
    det = prod.get("ivs_detail") or {}
    return _StubScoreResult(
        total_100=int(det.get("total_100") or 0),
        ivs_score=float(prod.get("ivs_score") or 0.0),
    )


# モジュール import 直後に差し替えておく (各 TestCase は import 済みの bfl を共有)。
bfl.calculate_score = _stub_calculate_score


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _make_article(
    asin: str,
    *,
    slug: str | None = None,
    name: str = "テスト商品",
    ivs_score: float = 4.5,
    ivs_100: int = 90,
    best_price: int = 2000,
    best_platform: str = "Amazon",
) -> dict:
    return {
        "slug": slug or f"2026-05-13-{asin.lower()}",
        "title": f"{name} 徹底レビュー",
        "product": {
            "asin": asin,
            "name": name,
            "image": f"https://example.invalid/{asin}.jpg",
            "ivs_score": ivs_score,
            "ivs_detail": {"total_100": ivs_100},
            "best_price": best_price,
            "best_platform": best_platform,
            "prices": {
                "amazon": {
                    "price": best_price,
                    "url": f"https://www.amazon.co.jp/dp/{asin}/?tag=test-22",
                }
            },
        },
    }


def _write_article(articles_dir: pathlib.Path, payload: dict) -> pathlib.Path:
    path = articles_dir / f"{payload['slug']}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _write_per_asin(
    per_asin_dir: pathlib.Path,
    asin: str,
    *,
    savings: int | None,
    fetched_at: str | None,
) -> None:
    dir_ = per_asin_dir / asin
    dir_.mkdir(parents=True, exist_ok=True)
    body: dict = {
        "asin": asin,
        "fetched_at": fetched_at,
        "item": {"asin": asin, "savings_percentage": savings},
    }
    (dir_ / "amazon.json").write_text(
        json.dumps(body, ensure_ascii=False), encoding="utf-8"
    )


def _write_matched(raw_root: pathlib.Path, platform: str, items: list[dict]) -> None:
    """#4007 follow-up 1: data/raw/{rakuten,yahoo}_matched.json 相当の fixture。"""
    raw_root.mkdir(parents=True, exist_ok=True)
    (raw_root / f"{platform}_matched.json").write_text(
        json.dumps({"items": items}, ensure_ascii=False), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# load_articles
# ---------------------------------------------------------------------------

class LoadArticlesTest(unittest.TestCase):
    def test_loads_only_article_json(self):
        with tempfile.TemporaryDirectory() as td:
            d = pathlib.Path(td)
            _write_article(d, _make_article("B00000001"))
            # sidecar files that should be ignored:
            (d / "2026-05-13-b00000001.quality.json").write_text("{}", encoding="utf-8")
            (d / "2026-05-13-b00000001.enrichment.json").write_text("{}", encoding="utf-8")
            (d / "2026-05-13-b00000001.seo.json").write_text("{}", encoding="utf-8")

            records = bfl.load_articles(d)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].asin, "B00000001")

    def test_skips_missing_required_fields(self):
        with tempfile.TemporaryDirectory() as td:
            d = pathlib.Path(td)
            # missing best_price
            bad = _make_article("B00000002")
            bad["product"].pop("best_price")
            _write_article(d, bad)
            # missing asin
            bad2 = _make_article("B00000003")
            bad2["product"].pop("asin")
            _write_article(d, bad2)
            # good
            _write_article(d, _make_article("B00000004"))

            records = bfl.load_articles(d)
            self.assertEqual([r.asin for r in records], ["B00000004"])

    def test_missing_ivs_score_is_included_in_pool(self):
        """#4007 follow-up 4: 生 JSON の ivs_score presence check は gate から撤去済み。

        ivs_score は score_calculator で再計算して捨てるだけの値 (Jules 推定で
        stale) なので、欠落していてもプールから除外されない。asin / best_price
        が無い記事は従来どおり除外されることは test_skips_missing_required_fields
        で確認済み。
        """
        with tempfile.TemporaryDirectory() as td:
            d = pathlib.Path(td)
            no_ivs = _make_article("B00000006")
            no_ivs["product"].pop("ivs_score")
            no_ivs["product"].pop("ivs_detail")
            _write_article(d, no_ivs)

            records = bfl.load_articles(d)
            self.assertEqual([r.asin for r in records], ["B00000006"])

    def test_unreadable_json_is_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            d = pathlib.Path(td)
            (d / "broken.json").write_text("{not json", encoding="utf-8")
            _write_article(d, _make_article("B00000005"))
            records = bfl.load_articles(d)
            self.assertEqual([r.asin for r in records], ["B00000005"])

    def test_missing_dir_returns_empty(self):
        records = bfl.load_articles(pathlib.Path("/does/not/exist/articles"))
        self.assertEqual(records, [])

    def test_calculate_score_exception_is_skipped_not_fatal(self):
        """#4007 follow-up 4: ivs_score gate 撤去でプール対象が全記事に広がるため、
        calculate_score が想定外の例外を投げても他の記事の集計を止めない (fail-soft)。
        """
        with tempfile.TemporaryDirectory() as td:
            d = pathlib.Path(td)
            _write_article(d, _make_article("B00000007"))
            _write_article(d, _make_article("B00000008"))

            def _raising_calculate_score(article, brand, asin=None):
                if asin == "B00000007":
                    raise ValueError("boom")
                return _stub_calculate_score(article, brand, asin=asin)

            original = bfl.calculate_score
            bfl.calculate_score = _raising_calculate_score
            try:
                records = bfl.load_articles(d)
            finally:
                bfl.calculate_score = original

            self.assertEqual([r.asin for r in records], ["B00000008"])


# ---------------------------------------------------------------------------
# attach_amazon_meta
# ---------------------------------------------------------------------------

class AttachAmazonMetaTest(unittest.TestCase):
    def test_fills_savings_and_fetched_at(self):
        with tempfile.TemporaryDirectory() as td:
            d = pathlib.Path(td)
            arts = d / "articles"
            arts.mkdir()
            per = d / "per_asin"
            _write_article(arts, _make_article("B00000010"))
            _write_per_asin(
                per, "B00000010", savings=30, fetched_at="2026-05-24T10:00:00+00:00"
            )

            records = bfl.load_articles(arts)
            bfl.attach_amazon_meta(records, per)
            self.assertEqual(records[0].savings_percentage, 30)
            self.assertEqual(records[0].fetched_at, "2026-05-24T10:00:00+00:00")

    def test_missing_per_asin_is_silent(self):
        with tempfile.TemporaryDirectory() as td:
            d = pathlib.Path(td)
            arts = d / "articles"
            arts.mkdir()
            per = d / "per_asin"  # never created
            _write_article(arts, _make_article("B00000011"))
            records = bfl.load_articles(arts)
            bfl.attach_amazon_meta(records, per)
            self.assertIsNone(records[0].savings_percentage)
            self.assertIsNone(records[0].fetched_at)


# ---------------------------------------------------------------------------
# overlay_current_prices (#4007 価格の日次化)
# ---------------------------------------------------------------------------

class OverlayCurrentPricesTest(unittest.TestCase):
    """記事 JSON の凍結価格を日次観測で上書きし best_price を再計算する。"""

    def _records(self, articles_dir: pathlib.Path, payload: dict):
        _write_article(articles_dir, payload)
        return bfl.load_articles(articles_dir)

    def test_amazon_price_is_refreshed_and_best_price_recomputed(self):
        with tempfile.TemporaryDirectory() as td:
            d = pathlib.Path(td)
            arts = d / "articles"
            arts.mkdir()
            art = _make_article("B00000020", best_price=2518)
            records = self._records(arts, art)
            self.assertEqual(records[0].best_price, 2518)

            obs = bfl.price_overlay.PriceObservation(
                asin="B00000020", price=7927, savings_percentage=26,
                availability="在庫あり。", observed_at="2026-07-25T21:11:27Z",
                source="price_watch",
            )
            stats = bfl.overlay_current_prices(
                records, d / "per_asin", watch_index={"B00000020": obs}
            )

            self.assertEqual(stats["price_watch"], 1)
            self.assertEqual(records[0].price_amazon, 7927)
            self.assertEqual(records[0].best_price, 7927)
            self.assertEqual(records[0].best_platform, "Amazon")
            self.assertEqual(records[0].savings_percentage, 26)
            self.assertEqual(records[0].fetched_at, "2026-07-25T21:11:27Z")

    def test_best_platform_flips_when_rakuten_becomes_cheapest(self):
        """Amazon 値上がりで楽天が最安になったら best_platform も反転する。

        #4007 実測で 109 件 (7.3%) が反転し、うち 83 件は Amazon を誤って最安表示。
        """
        with tempfile.TemporaryDirectory() as td:
            d = pathlib.Path(td)
            arts = d / "articles"
            arts.mkdir()
            art = _make_article("B00000021", best_price=2518)
            art["product"]["prices"]["rakuten"] = {"price": 3000, "url": "https://r.invalid/"}
            records = self._records(arts, art)
            self.assertEqual(records[0].best_platform, "Amazon")

            obs = bfl.price_overlay.PriceObservation(
                asin="B00000021", price=7927, savings_percentage=None,
                availability=None, observed_at="2026-07-25T21:11:27Z",
                source="price_watch",
            )
            bfl.overlay_current_prices(records, d / "per_asin",
                                       watch_index={"B00000021": obs})

            self.assertEqual(records[0].best_price, 3000)
            self.assertEqual(records[0].best_platform, "楽天市場")

    def test_no_observation_keeps_article_json_values(self):
        with tempfile.TemporaryDirectory() as td:
            d = pathlib.Path(td)
            arts = d / "articles"
            arts.mkdir()
            records = self._records(arts, _make_article("B00000022", best_price=2518))
            stats = bfl.overlay_current_prices(records, d / "per_asin", watch_index={})

            self.assertEqual(stats["none"], 1)
            self.assertEqual(records[0].best_price, 2518)
            self.assertEqual(records[0].best_platform, "Amazon")


# ---------------------------------------------------------------------------
# overlay_current_prices market overlay (#4007 follow-up 1: raw_root)
# ---------------------------------------------------------------------------

class MarketPriceOverlayTest(unittest.TestCase):
    """楽天/Yahoo を matched JSON (raw_root) で更新する経路。"""

    def _records(self, articles_dir: pathlib.Path, payload: dict):
        _write_article(articles_dir, payload)
        return bfl.load_articles(articles_dir)

    def test_raw_root_none_leaves_rakuten_yahoo_unchanged(self):
        """raw_root 未指定 (既定) では従来どおり記事 JSON の値のまま変化しない。"""
        with tempfile.TemporaryDirectory() as td:
            d = pathlib.Path(td)
            arts = d / "articles"
            arts.mkdir()
            art = _make_article("B0RAW0001", best_price=2000)
            art["product"]["prices"]["rakuten"] = {"price": 9999, "url": "https://r.invalid/"}
            records = self._records(arts, art)

            stats = bfl.overlay_current_prices(records, d / "per_asin", watch_index={})

            self.assertEqual(records[0].price_rakuten, 9999)
            self.assertEqual(records[0].best_price, 2000)  # amazon のまま
            # raw_root=None: market overlay 自体が走らないので更新件数は 0 のまま
            self.assertEqual(stats["market_rakuten"], 0)
            self.assertEqual(stats["market_extreme_dropped"], 0)

    def test_raw_root_updates_stale_rakuten_price_and_best_price(self):
        """記事 JSON の凍結楽天価格が matched JSON の新値で更新され、best_price/platform も反映する。"""
        with tempfile.TemporaryDirectory() as td:
            d = pathlib.Path(td)
            arts = d / "articles"
            arts.mkdir()
            raw_root = d / "raw"
            art = _make_article("B0RAW0002", best_price=3000)  # amazon=3000 が最安
            # 記事生成時点で凍結された楽天旧価格 (今はもっと安くなっている想定)
            art["product"]["prices"]["rakuten"] = {"price": 3500, "url": "https://r.invalid/"}
            records = self._records(arts, art)
            self.assertEqual(records[0].price_rakuten, 3500)

            _write_matched(raw_root, "rakuten", [
                {"asin": "B0RAW0002", "price": 1980, "title": "テスト商品 新価格",
                 "search_keyword": "", "url": "https://r.invalid/new"},
            ])

            stats = bfl.overlay_current_prices(
                records, d / "per_asin", watch_index={}, raw_root=raw_root,
            )

            self.assertEqual(records[0].price_rakuten, 1980)
            self.assertEqual(records[0].best_price, 1980)
            self.assertEqual(records[0].best_platform, "楽天市場")
            self.assertEqual(stats["market_rakuten"], 1)

    def test_raw_root_drops_extreme_outlier_existing_price(self):
        """matched が無く、既存の楽天価格が Amazon の 3.0x 超の extreme outlier なら破棄し
        best_price が Amazon (最安) のままになる。"""
        with tempfile.TemporaryDirectory() as td:
            d = pathlib.Path(td)
            arts = d / "articles"
            arts.mkdir()
            raw_root = d / "raw"  # rakuten_matched.json を置かない = 空 index
            art = _make_article("B0RAW0003", best_price=1579)
            art["product"]["prices"]["yahoo"] = {"price": 14395, "url": "https://y.invalid/"}
            records = self._records(arts, art)
            self.assertEqual(records[0].price_yahoo, 14395)

            stats = bfl.overlay_current_prices(
                records, d / "per_asin", watch_index={}, raw_root=raw_root,
            )

            self.assertEqual(records[0].price_yahoo, 0)
            self.assertEqual(records[0].best_price, 1579)
            self.assertEqual(records[0].best_platform, "Amazon")
            self.assertEqual(stats["market_extreme_dropped"], 1)

    def test_raw_root_matched_failing_gate_keeps_existing_rakuten(self):
        """matched はあるが quality gate 落ち (price band 外) → 既存の楽天価格を維持する。"""
        with tempfile.TemporaryDirectory() as td:
            d = pathlib.Path(td)
            arts = d / "articles"
            arts.mkdir()
            raw_root = d / "raw"
            art = _make_article("B0RAW0004", best_price=2302)
            art["product"]["prices"]["rakuten"] = {"price": 2200, "url": "https://r.invalid/"}
            records = self._records(arts, art)

            # amazon=2302 の band 下限は 920.8。399 は band 外で gate 落ち。
            _write_matched(raw_root, "rakuten", [
                {"asin": "B0RAW0004", "price": 399,
                 "title": "ハズブロ ナーフ エリート 2.0 コマンダー",
                 "search_keyword": "ハズブロ ナーフ コマンダー"},
            ])

            stats = bfl.overlay_current_prices(
                records, d / "per_asin", watch_index={}, raw_root=raw_root,
            )

            self.assertEqual(records[0].price_rakuten, 2200)  # 既存値を維持
            self.assertEqual(stats["market_rakuten"], 0)
            self.assertEqual(stats["market_extreme_dropped"], 0)  # extreme ではなく単なる gate 落ち


# ---------------------------------------------------------------------------
# build_cospa
# ---------------------------------------------------------------------------

class BuildCospaTest(unittest.TestCase):
    def test_drops_low_ivs(self):
        recs = [
            bfl.ArticleRecord(
                asin="A1", slug="a1", name="lo", image=None,
                ivs_score=3.9, ivs_100=78, best_price=2000,
                best_platform="Amazon", amazon_url=None,
            ),
            bfl.ArticleRecord(
                asin="A2", slug="a2", name="ok", image=None,
                ivs_score=4.0, ivs_100=80, best_price=2000,
                best_platform="Amazon", amazon_url=None,
            ),
        ]
        items, drops = bfl.build_cospa(recs)
        self.assertEqual([r.asin for r in items], ["A2"])
        self.assertEqual(drops["low_ivs"], 1)

    def test_price_band(self):
        def rec(asin, price):
            return bfl.ArticleRecord(
                asin=asin, slug=asin.lower(), name="x", image=None,
                ivs_score=4.5, ivs_100=90, best_price=price,
                best_platform="Amazon", amazon_url=None,
            )

        items, drops = bfl.build_cospa(
            [rec("A1", 400), rec("A2", 500), rec("A3", 5000), rec("A4", 5001)],
            price_min=500, price_max=5000,
        )
        self.assertEqual({r.asin for r in items}, {"A2", "A3"})
        self.assertEqual(drops["price_out_of_band"], 2)

    def test_sort_order_by_cospa_score(self):
        # Higher IVS / cheaper price ranks first.
        recs = [
            bfl.ArticleRecord(
                asin="HI", slug="hi", name="hi", image=None,
                ivs_score=5.0, ivs_100=100, best_price=800,
                best_platform="Amazon", amazon_url=None,
            ),
            bfl.ArticleRecord(
                asin="MID", slug="mid", name="mid", image=None,
                ivs_score=4.5, ivs_100=90, best_price=2000,
                best_platform="Amazon", amazon_url=None,
            ),
            bfl.ArticleRecord(
                asin="LO", slug="lo", name="lo", image=None,
                ivs_score=4.0, ivs_100=80, best_price=4500,
                best_platform="Amazon", amazon_url=None,
            ),
        ]
        items, _ = bfl.build_cospa(recs)
        self.assertEqual([r.asin for r in items], ["HI", "MID", "LO"])

    def test_top_n_truncates(self):
        recs = [
            bfl.ArticleRecord(
                asin=f"A{i}", slug=f"a{i}", name="x", image=None,
                ivs_score=4.5, ivs_100=90 - i, best_price=2000,
                best_platform="Amazon", amazon_url=None,
            )
            for i in range(10)
        ]
        items, _ = bfl.build_cospa(recs, top_n=3)
        self.assertEqual(len(items), 3)

    def test_sort_order_by_ivs(self):
        # sort_key="ivs": 帯内 UI 用 — ivs_100 降順 → 価格 昇順。
        # cospa score 順だと「IVS 88 で ¥3,593」より「IVS 87 で ¥3,043」が上位に
        # 来るが、UI 文言「知育スコアの高い順」と矛盾するので別ソートを用意する。
        recs = [
            bfl.ArticleRecord(
                asin="MID_CHEAP", slug="a", name="a", image=None,
                ivs_score=4.5, ivs_100=87, best_price=3043,
                best_platform="Amazon", amazon_url=None,
            ),
            bfl.ArticleRecord(
                asin="HI_PRICEY", slug="b", name="b", image=None,
                ivs_score=4.6, ivs_100=88, best_price=3593,
                best_platform="Amazon", amazon_url=None,
            ),
            bfl.ArticleRecord(
                asin="LO_CHEAP", slug="c", name="c", image=None,
                ivs_score=4.4, ivs_100=86, best_price=3145,
                best_platform="Amazon", amazon_url=None,
            ),
            bfl.ArticleRecord(
                asin="LO_CHEAPER", slug="d", name="d", image=None,
                ivs_score=4.4, ivs_100=86, best_price=3100,
                best_platform="Amazon", amazon_url=None,
            ),
        ]
        items, _ = bfl.build_cospa(
            recs, price_min=3000, price_max=5000, sort_key="ivs",
        )
        # 88 が先頭、87 が 2 番手、IVS 同点 86 は安い方が先 (¥3,100 < ¥3,145)
        self.assertEqual(
            [r.asin for r in items],
            ["HI_PRICEY", "MID_CHEAP", "LO_CHEAPER", "LO_CHEAP"],
        )

    def test_dedupe_by_asin_keeps_higher_ivs(self):
        recs = [
            bfl.ArticleRecord(
                asin="DUP", slug="old", name="old", image=None,
                ivs_score=4.0, ivs_100=80, best_price=2000,
                best_platform="Amazon", amazon_url=None,
            ),
            bfl.ArticleRecord(
                asin="DUP", slug="new", name="new", image=None,
                ivs_score=4.8, ivs_100=96, best_price=2000,
                best_platform="Amazon", amazon_url=None,
            ),
        ]
        items, _ = bfl.build_cospa(recs)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].slug, "new")


# ---------------------------------------------------------------------------
# build_deals
# ---------------------------------------------------------------------------

class BuildDealsTest(unittest.TestCase):
    def _rec(self, asin, savings, fetched_at, *, ivs_score=4.5, ivs_100=90,
             availability=None):
        return bfl.ArticleRecord(
            asin=asin, slug=asin.lower(), name="x", image=None,
            ivs_score=ivs_score, ivs_100=ivs_100, best_price=2000,
            best_platform="Amazon", amazon_url=None,
            savings_percentage=savings, fetched_at=fetched_at,
            availability=availability,
        )

    def test_drops_low_ivs(self):
        now = datetime(2026, 5, 25, tzinfo=timezone.utc)
        fresh = (now - timedelta(days=1)).isoformat()
        items, drops = bfl.build_deals(
            [self._rec("A1", 40, fresh, ivs_score=3.9)],
            now=now,
        )
        self.assertEqual(items, [])
        self.assertEqual(drops["low_ivs"], 1)

    def test_drops_missing_savings_data(self):
        now = datetime(2026, 5, 25, tzinfo=timezone.utc)
        fresh = (now - timedelta(days=1)).isoformat()
        items, drops = bfl.build_deals(
            [self._rec("A1", None, fresh)],
            now=now,
        )
        self.assertEqual(items, [])
        self.assertEqual(drops["no_savings_data"], 1)

    def test_drops_below_threshold(self):
        now = datetime(2026, 5, 25, tzinfo=timezone.utc)
        fresh = (now - timedelta(days=1)).isoformat()
        items, drops = bfl.build_deals(
            [self._rec("A1", 19, fresh)],
            now=now,
            min_savings=20,
        )
        self.assertEqual(items, [])
        self.assertEqual(drops["savings_below_threshold"], 1)

    def test_stale_guard(self):
        now = datetime(2026, 5, 25, tzinfo=timezone.utc)
        stale = (now - timedelta(days=15)).isoformat()
        fresh = (now - timedelta(days=1)).isoformat()
        items, drops = bfl.build_deals(
            [
                self._rec("STALE", 50, stale),
                self._rec("FRESH", 30, fresh),
            ],
            now=now,
            stale_days=14,
        )
        self.assertEqual([r.asin for r in items], ["FRESH"])
        self.assertEqual(drops["stale_or_unknown_fetch"], 1)

    def test_none_fetched_at_drops(self):
        now = datetime(2026, 5, 25, tzinfo=timezone.utc)
        items, drops = bfl.build_deals(
            [self._rec("A1", 40, None)],
            now=now,
        )
        self.assertEqual(items, [])
        self.assertEqual(drops["stale_or_unknown_fetch"], 1)

    def test_sort_savings_then_ivs(self):
        now = datetime(2026, 5, 25, tzinfo=timezone.utc)
        fresh = (now - timedelta(days=1)).isoformat()
        recs = [
            self._rec("A_TIE_LO", 40, fresh, ivs_100=80),
            self._rec("HIGH", 50, fresh, ivs_100=82),
            self._rec("A_TIE_HI", 40, fresh, ivs_100=95),
        ]
        items, _ = bfl.build_deals(recs, now=now)
        self.assertEqual([r.asin for r in items], ["HIGH", "A_TIE_HI", "A_TIE_LO"])

    def test_top_n(self):
        now = datetime(2026, 5, 25, tzinfo=timezone.utc)
        fresh = (now - timedelta(days=1)).isoformat()
        recs = [
            self._rec(f"A{i}", 30 + i, fresh) for i in range(10)
        ]
        items, _ = bfl.build_deals(recs, now=now, top_n=2)
        self.assertEqual(len(items), 2)


# ---------------------------------------------------------------------------
# build_deals_with_stale_fallback (#4007 follow-up 2)
# ---------------------------------------------------------------------------

class BuildDealsStaleFallbackTest(unittest.TestCase):
    def _rec(self, asin, savings, fetched_at, *, ivs_score=4.5, ivs_100=90):
        return bfl.ArticleRecord(
            asin=asin, slug=asin.lower(), name="x", image=None,
            ivs_score=ivs_score, ivs_100=ivs_100, best_price=2000,
            best_platform="Amazon", amazon_url=None,
            savings_percentage=savings, fetched_at=fetched_at,
        )

    def test_all_within_3days_uses_base_window(self):
        """全件が 3 日以内に観測されていれば base window (既定 3日) がそのまま
        採用され、フォールバックは発動しない。"""
        now = datetime(2026, 7, 27, tzinfo=timezone.utc)
        fresh = (now - timedelta(days=1)).isoformat()
        recs = [self._rec(f"A{i}", 30, fresh) for i in range(10)]

        items, drops, used = bfl.build_deals_with_stale_fallback(
            recs, now=now, stale_days=3, stale_min_cards=8, top_n=20,
        )
        self.assertEqual(used, 3)
        self.assertEqual(len(items), 10)
        self.assertEqual(drops["stale_or_unknown_fetch"], 0)

    def test_widens_to_7days_when_below_min_cards(self):
        """全件が 5 日前 (3日 window では 0 件) → 3日で下限割れ → 7日 window に
        広がり、採用 window が呼び出し側から取得できる。"""
        now = datetime(2026, 7, 27, tzinfo=timezone.utc)
        five_days_ago = (now - timedelta(days=5)).isoformat()
        recs = [self._rec(f"A{i}", 30, five_days_ago) for i in range(10)]

        items, drops, used = bfl.build_deals_with_stale_fallback(
            recs, now=now, stale_days=3, stale_min_cards=8, top_n=20,
        )
        self.assertEqual(used, 7)
        self.assertEqual(len(items), 10)

    def test_widens_to_14days_but_stays_empty_when_all_30days_old(self):
        """全件が 30 日前 → 14 日まで広げても stale_min_cards に届かず空になる
        ことを許容する (14 日を超えては広げない)。"""
        now = datetime(2026, 7, 27, tzinfo=timezone.utc)
        thirty_days_ago = (now - timedelta(days=30)).isoformat()
        recs = [self._rec(f"A{i}", 30, thirty_days_ago) for i in range(10)]

        items, drops, used = bfl.build_deals_with_stale_fallback(
            recs, now=now, stale_days=3, stale_min_cards=8, top_n=20,
        )
        self.assertEqual(used, 14)
        self.assertEqual(items, [])
        self.assertEqual(drops["stale_or_unknown_fetch"], 10)

    def test_below_min_cards_but_nonzero_still_widens_and_can_stay_below(self):
        """下限に届かない件数 (0 件超) でも widen は続き、14 日まで広げても
        届かなければそのまま (widen を止めた時点の) 結果を返す。"""
        now = datetime(2026, 7, 27, tzinfo=timezone.utc)
        fresh = (now - timedelta(days=1)).isoformat()
        stale30 = (now - timedelta(days=30)).isoformat()
        # 3日以内に観測があるのは 2 件だけ (stale_min_cards=8 を満たさない)。
        recs = [self._rec(f"FRESH{i}", 30, fresh) for i in range(2)]
        recs += [self._rec(f"STALE{i}", 30, stale30) for i in range(5)]

        items, drops, used = bfl.build_deals_with_stale_fallback(
            recs, now=now, stale_days=3, stale_min_cards=8, top_n=20,
        )
        # 30日前の記事は 14日 window でも stale のまま拾われないので
        # 最終的に 2 件のまま (8 件には届かない) が widen は 14 まで行われる。
        self.assertEqual(used, 14)
        self.assertEqual(len(items), 2)

    def test_custom_stale_days_above_7_skips_7day_rung(self):
        """--stale-days に 10 を指定した場合、フォールバックの梯子は
        [10, 14] のみ (7 は base より狭いので候補に入らない)。"""
        now = datetime(2026, 7, 27, tzinfo=timezone.utc)
        stale12 = (now - timedelta(days=12)).isoformat()
        recs = [self._rec(f"A{i}", 30, stale12) for i in range(10)]

        items, drops, used = bfl.build_deals_with_stale_fallback(
            recs, now=now, stale_days=10, stale_min_cards=8, top_n=20,
        )
        # 12日前は 10日 window では stale だが 14日 window では拾われる。
        self.assertEqual(used, 14)
        self.assertEqual(len(items), 10)


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

class SerializeTest(unittest.TestCase):
    def test_cospa_url_lowercased_and_score_present(self):
        rec = bfl.ArticleRecord(
            asin="B00ABCXYZ", slug="2026-05-13-B00ABCXYZ", name="x", image=None,
            ivs_score=4.6, ivs_100=92, best_price=1500,
            best_platform="Amazon", amazon_url="https://example/dp/B00ABCXYZ",
        )
        rec.score_cospa = 0.333333
        # session 58 で serialize_cospa は bands 構造を返す serialize_cospa_bands に置換済
        bands = [
            {"key": "1000-2000", "label": "¥1,000-¥2,000", "price_min": 1000,
             "price_max": 1999, "default": True, "items": [rec]},
        ]
        payload = bfl.serialize_cospa_bands(
            bands, filter_params={}, generated_at="2026-05-25T00:00:00Z"
        )
        self.assertEqual(payload["type"], "cospa")
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["bands"][0]["key"], "1000-2000")
        self.assertTrue(payload["bands"][0]["default"])
        entry = payload["bands"][0]["items"][0]
        # #3364: url_internal は /products/{asin}/ 形式 (build_post.py と同じ)。
        # Hugo lowercases URLs - asin.lower() must be applied (memory trap).
        self.assertEqual(entry["url_internal"], "/products/b00abcxyz/")
        self.assertEqual(entry["score_cospa"], 0.3333)  # rounded to 4dp
        self.assertNotIn("savings_percentage", entry)

    def test_deals_includes_savings_and_fetched_at(self):
        rec = bfl.ArticleRecord(
            asin="B00DEFGHI", slug="2026-05-13-B00DEFGHI", name="x", image=None,
            ivs_score=4.5, ivs_100=90, best_price=2000,
            best_platform="Amazon", amazon_url=None,
            savings_percentage=35, fetched_at="2026-05-24T10:00:00+00:00",
        )
        payload = bfl.serialize_deals(
            [rec], filter_params={}, generated_at="2026-05-25T00:00:00Z"
        )
        entry = payload["items"][0]
        self.assertEqual(entry["savings_percentage"], 35)
        self.assertEqual(entry["fetched_at"], "2026-05-24T10:00:00+00:00")
        self.assertNotIn("score_cospa", entry)


# ---------------------------------------------------------------------------
# End-to-end run()
# ---------------------------------------------------------------------------

class RunEndToEndTest(unittest.TestCase):
    def test_writes_all_three_outputs(self):
        with tempfile.TemporaryDirectory() as td:
            d = pathlib.Path(td)
            arts = d / "articles"
            arts.mkdir()
            per = d / "per_asin"
            out_hugo = d / "hugo_features"
            out_manifest = d / "manifest" / "_build_manifest.json"

            # Article 1: cospa win (high IVS, cheap), no savings
            _write_article(arts, _make_article(
                "B0AAA1", ivs_score=4.8, ivs_100=96, best_price=1200,
            ))
            # Article 2: deals win (moderate IVS, big discount, fresh)
            _write_article(arts, _make_article(
                "B0BBB2", ivs_score=4.2, ivs_100=84, best_price=3000,
            ))
            now = datetime(2026, 5, 25, tzinfo=timezone.utc)
            _write_per_asin(
                per, "B0BBB2",
                savings=40, fetched_at=(now - timedelta(days=2)).isoformat(),
            )
            # Article 3: stale discount (should be dropped from deals)
            _write_article(arts, _make_article(
                "B0CCC3", ivs_score=4.4, ivs_100=88, best_price=2500,
            ))
            _write_per_asin(
                per, "B0CCC3",
                savings=50, fetched_at=(now - timedelta(days=60)).isoformat(),
            )

            # stale_min_cards=1: このテストは低母数 (記事3件) なので、既定の
            # stale_min_cards=8 だと 3日 window の 1 件では届かず 14日 まで
            # 自動で広がってしまう (#4007 follow-up 2 のフォールバック挙動)。
            # ここでは base window (3日) がそのまま採用されることを検証したいので
            # 下限を 1 に下げて widen が発動しないようにする。
            manifest = bfl.run(
                articles_dir=arts,
                per_asin_dir=per,
                out_hugo=out_hugo,
                out_manifest=out_manifest,
                now=now,
                stale_min_cards=1,
            )

            cospa = json.loads((out_hugo / "cospa.json").read_text(encoding="utf-8"))
            deals = json.loads((out_hugo / "deals.json").read_text(encoding="utf-8"))
            saved_manifest = json.loads(out_manifest.read_text(encoding="utf-8"))

            # cospa: 価格帯ナビ構造。記事 3 件はそれぞれ別の帯に入る
            # (¥1,200 -> 1000-2000, ¥2,500 -> 2000-3000, ¥3,000 -> 3000-5000)
            self.assertEqual(cospa["count"], 3)
            # bands は PRICE_BANDS の順で 6 要素
            self.assertEqual(len(cospa["bands"]), 6)
            band_by_key = {b["key"]: b for b in cospa["bands"]}
            self.assertEqual(band_by_key["1000-2000"]["items"][0]["asin"], "B0AAA1")
            self.assertEqual(band_by_key["2000-3000"]["items"][0]["asin"], "B0CCC3")
            self.assertEqual(band_by_key["3000-5000"]["items"][0]["asin"], "B0BBB2")
            self.assertTrue(band_by_key["3000-5000"]["default"])  # 既定タブ

            # deals: only B0BBB2 (B0CCC3 stale, B0AAA1 no savings data)
            self.assertEqual(deals["count"], 1)
            self.assertEqual(deals["items"][0]["asin"], "B0BBB2")
            self.assertEqual(deals["items"][0]["savings_percentage"], 40)

            # manifest accounting
            self.assertEqual(saved_manifest["articles_loaded"], 3)
            self.assertEqual(saved_manifest["deals"]["drops"]["no_savings_data"], 1)
            self.assertEqual(
                saved_manifest["deals"]["drops"]["stale_or_unknown_fetch"], 1
            )
            # #4007 follow-up 2: stale_min_cards=1 なので base window (3日) の
            # ままウィジェンしないことを確認する。
            self.assertEqual(saved_manifest["deals"]["stale_window_used"], 3)
            self.assertEqual(saved_manifest["deals"]["filter"]["stale_days"], 3)
            # cospa manifest は band 別 dict
            self.assertEqual(manifest["cospa"]["bands"]["1000-2000"], 1)
            self.assertEqual(manifest["cospa"]["bands"]["2000-3000"], 1)
            self.assertEqual(manifest["cospa"]["bands"]["3000-5000"], 1)


class IvsAxesPropagationTest(unittest.TestCase):
    """#589: 4 軸 (education / safety / cost_performance / longevity) が
    ArticleRecord → cospa.json / deals.json payload まで透過することを確認。"""

    def test_load_articles_attaches_ivs_axes(self):
        with tempfile.TemporaryDirectory() as td:
            d = pathlib.Path(td)
            _write_article(d, _make_article("B0AXIS0001"))
            records = bfl.load_articles(d)
        self.assertEqual(len(records), 1)
        axes = records[0].ivs_axes
        self.assertIsNotNone(axes)
        self.assertEqual(set(axes), {"education", "safety", "cost_performance", "longevity"})
        # stub の breakdown は全 0 なので axes は 2.0 floor
        for v in axes.values():
            self.assertEqual(v, 2.0)

    def test_cospa_payload_includes_ivs_axes(self):
        rec = bfl.ArticleRecord(
            asin="B0AXIS0002", slug="2026-05-30-B0AXIS0002", name="x", image=None,
            ivs_score=4.5, ivs_100=90, best_price=2500,
            best_platform="Amazon", amazon_url=None,
            ivs_axes={"education": 4.2, "safety": 5.0, "cost_performance": 3.8, "longevity": 4.5},
        )
        bands = [
            {"key": "2000-3000", "label": "¥2,000-¥3,000", "price_min": 2000,
             "price_max": 2999, "default": False, "items": [rec]},
        ]
        payload = bfl.serialize_cospa_bands(
            bands, filter_params={}, generated_at="2026-05-30T00:00:00Z"
        )
        entry = payload["bands"][0]["items"][0]
        self.assertEqual(entry["ivs_axes"], {
            "education": 4.2, "safety": 5.0, "cost_performance": 3.8, "longevity": 4.5,
        })

    def test_deals_payload_includes_ivs_axes(self):
        rec = bfl.ArticleRecord(
            asin="B0AXIS0003", slug="2026-05-30-B0AXIS0003", name="x", image=None,
            ivs_score=4.3, ivs_100=86, best_price=2000,
            best_platform="Amazon", amazon_url=None,
            savings_percentage=30, fetched_at="2026-05-29T10:00:00+00:00",
            ivs_axes={"education": 4.0, "safety": 4.6, "cost_performance": 4.1, "longevity": 3.9},
        )
        payload = bfl.serialize_deals(
            [rec], filter_params={}, generated_at="2026-05-30T00:00:00Z"
        )
        entry = payload["items"][0]
        self.assertEqual(entry["ivs_axes"]["education"], 4.0)
        self.assertEqual(entry["ivs_axes"]["safety"], 4.6)

    def test_payload_omits_ivs_axes_when_absent(self):
        rec = bfl.ArticleRecord(
            asin="B0AXIS0004", slug="2026-05-30-B0AXIS0004", name="x", image=None,
            ivs_score=4.0, ivs_100=80, best_price=1500,
            best_platform="Amazon", amazon_url=None,
            ivs_axes=None,
        )
        bands = [
            {"key": "1000-2000", "label": "¥1,000-¥2,000", "price_min": 1000,
             "price_max": 1999, "default": True, "items": [rec]},
        ]
        payload = bfl.serialize_cospa_bands(
            bands, filter_params={}, generated_at="2026-05-30T00:00:00Z"
        )
        entry = payload["bands"][0]["items"][0]
        self.assertNotIn("ivs_axes", entry)


class DeletedArticleRegenTest(unittest.TestCase):
    """#3364: 記事 json が削除された後の再生成で stale entry が落ちることを確認。

    build_feature_lists は毎回 articles_dir を再走査するだけなので、削除済み
    ASIN は次回生成で自然に除外される (追加の存在チェックは不要)。legacy
    /posts/{slug}/ は Hugo alias が記事と一緒に消えて 404 になるが、
    /products/{asin}/ 形式なら「記事が無い = リンク自体を出さない」で済む。
    """

    def test_deleted_article_dropped_from_next_generation(self):
        with tempfile.TemporaryDirectory() as td:
            d = pathlib.Path(td)
            arts = d / "articles"
            arts.mkdir()
            per = d / "per_asin"
            out_hugo = d / "hugo_features"
            out_manifest = d / "manifest" / "_build_manifest.json"

            path_keep = _write_article(arts, _make_article(
                "B0KEEP1", ivs_score=4.8, ivs_100=96, best_price=1200,
            ))
            path_removed = _write_article(arts, _make_article(
                "B0GONE1", ivs_score=4.5, ivs_100=90, best_price=1300,
            ))

            bfl.run(
                articles_dir=arts, per_asin_dir=per,
                out_hugo=out_hugo, out_manifest=out_manifest,
            )
            first = json.loads((out_hugo / "cospa.json").read_text(encoding="utf-8"))
            first_asins = {
                it["asin"] for b in first["bands"] for it in b["items"]
            }
            self.assertIn("B0GONE1", first_asins)

            # 記事削除 (Jules cleanup 等) をシミュレート。
            path_removed.unlink()

            bfl.run(
                articles_dir=arts, per_asin_dir=per,
                out_hugo=out_hugo, out_manifest=out_manifest,
            )
            second = json.loads((out_hugo / "cospa.json").read_text(encoding="utf-8"))
            second_asins = {
                it["asin"] for b in second["bands"] for it in b["items"]
            }
            self.assertNotIn("B0GONE1", second_asins)
            self.assertIn("B0KEEP1", second_asins)
            self.assertTrue(path_keep.exists())


class ParseMinMonthsTest(unittest.TestCase):
    """#3563: parse_min_months は build_category_hubs.py から移動してきた関数。
    build_category_hubs 側のテスト (ParseMinMonthsTest) と同じケースを
    移動元 (bfl) に対しても確認する。"""

    def test_years_variants(self):
        self.assertEqual(bfl.parse_min_months("3歳以上"), 36)
        self.assertEqual(bfl.parse_min_months("3歳〜"), 36)
        self.assertEqual(bfl.parse_min_months("3才"), 36)

    def test_half_year_variants(self):
        self.assertEqual(bfl.parse_min_months("1歳6ヶ月〜"), 18)
        self.assertEqual(bfl.parse_min_months("1.5歳〜"), 18)
        self.assertEqual(bfl.parse_min_months("1歳半〜"), 18)

    def test_months_only(self):
        self.assertEqual(bfl.parse_min_months("0ヶ月〜"), 0)
        self.assertEqual(bfl.parse_min_months("6ヶ月〜"), 6)

    def test_none_cases(self):
        self.assertIsNone(bfl.parse_min_months(None))
        self.assertIsNone(bfl.parse_min_months(""))
        self.assertIsNone(bfl.parse_min_months("対象年齢の記載なし"))


class AgeMinMonthsFromArticleTest(unittest.TestCase):
    """#3563: 優先順 product.target_age or product.age_range ->
    persona_fit.age_range -> technical_specs.age_range (build_post.py
    L2668-2699 と同じ)。"""

    def test_prefers_product_target_age(self):
        raw = {
            "product": {"target_age": "3歳〜", "age_range": "5歳〜"},
            "persona_fit": {"age_range": "1歳〜"},
        }
        self.assertEqual(bfl.age_min_months_from_article(raw), 36)

    def test_falls_back_to_product_age_range(self):
        raw = {
            "product": {"age_range": "2歳〜"},
            "persona_fit": {"age_range": "1歳〜"},
        }
        self.assertEqual(bfl.age_min_months_from_article(raw), 24)

    def test_falls_back_to_persona_fit(self):
        raw = {
            "product": {},
            "persona_fit": {"age_range": "1歳6ヶ月〜"},
            "technical_specs": {"age_range": "5歳〜"},
        }
        self.assertEqual(bfl.age_min_months_from_article(raw), 18)

    def test_falls_back_to_technical_specs(self):
        raw = {
            "product": {},
            "technical_specs": {"age_range": "6ヶ月〜"},
        }
        self.assertEqual(bfl.age_min_months_from_article(raw), 6)

    def test_returns_none_when_all_absent(self):
        self.assertIsNone(bfl.age_min_months_from_article({"product": {}}))


class LoadArticlesAgeMinMonthsTest(unittest.TestCase):
    """#3563: load_articles が age_min_months を ArticleRecord に伝播し、
    payload にも emit することを確認する。"""

    def test_load_articles_attaches_age_min_months(self):
        with tempfile.TemporaryDirectory() as td:
            d = pathlib.Path(td)
            article = _make_article("B0AGEPROP01")
            article["product"]["target_age"] = "3歳〜"
            _write_article(d, article)
            records = bfl.load_articles(d)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].age_min_months, 36)

    def test_load_articles_none_when_no_age_data(self):
        with tempfile.TemporaryDirectory() as td:
            d = pathlib.Path(td)
            _write_article(d, _make_article("B0AGEPROP02"))
            records = bfl.load_articles(d)
        self.assertIsNone(records[0].age_min_months)

    def test_payload_includes_age_min_months(self):
        rec = bfl.ArticleRecord(
            asin="B0AGEPROP03", slug="x", name="x", image=None,
            ivs_score=4.5, ivs_100=90, best_price=2000,
            best_platform="Amazon", amazon_url=None,
            age_min_months=24,
        )
        payload = bfl._record_to_payload_common(rec, 1)
        self.assertEqual(payload["age_min_months"], 24)

    def test_payload_age_min_months_none_when_absent(self):
        rec = bfl.ArticleRecord(
            asin="B0AGEPROP04", slug="x", name="x", image=None,
            ivs_score=4.5, ivs_100=90, best_price=2000,
            best_platform="Amazon", amazon_url=None,
        )
        payload = bfl._record_to_payload_common(rec, 1)
        self.assertIsNone(payload["age_min_months"])


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# 在庫ガード (#5130 残件3)
# ---------------------------------------------------------------------------

class UnavailableGateTest(unittest.TestCase):
    """買えない商品を /deals/ /cospa/ の母数から外す。

    stale ガードでは代われない。**価格が取れているのに在庫が無い**観測が実在し
    (2026-08-18 実測で 15 ASIN が `一時的に在庫切れ; 入荷時期は未定です。` と価格を
    同時に持つ)、その形は毎日新しく観測されるので fetched_at が一生新鮮なままに
    なるため。
    """

    def _rec(self, asin, availability, *, savings=40, fetched_at=None,
             best_price=2000):
        return bfl.ArticleRecord(
            asin=asin, slug=asin.lower(), name="x", image=None,
            ivs_score=4.5, ivs_100=90, best_price=best_price,
            best_platform="Amazon", amazon_url=None,
            savings_percentage=savings, fetched_at=fetched_at,
            availability=availability,
        )

    def test_deals_drops_out_of_stock_even_when_observation_is_fresh(self):
        now = datetime(2026, 8, 18, tzinfo=timezone.utc)
        fresh = (now - timedelta(hours=12)).isoformat()
        items, drops = bfl.build_deals(
            [
                self._rec("OOS", "一時的に在庫切れ; 入荷時期は未定です。", fetched_at=fresh),
                self._rec("OK", "在庫あり。", fetched_at=fresh),
            ],
            now=now,
        )
        self.assertEqual([r.asin for r in items], ["OK"])
        self.assertEqual(drops["unavailable"], 1)
        # stale ガードでは 1 件も落ちていない = 在庫ガードでしか防げない形。
        self.assertEqual(drops["stale_or_unknown_fetch"], 0)

    def test_deals_keeps_records_without_any_observation(self):
        """観測が無い (availability=None) 記録は落とさない。

        「観測が無い」と「在庫が無いと観測した」は別物。倒すと観測を持たない
        記事 (2026-08-18 実測で 212 件) が一覧からまとめて消える。
        """
        now = datetime(2026, 8, 18, tzinfo=timezone.utc)
        fresh = (now - timedelta(hours=12)).isoformat()
        items, drops = bfl.build_deals([self._rec("NOOBS", None, fetched_at=fresh)], now=now)
        self.assertEqual([r.asin for r in items], ["NOOBS"])
        self.assertEqual(drops["unavailable"], 0)

    def test_deals_keeps_low_stock_wording(self):
        """「残りN点」「N〜N日以内に発送」は購入可なので落とさない。"""
        now = datetime(2026, 8, 18, tzinfo=timezone.utc)
        fresh = (now - timedelta(hours=12)).isoformat()
        items, _ = bfl.build_deals(
            [
                self._rec("A1", "残り1点 ご注文はお早めに", fetched_at=fresh),
                self._rec("A2", "1～2日以内に発送します。", fetched_at=fresh),
            ],
            now=now,
        )
        self.assertEqual(sorted(r.asin for r in items), ["A1", "A2"])

    def test_cospa_drops_out_of_stock(self):
        items, drops = bfl.build_cospa(
            [
                self._rec("OOS", "現在在庫切れです。"),
                self._rec("OK", "在庫あり。"),
            ],
        )
        self.assertEqual([r.asin for r in items], ["OK"])
        self.assertEqual(drops["unavailable"], 1)

    def test_overlay_carries_availability_even_when_price_is_none(self):
        """価格を上書きしない観測でも在庫状態は運ぶ。

        これを price の有無で条件分けすると、「価格は per_asin 由来のまま、
        在庫は最新の在庫切れ」というケースを取りこぼす。
        """
        rec = self._rec("A1", None, fetched_at=None)
        obs = price_overlay.PriceObservation(
            asin="A1", price=None, savings_percentage=None,
            availability="一時的に在庫切れ; 入荷時期は未定です。",
            observed_at="2026-08-18T00:00:00+00:00", source="price_watch",
        )
        bfl.overlay_current_prices([rec], pathlib.Path("nonexistent"),
                                   watch_index={"A1": obs})
        self.assertEqual(rec.availability, "一時的に在庫切れ; 入荷時期は未定です。")
        self.assertEqual(rec.best_price, 2000, "価格は上書きされない")
