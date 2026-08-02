"""Unit tests for audit_site_health (#2995 案2)。

カバレッジ:
1. sitemap パース: urlset / sitemapindex 再帰、off-host 除外
2. normalize_url: 相対解決・fragment/query 除去・trailing slash 統一・
   アセット拡張子/off-host/非 http スキーム除外
3. noindex 検出: meta robots / X-Robots-Tag ヘッダ、大小文字非依存
4. リダイレクト記録 (requested != final)
5. BFS + inbound 集計: 自己リンク除外・sitemap straggler の取得・inbound 0
6. R1-R4 計算 と has_regressions (regression あり/なしの両方)
7. broken_internal の sources が最大 5 件にキャップされること
8. latest.json の形状 と history.jsonl の追記 (2 回実行で 2 行)
9. URL 単位の例外 -> status 0 として記録され、実行が継続すること (R4 に計上)
10. --fail-on-regression で exit code 2、デフォルトは exit 0
11. canonical_mismatch / noindex_inlinked の pattern 分類・pattern 集計の並び順・
    latest_details.json への全件書き出し・latest.json に明細が含まれないこと (#3727)
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

import requests

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.dirname(THIS_DIR)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import audit_site_health as A  # noqa: E402

BASE_URL = "https://test.example"
HOST = "test.example"
HOME = "https://test.example/"


def make_pages_fetcher(pages: dict):
    """pages: url -> {"status","content_type","text","headers","final_url","exception"}。

    未定義の URL がフェッチされたら AssertionError (テストの想定漏れを検出するため)。
    """

    def _fetch(session, url, timeout, user_agent):
        spec = pages.get(url)
        if spec is None:
            raise AssertionError(f"unexpected fetch: {url}")
        if spec.get("exception"):
            return A.FetchResult(status=0, final_url=url)
        return A.FetchResult(
            status=spec.get("status", 200),
            final_url=spec.get("final_url", url),
            content_type=spec.get("content_type", "text/html"),
            text=spec.get("text", ""),
            headers=spec.get("headers", {}),
        )

    return _fetch


def _mock_response(status=200, url=None, headers=None, text=""):
    r = mock.Mock()
    r.status_code = status
    r.url = url
    r.headers = headers or {}
    r.text = text
    return r


def _noop_sleeper(_seconds):
    return None


class NormalizeUrlTest(unittest.TestCase):
    def test_relative_resolution(self):
        self.assertEqual(
            A.normalize_url("/foo/bar", "https://test.example/x/y/", HOST),
            "https://test.example/foo/bar/",
        )

    def test_fragment_and_query_stripped(self):
        self.assertEqual(
            A.normalize_url("/foo?x=1#y", HOME, HOST),
            "https://test.example/foo/",
        )

    def test_trailing_slash_unification(self):
        a = A.normalize_url("/foo", HOME, HOST)
        b = A.normalize_url("/foo/", HOME, HOST)
        self.assertEqual(a, b)
        self.assertEqual(a, "https://test.example/foo/")

    def test_asset_extension_excluded_case_insensitive(self):
        self.assertIsNone(A.normalize_url("/image.jpg", HOME, HOST))
        self.assertIsNone(A.normalize_url("/style.CSS", HOME, HOST))

    def test_off_host_excluded(self):
        self.assertIsNone(A.normalize_url("https://other.example/x", HOME, HOST))

    def test_non_http_scheme_excluded(self):
        self.assertIsNone(A.normalize_url("mailto:test@example.com", HOME, HOST))
        self.assertIsNone(A.normalize_url("javascript:void(0)", HOME, HOST))

    def test_extensioned_path_not_forced_trailing_slash(self):
        self.assertEqual(
            A.normalize_url("/report.html", HOME, HOST),
            "https://test.example/report.html",
        )

    def test_empty_url_returns_none(self):
        self.assertIsNone(A.normalize_url("", HOME, HOST))


class SitemapParsingTest(unittest.TestCase):
    def test_plain_urlset(self):
        xml = """<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://test.example/</loc></url>
