"""scripts/collect_first_party_sources.py unit tests (omcha-ops#264 A/D)。

ネットワークは一切叩かない (requests.Session を mock、sleeper を注入)。

カバレッジ:
1. ASIN 抽出 (extract_asin_mentions): dp/gp/product/asin= の 3 パターン・大小文字・出現回数
2. 一人称マーカー数 / 自前画像数のカウント
3. 役割判定 (determine_roles): title_match / max_mentions (同率含む) / compared
4. カタログ構築 (build_asin_title_catalog): amazon.json を articles で上書き
5. in_corpus / has_experience 集合構築
6. WP REST パース (parse_post_list_entry) と一覧取得 (fetch_post_list) のページング
7. 本文取得 (fetch_post_content): リトライ後成功・上限到達で None
8. 収集本体 (collect): modified 変化なしでのキャッシュ再利用 (本文再取得しない)・
   sources / uncatalogued の組み立て
9. first_party_pool 選定 (build_first_party_pool)
"""
from __future__ import annotations

import json
import pathlib
import tempfile
import unittest
from unittest import mock

import requests

from scripts.collect_first_party_sources import (
    TruncatedCollectionError,
    assert_not_truncated,
    build_asin_title_catalog,
    build_first_party_pool,
    build_pool_excluded,
    build_uncatalogued,
    collect,
    count_fp_markers,
    count_own_images,
    determine_roles,
    extract_asin_mentions,
    fetch_post_content,
    fetch_post_list,
    load_has_experience_asins,
    load_in_corpus_asins,
    parse_post_list_entry,
)


def _no_sleep(_seconds):
    pass


