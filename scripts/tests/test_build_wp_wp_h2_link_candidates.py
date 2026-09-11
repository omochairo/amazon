"""scripts/build_wp_wp_h2_link_candidates.py unit tests (omcha-ops#174)。

カバレッジ:
1. index.jsonl パース (parse_index_entry/load_index_jsonl): publish 以外の除外・不正行スキップ
2. document 候補構築 (build_document_candidates): 自記事の id 除外 (URL 一致ではない)
3. H2 セクション分割 (extract_h2_sections): H3/H4 は親 H2 に含まれる・ショートコード除去
4. 既出 [blogcard] 検出 (extract_blogcard_urls): URL 正規化・引用符違い
5. query 記事の読み込み (parse_query_post/load_query_from_file): WP REST 単発取得・
   wp_draft.py pull 形式ファイルの両方
6. 候補選定 (select_document_candidates_for_h2): 閾値フィルタ・top_k・reranker・既出フラグ
7. レポート整形 (render_markdown_report)
8. run() の E2E (HTTP はモック): レポート書き込み・自リンク除外・embed 失敗時の abort
"""
from __future__ import annotations

import json
import pathlib
import tempfile
import unittest
from unittest import mock

import requests

from scripts.build_wp_wp_h2_link_candidates import (
    EmbeddingBatchError,
    build_document_candidates,
    clean_heading_text,
    cosine_similarity_cross,
    embed_batch_ruri,
    extract_blogcard_urls,
    extract_h2_sections,
    fetch_query_article,
    load_index_jsonl,
    load_query_from_file,
    normalize_url,
    parse_index_entry,
    parse_query_post,
    render_markdown_report,
    rerank_candidates,
    run,
    select_document_candidates_for_h2,
    strip_html,
)


def _no_sleep(_seconds):
    pass