<url><loc>https://test.example/a/</loc></url>
<url><loc>https://other.example/y</loc></url>
</urlset>"""
        pages = {
            "https://test.example/sitemap.xml": {"content_type": "application/xml", "text": xml},
        }
        fetcher = make_pages_fetcher(pages)
        result = A.collect_sitemap_urls(
            BASE_URL, HOST, mock.Mock(), 15, "ua", fetcher, _noop_sleeper, 0,
        )
        self.assertEqual(result, {"https://test.example/", "https://test.example/a/"})

    def test_sitemapindex_recursion_and_off_host_child_ignored(self):
        root_xml = """<?xml version="1.0"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<sitemap><loc>https://test.example/sitemap-1.xml</loc></sitemap>
<sitemap><loc>https://test.example/sitemap-2.xml</loc></sitemap>
<sitemap><loc>https://other.example/sitemap-x.xml</loc></sitemap>
</sitemapindex>"""
        child1_xml = """<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://test.example/</loc></url>
<url><loc>https://test.example/a/</loc></url>
</urlset>"""
        child2_xml = """<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://test.example/b/</loc></url>
<url><loc>https://other.example/z</loc></url>
</urlset>"""
        pages = {
            "https://test.example/sitemap.xml": {"text": root_xml},
            "https://test.example/sitemap-1.xml": {"text": child1_xml},
            "https://test.example/sitemap-2.xml": {"text": child2_xml},
        }
        fetcher = make_pages_fetcher(pages)
        result = A.collect_sitemap_urls(
            BASE_URL, HOST, mock.Mock(), 15, "ua", fetcher, _noop_sleeper, 0,
        )
        self.assertEqual(
            result,
            {"https://test.example/", "https://test.example/a/", "https://test.example/b/"},
        )
        # off-host sitemapindex child (other.example/sitemap-x.xml) must never even be fetched;
        # make_pages_fetcher raises AssertionError on unexpected urls, so reaching here proves it.

    def test_missing_sitemap_returns_empty_set(self):
        pages = {"https://test.example/sitemap.xml": {"status": 404, "text": ""}}
        fetcher = make_pages_fetcher(pages)
        result = A.collect_sitemap_urls(
            BASE_URL, HOST, mock.Mock(), 15, "ua", fetcher, _noop_sleeper, 0,
        )
        self.assertEqual(result, set())


class NoindexDetectionTest(unittest.TestCase):
    def test_meta_robots_noindex_case_insensitive(self):
        fr = A.FetchResult(
            status=200, final_url=HOME, content_type="text/html",
            text='<html><head><meta name="Robots" content="NoIndex, Follow"></head><body></body></html>',
        )
        rec = A.process_fetch(HOME, fr, HOST)
        self.assertTrue(rec.noindex)

    def test_x_robots_tag_header_case_insensitive(self):
        fr = A.FetchResult(
            status=200, final_url=HOME, content_type="text/html",
            text="<html><body></body></html>",
            headers={"x-robots-tag": "NOINDEX"},
        )
        rec = A.process_fetch(HOME, fr, HOST)
        self.assertTrue(rec.noindex)

    def test_clean_page_not_noindex(self):
        fr = A.FetchResult(
            status=200, final_url=HOME, content_type="text/html",
            text="<html><body>hello</body></html>",
        )
        rec = A.process_fetch(HOME, fr, HOST)
        self.assertFalse(rec.noindex)

    def test_jsonld_count_ignores_other_script_types(self):
        fr = A.FetchResult(
            status=200, final_url=HOME, content_type="text/html",
            text=(
                '<html><head>'
                '<script type="application/ld+json">{"a":1}</script>'
                '<script type="application/ld+json">{"b":2}</script>'
                '<script type="application/javascript">var x=1;</script>'
                "</head><body></body></html>"
            ),
        )
        rec = A.process_fetch(HOME, fr, HOST)
        self.assertEqual(rec.jsonld_count, 2)

    def test_canonical_extraction(self):
        url = "https://test.example/a/"
        fr = A.FetchResult(
            status=200, final_url=url, content_type="text/html",
            text='<html><head><link rel="canonical" href="/a/"></head><body></body></html>',
        )
        rec = A.process_fetch(url, fr, HOST)
        self.assertEqual(rec.canonical, url)


class RedirectRecordingTest(unittest.TestCase):
    def test_redirect_detected(self):
        url = "https://test.example/old/"
        fr = A.FetchResult(status=200, final_url="https://test.example/new/?utm=1", text="")
        rec = A.process_fetch(url, fr, HOST)
        self.assertTrue(rec.redirected)
        self.assertEqual(rec.final_url, "https://test.example/new/")

    def test_no_redirect_when_same_url(self):
        url = "https://test.example/old/"
        fr = A.FetchResult(status=200, final_url=url, text="")
        rec = A.process_fetch(url, fr, HOST)
        self.assertFalse(rec.redirected)

    def test_no_redirect_when_final_normalizes_equal(self):
        url = "https://test.example/old/"
        fr = A.FetchResult(status=200, final_url="https://test.example/old?x=1#y", text="")
        rec = A.process_fetch(url, fr, HOST)
        self.assertFalse(rec.redirected)

    def test_status_zero_not_treated_as_redirect(self):
        url = "https://test.example/unreachable/"
        fr = A.FetchResult(status=0, final_url=url)
        rec = A.process_fetch(url, fr, HOST)
        self.assertFalse(rec.redirected)
        self.assertEqual(rec.status, 0)


class CrawlSiteInboundTest(unittest.TestCase):
    def test_bfs_inbound_self_link_excluded_and_straggler_fetched(self):
        a_url = "https://test.example/a/"
        b_url = "https://test.example/b/"
        c_url = "https://test.example/c/"
        orphan_url = "https://test.example/orphan2/"

        pages = {
            HOME: {"text": f'<a href="/a/">a</a><a href="/c/">c</a>'},
            a_url: {"text": f'<a href="/b/">b</a>'},
            b_url: {"text": ""},
            c_url: {"text": '<a href="/c/">self</a>'},
            orphan_url: {"text": ""},
        }
        fetcher = make_pages_fetcher(pages)
        records = A.crawl_site(
            BASE_URL, HOST, mock.Mock(), max_pages=50, delay=0, timeout=15,
            user_agent="ua", effective_limit=None, fetch_fn=fetcher, sleeper=_noop_sleeper,
        )
        self.assertEqual(set(records.keys()), {HOME, a_url, b_url, c_url})
        self.assertNotIn(orphan_url, records, "straggler must not be reached by BFS")

        sitemap_urls = {HOME, a_url, b_url, c_url, orphan_url}
        stragglers = A.fetch_stragglers(
            sitemap_urls, records, HOST, mock.Mock(), 15, "ua", fetcher, _noop_sleeper, 0,
        )
        self.assertEqual(stragglers, {orphan_url})
        self.assertIn(orphan_url, records, "straggler should be fetched in phase 3")

        inbound = A.compute_inbound(records, stragglers)
        self.assertEqual(inbound[c_url], {HOME}, "c's own self-link must not count as inbound")
        self.assertEqual(inbound[a_url], {HOME})
        self.assertEqual(inbound[b_url], {a_url})
        self.assertEqual(inbound[orphan_url], set(), "straggler inbound is forced to 0")


def _build_regression_fixture():
    """host にわたる複数の regression パターンを含む固定サイト。"""
    a = "https://test.example/a/"
    b = "https://test.example/b/"
    old = "https://test.example/old/"
    broken = "https://test.example/broken/"
    noindex_meta = "https://test.example/noindex-meta/"
    noindex_header = "https://test.example/noindex-header/"
    server_error = "https://test.example/server-error/"
    unreachable = "https://test.example/unreachable-exception/"
    orphan = "https://test.example/orphan/"

    sitemap_xml = f"""<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>{HOME}</loc></url>
