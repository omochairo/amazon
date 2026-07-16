"""scripts/generate_faq_seo.py unit tests (#3332 N1)."""
from __future__ import annotations

import json
import pathlib
import tempfile
import unittest
from unittest import mock

import requests

from scripts._seo_sidecar import load_sidecar
from scripts.generate_faq_seo import (
    build_asin_page_map,
    build_priority_queue,
    call_gemma_faq_seo,
    load_answerability_missing_aspects,
    load_suggest_queries,
    merge_demand_queries,
    run,
    select_targets,
    validate_faq,
    validate_meta_description,
)


def _write_json(path: pathlib.Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


# --------------------------------------------------------------------------
# validate_faq
# --------------------------------------------------------------------------

class ValidateFaqTest(unittest.TestCase):
    def test_valid_items_are_kept(self):
        faq = [{"question": "何歳から使えますか?", "answer": "3歳以上のお子様からご使用いただけます。安全性にも配慮した設計です。"}]
        result = validate_faq(faq)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["question"], "何歳から使えますか?")

    def test_drops_price_word(self):
        faq = [{"question": "価格はいくらですか?", "answer": "この商品の価格帯は幅広い年齢層に人気があります。安心してご利用いただけます。"}]
        self.assertEqual(validate_faq(faq), [])

    def test_drops_numeric_yen_pattern(self):
        faq = [{"question": "この商品の特徴は何ですか?", "answer": "Amazonや楽天がともに3,520円で最安値となっています。とても人気の商品です。"}]
        self.assertEqual(validate_faq(faq), [])

    def test_drops_other_dynamic_words(self):
        for word in ("最安", "在庫", "セール", "割引", "%オフ", "送料", "キャンペーン", "タイムセール"):
            answer = f"この商品は{word}に関する情報を含む説明文でありテスト用の文章です。"
            faq = [{"question": "この商品について教えてください", "answer": answer}]
            self.assertEqual(validate_faq(faq), [], msg=f"word={word} should be dropped")

    def test_drops_kanji_numeral_yen(self):
        # 「数百円」「三千円」のような漢数詞の金額も動的事実として弾く。
        for amount in ("数百円", "三千円", "千円", "1万円"):
            answer = f"この商品は{amount}ほどで購入できる知育玩具です。長く遊べる設計になっています。"
            faq = [{"question": "この商品について教えてください", "answer": answer}]
            self.assertEqual(validate_faq(faq), [], msg=f"amount={amount} should be dropped")

    def test_drops_point_reward_context_only(self):
        # Amazon のポイント還元は動的事実として弾く。
        for phrase in ("ポイント還元", "ポイント付与", "ポイントアップ", "ポイントが貯まります", "ポイ活"):
            answer = f"この商品は{phrase}の対象となる場合があります。詳細はご確認ください。"
            faq = [{"question": "この商品について教えてください", "answer": answer}]
            self.assertEqual(validate_faq(faq), [], msg=f"phrase={phrase} should be dropped")

    def test_keeps_static_yen_and_point_usages(self):
        # 「楕円形」(図形パズル/形合わせ商品の説明) と「着目ポイント」(= 要点) は
        # 動的事実ではないので残す。素の「円」「ポイント」を弾くと実測 0.26% の
        # 既存 FAQ が誤って落ちる (#3332 レビュー)。
        faq = [
            {"question": "どんな形が含まれていますか?",
             "answer": "楕円形、円形、四角形、台形など12種類の異なる形が含まれています。円盤状のピースもあります。"},
            {"question": "親が教えるのは難しくないですか?",
             "answer": "説明書にステップごとの着目ポイントがまとめられているため、安心してサポートできます。"},
        ]
        result = validate_faq(faq)
        self.assertEqual(len(result), 2)

    def test_answer_too_short_is_dropped(self):
        faq = [{"question": "何歳から使えますか?", "answer": "短い"}]
        self.assertEqual(validate_faq(faq), [])

    def test_answer_too_long_is_dropped(self):
        faq = [{"question": "何歳から使えますか?", "answer": "あ" * 401}]
        self.assertEqual(validate_faq(faq), [])

    def test_answer_boundary_lengths_accepted(self):
        faq = [
            {"question": "何歳から使えますか?", "answer": "あ" * 20},
            {"question": "この商品の対象年齢を教えてください", "answer": "あ" * 400},
        ]
        result = validate_faq(faq)
        self.assertEqual(len(result), 2)

    def test_question_too_short_is_dropped(self):
        faq = [{"question": "何?", "answer": "十分な長さの回答文をここに用意しています。安全に配慮した設計です。"}]
        self.assertEqual(validate_faq(faq), [])

    def test_question_too_long_is_dropped(self):
        faq = [{"question": "あ" * 101, "answer": "十分な長さの回答文をここに用意しています。安全に配慮した設計です。"}]
        self.assertEqual(validate_faq(faq), [])

    def test_duplicate_question_keeps_first(self):
        faq = [
            {"question": "何歳から使えますか?", "answer": "3歳以上のお子様からご使用いただけます。安全性にも配慮した設計です。"},
            {"question": "何歳から使えますか?", "answer": "別の回答文ですが重複した質問なので採用されません。テスト用文章。"},
        ]
        result = validate_faq(faq)
        self.assertEqual(len(result), 1)
        self.assertIn("3歳以上", result[0]["answer"])

    def test_truncates_to_max_six(self):
        faq = [
            {"question": f"質問その{i}について教えてください", "answer": f"回答その{i}についての十分な長さの説明文です。安心してご利用いただけます。"}
            for i in range(10)
        ]
        result = validate_faq(faq)
        self.assertEqual(len(result), 6)

    def test_zero_valid_items_returns_empty_list(self):
        faq = [{"question": "価格は?", "answer": "セール中で在庫も豊富、ポイント還元もあります。"}]
        self.assertEqual(validate_faq(faq), [])

    def test_not_a_list_returns_empty(self):
        self.assertEqual(validate_faq(None), [])
        self.assertEqual(validate_faq("not a list"), [])
        self.assertEqual(validate_faq({"question": "x"}), [])

    def test_malformed_items_are_skipped_individually(self):
        faq = [
            "not a dict",
            {"question": "質問だけで回答が無い"},
            {"question": "何歳から使えますか?", "answer": "3歳以上のお子様からご使用いただけます。安全性にも配慮した設計です。"},
        ]
        result = validate_faq(faq)
        self.assertEqual(len(result), 1)


