"""scripts/generate_internal_links.py unit tests (#3332 N3 供給側)。"""
from __future__ import annotations

import json
import pathlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import requests

from scripts._seo_sidecar import load_sidecar
from scripts.generate_internal_links import (
    allocate_link_targets,
    build_section_paragraphs,
    build_source_priority,
    call_gemma_internal_links,
    compute_unavailable_targets,
    extract_not_indexed_asins,
    extract_priority_targets,
    is_purchase_unavailable,
    is_semantic_data_stale,
    load_semantic_neighbors,
    run,
    validate_link_suggestions,
)


def _write_json(path: pathlib.Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# load_semantic_neighbors
# --------------------------------------------------------------------------

class LoadSemanticNeighborsTest(unittest.TestCase):
    def test_excludes_meta_and_uppercases_asins(self):
        data = {
            "_meta": {"generated_at": "2026-07-16T20:21:50Z"},
            "aaaaaaaaaa": [{"asin": "bbbbbbbbbb", "score": 0.98}],
        }
        result = load_semantic_neighbors(data)
        self.assertNotIn("_META", result)
        self.assertEqual(result["AAAAAAAAAA"], [{"asin": "BBBBBBBBBB", "score": 0.98}])

    def test_skips_malformed_entries(self):
        data = {"aaaaaaaaaa": [{"asin": "bbbbbbbbbb"}, "not a dict", {"asin": 123, "score": 0.9}]}
        result = load_semantic_neighbors(data)
        self.assertEqual(result["AAAAAAAAAA"], [])


# --------------------------------------------------------------------------
# 鮮度チェック
# --------------------------------------------------------------------------

class IsSemanticDataStaleTest(unittest.TestCase):
    def test_fresh_data_not_stale(self):
        now = datetime(2026, 7, 17, 10, 0, 0, tzinfo=timezone.utc)
        meta = {"generated_at": _iso(now - timedelta(hours=1))}
        self.assertFalse(is_semantic_data_stale(meta, 24, now=now))

    def test_stale_data_detected(self):
        now = datetime(2026, 7, 17, 10, 0, 0, tzinfo=timezone.utc)
        meta = {"generated_at": _iso(now - timedelta(hours=25))}
        self.assertTrue(is_semantic_data_stale(meta, 24, now=now))

    def test_boundary_exactly_at_limit_not_stale(self):
        now = datetime(2026, 7, 17, 10, 0, 0, tzinfo=timezone.utc)
        meta = {"generated_at": _iso(now - timedelta(hours=24))}
        self.assertFalse(is_semantic_data_stale(meta, 24, now=now))

    def test_missing_generated_at_is_stale(self):
        self.assertTrue(is_semantic_data_stale({}, 24))

    def test_malformed_generated_at_is_stale(self):
        self.assertTrue(is_semantic_data_stale({"generated_at": "not-a-date"}, 24))


# --------------------------------------------------------------------------
# extract_priority_targets / extract_not_indexed_asins
# --------------------------------------------------------------------------

class ExtractPriorityTargetsTest(unittest.TestCase):
    def setUp(self):
        self.article_index = {
            "BBBBBBBBBB": pathlib.Path("b.json"),
            "CCCCCCCCCC": pathlib.Path("c.json"),
            "DDDDDDDDDD": pathlib.Path("d.json"),
        }

    def test_only_priority_states_included(self):
        not_indexed = [
            {"url": "https://navi.omcha.jp/products/bbbbbbbbbb/", "coverage_state": "検出 - インデックス未登録"},
            {"url": "https://navi.omcha.jp/products/cccccccccc/", "coverage_state": "URL が Google に認識されていません"},
            {"url": "https://navi.omcha.jp/products/dddddddddd/", "coverage_state": "クロール済み - インデックス未登録"},
        ]
        result = extract_priority_targets(not_indexed, self.article_index)
        self.assertEqual(result, {"BBBBBBBBBB", "CCCCCCCCCC"})

    def test_404_state_excluded(self):
        not_indexed = [
            {"url": "https://navi.omcha.jp/products/bbbbbbbbbb/", "coverage_state": "見つかりませんでした（404）"},
        ]
        self.assertEqual(extract_priority_targets(not_indexed, self.article_index), set())

    def test_asin_without_local_article_excluded(self):
        not_indexed = [
            {"url": "https://navi.omcha.jp/products/zzzzzzzzzz/", "coverage_state": "検出 - インデックス未登録"},
        ]
        self.assertEqual(extract_priority_targets(not_indexed, self.article_index), set())


class ExtractNotIndexedAsinsTest(unittest.TestCase):
    def test_collects_all_states(self):
        not_indexed = [
            {"url": "https://navi.omcha.jp/products/bbbbbbbbbb/", "coverage_state": "検出 - インデックス未登録"},
            {"url": "https://navi.omcha.jp/products/cccccccccc/", "coverage_state": "見つかりませんでした（404）"},
        ]
        self.assertEqual(extract_not_indexed_asins(not_indexed), {"BBBBBBBBBB", "CCCCCCCCCC"})


# --------------------------------------------------------------------------
# build_source_priority
# --------------------------------------------------------------------------

class BuildSourcePriorityTest(unittest.TestCase):
    def test_gsc_impressions_desc_first(self):
        article_index = {
            "AAAAAAAAAA": pathlib.Path("2026-01-01-AAAAAAAAAA.json"),
            "BBBBBBBBBB": pathlib.Path("2026-01-01-BBBBBBBBBB.json"),
        }
        by_page = [
            {"page": "https://navi.omcha.jp/products/aaaaaaaaaa/", "impressions": 10},
            {"page": "https://navi.omcha.jp/products/bbbbbbbbbb/", "impressions": 100},
        ]
        ordered = build_source_priority(article_index, by_page, set())
        self.assertEqual(ordered, ["BBBBBBBBBB", "AAAAAAAAAA"])

    def test_uncrawled_articles_last(self):
        article_index = {
            "AAAAAAAAAA": pathlib.Path("2026-01-01-AAAAAAAAAA.json"),
            "BBBBBBBBBB": pathlib.Path("2026-01-01-BBBBBBBBBB.json"),
            "CCCCCCCCCC": pathlib.Path("2026-01-01-CCCCCCCCCC.json"),
        }
        ordered = build_source_priority(article_index, [], {"CCCCCCCCCC"})
        self.assertEqual(ordered, ["AAAAAAAAAA", "BBBBBBBBBB", "CCCCCCCCCC"])

    def test_zero_impressions_rows_do_not_count_as_gsc_priority(self):
        article_index = {
            "AAAAAAAAAA": pathlib.Path("2026-01-01-AAAAAAAAAA.json"),
            "BBBBBBBBBB": pathlib.Path("2026-01-01-BBBBBBBBBB.json"),
        }
        by_page = [{"page": "https://navi.omcha.jp/products/bbbbbbbbbb/", "impressions": 0}]
        ordered = build_source_priority(article_index, by_page, set())
        self.assertEqual(ordered, ["AAAAAAAAAA", "BBBBBBBBBB"])


# --------------------------------------------------------------------------
# allocate_link_targets (グローバル割当; 設計の肝)
# --------------------------------------------------------------------------

class AllocateLinkTargetsTest(unittest.TestCase):
    def test_min_score_excludes_below_threshold(self):
        neighbors = {"S1": [{"asin": "T1", "score": 0.80}]}
        allocated = allocate_link_targets(["S1"], neighbors, {"T1"}, min_score=0.85, max_inbound=20)
        self.assertEqual(allocated, {})

    def test_score_at_threshold_included(self):
        neighbors = {"S1": [{"asin": "T1", "score": 0.85}]}
        allocated = allocate_link_targets(["S1"], neighbors, {"T1"}, min_score=0.85, max_inbound=20)
        self.assertEqual(allocated["S1"], [{"asin": "T1", "score": 0.85}])

    def test_max_inbound_cuts_off(self):
        neighbors = {
            "S1": [{"asin": "T1", "score": 0.9}],
            "S2": [{"asin": "T1", "score": 0.9}],
        }
        allocated = allocate_link_targets(["S1", "S2"], neighbors, {"T1"}, min_score=0.85, max_inbound=1)
        self.assertIn("S1", allocated)
        self.assertNotIn("S2", allocated)

    def test_least_linked_target_preferred_over_raw_score(self):
        neighbors = {
            "S1": [{"asin": "T1", "score": 0.90}, {"asin": "T2", "score": 0.95}],
            "S2": [{"asin": "T1", "score": 0.90}, {"asin": "T2", "score": 0.95}],
        }
        allocated = allocate_link_targets(
            ["S1", "S2"], neighbors, {"T1", "T2"}, min_score=0.85, max_inbound=20,
            max_candidates_per_source=1,
        )
        # S1: 両方 inbound=0 なので score 降順で T2 が勝つ
        self.assertEqual(allocated["S1"], [{"asin": "T2", "score": 0.95}])
        # S2: T2 は既に inbound=1、T1 は inbound=0 なので被リンク数の少ない T1 が優先される
        self.assertEqual(allocated["S2"], [{"asin": "T1", "score": 0.90}])

    def test_tie_break_score_desc_then_asin_asc(self):
        neighbors = {"S1": [{"asin": "T2", "score": 0.90}, {"asin": "T1", "score": 0.90}]}
        allocated = allocate_link_targets(
            ["S1"], neighbors, {"T1", "T2"}, min_score=0.85, max_inbound=20, max_candidates_per_source=2,
        )
        # 同スコアは asin 昇順
        self.assertEqual([c["asin"] for c in allocated["S1"]], ["T1", "T2"])

    def test_self_link_excluded(self):
        neighbors = {"S1": [{"asin": "S1", "score": 0.99}]}
        allocated = allocate_link_targets(["S1"], neighbors, {"S1"}, min_score=0.85, max_inbound=20)
        self.assertEqual(allocated, {})

    def test_max_3_candidates_per_source(self):
        neighbors = {"S1": [{"asin": f"T{i}", "score": 0.9} for i in range(8)]}
        allocated = allocate_link_targets(
            ["S1"], neighbors, {f"T{i}" for i in range(8)}, min_score=0.85, max_inbound=20,
        )
        self.assertEqual(len(allocated["S1"]), 3)

    def test_deterministic_across_repeated_calls(self):
        neighbors = {
            "S1": [{"asin": "T1", "score": 0.90}, {"asin": "T2", "score": 0.95}],
            "S2": [{"asin": "T1", "score": 0.90}, {"asin": "T2", "score": 0.95}],
            "S3": [{"asin": "T2", "score": 0.86}, {"asin": "T1", "score": 0.87}],
        }
        first = allocate_link_targets(["S1", "S2", "S3"], neighbors, {"T1", "T2"}, min_score=0.85, max_inbound=20)
        second = allocate_link_targets(["S1", "S2", "S3"], neighbors, {"T1", "T2"}, min_score=0.85, max_inbound=20)
        self.assertEqual(first, second)

    def test_non_priority_targets_excluded(self):
        neighbors = {"S1": [{"asin": "T1", "score": 0.99}]}
        allocated = allocate_link_targets(["S1"], neighbors, set(), min_score=0.85, max_inbound=20)
        self.assertEqual(allocated, {})


# --------------------------------------------------------------------------
# 販売終了先の除外
# --------------------------------------------------------------------------

class IsPurchaseUnavailableTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.per_asin_root = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_gone_status_without_direct_links_is_unavailable(self):
        _write_json(self.per_asin_root / "BBBBBBBBBB" / "amazon.json", {"status": "gone"})
        article = {"product": {"prices": {}}}
        self.assertTrue(is_purchase_unavailable(article, self.per_asin_root, "BBBBBBBBBB"))

    def test_gone_status_with_rakuten_direct_link_is_available(self):
        _write_json(self.per_asin_root / "BBBBBBBBBB" / "amazon.json", {"status": "gone"})
        article = {"product": {"prices": {"rakuten": {"url": "https://rakuten/x", "is_search": False}}}}
        self.assertFalse(is_purchase_unavailable(article, self.per_asin_root, "BBBBBBBBBB"))

    def test_search_only_link_does_not_count_as_direct(self):
        _write_json(self.per_asin_root / "BBBBBBBBBB" / "amazon.json", {"status": "gone"})
        article = {"product": {"prices": {"yahoo": {"url": "https://yahoo/search", "is_search": True}}}}
        self.assertTrue(is_purchase_unavailable(article, self.per_asin_root, "BBBBBBBBBB"))

    def test_status_not_gone_is_available(self):
        _write_json(self.per_asin_root / "BBBBBBBBBB" / "amazon.json", {"status": ""})
        article = {"product": {"prices": {}}}
        self.assertFalse(is_purchase_unavailable(article, self.per_asin_root, "BBBBBBBBBB"))

    def test_missing_snapshot_is_available(self):
        article = {"product": {"prices": {}}}
        self.assertFalse(is_purchase_unavailable(article, self.per_asin_root, "NOPE"))


class ComputeUnavailableTargetsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.per_asin_root = self.root / "per_asin"
        self.articles_dir = self.root / "articles"

    def tearDown(self):
        self._tmp.cleanup()

    def test_only_flags_gone_and_no_direct_link(self):
        _write_json(self.articles_dir / "AAAAAAAAAA.json", {"product": {"prices": {}}})
        _write_json(self.articles_dir / "BBBBBBBBBB.json", {"product": {"prices": {"rakuten": {"url": "x"}}}})
        _write_json(self.per_asin_root / "AAAAAAAAAA" / "amazon.json", {"status": "gone"})
        _write_json(self.per_asin_root / "BBBBBBBBBB" / "amazon.json", {"status": "gone"})
        article_index = {
            "AAAAAAAAAA": self.articles_dir / "AAAAAAAAAA.json",
            "BBBBBBBBBB": self.articles_dir / "BBBBBBBBBB.json",
        }
        result = compute_unavailable_targets({"AAAAAAAAAA", "BBBBBBBBBB"}, article_index, self.per_asin_root)
        self.assertEqual(result, {"AAAAAAAAAA"})


# --------------------------------------------------------------------------
# build_section_paragraphs
# --------------------------------------------------------------------------

class BuildSectionParagraphsTest(unittest.TestCase):
    def test_str_section_wrapped_as_single_item(self):
        narrative = {"why_this_product": "テキスト本文です。"}
        result = build_section_paragraphs(narrative)
        self.assertEqual(result["why_this_product"], ["テキスト本文です。"])

    def test_list_section_preserves_indices(self):
        narrative = {"daily_use": ["段落0", "", "段落2"]}
        result = build_section_paragraphs(narrative)
        self.assertEqual(result["daily_use"], ["段落0", "", "段落2"])

    def test_disallowed_sections_excluded(self):
        narrative = {"lead": "リード文", "safety_note": "安全", "closing": "結び"}
        self.assertEqual(build_section_paragraphs(narrative), {})

    def test_empty_or_blank_section_excluded(self):
        narrative = {"gift_appeal": "", "how_to_choose": ["", "  "]}
        self.assertEqual(build_section_paragraphs(narrative), {})


# --------------------------------------------------------------------------
# validate_link_suggestions (build_post._inject_internal_links と同じ規則)
# --------------------------------------------------------------------------

class ValidateLinkSuggestionsTest(unittest.TestCase):
    def _narrative(self, **overrides):
        base = {
            "why_this_product": "木製レールで拡張性抜群のセットです。",
            "how_to_choose": ["対象年齢は3歳からです。", "サイズ展開も豊富です。"],
        }
        base.update(overrides)
        return base

    def _sugg(self, **overrides):
        base = {
            "section": "why_this_product",
            "paragraph_index": 0,
            "anchor_text": "木製レールで拡張性",
            "target_asin": "BBBBBBBBBB",
            "reason": "関連商品",
        }
        base.update(overrides)
        return base

    def test_valid_suggestion_kept(self):
        kept, dropped = validate_link_suggestions(
            [self._sugg()], self._narrative(), "AAAAAAAAAA", {"BBBBBBBBBB"}, set(),
        )
        self.assertEqual(len(kept), 1)
        self.assertEqual(dropped, 0)
        self.assertEqual(kept[0]["target_asin"], "BBBBBBBBBB")

    def test_verbatim_mismatch_dropped(self):
        kept, dropped = validate_link_suggestions(
            [self._sugg(anchor_text="木製レールが拡張")], self._narrative(), "AAAAAAAAAA", {"BBBBBBBBBB"}, set(),
        )
        self.assertEqual(kept, [])
        self.assertEqual(dropped, 1)

    def test_disallowed_section_dropped(self):
        kept, dropped = validate_link_suggestions(
            [self._sugg(section="lead")], self._narrative(lead="木製レールで拡張性抜群のセットです。"),
            "AAAAAAAAAA", {"BBBBBBBBBB"}, set(),
        )
        self.assertEqual(kept, [])
        self.assertEqual(dropped, 1)

    def test_paragraph_index_out_of_range_dropped(self):
        kept, dropped = validate_link_suggestions(
            [self._sugg(section="how_to_choose", paragraph_index=5, anchor_text="対象年齢は3歳")],
            self._narrative(), "AAAAAAAAAA", {"BBBBBBBBBB"}, set(),
        )
        self.assertEqual(kept, [])
        self.assertEqual(dropped, 1)

    def test_str_section_nonzero_index_dropped(self):
        kept, dropped = validate_link_suggestions(
            [self._sugg(paragraph_index=1)], self._narrative(), "AAAAAAAAAA", {"BBBBBBBBBB"}, set(),
        )
        self.assertEqual(kept, [])
        self.assertEqual(dropped, 1)

    def test_self_link_dropped(self):
        kept, dropped = validate_link_suggestions(
            [self._sugg(target_asin="AAAAAAAAAA")], self._narrative(), "AAAAAAAAAA", {"AAAAAAAAAA"}, set(),
        )
        self.assertEqual(kept, [])
        self.assertEqual(dropped, 1)

    def test_unpublished_target_dropped(self):
        kept, dropped = validate_link_suggestions(
            [self._sugg(target_asin="ZZZZZZZZZZ")], self._narrative(), "AAAAAAAAAA", {"BBBBBBBBBB"}, set(),
        )
        self.assertEqual(kept, [])
        self.assertEqual(dropped, 1)

    def test_nested_inside_existing_markdown_link_dropped(self):
        narrative = self._narrative(why_this_product="[木製レールで拡張性](https://example.com/) を採用しています。")
        kept, dropped = validate_link_suggestions(
            [self._sugg()], narrative, "AAAAAAAAAA", {"BBBBBBBBBB"}, set(),
        )
        self.assertEqual(kept, [])
        self.assertEqual(dropped, 1)

    def test_anchor_too_short_dropped(self):
        narrative = self._narrative(why_this_product="木製です。とても良い。")
        kept, dropped = validate_link_suggestions(
            [self._sugg(anchor_text="木製")], narrative, "AAAAAAAAAA", {"BBBBBBBBBB"}, set(),
        )
        self.assertEqual(kept, [])
        self.assertEqual(dropped, 1)

    def test_anchor_too_long_dropped(self):
        long_text = "あ" * 31
        narrative = self._narrative(why_this_product=long_text + "です。")
        kept, dropped = validate_link_suggestions(
            [self._sugg(anchor_text=long_text)], narrative, "AAAAAAAAAA", {"BBBBBBBBBB"}, set(),
        )
        self.assertEqual(kept, [])
        self.assertEqual(dropped, 1)

    def test_anchor_boundary_lengths_accepted(self):
        narrative_min = self._narrative(why_this_product="あ" * 4 + "です。")
        kept_min, _ = validate_link_suggestions(
            [self._sugg(anchor_text="あ" * 4)], narrative_min, "AAAAAAAAAA", {"BBBBBBBBBB"}, set(),
        )
        self.assertEqual(len(kept_min), 1)

        narrative_max = self._narrative(why_this_product="あ" * 30 + "です。")
        kept_max, _ = validate_link_suggestions(
            [self._sugg(anchor_text="あ" * 30)], narrative_max, "AAAAAAAAAA", {"BBBBBBBBBB"}, set(),
        )
        self.assertEqual(len(kept_max), 1)

    def test_denylist_generic_word_dropped(self):
        for word in ("この商品", "こちら", "詳しくは", "関連記事"):
            narrative = self._narrative(why_this_product=f"{word}の詳細をご確認ください。")
            kept, dropped = validate_link_suggestions(
                [self._sugg(anchor_text=word)], narrative, "AAAAAAAAAA", {"BBBBBBBBBB"}, set(),
            )
            self.assertEqual(kept, [], msg=f"word={word}")
            self.assertEqual(dropped, 1)

    def test_price_amount_dropped(self):
        for amount in ("3,520円", "数百円"):
            narrative = self._narrative(why_this_product=f"この商品は{amount}ほどで購入可能です。")
            kept, dropped = validate_link_suggestions(
                [self._sugg(anchor_text=amount if len(amount) >= 4 else amount + "ほど")],
                narrative, "AAAAAAAAAA", {"BBBBBBBBBB"}, set(),
            )
            self.assertEqual(kept, [], msg=f"amount={amount}")

    def test_point_reward_dropped(self):
        narrative = self._narrative(why_this_product="購入するとポイント還元があります。")
        kept, dropped = validate_link_suggestions(
            [self._sugg(anchor_text="ポイント還元があり")], narrative, "AAAAAAAAAA", {"BBBBBBBBBB"}, set(),
        )
        self.assertEqual(kept, [])
        self.assertEqual(dropped, 1)

    def test_static_ellipse_and_focus_point_not_dropped(self):
        narrative = self._narrative(
            why_this_product="楕円形のピースも含まれる形合わせパズルです。",
            how_to_choose=["説明書の着目ポイントを確認しましょう。", "サイズ展開も豊富です。"],
        )
        kept1, dropped1 = validate_link_suggestions(
            [self._sugg(anchor_text="楕円形のピース")], narrative, "AAAAAAAAAA", {"BBBBBBBBBB"}, set(),
        )
        self.assertEqual(len(kept1), 1)
        self.assertEqual(dropped1, 0)

        kept2, dropped2 = validate_link_suggestions(
            [self._sugg(section="how_to_choose", paragraph_index=0, anchor_text="着目ポイントを確認")],
            narrative, "AAAAAAAAAA", {"BBBBBBBBBB"}, set(),
        )
        self.assertEqual(len(kept2), 1)
        self.assertEqual(dropped2, 0)

    def test_markdown_breaking_chars_dropped(self):
        narrative = self._narrative(why_this_product="テスト[注釈]付きの表現を含む文章です。")
        kept, dropped = validate_link_suggestions(
            [self._sugg(anchor_text="テスト[注釈]付き")], narrative, "AAAAAAAAAA", {"BBBBBBBBBB"}, set(),
        )
        self.assertEqual(kept, [])
        self.assertEqual(dropped, 1)

    def test_max_3_links_per_article(self):
        narrative = {
            "why_this_product": "木製レールで拡張性抜群のセットです。",
            "daily_use": "毎日の遊びに取り入れやすい設計です。",
            "gift_appeal": "贈り物にも喜ばれる商品です。",
            "how_to_choose": ["対象年齢は3歳からです。第四の候補文章です。"],
        }
        suggestions = [
            self._sugg(section="why_this_product", anchor_text="木製レールで拡張性", target_asin="B1"),
            self._sugg(section="daily_use", paragraph_index=0, anchor_text="毎日の遊びに取り入れ", target_asin="B2"),
            self._sugg(section="gift_appeal", paragraph_index=0, anchor_text="贈り物にも喜ばれる", target_asin="B3"),
            self._sugg(section="how_to_choose", paragraph_index=0, anchor_text="対象年齢は3歳から", target_asin="B4"),
        ]
        kept, dropped = validate_link_suggestions(
            suggestions, narrative, "AAAAAAAAAA", {"B1", "B2", "B3", "B4"}, set(),
        )
        self.assertEqual(len(kept), 3)
        self.assertEqual(dropped, 1)

    def test_one_per_paragraph(self):
        suggestions = [
            self._sugg(target_asin="B1"),
            self._sugg(target_asin="B2"),  # 同じ section:paragraph_index
        ]
        kept, dropped = validate_link_suggestions(
            suggestions, self._narrative(), "AAAAAAAAAA", {"B1", "B2"}, set(),
        )
        self.assertEqual(len(kept), 1)
        self.assertEqual(dropped, 1)

    def test_one_per_section(self):
        narrative = self._narrative(how_to_choose=["木製レールで拡張性抜群です。", "サイズ展開も豊富です。"])
        suggestions = [
            self._sugg(section="how_to_choose", paragraph_index=0, anchor_text="木製レールで拡張性", target_asin="B1"),
            self._sugg(section="how_to_choose", paragraph_index=1, anchor_text="サイズ展開も豊富", target_asin="B2"),
        ]
        kept, dropped = validate_link_suggestions(
            suggestions, narrative, "AAAAAAAAAA", {"B1", "B2"}, set(),
        )
        self.assertEqual(len(kept), 1)
        self.assertEqual(dropped, 1)

    def test_duplicate_target_dropped(self):
        narrative = {
            "why_this_product": "木製レールで拡張性抜群のセットです。",
            "daily_use": "毎日の遊びに取り入れやすい設計です。",
        }
        suggestions = [
            self._sugg(section="why_this_product", anchor_text="木製レールで拡張性", target_asin="BBBBBBBBBB"),
            self._sugg(section="daily_use", paragraph_index=0, anchor_text="毎日の遊びに取り入れ", target_asin="BBBBBBBBBB"),
        ]
        kept, dropped = validate_link_suggestions(
            suggestions, narrative, "AAAAAAAAAA", {"BBBBBBBBBB"}, set(),
        )
        self.assertEqual(len(kept), 1)
        self.assertEqual(dropped, 1)

    def test_purchase_unavailable_target_dropped(self):
        kept, dropped = validate_link_suggestions(
            [self._sugg()], self._narrative(), "AAAAAAAAAA", {"BBBBBBBBBB"}, {"BBBBBBBBBB"},
        )
        self.assertEqual(kept, [])
        self.assertEqual(dropped, 1)

    def test_not_a_list_returns_empty(self):
        self.assertEqual(validate_link_suggestions(None, self._narrative(), "A", set(), set()), ([], 0))
        self.assertEqual(validate_link_suggestions("nope", self._narrative(), "A", set(), set()), ([], 0))

    def test_malformed_narrative_drops_all(self):
        kept, dropped = validate_link_suggestions([self._sugg()], None, "A", {"BBBBBBBBBB"}, set())
        self.assertEqual(kept, [])
        self.assertEqual(dropped, 1)


# --------------------------------------------------------------------------
# call_gemma_internal_links (requests mocked via a fake session)
# --------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, json_body, status=200):
        self._json = json_body
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")

    def json(self):
        return self._json


class _FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def post(self, url, json=None, timeout=None):
        self.calls += 1
        resp = self._responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp


class CallGemmaInternalLinksTest(unittest.TestCase):
    def test_parses_valid_response(self):
        inner = json.dumps({"links": [{"section": "why_this_product", "paragraph_index": 0,
                                        "anchor_text": "アンカー", "target_asin": "B", "reason": "r"}]})
        session = _FakeSession([_FakeResponse({"response": inner})])
        result = call_gemma_internal_links("sections", "candidates", "http://ollama", "gemma4", session, sleeper=lambda s: None)
        self.assertEqual(len(result["links"]), 1)
        self.assertIsNone(result["error"])

    def test_retries_then_succeeds(self):
        inner = json.dumps({"links": []})
        session = _FakeSession([requests.ConnectionError("boom"), _FakeResponse({"response": inner})])
        result = call_gemma_internal_links("s", "c", "http://ollama", "gemma4", session, sleeper=lambda s: None)
        self.assertIsNone(result["error"])
        self.assertEqual(session.calls, 2)

    def test_gives_up_after_max_retries(self):
        session = _FakeSession([
            requests.ConnectionError("boom1"),
            requests.ConnectionError("boom2"),
            requests.ConnectionError("boom3"),
        ])
        result = call_gemma_internal_links("s", "c", "http://ollama", "gemma4", session, sleeper=lambda s: None)
        self.assertIsNotNone(result["error"])
        self.assertEqual(result["links"], [])

    def test_malformed_json_response_is_retried_and_fails(self):
        session = _FakeSession([
            _FakeResponse({"response": "not valid json"}),
            _FakeResponse({"response": "still not json"}),
            _FakeResponse({"response": "nope"}),
        ])
        result = call_gemma_internal_links("s", "c", "http://ollama", "gemma4", session, sleeper=lambda s: None)
        self.assertIsNotNone(result["error"])


# --------------------------------------------------------------------------
# run() end-to-end
# --------------------------------------------------------------------------

class RunEndToEndTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.articles_dir = self.root / "articles"
        self.history_dir = self.root / "gsc_history"
        self.per_asin_root = self.root / "per_asin"  # わざと存在させない (販売終了なし扱い)
        self.semantic_related_path = self.root / "semantic_related.json"
        self.census_path = self.root / "census.json"
        self.manifest_path = self.root / "manifest.json"
        self.articles_dir.mkdir()
        self.history_dir.mkdir()

        now = datetime.now(timezone.utc)
        _write_json(self.semantic_related_path, {
            "_meta": {"generated_at": _iso(now - timedelta(hours=1)), "model": "cl-nagoya/ruri-v3-310m"},
            "aaaaaaaaaa": [{"asin": "bbbbbbbbbb", "score": 0.95}],
        })
        _write_json(self.census_path, {
            "not_indexed_urls": [
                {"url": "https://navi.omcha.jp/products/bbbbbbbbbb/", "coverage_state": "検出 - インデックス未登録"},
            ],
        })
        _write_json(self.articles_dir / "2026-01-01-AAAAAAAAAA.json", {
            "narrative": {"why_this_product": "木製レールで拡張性抜群のセットです。"},
        })
        _write_json(self.articles_dir / "2026-01-02-BBBBBBBBBB.json", {
            "title": "対象商品B", "meta_description": "対象商品Bの説明です。",
            "product": {"name": "商品B", "prices": {}},
        })

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, session, **kwargs):
        return run(
            articles_dir=self.articles_dir,
            semantic_related_path=self.semantic_related_path,
            census_path=self.census_path,
            history_dir=self.history_dir,
            per_asin_root=self.per_asin_root,
            manifest_path=self.manifest_path,
            session=session,
            sleeper=lambda s: None,
            **kwargs,
        )

    def test_writes_sidecar_for_valid_generation(self):
        inner = json.dumps({"links": [{
            "section": "why_this_product", "paragraph_index": 0,
            "anchor_text": "木製レールで拡張性", "target_asin": "BBBBBBBBBB", "reason": "関連商品",
        }]})
        session = _FakeSession([_FakeResponse({"response": inner})])

        summary = self._run(session)
        self.assertEqual(summary["errors"], 0)
        self.assertEqual(summary["links_written"], 1)
        self.assertEqual(summary["articles_with_links"], 1)

        sidecar = load_sidecar(self.articles_dir / "2026-01-01-AAAAAAAAAA.seo.json")
        self.assertEqual(len(sidecar["internal_link_suggestions"]), 1)
        self.assertEqual(sidecar["internal_link_suggestions"][0]["target_asin"], "BBBBBBBBBB")

        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["links"]), 1)
        self.assertEqual(manifest["links"][0]["source_asin"], "AAAAAAAAAA")
        self.assertEqual(manifest["links"][0]["semantic_score"], 0.95)

    def test_dry_run_does_not_write_sidecar_but_writes_manifest(self):
        inner = json.dumps({"links": [{
            "section": "why_this_product", "paragraph_index": 0,
            "anchor_text": "木製レールで拡張性", "target_asin": "BBBBBBBBBB", "reason": "関連商品",
        }]})
        session = _FakeSession([_FakeResponse({"response": inner})])

        summary = self._run(session, dry_run=True)
        self.assertEqual(summary["links_written"], 1)
        self.assertFalse((self.articles_dir / "2026-01-01-AAAAAAAAAA.seo.json").exists())
        self.assertTrue(self.manifest_path.exists())
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        self.assertTrue(manifest["dry_run"])

    def test_zero_links_does_not_write_sidecar_key(self):
        inner = json.dumps({"links": []})
        session = _FakeSession([_FakeResponse({"response": inner})])

        summary = self._run(session)
        self.assertEqual(summary["links_written"], 0)
        self.assertFalse((self.articles_dir / "2026-01-01-AAAAAAAAAA.seo.json").exists())

    def test_broken_gemma_json_does_not_stop_other_articles(self):
        # ソース記事を追加 (両方とも B へリンクできる)
        _write_json(self.articles_dir / "2026-01-01-CCCCCCCCCC.json", {
            "narrative": {"why_this_product": "頑丈な設計で長く使えるおもちゃです。"},
        })
        _write_json(self.semantic_related_path, {
            "_meta": {"generated_at": _iso(datetime.now(timezone.utc) - timedelta(hours=1))},
            "aaaaaaaaaa": [{"asin": "bbbbbbbbbb", "score": 0.95}],
            "cccccccccc": [{"asin": "bbbbbbbbbb", "score": 0.90}],
        })
        good_inner = json.dumps({"links": [{
            "section": "why_this_product", "paragraph_index": 0,
            "anchor_text": "頑丈な設計で長く使える", "target_asin": "BBBBBBBBBB", "reason": "関連商品",
        }]})
        # AAAAAAAAAA (stem 順で先) は3回とも壊れたJSON、CCCCCCCCCC は成功
        session = _FakeSession([
            _FakeResponse({"response": "broken"}),
            _FakeResponse({"response": "broken"}),
            _FakeResponse({"response": "broken"}),
            _FakeResponse({"response": good_inner}),
        ])

        summary = self._run(session, max_inbound=20)
        self.assertEqual(summary["errors"], 1)
        self.assertEqual(summary["links_written"], 1)
        self.assertFalse((self.articles_dir / "2026-01-01-AAAAAAAAAA.seo.json").exists())
        self.assertTrue((self.articles_dir / "2026-01-01-CCCCCCCCCC.seo.json").exists())

    def test_without_force_existing_suggestions_skipped_no_network_call(self):
        _write_json(self.articles_dir / "2026-01-01-AAAAAAAAAA.seo.json", {
            "internal_link_suggestions": [{"section": "why_this_product", "paragraph_index": 0,
                                            "anchor_text": "既存", "target_asin": "BBBBBBBBBB", "reason": ""}],
        })
        session = mock.Mock()
        summary = self._run(session)
        self.assertEqual(summary["processed"], 0)
        session.post.assert_not_called()

    def test_force_reprocesses_existing_suggestions(self):
        _write_json(self.articles_dir / "2026-01-01-AAAAAAAAAA.seo.json", {
            "internal_link_suggestions": [{"section": "why_this_product", "paragraph_index": 0,
                                            "anchor_text": "既存", "target_asin": "BBBBBBBBBB", "reason": ""}],
        })
        inner = json.dumps({"links": []})
        session = _FakeSession([_FakeResponse({"response": inner})])
        summary = self._run(session, force=True)
        self.assertEqual(summary["processed"], 1)

    def test_stale_semantic_data_aborts(self):
        _write_json(self.semantic_related_path, {
            "_meta": {"generated_at": _iso(datetime.now(timezone.utc) - timedelta(hours=48))},
            "aaaaaaaaaa": [{"asin": "bbbbbbbbbb", "score": 0.95}],
        })
        session = mock.Mock()
        summary = self._run(session)
        self.assertTrue(summary["aborted"])
        self.assertEqual(summary["processed"], 0)
        session.post.assert_not_called()

    def test_purchase_unavailable_target_excluded_from_allocation(self):
        _write_json(self.per_asin_root / "BBBBBBBBBB" / "amazon.json", {"status": "gone"})
        session = mock.Mock()
        summary = self._run(session)
        self.assertEqual(summary["processed"], 0)  # 候補が無いのでソースは処理対象にならない
        session.post.assert_not_called()

    def test_limit_restricts_number_processed(self):
        _write_json(self.articles_dir / "2026-01-01-CCCCCCCCCC.json", {
            "narrative": {"why_this_product": "頑丈な設計で長く使えるおもちゃです。"},
        })
        _write_json(self.semantic_related_path, {
            "_meta": {"generated_at": _iso(datetime.now(timezone.utc) - timedelta(hours=1))},
            "aaaaaaaaaa": [{"asin": "bbbbbbbbbb", "score": 0.95}],
            "cccccccccc": [{"asin": "bbbbbbbbbb", "score": 0.90}],
        })
        inner = json.dumps({"links": []})
        session = _FakeSession([_FakeResponse({"response": inner})])
        summary = self._run(session, limit=1)
        self.assertEqual(summary["processed"], 1)

    def test_no_priority_targets_returns_zero_summary_without_network(self):
        _write_json(self.census_path, {"not_indexed_urls": []})
        session = mock.Mock()
        summary = self._run(session)
        self.assertEqual(summary["processed"], 0)
        session.post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
