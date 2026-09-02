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



class BuildMapLinesTest(unittest.TestCase):
    """nginx map 形式 (#6205: 配信を NAS の nginx へ移す)。"""

    def test_emits_quoted_key_and_value_with_semicolon(self):
        indexed = {"tags": {"lego"}, "brands": set()}
        reverse = {"lego": "レゴ"}
        self.assertEqual(
            gtr.build_map_lines(indexed, reverse),
            ['"/tags/レゴ/" "/tags/lego/";'],
        )

    def test_whitespace_term_is_kept_unlike_redirects(self):
        # map はキーをクォートできるので、_redirects では表現できず落として
        # いた用語 (#2817 Phase 5 の積み残し) を拾える。
        indexed = {"tags": {"lotus-life"}, "brands": set()}
        reverse = {"lotus-life": "LOTUS LIFE"}
        self.assertEqual(
            gtr.build_map_lines(indexed, reverse),
            ['"/tags/LOTUS LIFE/" "/tags/lotus-life/";'],
        )
        self.assertEqual(gtr.build_redirect_lines(indexed, reverse), [])

    def test_quote_and_backslash_are_escaped(self):
        # エスケープ漏れは map ファイルを壊し、**オリジンが起動しなくなる**。
        indexed = {"tags": {"q", "b"}, "brands": set()}
        reverse = {"q": 'A"B', "b": 'C\\D'}
        lines = gtr.build_map_lines(indexed, reverse)
        self.assertIn('"/tags/C\\\\D/" "/tags/b/";', lines)
        self.assertIn('"/tags/A\\"B/" "/tags/q/";', lines)

    def test_control_characters_are_dropped(self):
        indexed = {"tags": {"bad", "ok"}, "brands": set()}
        reverse = {"bad": "A\nB", "ok": "レゴ"}
        lines = gtr.build_map_lines(indexed, reverse)
        self.assertEqual(lines, ['"/tags/レゴ/" "/tags/ok/";'])

    def test_skips_when_term_equals_slug_or_missing(self):
        indexed = {"tags": {"stem", "unknown"}, "brands": set()}
        reverse = {"stem": "stem"}
        self.assertEqual(gtr.build_map_lines(indexed, reverse), [])

    def test_every_line_is_a_complete_map_entry(self):
        # nginx は 1 行でも壊れていると起動しない
        # (`[emerg] invalid number of the map parameters`)。
        indexed = {"tags": {"a", "b"}, "brands": {"c"}}
        reverse = {"a": "Baby curiosity", "b": "レゴ", "c": "LOTUS LIFE"}
        for line in gtr.build_map_lines(indexed, reverse):
            self.assertTrue(line.endswith(";"), line)
            self.assertEqual(line.count('"'), 4, f"quote count broken: {line!r}")


if __name__ == "__main__":
    unittest.main()


class CaseOnlyVariantTest(unittest.TestCase):
    """大小文字だけ違う旧 URL は 301 自己ループになる (#6388)。

    配信側 (`_redirects` の tj/go-redirects / nginx の map) はどちらも from 列の
    大小文字を区別しない。`/brands/4M/ -> /brands/4m/` を出すと、宛先である
    `/brands/4m/` 自身がルールにマッチして 301 が自分を指す。2026-09-01 の
    site audit がこの形で 42 URL を r1_sitemap_broken として検出した。
    """

    def test_redirects_drops_case_only_variant(self):
        indexed = {"tags": set(), "brands": {"4m"}}
        reverse = {"4m": "4M"}
        self.assertEqual(gtr.build_redirect_lines(indexed, reverse), [])

    def test_map_drops_case_only_variant(self):
        indexed = {"tags": set(), "brands": {"4m"}}
        reverse = {"4m": "4M"}
        self.assertEqual(gtr.build_map_lines(indexed, reverse), [])

    def test_case_only_variant_does_not_affect_other_terms(self):
        indexed = {"tags": {"lego"}, "brands": {"4m"}}
        reverse = {"lego": "レゴ", "4m": "4M"}
        self.assertEqual(
            gtr.build_redirect_lines(indexed, reverse),
            ["/tags/レゴ/ /tags/lego/ 301"],
        )

    def test_non_ascii_term_is_not_treated_as_case_only(self):
        # 日本語用語は lower() しても変わらないので、通常どおり出す
        indexed = {"tags": {"tsumiki"}, "brands": set()}
        reverse = {"tsumiki": "つみき"}
        self.assertEqual(
            gtr.build_redirect_lines(indexed, reverse),
            ["/tags/つみき/ /tags/tsumiki/ 301"],
        )

    def test_shadowed_key_space_drops_the_whole_group(self):
        """本体が住むキー空間には、別スラッグ行も置けない。

        `Connetix -> connetix` を落とすだけだと `CONNETIX -> connetix-2` が残り、
        大小文字非区別マッチで正規 URL `/brands/connetix/` まで connetix-2 へ飛ぶ。
        """
        indexed = {"tags": set(), "brands": {"connetix", "connetix-2"}}
        reverse = {"connetix": "Connetix", "connetix-2": "CONNETIX"}
        self.assertEqual(gtr.build_redirect_lines(indexed, reverse), [])
        self.assertEqual(gtr.build_map_lines(indexed, reverse), [])

    def test_no_emitted_rule_shadows_its_own_destination(self):
        """出力全体の不変条件: from 列 (小文字) が宛先 (小文字) と一致しない。"""
        indexed = {
            "tags": {"stem", "brio", "tsumiki"},
            "brands": {"4m", "lego", "connetix", "connetix-2"},
        }
        reverse = {
            "stem": "STEM", "brio": "BRIO", "tsumiki": "つみき",
            "4m": "4M", "lego": "レゴ",
            "connetix": "Connetix", "connetix-2": "CONNETIX",
        }
        for line in gtr.build_redirect_lines(indexed, reverse):
            old, new, _code = line.split()
            self.assertNotEqual(old.lower(), new.lower(), line)
