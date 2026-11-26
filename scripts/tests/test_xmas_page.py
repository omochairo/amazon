"""scripts/xmas_page.py のテスト (navi-brain#214 v2 第 1 弾)。

- 遊びのタイプ: diagnosis_core.js の INTERESTS を読めること・診断の genreOf と同じ順で当てること
- 声: 購入者・レビュー記事の出典があるものを先に・出典の種類と数・メーカーの説明だけの声を数えない
- メーカー公式の事実: 安全 > 品質 > 耐久・比較の優位と宣伝・価格の語は出さない・リンクはメーカーのサイトだけ
- ふだんの価格: 値下げ速報と同じ判定 (5% 未満・観測 7 日未満は出さない)・直近 30 日の在庫切れ
- 順位の理由: 並びを決めた要素だけ・1 ページで同じ組み合わせを出さない
- ページ: 迷ったらこの 3 つ (重ならない)・タイプの内訳・年齢帯ごとの FAQ
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.dirname(THIS_DIR)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import xmas_page as xp  # noqa: E402
import build_category_hubs as bch  # noqa: E402
from build_feature_lists import ArticleRecord  # noqa: E402

NOW = datetime(2026, 11, 26, 3, 0, tzinfo=timezone.utc)
PAGE3 = bch.XMAS_PAGES["xmas-age-3"]


def _rec(asin, ivs_100=80, best_price=4000, price_amazon=None, fetched_at=None, name=None):
    return ArticleRecord(
        asin=asin, slug=asin, name=name or f"name-{asin}", image=None,
        ivs_score=4.5, ivs_100=ivs_100, best_price=best_price, best_platform="Amazon",
        amazon_url=f"https://www.amazon.co.jp/dp/{asin}", price_amazon=price_amazon,
        fetched_at=fetched_at,
    )


def _raw(name="おもちゃ", tags=(), **extra):
    d = {"product": {"name": name, "asin": "B000000000"}, "tags": list(tags)}
    d.update(extra)
    return d


SOURCES = [
    {"id": "s1", "author": "メーカー公式", "url": "https://www.amazon.co.jp/dp/B000000000"},
    {"id": "s2", "author": "購入者レビュー", "url": "https://review.rakuten.co.jp/x"},
    {"id": "s3", "author": "第三者ライター", "url": "https://blog.example.jp/a"},
    {"id": "s4", "author": "メーカー公式", "url": "https://www.maker.co.jp/item/1"},
]


class PlayTypeTest(unittest.TestCase):
    def test_reads_interests_from_diagnosis_js(self):
        types = xp.load_play_types()
        keys = [t.key for t in types]
        # 診断の 10 タイプ (語の無い「おまかせ」は除く)。JS の書き方が変わって読めなくなったらここで落ちる
        self.assertEqual(keys, ["sense", "build", "pretend", "vehicle", "think", "art", "words",
                                "music", "science", "active"])
        self.assertTrue(all(t.kw and t.label and t.emoji for t in types))

    def test_first_type_in_interests_order_wins(self):
        # 「ブロック」(build) と「電車」(vehicle) の両方 → INTERESTS の順で build
        t = xp.play_type_of(_raw(name="電車 ブロック セット"))
        self.assertEqual(t.key, "build")

    def test_tag_match_and_none(self):
        self.assertEqual(xp.play_type_of(_raw(name="ふしぎな箱", tags=["ボードゲーム"])).key, "think")
        self.assertIsNone(xp.play_type_of(_raw(name="ふしぎな箱", tags=["知育玩具"])))


class VoicesTest(unittest.TestCase):
    def test_voiced_points_first_and_source_note(self):
        raw = _raw(sources=SOURCES, review_signals={
            "high_points": [
                {"text": "公式の説明だけの点", "supporting_source_ids": ["s1"]},
                {"text": "買った人が褒めた点", "supporting_source_ids": ["s2", "s3"]},
                {"text": "もう一つの声", "supporting_source_ids": ["s2"]},
            ],
            "concerns": [{"text": "電池が別売り", "supporting_source_ids": ["s2"]}],
        })
        v = xp.voices_of(raw)
        self.assertEqual([g["text"] for g in v["good"]], ["買った人が褒めた点", "もう一つの声"])
        self.assertEqual(v["good"][0]["sources"], "購入者の声 1・レビュー記事・動画 1")
        self.assertEqual(v["concern"]["text"], "電池が別売り")

    def test_voice_source_count_ignores_official(self):
        raw = _raw(sources=SOURCES, review_signals={
            "high_points": [{"text": "a", "supporting_source_ids": ["s1", "s2"]}],
            "use_scenes": [{"text": "b", "supporting_source_ids": ["s3", "s2"]}],
        })
        self.assertEqual(xp.voice_source_count(raw), 2)

    def test_no_signals(self):
        self.assertIsNone(xp.voices_of(_raw()))
        self.assertEqual(xp.voice_source_count(_raw()), 0)


class OfficialFactTest(unittest.TestCase):
    def test_prefers_safety_and_maker_site(self):
        raw = _raw(sources=SOURCES, claims=[
            {"claim": "丈夫な作り", "category": "durability", "supporting_source_ids": ["s4"], "cross_checked": True},
            {"claim": "誤飲しにくい大きさの部品", "category": "safety", "supporting_source_ids": ["s4"], "cross_checked": True},
        ])
        f = xp.official_fact(raw)
        self.assertEqual(f["text"], "誤飲しにくい大きさの部品")
        self.assertEqual(f["url"], "https://www.maker.co.jp/item/1")
        self.assertEqual(f["site"], "maker.co.jp")

    def test_amazon_hosted_official_has_no_link(self):
        raw = _raw(sources=SOURCES, claims=[
            {"claim": "電池ケースはネジ止め", "category": "safety", "supporting_source_ids": ["s1"]}])
        f = xp.official_fact(raw)
        self.assertEqual(f["text"], "電池ケースはネジ止め")
        self.assertNotIn("url", f)

    def test_skips_promotional_price_and_unofficial(self):
        raw = _raw(sources=SOURCES, claims=[
            {"claim": "他社より優れている", "category": "comparative_advantage", "supporting_source_ids": ["s4"]},
            {"claim": "絶対に壊れない", "category": "durability", "supporting_source_ids": ["s4"]},
            {"claim": "3,000円で買える品質", "category": "quality", "supporting_source_ids": ["s4"]},
            {"claim": "購入者だけが言う安全", "category": "safety", "supporting_source_ids": ["s2"]},
        ])
        self.assertIsNone(xp.official_fact(raw))


def _write_history(root: Path, asin: str, rows: list[dict]) -> None:
    (root / "price_watch" / "history").mkdir(parents=True, exist_ok=True)
    (root / "price_history").mkdir(parents=True, exist_ok=True)
    with open(root / "price_history" / f"{asin}.jsonl", "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


class PriceSignalTest(unittest.TestCase):
    def test_drop_against_usual_price(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            rows = [{"ts": (NOW - timedelta(days=d)).isoformat(), "price": 5000, "source": "amazon"}
                    for d in range(40, 2, -1)]
            _write_history(root, "B0TEST0001", rows)
            rec = _rec("B0TEST0001", price_amazon=4000, fetched_at=NOW.isoformat())
            got = xp.price_signal(rec, now=NOW, price_watch_dir=root / "price_watch",
                                  price_history_dir=root / "price_history")
            self.assertEqual(got["usual"], 5000)
            self.assertEqual(got["drop_yen"], 1000)
            self.assertTrue(got["all_time_low"])

    def test_small_drop_or_short_history_is_not_shown(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            rows = [{"ts": (NOW - timedelta(days=d)).isoformat(), "price": 4100} for d in range(40, 2, -1)]
            _write_history(root, "B0TEST0002", rows)
            rec = _rec("B0TEST0002", price_amazon=4000, fetched_at=NOW.isoformat())  # 2.4% 安いだけ
            self.assertIsNone(xp.price_signal(rec, now=NOW, price_watch_dir=root / "price_watch",
                                              price_history_dir=root / "price_history"))
            _write_history(root, "B0TEST0003", [{"ts": (NOW - timedelta(days=2)).isoformat(), "price": 5000}])
            rec = _rec("B0TEST0003", price_amazon=4000, fetched_at=NOW.isoformat())  # 観測 2 日だけ
            self.assertIsNone(xp.price_signal(rec, now=NOW, price_watch_dir=root / "price_watch",
                                              price_history_dir=root / "price_history"))

    def test_no_daily_observation_or_out_of_stock_is_not_shown(self):
        """日次の観測が無い (記事を書いた時の価格のまま) か、在庫切れと観測した商品は値下げを言わない。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            rows = [{"ts": (NOW - timedelta(days=d)).isoformat(), "price": 5000} for d in range(40, 2, -1)]
            _write_history(root, "B0TEST0005", rows)
            kw = dict(now=NOW, price_watch_dir=root / "price_watch", price_history_dir=root / "price_history")
            self.assertIsNone(xp.price_signal(_rec("B0TEST0005", price_amazon=4000), **kw))
            rec = _rec("B0TEST0005", price_amazon=4000, fetched_at=NOW.isoformat())
            rec.availability = "一時的に在庫切れ; 入荷時期は未定です。"
            self.assertIsNone(xp.price_signal(rec, **kw))

    def test_stockout_within_30_days(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_history(root, "B0TEST0004", [
                {"ts": (NOW - timedelta(days=45)).isoformat(), "price": None, "source": "amazon",
                 "availability": "在庫切れ"}])
            kw = dict(now=NOW, price_watch_dir=root / "price_watch", price_history_dir=root / "price_history")
            self.assertFalse(xp.stocked_out_recently("B0TEST0004", **kw))
            _write_history(root, "B0TEST0004", [
                {"ts": (NOW - timedelta(days=5)).isoformat(), "price": None, "source": "amazon",
                 "availability": "一時的に在庫切れ"}])
            self.assertTrue(xp.stocked_out_recently("B0TEST0004", **kw))


class ShopUrlsTest(unittest.TestCase):
    RAW = _raw(product={"asin": "B0SHOP0001", "name": "x", "prices": {
        "rakuten": {"price": 5000, "url": "https://item.rakuten.co.jp/old"},
        "yahoo": {"price": 5200, "url": "https://store.shopping.yahoo.co.jp/s/s-b0bxslrtpj-1.html"}}})

    def test_price_and_url_share_the_source(self):
        rec = _rec("B0SHOP0001", price_amazon=5000)
        rec.price_rakuten, rec.price_yahoo = 4800, 5200
        good = {"price": "4800", "url": "https://hb.afl.rakuten.co.jp/new", "title": "name-B0SHOP0001"}
        with unittest.mock.patch.object(xp.market_prices, "matched_passes_quality", return_value=True):
            got = xp.shop_urls(rec, self.RAW, {"rakuten": {"B0SHOP0001": good}, "yahoo": {}})
        # 照合結果が通れば、その URL (価格もそこから来ている)
        self.assertEqual(got["rakuten"], "https://hb.afl.rakuten.co.jp/new")
        # 記事 JSON の Yahoo の URL が別の ASIN の出品ページなら出さない
        self.assertNotIn("yahoo", got)

    def test_no_price_no_button(self):
        rec = _rec("B0SHOP0001", price_amazon=5000)
        rec.price_rakuten, rec.price_yahoo = 0, None  # 外れ値として捨てた・価格なし
        self.assertEqual(xp.shop_urls(rec, self.RAW, None), {})

    def test_falls_back_to_article_url_when_match_fails(self):
        rec = _rec("B0SHOP0001", price_amazon=5000)
        rec.price_rakuten = 5000
        with unittest.mock.patch.object(xp.market_prices, "matched_passes_quality", return_value=False):
            got = xp.shop_urls(rec, self.RAW, {"rakuten": {"B0SHOP0001": {"url": "https://x"}}})
        self.assertEqual(got, {"rakuten": "https://item.rakuten.co.jp/old"})


class RankReasonsTest(unittest.TestCase):
    def test_reasons_follow_ranking_factors_and_are_unique(self):
        recs = [_rec("A", ivs_100=90), _rec("B", ivs_100=85), _rec("C", ivs_100=85)]
        recs[0].rank_days_30 = 5
        mm = {"A": 36, "B": 36, "C": 36}
        t = xp.load_play_types()
        vehicle = next(x for x in t if x.key == "vehicle")
        types = {"A": vehicle, "B": None, "C": None}
        got = xp.rank_reasons(recs, PAGE3, mm, types, rank_days_threshold=3)
        self.assertEqual(got["A"][0], "楽天のおもちゃランキングに直近30日で5日入った売れ筋")
        self.assertIn("3歳ちょうどから遊べる", got["B"])
        self.assertIn("知育スコア85（スコア順で2位タイ）", got["B"])
        # B と C は年齢・スコアの順位が同じ → C は値段で分ける (同じ文を 2 回出さない)
        self.assertNotEqual(got["B"], got["C"])
        self.assertEqual(len({tuple(v) for v in got.values()}), 3)

    def test_tied_scores_share_a_rank_and_skip_the_next(self):
        recs = [_rec("A", ivs_100=95), _rec("B", ivs_100=95), _rec("C", ivs_100=90)]
        got = xp.rank_reasons(recs, PAGE3, {}, {}, rank_days_threshold=3)
        self.assertEqual(got["B"][0], "知育スコア95（スコア順で1位タイ）")
        self.assertEqual(got["C"][0], "知育スコア90（スコア順で3位）")

    def test_many_collisions_stay_unique(self):
        """年齢・スコア・価格がすべて同じでも、タイプの順番で分けて同じ文を出さない。"""
        recs = [_rec(f"R{i}", ivs_100=80, best_price=3980) for i in range(4)]
        build = next(x for x in xp.load_play_types() if x.key == "build")
        got = xp.rank_reasons(recs, PAGE3, {r.asin: 36 for r in recs}, {r.asin: build for r in recs},
                              rank_days_threshold=3)
        self.assertEqual(len({tuple(v) for v in got.values()}), 4)

    def test_age_phrases(self):
        self.assertEqual(xp._age_phrase(36, PAGE3), "3歳ちょうどから遊べる")
        self.assertEqual(xp._age_phrase(24, PAGE3), "2歳から遊べて、3歳でも長く使える")
        self.assertEqual(xp._age_phrase(18, PAGE3), "18ヶ月から遊べる息の長いおもちゃ")
        self.assertIsNone(xp._age_phrase(None, PAGE3))


class PageExtrasTest(unittest.TestCase):
    def _build(self, page=PAGE3, n=12, mm_value=36):
        recs = [_rec(f"B0X{i:07d}", ivs_100=90 - i, best_price=2000 + 500 * i,
                     name=("電車のおもちゃ" if i % 2 else "ブロックのおもちゃ") + str(i)) for i in range(n)]
        raw_index = {}
        for i, r in enumerate(recs):
            raw_index[r.asin] = _raw(name=r.name, sources=SOURCES, review_signals={
                "high_points": [{"text": f"良い点{i}", "supporting_source_ids": ["s2"] * (1 + i % 3)}]},
                persona_fit={"not_recommended_for": [f"向かない人{i}"]})
        mm = {r.asin: mm_value for r in recs}
        with tempfile.TemporaryDirectory() as td:
            per, page_extra = xp.build_page_extras(
                recs, raw_index, page, mm, rank_days_threshold=3, now=NOW,
                price_watch_dir=Path(td) / "pw", price_history_dir=Path(td) / "ph",
                generated_at=NOW.isoformat(),
                characters_of=lambda raw: [("プラレール", "/plarail-toys/")] if "電車" in raw["product"]["name"] else [])
        return recs, per, page_extra

    def test_items_and_page(self):
        recs, per, page_extra = self._build()
        first = per[recs[0].asin]
        self.assertEqual(first["play_type"]["key"], "build")
        self.assertEqual(first["not_for"], "向かない人0")
        self.assertEqual(page_extra["band"], "kids")
        self.assertEqual(page_extra["dev_stage_key"], "36m")
        self.assertEqual({t["key"]: t["count"] for t in page_extra["type_counts"]}, {"build": 6, "vehicle": 6})
        self.assertEqual(page_extra["median_price"], 4750)
        # 迷ったらこの 3 つ: 1 位・声の厚さ・(値下げが無いので) いちばん手ごろ。同じ商品を 2 回出さない
        slots = [p["slot"] for p in page_extra["picks"]]
        self.assertEqual(slots, ["overall", "voices", "cheapest"])
        self.assertEqual(len({p["asin"] for p in page_extra["picks"]}), 3)
        self.assertEqual(page_extra["picks"][0]["rank"], 1)

    def test_faq_by_age_band(self):
        _, _, kids = self._build()
        qs = [f["q"] for f in kids["faq"]]
        self.assertIn("男の子と女の子で選び方は変わりますか？", qs)
        self.assertIn("キャラクターのおもちゃでもいいですか？", qs)
        chara = next(f for f in kids["faq"] if f["q"].startswith("キャラクター"))
        self.assertIn("プラレール（6件）", chara["a"])
        self.assertEqual(chara["links"], [{"label": "プラレールのおもちゃ", "url": "/plarail-toys/"}])
        # 下のきょうだい: 全件が 3 歳から → 「0件」と言わない
        sib = next(f for f in kids["faq"] if f["q"].startswith("下のきょうだい"))
        self.assertIn("どれも対象年齢が3歳以上", sib["a"])

        _, _, baby = self._build(page=bch.XMAS_PAGES["xmas-age-0"], mm_value=0)
        bqs = [f["q"] for f in baby["faq"]]
        self.assertIn("小さい部品の誤飲が心配です。何を確かめればいいですか？", bqs)
        self.assertNotIn("男の子と女の子で選び方は変わりますか？", bqs)

        _, _, school = self._build(page=bch.XMAS_PAGES["xmas-age-6"], mm_value=96)
        sqs = [f["q"] for f in school["faq"]]
        self.assertIn("小学生には幼すぎませんか？", sqs)
        too_young = next(f for f in school["faq"] if f["q"].startswith("小学生には"))
        self.assertIn("8歳以上の物は12件", too_young["a"])
        # 「考える・ゲーム」が 0 件なら「親子で遊べるもの」の問いは出さない
        self.assertNotIn("親子で一緒に遊べるものはありますか？", sqs)

        _, _, low = self._build(page=bch.XMAS_PAGES["xmas-age-6"], mm_value=72)
        ans = next(f for f in low["faq"] if f["q"].startswith("小学生には"))["a"]
        self.assertNotIn("0件", ans)
        self.assertIn("どれも対象年齢の下限が8歳未満", ans)


class WriteXmasPagesTest(unittest.TestCase):
    def test_items_carry_xmas_extras(self):
        """run() の流れで、作ったクリスマスページの items に xmas、ページに xmas_page が付く。"""
        recs = [_rec(f"B0Y{i:07d}", ivs_100=95 - i, best_price=3000 + 100 * i) for i in range(bch.XMAS_TOP_N)]
        mm = {r.asin: 36 for r in recs}
        raw_index = {r.asin: _raw(name=f"ブロック{i}") for i, r in enumerate(recs)}
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            counts: dict[str, int] = {}
            bch._write_xmas_pages(recs, mm, {}, {k: set() for k in bch.AGE_HUBS}, out, NOW.isoformat(),
                                  counts, min_ivs=0.0, age_max_per_brand=0, ranking_history={}, today=None,
                                  raw_index=raw_index, price_watch_dir=out / "pw", price_history_dir=out / "ph")
            d = json.loads((out / "xmas-age-3.json").read_text(encoding="utf-8"))
        self.assertEqual(len(d["items"]), bch.XMAS_TOP_N)
        self.assertTrue(all(it["xmas"]["play_type"]["key"] == "build" for it in d["items"]))
        self.assertEqual(d["xmas_page"]["picks"][0]["asin"], d["items"][0]["asin"])


if __name__ == "__main__":
    unittest.main()
