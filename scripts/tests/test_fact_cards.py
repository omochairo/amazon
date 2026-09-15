"""scripts/experimental/multistage_brief/fact_cards.py unit tests (#4841 M2)。"""
from __future__ import annotations

import json
import pathlib
import tempfile
import unittest

from scripts.experimental.multistage_brief.fact_cards import (
    annotate_support,
    annotate_uniqueness,
    build_atomic_material_items,
    build_extract_prompt,
    build_fact_cards_for_asin,
    count_body_content_coverage,
    extract_fact_candidates,
    format_fact_cards,
    parse_extract_response,
    select_fact_cards,
)


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeSession:
    """/api/generate (call_gemma) と /embed (embed_batch_ruri) の両方に応答する。"""

    def __init__(self, *, extract_candidates=None, entailment_judgments=None, vector_map=None):
        self._extract_candidates = extract_candidates
        self._entailment_judgments = entailment_judgments
        self._vector_map = vector_map or {}
        self.generate_calls: list[dict] = []

    def post(self, url, json=None, timeout=None):
        payload = json or {}
        if url.endswith("/api/generate"):
            self.generate_calls.append(payload)
            prompt = payload.get("prompt", "")
            if "候補" in prompt and self._extract_candidates is not None:
                body = {"candidates": self._extract_candidates}
            else:
                body = {"judgments": self._entailment_judgments or []}
            return _FakeResp({
                "response": __import__("json").dumps(body, ensure_ascii=False),
                "model": "gemma4:26b-a4b-it-qat", "prompt_eval_count": 1000, "eval_count": 10,
                "total_duration": 1_000_000_000,
            })
        texts = payload.get("texts", [])
        return _FakeResp({"vectors": [self._vector_map.get(t, [0.5, 0.5]) for t in texts]})


class BuildAtomicMaterialItemsTest(unittest.TestCase):
    def test_includes_amazon_features_and_price(self):
        raw = {"amazon": {"item": {"price": 1000, "features": ["特徴1", "特徴2", ""]}}}
        items = build_atomic_material_items(raw)
        texts = [i["text"] for i in items]
        self.assertIn("価格は1000円", texts)
        self.assertIn("特徴1", texts)
        self.assertIn("特徴2", texts)
        # 空文字列の feature は含めない
        self.assertEqual(len(texts), 3)

    def test_includes_experience_snippets_with_aspect(self):
        raw = {"experience": {"snippets": [{"aspect": "不満", "text": "不満点です"}]}}
        items = build_atomic_material_items(raw)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["aspect"], "不満")
        self.assertEqual(items[0]["source_file"], "experience.json")
        self.assertEqual(items[0]["source_field"], "snippets[0]")

    def test_excludes_competitors_and_youtube_news(self):
        raw = {
            "competitors": {"competitors": [{"asin": "B000000001", "name": "他社品"}]},
            "youtube": {"items": [{"title": "動画タイトル"}]},
            "news": {"items": [{"title": "ニュース見出し"}]},
        }
        items = build_atomic_material_items(raw)
        self.assertEqual(items, [])

    def test_indices_are_1_based_and_sequential(self):
        raw = {"amazon": {"item": {"features": ["a", "b"]}}}
        items = build_atomic_material_items(raw)
        self.assertEqual([i["index"] for i in items], [1, 2])

    def test_empty_raw_material(self):
        self.assertEqual(build_atomic_material_items({}), [])


class BuildExtractPromptTest(unittest.TestCase):
    def test_numbers_items_with_source(self):
        items = [{"index": 1, "source_file": "amazon.json", "source_field": "item.features[0]", "text": "特徴A", "aspect": None}]
        prompt = build_extract_prompt(items)
        self.assertIn("1. [amazon.json:item.features[0]] 特徴A", prompt)


class ParseExtractResponseTest(unittest.TestCase):
    def setUp(self):
        self.atomic_items = [
            {"index": 1, "source_file": "amazon.json", "source_field": "item.features[0]", "aspect": None, "text": "特徴A"},
            {"index": 2, "source_file": "experience.json", "source_field": "snippets[0]", "aspect": "不満", "text": "不満点"},
        ]

    def test_resolves_source_from_index(self):
        parsed = {"candidates": [{"source_index": 2, "fact_text": "書き換えた不満点"}]}
        result = parse_extract_response(parsed, self.atomic_items)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["source_file"], "experience.json")
        self.assertEqual(result[0]["source_field"], "snippets[0]")
        self.assertEqual(result[0]["aspect"], "不満")
        self.assertEqual(result[0]["text"], "書き換えた不満点")

    def test_out_of_range_index_is_dropped(self):
        parsed = {"candidates": [{"source_index": 99, "fact_text": "根拠不明"}]}
        result = parse_extract_response(parsed, self.atomic_items)
        self.assertEqual(result, [])

    def test_malformed_entries_are_dropped(self):
        parsed = {"candidates": [{"source_index": "1", "fact_text": "型不正"}, {"source_index": 1}, "not a dict"]}
        result = parse_extract_response(parsed, self.atomic_items)
        self.assertEqual(result, [])

    def test_empty_candidates(self):
        self.assertEqual(parse_extract_response({}, self.atomic_items), [])


