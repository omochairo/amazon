"""Unit tests for #1301 B4 Stage 2 — Article JSON-LD reviewedBy/creator routing.

Covers:
    - _determine_reviewers() routing logic across age / tags / product name
    - _build_webpage_jsonld() structure (author / reviewedBy / creator / @id)
"""
from __future__ import annotations

import pathlib
import sys
import unittest

THIS_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = THIS_DIR.parent.parent  # amazon-clone/
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from build_post import (  # type: ignore[import-not-found]
    ORG_AI_AGENT,
    ORG_OMOCHAIRO_EDITORIAL,
    PERSON_IROMAMA,
    PERSON_IROPAPA,
    _build_webpage_jsonld,
    _determine_reviewers,
)


class DetermineReviewersTest(unittest.TestCase):
    """`_determine_reviewers()` の判定優先度を検証。"""

    # ───── いろママ判定 (安全性主軸) ─────

    def test_age_under_36mo_routes_to_iromama(self) -> None:
        result = _determine_reviewers({"age_min_months": 12, "name": "knock toy"}, [])
        self.assertEqual(result, [PERSON_IROMAMA])

    def test_age_24mo_routes_to_iromama(self) -> None:
        result = _determine_reviewers({"age_min_months": 24, "name": ""}, [])
        self.assertEqual(result, [PERSON_IROMAMA])

    def test_safety_tag_routes_to_iromama(self) -> None:
        result = _determine_reviewers(
            {"age_min_months": 72, "name": "generic toy"}, ["食育", "知育"]
        )
        self.assertEqual(result, [PERSON_IROMAMA])

    def test_mamagoto_name_routes_to_iromama(self) -> None:
        result = _determine_reviewers(
            {"age_min_months": 72, "name": "おままごとセット"}, []
        )
        self.assertEqual(result, [PERSON_IROMAMA])

    def test_nuigurumi_name_routes_to_iromama(self) -> None:
        result = _determine_reviewers(
            {"age_min_months": 60, "name": "クマのぬいぐるみ"}, []
        )
        self.assertEqual(result, [PERSON_IROMAMA])

    # ───── いろパパ判定 (分析主軸) ─────

    def test_stem_tag_routes_to_iropapa(self) -> None:
        result = _determine_reviewers(
            {"age_min_months": 72, "name": "abc"}, ["STEM", "知育"]
        )
        self.assertEqual(result, [PERSON_IROPAPA])

    def test_programming_tag_routes_to_iropapa(self) -> None:
        result = _determine_reviewers(
            {"age_min_months": 96, "name": "ロボキッズ"}, ["プログラミング"]
        )
        self.assertEqual(result, [PERSON_IROPAPA])

    def test_robot_name_routes_to_iropapa(self) -> None:
        result = _determine_reviewers(
            {"age_min_months": 84, "name": "プログラミングロボット PRO"}, []
        )
        self.assertEqual(result, [PERSON_IROPAPA])

    # ───── default = dual reviewer (両者連名) ─────

    def test_no_signal_defaults_to_dual(self) -> None:
        result = _determine_reviewers(
            {"age_min_months": 72, "name": "汎用ブロック"}, ["ブロック"]
        )
        self.assertEqual(result, [PERSON_IROPAPA, PERSON_IROMAMA])

    def test_missing_age_and_tags_defaults_to_dual(self) -> None:
        result = _determine_reviewers({"name": "玩具"}, None)
        self.assertEqual(result, [PERSON_IROPAPA, PERSON_IROMAMA])

    # ───── 境界・型耐性 ─────

    def test_age_exactly_36mo_does_not_trigger_iromama_by_age(self) -> None:
        """36 ヶ月ちょうど (3 歳) は誤飲リスク圏外なので、age だけでは IROMAMA に行かない。"""
        result = _determine_reviewers(
            {"age_min_months": 36, "name": "blocks"}, ["ブロック"]
        )
        self.assertEqual(result, [PERSON_IROPAPA, PERSON_IROMAMA])

    def test_invalid_age_str_safely_defaults(self) -> None:
        result = _determine_reviewers({"age_min_months": "??", "name": "x"}, [])
        self.assertEqual(result, [PERSON_IROPAPA, PERSON_IROMAMA])

    def test_iromama_priority_over_iropapa(self) -> None:
        """安全性シグナル + 分析シグナルが同居した場合、いろママを優先 (年齢が先)。"""
        result = _determine_reviewers(
            {"age_min_months": 12, "name": "プログラミングロボット"}, ["STEM"]
        )
        self.assertEqual(result, [PERSON_IROMAMA])


class BuildWebpageJsonLdTest(unittest.TestCase):
    """`_build_webpage_jsonld()` が schema.org 仕様で正しい構造を出すか検証。"""

    def _build(self, **overrides) -> dict:
        defaults = {
            "product": {"age_min_months": 72, "name": "デフォルト玩具"},
            "tags": [],
            "title": "テスト記事",
            "date": "2026-06-01T00:00:00+00:00",
            "lastmod": "2026-06-01T12:00:00+00:00",
            "asin": "B0TEST0001",
        }
        defaults.update(overrides)
        return _build_webpage_jsonld(**defaults)

    def test_basic_structure(self) -> None:
        ld = self._build()
        self.assertEqual(ld["@context"], "https://schema.org")
        self.assertEqual(ld["@type"], "WebPage")
        self.assertEqual(ld["author"], ORG_OMOCHAIRO_EDITORIAL)
        self.assertEqual(ld["creator"], ORG_AI_AGENT)
        self.assertEqual(ld["name"], "テスト記事")
        self.assertEqual(ld["datePublished"], "2026-06-01T00:00:00+00:00")
        self.assertEqual(ld["dateModified"], "2026-06-01T12:00:00+00:00")

    def test_asin_lowercased_in_url(self) -> None:
        ld = self._build(asin="B0TEST0001")
        self.assertEqual(ld["url"], "https://navi.omcha.jp/products/b0test0001/")
        self.assertEqual(
            ld["@id"], "https://navi.omcha.jp/products/b0test0001/#webpage"
        )

    def test_iromama_single_reviewer_emitted_as_object(self) -> None:
        ld = self._build(product={"age_min_months": 18, "name": "x"}, tags=[])
        # 単独 reviewer は配列ではなく単一 object として出力
        self.assertIsInstance(ld["reviewedBy"], dict)
        self.assertEqual(ld["reviewedBy"]["name"], "いろママ")

    def test_iropapa_single_reviewer_emitted_as_object(self) -> None:
        ld = self._build(
            product={"age_min_months": 72, "name": "STEM ロボ"}, tags=["STEM"]
        )
        self.assertIsInstance(ld["reviewedBy"], dict)
        self.assertEqual(ld["reviewedBy"]["name"], "いろパパ")

    def test_dual_reviewer_emitted_as_array(self) -> None:
        ld = self._build(product={"age_min_months": 72, "name": "ブロック"}, tags=[])
        # 両者連名は配列として出力
        self.assertIsInstance(ld["reviewedBy"], list)
        self.assertEqual(len(ld["reviewedBy"]), 2)
        names = [r["name"] for r in ld["reviewedBy"]]
        self.assertIn("いろパパ", names)
        self.assertIn("いろママ", names)

    def test_is_part_of_website_referenced(self) -> None:
        ld = self._build()
        self.assertEqual(ld["isPartOf"]["@type"], "WebSite")
        self.assertEqual(ld["isPartOf"]["url"], "https://navi.omcha.jp/")


if __name__ == "__main__":
    unittest.main()
