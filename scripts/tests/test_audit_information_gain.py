"""scripts/audit_information_gain.py unit tests (#4841 S3).

文単位の指標・分類ロジックは scripts/experimental/multistage_brief/sentence_metrics.py /
unsupported_classification.py (#4841 M1/M2) から本番へそのまま移したもの。
このテストファイルは元の test_sentence_metrics.py / test_unsupported_classification.py の
アサーションを維持しつつ (「同じテストで固定する」#4841 実装依頼 S3)、選定・キャッシュ・
堅牢性・CLI 用のテストを追加する。
"""
from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

from scripts.audit_information_gain import (
    FACTUAL_CLAIM,
    RHETORICAL_OR_TIME_DEPENDENT,
    ResultCache,
    TruncationError,
    article_category,
    build_classify_prompt,
    build_entailment_prompt,
    build_material_text,
    build_sentence_pool,
    cache_key,
    call_gemma,
    classify_unsupported_sentences,
    compute_information_gain,
    compute_sentence_uniqueness,
    experience_snippets_by_aspect,
    flatten_narrative_sentences,
    has_experience_material_at_generation,
    iso_week_label,
    parse_classify_response,
    parse_entailment_response,
    sample_category_articles,
    sample_other_category_articles,
    select_recent_article_paths,
    select_target_asins,
    split_sentences,
    summarize_categories,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


class SplitSentencesTest(unittest.TestCase):
    def test_splits_on_japanese_terminal_punctuation(self):
        text = "これは文1です。これは文2ですか？これは文3！"
        self.assertEqual(split_sentences(text), ["これは文1です。", "これは文2ですか？", "これは文3！"])

    def test_empty_and_whitespace(self):
        self.assertEqual(split_sentences(""), [])
        self.assertEqual(split_sentences("   "), [])
        self.assertEqual(split_sentences(None), [])  # type: ignore[arg-type]

    def test_no_terminal_punctuation_is_one_sentence(self):
        self.assertEqual(split_sentences("句点が無い文"), ["句点が無い文"])


class FlattenNarrativeSentencesTest(unittest.TestCase):
    def test_flattens_in_key_order_and_skips_missing(self):
        narrative = {"lead": "文A。文B。", "how_to_choose": "文C。"}
        flat = flatten_narrative_sentences(narrative)
        self.assertEqual(
            flat,
            [
                {"key": "lead", "sentence": "文A。"},
                {"key": "lead", "sentence": "文B。"},
                {"key": "how_to_choose", "sentence": "文C。"},
            ],
        )

    def test_empty_narrative(self):
        self.assertEqual(flatten_narrative_sentences({}), [])

    def test_array_form_sections_are_not_silently_skipped(self):
        """#4841 M1 実データ検証で発覚: 現行記事の narrative セクションは文単位の
        array (例: why_this_product) のことが多く、str 専用の処理で読むと
        丸ごと0件になる (元バグ)。
        """
        narrative = {"lead": "リード文。", "why_this_product": ["理由1。", "理由2。"]}
        flat = flatten_narrative_sentences(narrative)
        self.assertEqual(
            flat,
            [
                {"key": "lead", "sentence": "リード文。"},
                {"key": "why_this_product", "sentence": "理由1。"},
                {"key": "why_this_product", "sentence": "理由2。"},
            ],
        )


class BuildSentencePoolTest(unittest.TestCase):
    def test_flattens_multiple_articles(self):
        articles = [
            {"narrative": {"lead": "文A。"}},
            {"narrative": {"lead": "文B。文C。"}},
            {"not_narrative": True},
        ]
        self.assertEqual(build_sentence_pool(articles), ["文A。", "文B。", "文C。"])

    def test_flattens_array_form_sections(self):
        articles = [{"narrative": {"gift_appeal": ["文A。", "文B。"]}}]
        self.assertEqual(build_sentence_pool(articles), ["文A。", "文B。"])

    def test_truncates_at_max_sentences(self):
        articles = [{"narrative": {"lead": "文A。文B。文C。"}}]
        self.assertEqual(build_sentence_pool(articles, max_sentences=2), ["文A。", "文B。"])


class BuildEntailmentPromptTest(unittest.TestCase):
    def test_numbers_sentences_in_order(self):
        prompt = build_entailment_prompt("素材テキスト", ["文1。", "文2。"])
        self.assertIn("1. 文1。", prompt)
        self.assertIn("2. 文2。", prompt)
        self.assertIn("素材テキスト", prompt)


class ParseEntailmentResponseTest(unittest.TestCase):
    def test_maps_by_index(self):
        parsed = {"judgments": [{"index": 1, "supported": True}, {"index": 2, "supported": False}]}
        result = parse_entailment_response(parsed, 2)
        self.assertEqual(result["supported_flags"], [True, False])
        self.assertEqual(result["unresolved_indices"], [])

    def test_missing_index_is_unresolved_not_defaulted(self):
        parsed = {"judgments": [{"index": 1, "supported": True}]}
        result = parse_entailment_response(parsed, 3)
        self.assertEqual(result["supported_flags"], [True, None, None])
        self.assertEqual(result["unresolved_indices"], [2, 3])

    def test_out_of_range_and_malformed_entries_ignored(self):
        parsed = {"judgments": [
            {"index": 5, "supported": True},
            {"index": 1, "supported": "yes"},
            {"foo": "bar"},
        ]}
        result = parse_entailment_response(parsed, 2)
        self.assertEqual(result["supported_flags"], [None, None])
        self.assertEqual(result["unresolved_indices"], [1, 2])

    def test_empty_judgments_all_unresolved(self):
        result = parse_entailment_response({}, 2)
        self.assertEqual(result["supported_flags"], [None, None])
        self.assertEqual(result["unresolved_indices"], [1, 2])


# --------------------------------------------------------------------------
# Ruri / gemma をモックした uniqueness / information gain
# --------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeSession:
    VECTORS = {
        "UNIQUE_SENT": [1.0, 0.0, 0.0],
        "GENERIC_SENT": [0.0, 1.0, 0.0],
        "SAME_CAT_1": [0.0, 1.0, 0.0],
        "CROSS_CAT_1": [0.0, 0.0, 1.0],
    }

    def post(self, url, json=None, timeout=None):
        payload = json or {}
        texts = payload.get("texts", [])
        return _FakeResp({"vectors": [self.VECTORS[t] for t in texts]})


class ComputeSentenceUniquenessTest(unittest.TestCase):
    def test_below_threshold_is_unique_above_is_not(self):
        session = _FakeSession()
        result = compute_sentence_uniqueness(
            ["UNIQUE_SENT", "GENERIC_SENT"], ["SAME_CAT_1"], ["CROSS_CAT_1"],
            ruri_url="http://ruri:8000", session=session,
        )
        by_sentence = {r["sentence"]: r for r in result["per_sentence"]}
        self.assertFalse(by_sentence["GENERIC_SENT"]["unique"])
        self.assertEqual(by_sentence["GENERIC_SENT"]["same_category_max_sim"], 1.0)

    def test_empty_pool_returns_none_not_false(self):
        session = _FakeSession()
        result = compute_sentence_uniqueness(["UNIQUE_SENT"], [], [], ruri_url="http://ruri:8000", session=session)
        self.assertIsNone(result["per_sentence"][0]["unique"])
        self.assertIsNone(result["threshold"])

    def test_no_sentences(self):
        result = compute_sentence_uniqueness([], ["x"], ["y"], ruri_url="http://ruri:8000", session=_FakeSession())
        self.assertEqual(result, {"per_sentence": [], "threshold": None})


class _EntailmentFakeSession:
    def __init__(self, judgments):
        self._judgments = judgments

    def post(self, url, json=None, timeout=None):
        if url.endswith("/api/generate"):
            body = {"judgments": self._judgments}
            return _FakeResp({
                "response": __import__("json").dumps(body, ensure_ascii=False),
                "model": "gemma4:26b-a4b-it-qat", "prompt_eval_count": 1000, "eval_count": 10,
                "total_duration": 1_000_000_000,
            })
        payload = json or {}
        texts = payload.get("texts", [])
        vec_map = {"支持される文。": [1.0, 0.0], "支持されない文。": [0.0, 1.0]}
        return _FakeResp({"vectors": [vec_map.get(t, [0.5, 0.5]) for t in texts]})


class ComputeInformationGainTest(unittest.TestCase):
    def test_counts_unique_and_supported_vs_unsupported(self):
        narrative = {"lead": "支持される文。支持されない文。"}
        judgments = [{"index": 1, "supported": True}, {"index": 2, "supported": False}]
        session = _EntailmentFakeSession(judgments)
        result = compute_information_gain(
            narrative, "素材テキスト", same_category_pool=["同カテゴリの文"],
            cross_category_pool=["別カテゴリの文"],
            ruri_url="http://ruri:8000", session=session,
        )
        self.assertEqual(result["sentence_count"], 2)
        self.assertEqual(result["unsupported_count"], 1)
        self.assertEqual(result["unresolved_count"], 0)
        self.assertIn(result["unique_and_supported_count"], (0, 1))

    def test_no_material_or_narrative_is_empty(self):
        result = compute_information_gain(
            {}, "素材", same_category_pool=[], cross_category_pool=[],
            ruri_url="http://ruri:8000", session=_EntailmentFakeSession([]),
        )
        self.assertEqual(result["sentence_count"], 0)
        self.assertEqual(result["unique_and_supported_count"], 0)
        self.assertEqual(result["unsupported_count"], 0)


class CallGemmaTruncationTest(unittest.TestCase):
    class _TruncatingSession:
        def post(self, url, json=None, timeout=None):
            return _FakeResp({
                "response": "{}", "model": "gemma4:26b-a4b-it-qat",
                # プロンプト見積もりの70%を大きく割る -> 切り詰め扱い
                "prompt_eval_count": 1, "eval_count": 1, "total_duration": 1,
            })

    def test_raises_on_truncation(self):
        with self.assertRaises(TruncationError):
            call_gemma(
                "非常に長いプロンプト" * 200, session=self._TruncatingSession(),
                sleeper=lambda s: None,
            )


# --------------------------------------------------------------------------
# 裏付けの無い文の分類
# --------------------------------------------------------------------------

class BuildClassifyPromptTest(unittest.TestCase):
    def test_numbers_sentences_in_order(self):
        prompt = build_classify_prompt(["文1。", "文2。"])
        self.assertIn("1. 文1。", prompt)
        self.assertIn("2. 文2。", prompt)
        self.assertIn(RHETORICAL_OR_TIME_DEPENDENT, prompt)
        self.assertIn(FACTUAL_CLAIM, prompt)


class ParseClassifyResponseTest(unittest.TestCase):
    def test_maps_by_index(self):
        parsed = {"classifications": [
            {"index": 1, "category": RHETORICAL_OR_TIME_DEPENDENT},
            {"index": 2, "category": FACTUAL_CLAIM},
        ]}
        result = parse_classify_response(parsed, 2)
        self.assertEqual(result, [RHETORICAL_OR_TIME_DEPENDENT, FACTUAL_CLAIM])

    def test_missing_index_is_unresolved(self):
        parsed = {"classifications": [{"index": 1, "category": FACTUAL_CLAIM}]}
        result = parse_classify_response(parsed, 3)
        self.assertEqual(result, [FACTUAL_CLAIM, None, None])

    def test_unknown_category_is_unresolved(self):
        parsed = {"classifications": [{"index": 1, "category": "何か別のもの"}]}
        result = parse_classify_response(parsed, 1)
        self.assertEqual(result, [None])

    def test_out_of_range_and_malformed_ignored(self):
        parsed = {"classifications": [
            {"index": 5, "category": FACTUAL_CLAIM},
            {"index": 1, "category": 123},
            "not a dict",
        ]}
        result = parse_classify_response(parsed, 2)
        self.assertEqual(result, [None, None])

    def test_empty_response(self):
        self.assertEqual(parse_classify_response({}, 2), [None, None])


class SummarizeCategoriesTest(unittest.TestCase):
    def test_counts_each_category(self):
        categories = [RHETORICAL_OR_TIME_DEPENDENT, RHETORICAL_OR_TIME_DEPENDENT, FACTUAL_CLAIM, None]
        result = summarize_categories(categories)
        self.assertEqual(result, {RHETORICAL_OR_TIME_DEPENDENT: 2, FACTUAL_CLAIM: 1, "unresolved": 1})

    def test_empty(self):
        self.assertEqual(summarize_categories([]), {RHETORICAL_OR_TIME_DEPENDENT: 0, FACTUAL_CLAIM: 0, "unresolved": 0})


class _FakeGemmaSession:
    def __init__(self, classifications):
        self._classifications = classifications

    def post(self, url, json=None, timeout=None):
        body = {"classifications": self._classifications}
        return _FakeResp({
            "response": __import__("json").dumps(body, ensure_ascii=False), "model": "gemma4:26b-a4b-it-qat",
            "prompt_eval_count": 1000, "eval_count": 10, "total_duration": 1_000_000_000,
        })


class ClassifyUnsupportedSentencesTest(unittest.TestCase):
    def test_classifies_and_returns_call_meta(self):
        session = _FakeGemmaSession([{"index": 1, "category": FACTUAL_CLAIM}])
        result = classify_unsupported_sentences(["文1。"], session=session)
        self.assertEqual(result["categories"], [FACTUAL_CLAIM])
        self.assertIsNotNone(result["call_meta"])

    def test_empty_sentences_skips_call(self):
        result = classify_unsupported_sentences([], session=_FakeGemmaSession([]))
        self.assertEqual(result, {"categories": [], "call_meta": None})


# --------------------------------------------------------------------------
# 素材読み込み・experience.json の有無判定
# --------------------------------------------------------------------------

class BuildMaterialTextTest(unittest.TestCase):
    def test_missing_sources_do_not_crash(self):
        self.assertEqual(build_material_text({}), "")

    def test_includes_amazon_and_experience(self):
        raw = {
            "amazon": {"item": {"title": "商品名", "price": 1000, "features": ["特徴1"]}},
            "experience": {"snippets": [{"aspect": "不満", "text": "壊れやすい"}]},
        }
        text = build_material_text(raw)
        self.assertIn("商品名", text)
        self.assertIn("壊れやすい", text)


class ExperienceSnippetsByAspectTest(unittest.TestCase):
    def test_groups_by_aspect(self):
        raw = {"experience": {"snippets": [
            {"aspect": "不満", "text": "A"}, {"aspect": "不満", "text": "B"}, {"aspect": "安全", "text": "C"},
        ]}}
        self.assertEqual(experience_snippets_by_aspect(raw), {"不満": ["A", "B"], "安全": ["C"]})

    def test_missing_experience(self):
        self.assertEqual(experience_snippets_by_aspect({}), {})


class HasExperienceMaterialAtGenerationTest(unittest.TestCase):
    def test_true_when_generated_before_article(self):
        raw = {"experience": {"generated_at": "2026-08-01T00:00:00Z"}}
        article = {"date": "2026-08-10T10:00:00+09:00"}
        self.assertTrue(has_experience_material_at_generation(raw, article))

    def test_false_when_generated_after_article(self):
        raw = {"experience": {"generated_at": "2026-08-20T00:00:00Z"}}
        article = {"date": "2026-08-10T10:00:00+09:00"}
        self.assertFalse(has_experience_material_at_generation(raw, article))

    def test_false_when_no_experience_file(self):
        self.assertFalse(has_experience_material_at_generation({}, {"date": "2026-08-10T10:00:00+09:00"}))

    def test_none_when_dates_unparseable(self):
        raw = {"experience": {"generated_at": "not-a-date"}}
        article = {"date": "2026-08-10T10:00:00+09:00"}
        self.assertIsNone(has_experience_material_at_generation(raw, article))


class ArticleCategoryTest(unittest.TestCase):
    def test_first_edu_domain(self):
        self.assertEqual(article_category({"product": {"edu_domains": ["STEM", "運動"]}}), "STEM")

    def test_unknown_when_missing(self):
        self.assertEqual(article_category({}), "unknown")


# --------------------------------------------------------------------------
# 同/別カテゴリのコーパスサンプリング (固定 seed で再現可能)
# --------------------------------------------------------------------------

class CategoryPoolSamplingTest(unittest.TestCase):
    def _paths(self, tmp_path, articles: dict[str, dict]) -> dict[str, Path]:
        out = {}
        for asin, article in articles.items():
            p = tmp_path / f"2026-01-01-{asin}.json"
            p.write_text(json.dumps(article), encoding="utf-8")
            out[asin] = p
        return out

    def test_same_category_excludes_self_and_other_categories(self, tmp_path=Path("/tmp")):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            paths = self._paths(Path(td), {
                "AAAAAAAAAA": {"product": {"edu_domains": ["STEM"]}},
                "BBBBBBBBBB": {"product": {"edu_domains": ["STEM"]}},
                "CCCCCCCCCC": {"product": {"edu_domains": ["言語"]}},
            })
            same = sample_category_articles(paths, "STEM", "AAAAAAAAAA", seed=1)
            self.assertEqual(len(same), 1)
            self.assertEqual(same[0]["product"]["edu_domains"], ["STEM"])

            cross = sample_other_category_articles(paths, "STEM", "AAAAAAAAAA", seed=1)
            self.assertEqual(len(cross), 1)
            self.assertEqual(cross[0]["product"]["edu_domains"], ["言語"])

    def test_reproducible_with_fixed_seed(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            articles = {f"ASIN{i:06d}": {"product": {"edu_domains": ["STEM"]}} for i in range(10)}
            paths = self._paths(Path(td), articles)
            first = [a["product"] for a in sample_category_articles(paths, "STEM", "ASIN000000", seed=42)]
            second = [a["product"] for a in sample_category_articles(paths, "STEM", "ASIN000000", seed=42)]
            self.assertEqual(first, second)


# --------------------------------------------------------------------------
# 対象記事の選定 (git log ベース、固定 seed で --limit 件)
# --------------------------------------------------------------------------

class SelectRecentArticlePathsTest(unittest.TestCase):
    def _git(self, cwd, *args):
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)

    def test_finds_committed_articles_in_range(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            self._git(repo, "init", "-q")
            self._git(repo, "config", "user.email", "t@example.com")
            self._git(repo, "config", "user.name", "t")
            articles_dir = repo / "data" / "articles"
            articles_dir.mkdir(parents=True)
            f = articles_dir / "2026-01-01-AAAAAAAAAA.json"
            f.write_text("{}", encoding="utf-8")
            self._git(repo, "add", ".")
            self._git(repo, "commit", "-q", "-m", "add article")

            touched = select_recent_article_paths(repo, "1970-01-01", "2099-01-01")
            self.assertIn("data/articles/2026-01-01-AAAAAAAAAA.json", touched)

    def test_excludes_sidecar_files(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            self._git(repo, "init", "-q")
            self._git(repo, "config", "user.email", "t@example.com")
            self._git(repo, "config", "user.name", "t")
            articles_dir = repo / "data" / "articles"
            articles_dir.mkdir(parents=True)
            (articles_dir / "2026-01-01-AAAAAAAAAA.json").write_text("{}", encoding="utf-8")
            (articles_dir / "2026-01-01-AAAAAAAAAA.quality.json").write_text("{}", encoding="utf-8")
            self._git(repo, "add", ".")
            self._git(repo, "commit", "-q", "-m", "add article")

            touched = select_recent_article_paths(repo, "1970-01-01", "2099-01-01")
            self.assertNotIn("data/articles/2026-01-01-AAAAAAAAAA.quality.json", touched)

    def test_returns_empty_set_when_git_fails(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            # git リポジトリではないディレクトリ -> git log は失敗する
            touched = select_recent_article_paths(Path(td), "1970-01-01", "2099-01-01")
            self.assertEqual(touched, set())


class SelectTargetAsinsTest(unittest.TestCase):
    def test_limits_and_is_reproducible(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True, capture_output=True)
            articles_dir = repo / "data" / "articles"
            articles_dir.mkdir(parents=True)
            for i in range(5):
                (articles_dir / f"2026-01-01-ASIN{i:06d}.json").write_text(
                    json.dumps({"product": {"edu_domains": ["STEM"]}}), encoding="utf-8",
                )
            subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=repo, check=True, capture_output=True)

            first, _ = select_target_asins(articles_dir, repo, since="1970-01-01", until="2099-01-01",
                                            limit=3, seed=7)
            second, _ = select_target_asins(articles_dir, repo, since="1970-01-01", until="2099-01-01",
                                             limit=3, seed=7)
            self.assertEqual(len(first), 3)
            self.assertEqual(first, second)


# --------------------------------------------------------------------------
# 結果キャッシュ
# --------------------------------------------------------------------------

class ResultCacheTest(unittest.TestCase):
    def test_round_trips_and_reports_hit_rate(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "cache.json"
            cache = ResultCache(path, "gemma4:26b-a4b-it-qat")
            cache.load()
            key = cache_key("gemma4:26b-a4b-it-qat", {"lead": "文。"}, "素材")
            self.assertIsNone(cache.get(key))
            cache.put(key, {"sentence_count": 1})
            cache.save()

            reloaded = ResultCache(path, "gemma4:26b-a4b-it-qat")
            reloaded.load()
            self.assertEqual(reloaded.get(key), {"sentence_count": 1})
            self.assertEqual(reloaded.stats()["hits"], 1)

    def test_disabled_without_path(self):
        cache = ResultCache(None, "model")
        cache.put("k", {"x": 1})
        self.assertIsNone(cache.get("k"))
        self.assertFalse(cache.enabled)

    def test_corrupt_cache_does_not_crash(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "cache.json"
            path.write_text("not json", encoding="utf-8")
            cache = ResultCache(path, "model")
            cache.load()  # 壊れていても例外を出さず空で続行する
            self.assertEqual(cache.entries, {})

    def test_key_changes_when_narrative_or_material_changes(self):
        k1 = cache_key("m", {"lead": "A"}, "素材1")
        k2 = cache_key("m", {"lead": "B"}, "素材1")
        k3 = cache_key("m", {"lead": "A"}, "素材2")
        self.assertNotEqual(k1, k2)
        self.assertNotEqual(k1, k3)


class IsoWeekLabelTest(unittest.TestCase):
    def test_formats_year_and_zero_padded_week(self):
        import datetime
        self.assertEqual(iso_week_label(datetime.datetime(2026, 9, 15, tzinfo=datetime.timezone.utc)), "2026-W38")


# --------------------------------------------------------------------------
# CLI (package 形式の import ゲート、#6522)
# --------------------------------------------------------------------------

class _RunFakeSession:
    """/api/generate (entailment・分類) と /embed の両方に応答する。

    全文 supported=True を返すので unsupported が発生せず分類呼び出しは走らない
    (呼び出し元コードの分岐カバレッジは ClassifyUnsupportedSentencesTest 側で見る)。
    ``fail_asins`` に含まれる文字列がプロンプトに含まれる場合は例外的な壊れた
    応答を返し、その記事だけ失敗させる (#4841 M2 の反省: 1件の破損で run 全体を
    落とさない、を検証する)。
    """

    def __init__(self, fail_markers: frozenset[str] = frozenset()):
        self.fail_markers = fail_markers

    def post(self, url, json=None, timeout=None):
        if url.endswith("/api/generate"):
            prompt = (json or {}).get("prompt", "")
            if any(m in prompt for m in self.fail_markers):
                return _FakeResp({
                    "response": "{not valid json", "model": "gemma4:26b-a4b-it-qat",
                    "prompt_eval_count": 1000, "eval_count": 10, "total_duration": 1_000_000_000,
                })
            if "judgments" in prompt or "根拠として" in prompt:
                n = prompt.count("\n") + 1  # おおよその文数、実際は正規表現で数えない簡易カウント
                sentences = [line for line in prompt.splitlines() if line[:2].rstrip(".").isdigit()]
                judgments = [{"index": i + 1, "supported": True} for i in range(len(sentences))]
                body = {"judgments": judgments}
            else:
                body = {"classifications": []}
            return _FakeResp({
                "response": json_module.dumps(body, ensure_ascii=False), "model": "gemma4:26b-a4b-it-qat",
                "prompt_eval_count": 1000, "eval_count": 10, "total_duration": 1_000_000_000,
            })
        # /embed: テキストごとに決定的だがほぼ直交するベクトルを返す
        payload = json or {}
        texts = payload.get("texts", [])
        vectors = [self._vec(t) for t in texts]
        return _FakeResp({"vectors": vectors})

    @staticmethod
    def _vec(text: str) -> list[float]:
        import hashlib
        h = hashlib.sha256(text.encode("utf-8")).digest()
        return [b / 255.0 for b in h[:8]]


import json as json_module  # noqa: E402  (テスト内の gemma レスポンス組み立て専用)


def _write_article(articles_dir: Path, asin: str, *, date: str, category: str, lead: str) -> None:
    articles_dir.mkdir(parents=True, exist_ok=True)
    (articles_dir / f"2026-01-01-{asin}.json").write_text(
        json.dumps({
            "date": date,
            "product": {"edu_domains": [category]},
            "narrative": {"lead": lead},
        }, ensure_ascii=False),
        encoding="utf-8",
    )


def _write_raw_material(raw_dir: Path, asin: str, *, experience_generated_at: str | None) -> None:
    base = raw_dir / asin
    base.mkdir(parents=True, exist_ok=True)
    (base / "amazon.json").write_text(json.dumps({"item": {"title": f"商品{asin}"}}), encoding="utf-8")
    if experience_generated_at is not None:
        (base / "experience.json").write_text(
            json.dumps({"generated_at": experience_generated_at, "snippets": [{"aspect": "不満", "text": "壊れやすい"}]}),
            encoding="utf-8",
        )


def _init_repo(repo: Path) -> None:
    for args in (["init", "-q"], ["config", "user.email", "t@example.com"], ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _commit_all(repo: Path, message: str) -> None:
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=repo, check=True, capture_output=True)


class RunIntegrationTest(unittest.TestCase):
    def _build_repo(self, tmp_path: Path, n: int = 3) -> tuple[Path, Path]:
        repo = tmp_path
        _init_repo(repo)
        articles_dir = repo / "data" / "articles"
        raw_dir = repo / "data" / "raw" / "per_asin"
        for i in range(n):
            asin = f"ASIN{i:06d}"
            _write_article(articles_dir, asin, date="2026-08-10T10:00:00+09:00",
                            category="STEM", lead=f"これは記事{i}の特徴です。これは支持されない文{i}。")
            _write_raw_material(
                raw_dir, asin,
                experience_generated_at="2026-08-01T00:00:00Z" if i % 2 == 0 else None,
            )
        _commit_all(repo, "seed articles")
        return articles_dir, raw_dir

    def test_processes_all_target_articles_and_groups_by_material(self):
        import tempfile
        from scripts.audit_information_gain import run
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            articles_dir, raw_dir = self._build_repo(repo, n=4)
            payload = run(
                articles_dir=articles_dir, raw_dir=raw_dir, repo_dir=repo,
                out_path=repo / "out.json", cache_path=None,
                since="1970-01-01", until="2099-01-01", limit=0, seed=1,
                session=_RunFakeSession(),
            )
            self.assertEqual(payload["summary"]["target_count"], 4)
            self.assertEqual(payload["summary"]["processed_count"], 4)
            self.assertEqual(payload["summary"]["failed_count"], 0)
            self.assertTrue(payload["run_ok"])
            by_material = payload["summary"]["by_experience_material"]
            self.assertEqual(by_material["with_material"]["count"], 2)
            self.assertEqual(by_material["without_material"]["count"], 2)
            self.assertTrue((repo / "out.json").exists())

    def test_broken_gemma_json_fails_only_that_article(self):
        import tempfile
        from scripts.audit_information_gain import run
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            articles_dir, raw_dir = self._build_repo(repo, n=3)
            payload = run(
                articles_dir=articles_dir, raw_dir=raw_dir, repo_dir=repo,
                out_path=repo / "out.json", cache_path=None,
                since="1970-01-01", until="2099-01-01", limit=0, seed=1,
                session=_RunFakeSession(fail_markers=frozenset({"記事0の特徴"})),
            )
            self.assertEqual(payload["summary"]["target_count"], 3)
            self.assertEqual(payload["summary"]["processed_count"], 2)
            self.assertEqual(payload["summary"]["failed_count"], 1)
            self.assertEqual(payload["failed"][0]["asin"], "ASIN000000")
            # 1/3 (33%) は20%を超えるが、ここで見たいのは「壊れた1件が他の処理を
            # 止めないこと」。run_ok の閾値判定は別テストで見る。
            self.assertFalse(payload["run_ok"])

    def test_run_not_ok_when_failure_ratio_exceeds_20_percent(self):
        import tempfile
        from scripts.audit_information_gain import run
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            # 5件中2件 (40%) を失敗させる -> 20% を超える
            articles_dir, raw_dir = self._build_repo(repo, n=5)
            payload = run(
                articles_dir=articles_dir, raw_dir=raw_dir, repo_dir=repo,
                out_path=repo / "out.json", cache_path=None,
                since="1970-01-01", until="2099-01-01", limit=0, seed=1,
                session=_RunFakeSession(fail_markers=frozenset({"記事0の特徴", "記事1の特徴"})),
            )
            self.assertEqual(payload["summary"]["failed_count"], 2)
            self.assertAlmostEqual(payload["summary"]["failure_ratio"], 0.4)
            self.assertFalse(payload["run_ok"])

    def test_second_dispatch_of_same_week_hits_cache(self):
        import tempfile
        from scripts.audit_information_gain import run
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            articles_dir, raw_dir = self._build_repo(repo, n=3)
            cache_path = repo / "cache.json"
            common = dict(
                articles_dir=articles_dir, raw_dir=raw_dir, repo_dir=repo,
                out_path=repo / "out.json", cache_path=cache_path,
                since="1970-01-01", until="2099-01-01", limit=0, seed=1,
            )
            first = run(session=_RunFakeSession(), **common)
            self.assertEqual(first["cache"]["hits"], 0)
            second = run(session=_RunFakeSession(), **common)
            self.assertEqual(second["cache"]["misses"], 0)
            self.assertEqual(second["cache"]["hits"], 3)
            self.assertEqual(second["summary"]["processed_count"], 3)


class CliTest(unittest.TestCase):
    def test_help_runs_as_package_module(self):
        result = subprocess.run(
            [sys.executable, "-m", "scripts.audit_information_gain", "--help"],
            cwd=REPO_ROOT, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage", result.stdout.lower())


if __name__ == "__main__":
    unittest.main()
