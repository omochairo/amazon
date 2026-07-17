"""scripts/build_wp_navi_link_candidates.py unit tests (#3333 Phase 1)。

カバレッジ:
1. WP REST パース (parse_wp_post): HTML/エンティティ除去・不正要素のスキップ
2. WP 取得のページネーション (fetch_wp_posts): 低レート sleep・最終ページ判定・
   400 (ページ範囲外) での終端・limit 打ち切り
3. navi 候補カタログ構築 (build_hub_candidates / build_product_candidates):
   固定データの形状・記事 JSON からのフィールド組み立て・欠損耐性
4. 類似度計算 (cosine_similarity_cross): 直交・同方向・非正方行列
5. 候補選定 (select_navi_candidates_for_wp): 閾値フィルタ・top_k 切り詰め・
   決定的な同点順序・reranker 併用時の並べ替え・reranker 失敗時のフォールバック
6. Ruri クライアント (embed_batch_ruri/rerank_candidates): リトライ後成功・
   リトライ上限到達時の挙動 (例外 / None フォールバック)
7. レポート整形 (render_markdown_report): 空/非空の出力形状
8. run() の E2E (HTTP はモック): レポート書き込み・embed 失敗時の abort
"""
from __future__ import annotations

import json
import pathlib
import tempfile
import unittest
from unittest import mock

import requests

from scripts.build_wp_navi_link_candidates import (
    DIAGNOSIS_HUB,
    EmbeddingBatchError,
    build_hub_candidates,
    build_navi_candidates,
    build_product_candidates,
    cosine_similarity_cross,
    embed_batch_ruri,
    fetch_wp_posts,
    parse_wp_post,
    render_markdown_report,
    rerank_candidates,
    run,
    select_navi_candidates_for_wp,
    strip_html,
)