# --------------------------------------------------------------------------
# validate_meta_description
# --------------------------------------------------------------------------

class ValidateMetaDescriptionTest(unittest.TestCase):
    def test_valid_description_is_accepted(self):
        text = "あ" * 100
        self.assertEqual(validate_meta_description(text), text)

    def test_boundary_lengths_accepted(self):
        self.assertEqual(validate_meta_description("あ" * 60), "あ" * 60)
        self.assertEqual(validate_meta_description("あ" * 140), "あ" * 140)

    def test_too_short_rejected(self):
        self.assertIsNone(validate_meta_description("あ" * 59))

    def test_too_long_rejected(self):
        self.assertIsNone(validate_meta_description("あ" * 141))

    def test_contains_dynamic_word_rejected(self):
        self.assertIsNone(validate_meta_description("価格が安い" + "あ" * 60))

    def test_contains_numeric_yen_rejected(self):
        self.assertIsNone(validate_meta_description("3,520円でお得な" + "あ" * 60))

    def test_contains_newline_rejected(self):
        self.assertIsNone(validate_meta_description("あ" * 40 + "\n" + "あ" * 40))

    def test_not_a_string_rejected(self):
        self.assertIsNone(validate_meta_description(None))
        self.assertIsNone(validate_meta_description(123))


# --------------------------------------------------------------------------
# 対象選定
# --------------------------------------------------------------------------