def _write_json(path: pathlib.Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


class ExtractAsinMentionsTest(unittest.TestCase):
    def test_dp_link(self):
        html = '<a href="https://www.amazon.co.jp/dp/B009CMIFYA/ref=xyz">買う</a>'
        self.assertEqual(extract_asin_mentions(html), {"B009CMIFYA": 1})

    def test_gp_product_link(self):
        html = '<a href="https://www.amazon.co.jp/gp/product/B074N9HWVW">買う</a>'
        self.assertEqual(extract_asin_mentions(html), {"B074N9HWVW": 1})

    def test_asin_query_param(self):
        html = '<a href="https://www.amazon.co.jp/exec/obidos/ASIN/B01N11I7NO?asin=B01N11I7NO">買う</a>'
        # /ASIN/... は対象パターンではないが ?asin= は拾う
        self.assertEqual(extract_asin_mentions(html), {"B01N11I7NO": 1})

    def test_counts_multiple_occurrences(self):
        html = (
            '<a href="/dp/B009CMIFYA">1</a>'
            '<a href="/dp/B009CMIFYA">2</a>'
            '<a href="/dp/B074N9HWVW">3</a>'
        )
        self.assertEqual(extract_asin_mentions(html), {"B009CMIFYA": 2, "B074N9HWVW": 1})

    def test_lowercase_normalized_to_upper(self):
        html = '<a href="/dp/b009cmifya">買う</a>'
        self.assertEqual(extract_asin_mentions(html), {"B009CMIFYA": 1})

    def test_empty_or_non_string(self):
        self.assertEqual(extract_asin_mentions(""), {})
        self.assertEqual(extract_asin_mentions(None), {})


class CountFpMarkersTest(unittest.TestCase):
    def test_sums_all_marker_occurrences(self):
        html = "<p>うちの子が実際に使ってみた感想です。我が家でも実際に活躍中。</p>"
        # うちの子=1, 実際に=2, 使って=1, 我が家=1 -> 5
        self.assertEqual(count_fp_markers(html), 5)

    def test_no_markers(self):
        self.assertEqual(count_fp_markers("<p>商品スペックの説明です。</p>"), 0)


class CountOwnImagesTest(unittest.TestCase):
    def test_counts_upload_urls(self):
        html = (
            '<img src="https://omcha.jp/wp-content/uploads/2026/01/a.jpg">'
            '<img src="https://omcha.jp/wp-content/uploads/2026/01/b.jpg">'
            '<img src="https://m.media-amazon.com/images/c.jpg">'
        )
        self.assertEqual(count_own_images(html), 2)

    def test_non_string(self):
        self.assertEqual(count_own_images(None), 0)


class DetermineRolesTest(unittest.TestCase):
    """primary = title_match、または 最多 かつ share >= PRIMARY_SHARE_FLOOR。"""

    def test_title_match_wins_over_mentions(self):
        counts = {"B009CMIFYA": 1, "B074N9HWVW": 3}
        catalog = {"B009CMIFYA": "ベビードラム"}
        roles = determine_roles(counts, "ベビードラムを徹底レビュー", catalog)
        self.assertEqual(roles["B009CMIFYA"], ("primary", "title_match"))
        # 3/4 = 75% で過半も満たす
        self.assertEqual(roles["B074N9HWVW"], ("primary", "mention_share"))

    def test_title_match_rescues_non_max_mentions(self):
        counts = {"B009CMIFYA": 1, "B074N9HWVW": 5, "B01N11I7NO": 2}
        catalog = {"B009CMIFYA": "ベビードラム"}
        roles = determine_roles(counts, "ベビードラムを徹底レビュー", catalog)
        self.assertEqual(roles["B009CMIFYA"], ("primary", "title_match"))
        self.assertEqual(roles["B074N9HWVW"], ("primary", "mention_share"))  # 5/8 = 63%
        self.assertEqual(roles["B01N11I7NO"], ("compared", "mention_share"))

    def test_two_way_tie_stays_primary(self):
        # 手検証の正解: bebydrum 記事は 2 商品が各 6 回 (share 0.50 ちょうど) で両方 primary。
        counts = {"B009CMIFYA": 6, "B074N9HWVW": 6}
        roles = determine_roles(counts, "2つのおもちゃを比較", {})
        self.assertEqual(roles["B009CMIFYA"], ("primary", "mention_share"))
        self.assertEqual(roles["B074N9HWVW"], ("primary", "mention_share"))

    def test_roundup_with_all_tied_has_no_primary(self):
        # 実測 2026-09-17: 商品紹介ですらない記事で 6 商品が各 4 回 (カードリンクの
        # 定型) 並び、設計どおりの「最多なら全部 primary」では 6 件とも primary に
        # なっていた。share 1/6 = 17% で全部落ちる。
        counts = {f"B00000000{i}": 4 for i in range(6)}
        roles = determine_roles(counts, "テーマパーク紹介", {})
        self.assertEqual({r for r, _ in roles.values()}, {"compared"})

    def test_three_way_tie_is_not_primary(self):
        counts = {"A": 2, "B": 2, "C": 2}
        roles = determine_roles(counts, "3つ比較", {})
        self.assertEqual({r for r, _ in roles.values()}, {"compared"})

    def test_single_asin_is_primary(self):
        roles = determine_roles({"B009CMIFYA": 1}, "レビュー", {})
        self.assertEqual(roles["B009CMIFYA"], ("primary", "mention_share"))

    def test_non_max_without_title_match_is_compared(self):
        counts = {"B009CMIFYA": 1, "B074N9HWVW": 5}
        roles = determine_roles(counts, "何かのタイトル", {})
        self.assertEqual(roles["B009CMIFYA"], ("compared", "mention_share"))
        self.assertEqual(roles["B074N9HWVW"], ("primary", "mention_share"))

    def test_max_below_the_share_floor_is_compared(self):
        # 最多でも share が PRIMARY_SHARE_FLOOR に届かなければ primary にしない
        # (4/12 = 33%)
        counts = {"A": 4, "B": 3, "C": 3, "D": 2}
        roles = determine_roles(counts, "まとめ記事", {})
        self.assertEqual(roles["A"], ("compared", "mention_share"))

    def test_empty_counts(self):
        self.assertEqual(determine_roles({}, "title", {}), {})


class BuildAsinTitleCatalogTest(unittest.TestCase):
    def test_articles_override_amazon_json(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            amazon_json = root / "amazon.json"
            _write_json(amazon_json, {"items": [
                {"asin": "B009CMIFYA", "title": "ベビードラム おもちゃ｜0歳からの知育玩具"},
                {"asin": "B074N9HWVW", "title": "積み木セット"},
            ]})
            articles_dir = root / "articles"
            _write_json(articles_dir / "add-article-B009CMIFYA.json", {
                "title": "ベビードラム レビュー",
                "product": {"name": "ベビードラム"},
            })
            catalog = build_asin_title_catalog(articles_dir, amazon_json)
            self.assertEqual(catalog["B009CMIFYA"], "ベビードラム")  # articles 側の product.name で上書き
            self.assertEqual(catalog["B074N9HWVW"], "積み木セット")  # amazon.json 由来のまま

    def test_missing_files_return_empty(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            catalog = build_asin_title_catalog(root / "articles", root / "amazon.json")
            self.assertEqual(catalog, {})


class LoadCorpusSetsTest(unittest.TestCase):
    def test_load_in_corpus_asins(self):
        with tempfile.TemporaryDirectory() as td:
            amazon_json = pathlib.Path(td) / "amazon.json"
            _write_json(amazon_json, {"items": [{"asin": "b009cmifya"}, {"asin": "B074N9HWVW"}]})
            self.assertEqual(load_in_corpus_asins(amazon_json), {"B009CMIFYA", "B074N9HWVW"})

    def test_load_has_experience_asins(self):
        with tempfile.TemporaryDirectory() as td:
            per_asin_dir = pathlib.Path(td) / "per_asin"
            _write_json(per_asin_dir / "B009CMIFYA" / "experience.json", {"snippets": []})
            (per_asin_dir / "B074N9HWVW").mkdir(parents=True)
            self.assertEqual(load_has_experience_asins(per_asin_dir), {"B009CMIFYA"})

    def test_missing_dir_returns_empty(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(load_has_experience_asins(pathlib.Path(td) / "nope"), set())


class ParsePostListEntryTest(unittest.TestCase):
    def test_valid(self):
        raw = {
            "id": 1,
            "link": "https://omcha.jp/bebydrum/",
            "title": {"rendered": "ベビードラム レビュー"},
            "modified": "2026-09-01T00:00:00",
        }
        self.assertEqual(parse_post_list_entry(raw), {
            "id": 1, "link": "https://omcha.jp/bebydrum/", "title": "ベビードラム レビュー",
            "modified": "2026-09-01T00:00:00",
        })

    def test_missing_modified_returns_none(self):
        raw = {"id": 1, "link": "https://omcha.jp/x/", "title": {"rendered": "x"}}
        self.assertIsNone(parse_post_list_entry(raw))

    def test_non_dict_returns_none(self):
        self.assertIsNone(parse_post_list_entry("not a dict"))


class FetchPostListTest(unittest.TestCase):
    def _mock_session(self, pages: list[list[dict]]):
        session = mock.Mock(spec=requests.Session)
        responses = []
        for page in pages:
            resp = mock.Mock()
            resp.status_code = 200
            resp.raise_for_status = mock.Mock()
            resp.json.return_value = page
            responses.append(resp)
        # 最終ページの次は 400 (終端)
        end_resp = mock.Mock()
        end_resp.status_code = 400
        responses.append(end_resp)
        session.get.side_effect = responses
        return session

    def test_paginates_until_short_page(self):
        page1 = [
            {"id": i, "link": f"https://omcha.jp/p{i}/", "title": {"rendered": f"t{i}"}, "modified": "2026-01-01"}
            for i in range(100)
        ]
        page2 = [{"id": 100, "link": "https://omcha.jp/p100/", "title": {"rendered": "t100"}, "modified": "2026-01-01"}]
        session = self._mock_session([page1, page2])
        posts = fetch_post_list("https://omcha.jp", session, sleep_seconds=0, sleeper=_no_sleep)
        self.assertEqual(len(posts), 101)
        self.assertEqual(session.get.call_count, 2)  # 短いページ2で終端、400ページは呼ばれない

    def test_limit_stops_early(self):
        page1 = [
            {"id": i, "link": f"https://omcha.jp/p{i}/", "title": {"rendered": f"t{i}"}, "modified": "2026-01-01"}
            for i in range(100)
        ]
        session = self._mock_session([page1])
        posts = fetch_post_list("https://omcha.jp", session, sleep_seconds=0, limit=5, sleeper=_no_sleep)
        self.assertEqual(len(posts), 5)


class FetchPostContentTest(unittest.TestCase):
    def test_success(self):
        session = mock.Mock(spec=requests.Session)
        resp = mock.Mock()
        resp.raise_for_status = mock.Mock()
        resp.json.return_value = {"content": {"rendered": "<p>本文</p>"}}
        session.get.return_value = resp
        content = fetch_post_content(1, "https://omcha.jp", session, sleeper=_no_sleep)
        self.assertEqual(content, "<p>本文</p>")

    def test_retries_then_succeeds(self):
        session = mock.Mock(spec=requests.Session)
        fail_resp = mock.Mock()
        fail_resp.raise_for_status.side_effect = requests.RequestException("boom")
        ok_resp = mock.Mock()
        ok_resp.raise_for_status = mock.Mock()
        ok_resp.json.return_value = {"content": {"rendered": "ok"}}
        session.get.side_effect = [fail_resp, ok_resp]
        content = fetch_post_content(1, "https://omcha.jp", session, sleeper=_no_sleep)
        self.assertEqual(content, "ok")

    def test_exhausts_retries_returns_none(self):
        session = mock.Mock(spec=requests.Session)
        fail_resp = mock.Mock()
        fail_resp.raise_for_status.side_effect = requests.RequestException("boom")
        session.get.return_value = fail_resp
        content = fetch_post_content(1, "https://omcha.jp", session, sleeper=_no_sleep)
        self.assertIsNone(content)


class CollectTest(unittest.TestCase):
    def _setup_corpus(self, root: pathlib.Path):
        amazon_json = root / "amazon.json"
        _write_json(amazon_json, {"items": [{"asin": "B009CMIFYA"}, {"asin": "B074N9HWVW"}]})
        articles_dir = root / "articles"
        articles_dir.mkdir()
        per_asin_dir = root / "per_asin"
        per_asin_dir.mkdir()
        return amazon_json, articles_dir, per_asin_dir

    def test_builds_sources_and_pool_candidates(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            amazon_json, articles_dir, per_asin_dir = self._setup_corpus(root)

            session = mock.Mock(spec=requests.Session)
            with mock.patch(
                "scripts.collect_first_party_sources.fetch_post_list",
                return_value=[{
                    "id": 1, "link": "https://omcha.jp/bebydrum/", "title": "ベビードラム 比較レビュー",
                    "modified": "2026-09-01T00:00:00",
                }],
            ), mock.patch(
                "scripts.collect_first_party_sources.fetch_post_content",
                return_value=(
                    '<a href="/dp/B009CMIFYA">A</a><a href="/dp/B009CMIFYA">A2</a>'
                    '<a href="/dp/B074N9HWVW">B</a>'
                ),
            ) as mock_content:
                result = collect(
                    "https://omcha.jp", session, {},
                    articles_dir=articles_dir, amazon_json_path=amazon_json, per_asin_dir=per_asin_dir,
                    sleep_seconds=0, sleeper=_no_sleep,
                )
                mock_content.assert_called_once()

            asins = {r["asin"]: r for r in result["sources"]}
            # 2/3 = 67% で最多かつ過半
            self.assertEqual(asins["B009CMIFYA"]["role"], "primary")
            self.assertEqual(asins["B009CMIFYA"]["role_reason"], "mention_share")
            self.assertEqual(asins["B074N9HWVW"]["role"], "compared")
            self.assertTrue(asins["B009CMIFYA"]["in_corpus"])
            self.assertFalse(asins["B009CMIFYA"]["has_article"])

            pool = build_first_party_pool(result["sources"])
            self.assertEqual(pool, ["B009CMIFYA"])  # primary かつ未記事化

    def test_unchanged_modified_skips_content_refetch(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            amazon_json, articles_dir, per_asin_dir = self._setup_corpus(root)
            session = mock.Mock(spec=requests.Session)
            previous = {
                "posts_cache": {
                    "1": {
                        "modified": "2026-09-01T00:00:00",
                        "asin_counts": {"B009CMIFYA": 1},
                        "fp_markers": 0,
                        "own_images": 0,
                        "link": "https://omcha.jp/bebydrum/",
                        "title": "旧タイトル",
                    }
                }
            }
            with mock.patch(
                "scripts.collect_first_party_sources.fetch_post_list",
                return_value=[{
                    "id": 1, "link": "https://omcha.jp/bebydrum/", "title": "ベビードラム",
                    "modified": "2026-09-01T00:00:00",
                }],
            ), mock.patch("scripts.collect_first_party_sources.fetch_post_content") as mock_content:
                result = collect(
                    "https://omcha.jp", session, previous,
                    articles_dir=articles_dir, amazon_json_path=amazon_json, per_asin_dir=per_asin_dir,
                    sleep_seconds=0, sleeper=_no_sleep,
                )
                mock_content.assert_not_called()  # modified 不変 -> 本文再取得しない
            self.assertEqual(result["sources"][0]["asin"], "B009CMIFYA")
            self.assertEqual(result["sources"][0]["post_title"], "ベビードラム")  # title は最新一覧の値で更新

    def test_no_asins_in_post_produces_no_sources(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            amazon_json, articles_dir, per_asin_dir = self._setup_corpus(root)
            session = mock.Mock(spec=requests.Session)
            with mock.patch(
                "scripts.collect_first_party_sources.fetch_post_list",
                return_value=[{
                    "id": 2, "link": "https://omcha.jp/no-link/", "title": "リンクなし記事",
                    "modified": "2026-09-01T00:00:00",
                }],
            ), mock.patch(
                "scripts.collect_first_party_sources.fetch_post_content", return_value="<p>本文のみ</p>",
            ):
                result = collect(
                    "https://omcha.jp", session, {},
                    articles_dir=articles_dir, amazon_json_path=amazon_json, per_asin_dir=per_asin_dir,
                    sleep_seconds=0, sleeper=_no_sleep,
                )
            self.assertEqual(result["sources"], [])
            self.assertEqual(result["uncatalogued"], [])


class BuildUncataloguedTest(unittest.TestCase):
    """D: navi に記事が無い ASIN の一覧 (in_corpus では絞らない)。"""

    def test_prefers_primary_occurrence(self):
        sources = [
            {"asin": "B0XXXXXXXX", "role": "compared", "role_reason": "mention_share",
             "post_url": "https://omcha.jp/a/", "post_title": "a", "has_article": False},
            {"asin": "B0XXXXXXXX", "role": "primary", "role_reason": "title_match",
             "post_url": "https://omcha.jp/b/", "post_title": "b", "has_article": False},
        ]
        out = build_uncatalogued(sources)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["role"], "primary")
        self.assertEqual(out[0]["post_url"], "https://omcha.jp/b/")

    def test_excludes_asins_that_already_have_an_article(self):
        sources = [
            {"asin": "B0XXXXXXXX", "role": "primary", "role_reason": "mention_share",
             "post_url": "u", "post_title": "t", "has_article": True},
        ]
        self.assertEqual(build_uncatalogued(sources), [])

    def test_excludes_compared_only_asins(self):
        # 比較対象として並んでいるだけの ASIN は「実体験の取りこぼし」ではない。
        sources = [
            {"asin": "B0XXXXXXXX", "role": "compared", "role_reason": "mention_share",
             "post_url": "u", "post_title": "t", "has_article": False},
        ]
        self.assertEqual(build_uncatalogued(sources), [])

    def test_in_corpus_does_not_filter(self):
        # in_corpus の出所 (amazon.json) は日次 fetch の作業セットなので絞りに使わない。
        sources = [
            {"asin": "B0XXXXXXXX", "role": "primary", "role_reason": "mention_share",
             "post_url": "u", "post_title": "t", "has_article": False, "in_corpus": True},
        ]
        self.assertEqual(len(build_uncatalogued(sources)), 1)


class BuildFirstPartyPoolTest(unittest.TestCase):
    def test_filters_primary_without_article(self):
        sources = [
            {"asin": "B00000000A", "role": "primary", "in_corpus": True, "has_article": False},
            {"asin": "B00000000B", "role": "primary", "in_corpus": True, "has_article": True},
            {"asin": "B00000000C", "role": "primary", "in_corpus": False, "has_article": False},
            {"asin": "B00000000D", "role": "compared", "in_corpus": True, "has_article": False},
        ]
        # B00000000C は in_corpus=False でも入る (消費側 #7511 は候補列に直接前置するため)
        self.assertEqual(build_first_party_pool(sources), ["B00000000A", "B00000000C"])

    def test_excludes_non_b0_asins(self):
        # ISBN 形式 (紙の書籍) は消費側 (03-invoke-jules.yml の _ASIN_RE) が構造的に
        # 消費できないのでプールに載せない (#7573)。
        sources = [
            {"asin": "B00000000A", "role": "primary", "in_corpus": True, "has_article": False},
            {"asin": "4023333859", "role": "primary", "in_corpus": True, "has_article": False},
            {"asin": "000838214X", "role": "primary", "in_corpus": True, "has_article": False},
        ]
        self.assertEqual(build_first_party_pool(sources), ["B00000000A"])


class BuildPoolExcludedTest(unittest.TestCase):
    def test_returns_only_non_b0_asins_from_the_pool_population(self):
        sources = [
            {"asin": "B00000000A", "role": "primary", "has_article": False,
             "post_url": "https://omcha.jp/a/", "post_title": "a"},
            {"asin": "4023333859", "role": "primary", "has_article": False,
             "post_url": "https://omcha.jp/b/", "post_title": "b"},
        ]
        self.assertEqual(build_pool_excluded(sources), [
            {"asin": "4023333859", "reason": "non_b0_asin",
             "post_url": "https://omcha.jp/b/", "post_title": "b"},
        ])

    def test_dedupes_by_asin_ascending(self):
        sources = [
            {"asin": "000838214X", "role": "primary", "has_article": False,
             "post_url": "https://omcha.jp/z/", "post_title": "z"},
            {"asin": "4023333859", "role": "primary", "has_article": False,
             "post_url": "https://omcha.jp/y/", "post_title": "y"},
            {"asin": "4023333859", "role": "primary", "has_article": False,
             "post_url": "https://omcha.jp/x/", "post_title": "x"},
        ]
        out = build_pool_excluded(sources)
        self.assertEqual([r["asin"] for r in out], ["000838214X", "4023333859"])
        self.assertEqual(out[1]["post_url"], "https://omcha.jp/y/")  # 最初に見つけた 1 件を採る

    def test_excludes_non_primary_and_already_articled(self):
        sources = [
            {"asin": "4023333859", "role": "compared", "has_article": False,
             "post_url": "u", "post_title": "t"},
            {"asin": "000838214X", "role": "primary", "has_article": True,
             "post_url": "u", "post_title": "t"},
        ]
        self.assertEqual(build_pool_excluded(sources), [])

    def test_b0_asins_are_not_excluded(self):
        sources = [
            {"asin": "B00000000A", "role": "primary", "has_article": False,
             "post_url": "u", "post_title": "t"},
        ]
        self.assertEqual(build_pool_excluded(sources), [])


class AssertNotTruncatedTest(unittest.TestCase):
    """途中で切れた収集結果で posts_cache を上書きしない (auto-merge の手前で落とす)。"""

    @staticmethod
    def _cache(n):
        return {"posts_cache": {str(i): {} for i in range(n)}}

    def test_raises_when_post_list_shrinks_past_the_floor(self):
        with self.assertRaises(TruncatedCollectionError):
            assert_not_truncated(self._cache(800), self._cache(400))

    def test_allows_a_small_dip(self):
        assert_not_truncated(self._cache(800), self._cache(760))

    def test_allows_growth(self):
        assert_not_truncated(self._cache(800), self._cache(830))

    def test_first_run_has_no_baseline(self):
        assert_not_truncated({}, self._cache(5))

    def test_limit_smoke_run_is_exempt(self):
        assert_not_truncated(self._cache(800), self._cache(20), limit=20)


if __name__ == "__main__":
    unittest.main()
