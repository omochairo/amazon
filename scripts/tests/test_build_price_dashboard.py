"""Unit tests for build_price_dashboard.py (#3332 N4-P2).

Coverage:
1. evaluate_drop  - reference=90日窓内max、drop_pct/drop_yen 算出。
2. evaluate_drop  - 閾値未満 (5%未満 / 100円未満 / 窓内1点のみ) の除外。
3. is_unavailable - 在庫切れ系文字列 / None を除外、在庫あり系は残す。
4. evaluate_drop  - all_time_low フラグ。
5. build_items    - drop_pct 降順ソート + top_n cap。
6. build_items    - 記事が無い ASIN の skip。
7. run            - 履歴欠落/空入力でも items 空 + exit 0 (graceful)。
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.dirname(THIS_DIR)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import build_price_dashboard as bpd  # noqa: E402


NOW = datetime(2026, 7, 18, 0, 0, 0, tzinfo=timezone.utc)


def _ts(days_ago: float) -> datetime:
    return NOW - timedelta(days=days_ago)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


# ---------------------------------------------------------------------------
# evaluate_drop
# ---------------------------------------------------------------------------

class TestEvaluateDrop:
    def test_basic_drop_uses_90_day_window_max_as_reference(self):
        history = [
            (_ts(80), 3000),
            (_ts(40), 2800),
            (_ts(10), 2500),
        ]
        result = bpd.evaluate_drop(2000, NOW, history)
        assert result is not None
        assert result["reference"] == 3000
        assert result["drop_yen"] == 1000
        assert result["drop_pct"] == pytest.approx(1000 / 3000, abs=1e-3)
        assert result["all_time_low"] is True

    def test_points_outside_90_day_window_are_ignored(self):
        history = [
            (_ts(200), 9000),  # 窓外: reference に使われてはいけない
            (_ts(20), 2200),
        ]
        result = bpd.evaluate_drop(2000, NOW, history)
        assert result is not None
        assert result["reference"] == 2200  # 9000 は無視される

    def test_below_pct_threshold_is_excluded(self):
        # 100 -> 96 は 4% 未満 (閾値5%) で除外
        history = [(_ts(10), 100)]
        result = bpd.evaluate_drop(96, NOW, history)
        assert result is None

    def test_below_yen_threshold_is_excluded(self):
        # 率は5%閾値を満たす(6%)が、絶対額が100円未満(90円) -> 除外
        history = [(_ts(10), 1500)]
        result = bpd.evaluate_drop(1410, NOW, history)  # drop_yen=90 (<100), drop_pct=6%
        assert result is None

    def test_single_point_window_is_excluded(self):
        # current 以外に窓内観測点が無い -> 比較不能で除外
        result = bpd.evaluate_drop(2000, NOW, [])
        assert result is None

    def test_all_time_low_false_when_lower_price_existed(self):
        history = [
            (_ts(10), 3000),
            (_ts(5), 1500),  # current (2000) より安い過去点がある
        ]
        result = bpd.evaluate_drop(2000, NOW, history)
        assert result is not None
        assert result["all_time_low"] is False

    def test_all_time_low_true_when_current_ties_the_minimum(self):
        history = [
            (_ts(30), 2000),
            (_ts(10), 2000),
        ]
        result = bpd.evaluate_drop(1800, NOW, history)
        assert result is not None
        assert result["all_time_low"] is True


# ---------------------------------------------------------------------------
# is_unavailable
# ---------------------------------------------------------------------------

class TestIsUnavailable:
    @pytest.mark.parametrize("avail", [
        "在庫あり。",
        "残り1点 ご注文はお早めに",
        "残り5点（入荷予定あり）",
        "通常2～3日以内に発送します。",
    ])
    def test_available_markers_pass(self, avail):
        assert bpd.is_unavailable(avail) is False

    @pytest.mark.parametrize("avail", [
        None,
        "",
        "現在在庫切れです。",
        "一時的に在庫切れ; 入荷時期は未定です。",
        "この商品は現在お取り扱いできません。",
    ])
    def test_unavailable_markers_excluded(self, avail):
        assert bpd.is_unavailable(avail) is True


# ---------------------------------------------------------------------------
# build_items (higher-level, with tmp history dirs + article meta)
# ---------------------------------------------------------------------------

def _write_history_file(path: Path, points: list[tuple[datetime, int]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for ts, price in points:
            f.write(json.dumps({
                "ts": _iso(ts), "source": "amazon", "price": price, "availability": None,
            }) + "\n")


@pytest.fixture()
def dashboard_dirs(tmp_path: Path):
    price_watch_dir = tmp_path / "price_watch"
    price_history_dir = tmp_path / "price_history"
    (price_watch_dir / "history").mkdir(parents=True)
    price_history_dir.mkdir(parents=True)
    return price_watch_dir, price_history_dir


class TestBuildItems:
    def test_skips_asin_without_article(self, dashboard_dirs):
        price_watch_dir, price_history_dir = dashboard_dirs
        _write_history_file(price_watch_dir / "history" / "NOARTICLE1.jsonl", [(_ts(30), 3000)])
        latest = {"items": {"NOARTICLE1": {"avail": "在庫あり。", "p": 2000, "ts": _iso(NOW)}}}
        items = bpd.build_items(latest, {}, price_watch_dir, price_history_dir)
        assert items == []

    def test_sorts_by_drop_pct_desc_and_caps_top_n(self, dashboard_dirs):
        price_watch_dir, price_history_dir = dashboard_dirs
        article_meta = {}
        latest_items = {}
        # 3 candidates with different drop_pct; cap at 2 to verify slicing.
        specs = [
            ("AAAA000001", 3000, 2000),  # drop 33%
            ("AAAA000002", 3000, 2500),  # drop 16.7%
            ("AAAA000003", 3000, 2900),  # drop 3.3% (below 5% threshold, excluded regardless)
        ]
        for asin, ref_price, cur_price in specs:
            _write_history_file(price_watch_dir / "history" / f"{asin}.jsonl", [(_ts(30), ref_price)])
            latest_items[asin] = {"avail": "在庫あり。", "p": cur_price, "ts": _iso(NOW)}
            article_meta[asin] = {
                "name": f"商品{asin}", "brand": "テストブランド",
                "image": "https://x/i.jpg", "url": f"/products/{asin.lower()}/",
            }
        latest = {"items": latest_items}
        items = bpd.build_items(
            latest, article_meta, price_watch_dir, price_history_dir, top_n_cap=2,
        )
        # AAAA000003 excluded by threshold (3.3% < 5%); remaining 2 fit under cap=2.
        assert [i["asin"] for i in items] == ["AAAA000001", "AAAA000002"]
        assert items[0]["drop_pct"] >= items[1]["drop_pct"]

    def test_unavailable_asin_excluded(self, dashboard_dirs):
        price_watch_dir, price_history_dir = dashboard_dirs
        _write_history_file(price_watch_dir / "history" / "SOLDOUT001.jsonl", [(_ts(30), 3000)])
        latest = {"items": {"SOLDOUT001": {"avail": "現在在庫切れです。", "p": 2000, "ts": _iso(NOW)}}}
        article_meta = {"SOLDOUT001": {"name": "n", "brand": "b", "image": "i", "url": "/products/soldout001/"}}
        items = bpd.build_items(latest, article_meta, price_watch_dir, price_history_dir)
        assert items == []

    def test_build_items_output_is_enriched(self, dashboard_dirs, tmp_path: Path):
        # #3563: build_items は最終的に top_n_cap 適用後の enrich_items 呼出しを
        # 経由するので、返る item に age_min_months 等が乗っていることを確認する。
        price_watch_dir, price_history_dir = dashboard_dirs
        articles_dir = tmp_path / "articles"
        articles_dir.mkdir()
        asin = "B0ENRICHED001"
        article = {
            "product": {
                "asin": asin, "name": "n", "brand": "レゴ",
                "target_age": "4歳〜",
                "prices": {"amazon": {"url": f"https://amazon.example/dp/{asin}"}},
            }
        }
        (articles_dir / f"2026-01-01-{asin}.json").write_text(
            json.dumps(article, ensure_ascii=False), encoding="utf-8",
        )
        article_meta = bpd.load_article_meta(articles_dir)
        # load_article_meta の url は article JSON に brand/image が無い前提の
        # デフォルトと違うため、build_items が要求する形に synth する必要はない
        # (load_article_meta が既に name/brand/image/url/path を全部埋める)。
        _write_history_file(price_watch_dir / "history" / f"{asin}.jsonl", [(_ts(30), 3000)])
        latest = {"items": {asin: {"avail": "在庫あり。", "p": 2000, "ts": _iso(NOW)}}}
        items = bpd.build_items(latest, article_meta, price_watch_dir, price_history_dir)
        assert len(items) == 1
        item = items[0]
        assert item["age_min_months"] == 48
        assert item["amazon_url"] == f"https://amazon.example/dp/{asin}"
        assert isinstance(item["ivs_100"], int)

    def test_merges_both_history_dirs(self, dashboard_dirs):
        price_watch_dir, price_history_dir = dashboard_dirs
        asin = "MERGE00001"
        # price_watch/history has the recent points; price_history (weekly lane)
        # has an older, higher price that should still be picked up as reference.
        _write_history_file(price_watch_dir / "history" / f"{asin}.jsonl", [(_ts(10), 2200)])
        _write_history_file(price_history_dir / f"{asin}.jsonl", [(_ts(60), 3500)])
        latest = {"items": {asin: {"avail": "在庫あり。", "p": 2000, "ts": _iso(NOW)}}}
        article_meta = {asin: {"name": "n", "brand": "b", "image": "i", "url": f"/products/{asin.lower()}/"}}
        items = bpd.build_items(latest, article_meta, price_watch_dir, price_history_dir)
        assert len(items) == 1
        assert items[0]["reference"] == 3500


# ---------------------------------------------------------------------------
# load_article_meta
# ---------------------------------------------------------------------------

class TestLoadArticleMeta:
    def test_skips_sidecar_files(self, tmp_path: Path):
        articles_dir = tmp_path / "articles"
        articles_dir.mkdir()
        primary = {"product": {"asin": "B0PRIMARY01", "name": "本体", "brand": "b", "image": "i"}}
        sidecar = {"product": {"asin": "B0SIDECAR01"}}
        (articles_dir / "2026-01-01-B0PRIMARY01.json").write_text(
            json.dumps(primary, ensure_ascii=False), encoding="utf-8"
        )
        (articles_dir / "2026-01-01-B0PRIMARY01.quality.json").write_text(
            json.dumps(sidecar, ensure_ascii=False), encoding="utf-8"
        )
        (articles_dir / "2026-01-01-B0PRIMARY01.enrichment.json").write_text(
            json.dumps(sidecar, ensure_ascii=False), encoding="utf-8"
        )
        (articles_dir / "2026-01-01-B0PRIMARY01.seo.json").write_text(
            json.dumps(sidecar, ensure_ascii=False), encoding="utf-8"
        )
        meta = bpd.load_article_meta(articles_dir)
        assert set(meta.keys()) == {"B0PRIMARY01"}
        assert meta["B0PRIMARY01"]["url"] == "/products/b0primary01/"

    def test_missing_dir_returns_empty(self, tmp_path: Path):
        meta = bpd.load_article_meta(tmp_path / "does-not-exist")
        assert meta == {}

    def test_meta_includes_path_for_later_enrichment(self, tmp_path: Path):
        # #3563: path が meta に含まれることで enrich_items が採択後の item だけ
        # 記事 json を再読込してスコア計算できる (全記事でスコア計算しないため)。
        articles_dir = tmp_path / "articles"
        articles_dir.mkdir()
        path = articles_dir / "2026-01-01-B0WITHPATH1.json"
        path.write_text(
            json.dumps({"product": {"asin": "B0WITHPATH1", "name": "n"}}, ensure_ascii=False),
            encoding="utf-8",
        )
        meta = bpd.load_article_meta(articles_dir)
        assert meta["B0WITHPATH1"]["path"] == str(path)


# ---------------------------------------------------------------------------
# enrich_items (#3563: age_min_months / amazon_url / ivs_100 / ivs_axes)
# ---------------------------------------------------------------------------

class TestEnrichItems:
    def _write_article(self, articles_dir: Path, asin: str, **product_extra) -> dict:
        product = {"asin": asin, "name": "商品", "brand": "レゴ"}
        product.update(product_extra)
        article = {"product": product}
        (articles_dir / f"2026-01-01-{asin}.json").write_text(
            json.dumps(article, ensure_ascii=False), encoding="utf-8",
        )
        return article

    def test_enriches_age_amazon_url_and_score(self, tmp_path: Path):
        articles_dir = tmp_path / "articles"
        articles_dir.mkdir()
        asin = "B0ENRICH0001"
        self._write_article(
            articles_dir, asin,
            target_age="3歳〜",
            prices={"amazon": {"url": f"https://amazon.example/dp/{asin}"}},
        )
        meta = bpd.load_article_meta(articles_dir)
        items = [{"asin": asin}]
        bpd.enrich_items(items, meta)
        item = items[0]
        assert item["age_min_months"] == 36
        assert item["amazon_url"] == f"https://amazon.example/dp/{asin}"
        assert isinstance(item["ivs_100"], int)
        assert set(item["ivs_axes"]) == {
            "education", "safety", "cost_performance", "longevity",
        }

    def test_nil_guards_when_path_missing(self):
        # article_meta に該当 ASIN が無い (path 解決不能) -> 4フィールドとも None。
        items = [{"asin": "B0NOMETA0001"}]
        bpd.enrich_items(items, {})
        item = items[0]
        assert item["age_min_months"] is None
        assert item["amazon_url"] is None
        assert item["ivs_100"] is None
        assert item["ivs_axes"] is None

    def test_nil_guards_when_article_unreadable(self, tmp_path: Path):
        articles_dir = tmp_path / "articles"
        articles_dir.mkdir()
        path = articles_dir / "broken.json"
        path.write_text("{not json", encoding="utf-8")
        items = [{"asin": "B0BROKEN001"}]
        bpd.enrich_items(items, {"B0BROKEN001": {"path": str(path)}})
        item = items[0]
        assert item["age_min_months"] is None
        assert item["ivs_100"] is None

    def test_only_enriches_given_items_not_all_meta(self, tmp_path: Path):
        # enrich_items は渡された items のみ処理する (呼出し側が top_n_cap 適用後の
        # リストだけを渡すことで、全 1580 記事のスコア計算を避ける設計)。
        articles_dir = tmp_path / "articles"
        articles_dir.mkdir()
        self._write_article(articles_dir, "B0SELECTED01", target_age="2歳〜")
        self._write_article(articles_dir, "B0NOTSELECTED01", target_age="5歳〜")
        meta = bpd.load_article_meta(articles_dir)
        items = [{"asin": "B0SELECTED01"}]
        bpd.enrich_items(items, meta)
        assert items[0]["age_min_months"] == 24
        assert len(items) == 1  # B0NOTSELECTED01 never touched


# ---------------------------------------------------------------------------
# run() end-to-end: graceful empty-input handling
# ---------------------------------------------------------------------------

class TestRunGraceful:
    def test_missing_latest_json_produces_empty_items_not_error(self, tmp_path: Path):
        articles_dir = tmp_path / "data" / "articles"
        articles_dir.mkdir(parents=True)
        price_watch_dir = tmp_path / "data" / "price_watch"
        price_history_dir = tmp_path / "data" / "price_history"
        out_path = tmp_path / "hugo" / "data" / "price_dashboard.json"

        payload = bpd.run(
            articles_dir=articles_dir,
            price_watch_dir=price_watch_dir,
            price_history_dir=price_history_dir,
            out_path=out_path,
        )
        assert payload["count"] == 0
        assert payload["items"] == []
        assert out_path.exists()
        on_disk = json.loads(out_path.read_text(encoding="utf-8"))
        assert on_disk["items"] == []

    def test_end_to_end_writes_qualifying_item(self, tmp_path: Path):
        articles_dir = tmp_path / "data" / "articles"
        articles_dir.mkdir(parents=True)
        price_watch_dir = tmp_path / "data" / "price_watch"
        price_history_dir = tmp_path / "data" / "price_history"
        (price_watch_dir / "history").mkdir(parents=True)
        price_history_dir.mkdir(parents=True)
        out_path = tmp_path / "hugo" / "data" / "price_dashboard.json"

        asin = "B0E2E000001"
        article = {"product": {"asin": asin, "name": "E2Eテスト商品", "brand": "b", "image": "https://x/i.jpg"}}
        (articles_dir / f"2026-01-01-{asin}.json").write_text(
            json.dumps(article, ensure_ascii=False), encoding="utf-8"
        )
        _write_history_file(price_watch_dir / "history" / f"{asin}.jsonl", [(_ts(20), 3000)])

        latest = {
            "generated_at": _iso(NOW),
            "items": {asin: {"avail": "在庫あり。", "p": 2000, "ts": _iso(NOW)}},
        }
        (price_watch_dir / "latest.json").write_text(
            json.dumps(latest, ensure_ascii=False), encoding="utf-8"
        )

        payload = bpd.run(
            articles_dir=articles_dir,
            price_watch_dir=price_watch_dir,
            price_history_dir=price_history_dir,
            out_path=out_path,
        )
        assert payload["count"] == 1
        assert payload["source_updated"] == _iso(NOW)
        item = payload["items"][0]
        assert item["asin"] == asin
        assert item["current"] == 2000
        assert item["reference"] == 3000
        assert item["drop_yen"] == 1000
        assert item["url"] == f"/products/{asin.lower()}/"