<url><loc>{a}</loc></url>
<url><loc>{b}</loc></url>
<url><loc>{old}</loc></url>
<url><loc>{orphan}</loc></url>
<url><loc>{noindex_meta}</loc></url>
<url><loc>{noindex_header}</loc></url>
</urlset>"""

    home_html = f'<a href="/a/">a</a><a href="/a/">dup</a><a href="/">self</a><a href="/asset.jpg">img</a><a href="https://other.example/x">ext</a>'

    a_html = (
        '<link rel="canonical" href="/a/">'
        '<script type="application/ld+json">{}</script>'
        '<a href="/b/?x=1#frag">b</a>'
        '<a href="/old">old</a>'
        '<a href="/broken">broken</a>'
        '<a href="/noindex-meta/">nm</a>'
        '<a href="/noindex-header/">nh</a>'
        '<a href="/server-error/">se</a>'
        '<a href="/unreachable-exception/">un</a>'
    )
    b_html = '<link rel="canonical" href="/a/">'  # mismatch: /b/'s own canonical points elsewhere

    pages = {
        "https://test.example/sitemap.xml": {"content_type": "application/xml", "text": sitemap_xml},
        HOME: {"text": home_html},
        a: {"text": a_html},
        b: {"text": b_html},
        old: {"status": 200, "final_url": "https://test.example/new/", "text": ""},
        broken: {"status": 404, "text": ""},
        noindex_meta: {"text": '<meta name="robots" content="NoIndex, follow">'},
        noindex_header: {"text": "", "headers": {"X-Robots-Tag": "NOINDEX"}},
        server_error: {"status": 500, "text": ""},
        unreachable: {"exception": True},
        orphan: {"text": ""},
    }
    return pages, {
        "a": a, "b": b, "old": old, "broken": broken,
        "noindex_meta": noindex_meta, "noindex_header": noindex_header,
        "server_error": server_error, "unreachable": unreachable, "orphan": orphan,
    }


class RunRegressionsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.out_dir = pathlib.Path(self._tmp.name) / "out"

    def tearDown(self):
        self._tmp.cleanup()

    def test_regressions_detected(self):
        pages, urls = _build_regression_fixture()
        fetcher = make_pages_fetcher(pages)
        result = A.run(
            base_url=BASE_URL, max_pages=100, delay=0, timeout=15,
            out_dir=self.out_dir, user_agent="ua", limit=0,
            session=mock.Mock(), fetch_fn=fetcher, sleeper=_noop_sleeper,
            now=datetime(2026, 7, 13, tzinfo=timezone.utc),
        )

        self.assertTrue(result["has_regressions"])
        self.assertEqual(result["sitemap_urls"], 7)
        self.assertEqual(result["pages_crawled"], 10)  # 9 via BFS + 1 straggler (orphan)

        self.assertEqual(len(result["r1_sitemap_broken"]), 1)
        self.assertEqual(result["r1_sitemap_broken"][0]["url"], urls["old"])

        self.assertEqual(
            set(result["r2_sitemap_noindex"]),
            {urls["noindex_meta"], urls["noindex_header"]},
        )

        self.assertEqual(len(result["r3_broken_internal"]), 1)
        self.assertEqual(result["r3_broken_internal"][0]["url"], urls["broken"])
        self.assertIn("https://test.example/a/", result["r3_broken_internal"][0]["sources"])

        r4_urls = {e["url"]: e["status"] for e in result["r4_server_errors"]}
        self.assertEqual(r4_urls.get(urls["server_error"]), 500)
        self.assertEqual(r4_urls.get(urls["unreachable"]), 0)

        self.assertEqual(result["r5_orphans"], [urls["orphan"]])
        self.assertNotIn(HOME, result["r5_orphans"], "homepage must be excluded from orphans")

        self.assertEqual(result["summary"]["canonical_mismatch_count"], 1)
        self.assertEqual(result["summary"]["noindex_inlinked_count"], 2)

    def test_clean_site_has_no_regressions(self):
        good = "https://test.example/good/"
        sitemap_xml = f"""<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>{HOME}</loc></url>
