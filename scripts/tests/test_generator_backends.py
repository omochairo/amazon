"""scripts/experimental/multistage_brief/generator_backends.py の単体テスト (#4841 P1)。

agy (subprocess) は Fake runner で差し替え、実際の agy/ollama は叩かない。
"""
from __future__ import annotations

import json
import unittest

from scripts.experimental.multistage_brief import generator_backends


class _FakeCompletedProcess:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class BuildAgyArgvTest(unittest.TestCase):
    def test_model_placed_before_print(self):
        argv = generator_backends.build_agy_argv("プロンプト", "gemini-3.1-pro-high")
        self.assertEqual(
            argv,
            ["agy", "--model", "gemini-3.1-pro-high", "--output-format", "json", "--print=プロンプト"],
        )

    def test_no_model_flag_when_model_empty(self):
        argv = generator_backends.build_agy_argv("プロンプト", "")
        self.assertEqual(argv, ["agy", "--output-format", "json", "--print=プロンプト"])


class BuildAgyPromptTest(unittest.TestCase):
    def test_matches_baseline_prompt_template(self):
        from scripts.experimental.multistage_brief import narrative_stage

        prompt = generator_backends.build_agy_prompt("素材テキスト")
        expected = narrative_stage.BASELINE_PROMPT_TEMPLATE.format(
            style_guide=narrative_stage.STYLE_GUIDE, material_text="素材テキスト",
            schema=narrative_stage.NARRATIVE_OUTPUT_SCHEMA,
        )
        self.assertEqual(prompt, expected)


class CountUrlsTest(unittest.TestCase):
    def test_counts_urls_across_keys(self):
        narrative = {
            "lead": "詳しくは https://example.com/a をご覧ください。",
            "closing": "こちらも https://example.com/b と https://example.com/c 。",
            "safety_note": "URLなし。",
        }
        self.assertEqual(generator_backends.count_urls(narrative), 3)

    def test_zero_when_no_urls(self):
        self.assertEqual(generator_backends.count_urls({"lead": "URLなしの文章です。"}), 0)


class CallAgyTest(unittest.TestCase):
    def _runner(self, result):
        return lambda cmd, **kwargs: result

    def test_returns_parsed_envelope_on_success(self):
        envelope = (
            '{"conversation_id": "c1", "status": "SUCCESS", '
            '"response": "{\\"lead\\": \\"導入\\"}", "num_turns": 1, '
            '"usage": {"input_tokens": 10, "output_tokens": 5}, "duration_seconds": 3.2}'
        )
        runner = self._runner(_FakeCompletedProcess(returncode=0, stdout=envelope))
        result = generator_backends.call_agy("プロンプト", model="gemini-3.1-pro-high", runner=runner, sleeper=lambda s: None)
        self.assertEqual(result["text"], '{"lead": "導入"}')
        self.assertEqual(result["num_turns"], 1)
        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(result["attempts_used"], 1)

    def test_raises_on_nonzero_exit(self):
        runner = self._runner(_FakeCompletedProcess(returncode=1, stderr="auth error"))
        with self.assertRaises(generator_backends.AgyGenerationError):
            generator_backends.call_agy("プロンプト", runner=runner, sleeper=lambda s: None)

    def test_retries_once_on_empty_response_then_succeeds(self):
        calls = []
        envelope = '{"status": "SUCCESS", "response": "ok", "num_turns": 1}'

        def runner(cmd, **kwargs):
            calls.append(cmd)
            if len(calls) == 1:
                return _FakeCompletedProcess(returncode=0, stdout="", stderr="empty turn")
            return _FakeCompletedProcess(returncode=0, stdout=envelope)

        result = generator_backends.call_agy("プロンプト", runner=runner, sleeper=lambda s: None)
        self.assertEqual(result["text"], "ok")
        self.assertEqual(result["attempts_used"], 2)

    def test_raises_after_exhausting_retries_on_empty_response(self):
        runner = self._runner(_FakeCompletedProcess(returncode=0, stdout="", stderr="still empty"))
        with self.assertRaises(generator_backends.AgyGenerationError):
            generator_backends.call_agy("プロンプト", runner=runner, sleeper=lambda s: None)

    def test_raises_when_agy_not_found(self):
        def runner(cmd, **kwargs):
            raise FileNotFoundError()

        with self.assertRaises(generator_backends.AgyGenerationError):
            generator_backends.call_agy("プロンプト", runner=runner, sleeper=lambda s: None)

    def test_raises_when_response_missing_text_field(self):
        runner = self._runner(_FakeCompletedProcess(returncode=0, stdout='{"status": "SUCCESS"}'))
        with self.assertRaises(generator_backends.AgyGenerationError):
            generator_backends.call_agy("プロンプト", runner=runner, sleeper=lambda s: None)


class GenerateNarrativeAgyTest(unittest.TestCase):
    def test_extracts_narrative_and_records_call_meta(self):
        response_text = '{"lead": "導入文です。", "why_this_product": "理由です。"}'
        envelope = json.dumps({"status": "SUCCESS", "response": response_text, "num_turns": 1})

        def runner(cmd, **kwargs):
            return _FakeCompletedProcess(returncode=0, stdout=envelope)

        out = generator_backends.generate_narrative_agy(
            "素材テキスト", model="gemini-3.1-pro-high", runner=runner, sleeper=lambda s: None,
        )
        self.assertEqual(out["narrative"]["lead"], "導入文です。")
        self.assertEqual(out["call_meta"]["generator"], "agy")
        self.assertEqual(out["call_meta"]["model_id"], "gemini-3.1-pro-high")
        self.assertEqual(out["call_meta"]["num_turns"], 1)
        self.assertEqual(out["call_meta"]["url_count_in_narrative"], 0)

    def test_counts_urls_leaked_into_narrative(self):
        response_text = json.dumps({"lead": "詳しくは https://example.com/x をご覧ください。"})
        envelope = json.dumps({"status": "SUCCESS", "response": response_text, "num_turns": 3})

        def runner(cmd, **kwargs):
            return _FakeCompletedProcess(returncode=0, stdout=envelope)

        out = generator_backends.generate_narrative_agy(
            "素材テキスト", runner=runner, sleeper=lambda s: None,
        )
        self.assertEqual(out["call_meta"]["url_count_in_narrative"], 1)
        self.assertEqual(out["call_meta"]["num_turns"], 3)


class GeneratorsRegistryTest(unittest.TestCase):
    def test_registry_has_both_backends(self):
        self.assertEqual(set(generator_backends.GENERATORS), {"ollama", "agy"})


if __name__ == "__main__":
    unittest.main()
