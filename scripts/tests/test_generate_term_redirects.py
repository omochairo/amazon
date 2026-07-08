"""generate_term_redirects.py の単体テスト (#2817 Phase 3/5)。

カバレッジ:
1. _indexed_slugs_by_kind: sitemap.xml から /tags/ /brands/ の slug 抽出
   (一覧ページ自体は除外)
2. _reverse_slug_map: slug -> JP用語 の逆引き
3. build_redirect_lines: 生UTF-8 (percent-encode しない) の1行を出力、
   term==slug (実質変化なし) は redirect 対象外、生の空白を含む term は
   _redirects で表現不可能なためスキップ (#2817 Phase 5 実機検証)
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

THIS_DIR = pathlib.Path(__file__).resolve().parent
SCRIPTS_DIR = THIS_DIR.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import generate_term_redirects as gtr  # noqa: E402

_SAMPLE_SITEMAP = """<?xml version="1.0" encoding="utf-8" standalone="yes"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://navi.omcha.jp/tags/</loc></url>
<url><loc>https://navi.omcha.jp/tags/lego/</loc></url>
<url><loc>https://navi.omcha.jp/tags/anpanman/</loc></url>
<url><loc>https://navi.omcha.jp/brands/</loc></url>
<url><loc>https://navi.omcha.jp/brands/lego/</loc></url>
<url><loc>https://navi.omcha.jp/products/b0test00001/</loc></url>
</urlset>
"""


class IndexedSlugsTest(unittest.TestCase):
    def test_extracts_tags_and_brands_excluding_list_pages(self):
        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / "sitemap.xml"
            p.write_text(_SAMPLE_SITEMAP, encoding="utf-8")
            result = gtr._indexed_slugs_by_kind(p)
            self.assertEqual(result["tags"], {"lego", "anpanman"})
            self.assertEqual(result["brands"], {"lego"})

    def test_ignores_non_term_urls(self):
        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / "sitemap.xml"
            p.write_text(_SAMPLE_SITEMAP, encoding="utf-8")
            result = gtr._indexed_slugs_by_kind(p)
            self.assertNotIn("b0test00001", result["tags"] | result["brands"])


class ReverseSlugMapTest(unittest.TestCase):
    def test_builds_slug_to_term_map(self):
        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / "term_slugs.yaml"
            p.write_text("レゴ: lego\nアンパンマン: anpanman\n", encoding="utf-8")
            result = gtr._reverse_slug_map(p)
            self.assertEqual(result, {"lego": "レゴ", "anpanman": "アンパンマン"})


class BuildRedirectLinesTest(unittest.TestCase):
    def test_emits_single_raw_line_per_term(self):
        # percent-encoded 形式は GitLab Pages の実マッチングでは一致しない
        # ことを実機で確認済み (#2817 Phase 5)。raw (decode 済み) 形式のみ出す。
        indexed = {"tags": {"lego"}, "brands": set()}
        reverse = {"lego": "レゴ"}
        lines = gtr.build_redirect_lines(indexed, reverse)
        self.assertEqual(lines, ["/tags/レゴ/ /tags/lego/ 301"])

    def test_skips_when_term_equals_slug(self):
        # 実質変化なし (例: 元から ASCII で slug と同一) は redirect 不要
        indexed = {"tags": {"stem"}, "brands": set()}
        reverse = {"stem": "stem"}
        lines = gtr.build_redirect_lines(indexed, reverse)
        self.assertEqual(lines, [])

    def test_skips_when_slug_missing_from_reverse_map(self):
        indexed = {"tags": {"unknown-slug"}, "brands": set()}
        reverse = {}
        lines = gtr.build_redirect_lines(indexed, reverse)
        self.assertEqual(lines, [])

    def test_brands_and_tags_use_correct_path_prefix(self):
        indexed = {"tags": set(), "brands": {"lego"}}
        reverse = {"lego": "レゴ"}
        lines = gtr.build_redirect_lines(indexed, reverse)
        self.assertIn("/brands/レゴ/ /brands/lego/ 301", lines)

    def test_term_containing_whitespace_is_skipped_entirely(self):
        # 実機で確認された事故 (#2817 Phase 5): GitLab Pages の _redirects パーサ
        # (tj/go-redirects) は strings.Fields() で行を空白分割するため、term に
        # 生の空白を含む raw 行 (例: "/tags/LOTUS LIFE/ ...") を出すと3列に収まらず
        # ファイル全体がパースエラーで丸ごと無効化される。かつ percent-encoded
        # 行は実際のマッチングでは一致しない (無意味) ことも実機で確認済みのため、
        # この term は redirect 自体を生成しない (旧URLは404のまま=現状維持)。
        indexed = {"tags": {"lotus-life"}, "brands": set()}
        reverse = {"lotus-life": "LOTUS LIFE"}
        lines = gtr.build_redirect_lines(indexed, reverse)
        self.assertEqual(lines, [])

    def test_whitespace_term_skipped_without_affecting_other_terms(self):
        # 上記事故の再発防止ガード。空白を含む term が混ざっていても、
        # 空白を含まない他の term は正常に redirect が生成されること。
        indexed = {"tags": {"a", "b"}, "brands": {"c"}}
        reverse = {
            "a": "Baby curiosity",  # 空白あり -> スキップ
            "b": "beyblade-x-alias",  # 空白なし (別名) -> 生成
            "c": "LOTUS LIFE",  # 空白あり -> スキップ
        }
        lines = gtr.build_redirect_lines(indexed, reverse)
        self.assertEqual(lines, ["/tags/beyblade-x-alias/ /tags/b/ 301"])
        for line in lines:
            from_path = line.split(" ", 1)[0]
            self.assertFalse(
                any(ch.isspace() for ch in from_path),
                f"raw whitespace leaked into a _redirects rule: {line!r}",
            )


if __name__ == "__main__":
    unittest.main()
