"""select_brand_narrative_target.py の単体テスト (#2817 Phase 2)。

カバレッジ: select() が hugo/content/brands/<slug>/_index.md (英語スラッグ物理
ディレクトリ) の存在有無を正しく解決すること。JP canonical のままパスを組むと
既存ディレクトリを見失い、narrative を旧 JP パスに再生成してしまう回帰を防ぐ。
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

THIS_DIR = pathlib.Path(__file__).resolve().parent
SCRIPTS_DIR = THIS_DIR.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import select_brand_narrative_target as sbnt  # noqa: E402
from term_slug import TermSlugMap  # noqa: E402


def _stats(**overrides):
    base = {
        "count": 5,
        "avg_age_min_months": 24.0,
        "avg_ivs_100": 80.0,
        "lowest_price": 1000,
        "tier": "A",
        "top_3_series": ["レゴ", "シリーズA", "シリーズB"],
        "representative_asin": "B0TEST00001",
        "representative_image": "https://x/i.jpg",
    }
    base.update(overrides)
    return base


class SelectSlugPathTest(unittest.TestCase):
    def setUp(self):
        self._orig = sbnt._TERM_SLUGS

    def tearDown(self):
        sbnt._TERM_SLUGS = self._orig

    def _use_slug_map(self, td):
        # 本番は data/term_slugs.yaml が既にバックフィル済みの前提。テストでも
        # "レゴ" のスラッグを事前に確定させておく (get_or_create は空マップだと
        # 何もヒットしないため .get() が None を返し JP パスへ fallback してしまう)。
        taxonomy = pathlib.Path(td) / "brand_taxonomy.yaml"
        taxonomy.write_text(
            "brands:\n  - canonical: レゴ\n    aliases: [レゴ(LEGO), LEGO]\n",
            encoding="utf-8",
        )
        m = TermSlugMap(path=pathlib.Path(td) / "term_slugs.yaml", brand_taxonomy_path=taxonomy)
        m.get_or_create("レゴ")
        m.save()
        sbnt._TERM_SLUGS = m

    def test_finds_existing_index_at_slug_path_not_jp_path(self):
        with tempfile.TemporaryDirectory() as td:
            self._use_slug_map(td)
            brands_dir = pathlib.Path(td) / "brands"
            (brands_dir / "lego").mkdir(parents=True)
            (brands_dir / "lego" / "_index.md").write_text(
                '---\ntitle: "レゴ"\nauto_generated: true\nlast_generated_at: "2026-01-01T00:00:00+09:00"\n---\n',
                encoding="utf-8",
            )
            brand_hub = {"brands": {"レゴ": _stats()}}
            candidate = sbnt.select(brand_hub, brands_dir)
            self.assertIsNotNone(candidate)
            # 既存ファイルを正しく見つけられていれば has_index=True (新規扱いされない)
            self.assertTrue(candidate.has_index)

    def test_missing_jp_path_no_longer_causes_false_new_coverage(self):
        # 回帰防止: JP パス (brands_dir/レゴ) だけが存在するケースは移行後は
        # 発生しない前提だが、万一 slug が未登録でも JP 名 fallback で動作すること。
        with tempfile.TemporaryDirectory() as td:
            self._use_slug_map(td)
            brands_dir = pathlib.Path(td) / "brands"
            brand_hub = {"brands": {"未登録ブランド": _stats(top_3_series=["未登録ブランド", "A", "B"])}}
            candidate = sbnt.select(brand_hub, brands_dir)
            self.assertIsNotNone(candidate)
            self.assertFalse(candidate.has_index)


if __name__ == "__main__":
    unittest.main()
