"""Unit tests for #3332 (N3 消費側) — build_post._inject_internal_links().

sidecar (``<slug>.seo.json``) が持ち込む ``internal_link_suggestions`` を
narrative 本文へ決定的な文字列置換で注入する処理のテスト。LLM 由来の提案は
一切信用しないため、検証規則に落ちた提案は無条件 drop (fail-closed) される
ことを確認する。
"""
from __future__ import annotations

import copy
import pathlib
import sys
import unittest

THIS_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = THIS_DIR.parent.parent  # amazon-clone/
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from build_post import _inject_internal_links  # type: ignore[import-not-found]


SITE_BASE = ""

# 公開記事 index (article_index) の最小フィクスチャ。
ARTICLE_INDEX = {
    "B0TARGET01": {"slug": "2026-01-01-b0target01", "ivs_score_100": 80,
                   "ivs_score": 4.0, "name": "対象商品1", "image": "", "amazon_price": 1000},
    "B0TARGET02": {"slug": "2026-01-02-b0target02", "ivs_score_100": 75,
                   "ivs_score": 3.8, "name": "対象商品2", "image": "", "amazon_price": 2000},
    "B0TARGET03": {"slug": "2026-01-03-b0target03", "ivs_score_100": 70,
                   "ivs_score": 3.5, "name": "対象商品3", "image": "", "amazon_price": 3000},
    "B0TARGET04": {"slug": "2026-01-04-b0target04", "ivs_score_100": 65,
                   "ivs_score": 3.2, "name": "対象商品4", "image": "", "amazon_price": 4000},
}


def _make_data(narrative: dict, suggestions: list, self_asin: str = "B0SELF0001") -> dict:
    return {
        "product": {"asin": self_asin},
        "narrative": narrative,
        "internal_link_suggestions": suggestions,
    }


class NoOpTests(unittest.TestCase):
    def test_missing_key_is_noop(self):
        data = _make_data({"why_this_product": "本文です。"}, None)
        del data["internal_link_suggestions"]
        narrative_before = copy.deepcopy(data["narrative"])
        result = _inject_internal_links(data, ARTICLE_INDEX, SITE_BASE)
        self.assertEqual(result, (0, 0))
        self.assertEqual(data["narrative"], narrative_before)

    def test_empty_list_is_noop(self):
        data = _make_data({"why_this_product": "本文です。"}, [])
        narrative_before = copy.deepcopy(data["narrative"])
        result = _inject_internal_links(data, ARTICLE_INDEX, SITE_BASE)
        self.assertEqual(result, (0, 0))
        self.assertEqual(data["narrative"], narrative_before)

    def test_non_list_is_noop(self):
        data = _make_data({"why_this_product": "本文です。"}, "not-a-list")
        narrative_before = copy.deepcopy(data["narrative"])
        result = _inject_internal_links(data, ARTICLE_INDEX, SITE_BASE)
        self.assertEqual(result, (0, 0))
        self.assertEqual(data["narrative"], narrative_before)


class InjectionTests(unittest.TestCase):
    def test_injects_into_string_paragraph(self):
        data = _make_data(
            {"why_this_product": "木製レールの拡張性が高く飽きずに長く遊べます。"},
            [{
                "section": "why_this_product", "paragraph_index": 0,
                "anchor_text": "木製レールの拡張性", "target_asin": "b0target01",
                "reason": "similar",
            }],
        )
        injected, dropped = _inject_internal_links(data, ARTICLE_INDEX, SITE_BASE)
        self.assertEqual((injected, dropped), (1, 0))
        self.assertEqual(
            data["narrative"]["why_this_product"],
            "[木製レールの拡張性](/products/b0target01/)が高く飽きずに長く遊べます。",
        )

    def test_injects_into_list_paragraph_by_index(self):
        data = _make_data(
            {"daily_use": ["朝の準備に役立ちます。", "木製レールの拡張性が魅力です。"]},
            [{
                "section": "daily_use", "paragraph_index": 1,
                "anchor_text": "木製レールの拡張性", "target_asin": "B0TARGET01",
                "reason": "similar",
            }],
        )
        injected, dropped = _inject_internal_links(data, ARTICLE_INDEX, SITE_BASE)
        self.assertEqual((injected, dropped), (1, 0))
        self.assertEqual(data["narrative"]["daily_use"][0], "朝の準備に役立ちます。")
        self.assertEqual(
            data["narrative"]["daily_use"][1],
            "[木製レールの拡張性](/products/b0target01/)が魅力です。",
        )

    def test_site_base_path_is_prefixed(self):
        data = _make_data(
            {"why_this_product": "木製レールの拡張性が高いです。"},
            [{
                "section": "why_this_product", "paragraph_index": 0,
                "anchor_text": "木製レールの拡張性", "target_asin": "B0TARGET01",
            }],
        )
        _inject_internal_links(data, ARTICLE_INDEX, "/amazon")
        self.assertEqual(
            data["narrative"]["why_this_product"],
            "[木製レールの拡張性](/amazon/products/b0target01/)が高いです。",
        )

    def test_only_first_occurrence_is_replaced(self):
        data = _make_data(
            {"why_this_product": "木製レールの拡張性が魅力。木製レールの拡張性は他にない。"},
            [{
                "section": "why_this_product", "paragraph_index": 0,
                "anchor_text": "木製レールの拡張性", "target_asin": "B0TARGET01",
            }],
        )
        injected, dropped = _inject_internal_links(data, ARTICLE_INDEX, SITE_BASE)
        self.assertEqual((injected, dropped), (1, 0))
        self.assertEqual(
            data["narrative"]["why_this_product"],
            "[木製レールの拡張性](/products/b0target01/)が魅力。木製レールの拡張性は他にない。",
        )