<url><loc>{good}</loc></url>
</urlset>"""
        pages = {
            "https://test.example/sitemap.xml": {"content_type": "application/xml", "text": sitemap_xml},
            HOME: {"text": '<a href="/good/">good</a>'},
            good: {"text": ""},
        }
        fetcher = make_pages_fetcher(pages)
        result = A.run(
            base_url=BASE_URL, max_pages=100, delay=0, timeout=15,
            out_dir=self.out_dir, user_agent="ua", limit=0,
            session=mock.Mock(), fetch_fn=fetcher, sleeper=_noop_sleeper,
        )
        self.assertFalse(result["has_regressions"])
        self.assertEqual(result["r1_sitemap_broken"], [])
        self.assertEqual(result["r2_sitemap_noindex"], [])
        self.assertEqual(result["r3_broken_internal"], [])
        self.assertEqual(result["r4_server_errors"], [])
        self.assertEqual(result["r5_orphans"], [])


class BrokenInternalSourcesCapTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.out_dir = pathlib.Path(self._tmp.name) / "out"

    def tearDown(self):
        self._tmp.cleanup()

    def test_sources_capped_at_five(self):
        broken = "https://test.example/broken2/"
        pn = [f"https://test.example/p{i}/" for i in range(1, 7)]  # 6 distinct linkers

        pages = {
            "https://test.example/sitemap.xml": {"status": 404, "text": ""},
            HOME: {"text": "".join(f'<a href="/p{i}/">p{i}</a>' for i in range(1, 7))},
            broken: {"status": 404, "text": ""},
        }
        for p in pn:
            pages[p] = {"text": '<a href="/broken2/">broken</a>'}

        fetcher = make_pages_fetcher(pages)
        result = A.run(
            base_url=BASE_URL, max_pages=100, delay=0, timeout=15,
            out_dir=self.out_dir, user_agent="ua", limit=0,
            session=mock.Mock(), fetch_fn=fetcher, sleeper=_noop_sleeper,
        )
        self.assertEqual(len(result["r3_broken_internal"]), 1)
        entry = result["r3_broken_internal"][0]
        self.assertEqual(entry["url"], broken)
        self.assertEqual(len(entry["sources"]), 5)
        self.assertEqual(entry["sources"], sorted(pn)[:5])


class OutputFilesTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.out_dir = pathlib.Path(self._tmp.name) / "out"

    def tearDown(self):
        self._tmp.cleanup()

    def test_latest_json_shape_and_history_appends(self):
        good = "https://test.example/good/"
        sitemap_xml = f"""<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>{HOME}</loc></url>
