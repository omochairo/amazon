"""#4841 T3 (scripts/experimental/multistage_brief/) の pure function 単体テスト。

ネットワーク (gemma/Ruri) を叩く箇所は Fake セッションで差し替える。
"""
from __future__ import annotations

import unittest

from scripts.experimental.multistage_brief import (
    angle_stage,
    guardrails,
    raw_material,
    run_experiment,
    select_asins,
)
from scripts.experimental.multistage_brief.ollama_client import (
    call_gemma,
    estimate_tokens,
    parse_json_response,
)


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeGemmaSession:
    """/api/generate 呼び出しを固定レスポンスで返す。"""

    def __init__(self, response_text, prompt_eval_count=100, eval_count=20, total_duration=1_000_000_000):
        self.response_text = response_text
        self.prompt_eval_count = prompt_eval_count
        self.eval_count = eval_count
        self.total_duration = total_duration
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append(json)
        return _FakeResp({
            "response": self.response_text,
            "model": json.get("model"),
            "prompt_eval_count": self.prompt_eval_count,
            "eval_count": self.eval_count,
            "total_duration": self.total_duration,
        })


class RawMaterialTest(unittest.TestCase):
    def test_build_material_text_handles_missing_sections(self):
        raw = {"asin": "X", "amazon": None, "competitors": None, "experience": None, "youtube": None, "news": None}
        text = raw_material.build_material_text(raw)
        self.assertEqual(text, "")

    def test_build_material_text_includes_amazon_and_competitors(self):
        raw = {
            "asin": "X",
            "amazon": {"item": {"title": "商品A", "price": 1000, "features": ["特徴1", "特徴2"]}},
            "competitors": {"competitors": [{"asin": "Y0000000001", "name": "競合A", "price": 2000}]},
            "experience": None, "youtube": None, "news": None,
        }
        text = raw_material.build_material_text(raw)
        self.assertIn("商品A", text)
        self.assertIn("1000円", text)
        self.assertIn("競合A", text)
        self.assertIn("Y0000000001", text)

    def test_build_material_text_truncates(self):
        raw = {
            "asin": "X",
            "amazon": {"item": {"title": "T" * 10, "features": ["特徴" * 3000]}},
            "competitors": None, "experience": None, "youtube": None, "news": None,
        }
        text = raw_material.build_material_text(raw, max_len=200)
        self.assertLessEqual(len(text), 200)

    def test_allowed_competitor_asins(self):
        raw = {"competitors": {"competitors": [{"asin": "A0000000001"}, {"asin": "A0000000002"}, {}]}}
        self.assertEqual(raw_material.allowed_competitor_asins(raw), {"A0000000001", "A0000000002"})

    def test_experience_snippets_by_aspect(self):
        raw = {"experience": {"snippets": [
            {"aspect": "不満", "text": "壊れやすい"},
            {"aspect": "不満", "text": "重い"},
            {"aspect": "体験談", "text": "楽しい"},
            {"aspect": "不満", "text": ""},
        ]}}
        out = raw_material.experience_snippets_by_aspect(raw)
        self.assertEqual(out["不満"], ["壊れやすい", "重い"])
        self.assertEqual(out["体験談"], ["楽しい"])


class OllamaClientTest(unittest.TestCase):
    def test_estimate_tokens(self):
        self.assertEqual(estimate_tokens(""), 1)
        self.assertEqual(estimate_tokens("ab"), 1)
        self.assertEqual(estimate_tokens("a" * 10), 5)

    def test_parse_json_response_plain(self):
        self.assertEqual(parse_json_response('{"a": 1}'), {"a": 1})

    def test_parse_json_response_code_fence(self):
        self.assertEqual(parse_json_response('```json\n{"a": 1}\n```'), {"a": 1})

    def test_parse_json_response_invalid_raises(self):
        with self.assertRaises(ValueError):
            parse_json_response("not json at all")

    def test_call_gemma_returns_metadata(self):
        session = _FakeGemmaSession('{"ok": true}', prompt_eval_count=50)
        result = call_gemma("短いプロンプト", num_ctx=4096, session=session)
        self.assertEqual(result["text"], '{"ok": true}')
        self.assertFalse(result["truncated"])
        self.assertEqual(result["num_ctx"], 4096)
        self.assertIsNotNone(result["prompt_sha256"])

    def test_call_gemma_detects_truncation_by_ratio(self):
        long_prompt = "あ" * 4000  # estimated_tokens = 2000
        session = _FakeGemmaSession('{"ok": true}', prompt_eval_count=100)  # << 0.7*2000
        with self.assertRaises(Exception):
            call_gemma(long_prompt, num_ctx=8192, session=session)

    def test_call_gemma_detects_truncation_near_num_ctx(self):
        session = _FakeGemmaSession('{"ok": true}', prompt_eval_count=950)
        with self.assertRaises(Exception):
            call_gemma("x" * 10, num_ctx=1000, session=session)