class DropRuleTests(unittest.TestCase):
    def test_drops_non_verbatim_anchor(self):
        data = _make_data(
            {"why_this_product": "全然違う本文です。"},
            [{
                "section": "why_this_product", "paragraph_index": 0,
                "anchor_text": "木製レールの拡張性", "target_asin": "B0TARGET01",
            }],
        )
        result = _inject_internal_links(data, ARTICLE_INDEX, SITE_BASE)
        self.assertEqual(result, (0, 1))
        self.assertEqual(data["narrative"]["why_this_product"], "全然違う本文です。")

    def test_drops_disallowed_section(self):
        data = _make_data(
            {"safety_note": "木製レールの拡張性が高いです。"},
            [{
                "section": "safety_note", "paragraph_index": 0,
                "anchor_text": "木製レールの拡張性", "target_asin": "B0TARGET01",
            }],
        )
        result = _inject_internal_links(data, ARTICLE_INDEX, SITE_BASE)
        self.assertEqual(result, (0, 1))
        self.assertEqual(data["narrative"]["safety_note"], "木製レールの拡張性が高いです。")

    def test_drops_out_of_range_paragraph_index_for_list(self):
        data = _make_data(
            {"daily_use": ["朝の準備に役立ちます。"]},
            [{
                "section": "daily_use", "paragraph_index": 5,
                "anchor_text": "朝の準備に役立ちます", "target_asin": "B0TARGET01",
            }],
        )
        result = _inject_internal_links(data, ARTICLE_INDEX, SITE_BASE)
        self.assertEqual(result, (0, 1))

    def test_drops_nonzero_paragraph_index_for_string_section(self):
        data = _make_data(
            {"why_this_product": "木製レールの拡張性が高いです。"},
            [{
                "section": "why_this_product", "paragraph_index": 1,
                "anchor_text": "木製レールの拡張性", "target_asin": "B0TARGET01",
            }],
        )
        result = _inject_internal_links(data, ARTICLE_INDEX, SITE_BASE)
        self.assertEqual(result, (0, 1))

    def test_drops_unpublished_target(self):
        data = _make_data(
            {"why_this_product": "木製レールの拡張性が高いです。"},
            [{
                "section": "why_this_product", "paragraph_index": 0,
                "anchor_text": "木製レールの拡張性", "target_asin": "B0NOTPUBLISH",
            }],
        )
        result = _inject_internal_links(data, ARTICLE_INDEX, SITE_BASE)
        self.assertEqual(result, (0, 1))

    def test_drops_self_link(self):
        data = _make_data(
            {"why_this_product": "木製レールの拡張性が高いです。"},
            [{
                "section": "why_this_product", "paragraph_index": 0,
                "anchor_text": "木製レールの拡張性", "target_asin": "b0self0001",
            }],
            self_asin="B0SELF0001",
        )
        result = _inject_internal_links(data, ARTICLE_INDEX, SITE_BASE)
        self.assertEqual(result, (0, 1))

    def test_drops_when_anchor_overlaps_existing_markdown_link(self):
        data = _make_data(
            {"why_this_product": "[木製レールの拡張性](https://example.com/)が高いです。"},
            [{
                "section": "why_this_product", "paragraph_index": 0,
                "anchor_text": "木製レールの拡張性", "target_asin": "B0TARGET01",
            }],
        )
        result = _inject_internal_links(data, ARTICLE_INDEX, SITE_BASE)
        self.assertEqual(result, (0, 1))
        self.assertEqual(
            data["narrative"]["why_this_product"],
            "[木製レールの拡張性](https://example.com/)が高いです。",
        )

    def test_drops_when_anchor_overlaps_html_tag(self):
        # anchor_text の出現区間が <b> タグの区間と literal に重なるケース。
        data = _make_data(
            {"why_this_product": "<b>木製レールの拡張性</b>です。"},
            [{
                "section": "why_this_product", "paragraph_index": 0,
                "anchor_text": "<b>木製レール", "target_asin": "B0TARGET01",
            }],
        )
        result = _inject_internal_links(data, ARTICLE_INDEX, SITE_BASE)
        self.assertEqual(result, (0, 1))
        self.assertEqual(
            data["narrative"]["why_this_product"], "<b>木製レールの拡張性</b>です。"
        )

    def test_drops_anchor_too_short(self):
        data = _make_data(
            {"why_this_product": "拡張性が高いです。"},
            [{
                "section": "why_this_product", "paragraph_index": 0,
                "anchor_text": "拡張性", "target_asin": "B0TARGET01",
            }],
        )
        result = _inject_internal_links(data, ARTICLE_INDEX, SITE_BASE)
        self.assertEqual(result, (0, 1))

    def test_drops_anchor_too_long(self):
        long_anchor = "木製レールの拡張性がとても高くて子供が長く飽きずに毎日楽しく遊べるのが魅力です"  # >30 chars
        data = _make_data(
            {"why_this_product": f"{long_anchor}という特徴があります。"},
            [{
                "section": "why_this_product", "paragraph_index": 0,
                "anchor_text": long_anchor, "target_asin": "B0TARGET01",
            }],
        )
        result = _inject_internal_links(data, ARTICLE_INDEX, SITE_BASE)
        self.assertEqual(result, (0, 1))

    def test_drops_anchor_ending_in_sentence_final_punctuation(self):
        for punct in ("。", "！", "？", ".", "!", "?"):
            anchor = "空間認識力が育まれます" + punct
            data = _make_data(
                {"why_this_product": anchor},
                [{
                    "section": "why_this_product", "paragraph_index": 0,
                    "anchor_text": anchor, "target_asin": "B0TARGET01",
                }],
            )
            result = _inject_internal_links(data, ARTICLE_INDEX, SITE_BASE)
            self.assertEqual(result, (0, 1), msg=f"punct={punct!r}")

    def test_keeps_anchor_without_trailing_punctuation(self):
        data = _make_data(
            {"why_this_product": "空間認識力が育まれます。"},
            [{
                "section": "why_this_product", "paragraph_index": 0,
                "anchor_text": "空間認識力が育まれます", "target_asin": "B0TARGET01",
            }],
        )
        result = _inject_internal_links(data, ARTICLE_INDEX, SITE_BASE)
        self.assertEqual(result, (1, 0))

    def test_drops_anchor_with_markdown_breaking_brackets(self):
        # anchor に [ ] を含むと `[テスト[注釈]付き](...)` という壊れた
        # markdown を生成するため drop (#3332 レビュー指摘)。
        data = _make_data(
            {"why_this_product": "テスト[注釈]付きの商品です。"},
            [{
                "section": "why_this_product", "paragraph_index": 0,
                "anchor_text": "テスト[注釈]付き", "target_asin": "B0TARGET01",
            }],
        )
        result = _inject_internal_links(data, ARTICLE_INDEX, SITE_BASE)
        self.assertEqual(result, (0, 1))
        self.assertEqual(
            data["narrative"]["why_this_product"], "テスト[注釈]付きの商品です。"
        )

    def test_drops_anchor_with_parens_or_backtick(self):
        narrative = {
            "why_this_product": "拡張性(高)が売りです。",
            "daily_use": "`コード`風の表記もある本文です。",
        }
        suggestions = [
            {"section": "why_this_product", "paragraph_index": 0,
             "anchor_text": "拡張性(高)が売り", "target_asin": "B0TARGET01"},
            {"section": "daily_use", "paragraph_index": 0,
             "anchor_text": "`コード`風の表記", "target_asin": "B0TARGET02"},
        ]
        data = _make_data(narrative, suggestions)
        result = _inject_internal_links(data, ARTICLE_INDEX, SITE_BASE)
        self.assertEqual(result, (0, 2))
        self.assertEqual(data["narrative"]["why_this_product"], "拡張性(高)が売りです。")
        self.assertEqual(data["narrative"]["daily_use"], "`コード`風の表記もある本文です。")

    def test_drops_generic_denylist_anchor(self):
        data = _make_data(
            {"why_this_product": "こちらもおすすめです。"},
            [{
                "section": "why_this_product", "paragraph_index": 0,
                "anchor_text": "こちら", "target_asin": "B0TARGET01",
            }],
        )
        result = _inject_internal_links(data, ARTICLE_INDEX, SITE_BASE)
        self.assertEqual(result, (0, 1))

    def test_drops_dynamic_fact_word_in_anchor(self):
        data = _make_data(
            {"why_this_product": "驚きの最安値で購入できます。"},
            [{
                "section": "why_this_product", "paragraph_index": 0,
                "anchor_text": "驚きの最安値で購入", "target_asin": "B0TARGET01",
            }],
        )
        result = _inject_internal_links(data, ARTICLE_INDEX, SITE_BASE)
        self.assertEqual(result, (0, 1))

    def test_drops_price_and_point_reward_anchors(self):
        # 金額 (数字/漢数詞 + 円) と ポイント還元は静的アンカーとして腐るので drop。
        for text, anchor in (
            ("本体は3,520円で購入できます。", "3,520円で購入"),
            ("差額は数百円ほどにおさまります。", "数百円ほどにおさまる"),
            ("ポイント還元の対象になる商品です。", "ポイント還元の対象"),
        ):
            data = _make_data(
                {"why_this_product": text},
                [{
                    "section": "why_this_product", "paragraph_index": 0,
                    "anchor_text": anchor, "target_asin": "B0TARGET01",
                }],
            )
            result = _inject_internal_links(data, ARTICLE_INDEX, SITE_BASE)
            self.assertEqual(result, (0, 1), msg=f"anchor={anchor} should be dropped")

    def test_keeps_static_yen_and_point_anchors(self):
        # 「楕円形」(形状) や「着目ポイント」(要点) は動的事実ではないので注入する。
        for text, anchor in (
            ("楕円形のピースが入っています。", "楕円形のピース"),
            ("選び方の着目ポイントを解説します。", "選び方の着目ポイント"),
        ):
            data = _make_data(
                {"why_this_product": text},
                [{
                    "section": "why_this_product", "paragraph_index": 0,
                    "anchor_text": anchor, "target_asin": "B0TARGET01",
                }],
            )
            result = _inject_internal_links(data, ARTICLE_INDEX, SITE_BASE)
            self.assertEqual(result, (1, 0), msg=f"anchor={anchor} should be injected")
            self.assertIn(
                f"[{anchor}]({SITE_BASE}/products/b0target01/)",
                data["narrative"]["why_this_product"],
            )