<url><loc>{good}</loc></url>
</urlset>"""
        pages = {
            "https://test.example/sitemap.xml": {"content_type": "application/xml", "text": sitemap_xml},
            HOME: {"text": '<a href="/good/">good</a>'},
            good: {"text": ""},
        }
        fetcher = make_pages_fetcher(pages)

        A.run(
            base_url=BASE_URL, max_pages=100, delay=0, timeout=15,
            out_dir=self.out_dir, user_agent="ua", limit=0,
            session=mock.Mock(), fetch_fn=fetcher, sleeper=_noop_sleeper,
            now=datetime(2026, 7, 1, tzinfo=timezone.utc),
        )
        A.run(
            base_url=BASE_URL, max_pages=100, delay=0, timeout=15,
            out_dir=self.out_dir, user_agent="ua", limit=0,
            session=mock.Mock(), fetch_fn=fetcher, sleeper=_noop_sleeper,
            now=datetime(2026, 7, 8, tzinfo=timezone.utc),
        )

        latest_path = self.out_dir / "latest.json"
        latest = json.loads(latest_path.read_text(encoding="utf-8"))
        self.assertEqual(latest["generated_at"], "2026-07-08T00:00:00Z")
        self.assertEqual(latest["base_url"], BASE_URL)
        for key in (
            "pages_crawled", "sitemap_urls", "has_regressions", "summary",
            "r1_sitemap_broken", "r2_sitemap_noindex", "r3_broken_internal",
            "r4_server_errors", "r5_orphans",
        ):
            self.assertIn(key, latest)

        history_path = self.out_dir / "history.jsonl"
        lines = history_path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 2)
        entries = [json.loads(line) for line in lines]
        self.assertEqual(entries[0]["date"], "2026-07-01")
        self.assertEqual(entries[1]["date"], "2026-07-08")
        for entry in entries:
            for key in ("date", "pages_crawled", "sitemap_urls", "r1", "r2", "r3", "r4", "orphans", "has_regressions"):
                self.assertIn(key, entry)

    def test_inbound_links_json_written_with_provenance(self):
        good = "https://test.example/good/"
        sitemap_xml = f"""<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>{HOME}</loc></url>
