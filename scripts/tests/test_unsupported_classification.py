"""scripts/experimental/multistage_brief/unsupported_classification.py unit tests (#4841 M2)。"""
from __future__ import annotations

import json
import unittest

from scripts.experimental.multistage_brief.unsupported_classification import (
    FACTUAL_CLAIM,
    RHETORICAL_OR_TIME_DEPENDENT,
    build_classify_prompt,
    classify_unsupported_sentences,
    parse_classify_response,
    summarize_categories,
)


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


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeGemmaSession:
    def __init__(self, classifications):
        self._classifications = classifications

    def post(self, url, json=None, timeout=None):
        body = {"classifications": self._classifications}
        return _FakeResp({
            "response": json_dumps(body), "model": "gemma4:26b-a4b-it-qat",
            "prompt_eval_count": 1000, "eval_count": 10, "total_duration": 1_000_000_000,
        })


def json_dumps(obj):
    return json.dumps(obj, ensure_ascii=False)


class ClassifyUnsupportedSentencesTest(unittest.TestCase):
    def test_classifies_and_returns_call_meta(self):
        session = _FakeGemmaSession([{"index": 1, "category": FACTUAL_CLAIM}])
        result = classify_unsupported_sentences(["文1。"], session=session)
        self.assertEqual(result["categories"], [FACTUAL_CLAIM])
        self.assertIsNotNone(result["call_meta"])

    def test_empty_sentences_skips_call(self):
        result = classify_unsupported_sentences([], session=_FakeGemmaSession([]))
        self.assertEqual(result, {"categories": [], "call_meta": None})


if __name__ == "__main__":
    unittest.main()
