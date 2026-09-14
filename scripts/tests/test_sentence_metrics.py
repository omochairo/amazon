"""scripts/experimental/multistage_brief/sentence_metrics.py unit tests (#4841 M1-b)。"""
from __future__ import annotations

import unittest

from scripts.experimental.multistage_brief.sentence_metrics import (
    build_entailment_prompt,
    build_sentence_pool,
    compute_information_gain,
    compute_sentence_uniqueness,
    flatten_narrative_sentences,
    parse_entailment_response,
    split_sentences,
)


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
        """#4841 M1 実データ検証で発覚: 現行記事の narrativeSection は文単位の

        array (例: why_this_product) のことが多く、これを str 専用の処理で
        読むと丸ごと0件になり、新しい記事ほど文が消える (今回の元バグ)。
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
            {"index": 5, "supported": True},  # out of range
            {"index": 1, "supported": "yes"},  # not bool
            {"foo": "bar"},  # missing keys
        ]}
        result = parse_entailment_response(parsed, 2)
        self.assertEqual(result["supported_flags"], [None, None])
        self.assertEqual(result["unresolved_indices"], [1, 2])

    def test_empty_judgments_all_unresolved(self):
        result = parse_entailment_response({}, 2)
        self.assertEqual(result["supported_flags"], [None, None])
        self.assertEqual(result["unresolved_indices"], [1, 2])


# --------------------------------------------------------------------------
# Ruri をモックした uniqueness / information gain
# --------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeSession:
    """compute_semantic_related.embed_batch_ruri は常に kind=document で呼ぶ。"""

    VECTORS = {
        "UNIQUE_SENT": [1.0, 0.0, 0.0],
        "GENERIC_SENT": [0.0, 1.0, 0.0],
        "SAME_CAT_1": [0.0, 1.0, 0.0],  # GENERIC_SENT と同じ向き = 似ている
        "CROSS_CAT_1": [0.0, 0.0, 1.0],  # どちらとも似ていない
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
        # UNIQUE_SENT は SAME_CAT_1 と直交 (sim=0) なので、閾値 (cross_sim=0 の p95=0) 未満ではない
        # (0 < 0 は False) が、GENERIC_SENT は SAME_CAT_1 と同じ向き (sim=1) で閾値(0)を超える
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
    """call_gemma (ollama /api/generate) と embed_batch_ruri (/embed) の両方に応答する。"""

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
        # 支持される文の same_category_max_sim は 別カテゴリ分布のp95閾値と比較される
        self.assertIn(result["unique_and_supported_count"], (0, 1))

    def test_no_material_or_narrative_is_empty(self):
        result = compute_information_gain(
            {}, "素材", same_category_pool=[], cross_category_pool=[],
            ruri_url="http://ruri:8000", session=_EntailmentFakeSession([]),
        )
        self.assertEqual(result["sentence_count"], 0)
        self.assertEqual(result["unique_and_supported_count"], 0)
        self.assertEqual(result["unsupported_count"], 0)


if __name__ == "__main__":
    unittest.main()
