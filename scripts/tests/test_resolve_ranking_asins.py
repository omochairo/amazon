"""resolve_ranking_asins.py の単体テスト (Issue #810 Phase 1)。

Creator API は呼ばずに FakeAPI でモックする。カバレッジ:
1. _collect_unmatched_jans: matched 済を除外し未マッチ JAN を rank 順・重複排除で抽出
2. resolve_jan_to_asin: externalIds.eans/upcs の JAN 一致で ASIN 抽出 / 不一致は ""
3. resolve_ranking_asins: resolved / unresolved / already-covered / 同 ASIN 重複の分岐
4. --limit による解決上限
5. #2818: itemNumber 優先抽出 / search_index 既定 "All" / upcs フォールバック
"""
from __future__ import annotations

import inspect
import os
import pathlib
import sys
import tempfile
import unittest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.dirname(THIS_DIR)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import resolve_ranking_asins as rr  # noqa: E402


class FakeAPI:
    """search_items(keywords=JAN) → JAN→ASIN マップを引いて searchResult を返す。"""

    def __init__(self, jan_to_asin: dict, extra_noise: bool = True):
        self.jan_to_asin = jan_to_asin
        self.extra_noise = extra_noise
        self.calls = []

    def search_items(self, keywords=None, search_index="Toys", item_count=10,
                     item_page=1, resources=None):
        self.calls.append(keywords)
        items = []
        # 先頭に JAN 不一致のノイズ item を混ぜ、eans 照合が効くことを検証する。
        if self.extra_noise:
            items.append({
                "asin": "BNOISE0001",
                "itemInfo": {"externalIds": {"eans": {"displayValues": ["4900000000000"]}}},
            })
        asin = self.jan_to_asin.get(keywords)
        if asin:
            items.append({
                "asin": asin,
                "itemInfo": {"externalIds": {"eans": {"displayValues": [keywords]}}},
            })
        return {"searchResult": {"items": items}}


class CollectUnmatchedJansTest(unittest.TestCase):
    def test_skips_matched_and_dedups_in_rank_order(self):
        items = [
            {"rank": 1, "matched_asin": "B0EXIST", "itemCaption": "JAN 4904810000001"},
            {"rank": 2, "matched_asin": None, "title": "ブロック 4904810000002"},
            {"rank": 3, "matched_asin": None, "itemCaption": "コード 4904810000002 重複"},
            {"rank": 4, "matched_asin": None, "title": "JAN なし商品"},
            {"rank": 5, "matched_asin": None, "itemCaption": "EAN 4904810000003 です"},
        ]
        jans = rr._collect_unmatched_jans(items)
        self.assertEqual(
            jans,
            [("4904810000002", 2, "ブロック 4904810000002"),
             ("4904810000003", 5, None)],
        )


class CollectUnmatchedJansItemNumberTest(unittest.TestCase):
    def test_prefers_item_number_over_caption(self):
        # #2818 対策1: itemNumber がある場合はそちらを優先して JAN を抽出する。
        items = [
            {"rank": 1, "matched_asin": None, "itemNumber": "4904810000002",
             "itemCaption": "別の型番 4904810000001", "title": ""},
        ]
        jans = rr._collect_unmatched_jans(items)
        self.assertEqual(jans, [("4904810000002", 1, "")])


