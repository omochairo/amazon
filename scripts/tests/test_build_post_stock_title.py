"""Unit tests for #2686 / #4964 — build_post 側の「どこで買える/在庫」型配線。

_frontmatter_meta の stock_title_override が:
  - 与えられたときはそれを最優先で使う (title / title_variants を無視する)
  - 与えられない (None) ときは従来どおり title / title_variants から決める
    (=既存記事のタイトルは一切変化しない)

を保証する。データ層 (where_to_buy_format.is_stock_format_eligible /
stock_status) 側のロールアウト日ゲートは test_where_to_buy_format.py で
別途カバーしている。
"""
from __future__ import annotations

import pathlib
import sys
import unittest

THIS_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = THIS_DIR.parent.parent  # amazon-clone/
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from build_post import _frontmatter_meta  # type: ignore[import-not-found]


class FrontmatterMetaStockTitleOverrideTests(unittest.TestCase):
    def _meta(self, data: dict, stock_title_override):
        return _frontmatter_meta(
            data, "2026-08-13-B0GFVTPDBM", False, {}, pathlib.Path("B0GFVTPDBM.json"),
            stock_title_override=stock_title_override,
        )

    def test_override_wins_over_plain_title(self):
        data = {
            "title": "元のタイトル（LLM生成）",
            "product": {"asin": "B0GFVTPDBM"},
            "date": "2026-08-13T10:00:00+09:00",
        }
        meta = self._meta(data, "テスト商品はどこで買える？在庫と価格を毎日チェック（Amazon）")
        self.assertEqual(meta["title"], "テスト商品はどこで買える？在庫と価格を毎日チェック（Amazon）")

    def test_override_wins_over_title_variants(self):
        data = {
            "title": "元のタイトル",
            "title_variants": [{"title": "SEO最適化タイトル"}],
            "product": {"asin": "B0GFVTPDBM"},
            "date": "2026-08-13T10:00:00+09:00",
        }
        meta = self._meta(data, "新型タイトル（Amazon）")
        self.assertEqual(meta["title"], "新型タイトル（Amazon）")

    def test_no_override_keeps_existing_title_variant_behavior(self):
        """既存記事保護: stock_title_override=None のときは従来の
        title_variants 優先ロジックが一切変わらない。"""
        data = {
            "title": "元のタイトル",
            "title_variants": [{"title": "SEO最適化タイトル"}],
            "product": {"asin": "B0GFVTPDBM"},
            "date": "2026-05-14T10:00:00+09:00",
        }
        meta = self._meta(data, None)
        self.assertEqual(meta["title"], "SEO最適化タイトル")

    def test_no_override_plain_title_unchanged(self):
        data = {
            "title": "元のタイトル",
            "product": {"asin": "B0GFVTPDBM"},
            "date": "2026-05-14T10:00:00+09:00",
        }
        meta = self._meta(data, None)
        self.assertEqual(meta["title"], "元のタイトル")

    def test_empty_string_override_is_treated_as_no_override(self):
        # falsy override (空文字) は「未設定」として扱い、既存ロジックへ fallback する。
        data = {
            "title": "元のタイトル",
            "product": {"asin": "B0GFVTPDBM"},
            "date": "2026-05-14T10:00:00+09:00",
        }
        meta = self._meta(data, "")
        self.assertEqual(meta["title"], "元のタイトル")


if __name__ == "__main__":
    unittest.main()