class AngleStageTest(unittest.TestCase):
    def test_coerce_candidate_list_from_bare_array(self):
        parsed = [{"angle": "a", "evidence": "e"}]
        self.assertEqual(angle_stage._coerce_candidate_list(parsed), parsed)

    def test_coerce_candidate_list_from_wrapped_object(self):
        parsed = {"candidates": [{"angle": "a", "evidence": "e"}]}
        self.assertEqual(angle_stage._coerce_candidate_list(parsed), [{"angle": "a", "evidence": "e"}])

    def test_coerce_candidate_list_from_single_object(self):
        parsed = {"angle": "a", "evidence": "e"}
        self.assertEqual(angle_stage._coerce_candidate_list(parsed), [parsed])

    def test_coerce_candidate_list_empty_on_junk(self):
        self.assertEqual(angle_stage._coerce_candidate_list("not a dict or list"), [])

    def test_select_angle_picks_lowest_max_sim(self):
        candidates = [{"angle": "A_TEXT", "evidence": "e1"}, {"angle": "B_TEXT", "evidence": "e2"}]

        class _EmbedSession:
            def post(self, url, json=None, timeout=None):
                vecs = {"A_TEXT": [1.0, 0.0], "B_TEXT": [0.0, 1.0]}
                return _FakeResp({"vectors": [vecs[t] for t in json["texts"]]})

        corpus_vectors = [[1.0, 0.0]]  # 既存コーパスは A_TEXT に近い
        result = angle_stage.select_angle(
            candidates, corpus_vectors, ruri_url="http://fake", session=_EmbedSession(),
        )
        self.assertEqual(result["selected"]["angle"], "B_TEXT")


class GuardrailsTest(unittest.TestCase):
    def test_check_asin_containment_ok(self):
        result = guardrails.check_asin_containment(
            "この商品と B0AAAAAAAA を比較しました。", allowed_asins={"B0AAAAAAAA"}, own_asin="B0OWN000001",
        )
        self.assertTrue(result["ok"])

    def test_check_asin_containment_flags_foreign_asin(self):
        result = guardrails.check_asin_containment(
            "この商品と B0FOREIGN1 を比較しました。", allowed_asins={"B0AAAAAAAA"}, own_asin="B0OWN000001",
        )
        self.assertFalse(result["ok"])
        self.assertIn("B0FOREIGN1", result["disallowed_asins"])

    def test_check_asin_containment_flags_foreign_product_name(self):
        result = guardrails.check_asin_containment(
            "無関係ブランドXYZのおもちゃとも比較できます。", allowed_asins=set(), own_asin="B0OWN000001",
            foreign_product_names=["無関係ブランドXYZ"],
        )
        self.assertFalse(result["ok"])
        self.assertIn("無関係ブランドXYZ", result["disallowed_product_names"])