class ResolveJanToAsinTest(unittest.TestCase):
    def test_matches_via_externalids(self):
        api = FakeAPI({"4904810000002": "B0NEW002"})
        self.assertEqual(rr.resolve_jan_to_asin(api, "4904810000002"), "B0NEW002")

    def test_no_match_returns_empty(self):
        api = FakeAPI({})
        self.assertEqual(rr.resolve_jan_to_asin(api, "4904810999999"), "")

    def test_api_exception_returns_empty(self):
        class BoomAPI:
            def search_items(self, **kw):
                raise RuntimeError("boom")
        self.assertEqual(rr.resolve_jan_to_asin(BoomAPI(), "4904810000002"), "")

    def test_matches_via_upc_when_eans_absent(self):
        # #2818 対策3: UPC-A のみ登録された商品 (eans が空・upcs にだけ識別子がある) でも
        # 解決できる (_external_ids_of が eans と upcs の両方を見るため)。
        class UpcOnlyAPI:
            def search_items(self, **kw):
                return {"searchResult": {"items": [
                    {"asin": "B0UPCONLY", "itemInfo": {"externalIds": {
                        "upcs": {"displayValues": ["012345678905"]},
                    }}},
                ]}}
        self.assertEqual(rr.resolve_jan_to_asin(UpcOnlyAPI(), "012345678905"), "B0UPCONLY")

    def test_default_search_index_is_all(self):
        # #2818 対策2: Toys 固定だとベビー/ホビー等の別カテゴリ商品を取りこぼすため All に緩和。
        self.assertEqual(
            inspect.signature(rr.resolve_jan_to_asin).parameters["search_index"].default,
            "All",
        )
        self.assertEqual(
            inspect.signature(rr.resolve_ranking_asins).parameters["search_index"].default,
            "All",
        )


class ResolveRankingAsinsTest(unittest.TestCase):
    def setUp(self):
        self.items = [
            {"rank": 1, "matched_asin": None, "title": "A 4904810000010"},
            {"rank": 2, "matched_asin": None, "title": "B 4904810000020"},
            {"rank": 3, "matched_asin": None, "title": "C 4904810000030"},
        ]

    def test_resolved_unresolved_and_covered_split(self):
        api = FakeAPI({
            "4904810000010": "B0NEW010",   # 新規
            "4904810000020": "B0COVERED",  # 既 covered → skip
            # 4904810000030 はマップ外 → unresolved
        })
        covered = {"B0COVERED"}
        m = rr.resolve_ranking_asins(self.items, api, covered, sleep=0)
        self.assertEqual(m["new_asins"], ["B0NEW010"])
        self.assertEqual([r["jan"] for r in m["unresolved"]], ["4904810000030"])
        self.assertEqual([s["asin"] for s in m["skipped_already_covered"]], ["B0COVERED"])
        self.assertEqual(m["input_unmatched_jans"], 3)

    def test_covered_match_is_case_insensitive(self):
        api = FakeAPI({"4904810000010": "b0new010"})
        m = rr.resolve_ranking_asins(self.items[:1], api, {"B0NEW010"}, sleep=0)
        self.assertEqual(m["new_asins"], [])
        self.assertEqual(len(m["skipped_already_covered"]), 1)

    def test_same_asin_from_two_jans_dedups(self):
        items = [
            {"rank": 1, "matched_asin": None, "title": "A 4904810000010"},
            {"rank": 2, "matched_asin": None, "title": "B 4904810000020"},
        ]
        api = FakeAPI({"4904810000010": "B0SAME", "4904810000020": "B0SAME"})
        m = rr.resolve_ranking_asins(items, api, set(), sleep=0)
        self.assertEqual(m["new_asins"], ["B0SAME"])

    def test_limit_caps_resolution(self):
        api = FakeAPI({
            "4904810000010": "B0NEW010",
            "4904810000020": "B0NEW020",
            "4904810000030": "B0NEW030",
        })
        m = rr.resolve_ranking_asins(self.items, api, set(), limit=1, sleep=0)
        self.assertEqual(m["input_unmatched_jans"], 1)
        self.assertEqual(m["new_asins"], ["B0NEW010"])
        self.assertEqual(api.calls, ["4904810000010"])

    def test_limit_reports_pre_limit_candidate_count(self):
        # #2818 対策0: --limit で切り詰める前の候補プール総数 (3件) を
        # input_unmatched_jans (切り詰め後=1件) と区別して manifest に残す。
        # 旧実装は candidates_before_limit が無く、smoke run (--limit 1) の
        # ログから「候補は1件しか無かった」と誤読される事故があった。
        api = FakeAPI({"4904810000010": "B0NEW010"})
        m = rr.resolve_ranking_asins(self.items, api, set(), limit=1, sleep=0)
        self.assertEqual(m["jan_candidates_before_limit"], 3)
        self.assertEqual(m["input_unmatched_jans"], 1)

    def test_no_limit_pre_and_post_counts_match(self):
        api = FakeAPI({})
        m = rr.resolve_ranking_asins(self.items, api, set(), limit=0, sleep=0)
        self.assertEqual(m["jan_candidates_before_limit"], 3)
        self.assertEqual(m["input_unmatched_jans"], 3)


