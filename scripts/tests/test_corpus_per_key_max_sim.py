"""scripts/experimental/multistage_brief/corpus.py の per_key_max_sim 単体テスト。

critique_stage.critique_paragraphs (#4841 ③ 自己批判パス) への入力 (key_scores) を
作る関数で、これまでテストが無かった。Ruri /embed 呼び出しは Fake セッションで
差し替える (テキスト文字列で固定ベクトルを返す)。
"""
from __future__ import annotations

import unittest

from scripts.experimental.multistage_brief import corpus


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeRuriSession:
    """/embed 呼び出しを、テキスト -> 固定ベクトルの辞書引きで返す。"""

    def __init__(self, vectors: dict[str, list[float]]):
        self.vectors = vectors
        self.calls = []

    def post(self, url, json=None, timeout=None, headers=None):
        self.calls.append(json)
        texts = json["texts"]
        return _FakeResp({"vectors": [self.vectors[t] for t in texts]})


class PerKeyMaxSimTest(unittest.TestCase):
    def test_picks_max_similarity_across_corpus_articles(self):
        vectors = {
            "LEAD_TEXT": [1.0, 0.0],
            "CORPUS_LEAD_IDENTICAL": [1.0, 0.0],
            "CORPUS_LEAD_ORTHOGONAL": [0.0, 1.0],
        }
        session = _FakeRuriSession(vectors)
        narrative = {"lead": "LEAD_TEXT"}
        corpus_articles = [
            {"narrative": {"lead": "CORPUS_LEAD_ORTHOGONAL"}},
            {"narrative": {"lead": "CORPUS_LEAD_IDENTICAL"}},
        ]
        result = corpus.per_key_max_sim(narrative, corpus_articles, ruri_url="http://fake", session=session)
        self.assertAlmostEqual(result["lead"], 1.0)

    def test_zero_when_key_absent_from_all_corpus_articles(self):
        vectors = {"LEAD_TEXT": [1.0, 0.0]}
        session = _FakeRuriSession(vectors)
        narrative = {"lead": "LEAD_TEXT"}
        corpus_articles = [{"narrative": {"closing": "他のキーだけ"}}]
        result = corpus.per_key_max_sim(narrative, corpus_articles, ruri_url="http://fake", session=session)
        self.assertEqual(result["lead"], 0.0)

    def test_skips_empty_narrative_values(self):
        session = _FakeRuriSession({})
        result = corpus.per_key_max_sim({"lead": ""}, [], ruri_url="http://fake", session=session)
        self.assertEqual(result, {})
        self.assertEqual(session.calls, [])


if __name__ == "__main__":
    unittest.main()