class SelectFactCardsTest(unittest.TestCase):
    def _candidate(self, text, *, unique=True, supported=True, aspect=None):
        return {"text": text, "unique": unique, "supported": supported, "aspect": aspect, "source_file": "amazon.json", "source_field": "x"}

    def test_filters_to_unique_and_supported(self):
        candidates = [
            self._candidate("A", unique=True, supported=True),
            self._candidate("B", unique=False, supported=True),
            self._candidate("C", unique=True, supported=False),
            self._candidate("D", unique=True, supported=None),
        ]
        selected = select_fact_cards(candidates)
        self.assertEqual([c["text"] for c in selected], ["A"])

    def test_caps_complaint_cards_at_max(self):
        candidates = [
            self._candidate("不満1", aspect="不満"),
            self._candidate("不満2", aspect="不満"),
            self._candidate("普通1", aspect=None),
        ]
        selected = select_fact_cards(candidates, max_complaint_cards=1)
        aspects = [c["aspect"] for c in selected]
        self.assertEqual(aspects.count("不満"), 1)
        self.assertIn(None, aspects)

    def test_caps_total_at_max_cards(self):
        candidates = [self._candidate(f"C{i}") for i in range(10)]
        selected = select_fact_cards(candidates, max_cards=5)
        self.assertEqual(len(selected), 5)

    def test_preserves_input_order(self):
        candidates = [self._candidate("Z"), self._candidate("A"), self._candidate("M")]
        selected = select_fact_cards(candidates)
        self.assertEqual([c["text"] for c in selected], ["Z", "A", "M"])

    def test_empty_input(self):
        self.assertEqual(select_fact_cards([]), [])


class FormatFactCardsTest(unittest.TestCase):
    def test_formats_with_source(self):
        cards = [{"text": "事実A", "source_file": "amazon.json", "source_field": "item.features[0]"}]
        text = format_fact_cards(cards)
        self.assertEqual(text, "- [出典: amazon.json:item.features[0]] 事実A")

    def test_empty_cards(self):
        self.assertEqual(format_fact_cards([]), "(なし)")


class CountBodyContentCoverageTest(unittest.TestCase):
    def test_counts_items_without_body_fields_as_zero_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            (base / "B000000001").mkdir()
            (base / "B000000001" / "youtube.json").write_text(
                json.dumps({"items": [{"title": "t1", "url": "u1"}, {"title": "t2", "url": "u2"}]}), encoding="utf-8",
            )
            (base / "B000000001" / "news.json").write_text(json.dumps({"items": []}), encoding="utf-8")
            result = count_body_content_coverage(str(base) + "/*")
            self.assertEqual(result["youtube"]["item_count"], 2)
            self.assertEqual(result["youtube"]["items_with_body_count"], 0)
            self.assertEqual(result["youtube"]["body_coverage"], 0.0)
            self.assertIsNone(result["news"]["body_coverage"])

    def test_counts_items_with_body_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            (base / "B000000002").mkdir()
            (base / "B000000002" / "youtube.json").write_text(
                json.dumps({"items": [{"title": "t1", "caption": "字幕本文"}]}), encoding="utf-8",
            )
            result = count_body_content_coverage(str(base) + "/*")
            self.assertEqual(result["youtube"]["items_with_body_count"], 1)
            self.assertEqual(result["youtube"]["body_coverage"], 1.0)

    def test_no_matching_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = count_body_content_coverage(str(pathlib.Path(tmp)) + "/*")
            self.assertEqual(result["youtube"]["item_count"], 0)
            self.assertIsNone(result["youtube"]["body_coverage"])