class LoadCoveredAsinsTest(unittest.TestCase):
    def test_articles_suffix_and_per_asin_dirs(self):
        with tempfile.TemporaryDirectory() as td:
            adir = pathlib.Path(td) / "articles"
            adir.mkdir()
            (adir / "2026-05-26-B01MUBACGI.json").write_text("{}", encoding="utf-8")
            (adir / "2026-05-26-4910762175.json").write_text("{}", encoding="utf-8")  # ISBN-10
            (adir / "2026-05-26-B01MUBACGI.quality.json").write_text("{}", encoding="utf-8")  # 除外
            (adir / "notes.json").write_text("{}", encoding="utf-8")  # ASIN サフィックス無し
            proot = pathlib.Path(td) / "per_asin"
            (proot / "B0NEWSNAP1").mkdir(parents=True)
            covered = rr._load_covered_asins(str(adir), str(proot))
            self.assertEqual(covered, {"B01MUBACGI", "4910762175", "B0NEWSNAP1"})

    def test_missing_dirs_return_empty(self):
        self.assertEqual(rr._load_covered_asins("/no/such/a", "/no/such/p"), set())

    def test_article_asins_excludes_per_asin(self):
        # #810 Phase 1.5: article-only set は per_asin を含まない (pool prune の正しさ)。
        with tempfile.TemporaryDirectory() as td:
            adir = pathlib.Path(td) / "articles"
            adir.mkdir()
            (adir / "2026-05-26-B01MUBACGI.json").write_text("{}", encoding="utf-8")
            proot = pathlib.Path(td) / "per_asin"
            (proot / "B0RANKING1").mkdir(parents=True)
            self.assertEqual(rr._load_article_asins(str(adir)), {"B01MUBACGI"})
            self.assertEqual(rr._load_per_asin_asins(str(proot)), {"B0RANKING1"})
            # union は両方 (resolve step の skip 判定はこちらを使う)
            self.assertEqual(
                rr._load_covered_asins(str(adir), str(proot)),
                {"B01MUBACGI", "B0RANKING1"},
            )


class SearchPoolMiningTest(unittest.TestCase):
    """#810 Phase 2: ランキング + 楽天 Search items を混ぜた JAN mining。

    Search items は matched_asin を持たない (= 全件候補) ので、ランキングが
    マッチ済でも Search 側の JAN から新規 ASIN を解決できることを検証する。
    main() が ranking_items + search_items を concat して resolve に渡す挙動を、
    その concat 済リストを直接 resolve_ranking_asins に与えて再現する。
    """

    def test_search_item_jan_resolves_when_ranking_matched(self):
        ranking = [
            # ランキングは既存記事にマッチ済 → JAN mining から除外される
            {"rank": 1, "matched_asin": "B0EXIST001", "title": "既存 4904810000010"},
        ]
        search = [
            # Search item は matched_asin 無し → 候補。itemCaption に JAN。
            {"title": "新作ブロック", "itemCaption": "型番 JAN 4904810000099 知育",
             "source": "Rakuten"},
        ]
        api = FakeAPI({"4904810000099": "B0SEARCH99"})
        m = rr.resolve_ranking_asins(ranking + search, api, set(), sleep=0)
        self.assertEqual(m["new_asins"], ["B0SEARCH99"])
        self.assertEqual(m["input_unmatched_jans"], 1)

    def test_same_jan_in_ranking_and_search_dedups(self):
        # 同 JAN がランキング(未マッチ)と Search の双方にあっても 1 回だけ解決。
        ranking = [{"rank": 2, "matched_asin": None, "title": "A 4904810000020"}]
        search = [{"title": "A 別出品", "itemCaption": "4904810000020", "source": "Rakuten"}]
        api = FakeAPI({"4904810000020": "B0DUP00020"})
        m = rr.resolve_ranking_asins(ranking + search, api, set(), sleep=0)
        self.assertEqual(m["new_asins"], ["B0DUP00020"])
        self.assertEqual(m["input_unmatched_jans"], 1)
        self.assertEqual(api.calls, ["4904810000020"])  # 1 回だけ


