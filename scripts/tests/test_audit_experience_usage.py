"""scripts/audit_experience_usage.py unit tests (#4841 T1)。

カバレッジ:
1. build_paragraph_map: string/array narrativeSection・editorial_comment・欠損耐性
2. load_experience_records: 正常系・壊れたファイルの読み飛ばし
3. select_population: 母集団選定の全除外理由 (zero_snippets / no_article /
   invalid_generated_at / invalid_article_date / article_older_than_material /
   no_paragraphs) と included の日付条件
4. build_snippet_pool / sample_negative_snippet: 同一 aspect・別 ASIN の抽出、
   固定 seed での再現性、候補無しで None
5. percentile / distribution_stats / histogram_overlap / _rate_stats: 集計の pure function
6. run(): Ruri をモックした E2E (query/document の kind 分離、閾値、記事/snippet 単位の
   集計、出力 JSON の形)
"""
from __future__ import annotations

import json
import pathlib
import tempfile
import unittest

from scripts.audit_experience_usage import (
    build_paragraph_map,
    build_snippet_pool,
    cosine_similarity,
    date_boundary_within_24h,
    distribution_stats,
    histogram_overlap,
    load_experience_records,
    percentile,
    run,
    sample_negative_snippet,
    select_population,
    select_same_asin_control,
    _diff_rate,
    _rate_stats,
)


def _write(path: pathlib.Path, data) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# build_paragraph_map
# --------------------------------------------------------------------------

class BuildParagraphMapTest(unittest.TestCase):
    def test_collects_narrative_keys_and_editorial_comment(self):
        article = {
            "narrative": {
                "lead": "リード文",
                "why_this_product": ["理由1", "理由2"],
                "how_to_choose": "選び方",
            },
            "editorial_comment": "編集後記",
        }
        paragraphs = build_paragraph_map(article)
        self.assertEqual(paragraphs["lead"], "リード文")
        self.assertEqual(paragraphs["why_this_product"], "理由1 理由2")
        self.assertEqual(paragraphs["how_to_choose"], "選び方")
        self.assertEqual(paragraphs["editorial_comment"], "編集後記")

    def test_empty_sections_are_omitted(self):
        article = {"narrative": {"lead": "", "gift_appeal": []}, "editorial_comment": "  "}
        self.assertEqual(build_paragraph_map(article), {})

    def test_missing_fields_do_not_crash(self):
        self.assertEqual(build_paragraph_map({}), {})
        self.assertEqual(build_paragraph_map(None), {})  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# load_experience_records
# --------------------------------------------------------------------------