<url><loc>{good}</loc></url>
</urlset>"""
        pages = {
            "https://test.example/sitemap.xml": {"content_type": "application/xml", "text": sitemap_xml},
            HOME: {"text": '<a href="/good/">good</a>'},
            good: {"text": ""},
        }
        fetcher = make_pages_fetcher(pages)

        A.run(
            base_url=BASE_URL, max_pages=100, delay=0, timeout=15,
            out_dir=self.out_dir, user_agent="ua", limit=0,
            session=mock.Mock(), fetch_fn=fetcher, sleeper=_noop_sleeper,
            now=datetime(2026, 7, 1, tzinfo=timezone.utc),
        )

        inbound_path = self.out_dir / "inbound_links.json"
        self.assertTrue(inbound_path.exists())
        payload = json.loads(inbound_path.read_text(encoding="utf-8"))
        for key in ("generated_at", "base_url", "pages_crawled", "sitemap_urls", "sources_cap", "links"):
            self.assertIn(key, payload)
        self.assertEqual(payload["generated_at"], "2026-07-01T00:00:00Z")
        self.assertEqual(payload["base_url"], BASE_URL)
        self.assertEqual(payload["sources_cap"], A.INBOUND_LINKS_SOURCES_CAP)
        self.assertEqual(payload["links"][good]["inbound_count"], 1)
        self.assertEqual(payload["links"][good]["sources"], [HOME])
        # latest.json は変更されない (肥大化させない設計)
        latest = json.loads((self.out_dir / "latest.json").read_text(encoding="utf-8"))
        self.assertNotIn("links", latest)


class BuildInboundLinksOutputTest(unittest.TestCase):
    def test_inbound_count_accurate_even_when_sources_capped(self):
        url = "https://test.example/target/"
        sources = {f"https://test.example/src{i}/" for i in range(15)}
        inbound = {url: sources}
        links = A.build_inbound_links_output(inbound, sitemap_urls={url}, sources_cap=5)
        self.assertEqual(links[url]["inbound_count"], 15)
        self.assertEqual(len(links[url]["sources"]), 5)
        # sources は inbound の昇順ソート先頭5件
        self.assertEqual(links[url]["sources"], sorted(sources)[:5])

    def test_sitemap_url_with_zero_inbound_included(self):
        url = "https://test.example/orphan/"
        links = A.build_inbound_links_output({}, sitemap_urls={url})
        self.assertEqual(links[url]["inbound_count"], 0)
        self.assertEqual(links[url]["sources"], [])

    def test_non_sitemap_url_with_inbound_still_included(self):
        url = "https://test.example/not-in-sitemap/"
        inbound = {url: {"https://test.example/other/"}}
        links = A.build_inbound_links_output(inbound, sitemap_urls=set())
        self.assertIn(url, links)
        self.assertEqual(links[url]["inbound_count"], 1)


class FetchUrlExceptionTest(unittest.TestCase):
    def test_fetch_url_catches_connection_error(self):
        session = mock.Mock()
        session.get.side_effect = requests.ConnectionError("boom")
        result = A.fetch_url(session, "https://test.example/x/", 15, "ua")
        self.assertEqual(result.status, 0)
        self.assertEqual(result.final_url, "https://test.example/x/")

    def test_fetch_url_catches_timeout(self):
        session = mock.Mock()
        session.get.side_effect = requests.Timeout("slow")
        result = A.fetch_url(session, "https://test.example/y/", 15, "ua")
        self.assertEqual(result.status, 0)


class RunPerUrlExceptionIntegrationTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.out_dir = pathlib.Path(self._tmp.name) / "out"

    def tearDown(self):
        self._tmp.cleanup()

    def test_single_url_exception_does_not_abort_run(self):
        bad = "https://test.example/bad/"

        def _get(url, **kwargs):
            if url == "https://test.example/sitemap.xml":
                return _mock_response(404, url=url, headers={}, text="")
            if url == HOME:
                return _mock_response(200, url=HOME, headers={"Content-Type": "text/html"}, text='<a href="/bad/">bad</a>')
            if url == bad:
                raise requests.ConnectionError("boom")
            raise AssertionError(f"unexpected url {url}")

        session = mock.Mock()
        session.get.side_effect = _get

        result = A.run(
            base_url=BASE_URL, max_pages=100, delay=0, timeout=15,
            out_dir=self.out_dir, user_agent="ua", limit=0,
            session=session, fetch_fn=A.fetch_url, sleeper=_noop_sleeper,
        )

        self.assertEqual(result["pages_crawled"], 2)
        self.assertTrue(result["has_regressions"])
        r4_urls = {e["url"]: e["status"] for e in result["r4_server_errors"]}
        self.assertEqual(r4_urls.get(bad), 0)


class CanonicalMismatchPatternTest(unittest.TestCase):
    def test_scheme_mismatch(self):
        self.assertEqual(
            A._classify_canonical_mismatch("https://test.example/a/", "http://test.example/a/"),
            "scheme",
        )

    def test_host_mismatch(self):
        self.assertEqual(
            A._classify_canonical_mismatch("https://test.example/a/", "https://other.example/a/"),
            "host",
        )

    def test_query_mismatch_same_path(self):
        self.assertEqual(
            A._classify_canonical_mismatch(
                "https://test.example/a/?x=1", "https://test.example/a/?x=2"
            ),
            "query",
        )

    def test_trailing_slash_only(self):
        self.assertEqual(
            A._classify_canonical_mismatch("https://test.example/a", "https://test.example/a/"),
            "trailing_slash",
        )

    def test_path_mismatch(self):
        self.assertEqual(
            A._classify_canonical_mismatch("https://test.example/a/", "https://test.example/b/"),
            "path",
        )

    def test_other_fragment_only(self):
        self.assertEqual(
            A._classify_canonical_mismatch(
                "https://test.example/a/#foo", "https://test.example/a/#bar"
            ),
            "other",
        )

    def test_priority_scheme_wins_over_path(self):
        """scheme と path が同時に違う場合は scheme が優先される。"""
        self.assertEqual(
            A._classify_canonical_mismatch("https://test.example/a/", "http://test.example/b/"),
            "scheme",
        )

    def test_priority_host_wins_over_query(self):
        self.assertEqual(
            A._classify_canonical_mismatch(
                "https://test.example/a/?x=1", "https://other.example/a/?x=2"
            ),
            "host",
        )

    def test_priority_path_wins_over_query_when_both_differ(self):
        self.assertEqual(
            A._classify_canonical_mismatch(
                "https://test.example/a/?x=1", "https://test.example/b/?x=2"
            ),
            "path",
        )


class NoindexInlinkedPatternTest(unittest.TestCase):
    def test_first_segment_used(self):
        self.assertEqual(
            A._classify_noindex_inlinked("https://test.example/search/foo/"), "search/",
        )
        self.assertEqual(
            A._classify_noindex_inlinked("https://test.example/products/b0x/"), "products/",
        )

    def test_root_url(self):
        self.assertEqual(A._classify_noindex_inlinked("https://test.example/"), "(root)")


class PatternCountsOrderingTest(unittest.TestCase):
    def test_ordered_by_count_desc_then_name_asc(self):
        patterns = ["path", "query", "path", "host", "query", "path"]
        result = A._pattern_counts(patterns)
        self.assertEqual(list(result.items()), [("path", 3), ("query", 2), ("host", 1)])

    def test_tie_broken_by_name_ascending(self):
        patterns = ["zeta", "alpha", "zeta", "alpha"]
        result = A._pattern_counts(patterns)
        self.assertEqual(list(result.items()), [("alpha", 2), ("zeta", 2)])


class CanonicalNoindexDetailsTest(unittest.TestCase):
    """canonical_mismatch / noindex_inlinked の明細出力まわり (#3727)。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.out_dir = pathlib.Path(self._tmp.name) / "out"

    def tearDown(self):
        self._tmp.cleanup()

    def _run_fixture(self):
        pages, urls = _build_regression_fixture()
        fetcher = make_pages_fetcher(pages)
        result = A.run(
            base_url=BASE_URL, max_pages=100, delay=0, timeout=15,
            out_dir=self.out_dir, user_agent="ua", limit=0,
            session=mock.Mock(), fetch_fn=fetcher, sleeper=_noop_sleeper,
            now=datetime(2026, 7, 13, tzinfo=timezone.utc),
        )
        return result, urls

    def test_summary_counts_match_details_length(self):
        result, _ = self._run_fixture()
        latest_details = json.loads((self.out_dir / "latest_details.json").read_text(encoding="utf-8"))
        self.assertEqual(
            result["summary"]["canonical_mismatch_count"],
            len(latest_details["canonical_mismatch"]),
        )
        self.assertEqual(
            result["summary"]["noindex_inlinked_count"],
            len(latest_details["noindex_inlinked"]),
        )

    def test_patterns_present_in_summary(self):
        result, _ = self._run_fixture()
        self.assertIn("canonical_mismatch_patterns", result["summary"])
        self.assertIn("noindex_inlinked_patterns", result["summary"])
        # fixture: /b/'s canonical is /a/ -> same host/scheme, different path -> "path"
        self.assertEqual(result["summary"]["canonical_mismatch_patterns"], {"path": 1})
        # noindex-meta and noindex-header are both first-segment matches of themselves
        self.assertEqual(
            result["summary"]["noindex_inlinked_patterns"],
            {"noindex-header/": 1, "noindex-meta/": 1},
        )

    def test_latest_details_file_has_full_entries(self):
        result, urls = self._run_fixture()
        latest_details = json.loads((self.out_dir / "latest_details.json").read_text(encoding="utf-8"))
        self.assertEqual(latest_details["generated_at"], result["generated_at"])
        self.assertEqual(latest_details["base_url"], result["base_url"])

        cm = latest_details["canonical_mismatch"]
        self.assertEqual(len(cm), 1)
        self.assertEqual(cm[0]["url"], urls["b"])
        self.assertEqual(cm[0]["canonical"], urls["a"])
        self.assertEqual(cm[0]["pattern"], "path")

        ni = latest_details["noindex_inlinked"]
        self.assertEqual(len(ni), 2)
        ni_urls = {e["url"] for e in ni}
        self.assertEqual(ni_urls, {urls["noindex_meta"], urls["noindex_header"]})
        for entry in ni:
            self.assertIn("inbound_count", entry)
            self.assertIn("sources", entry)
            self.assertIn("pattern", entry)

    def test_latest_json_excludes_details(self):
        self._run_fixture()
        latest = json.loads((self.out_dir / "latest.json").read_text(encoding="utf-8"))
        self.assertNotIn("canonical_mismatch", latest)
        self.assertNotIn("noindex_inlinked", latest)
        summary = latest["summary"]
        self.assertIn("canonical_mismatch_count", summary)
        self.assertIn("noindex_inlinked_count", summary)
        self.assertIn("canonical_mismatch_patterns", summary)
        self.assertIn("noindex_inlinked_patterns", summary)

    def test_details_sorted_by_url(self):
        """明細は url 昇順であること (noindex 側で複数件を確認)。"""
        result, urls = self._run_fixture()
        latest_details = json.loads((self.out_dir / "latest_details.json").read_text(encoding="utf-8"))
        ni_urls = [e["url"] for e in latest_details["noindex_inlinked"]]
        self.assertEqual(ni_urls, sorted(ni_urls))


class MainExitCodeTest(unittest.TestCase):
    def test_fail_on_regression_exits_2(self):
        fake_result = {"has_regressions": True}
        with mock.patch.object(A, "run", return_value=fake_result), \
             mock.patch.object(sys, "argv", ["prog", "--fail-on-regression"]):
            with self.assertRaises(SystemExit) as cm:
                A.main()
            self.assertEqual(cm.exception.code, 2)

    def test_default_exits_zero_even_with_regressions(self):
        fake_result = {"has_regressions": True}
        with mock.patch.object(A, "run", return_value=fake_result), \
             mock.patch.object(sys, "argv", ["prog"]):
            A.main()  # must not raise SystemExit

    def test_fail_on_regression_no_regressions_does_not_exit(self):
        fake_result = {"has_regressions": False}
        with mock.patch.object(A, "run", return_value=fake_result), \
             mock.patch.object(sys, "argv", ["prog", "--fail-on-regression"]):
            A.main()  # must not raise SystemExit


if __name__ == "__main__":
    unittest.main()