class UpdateRankingPoolTest(unittest.TestCase):
    def test_appends_new_and_dedups_preserving_order(self):
        pool = rr.update_ranking_pool(["B0OLD001", "B0OLD002"], ["B0NEW003", "B0OLD001"], set())
        # B0OLD001 は既存なので重複追加されない、順序は既存→新規
        self.assertEqual(pool, ["B0OLD001", "B0OLD002", "B0NEW003"])

    def test_prunes_article_covered_only(self):
        # B0OLD001 が記事化された → pool から除く。per_asin 済でも article でなければ残す。
        pool = rr.update_ranking_pool(["B0OLD001", "B0OLD002"], [], {"B0OLD001"})
        self.assertEqual(pool, ["B0OLD002"])

    def test_prune_is_case_insensitive(self):
        pool = rr.update_ranking_pool(["b0old001"], [], {"B0OLD001"})
        self.assertEqual(pool, [])

    def test_skips_blank_and_non_str(self):
        pool = rr.update_ranking_pool(["B0OK00001", "", None, "  "], [123, "B0OK00002"], set())
        self.assertEqual(pool, ["B0OK00001", "B0OK00002"])

    def test_resolved_then_covered_lifecycle(self):
        # Run 1: resolve B0X → pool=[B0X]。Run 2: B0X 記事化 → pool=[]。
        p1 = rr.update_ranking_pool([], ["B0X0000001"], set())
        self.assertEqual(p1, ["B0X0000001"])
        p2 = rr.update_ranking_pool(p1, [], {"B0X0000001"})
        self.assertEqual(p2, [])


# --------------------------------------------------------------------------
# #2818 対策4c (title fuzzy) + #3332 N5-V1 (vision ゲート)
# --------------------------------------------------------------------------

def _fuzzy_candidate(asin, title, price, image="https://amazon/img.jpg"):
    """Creator API searchItems 応答 item の最小構造を組み立てるヘルパ。"""
    return {
        "asin": asin,
        "itemInfo": {"title": {"displayValue": title}},
        "offersV2": {"listings": [{"price": {"money": {"amount": price}}}]},
        "images": {"primary": {"large": {"url": image}}},
    }


class FakeFuzzyAPI:
    """search_items(keywords=正規化タイトル) → 固定の候補リストを返す (title fuzzy 用)。

    ``candidates`` は「検索クエリの部分文字列 → 候補 item リスト」のマップ。実際の
    ``_normalize_title_for_search`` を通した後のクエリに対し、キーが含まれていれば
    そのリストを返す (楽天タイトルの装飾除去を経ても引けることを検証するため)。
    """

    def __init__(self, candidates: dict):
        self.candidates = candidates
        self.calls = []

    def search_items(self, keywords=None, search_index="All", item_count=10,
                     item_page=1, resources=None):
        self.calls.append(keywords)
        for key, items in self.candidates.items():
            if key in (keywords or ""):
                return {"searchResult": {"items": items}}
        return {"searchResult": {"items": []}}


class FakeVisionClient:
    """URL ペア → 類似度を固定で返すモック (image_similarity 経由で使う)。"""

    def __init__(self, url_to_vector):
        self.url_to_vector = url_to_vector

    def embed_images(self, image_urls):
        return [self.url_to_vector.get(u) for u in image_urls]


class NormalizeTitleTest(unittest.TestCase):
    def test_strips_decorations_and_truncates(self):
        title = "【送料無料】レゴ (LEGO) デュプロ 10913 基礎ブロック"
        out = rr._normalize_title_for_search(title)
        self.assertNotIn("【", out)
        self.assertNotIn("(", out)
        self.assertIn("レゴ", out)
        self.assertIn("10913", out)

    def test_empty_returns_empty(self):
        self.assertEqual(rr._normalize_title_for_search(""), "")