class LimitTests(unittest.TestCase):
    def _suggestion(self, section: str, anchor: str, target: str) -> dict:
        return {
            "section": section, "paragraph_index": 0,
            "anchor_text": anchor, "target_asin": target,
        }

    def test_fourth_suggestion_is_dropped_by_article_cap(self):
        narrative = {
            "how_to_choose": "選び方の基準は素材の安全性です。",
            "why_this_product": "木製レールの拡張性が魅力です。",
            "daily_use": "毎日の遊びに取り入れやすいです。",
            "gift_appeal": "贈り物としても喜ばれます。",
        }
        suggestions = [
            self._suggestion("how_to_choose", "素材の安全性", "B0TARGET01"),
            self._suggestion("why_this_product", "木製レールの拡張性", "B0TARGET02"),
            self._suggestion("daily_use", "毎日の遊びに取り入れ", "B0TARGET03"),
            self._suggestion("gift_appeal", "贈り物としても喜ばれ", "B0TARGET04"),
        ]
        data = _make_data(narrative, suggestions)
        injected, dropped = _inject_internal_links(data, ARTICLE_INDEX, SITE_BASE)
        self.assertEqual((injected, dropped), (3, 1))
        # 4本目 (gift_appeal) は list 順で後のものとして drop され未置換のまま
        self.assertEqual(data["narrative"]["gift_appeal"], "贈り物としても喜ばれます。")

    def test_second_suggestion_in_same_paragraph_is_dropped(self):
        narrative = {"why_this_product": "木製レールの拡張性と安全な塗料が魅力です。"}
        suggestions = [
            self._suggestion("why_this_product", "木製レールの拡張性", "B0TARGET01"),
            self._suggestion("why_this_product", "安全な塗料", "B0TARGET02"),
        ]
        data = _make_data(narrative, suggestions)
        injected, dropped = _inject_internal_links(data, ARTICLE_INDEX, SITE_BASE)
        self.assertEqual((injected, dropped), (1, 1))

    def test_duplicate_target_is_dropped(self):
        narrative = {
            "why_this_product": "木製レールの拡張性が魅力です。",
            "daily_use": "毎日の遊びに取り入れやすいです。",
        }
        suggestions = [
            self._suggestion("why_this_product", "木製レールの拡張性", "B0TARGET01"),
            self._suggestion("daily_use", "毎日の遊びに取り入れ", "B0TARGET01"),
        ]
        data = _make_data(narrative, suggestions)
        injected, dropped = _inject_internal_links(data, ARTICLE_INDEX, SITE_BASE)
        self.assertEqual((injected, dropped), (1, 1))


if __name__ == "__main__":
    unittest.main()