class ExtractFactCandidatesTest(unittest.TestCase):
    def test_extracts_and_resolves_source(self):
        atomic_items = [{"index": 1, "source_file": "amazon.json", "source_field": "item.features[0]", "aspect": None, "text": "特徴A"}]
        session = _FakeSession(extract_candidates=[{"source_index": 1, "fact_text": "抽出された事実"}])
        result = extract_fact_candidates(atomic_items, session=session)
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(result["candidates"][0]["text"], "抽出された事実")
        self.assertIsNotNone(result["call_meta"])

    def test_empty_atomic_items_skips_call(self):
        session = _FakeSession()
        result = extract_fact_candidates([], session=session)
        self.assertEqual(result, {"candidates": [], "call_meta": None})
        self.assertEqual(session.generate_calls, [])


class AnnotateUniquenessTest(unittest.TestCase):
    def test_marks_unique_based_on_cross_category_threshold(self):
        candidates = [{"text": "UNIQUE_SENT"}, {"text": "GENERIC_SENT"}]
        vector_map = {
            "UNIQUE_SENT": [1.0, 0.0, 0.0], "GENERIC_SENT": [0.0, 1.0, 0.0],
            "SAME_CAT": [0.0, 1.0, 0.0], "CROSS_CAT": [0.0, 0.0, 1.0],
        }
        session = _FakeSession(vector_map=vector_map)
        result = annotate_uniqueness(candidates, ["SAME_CAT"], ["CROSS_CAT"], ruri_url="http://ruri:8000", session=session)
        by_text = {c["text"]: c for c in result}
        self.assertFalse(by_text["GENERIC_SENT"]["unique"])

    def test_empty_pool_yields_none_not_false(self):
        candidates = [{"text": "X"}]
        session = _FakeSession()
        result = annotate_uniqueness(candidates, [], [], ruri_url="http://ruri:8000", session=session)
        self.assertIsNone(result[0]["unique"])

    def test_empty_candidates(self):
        self.assertEqual(annotate_uniqueness([], ["a"], ["b"], ruri_url="http://ruri:8000", session=_FakeSession()), [])


class AnnotateSupportTest(unittest.TestCase):
    def test_marks_supported_from_entailment_judgments(self):
        candidates = [{"text": "支持文"}, {"text": "不支持文"}]
        judgments = [{"index": 1, "supported": True}, {"index": 2, "supported": False}]
        session = _FakeSession(entailment_judgments=judgments)
        result = annotate_support(candidates, "素材", session=session)
        self.assertEqual([c["supported"] for c in result["candidates"]], [True, False])

    def test_empty_candidates_skips_call(self):
        result = annotate_support([], "素材", session=_FakeSession())
        self.assertEqual(result, {"candidates": [], "call_meta": None})


class BuildFactCardsForAsinTest(unittest.TestCase):
    def test_end_to_end_selects_unique_and_supported_cards(self):
        raw = {
            "amazon": {"item": {"features": ["特徴A"]}},
            "experience": {"snippets": [{"aspect": "不満", "text": "不満点B"}]},
        }
        vector_map = {
            # same_category_max_sim: 抽出Aは同カテゴリ文と逆向き(負)、抽出Bは同じ向き(正)。
            # cross_sim は両方0になるよう直交させ、閾値(p95)を0にそろえる。
            "抽出A": [0.0, -1.0, 0.0], "抽出B": [0.0, 1.0, 0.0],
            "同カテゴリ文": [0.0, 1.0, 0.0], "別カテゴリ文": [0.0, 0.0, 1.0],
        }
        session = _FakeSession(
            extract_candidates=[{"source_index": 1, "fact_text": "抽出A"}, {"source_index": 2, "fact_text": "抽出B"}],
            entailment_judgments=[{"index": 1, "supported": True}, {"index": 2, "supported": True}],
            vector_map=vector_map,
        )
        result = build_fact_cards_for_asin(
            raw, ["同カテゴリ文"], ["別カテゴリ文"], "素材テキスト",
            ruri_url="http://ruri:8000", session=session,
        )
        self.assertEqual(result["atomic_item_count"], 2)
        self.assertEqual(result["extraction_candidate_count"], 2)
        # 抽出Aはsame_categoryと直交(unique)、抽出Bはsame_categoryと同じ向き(not unique)
        selected_texts = [c["text"] for c in result["selected_cards"]]
        self.assertEqual(selected_texts, ["抽出A"])
        self.assertTrue(result["is_thin"])  # 1件 < MIN_CARDS_NOT_THIN(3)

    def test_no_atomic_items_produces_no_cards(self):
        session = _FakeSession()
        result = build_fact_cards_for_asin({}, [], [], "素材", ruri_url="http://ruri:8000", session=session)
        self.assertEqual(result["selected_cards"], [])
        self.assertTrue(result["is_thin"])


if __name__ == "__main__":
    unittest.main()