class ModelTokensTest(unittest.TestCase):
    def test_extracts_only_digit_bearing_tokens(self):
        # "LEGO" (純アルファベット) は落ち、数字を含む型番トークンだけ残る。
        self.assertEqual(rr._extract_model_tokens("LEGO 10913 N700 ab abc"), {"10913", "N700"})

    def test_pure_alpha_tokens_excluded(self):
        self.assertEqual(rr._extract_model_tokens("DUPLO CLASSIC"), set())

    def test_none_returns_empty(self):
        self.assertEqual(rr._extract_model_tokens(""), set())


class EvaluateTitleFuzzyCandidateTest(unittest.TestCase):
    def test_all_guardrails_pass(self):
        cand = _fuzzy_candidate("B0LEGO0001", "レゴ LEGO デュプロ 10913", 3000)
        evald = rr.evaluate_title_fuzzy_candidate(
            "レゴ LEGO デュプロ 10913 基礎ブロックセット", 3200, cand
        )
        self.assertTrue(evald["passed_guardrails"])
        self.assertTrue(evald["brand_match"])
        self.assertTrue(evald["model_token_match"])
        self.assertTrue(evald["price_ok"])
        self.assertIn("10913", evald["common_tokens"])

    def test_brand_mismatch_fails(self):
        # 候補が別ブランド (タカラトミー) → brand_match False → 不採用。
        cand = _fuzzy_candidate("B0OTHER001", "タカラトミー プラレール 10913", 3000)
        evald = rr.evaluate_title_fuzzy_candidate("レゴ LEGO デュプロ 10913", 3000, cand)
        self.assertFalse(evald["brand_match"])
        self.assertFalse(evald["passed_guardrails"])

    def test_model_token_mismatch_fails(self):
        cand = _fuzzy_candidate("B0LEGO0002", "レゴ LEGO クラシック 99999", 3000)
        evald = rr.evaluate_title_fuzzy_candidate("レゴ LEGO デュプロ 10913", 3000, cand)
        self.assertTrue(evald["brand_match"])
        self.assertFalse(evald["model_token_match"])
        self.assertFalse(evald["passed_guardrails"])

    def test_price_out_of_band_fails(self):
        # 楽天 3000 円に対し候補 10000 円 (+233%) は ±40% 外 → price_ok False。
        cand = _fuzzy_candidate("B0LEGO0003", "レゴ LEGO デュプロ 10913", 10000)
        evald = rr.evaluate_title_fuzzy_candidate("レゴ LEGO デュプロ 10913", 3000, cand)
        self.assertFalse(evald["price_ok"])
        self.assertFalse(evald["passed_guardrails"])

    def test_missing_price_fails_closed(self):
        cand = _fuzzy_candidate("B0LEGO0004", "レゴ LEGO デュプロ 10913", 0)
        evald = rr.evaluate_title_fuzzy_candidate("レゴ LEGO デュプロ 10913", 0, cand)
        self.assertFalse(evald["price_ok"])
        self.assertFalse(evald["passed_guardrails"])

    def test_unknown_brand_both_sides_fails(self):
        # 双方 unknown ブランドでは brand_match を成立させない (誤マッチ防止)。
        cand = _fuzzy_candidate("B0NOBRAND1", "謎の知育玩具 ABCD1234", 3000)
        evald = rr.evaluate_title_fuzzy_candidate("別の謎おもちゃ ABCD1234", 3000, cand)
        self.assertFalse(evald["brand_match"])
        self.assertFalse(evald["passed_guardrails"])


class CollectUnmatchedNoJanTest(unittest.TestCase):
    def test_selects_only_unmatched_without_jan(self):
        items = [
            {"rank": 1, "matched_asin": "B0EXIST", "title": "既存 4904810000001"},
            {"rank": 2, "matched_asin": None, "title": "JAN あり 4904810000002"},
            {"rank": 3, "matched_asin": None, "title": "JAN なし プール玩具"},
            {"rank": 4, "matched_asin": None, "title": ""},  # title 空 → 除外
        ]
        out = rr._collect_unmatched_no_jan(items)
        self.assertEqual([it["rank"] for it in out], [3])