class BuildPriorityQueueTest(unittest.TestCase):
    def test_low_ctr_high_impressions_prioritized(self):
        article_index = {
            "AAAAAAAAAA": pathlib.Path("2026-01-01-AAAAAAAAAA.json"),
            "BBBBBBBBBB": pathlib.Path("2026-01-01-BBBBBBBBBB.json"),
        }
        by_page = [
            {"page": "https://navi.omcha.jp/products/aaaaaaaaaa/", "impressions": 10, "ctr": 0.5},
            {"page": "https://navi.omcha.jp/products/bbbbbbbbbb/", "impressions": 100, "ctr": 0.01},
        ]
        ordered = build_priority_queue(article_index, by_page)
        self.assertEqual(ordered, ["BBBBBBBBBB", "AAAAAAAAAA"])

    def test_unmatched_articles_appended_in_stem_order(self):
        article_index = {
            "ZZZZZZZZZZ": pathlib.Path("2026-02-02-ZZZZZZZZZZ.json"),
            "AAAAAAAAAA": pathlib.Path("2026-01-01-AAAAAAAAAA.json"),
        }
        ordered = build_priority_queue(article_index, [])
        self.assertEqual(ordered, ["AAAAAAAAAA", "ZZZZZZZZZZ"])

    def test_gsc_rows_for_unknown_asin_ignored(self):
        article_index = {"AAAAAAAAAA": pathlib.Path("2026-01-01-AAAAAAAAAA.json")}
        by_page = [{"page": "https://navi.omcha.jp/products/unknownnn/", "impressions": 999, "ctr": 0.0}]
        ordered = build_priority_queue(article_index, by_page)
        self.assertEqual(ordered, ["AAAAAAAAAA"])

    def test_zero_impressions_rows_skipped_from_gsc_priority(self):
        article_index = {
            "AAAAAAAAAA": pathlib.Path("2026-01-01-AAAAAAAAAA.json"),
            "BBBBBBBBBB": pathlib.Path("2026-01-01-BBBBBBBBBB.json"),
        }
        by_page = [{"page": "https://navi.omcha.jp/products/bbbbbbbbbb/", "impressions": 0, "ctr": 0.0}]
        ordered = build_priority_queue(article_index, by_page)
        # impressions=0 は GSC 優先度に乗らず、backfill 側 (stem 順) に回る
        self.assertEqual(ordered, ["AAAAAAAAAA", "BBBBBBBBBB"])


class BuildAsinPageMapTest(unittest.TestCase):
    def test_maps_asin_to_first_page(self):
        by_page = [
            {"page": "https://navi.omcha.jp/products/aaaaaaaaaa/"},
            {"page": "https://navi.omcha.jp/products/bbbbbbbbbb/"},
        ]
        m = build_asin_page_map(by_page)
        self.assertEqual(m["AAAAAAAAAA"], "https://navi.omcha.jp/products/aaaaaaaaaa/")
        self.assertEqual(m["BBBBBBBBBB"], "https://navi.omcha.jp/products/bbbbbbbbbb/")


class SelectTargetsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.articles_dir = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_skips_articles_with_complete_sidecar_unless_forced(self):
        article_index = {
            "AAAAAAAAAA": self.articles_dir / "2026-01-01-AAAAAAAAAA.json",
            "BBBBBBBBBB": self.articles_dir / "2026-01-02-BBBBBBBBBB.json",
        }
        sc = self.articles_dir / "2026-01-01-AAAAAAAAAA.seo.json"
        _write_json(sc, {"faq_extended": [{"question": "Q", "answer": "A"}], "meta_description_optimized": "説明"})

        targets = select_targets(article_index, [], self.articles_dir, force=False, limit=30)
        asins = [t[0] for t in targets]
        self.assertNotIn("AAAAAAAAAA", asins)
        self.assertIn("BBBBBBBBBB", asins)

    def test_force_includes_already_complete_articles(self):
        article_index = {"AAAAAAAAAA": self.articles_dir / "2026-01-01-AAAAAAAAAA.json"}
        sc = self.articles_dir / "2026-01-01-AAAAAAAAAA.seo.json"
        _write_json(sc, {"faq_extended": [{"question": "Q", "answer": "A"}], "meta_description_optimized": "説明"})

        targets = select_targets(article_index, [], self.articles_dir, force=True, limit=30)
        self.assertEqual([t[0] for t in targets], ["AAAAAAAAAA"])

    def test_partial_sidecar_is_not_skipped(self):
        article_index = {"AAAAAAAAAA": self.articles_dir / "2026-01-01-AAAAAAAAAA.json"}
        sc = self.articles_dir / "2026-01-01-AAAAAAAAAA.seo.json"
        _write_json(sc, {"faq_extended": [{"question": "Q", "answer": "A"}]})  # meta 側が無い

        targets = select_targets(article_index, [], self.articles_dir, force=False, limit=30)
        self.assertEqual([t[0] for t in targets], ["AAAAAAAAAA"])

    def test_limit_caps_result(self):
        article_index = {
            f"A{'A' * 8}{i}": self.articles_dir / f"2026-01-0{i}-A{'A' * 8}{i}.json"
            for i in range(1, 4)
        }
        targets = select_targets(article_index, [], self.articles_dir, force=False, limit=2)
        self.assertEqual(len(targets), 2)


