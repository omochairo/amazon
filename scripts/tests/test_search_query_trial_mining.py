"""scripts/experimental/search_query_trial/mining.py の単体テスト (#4841 V2)。

call_gemma (ollama /api/generate) 経由での num_ctx 明示・切り詰め検出が
効くことを確認する (共通ルール)。
"""
from __future__ import annotations

import json
import unittest

from scripts.experimental.search_query_trial.mining import extract_snippets_checked


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, payload):
        self._payload = payload

    def post(self, url, json=None, timeout=None, headers=None):
        return _FakeResp(self._payload)


def _ok_response(entailed=True, snippets=None, prompt_eval_count=1000):
    body = {"entailed": entailed, "snippets": snippets or []}
    return {
        "response": json.dumps(body, ensure_ascii=False),
        "model": "gemma4:26b-a4b-it-qat",
        "prompt_eval_count": prompt_eval_count,
        "eval_count": 10,
        "total_duration": 1_000_000_000,
    }


class ExtractSnippetsCheckedTest(unittest.TestCase):
    def test_empty_text_short_circuits(self):
        snippets, meta = extract_snippets_checked(
            {"text": "  ", "source_type": "blog", "source_url": "https://x"},
            "レゴ", "LEGO", session=_FakeSession({}),
        )
        self.assertEqual(snippets, [])
        self.assertEqual(meta["status"], "empty_text")

    def test_entailed_snippet_is_extracted_with_usable_as(self):
        payload = _ok_response(entailed=True, snippets=[
            {"aspect": "不満", "text": "坂で脱線しやすい" * 5, "confidence": "high"},
        ])
        candidate = {"text": "レゴの口コミ本文...", "source_type": "blog", "source_url": "https://x/1"}
        snippets, meta = extract_snippets_checked(
            candidate, "レゴ クラシック", "LEGO", session=_FakeSession(payload),
        )
        self.assertEqual(meta["status"], "ok")
        self.assertEqual(len(snippets), 1)
        self.assertEqual(snippets[0]["aspect"], "不満")
        self.assertEqual(snippets[0]["usable_as"], "quote")  # blog -> quote
        self.assertEqual(snippets[0]["source_url"], "https://x/1")

    def test_not_entailed_returns_no_snippets(self):
        payload = _ok_response(entailed=False)
        candidate = {"text": "無関係な文章", "source_type": "blog", "source_url": "https://x/1"}
        snippets, meta = extract_snippets_checked(
            candidate, "レゴ クラシック", "LEGO", session=_FakeSession(payload),
        )
        self.assertEqual(snippets, [])
        self.assertEqual(meta["status"], "ok")

    def test_truncation_is_detected_and_treated_as_failure(self):
        # prompt_eval_count が推定トークン数より大幅に小さい -> 切り詰め疑い
        long_text = "商品についての長いレビュー文章。" * 200
        payload = _ok_response(entailed=True, prompt_eval_count=1)
        candidate = {"text": long_text, "source_type": "blog", "source_url": "https://x/1"}
        snippets, meta = extract_snippets_checked(
            candidate, "レゴ クラシック", "LEGO", session=_FakeSession(payload),
        )
        self.assertEqual(snippets, [])
        self.assertEqual(meta["status"], "truncated")

    def test_bad_json_response_is_reported_not_silently_dropped(self):
        payload = {
            "response": "not json at all", "model": "gemma4:26b-a4b-it-qat",
            "prompt_eval_count": 1000, "eval_count": 10, "total_duration": 1_000_000_000,
        }
        candidate = {"text": "本文", "source_type": "blog", "source_url": "https://x/1"}
        snippets, meta = extract_snippets_checked(
            candidate, "レゴ クラシック", "LEGO", session=_FakeSession(payload),
        )
        self.assertEqual(snippets, [])
        self.assertEqual(meta["status"], "bad_json")


if __name__ == "__main__":
    unittest.main()