class ResolveTitleFuzzyTest(unittest.TestCase):
    def setUp(self):
        # JAN 抽出不可 (数字が JAN パターンでない)・未マッチの item。
        self.items = [
            {"rank": 1, "matched_asin": None,
             "title": "レゴ LEGO デュプロ 10913 基礎ブロック", "price": 3200,
             "image": "https://rakuten/lego.jpg"},
        ]

    def test_resolves_when_guardrails_pass_shadow_mode(self):
        api = FakeFuzzyAPI({"10913": [_fuzzy_candidate("B0LEGO0001", "レゴ LEGO デュプロ 10913", 3000)]})
        m = rr.resolve_title_fuzzy(self.items, api, covered=set(), seen_asins=set(), sleep=0)
        self.assertEqual([e["asin"] for e in m["title_fuzzy_resolved"]], ["B0LEGO0001"])
        entry = m["title_fuzzy_resolved"][0]
        self.assertEqual(entry["match_method"], "title_fuzzy")
        # shadow モード (vision_client 無し) では image_sim=None でも採用される。
        self.assertIsNone(entry["image_sim"])
        self.assertTrue(entry["vision_gate_passed"])

    def test_rejects_when_no_guardrail_pass(self):
        api = FakeFuzzyAPI({"10913": [_fuzzy_candidate("B0OTHER001", "タカラトミー プラレール 10913", 3000)]})
        m = rr.resolve_title_fuzzy(self.items, api, covered=set(), seen_asins=set(), sleep=0)
        self.assertEqual(m["title_fuzzy_resolved"], [])
        self.assertEqual(m["title_fuzzy_rejected"][0]["reason"], "no_guardrail_pass")

    def test_skips_already_covered_and_seen(self):
        api = FakeFuzzyAPI({"10913": [_fuzzy_candidate("B0LEGO0001", "レゴ LEGO デュプロ 10913", 3000)]})
        m = rr.resolve_title_fuzzy(self.items, api, covered={"B0LEGO0001"}, seen_asins=set(), sleep=0)
        self.assertEqual(m["title_fuzzy_resolved"], [])

    def test_vision_enforce_rejects_low_similarity(self):
        api = FakeFuzzyAPI({"10913": [
            _fuzzy_candidate("B0LEGO0001", "レゴ LEGO デュプロ 10913", 3000, image="https://amazon/lego.jpg")
        ]})
        # 楽天画像 ⟂ Amazon 画像 (直交) → 類似度 0.0 < 0.9 → enforce で不採用。
        vc = FakeVisionClient({
            "https://rakuten/lego.jpg": [1.0, 0.0],
            "https://amazon/lego.jpg": [0.0, 1.0],
        })
        m = rr.resolve_title_fuzzy(
            self.items, api, covered=set(), seen_asins=set(), sleep=0,
            vision_client=vc, vision_gate_mode="enforce", vision_min_score=0.9,
        )
        self.assertEqual(m["title_fuzzy_resolved"], [])
        self.assertEqual(m["title_fuzzy_rejected"][0]["reason"], "vision_gate_rejected")
        self.assertAlmostEqual(m["title_fuzzy_rejected"][0]["image_sim"], 0.0)

    def test_vision_enforce_accepts_high_similarity(self):
        api = FakeFuzzyAPI({"10913": [
            _fuzzy_candidate("B0LEGO0001", "レゴ LEGO デュプロ 10913", 3000, image="https://amazon/lego.jpg")
        ]})
        vc = FakeVisionClient({
            "https://rakuten/lego.jpg": [1.0, 0.0],
            "https://amazon/lego.jpg": [1.0, 0.0],  # 同一方向 → 類似度 1.0
        })
        m = rr.resolve_title_fuzzy(
            self.items, api, covered=set(), seen_asins=set(), sleep=0,
            vision_client=vc, vision_gate_mode="enforce", vision_min_score=0.9,
        )
        self.assertEqual([e["asin"] for e in m["title_fuzzy_resolved"]], ["B0LEGO0001"])
        self.assertAlmostEqual(m["title_fuzzy_resolved"][0]["image_sim"], 1.0)

    def test_vision_shadow_records_but_does_not_gate(self):
        # shadow モードでは低類似度でも採用し、image_sim を記録するのみ。
        api = FakeFuzzyAPI({"10913": [
            _fuzzy_candidate("B0LEGO0001", "レゴ LEGO デュプロ 10913", 3000, image="https://amazon/lego.jpg")
        ]})
        vc = FakeVisionClient({
            "https://rakuten/lego.jpg": [1.0, 0.0],
            "https://amazon/lego.jpg": [0.0, 1.0],  # 直交 = 0.0
        })
        m = rr.resolve_title_fuzzy(
            self.items, api, covered=set(), seen_asins=set(), sleep=0,
            vision_client=vc, vision_gate_mode="shadow", vision_min_score=0.9,
        )
        self.assertEqual([e["asin"] for e in m["title_fuzzy_resolved"]], ["B0LEGO0001"])
        self.assertAlmostEqual(m["title_fuzzy_resolved"][0]["image_sim"], 0.0)

    def test_limit_caps_candidates_and_records_pre_limit(self):
        items = [
            dict(self.items[0], rank=1),
            {"rank": 2, "matched_asin": None, "title": "レゴ LEGO クラシック 11717", "price": 4000},
        ]
        api = FakeFuzzyAPI({
            "10913": [_fuzzy_candidate("B0LEGO0001", "レゴ LEGO デュプロ 10913", 3000)],
            "11717": [_fuzzy_candidate("B0LEGO0002", "レゴ LEGO クラシック 11717", 4000)],
        })
        m = rr.resolve_title_fuzzy(items, api, covered=set(), seen_asins=set(), limit=1, sleep=0)
        self.assertEqual(m["title_fuzzy_candidates_before_limit"], 2)
        self.assertEqual(m["title_fuzzy_input"], 1)


