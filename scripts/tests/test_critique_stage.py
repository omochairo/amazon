"""scripts/experimental/multistage_brief/critique_stage.py の単体テスト。

T3 (#7310) で実装されたが、群B (角度前段) が無効判定となったため一度も判定に
使われず、ネットワークを叩かないテストも無かった (#4841 ③ 自己批判パス単独評価
で初めて使う前に、まずここでカバーする)。

ネットワーク (gemma) を叩く箇所は Fake セッションで差し替える
(test_multistage_brief.py の _FakeGemmaSession と同じ形)。
"""
from __future__ import annotations

import json
import unittest

from scripts.experimental.multistage_brief import critique_stage


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeGemmaSession:
    """/api/generate 呼び出しを固定レスポンスで返す。"""

    def __init__(self, response_text, prompt_eval_count=1_000, eval_count=20, total_duration=1_000_000_000):
        self.response_text = response_text
        self.prompt_eval_count = prompt_eval_count
        self.eval_count = eval_count
        self.total_duration = total_duration
        self.calls = []

    def post(self, url, json=None, timeout=None, headers=None):
        self.calls.append(json)
        return _FakeResp({
            "response": self.response_text,
            "model": json.get("model"),
            "prompt_eval_count": self.prompt_eval_count,
            "eval_count": self.eval_count,
            "total_duration": self.total_duration,
        })


class BuildCritiqueInputTest(unittest.TestCase):
    def test_includes_score_for_known_key(self):
        text = critique_stage.build_critique_input(
            {"lead": "本文A"}, {"lead": 0.9123},
        )
        self.assertIn("## lead [max_sim_vs_corpus=0.9123]", text)
        self.assertIn("本文A", text)

    def test_uses_na_for_missing_score(self):
        text = critique_stage.build_critique_input({"lead": "本文A"}, {})
        self.assertIn("max_sim_vs_corpus=N/A", text)


class CritiqueParagraphsTest(unittest.TestCase):
    def test_flags_only_keys_present_in_narrative(self):
        response = json.dumps({
            "flagged": [
                {"key": "lead", "reason": "定型句的"},
                {"key": "not_a_real_key", "reason": "無視されるべき"},
            ]
        })
        session = _FakeGemmaSession(response)
        result = critique_stage.critique_paragraphs(
            {"lead": "本文A", "closing": "本文B"}, {"lead": 0.95, "closing": 0.2},
            ollama_url="http://fake", session=session,
        )
        self.assertEqual(result["flagged"], [{"key": "lead", "reason": "定型句的"}])
        self.assertEqual(len(session.calls), 1)

    def test_empty_flagged_when_gemma_returns_no_array(self):
        session = _FakeGemmaSession(json.dumps({"flagged": []}))
        result = critique_stage.critique_paragraphs(
            {"lead": "本文A"}, {"lead": 0.5}, ollama_url="http://fake", session=session,
        )
        self.assertEqual(result["flagged"], [])

    def test_malformed_flagged_entries_are_dropped(self):
        session = _FakeGemmaSession(json.dumps({"flagged": ["not_a_dict", {"reason": "keyが無い"}]}))
        result = critique_stage.critique_paragraphs(
            {"lead": "本文A"}, {"lead": 0.5}, ollama_url="http://fake", session=session,
        )
        self.assertEqual(result["flagged"], [])


class RewriteFlaggedTest(unittest.TestCase):
    def test_skips_gemma_call_when_nothing_flagged(self):
        session = _FakeGemmaSession("should not be called")
        result = critique_stage.rewrite_flagged(
            "素材", {"lead": "本文A", "closing": "本文B"}, [], ollama_url="http://fake", session=session,
        )
        self.assertEqual(result["narrative"], {"lead": "本文A", "closing": "本文B"})
        self.assertEqual(result["rewritten_keys"], [])
        self.assertIsNone(result["call_meta"])
        self.assertEqual(session.calls, [])

    def test_rewrites_only_flagged_keys_others_untouched(self):
        response = json.dumps({"lead": "書き直した本文A"})
        session = _FakeGemmaSession(response)
        flagged = [{"key": "lead", "reason": "定型句的"}]
        result = critique_stage.rewrite_flagged(
            "素材", {"lead": "元の本文A", "closing": "元の本文B"}, flagged,
            ollama_url="http://fake", session=session,
        )
        self.assertEqual(result["narrative"]["lead"], "書き直した本文A")
        self.assertEqual(result["narrative"]["closing"], "元の本文B")
        self.assertEqual(result["rewritten_keys"], ["lead"])
        self.assertIsNotNone(result["call_meta"])

    def test_keeps_original_when_rewrite_missing_key_in_response(self):
        session = _FakeGemmaSession(json.dumps({}))
        flagged = [{"key": "lead", "reason": "定型句的"}]
        result = critique_stage.rewrite_flagged(
            "素材", {"lead": "元の本文A"}, flagged, ollama_url="http://fake", session=session,
        )
        self.assertEqual(result["narrative"]["lead"], "元の本文A")
        self.assertEqual(result["rewritten_keys"], [])


if __name__ == "__main__":
    unittest.main()