def _write_jsonl(path: pathlib.Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")


def _write_json(path: pathlib.Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


# --------------------------------------------------------------------------
# parse_index_entry / load_index_jsonl
# --------------------------------------------------------------------------

class ParseIndexEntryTest(unittest.TestCase):
    def test_valid_publish_entry(self):
        raw = {"id": 1, "link": "https://omcha.jp/x/", "title": "タイトル", "status": "publish", "type": "post"}
        self.assertEqual(parse_index_entry(raw), {"id": 1, "url": "https://omcha.jp/x/", "title": "タイトル"})

    def test_non_publish_dropped(self):
        raw = {"id": 1, "link": "https://omcha.jp/x/", "title": "t", "status": "draft"}
        self.assertIsNone(parse_index_entry(raw))

    def test_missing_title_dropped(self):
        raw = {"id": 1, "link": "https://omcha.jp/x/", "title": "", "status": "publish"}
        self.assertIsNone(parse_index_entry(raw))

    def test_not_a_dict(self):
        self.assertIsNone(parse_index_entry("nope"))


class LoadIndexJsonlTest(unittest.TestCase):
    def test_loads_posts_and_pages_skips_bad_lines(self):
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "index.jsonl"
            path.write_text(
                '{"id": 1, "link": "https://omcha.jp/p1/", "title": "post1", "status": "publish", "type": "post"}\n'
                "not json\n"
                '{"id": 2, "link": "https://omcha.jp/pg1/", "title": "page1", "status": "publish", "type": "page"}\n'
                '{"id": 3, "link": "https://omcha.jp/draft/", "title": "draft", "status": "draft", "type": "post"}\n'
                "\n",
                encoding="utf-8",
            )
            entries = load_index_jsonl(path)
            self.assertEqual([e["id"] for e in entries], [1, 2])

    def test_missing_file_returns_empty(self):
        self.assertEqual(load_index_jsonl(pathlib.Path("/no/such/file.jsonl")), [])


# --------------------------------------------------------------------------
# build_document_candidates
# --------------------------------------------------------------------------

class BuildDocumentCandidatesTest(unittest.TestCase):
    def test_excludes_self_by_id_not_url(self):
        entries = [
            {"id": 15750, "url": "https://omcha.jp/anpanman-seal/", "title": "アンパンマンシール"},
            {"id": 42, "url": "https://omcha.jp/other/", "title": "他の記事"},
        ]
        candidates = build_document_candidates(entries, exclude_id=15750)
        self.assertEqual([c["id"] for c in candidates], [42])

    def test_no_exclude_id_keeps_all(self):
        entries = [{"id": 1, "url": "https://omcha.jp/a/", "title": "a"}]
        candidates = build_document_candidates(entries)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["embed_text"], "a")

    def test_excludes_by_exact_title_when_rewrite_draft_has_different_id(self):
        # omcha-ops#174 実測: wp_draft.py copy のリライト下書きは元記事と別idに
        # なるため、id 照合だけでは自分自身を除外できない (下書きid=99999、
        # 公開済み元記事id=15750)。
        entries = [
            {"id": 15750, "url": "https://omcha.jp/anpanman-seal/", "title": "アンパンマンシール特集"},
            {"id": 42, "url": "https://omcha.jp/other/", "title": "他の記事"},
        ]
        candidates = build_document_candidates(entries, exclude_id=99999, exclude_title="アンパンマンシール特集")
        self.assertEqual([c["id"] for c in candidates], [42])

    def test_extra_exclude_ids_and_urls(self):
        entries = [
            {"id": 1, "url": "https://omcha.jp/a/", "title": "a"},
            {"id": 2, "url": "https://omcha.jp/b/", "title": "b"},
            {"id": 3, "url": "https://omcha.jp/c/", "title": "c"},
        ]
        candidates = build_document_candidates(
            entries, extra_exclude_ids=frozenset({1}), extra_exclude_urls=frozenset({"https://omcha.jp/b/"}),
        )
        self.assertEqual([c["id"] for c in candidates], [3])


# --------------------------------------------------------------------------
# H2 セクション分割 / 既出 blogcard 検出
# --------------------------------------------------------------------------

class ExtractH2SectionsTest(unittest.TestCase):
    def test_splits_by_h2_h3_stays_in_parent(self):
        html_content = (
            "<h2>導入</h2><p>本文A</p>"
            "<h2>種類</h2><p>本文B</p><h3>小見出し</h3><p>本文C</p>"
            "<h2>まとめ</h2><p>本文D</p>"
        )
        sections = extract_h2_sections(html_content)
        self.assertEqual([s["heading"] for s in sections], ["導入", "種類", "まとめ"])
        self.assertIn("小見出し", sections[1]["body_html"])
        self.assertIn("本文C", sections[1]["body_html"])

    def test_strips_shortcodes_in_heading(self):
        html_content = '<h2>安全な選び方【[deco type="red_bold"]最重要[/deco]】</h2><p>x</p>'
        sections = extract_h2_sections(html_content)
        self.assertEqual(sections[0]["heading"], "安全な選び方【 最重要 】")

    def test_empty_content_returns_empty(self):
        self.assertEqual(extract_h2_sections(""), [])
        self.assertEqual(extract_h2_sections(None), [])

    def test_no_h2_returns_empty(self):
        self.assertEqual(extract_h2_sections("<p>H2 なし本文</p>"), [])


class ExtractBlogcardUrlsTest(unittest.TestCase):
    def test_finds_double_and_single_quoted(self):
        html_content = (
            '<h2>関連</h2>[blogcard url="https://omcha.jp/a/"]'
            "<h2>関連2</h2>[blogcard url='https://omcha.jp/b']"
        )
        urls = extract_blogcard_urls(html_content)
        self.assertEqual(urls, {"https://omcha.jp/a", "https://omcha.jp/b"})

    def test_no_blogcard_returns_empty_set(self):
        self.assertEqual(extract_blogcard_urls("<p>none here</p>"), set())

    def test_none_input(self):
        self.assertEqual(extract_blogcard_urls(None), set())


class NormalizeUrlTest(unittest.TestCase):
    def test_strips_trailing_slash(self):
        self.assertEqual(normalize_url("https://omcha.jp/a/"), "https://omcha.jp/a")
        self.assertEqual(normalize_url("https://omcha.jp/a"), "https://omcha.jp/a")


class CleanHeadingTextTest(unittest.TestCase):
    def test_removes_tags_and_shortcodes(self):
        self.assertEqual(clean_heading_text('<span>x</span> [deco]y[/deco]'), "x y")


# --------------------------------------------------------------------------
# query 記事の読み込み
# --------------------------------------------------------------------------

class ParseQueryPostTest(unittest.TestCase):
    def test_valid(self):
        raw = {
            "id": 15750,
            "link": "https://omcha.jp/anpanman-seal/",
            "title": {"rendered": "アンパンマンシール特集"},
            "content": {"rendered": "<h2>H2</h2><p>本文</p>"},
        }
        out = parse_query_post(raw)
        self.assertEqual(out["id"], 15750)
        self.assertEqual(out["title"], "アンパンマンシール特集")
        self.assertIn("<h2>H2</h2>", out["content_html"])

    def test_missing_title_dropped(self):
        raw = {"id": 1, "link": "https://omcha.jp/x/", "title": {"rendered": ""}, "content": {"rendered": ""}}
        self.assertIsNone(parse_query_post(raw))

    def test_not_a_dict(self):
        self.assertIsNone(parse_query_post(None))


def _resp(status_code: int, payload):
    r = mock.Mock()
    r.status_code = status_code
    if status_code >= 400:
        r.raise_for_status = mock.Mock(side_effect=requests.HTTPError(f"{status_code}"))
    else:
        r.raise_for_status = mock.Mock()
    r.json = mock.Mock(return_value=payload)
    return r


class FetchQueryArticleTest(unittest.TestCase):
    def test_found_as_post(self):
        session = mock.Mock()
        session.get.return_value = _resp(200, {
            "id": 1, "link": "https://omcha.jp/p1/",
            "title": {"rendered": "t"}, "content": {"rendered": "<h2>a</h2>"},
        })
        out = fetch_query_article("https://omcha.jp", 1, session)
        self.assertEqual(out["id"], 1)
        self.assertEqual(session.get.call_count, 1)

    def test_falls_back_to_pages_on_404(self):
        session = mock.Mock()
        session.get.side_effect = [
            _resp(404, {}),
            _resp(200, {
                "id": 24, "link": "https://omcha.jp/kids_mbtilike/",
                "title": {"rendered": "診断ページ"}, "content": {"rendered": "<h2>a</h2>"},
            }),
        ]
        out = fetch_query_article("https://omcha.jp", 24, session)
        self.assertEqual(out["id"], 24)
        self.assertEqual(session.get.call_count, 2)

    def test_not_found_anywhere_returns_none(self):
        session = mock.Mock()
        session.get.return_value = _resp(404, {})
        self.assertIsNone(fetch_query_article("https://omcha.jp", 999, session))

    def test_never_writes(self):
        session = mock.Mock()
        session.get.return_value = _resp(200, {
            "id": 1, "link": "https://omcha.jp/p1/",
            "title": {"rendered": "t"}, "content": {"rendered": "<h2>a</h2>"},
        })
        fetch_query_article("https://omcha.jp", 1, session)
        session.post.assert_not_called()
        session.put.assert_not_called()
        session.delete.assert_not_called()


class LoadQueryFromFileTest(unittest.TestCase):
    def test_wp_draft_pull_format(self):
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "after_15750.json"
            _write_json(path, {
                "source_id": 15750,
                "source_kind": "post",
                "source_link": "https://omcha.jp/?p=15750",
                "title": "アンパンマンシール特集",
                "content": "<h2>H2-1</h2><p>本文</p>",
                "excerpt": "",
            })
            out = load_query_from_file(path)
            self.assertEqual(out["id"], 15750)
            self.assertEqual(out["link"], "https://omcha.jp/?p=15750")
            self.assertIn("<h2>H2-1</h2>", out["content_html"])

    def test_missing_fields_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "bad.json"
            _write_json(path, {"title": "no id or content"})
            self.assertIsNone(load_query_from_file(path))

    def test_malformed_json_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "bad.json"
            path.write_text("{not valid", encoding="utf-8")
            self.assertIsNone(load_query_from_file(path))


# --------------------------------------------------------------------------
# cosine_similarity_cross (build_wp_navi_link_candidates.py と同じ実装)
# --------------------------------------------------------------------------

class CosineSimilarityCrossTest(unittest.TestCase):
    def test_identical_direction_is_one(self):
        sim = cosine_similarity_cross([[1.0, 1.0]], [[2.0, 2.0]])
        self.assertAlmostEqual(sim[0][0], 1.0, places=6)

    def test_empty_inputs(self):
        self.assertEqual(cosine_similarity_cross([], [[1.0]]), [])
        self.assertEqual(cosine_similarity_cross([[1.0]], []), [[]])


# --------------------------------------------------------------------------
# select_document_candidates_for_h2
# --------------------------------------------------------------------------

def _candidates(n: int) -> list[dict]:
    return [
        {"id": i, "url": f"https://omcha.jp/doc{i}/", "title": f"記事{i}", "embed_text": f"text{i}"}
        for i in range(n)
    ]


class SelectDocumentCandidatesForH2Test(unittest.TestCase):
    def test_filters_below_threshold_and_orders_desc(self):
        candidates = _candidates(3)
        similarity_row = [0.9, 0.3, 0.6]
        out = select_document_candidates_for_h2("h2", similarity_row, candidates, min_score=0.5, top_k=3)
        self.assertEqual([c["url"] for c in out], ["https://omcha.jp/doc0/", "https://omcha.jp/doc2/"])

    def test_top_k_truncates(self):
        candidates = _candidates(5)
        similarity_row = [0.9, 0.8, 0.7, 0.6, 0.5]
        out = select_document_candidates_for_h2("h2", similarity_row, candidates, min_score=0.0, top_k=2)
        self.assertEqual(len(out), 2)

    def test_already_linked_flag_set_and_not_excluded(self):
        candidates = _candidates(2)
        similarity_row = [0.9, 0.8]
        existing = frozenset({"https://omcha.jp/doc0"})  # 正規化済み (末尾スラッシュ無し)
        out = select_document_candidates_for_h2(
            "h2", similarity_row, candidates, min_score=0.5, top_k=2, existing_urls=existing,
        )
        self.assertEqual(len(out), 2)  # 除外されない
        self.assertTrue(out[0]["already_linked"])
        self.assertFalse(out[1]["already_linked"])

    def test_already_linked_below_threshold_still_shown_with_rank(self):
        # omcha-ops#174 レビュー指摘: 既出リンクは閾値未満でも黙って落ちてはいけない。
        candidates = _candidates(4)
        similarity_row = [0.9, 0.85, 0.3, 0.2]  # doc2 は閾値未満だが既出
        existing = frozenset({"https://omcha.jp/doc2/"})
        out = select_document_candidates_for_h2(
            "h2", similarity_row, candidates, min_score=0.5, top_k=3, existing_urls=existing,
        )
        urls = [c["url"] for c in out]
        self.assertIn("https://omcha.jp/doc2/", urls)
        doc2 = next(c for c in out if c["url"] == "https://omcha.jp/doc2/")
        self.assertTrue(doc2["already_linked"])
        self.assertTrue(doc2["below_threshold"])
        self.assertEqual(doc2["score"], 0.3)
        self.assertEqual(doc2["rank"], 3)  # 全4件中3位 (0.9, 0.85, [0.3], 0.2)

    def test_calibration_url_below_threshold_still_shown_with_rank(self):
        candidates = _candidates(3)
        similarity_row = [0.9, 0.4, 0.1]  # doc1 は較正対象だが閾値未満
        out = select_document_candidates_for_h2(
            "h2", similarity_row, candidates, min_score=0.87, top_k=3,
            calibration_urls=frozenset({"https://omcha.jp/doc1/"}),
        )
        doc1 = next(c for c in out if c["url"] == "https://omcha.jp/doc1/")
        self.assertTrue(doc1["is_calibration"])
        self.assertTrue(doc1["below_threshold"])
        self.assertEqual(doc1["rank"], 2)

    def test_no_forced_candidates_when_none_match(self):
        candidates = _candidates(2)
        out = select_document_candidates_for_h2(
            "h2", [0.1, 0.2], candidates, min_score=0.5, top_k=3,
            existing_urls=frozenset({"https://omcha.jp/not-a-candidate/"}),
        )
        self.assertEqual(out, [])

    def test_display_order_is_always_score_descending(self):
        candidates = _candidates(3)
        similarity_row = [0.9, 0.2, 0.6]
        out = select_document_candidates_for_h2(
            "h2", similarity_row, candidates, min_score=0.5, top_k=3,
            calibration_urls=frozenset({"https://omcha.jp/doc1/"}),  # below threshold, forced in
        )
        scores = [c["score"] for c in out]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_reranker_affects_selection_but_display_is_score_sorted(self):
        candidates = _candidates(3)
        similarity_row = [0.9, 0.8, 0.7]

        def fake_reranker(query_text, doc_texts):
            self.assertEqual(doc_texts, ["text0", "text1", "text2"])
            # reranker は doc2 を最上位に押し上げる (top_k=2 の選定に影響する)
            return [{"index": 2, "score": 9.0}, {"index": 0, "score": 5.0}, {"index": 1, "score": 1.0}]

        out = select_document_candidates_for_h2(
            "h2", similarity_row, candidates, min_score=0.5, top_k=2,
            rerank_top_n=3, reranker=fake_reranker,
        )
        # reranker が選んだのは doc2/doc0 (doc1 は落選) だが、表示順は常に
        # cosine スコア降順に整列し直す (レビュー指摘: reranker 順のままだと
        # レポート上でスコアが降順にならないことがあるため)。
        self.assertEqual({c["url"] for c in out}, {"https://omcha.jp/doc2/", "https://omcha.jp/doc0/"})
        self.assertEqual([c["url"] for c in out], ["https://omcha.jp/doc0/", "https://omcha.jp/doc2/"])
        self.assertEqual(out[0]["score"], 0.9)
        self.assertEqual(out[1]["score"], 0.7)

    def test_reranker_none_falls_back_to_cosine_order(self):
        candidates = _candidates(2)
        similarity_row = [0.9, 0.8]
        out = select_document_candidates_for_h2(
            "h2", similarity_row, candidates, min_score=0.5, top_k=2,
            reranker=lambda q, d: None,
        )
        self.assertEqual([c["url"] for c in out], ["https://omcha.jp/doc0/", "https://omcha.jp/doc1/"])

    def test_no_candidates_above_threshold_returns_empty(self):
        candidates = _candidates(2)
        out = select_document_candidates_for_h2("h2", [0.1, 0.2], candidates, min_score=0.5, top_k=3)
        self.assertEqual(out, [])


# --------------------------------------------------------------------------
# Ruri クライアント (build_wp_navi_link_candidates.py と同じ契約)
# --------------------------------------------------------------------------

def _post_resp(payload):
    r = mock.Mock()
    r.raise_for_status = mock.Mock()
    r.json = mock.Mock(return_value=payload)
    return r


class EmbedBatchRuriTest(unittest.TestCase):
    def test_success(self):
        session = mock.Mock()
        session.post.return_value = _post_resp({"vectors": [[1.0, 0.0], [0.0, 1.0]]})
        out = embed_batch_ruri(["a", "b"], "query", "http://ruri:8000", session, sleeper=_no_sleep)
        self.assertEqual(out, [[1.0, 0.0], [0.0, 1.0]])
        _, kwargs = session.post.call_args
        self.assertEqual(kwargs["json"], {"texts": ["a", "b"], "kind": "query"})

    def test_gives_up_after_retry_limit_raises(self):
        session = mock.Mock()
        session.post.side_effect = requests.ConnectionError("boom")
        with self.assertRaises(EmbeddingBatchError):
            embed_batch_ruri(["a"], "query", "http://ruri:8000", session, sleeper=_no_sleep)
        self.assertEqual(session.post.call_count, 3)


class RerankCandidatesTest(unittest.TestCase):
    def test_success(self):
        session = mock.Mock()
        session.post.return_value = _post_resp({"results": [{"index": 1, "score": 2.0}]})
        out = rerank_candidates("q", ["d1", "d2"], "http://ruri:8000", session, sleeper=_no_sleep)
        self.assertEqual(out, [{"index": 1, "score": 2.0}])

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
    def test_no_h2_sections(self):
        out = render_markdown_report(
            "記事", "https://omcha.jp/x/", [], generated_at="2026-09-11T00:00:00Z", min_score=0.87, doc_total=10,
        )
        self.assertIn("H2 見出しが検出できませんでした", out)

    def test_h2_with_and_without_candidates(self):
        entries = [
            {"heading": "H2-1", "candidates": [
                {"url": "https://omcha.jp/a/", "title": "記事A", "score": 0.912, "already_linked": False},
            ]},
            {"heading": "H2-2", "candidates": []},
        ]
        out = render_markdown_report(
            "アンパンマンシール特集", "https://omcha.jp/anpanman-seal/", entries,
            generated_at="2026-09-11T00:00:00Z", min_score=0.87, doc_total=843,
        )
        self.assertIn("## H2-1", out)
        self.assertIn("記事A", out)
        self.assertIn("0.912", out)
        self.assertIn("## H2-2", out)
        self.assertIn("候補なし", out)
        self.assertNotIn("**[既出]**", out)

    def test_already_linked_flag_shown(self):
        entries = [{"heading": "H2-1", "candidates": [
            {"url": "https://omcha.jp/a/", "title": "記事A", "score": 0.9, "already_linked": True},
        ]}]
        out = render_markdown_report(
            "記事", "https://omcha.jp/x/", entries, generated_at="x", min_score=0.87, doc_total=1,
        )
        self.assertIn("**[既出]**", out)

    def test_never_mentions_wp_write_actions(self):
        out = render_markdown_report("記事", "https://omcha.jp/x/", [], generated_at="x", min_score=0.87, doc_total=0)
        self.assertIn("自動挿入は一切行っていません", out)
        self.assertNotIn("投稿しました", out)

    def test_below_threshold_shows_rank_and_calibration_flags(self):
        entries = [{"heading": "H2-1", "candidates": [
            {
                "url": "https://omcha.jp/a/", "title": "記事A", "score": 0.61,
                "already_linked": False, "is_calibration": True, "below_threshold": True, "rank": 234,
            },
        ]}]
        out = render_markdown_report(
            "記事", "https://omcha.jp/x/", entries, generated_at="x", min_score=0.87, doc_total=843,
        )
        self.assertIn("較正対象", out)
        self.assertIn("閾値未満・全843件中234位", out)


# --------------------------------------------------------------------------
# run() E2E (HTTP はモック)
# --------------------------------------------------------------------------

class RunE2ETest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self._tmp.name)
        self.index_path = self.base / "index.jsonl"
        _write_jsonl(self.index_path, [
            {"id": 15750, "link": "https://omcha.jp/anpanman-seal/", "title": "アンパンマンシール特集(自分自身)", "status": "publish", "type": "post"},
            {"id": 42, "link": "https://omcha.jp/anpanman-toy-guide/", "title": "アンパンマン知育玩具ガイド", "status": "publish", "type": "post"},
        ])
        self.query_content_path = self.base / "query.json"
        _write_json(self.query_content_path, {
            "source_id": 15750,
            "source_link": "https://omcha.jp/?p=15750",
            "title": "アンパンマンシール特集",
            "content": '<h2>シールの選び方</h2><p>本文</p><h2>まとめ</h2><p>本文2</p>',
        })
        self.out_path = self.base / "out" / "report.md"

    def tearDown(self):
        self._tmp.cleanup()

    def _session_with_embed(self, embed_fn):
        session = mock.Mock()

        def post_side_effect(url, json=None, timeout=None):
            if url.endswith("/embed"):
                return _post_resp({"vectors": embed_fn(json["texts"])})
            if url.endswith("/rerank"):
                return _post_resp({"results": []})
            raise AssertionError(f"unexpected POST to {url}")

        session.post.side_effect = post_side_effect
        return session

    def test_writes_report_and_excludes_self_by_id(self):
        def embed_fn(texts):
            return [[1.0, 0.0] for _ in texts]

        session = self._session_with_embed(embed_fn)
        summary = run(
            index_path=self.index_path,
            ruri_url="http://ruri:8000",
            out_path=self.out_path,
            query_content_file=self.query_content_path,
            min_score=0.5,
            use_reranker=False,
            session=session,
            sleeper=_no_sleep,
        )
        self.assertFalse(summary["aborted"])
        self.assertEqual(summary["doc_candidates"], 1)  # 自分自身 (id=15750) は除外
        self.assertEqual(summary["h2_count"], 2)
        content = self.out_path.read_text(encoding="utf-8")
        self.assertIn("アンパンマン知育玩具ガイド", content)
        self.assertNotIn("アンパンマンシール特集(自分自身)", content)

    def test_rewrite_draft_excluded_by_title_when_id_differs(self):
        # omcha-ops#174 実測の再現: wp_draft.py copy の下書き id (99999) は
        # 公開済み元記事の id (15750) と別物なので、id 照合だけでは自分自身が
        # 候補に混入する。タイトル完全一致での除外がこれを補う。
        _write_json(self.query_content_path, {
            "source_id": 99999,
            "source_link": "https://omcha.jp/?p=99999",
            "title": "アンパンマンシール特集(自分自身)",  # index.jsonl の id=15750 と完全一致させる
            "content": '<h2>シールの選び方</h2><p>本文</p>',
        })

        def embed_fn(texts):
            return [[1.0, 0.0] for _ in texts]

        session = self._session_with_embed(embed_fn)
        summary = run(
            index_path=self.index_path,
            out_path=self.out_path,
            query_content_file=self.query_content_path,
            min_score=0.5,
            use_reranker=False,
            session=session,
            sleeper=_no_sleep,
        )
        self.assertFalse(summary["aborted"])
        self.assertEqual(summary["doc_candidates"], 1)  # id=15750 (タイトル一致) は除外
        content = self.out_path.read_text(encoding="utf-8")
        candidates_section = content.split("## シールの選び方", 1)[1]
        self.assertNotIn("アンパンマンシール特集(自分自身)", candidates_section)
        self.assertIn("アンパンマン知育玩具ガイド", candidates_section)

    def test_calibration_url_forced_into_report_even_below_threshold(self):
        def embed_fn(texts):
            # H2見出しと"アンパンマン知育玩具ガイド"を直交させ、閾値未満のスコアにする
            return [[0.0, 1.0] if "アンパンマン知育玩具ガイド" not in t else [1.0, 0.0] for t in texts]

        session = self._session_with_embed(embed_fn)
        summary = run(
            index_path=self.index_path,
            out_path=self.out_path,
            query_content_file=self.query_content_path,
            min_score=0.99,  # 通常なら直交ベクトル(score=0)は絶対に通らない
            use_reranker=False,
            calibration_urls=frozenset({"https://omcha.jp/anpanman-toy-guide/"}),
            session=session,
            sleeper=_no_sleep,
        )
        self.assertFalse(summary["aborted"])
        content = self.out_path.read_text(encoding="utf-8")
        self.assertIn("アンパンマン知育玩具ガイド", content)
        self.assertIn("較正対象", content)
        self.assertIn("閾値未満", content)

    def test_already_linked_scoped_to_its_own_h2_section(self):
        # H2-1 にだけ blogcard がある場合、H2-2 の候補一覧には既出フラグが
        # 付かないこと (記事全体スコープだと誤ってどのH2にも付いてしまう)。
        _write_json(self.query_content_path, {
            "source_id": 15750,
            "source_link": "https://omcha.jp/?p=15750",
            "title": "アンパンマンシール特集",
            "content": (
                '<h2>シールの選び方</h2><p>本文</p>[blogcard url="https://omcha.jp/anpanman-toy-guide/"]'
                '<h2>まとめ</h2><p>本文2</p>'
            ),
        })

        def embed_fn(texts):
            return [[1.0, 0.0] for _ in texts]

        session = self._session_with_embed(embed_fn)
        run(
            index_path=self.index_path,
            out_path=self.out_path,
            query_content_file=self.query_content_path,
            min_score=0.5,
            use_reranker=False,
            session=session,
            sleeper=_no_sleep,
        )
        content = self.out_path.read_text(encoding="utf-8")
        h2_1 = content.split("## まとめ")[0]
        h2_2 = content.split("## まとめ")[1]
        self.assertIn("既出", h2_1)
        self.assertNotIn("既出", h2_2)

    def test_no_query_source_aborts_without_writing(self):
        with self.assertRaises(ValueError):
            run(index_path=self.index_path, out_path=self.out_path)

    def test_missing_query_file_aborts_without_writing(self):
        session = self._session_with_embed(lambda t: [[1.0, 0.0] for _ in t])
        summary = run(
            index_path=self.index_path,
            out_path=self.out_path,
            query_content_file=self.base / "does-not-exist.json",
            session=session,
            sleeper=_no_sleep,
        )
        self.assertTrue(summary["aborted"])
        self.assertFalse(self.out_path.exists())

    def test_embed_failure_aborts_without_writing(self):
        session = mock.Mock()
        session.post.side_effect = requests.ConnectionError("boom")
        summary = run(
            index_path=self.index_path,
            out_path=self.out_path,
            query_content_file=self.query_content_path,
            session=session,
            sleeper=_no_sleep,
        )
        self.assertTrue(summary["aborted"])
        self.assertFalse(self.out_path.exists())

    def test_never_calls_wp_or_write_http_methods(self):
        session = self._session_with_embed(lambda t: [[1.0, 0.0] for _ in t])
        session.put = mock.Mock()
        session.delete = mock.Mock()
        run(
            index_path=self.index_path,
            out_path=self.out_path,
            query_content_file=self.query_content_path,
            use_reranker=False,
            session=session,
            sleeper=_no_sleep,
        )
        session.put.assert_not_called()
        session.delete.assert_not_called()
        session.get.assert_not_called()  # query はファイル入力なので WP への GET も無い


if __name__ == "__main__":
    unittest.main()