class ResolveRankingAsinsTitleFuzzyIntegrationTest(unittest.TestCase):
    def test_disabled_by_default_no_title_fuzzy_key(self):
        items = [{"rank": 1, "matched_asin": None, "title": "プール玩具 大型"}]
        api = FakeAPI({})
        m = rr.resolve_ranking_asins(items, api, set(), sleep=0)
        self.assertNotIn("title_fuzzy", m)

    def test_enabled_merges_fuzzy_new_asins(self):
        # JAN あり item (JAN 経路) + JAN 無し item (fuzzy 経路) を混在させ、
        # new_asins に両方が入ることを検証する。
        items = [
            {"rank": 1, "matched_asin": None, "title": "A 4904810000010"},  # JAN
            {"rank": 2, "matched_asin": None, "title": "レゴ LEGO デュプロ 10913", "price": 3000},  # fuzzy
        ]

        class CombinedAPI:
            """JAN 検索と title fuzzy 検索を 1 つの API で両対応する。"""

            def search_items(self, keywords=None, **kw):
                if keywords == "4904810000010":
                    return {"searchResult": {"items": [
                        {"asin": "B0JAN00010", "itemInfo": {
                            "externalIds": {"eans": {"displayValues": ["4904810000010"]}}}},
                    ]}}
                if "10913" in (keywords or ""):
                    return {"searchResult": {"items": [
                        _fuzzy_candidate("B0LEGO0001", "レゴ LEGO デュプロ 10913", 3000)]}}
                return {"searchResult": {"items": []}}

        m = rr.resolve_ranking_asins(
            items, CombinedAPI(), set(), sleep=0, enable_title_fuzzy=True,
        )
        self.assertIn("title_fuzzy", m)
        self.assertIn("B0JAN00010", m["new_asins"])
        self.assertIn("B0LEGO0001", m["new_asins"])


if __name__ == "__main__":
    unittest.main()