def _write_json(path: pathlib.Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _no_sleep(_seconds):
    pass


# --------------------------------------------------------------------------
# strip_html / parse_wp_post
# --------------------------------------------------------------------------

class StripHtmlTest(unittest.TestCase):
    def test_removes_tags_and_unescapes_entities(self):
        raw = "<p>おもちゃの&#8220;選び方&#8221;を解説</p>\n<p>続き</p>"
        self.assertEqual(strip_html(raw), "おもちゃの“選び方”を解説 続き")

    def test_non_string_returns_empty(self):
        self.assertEqual(strip_html(None), "")
        self.assertEqual(strip_html(123), "")

    def test_empty_string(self):
        self.assertEqual(strip_html(""), "")


class ParseWpPostTest(unittest.TestCase):
    def test_valid_post(self):
        raw = {
            "id": 42,
            "link": "https://omcha.jp/diaper-review/",
            "title": {"rendered": "おむつ比較レビュー"},
            "excerpt": {"rendered": "<p>おむつを徹底比較しました。</p>\n"},
        }
        out = parse_wp_post(raw)
        self.assertEqual(out, {
            "id": 42,
            "link": "https://omcha.jp/diaper-review/",
            "title": "おむつ比較レビュー",
            "excerpt": "おむつを徹底比較しました。",
        })

    def test_missing_title_dropped(self):
        raw = {"id": 1, "link": "https://omcha.jp/x/", "title": {"rendered": ""}, "excerpt": {"rendered": ""}}
        self.assertIsNone(parse_wp_post(raw))

    def test_missing_id_dropped(self):
        raw = {"link": "https://omcha.jp/x/", "title": {"rendered": "t"}, "excerpt": {"rendered": ""}}
        self.assertIsNone(parse_wp_post(raw))

    def test_missing_link_dropped(self):
        raw = {"id": 1, "title": {"rendered": "t"}, "excerpt": {"rendered": ""}}
        self.assertIsNone(parse_wp_post(raw))

    def test_not_a_dict(self):
        self.assertIsNone(parse_wp_post("not a dict"))
        self.assertIsNone(parse_wp_post(None))

    def test_missing_excerpt_ok(self):
        raw = {"id": 1, "link": "https://omcha.jp/x/", "title": {"rendered": "t"}}
        out = parse_wp_post(raw)
        self.assertEqual(out["excerpt"], "")


# --------------------------------------------------------------------------
# fetch_wp_posts
# --------------------------------------------------------------------------

def _resp(status_code: int, payload):
    r = mock.Mock()
    r.status_code = status_code
    if status_code >= 400:
        r.raise_for_status = mock.Mock(side_effect=requests.HTTPError(f"{status_code}"))
    else:
        r.raise_for_status = mock.Mock()
    r.json = mock.Mock(return_value=payload)
    return r


def _wp_raw(post_id: int) -> dict:
    return {
        "id": post_id,
        "link": f"https://omcha.jp/post-{post_id}/",
        "title": {"rendered": f"タイトル{post_id}"},
        "excerpt": {"rendered": f"抜粋{post_id}"},
    }


class FetchWpPostsTest(unittest.TestCase):
    def test_single_short_page_stops_pagination(self):
        session = mock.Mock()
        session.get.return_value = _resp(200, [_wp_raw(1), _wp_raw(2)])
        sleeper = mock.Mock()
        posts = fetch_wp_posts(
            "https://omcha.jp", session, per_page=100, sleep_seconds=1.0, sleeper=sleeper,
        )
        self.assertEqual([p["id"] for p in posts], [1, 2])
        self.assertEqual(session.get.call_count, 1)
        sleeper.assert_not_called()  # 最終ページの後は sleep しない

    def test_full_page_then_short_page_paginates_with_sleep(self):
        session = mock.Mock()
        page1 = [_wp_raw(i) for i in range(1, 3)]  # per_page=2 と一致 → 続く
        page2 = [_wp_raw(3)]  # per_page 未満 → 最終ページ
        session.get.side_effect = [_resp(200, page1), _resp(200, page2)]
        sleeper = mock.Mock()
        posts = fetch_wp_posts(
            "https://omcha.jp", session, per_page=2, sleep_seconds=1.5, sleeper=sleeper,
        )
        self.assertEqual([p["id"] for p in posts], [1, 2, 3])
        self.assertEqual(session.get.call_count, 2)
        sleeper.assert_called_once_with(1.5)

    def test_400_past_last_page_stops_cleanly(self):
        session = mock.Mock()
        session.get.side_effect = [_resp(200, [_wp_raw(1)] * 2), _resp(400, {})]
        posts = fetch_wp_posts(
            "https://omcha.jp", session, per_page=2, sleep_seconds=0, sleeper=_no_sleep,
        )
        self.assertEqual(len(posts), 2)

    def test_limit_truncates_mid_page(self):
        session = mock.Mock()
        session.get.return_value = _resp(200, [_wp_raw(i) for i in range(1, 6)])
        posts = fetch_wp_posts(
            "https://omcha.jp", session, per_page=100, limit=3, sleeper=_no_sleep,
        )
        self.assertEqual([p["id"] for p in posts], [1, 2, 3])

    def test_persistent_failure_returns_partial_results(self):
        session = mock.Mock()
        ok_page = _resp(200, [_wp_raw(1)] * 2)
        failing = requests.ConnectionError("boom")
        session.get.side_effect = [ok_page, failing, failing, failing]
        posts = fetch_wp_posts(
            "https://omcha.jp", session, per_page=2, sleep_seconds=0, sleeper=_no_sleep,
        )
        self.assertEqual(len(posts), 2)  # 1 ページ目は成功、2 ページ目は諦めて打ち切り

    def test_uses_get_only_never_writes(self):
        session = mock.Mock()
        session.get.return_value = _resp(200, [])
        fetch_wp_posts("https://omcha.jp", session, sleeper=_no_sleep)
        session.post.assert_not_called()
        session.put.assert_not_called()
        session.delete.assert_not_called()


# --------------------------------------------------------------------------
# navi 候補カタログ
# --------------------------------------------------------------------------

class BuildHubCandidatesTest(unittest.TestCase):
    def test_six_age_hubs_plus_diagnosis(self):
        candidates = build_hub_candidates()
        self.assertEqual(len(candidates), 7)
        urls = {c["url"] for c in candidates}
        self.assertEqual(
            urls,
            {"/toys-age-0/", "/toys-age-1/", "/toys-age-2/", "/toys-age-3/", "/toys-age-4/", "/toys-age-6/", "/diagnosis/"},
        )
        for c in candidates:
            self.assertTrue(c["embed_text"])
            self.assertTrue(c["anchor"])
            self.assertTrue(c["title"])

    def test_diagnosis_hub_present(self):
        candidates = build_hub_candidates()
        diag = next(c for c in candidates if c["kind"] == "diagnosis")
        self.assertEqual(diag["url"], DIAGNOSIS_HUB["url"])


class BuildProductCandidatesTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.articles_dir = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_builds_from_article_fields(self):
        _write_json(self.articles_dir / "2026-05-01-B0XXXXXXXX.json", {
            "title": "テスト商品｜口コミ・最安値を比較",
            "meta_description": "テスト商品の口コミと最安値を徹底比較。",
            "product": {"name": "テスト商品"},
        })
        candidates = build_product_candidates(self.articles_dir)
        self.assertEqual(len(candidates), 1)
        c = candidates[0]
        self.assertEqual(c["url"], "/products/b0xxxxxxxx/")
        self.assertEqual(c["anchor"], "テスト商品")
        self.assertIn("テスト商品", c["embed_text"])
        self.assertIn("徹底比較", c["embed_text"])

    def test_missing_product_name_falls_back_to_title_head(self):
        _write_json(self.articles_dir / "2026-05-01-B0YYYYYYYY.json", {
            "title": "何か長いタイトル｜補足文言",
        })
        candidates = build_product_candidates(self.articles_dir)
        self.assertEqual(candidates[0]["anchor"], "何か長いタイトル")

    def test_no_title_no_product_name_skipped(self):
        _write_json(self.articles_dir / "2026-05-01-B0ZZZZZZZZ.json", {"foo": "bar"})
        candidates = build_product_candidates(self.articles_dir)
        self.assertEqual(candidates, [])

    def test_quality_sidecar_excluded(self):
        _write_json(self.articles_dir / "2026-05-01-B0XXXXXXXX.json", {"title": "本体"})
        _write_json(self.articles_dir / "2026-05-01-B0XXXXXXXX.quality.json", {"title": "sidecar"})
        candidates = build_product_candidates(self.articles_dir)
        self.assertEqual(len(candidates), 1)

    def test_malformed_json_skipped_without_crash(self):
        (self.articles_dir / "2026-05-01-B0XXXXXXXX.json").write_text("{not valid json", encoding="utf-8")
        candidates = build_product_candidates(self.articles_dir)
        self.assertEqual(candidates, [])


class BuildNaviCandidatesTest(unittest.TestCase):
    def test_combines_hubs_and_products(self):
        with tempfile.TemporaryDirectory() as d:
            articles_dir = pathlib.Path(d)
            _write_json(articles_dir / "2026-05-01-B0XXXXXXXX.json", {"title": "商品タイトル"})
            candidates = build_navi_candidates(articles_dir)
            self.assertEqual(len(candidates), 8)  # 7 hub/diagnosis + 1 product


# --------------------------------------------------------------------------
# cosine_similarity_cross
# --------------------------------------------------------------------------

class CosineSimilarityCrossTest(unittest.TestCase):
    def test_orthogonal_vectors_are_zero(self):
        a = [[1.0, 0.0]]
        b = [[0.0, 1.0]]
        sim = cosine_similarity_cross(a, b)
        self.assertAlmostEqual(sim[0][0], 0.0, places=6)

    def test_identical_direction_is_one(self):
        a = [[1.0, 1.0]]
        b = [[2.0, 2.0]]
        sim = cosine_similarity_cross(a, b)
        self.assertAlmostEqual(sim[0][0], 1.0, places=6)

    def test_rectangular_shape(self):
        a = [[1.0, 0.0], [0.0, 1.0]]
        b = [[1.0, 0.0], [0.0, 1.0], [1.0, 1.0]]
        sim = cosine_similarity_cross(a, b)
        self.assertEqual(len(sim), 2)
        self.assertEqual(len(sim[0]), 3)

    def test_empty_inputs(self):
        self.assertEqual(cosine_similarity_cross([], [[1.0]]), [])
        self.assertEqual(cosine_similarity_cross([[1.0]], []), [[]])

    def test_zero_vector_no_crash(self):
        a = [[0.0, 0.0]]
        b = [[1.0, 0.0]]
        sim = cosine_similarity_cross(a, b)
        self.assertAlmostEqual(sim[0][0], 0.0, places=6)


# --------------------------------------------------------------------------
# select_navi_candidates_for_wp
# --------------------------------------------------------------------------

def _candidates(n: int) -> list[dict]:
    return [
        {"kind": "product", "url": f"/products/asin{i}/", "title": f"商品{i}", "anchor": f"商品{i}", "embed_text": f"text{i}"}
        for i in range(n)
    ]


class SelectNaviCandidatesTest(unittest.TestCase):
    def test_filters_below_threshold(self):
        candidates = _candidates(3)
        similarity_row = [0.9, 0.3, 0.6]
        out = select_navi_candidates_for_wp(
            "wp text", similarity_row, candidates, min_score=0.5, top_k=3,
        )
        self.assertEqual({c["url"] for c in out}, {"/products/asin0/", "/products/asin2/"})

    def test_orders_by_score_desc(self):
        candidates = _candidates(3)
        similarity_row = [0.6, 0.9, 0.7]
        out = select_navi_candidates_for_wp(
            "wp text", similarity_row, candidates, min_score=0.5, top_k=3,
        )
        self.assertEqual([c["url"] for c in out], ["/products/asin1/", "/products/asin2/", "/products/asin0/"])

    def test_top_k_truncates(self):
        candidates = _candidates(5)
        similarity_row = [0.9, 0.8, 0.7, 0.6, 0.5]
        out = select_navi_candidates_for_wp(
            "wp text", similarity_row, candidates, min_score=0.0, top_k=2,
        )
        self.assertEqual(len(out), 2)

    def test_deterministic_tie_break_by_url(self):
        candidates = _candidates(2)
        similarity_row = [0.7, 0.7]
        out = select_navi_candidates_for_wp(
            "wp text", similarity_row, candidates, min_score=0.0, top_k=2,
        )
        self.assertEqual([c["url"] for c in out], ["/products/asin0/", "/products/asin1/"])

    def test_no_candidates_above_threshold_returns_empty(self):
        candidates = _candidates(2)
        out = select_navi_candidates_for_wp(
            "wp text", [0.1, 0.2], candidates, min_score=0.5, top_k=3,
        )
        self.assertEqual(out, [])

    def test_reranker_reorders_shortlist(self):
        candidates = _candidates(3)
        similarity_row = [0.9, 0.8, 0.7]  # cosine order: asin0, asin1, asin2

        def fake_reranker(query_text, doc_texts):
            # doc_texts は order=[0,1,2] に対応する text0/text1/text2 のはず。
            # asin2 (index=2) を最上位に持ってくる。
            self.assertEqual(doc_texts, ["text0", "text1", "text2"])
            return [{"index": 2, "score": 9.0}, {"index": 0, "score": 5.0}, {"index": 1, "score": 1.0}]

        out = select_navi_candidates_for_wp(
            "wp text", similarity_row, candidates, min_score=0.5, top_k=2,
            rerank_top_n=3, reranker=fake_reranker,
        )
        self.assertEqual([c["url"] for c in out], ["/products/asin2/", "/products/asin0/"])
        # score 表示は常に cosine 類似度 (reranker のスコア尺度ではない)
        self.assertEqual(out[0]["score"], 0.7)

    def test_reranker_none_falls_back_to_cosine_order(self):
        candidates = _candidates(2)
        similarity_row = [0.9, 0.8]

        def failing_reranker(query_text, doc_texts):
            return None

        out = select_navi_candidates_for_wp(
            "wp text", similarity_row, candidates, min_score=0.5, top_k=2, reranker=failing_reranker,
        )
        self.assertEqual([c["url"] for c in out], ["/products/asin0/", "/products/asin1/"])

    def test_reranker_not_called_for_single_candidate(self):
        candidates = _candidates(1)
        reranker = mock.Mock()
        select_navi_candidates_for_wp(
            "wp text", [0.9], candidates, min_score=0.5, top_k=3, reranker=reranker,
        )
        reranker.assert_not_called()


# --------------------------------------------------------------------------
# Ruri クライアント
# --------------------------------------------------------------------------

def _post_resp(payload):
    r = mock.Mock()
    r.raise_for_status = mock.Mock()
    r.json = mock.Mock(return_value=payload)
    return r


class EmbedBatchRuriTest(unittest.TestCase):
    def test_success(self):
        session = mock.Mock()
        session.post.return_value = _post_resp({"dim": 2, "vectors": [[1.0, 0.0], [0.0, 1.0]]})
        out = embed_batch_ruri(["a", "b"], "document", "http://ruri:8000", session, sleeper=_no_sleep)
        self.assertEqual(out, [[1.0, 0.0], [0.0, 1.0]])
        _, kwargs = session.post.call_args
        self.assertEqual(kwargs["json"], {"texts": ["a", "b"], "kind": "document"})

    def test_retries_then_succeeds(self):
        session = mock.Mock()
        session.post.side_effect = [
            requests.ConnectionError("boom"),
            _post_resp({"vectors": [[1.0]]}),
        ]
        out = embed_batch_ruri(["a"], "query", "http://ruri:8000", session, sleeper=_no_sleep)
        self.assertEqual(out, [[1.0]])

    def test_gives_up_after_retry_limit_raises(self):
        session = mock.Mock()
        session.post.side_effect = requests.ConnectionError("boom")
        with self.assertRaises(EmbeddingBatchError):
            embed_batch_ruri(["a"], "query", "http://ruri:8000", session, sleeper=_no_sleep)
        self.assertEqual(session.post.call_count, 3)  # 初回 + 2 リトライ

    def test_bad_shape_raises(self):
        session = mock.Mock()
        session.post.return_value = _post_resp({"vectors": [[1.0]]})  # 2件要求に1件しか返らない
        with self.assertRaises(EmbeddingBatchError):
            embed_batch_ruri(["a", "b"], "document", "http://ruri:8000", session, sleeper=_no_sleep)


class RerankCandidatesTest(unittest.TestCase):
    def test_success(self):
        session = mock.Mock()
        session.post.return_value = _post_resp({"results": [{"index": 1, "score": 2.0}, {"index": 0, "score": 1.0}]})
        out = rerank_candidates("q", ["d1", "d2"], "http://ruri:8000", session, sleeper=_no_sleep)
        self.assertEqual(out, [{"index": 1, "score": 2.0}, {"index": 0, "score": 1.0}])

    def test_empty_docs_returns_none(self):
        session = mock.Mock()
        out = rerank_candidates("q", [], "http://ruri:8000", session, sleeper=_no_sleep)
        self.assertIsNone(out)
        session.post.assert_not_called()

    def test_failure_after_retries_returns_none_not_raise(self):
        session = mock.Mock()
        session.post.side_effect = requests.ConnectionError("boom")
        out = rerank_candidates("q", ["d1"], "http://ruri:8000", session, sleeper=_no_sleep)
        self.assertIsNone(out)


# --------------------------------------------------------------------------
# render_markdown_report
# --------------------------------------------------------------------------

class RenderMarkdownReportTest(unittest.TestCase):
    def test_empty_entries(self):
        out = render_markdown_report([], generated_at="2026-07-17T00:00:00Z", min_score=0.55, wp_total=10, navi_total=8)
        self.assertIn("候補が見つかった WP 記事はありませんでした", out)
        self.assertIn("WP 記事取得件数: 10", out)

    def test_non_empty_entries_contain_link_and_score(self):
        entries = [{
            "wp_title": "おむつ比較",
            "wp_link": "https://omcha.jp/diaper/",
            "candidates": [
                {"url": "/toys-age-0/", "title": "0歳の知育玩具 おすすめランキング", "anchor": "0歳の知育玩具", "score": 0.812},
            ],
        }]
        out = render_markdown_report(entries, generated_at="2026-07-17T00:00:00Z", min_score=0.55, wp_total=1, navi_total=7)
        self.assertIn("おむつ比較", out)
        self.assertIn("https://omcha.jp/diaper/", out)
        self.assertIn("https://navi.omcha.jp/toys-age-0/", out)
        self.assertIn("0.812", out)
        self.assertIn("自動改稿は一切行っていません", out)

    def test_never_mentions_wp_write_actions(self):
        out = render_markdown_report([], generated_at="x", min_score=0.5, wp_total=0, navi_total=0)
        self.assertNotIn("投稿しました", out)

    def test_entries_sorted_by_best_score_and_capped(self):
        def entry(title, link, score):
            return {
                "wp_title": title,
                "wp_link": link,
                "candidates": [{"url": "/toys-age-0/", "title": "t", "anchor": "a", "score": score}],
            }
        entries = [
            entry("低スコア", "https://omcha.jp/low/", 0.871),
            entry("高スコア", "https://omcha.jp/high/", 0.943),
            entry("中スコア", "https://omcha.jp/mid/", 0.902),
        ]
        out = render_markdown_report(
            entries, generated_at="x", min_score=0.87, wp_total=3, navi_total=3, max_posts=2,
        )
        self.assertLess(out.index("高スコア"), out.index("中スコア"))
        self.assertNotIn("低スコア", out)
        self.assertIn("上位 2 記事のみ表示", out)
        self.assertIn("候補あり全 3 記事", out)

    def test_max_posts_zero_shows_all(self):
        entries = [{
            "wp_title": f"記事{i}",
            "wp_link": f"https://omcha.jp/p{i}/",
            "candidates": [{"url": "/toys-age-0/", "title": "t", "anchor": "a", "score": 0.9}],
        } for i in range(3)]
        out = render_markdown_report(
            entries, generated_at="x", min_score=0.87, wp_total=3, navi_total=3, max_posts=0,
        )
        for i in range(3):
            self.assertIn(f"記事{i}", out)
        self.assertNotIn("のみ表示", out)


# --------------------------------------------------------------------------
# run() E2E (HTTP はモック)
# --------------------------------------------------------------------------

class RunE2ETest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self._tmp.name)
        self.articles_dir = self.base / "articles"
        _write_json(self.articles_dir / "2026-05-01-B0XXXXXXXX.json", {
            "title": "テスト商品｜口コミ",
            "meta_description": "テスト商品の説明",
            "product": {"name": "テスト商品"},
        })
        self.out_path = self.base / "out" / "report.md"

    def tearDown(self):
        self._tmp.cleanup()

    def _session_for(self, wp_pages, embed_vectors_by_kind, rerank_payload=None):
        session = mock.Mock()

        def get_side_effect(url, params=None, headers=None, timeout=None):
            page = params["page"]
            data = wp_pages[page - 1] if page - 1 < len(wp_pages) else []
            return _resp(200, data)

        def post_side_effect(url, json=None, timeout=None):
            if url.endswith("/embed"):
                kind = json["kind"]
                return _post_resp({"vectors": embed_vectors_by_kind[kind](json["texts"])})
            if url.endswith("/rerank"):
                return _post_resp(rerank_payload or {"results": []})
            raise AssertionError(f"unexpected POST to {url}")

        session.get.side_effect = get_side_effect
        session.post.side_effect = post_side_effect
        return session

    def test_writes_report_with_matching_candidate(self):
        wp_pages = [[_wp_raw(1)]]

        # WP 記事(query) と "テスト商品" 候補(document) を同方向ベクトルにして高スコアにする。
        def embed_fn(texts):
            return [[1.0, 0.0] for _ in texts]

        session = self._session_for(wp_pages, {"query": embed_fn, "document": embed_fn})
        summary = run(
            wp_base_url="https://omcha.jp",
            articles_dir=self.articles_dir,
            ruri_url="http://ruri:8000",
            out_path=self.out_path,
            min_score=0.5,
            top_k=3,
            use_reranker=False,
            session=session,
            sleeper=_no_sleep,
        )
        self.assertFalse(summary["aborted"])
        self.assertEqual(summary["wp_posts"], 1)
        self.assertEqual(summary["wp_with_candidates"], 1)
        self.assertTrue(self.out_path.exists())
        content = self.out_path.read_text(encoding="utf-8")
        self.assertIn("タイトル1", content)
        self.assertIn("テスト商品", content)

    def test_no_wp_posts_aborts_without_writing(self):
        session = self._session_for([[]], {"query": lambda t: [], "document": lambda t: []})
        summary = run(
            wp_base_url="https://omcha.jp",
            articles_dir=self.articles_dir,
            ruri_url="http://ruri:8000",
            out_path=self.out_path,
            session=session,
            sleeper=_no_sleep,
        )
        self.assertTrue(summary["aborted"])
        self.assertFalse(self.out_path.exists())

    def test_embed_failure_aborts_without_writing(self):
        wp_pages = [[_wp_raw(1)]]
        session = self._session_for(wp_pages, {"query": lambda t: (_ for _ in ()).throw(requests.ConnectionError("x"))})

        def post_side_effect(url, json=None, timeout=None):
            raise requests.ConnectionError("boom")

        session.post.side_effect = post_side_effect
        summary = run(
            wp_base_url="https://omcha.jp",
            articles_dir=self.articles_dir,
            ruri_url="http://ruri:8000",
            out_path=self.out_path,
            session=session,
            sleeper=_no_sleep,
        )
        self.assertTrue(summary["aborted"])
        self.assertFalse(self.out_path.exists())

    def test_never_calls_wp_write_methods(self):
        wp_pages = [[_wp_raw(1)]]

        def embed_fn(texts):
            return [[1.0, 0.0] for _ in texts]

        session = self._session_for(wp_pages, {"query": embed_fn, "document": embed_fn})
        session.put = mock.Mock()
        session.delete = mock.Mock()
        run(
            wp_base_url="https://omcha.jp",
            articles_dir=self.articles_dir,
            ruri_url="http://ruri:8000",
            out_path=self.out_path,
            use_reranker=False,
            session=session,
            sleeper=_no_sleep,
        )
        session.put.assert_not_called()
        session.delete.assert_not_called()
        # POST は Ruri (embed/rerank) 宛のみで、WP ドメインへは1件も無いことを確認
        for call in session.post.call_args_list:
            self.assertIn("ruri:8000", call.args[0] if call.args else call.kwargs.get("url", ""))


if __name__ == "__main__":
    unittest.main()