class SelectAsinsTest(unittest.TestCase):
    def test_article_category_uses_first_edu_domain(self):
        article = {"product": {"edu_domains": ["STEM", "運動"]}}
        self.assertEqual(select_asins.article_category(article), "STEM")

    def test_article_category_unknown_when_missing(self):
        self.assertEqual(select_asins.article_category({"product": {}}), "unknown")

    def test_is_post_v7(self):
        self.assertTrue(select_asins.is_post_v7({"slug": "2026-08-01-B0XXXXXXXX"}))
        self.assertFalse(select_asins.is_post_v7({"slug": "2026-01-01-B0XXXXXXXX"}))
        self.assertTrue(select_asins.is_post_v7({"slug": "short"}))
        self.assertTrue(select_asins.is_post_v7({}))

    def test_select_target_asins_spreads_categories_and_is_deterministic(self):
        candidates = [
            {"asin": f"A{i}", "category": cat, "snippet_count": 3}
            for i, cat in enumerate(["STEM"] * 4 + ["言語"] * 4 + ["運動"] * 4)
        ]
        result1 = select_asins.select_target_asins(candidates, target_count=6, seed=42)
        result2 = select_asins.select_target_asins(candidates, target_count=6, seed=42)
        self.assertEqual([r["asin"] for r in result1], [r["asin"] for r in result2])
        categories = {r["category"] for r in result1}
        self.assertGreaterEqual(len(categories), 3)

    def test_select_target_asins_caps_at_available_candidates(self):
        candidates = [{"asin": "A1", "category": "STEM", "snippet_count": 3}]
        result = select_asins.select_target_asins(candidates, target_count=10, seed=1)
        self.assertEqual(len(result), 1)


class RunExperimentPureTest(unittest.TestCase):
    def test_is_broken_narrative_empty(self):
        self.assertTrue(run_experiment.is_broken_narrative({}))

    def test_is_broken_narrative_too_few_keys(self):
        self.assertTrue(run_experiment.is_broken_narrative({"lead": "a", "closing": "b"}))

    def test_is_broken_narrative_duplicate_paragraphs(self):
        narrative = {k: "同じ文章です。" for k in
                     ("lead", "why_this_product", "gift_appeal", "daily_use", "safety_note")}
        self.assertTrue(run_experiment.is_broken_narrative(narrative))

    def test_is_broken_narrative_ok(self):
        narrative = {
            "lead": "導入文です。", "why_this_product": "理由です。", "gift_appeal": "魅力です。",
            "daily_use": "使い方です。", "safety_note": "注意です。",
        }
        self.assertFalse(run_experiment.is_broken_narrative(narrative))

    def _fake_result(self, label_stats):
        groups = {}
        for label, stats in label_stats.items():
            groups[label] = {
                "narrative": {"lead": "x"},
                "entailment": {"total_unsupported": stats["unsupported"], "by_key": {}},
                "asin_containment": {"ok": stats.get("containment_ok", True)},
                "usage_rate": {"snippet_rate": stats["usage"]},
                "uniqueness": {"max_sim": stats["max_sim"], "centroid_sim": stats["max_sim"] - 0.02},
            }
        return {"asin": "X", "groups": groups, "calls": [{"total_duration_s": 10.0, "estimated_prompt_tokens": 500}]}

    def test_summarize_verdict_b_and_c_effective(self):
        result = self._fake_result({
            "A": {"unsupported": 2, "usage": 0.5, "max_sim": 0.95},
            "B": {"unsupported": 1, "usage": 0.6, "max_sim": 0.90},
            "C": {"unsupported": 1, "usage": 0.65, "max_sim": 0.85},
        })
        summary = run_experiment.summarize([result])
        self.assertEqual(summary["verdict"], "B有効 / C有効")

    def test_summarize_verdict_b_ineffective_when_unsupported_increases(self):
        result = self._fake_result({
            "A": {"unsupported": 1, "usage": 0.5, "max_sim": 0.95},
            "B": {"unsupported": 3, "usage": 0.6, "max_sim": 0.80},
            "C": {"unsupported": 3, "usage": 0.6, "max_sim": 0.80},
        })
        summary = run_experiment.summarize([result])
        self.assertEqual(summary["verdict"], "B無効 (事前登録した判定基準を満たさない)")

    def test_summarize_cost_aggregation(self):
        result = self._fake_result({
            "A": {"unsupported": 0, "usage": 1.0, "max_sim": 0.9},
            "B": {"unsupported": 0, "usage": 1.0, "max_sim": 0.9},
            "C": {"unsupported": 0, "usage": 1.0, "max_sim": 0.9},
        })
        summary = run_experiment.summarize([result, result])
        self.assertEqual(summary["cost"]["asin_count"], 2)
        self.assertEqual(summary["cost"]["total_gemma_calls"], 2)


if __name__ == "__main__":
    unittest.main()
