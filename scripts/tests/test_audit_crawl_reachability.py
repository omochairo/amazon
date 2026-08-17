"""Unit tests for audit_crawl_reachability (#5343)。

カバレッジ:
1. detect_base_url: 成果物の sitemap から baseURL を推定 / sitemap 無しは None
2. url_to_path: pretty URL / ルート / 拡張子つきのマッピング
3. collect_sitemap_urls: urlset / sitemapindex 再帰 / off-host 除外
4. BFS 到達: seed から辿れないページは数えない (孤立ページを到達扱いしない)
5. alias (`page/1/` の meta refresh) を noindex に数えないこと
6. section_of: ページネーションを元セクション付きで分類する
7. summarize: 到達 / indexable / noindex の集計と noindex_ratio
8. リンクされているがビルド出力に無い URL の検出
9. render_summary: baseline との差分表示
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import unittest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.dirname(THIS_DIR)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import audit_crawl_reachability as A  # noqa: E402

BASE_URL = "https://test.example"
HOST = "test.example"

PAGE = """<html><head>{head}</head><body>{body}</body></html>"""
ALIAS = (
    '<html><head><meta name="robots" content="noindex">'
    '<meta http-equiv="refresh" content="0; url={to}"></head></html>'
)
NOINDEX_META = '<meta name="robots" content="noindex,follow">'


def write(root: pathlib.Path, rel: str, html: str) -> None:
    path = root.joinpath(*rel.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")


def link(href: str) -> str:
    return '<a href="{}">x</a>'.format(href)


def sitemap(locs) -> str:
    body = "".join("<url><loc>{}</loc></url>".format(u) for u in locs)
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        + body + "</urlset>"
    )


def build_site(root: pathlib.Path) -> None:
    """小さなサイトを作る。

      /                    indexable、/posts/ と /tags/t/ へリンク
      /posts/              indexable
      /tags/t/             indexable、page/2 と page/1 (alias) へリンク
      /tags/t/page/2/      noindex (ページネーション)
      /tags/t/page/1/      alias (meta refresh + noindex)
      /orphan/             indexable だがどこからもリンクされず sitemap にも無い
      /gone/               リンクされているがファイルが無い
    """
    write(root, "index.html", PAGE.format(
        head="", body=link("/posts/") + link("/tags/t/")))
    write(root, "posts/index.html", PAGE.format(head="", body=link("/")))
    write(root, "tags/t/index.html", PAGE.format(
        head="",
        body=link("/tags/t/page/2/") + link("/tags/t/page/1/") + link("/gone/")))
    write(root, "tags/t/page/2/index.html", PAGE.format(
        head=NOINDEX_META, body=link("/tags/t/")))
    write(root, "tags/t/page/1/index.html",
          ALIAS.format(to=BASE_URL + "/tags/t/"))
    write(root, "orphan/index.html", PAGE.format(head="", body=""))
    write(root, "sitemap.xml", sitemap([
        BASE_URL + "/", BASE_URL + "/posts/", BASE_URL + "/tags/t/",
    ]))


class TestHelpers(unittest.TestCase):
    def test_detect_base_url(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            build_site(root)
            self.assertEqual(A.detect_base_url(root), BASE_URL)

    def test_detect_base_url_without_sitemap(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(A.detect_base_url(pathlib.Path(d)))

    def test_url_to_path(self):
        root = pathlib.Path("public")
        self.assertEqual(A.url_to_path(BASE_URL + "/", root),
                         root / "index.html")
        self.assertEqual(A.url_to_path(BASE_URL + "/tags/t/", root),
                         root / "tags" / "t" / "index.html")
        # 拡張子つきはそのまま (sitemap.xml など)
        self.assertEqual(A.url_to_path(BASE_URL + "/sitemap.xml", root),
                         root / "sitemap.xml")
        # 末尾スラッシュなしの拡張子なしは index.html を足す
        self.assertEqual(A.url_to_path(BASE_URL + "/about", root),
                         root / "about" / "index.html")

    def test_section_of(self):
        self.assertEqual(A.section_of(BASE_URL + "/"), "home")
        self.assertEqual(A.section_of(BASE_URL + "/products/b01/"), "products")
        self.assertEqual(A.section_of(BASE_URL + "/tags/t/"), "tags")
        # ページネーションは元セクション付き
        self.assertEqual(A.section_of(BASE_URL + "/tags/t/page/2/"),
                         "tags-pagination")
        self.assertEqual(A.section_of(BASE_URL + "/brands/b/page/3/"),
                         "brands-pagination")
        self.assertEqual(A.section_of(BASE_URL + "/page/4/"), "home-pagination")
        self.assertEqual(A.section_of(BASE_URL + "/whatever/"), "other")

    def test_section_of_section_root_pagination(self):
        """セクション直下のページャを home に取りこぼさない。

        `/page/` の手前を切ると `/posts/page/2/` → `/posts` とスラッシュが落ち、
        prefix `/posts/` に startswith で当たらない。実測 (2026-08-17) では
        /posts/page/N/ の 85 本が home-pagination に計上されていた。
        """
        self.assertEqual(A.section_of(BASE_URL + "/posts/page/2/"),
                         "posts-pagination")
        self.assertEqual(A.section_of(BASE_URL + "/price/page/3/"),
                         "price-pagination")
        # term 配下 (スラッシュが残る側) は従来どおり
        self.assertEqual(A.section_of(BASE_URL + "/tags/t/page/9/"),
                         "tags-pagination")


class TestSitemap(unittest.TestCase):
    def test_collect_urlset(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            build_site(root)
            urls = A.collect_sitemap_urls(root, BASE_URL, HOST)
            self.assertEqual(urls, {
                BASE_URL + "/", BASE_URL + "/posts/", BASE_URL + "/tags/t/"})

    def test_collect_sitemapindex_recurses_and_drops_offhost(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            write(root, "sitemap.xml",
                  '<?xml version="1.0"?>'
                  '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                  "<sitemap><loc>{}/child.xml</loc></sitemap>"
                  "</sitemapindex>".format(BASE_URL))
            write(root, "child.xml", sitemap(
                [BASE_URL + "/a/", "https://other.example/b/"]))
            urls = A.collect_sitemap_urls(root, BASE_URL, HOST)
            self.assertEqual(urls, {BASE_URL + "/a/"})


class TestCrawl(unittest.TestCase):
    def crawl(self, root: pathlib.Path):
        sitemap_urls = A.collect_sitemap_urls(root, BASE_URL, HOST)
        seeds = set(sitemap_urls) | {BASE_URL + "/"}
        pages = A.crawl(root, BASE_URL, seeds, 1000)
        return pages, A.summarize(pages, sitemap_urls)

    def test_orphan_is_not_reachable(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            build_site(root)
            pages, _ = self.crawl(root)
            self.assertNotIn(BASE_URL + "/orphan/", pages)

    def test_alias_not_counted_as_noindex(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            build_site(root)
            _, summary = self.crawl(root)
            # page/1 は noindex meta を持つが alias なので noindex に数えない
            self.assertEqual(summary["alias"], 1)
            self.assertEqual(summary["noindex"], 1)  # page/2 だけ
            self.assertNotIn(BASE_URL + "/tags/t/page/1/",
                             summary["noindex_urls"])
            self.assertIn(BASE_URL + "/tags/t/page/2/", summary["noindex_urls"])

    def test_counts_and_sections(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            build_site(root)
            _, summary = self.crawl(root)
            # / , /posts/ , /tags/t/ , page/2 , page/1 = 5 (orphan と gone を除く)
            self.assertEqual(summary["reachable"], 5)
            self.assertEqual(summary["indexable"], 3)
            self.assertAlmostEqual(summary["noindex_ratio"], 0.2)
            self.assertEqual(summary["by_section"]["tags-pagination"],
                             {"total": 2, "noindex": 1, "alias": 1})

    def test_missing_target_is_reported(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            build_site(root)
            _, summary = self.crawl(root)
            self.assertEqual(summary["linked_but_missing_count"], 1)
            self.assertIn(BASE_URL + "/gone/", summary["linked_but_missing"])

    def test_max_pages_caps_crawl(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            build_site(root)
            pages = A.crawl(root, BASE_URL, {BASE_URL + "/"}, 2)
            self.assertEqual(len(pages), 2)


class TestRender(unittest.TestCase):
    def test_render_with_baseline_shows_delta(self):
        summary = {
            "reachable": 2840, "indexable": 2550, "noindex": 290, "alias": 0,
            "noindex_ratio": 0.1021, "sitemap_urls": 2550,
            "linked_but_missing": [], "linked_but_missing_count": 0,
            "by_section": {"tags-pagination": {"total": 168, "noindex": 168}},
            "noindex_urls": [],
        }
        baseline = dict(summary, reachable=3441, noindex=897,
                        by_section={"tags-pagination": {"total": 520}})
        out = A.render_summary(summary, baseline)
        self.assertIn("2840 (-601)", out)
        self.assertIn("290 (-607)", out)
        self.assertIn("(-352)", out)  # tags-pagination 520 -> 168

    def test_render_without_baseline_has_no_delta(self):
        summary = {
            "reachable": 10, "indexable": 8, "noindex": 2, "alias": 0,
            "noindex_ratio": 0.2, "sitemap_urls": 8,
            "linked_but_missing": [], "linked_but_missing_count": 0,
            "by_section": {}, "noindex_urls": [],
        }
        out = A.render_summary(summary)
        self.assertNotIn("(+", out)
        self.assertNotIn("(-", out)


if __name__ == "__main__":
    unittest.main()