class LoadExperienceRecordsTest(unittest.TestCase):
    def test_reads_all_valid_files_sorted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            _write(root / "B0000000ZZ/experience.json", {
                "asin": "B0000000ZZ", "generated_at": "2026-01-01T00:00:00Z",
                "snippets": [{"aspect": "体験談", "text": "t", "source_type": "blog"}],
            })
            _write(root / "B0000000AA/experience.json", {
                "asin": "B0000000AA", "generated_at": "2026-01-02T00:00:00Z", "snippets": [],
            })
            records = load_experience_records(str(root / "*/experience.json"))
            self.assertEqual([r["asin"] for r in records], ["B0000000AA", "B0000000ZZ"])

    def test_skips_broken_json_without_raising(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            p = root / "B0000000ZZ/experience.json"
            p.parent.mkdir(parents=True)
            p.write_text("{not json", encoding="utf-8")
            self.assertEqual(load_experience_records(str(root / "*/experience.json")), [])


# --------------------------------------------------------------------------
# select_population
# --------------------------------------------------------------------------

class SelectPopulationTest(unittest.TestCase):
    def _articles_dir(self, tmp: pathlib.Path, files: dict) -> pathlib.Path:
        d = tmp / "articles"
        d.mkdir()
        for name, data in files.items():
            (d / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return d

    def test_included_when_article_newer_than_material(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            articles_dir = self._articles_dir(tmp, {
                "2026-06-01-B0000000AA.json": {
                    "date": "2026-06-01T00:00:00Z",
                    "narrative": {"lead": "リード"},
                },
            })
            records = [{
                "asin": "B0000000AA", "generated_at": "2026-05-01T00:00:00Z",
                "snippets": [{"aspect": "体験談", "text": "t", "source_type": "blog"}],
            }]
            included, excluded = select_population(records, articles_dir)
            self.assertEqual(excluded, [])
            self.assertEqual(len(included), 1)
            self.assertEqual(included[0]["asin"], "B0000000AA")
            self.assertEqual(included[0]["paragraphs"], {"lead": "リード"})

    def test_excludes_zero_snippets(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            articles_dir = self._articles_dir(tmp, {})
            records = [{"asin": "B0000000AA", "generated_at": "2026-05-01T00:00:00Z", "snippets": []}]
            included, excluded = select_population(records, articles_dir)
            self.assertEqual(included, [])
            self.assertEqual(excluded, [{"asin": "B0000000AA", "reason": "zero_snippets"}])

    def test_excludes_no_article(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            articles_dir = self._articles_dir(tmp, {})
            records = [{
                "asin": "B0000000AA", "generated_at": "2026-05-01T00:00:00Z",
                "snippets": [{"aspect": "体験談", "text": "t", "source_type": "blog"}],
            }]
            _, excluded = select_population(records, articles_dir)
            self.assertEqual(excluded, [{"asin": "B0000000AA", "reason": "no_article"}])

    def test_excludes_invalid_generated_at(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            articles_dir = self._articles_dir(tmp, {})
            records = [{
                "asin": "B0000000AA", "generated_at": "not-a-date",
                "snippets": [{"aspect": "体験談", "text": "t", "source_type": "blog"}],
            }]
            _, excluded = select_population(records, articles_dir)
            self.assertEqual(excluded, [{"asin": "B0000000AA", "reason": "invalid_generated_at"}])

    def test_excludes_article_older_than_material(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            articles_dir = self._articles_dir(tmp, {
                "2026-04-01-B0000000AA.json": {
                    "date": "2026-04-01T00:00:00Z", "narrative": {"lead": "リード"},
                },
            })
            records = [{
                "asin": "B0000000AA", "generated_at": "2026-05-01T00:00:00Z",
                "snippets": [{"aspect": "体験談", "text": "t", "source_type": "blog"}],
            }]
            _, excluded = select_population(records, articles_dir)
            self.assertEqual(excluded, [{"asin": "B0000000AA", "reason": "article_older_than_material"}])

    def test_excludes_no_paragraphs(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            articles_dir = self._articles_dir(tmp, {
                "2026-06-01-B0000000AA.json": {"date": "2026-06-01T00:00:00Z"},
            })
            records = [{
                "asin": "B0000000AA", "generated_at": "2026-05-01T00:00:00Z",
                "snippets": [{"aspect": "体験談", "text": "t", "source_type": "blog"}],
            }]
            _, excluded = select_population(records, articles_dir)
            self.assertEqual(excluded, [{"asin": "B0000000AA", "reason": "no_paragraphs"}])


# --------------------------------------------------------------------------
# select_same_asin_control (R1) / date_boundary_within_24h (R4)
# --------------------------------------------------------------------------

class SelectSameAsinControlTest(unittest.TestCase):
    def _articles_dir(self, tmp: pathlib.Path, files: dict) -> pathlib.Path:
        d = tmp / "articles"
        d.mkdir()
        for name, data in files.items():
            (d / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return d

    def test_includes_article_older_than_material_with_boundary_hours(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            articles_dir = self._articles_dir(tmp, {
                "2026-04-01-B0000000AA.json": {
                    "date": "2026-04-01T00:00:00Z", "narrative": {"closing": "旧記事の段落"},
                },
            })
            records = [{
                "asin": "B0000000AA", "generated_at": "2026-04-03T12:00:00Z",
                "snippets": [{"aspect": "不満", "text": "t", "source_type": "blog"}],
            }]
            control = select_same_asin_control(records, articles_dir)
            self.assertEqual(len(control), 1)
            self.assertEqual(control[0]["asin"], "B0000000AA")
            self.assertEqual(control[0]["paragraphs"], {"closing": "旧記事の段落"})
            self.assertEqual(control[0]["boundary_hours"], 60.0)

    def test_excludes_article_newer_than_material(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            articles_dir = self._articles_dir(tmp, {
                "2026-06-01-B0000000AA.json": {
                    "date": "2026-06-01T00:00:00Z", "narrative": {"lead": "新しい記事"},
                },
            })
            records = [{
                "asin": "B0000000AA", "generated_at": "2026-05-01T00:00:00Z",
                "snippets": [{"aspect": "体験談", "text": "t", "source_type": "blog"}],
            }]
            self.assertEqual(select_same_asin_control(records, articles_dir), [])

    def test_excludes_when_no_article_or_no_snippets_or_no_paragraphs(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            articles_dir = self._articles_dir(tmp, {
                "2026-04-01-B0000000BB.json": {"date": "2026-04-01T00:00:00Z"},
            })
            records = [
                {"asin": "B0000000AA", "generated_at": "2026-05-01T00:00:00Z", "snippets": []},
                {
                    "asin": "B0000000BB", "generated_at": "2026-05-01T00:00:00Z",
                    "snippets": [{"aspect": "安全", "text": "t", "source_type": "blog"}],
                },
            ]
            # AA: 記事が無い、BB: 記事はあるが narrative が空 (no_paragraphs 相当)
            self.assertEqual(select_same_asin_control(records, articles_dir), [])


class DateBoundaryWithin24hTest(unittest.TestCase):
    def test_flags_included_and_control_items_within_24h(self):
        included = [{
            "asin": "B0000000AA", "generated_at": "2026-05-01T00:00:00Z",
            "article_date": "2026-05-01T10:00:00Z",  # 10時間差 → 境界
        }]
        control = [
            {"asin": "B0000000BB", "boundary_hours": 12.0},  # 境界
            {"asin": "B0000000CC", "boundary_hours": 720.0},  # 境界ではない
        ]
        result = date_boundary_within_24h(included, control)
        self.assertEqual(result["count"], 2)
        self.assertEqual(
            {(i["asin"], i["group"]) for i in result["items"]},
            {("B0000000AA", "included"), ("B0000000BB", "same_asin_control")},
        )

    def test_no_items_within_24h(self):
        included = [{
            "asin": "B0000000AA", "generated_at": "2026-05-01T00:00:00Z",
            "article_date": "2026-06-01T00:00:00Z",
        }]
        control = [{"asin": "B0000000BB", "boundary_hours": 720.0}]
        self.assertEqual(date_boundary_within_24h(included, control), {"count": 0, "items": []})


class DiffRateTest(unittest.TestCase):
    def test_subtracts_control_rate_from_included_rate(self):
        self.assertEqual(_diff_rate({"rate": 0.797}, {"rate": 0.421}), round(0.797 - 0.421, 4))

    def test_none_when_either_side_missing_or_unrated(self):
        self.assertIsNone(_diff_rate(None, {"rate": 0.5}))
        self.assertIsNone(_diff_rate({"rate": 0.5}, None))
        self.assertIsNone(_diff_rate({"rate": None}, {"rate": 0.5}))
        self.assertIsNone(_diff_rate({"rate": 0.5}, {"rate": None}))


# --------------------------------------------------------------------------
# 負の対照サンプリング
# --------------------------------------------------------------------------

class NegativeControlTest(unittest.TestCase):
    def _pool(self):
        return build_snippet_pool([
            {"asin": "AAA", "snippets": [{"aspect": "不満", "text": "a1", "source_type": "blog"}]},
            {"asin": "BBB", "snippets": [{"aspect": "不満", "text": "b1", "source_type": "blog"}]},
            {"asin": "BBB", "snippets": [{"aspect": "安全", "text": "b2", "source_type": "blog"}]},
        ])

    def test_excludes_same_asin_and_matches_aspect(self):
        pool = self._pool()
        import random
        rng = random.Random(1)
        picked = sample_negative_snippet(pool, "不満", "AAA", rng)
        self.assertIsNotNone(picked)
        self.assertEqual(picked["asin"], "BBB")
        self.assertEqual(picked["aspect"], "不満")

    def test_none_when_no_cross_asin_candidate(self):
        pool = build_snippet_pool([
            {"asin": "AAA", "snippets": [{"aspect": "不満", "text": "a1", "source_type": "blog"}]},
        ])
        import random
        self.assertIsNone(sample_negative_snippet(pool, "不満", "AAA", random.Random(1)))

    def test_deterministic_given_fixed_seed(self):
        pool = self._pool()
        import random
        a = sample_negative_snippet(pool, "不満", "AAA", random.Random(42))
        b = sample_negative_snippet(pool, "不満", "AAA", random.Random(42))
        self.assertEqual(a, b)


# --------------------------------------------------------------------------
# 集計 pure function
# --------------------------------------------------------------------------

class AggregationTest(unittest.TestCase):
    def test_percentile_known_values(self):
        self.assertEqual(percentile([1, 2, 3, 4, 5], 50), 3)
        self.assertIsNone(percentile([], 50))

    def test_distribution_stats_shape(self):
        stats = distribution_stats([0.1, 0.5, 0.9])
        self.assertEqual(stats["count"], 3)
        self.assertIn("p50", stats)
        self.assertIn("p95", stats)

    def test_histogram_overlap_identical_distributions_is_high(self):
        a = [0.1, 0.2, 0.3, 0.4, 0.5]
        self.assertGreater(histogram_overlap(a, list(a)), 0.9)

    def test_histogram_overlap_disjoint_distributions_is_zero(self):
        a = [0.0, 0.01, 0.02]
        b = [0.9, 0.91, 0.92]
        self.assertEqual(histogram_overlap(a, b), 0.0)

    def test_histogram_overlap_empty_is_none(self):
        self.assertIsNone(histogram_overlap([], [0.1]))

    def test_cosine_similarity(self):
        self.assertAlmostEqual(cosine_similarity([1, 0], [1, 0]), 1.0)
        self.assertAlmostEqual(cosine_similarity([1, 0], [0, 1]), 0.0)

    def test_rate_stats(self):
        items = [{"used": True}, {"used": False}, {"used": True}]
        stats = _rate_stats(items)
        self.assertEqual(stats, {"total": 3, "used": 2, "rate": round(2 / 3, 4)})


# --------------------------------------------------------------------------
# run() E2E (Ruri をモック)
# --------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeSession:
    """text -> vector の固定辞書で /embed・/health を返す最小セッション。

    直交ベクトルを使うことで、負の対照としてどの ASIN が選ばれても
    (rng.choice の結果に関わらず) 類似度が 0 になるよう設計している
    (テストを乱数の内部実装に依存させないため)。
    """

    VECTORS = {
        "A_PARA": [1.0, 0.0, 0.0, 0.0],
        "B_PARA": [0.0, 1.0, 0.0, 0.0],
        "A_SNIPPET": [1.0, 0.0, 0.0, 0.0],
        "B_SNIPPET": [0.0, 1.0, 0.0, 0.0],
        "C_SNIPPET": [0.0, 0.0, 1.0, 0.0],
        # D: article_older_than_material の同一 ASIN 対照 (R1)。D_PARA と
        # D_SNIPPET を同じ向きにして「同じ商品の話をしている (=類似度1)」を
        # 再現する — この記事は素材より前に書かれているので、この一致は
        # 定義上「使った」ではありえない。
        "D_PARA": [0.0, 0.0, 0.0, 1.0],
        "D_SNIPPET": [0.0, 0.0, 0.0, 1.0],
    }

    def __init__(self):
        self.calls: list[tuple[str, list[str]]] = []

    def get(self, url, timeout=None):
        return _FakeResp({"status": "ok", "embed_model": "cl-nagoya/ruri-v3-310m"})

    def post(self, url, json=None, timeout=None):
        payload = json or {}
        texts = payload.get("texts", [])
        kind = payload.get("kind")
        self.calls.append((kind, list(texts)))
        return _FakeResp({"vectors": [self.VECTORS[t] for t in texts]})


class RunEndToEndTest(unittest.TestCase):
    def _setup_repo(self, tmp: pathlib.Path):
        _write(tmp / "raw/B0000000AA/experience.json", {
            "asin": "B0000000AA", "generated_at": "2026-05-01T00:00:00Z",
            "snippets": [{"aspect": "不満", "text": "A_SNIPPET", "source_type": "antigravity",
                          "usable_as": "paraphrase", "confidence": "high"}],
        })
        _write(tmp / "raw/B0000000BB/experience.json", {
            "asin": "B0000000BB", "generated_at": "2026-05-01T00:00:00Z",
            "snippets": [{"aspect": "不満", "text": "B_SNIPPET", "source_type": "antigravity",
                          "usable_as": "paraphrase", "confidence": "high"}],
        })
        # C: 記事が無く母集団には入らないが、負の対照プールには入る
        _write(tmp / "raw/B0000000CC/experience.json", {
            "asin": "B0000000CC", "generated_at": "2026-05-01T00:00:00Z",
            "snippets": [{"aspect": "不満", "text": "C_SNIPPET", "source_type": "blog",
                          "usable_as": "paraphrase", "confidence": "high"}],
        })
        # D: 記事が素材 (generated_at) より前に書かれている → article_older_
        # than_material で除外され、R1 の同一 ASIN 対照になる
        _write(tmp / "raw/B0000000DD/experience.json", {
            "asin": "B0000000DD", "generated_at": "2026-05-01T00:00:00Z",
            "snippets": [{"aspect": "不満", "text": "D_SNIPPET", "source_type": "blog",
                          "usable_as": "paraphrase", "confidence": "high"}],
        })
        _write(tmp / "articles/2026-06-01-B0000000AA.json", {
            "date": "2026-06-01T00:00:00Z", "narrative": {"closing": "A_PARA"},
        })
        _write(tmp / "articles/2026-06-01-B0000000BB.json", {
            "date": "2026-06-01T00:00:00Z", "narrative": {"closing": "B_PARA"},
        })
        _write(tmp / "articles/2026-04-01-B0000000DD.json", {
            "date": "2026-04-01T00:00:00Z", "narrative": {"closing": "D_PARA"},
        })

    def test_full_pipeline_with_orthogonal_vectors(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            self._setup_repo(tmp)
            session = _FakeSession()
            out_path = tmp / "out.json"

            result = run(
                experience_glob=str(tmp / "raw/*/experience.json"),
                articles_dir=str(tmp / "articles"),
                out_path=out_path,
                ruri_url="http://ruri:8000",
                session=session,
                sleeper=lambda s: None,
            )

            payload = result["payload"]
            self.assertEqual(payload["embed_model"], "cl-nagoya/ruri-v3-310m")
            pop = payload["population"]
            self.assertEqual(pop["included"], 2)
            self.assertEqual(sorted(pop["included_asins"]), ["B0000000AA", "B0000000BB"])
            self.assertEqual(sorted(pop["excluded"], key=lambda e: e["asin"]), [
                {"asin": "B0000000CC", "reason": "no_article"},
                {"asin": "B0000000DD", "reason": "article_older_than_material"},
            ])
            # R1: article_older_than_material の DD が同一 ASIN 対照になる
            self.assertEqual(pop["same_asin_control_total"], 1)
            self.assertEqual(pop["same_asin_control_asins"], ["B0000000DD"])
            # R4: DD の generated_at と article_date は約1ヶ月差なので境界には当たらない
            self.assertEqual(pop["date_boundary_within_24h"], {"count": 0, "items": []})

            # 直交ベクトルなので負の対照は必ず 0、正例は自分自身の段落と一致するので 1
            self.assertEqual(payload["threshold"]["negative_distribution"]["count"], 2)
            self.assertEqual(payload["threshold"]["value"], 0.0)
            self.assertEqual(payload["threshold"]["positive_distribution"]["p50"], 1.0)

            self.assertEqual(payload["article_level"]["articles_with_used_snippet"], 2)
            self.assertEqual(payload["article_level"]["fraction"], 1.0)
            self.assertEqual(payload["snippet_level"]["overall"], {"total": 2, "used": 2, "rate": 1.0})
            self.assertEqual(payload["snippet_level"]["by_aspect"]["不満"]["rate"], 1.0)

            # R1: DD (同一ASIN対照) は D_PARA と D_SNIPPET を同じ向きにしてあるので
            # 元の閾値 (0.0) を超え、「同じ商品の話」だけでも100%に見えてしまう
            # ことを再現している。included と対照が両方100%なので、真に使った
            # ことの証拠としての差は0になる
            same_asin_control = payload["same_asin_control"]
            self.assertEqual(same_asin_control["total_articles"], 1)
            self.assertEqual(same_asin_control["asins"], ["B0000000DD"])
            self.assertEqual(same_asin_control["snippet_level"]["overall"], {"total": 1, "used": 1, "rate": 1.0})
            self.assertEqual(payload["diff"]["overall"], 0.0)
            self.assertEqual(payload["diff"]["by_aspect"]["不満"], 0.0)

            # R1: 対照自身の p95 (=1.0) を閾値にすると、included の一致 (max_sim=1.0)
            # はもう閾値を超えず (>であって>=ではない)、使用率は0まで下がる
            alt = payload["alt_threshold_from_control"]
            self.assertEqual(alt["value"], 1.0)
            self.assertEqual(alt["snippet_level"]["overall"], {"total": 2, "used": 0, "rate": 0.0})
            self.assertEqual(alt["article_level"]["fraction"], 0.0)

            self.assertEqual(len(payload["samples"]["above_threshold"]), 2)
            self.assertEqual(payload["samples"]["below_threshold"], [])

            # kind の使い分け (document は段落、query は snippet 側) を確認
            kinds_called = {kind for kind, _ in session.calls}
            self.assertEqual(kinds_called, {"document", "query"})

            self.assertTrue(out_path.exists())
            written = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertEqual(written["population"]["included"], 2)

    def test_writes_empty_payload_when_no_included_articles(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            (tmp / "articles").mkdir()
            _write(tmp / "raw/B0000000ZZ/experience.json", {
                "asin": "B0000000ZZ", "generated_at": "2026-05-01T00:00:00Z", "snippets": [],
            })
            out_path = tmp / "out.json"
            result = run(
                experience_glob=str(tmp / "raw/*/experience.json"),
                articles_dir=str(tmp / "articles"),
                out_path=out_path,
                session=_FakeSession(),
            )
            self.assertEqual(result["included"], 0)
            self.assertIsNone(result["payload"]["threshold"])
            self.assertTrue(out_path.exists())


if __name__ == "__main__":
    unittest.main()