# --------------------------------------------------------------------------
# load_suggest_queries / load_answerability_missing_aspects / merge_demand_queries
# --------------------------------------------------------------------------

class LoadSuggestQueriesTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.suggest_dir = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_reads_queries_from_suggestions(self):
        _write_json(self.suggest_dir / "AAAAAAAAAA.json", {
            "asin": "AAAAAAAAAA",
            "suggestions": [{"query": "クエリ1", "seed": "seed"}, {"query": "クエリ2", "seed": "seed"}],
        })
        self.assertEqual(load_suggest_queries(self.suggest_dir, "AAAAAAAAAA"), ["クエリ1", "クエリ2"])

    def test_missing_file_returns_empty(self):
        self.assertEqual(load_suggest_queries(self.suggest_dir, "NOPE"), [])

    def test_malformed_json_returns_empty(self):
        p = self.suggest_dir / "AAAAAAAAAA.json"
        p.write_text("{not json", encoding="utf-8")
        self.assertEqual(load_suggest_queries(self.suggest_dir, "AAAAAAAAAA"), [])

    def test_empty_suggestions_returns_empty(self):
        _write_json(self.suggest_dir / "AAAAAAAAAA.json", {"asin": "AAAAAAAAAA", "suggestions": []})
        self.assertEqual(load_suggest_queries(self.suggest_dir, "AAAAAAAAAA"), [])


class LoadAnswerabilityMissingAspectsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self._tmp.name) / "answerability_audit.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_flattens_missing_aspects_by_asin(self):
        _write_json(self.path, {
            "pages": [{
                "asin": "AAAAAAAAAA",
                "queries": [
                    {"missing_aspects": ["観点1", "観点2"]},
                    {"missing_aspects": ["観点3"]},
                ],
            }],
        })
        result = load_answerability_missing_aspects(self.path)
        self.assertEqual(result["AAAAAAAAAA"], ["観点1", "観点2", "観点3"])

    def test_missing_file_returns_empty_dict(self):
        self.assertEqual(load_answerability_missing_aspects(self.path), {})

    def test_page_with_no_missing_aspects_not_included(self):
        _write_json(self.path, {"pages": [{"asin": "AAAAAAAAAA", "queries": [{"missing_aspects": []}]}]})
        self.assertEqual(load_answerability_missing_aspects(self.path), {})


class MergeDemandQueriesTest(unittest.TestCase):
    def test_merges_and_dedupes_preserving_order(self):
        result = merge_demand_queries(["Q1", "Q2"], ["Q2", "Q3"], ["Q4"])
        self.assertEqual(result, ["Q1", "Q2", "Q3", "Q4"])

    def test_respects_limit(self):
        result = merge_demand_queries([f"Q{i}" for i in range(20)], [], [], limit=15)
        self.assertEqual(len(result), 15)

    def test_ignores_non_string_and_empty(self):
        result = merge_demand_queries(["Q1", "", None], [], [])  # type: ignore[list-item]
        self.assertEqual(result, ["Q1"])


# --------------------------------------------------------------------------
# call_gemma_faq_seo (requests mocked via a fake session)
# --------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, json_body, status=200):
        self._json = json_body
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")

    def json(self):
        return self._json


class _FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def post(self, url, json=None, timeout=None):
        self.calls += 1
        resp = self._responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp


class CallGemmaFaqSeoTest(unittest.TestCase):
    def test_parses_valid_response(self):
        inner = json.dumps({"faq": [{"question": "Q", "answer": "A"}], "meta_description": "説明"})
        session = _FakeSession([_FakeResponse({"response": inner})])
        result = call_gemma_faq_seo("article", ["q1"], [], "http://ollama", "gemma4", session, sleeper=lambda s: None)
        self.assertEqual(result["faq"], [{"question": "Q", "answer": "A"}])
        self.assertEqual(result["meta_description"], "説明")
        self.assertIsNone(result["error"])

    def test_retries_then_succeeds(self):
        inner = json.dumps({"faq": [], "meta_description": "説明"})
        session = _FakeSession([requests.ConnectionError("boom"), _FakeResponse({"response": inner})])
        result = call_gemma_faq_seo("article", [], [], "http://ollama", "gemma4", session, sleeper=lambda s: None)
        self.assertIsNone(result["error"])
        self.assertEqual(session.calls, 2)

    def test_gives_up_after_max_retries(self):
        session = _FakeSession([
            requests.ConnectionError("boom1"),
            requests.ConnectionError("boom2"),
            requests.ConnectionError("boom3"),
        ])
        result = call_gemma_faq_seo("article", [], [], "http://ollama", "gemma4", session, sleeper=lambda s: None)
        self.assertIsNotNone(result["error"])
        self.assertEqual(result["faq"], [])
        self.assertIsNone(result["meta_description"])

    def test_malformed_json_response_is_retried_and_fails(self):
        session = _FakeSession([
            _FakeResponse({"response": "not valid json"}),
            _FakeResponse({"response": "still not json"}),
            _FakeResponse({"response": "nope"}),
        ])
        result = call_gemma_faq_seo("article", [], [], "http://ollama", "gemma4", session, sleeper=lambda s: None)
        self.assertIsNotNone(result["error"])


# --------------------------------------------------------------------------
# run() end-to-end
# --------------------------------------------------------------------------

class RunEndToEndTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.articles_dir = self.root / "articles"
        self.history_dir = self.root / "gsc_history"
        self.suggest_dir = self.root / "suggest"
        self.answerability_path = self.root / "answerability_audit.json"
        self.articles_dir.mkdir()
        self.history_dir.mkdir()
        self.suggest_dir.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, session, **kwargs):
        return run(
            articles_dir=self.articles_dir,
            history_dir=self.history_dir,
            suggest_dir=self.suggest_dir,
            answerability_path=self.answerability_path,
            session=session,
            sleeper=lambda s: None,
            **kwargs,
        )

    def test_writes_sidecar_for_valid_generation(self):
        _write_json(self.articles_dir / "2026-01-01-AAAAAAAAAA.json", {
            "title": "テスト商品", "narrative": {"lead": "リード文"}, "faq": [],
        })
        inner = json.dumps({
            "faq": [{"question": "何歳から使えますか?", "answer": "3歳以上のお子様からご使用いただけます。安全性にも配慮した設計です。"}],
            "meta_description": "あ" * 100,
        })
        session = _FakeSession([_FakeResponse({"response": inner})])

        summary = self._run(session)
        self.assertEqual(summary["processed"], 1)
        self.assertEqual(summary["faq_written"], 1)
        self.assertEqual(summary["meta_written"], 1)
        self.assertEqual(summary["errors"], 0)

        sidecar = load_sidecar(self.articles_dir / "2026-01-01-AAAAAAAAAA.seo.json")
        self.assertEqual(len(sidecar["faq_extended"]), 1)
        self.assertEqual(sidecar["meta_description_optimized"], "あ" * 100)

    def test_dry_run_does_not_write_sidecar(self):
        _write_json(self.articles_dir / "2026-01-01-AAAAAAAAAA.json", {"title": "テスト商品", "faq": []})
        inner = json.dumps({
            "faq": [{"question": "何歳から使えますか?", "answer": "3歳以上のお子様からご使用いただけます。安全性にも配慮した設計です。"}],
            "meta_description": "あ" * 100,
        })
        session = _FakeSession([_FakeResponse({"response": inner})])

        summary = self._run(session, dry_run=True)
        self.assertEqual(summary["faq_written"], 1)
        self.assertFalse((self.articles_dir / "2026-01-01-AAAAAAAAAA.seo.json").exists())

    def test_malformed_gemma_response_counts_error_and_continues(self):
        _write_json(self.articles_dir / "2026-01-01-AAAAAAAAAA.json", {"title": "商品A", "faq": []})
        _write_json(self.articles_dir / "2026-01-02-BBBBBBBBBB.json", {"title": "商品B", "faq": []})

        good_inner = json.dumps({
            "faq": [{"question": "何歳から使えますか?", "answer": "3歳以上のお子様からご使用いただけます。安全性にも配慮した設計です。"}],
            "meta_description": "あ" * 100,
        })
        # AAAAAAAAAA (backfill 順で先) は3回とも壊れたJSON、BBBBBBBBBB は成功
        session = _FakeSession([
            _FakeResponse({"response": "broken"}),
            _FakeResponse({"response": "broken"}),
            _FakeResponse({"response": "broken"}),
            _FakeResponse({"response": good_inner}),
        ])

        summary = self._run(session)
        self.assertEqual(summary["processed"], 2)
        self.assertEqual(summary["errors"], 1)
        self.assertEqual(summary["faq_written"], 1)
        self.assertFalse((self.articles_dir / "2026-01-01-AAAAAAAAAA.seo.json").exists())
        self.assertTrue((self.articles_dir / "2026-01-02-BBBBBBBBBB.seo.json").exists())

    def test_faq_dropped_counted_when_validation_removes_items(self):
        _write_json(self.articles_dir / "2026-01-01-AAAAAAAAAA.json", {"title": "商品A", "faq": []})
        inner = json.dumps({
            "faq": [
                {"question": "価格はいくらですか?", "answer": "セール中でとてもお得な価格になっています。今がチャンスです。"},
                {"question": "何歳から使えますか?", "answer": "3歳以上のお子様からご使用いただけます。安全性にも配慮した設計です。"},
            ],
            "meta_description": "あ" * 100,
        })
        session = _FakeSession([_FakeResponse({"response": inner})])

        summary = self._run(session)
        self.assertEqual(summary["faq_dropped"], 1)
        sidecar = load_sidecar(self.articles_dir / "2026-01-01-AAAAAAAAAA.seo.json")
        self.assertEqual(len(sidecar["faq_extended"]), 1)

    def test_force_flag_reprocesses_complete_sidecar(self):
        article_path = self.articles_dir / "2026-01-01-AAAAAAAAAA.json"
        _write_json(article_path, {"title": "商品A", "faq": []})
        _write_json(self.articles_dir / "2026-01-01-AAAAAAAAAA.seo.json", {
            "faq_extended": [{"question": "旧質問", "answer": "旧回答"}],
            "meta_description_optimized": "旧い説明",
        })
        inner = json.dumps({
            "faq": [{"question": "何歳から使えますか?", "answer": "3歳以上のお子様からご使用いただけます。安全性にも配慮した設計です。"}],
            "meta_description": "あ" * 100,
        })
        session = _FakeSession([_FakeResponse({"response": inner})])

        summary = self._run(session, force=True)
        self.assertEqual(summary["processed"], 1)
        sidecar = load_sidecar(self.articles_dir / "2026-01-01-AAAAAAAAAA.seo.json")
        self.assertEqual(sidecar["meta_description_optimized"], "あ" * 100)

    def test_without_force_complete_sidecar_is_skipped_no_network_call(self):
        article_path = self.articles_dir / "2026-01-01-AAAAAAAAAA.json"
        _write_json(article_path, {"title": "商品A", "faq": []})
        _write_json(self.articles_dir / "2026-01-01-AAAAAAAAAA.seo.json", {
            "faq_extended": [{"question": "旧質問", "answer": "旧回答"}],
            "meta_description_optimized": "旧い説明",
        })
        session = mock.Mock()

        summary = self._run(session)
        self.assertEqual(summary["processed"], 0)
        session.post.assert_not_called()

    def test_limit_restricts_number_processed(self):
        for i in range(1, 4):
            _write_json(self.articles_dir / f"2026-01-0{i}-{'A' * 9}{i}.json", {"title": f"商品{i}", "faq": []})
        inner = json.dumps({"faq": [], "meta_description": None})
        session = _FakeSession([_FakeResponse({"response": inner}) for _ in range(2)])

        summary = self._run(session, limit=2)
        self.assertEqual(summary["processed"], 2)

    def test_no_articles_returns_zero_summary_without_network(self):
        session = mock.Mock()
        summary = self._run(session)
        self.assertEqual(summary["processed"], 0)
        session.post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
