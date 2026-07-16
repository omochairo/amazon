"""scripts/audit_uniqueness.py unit tests (#3203 Phase 3)."""
from __future__ import annotations

import json
import pathlib
import tempfile
import unittest
from unittest import mock

import requests

from scripts.audit_uniqueness import (
    build_uniqueness_text,
    cohort_for_slug,
    compute_centroid_similarities,
    compute_cohort_stats,
    compute_uniqueness_metrics,
    load_article_records,
    run,
    select_flagged,
    slug_date,
)
from scripts.compute_semantic_related import EmbeddingBatchError


def _write_article(dir_path: pathlib.Path, filename: str, data: dict | None = None) -> pathlib.Path:
    p = dir_path / filename
    p.write_text(json.dumps(data if data is not None else {"title": "テスト"}, ensure_ascii=False), encoding="utf-8")
    return p


def _mock_ruri_response(vectors: list[list[float]]):
    r = mock.Mock()
    r.raise_for_status = mock.Mock()
    r.json = mock.Mock(return_value={"vectors": vectors})
    return r


# --------------------------------------------------------------------------
# build_uniqueness_text
# --------------------------------------------------------------------------

class BuildUniquenessTextTest(unittest.TestCase):
    def test_concatenates_title_and_all_narrative_keys_including_how_to_choose(self):
        article = {
            "title": "タイトル",
            "narrative": {
                "lead": "リード文",
                "why_this_product": "選ぶ理由",
                "gift_appeal": "贈り物としての魅力",
                "daily_use": "日常での使い方",
                "safety_note": "安全に関する注意",
                "closing": "まとめ",
                "how_to_choose": "選び方の軸",
            },
            "tags": ["タグA", "タグB"],
        }
        text = build_uniqueness_text(article)
        for expected in ("タイトル", "リード文", "選ぶ理由", "贈り物としての魅力",
                          "日常での使い方", "安全に関する注意", "まとめ", "選び方の軸",
                          "タグ: タグA, タグB"):
            self.assertIn(expected, text)

    def test_narrative_section_as_array_is_joined(self):
        article = {"narrative": {"how_to_choose": ["観点1について", "観点2について"]}}
        text = build_uniqueness_text(article)
        self.assertIn("観点1について 観点2について", text)

    def test_missing_fields_do_not_crash(self):
        self.assertEqual(build_uniqueness_text({}), "")
        self.assertEqual(build_uniqueness_text(None), "")  # type: ignore[arg-type]
        self.assertEqual(build_uniqueness_text([1, 2, 3]), "")  # type: ignore[arg-type]

    def test_truncates_to_max_len(self):
        from scripts.audit_uniqueness import MAX_UNIQUENESS_TEXT_LEN
        article = {"title": "あ" * (MAX_UNIQUENESS_TEXT_LEN + 500)}
        text = build_uniqueness_text(article)
        self.assertEqual(len(text), MAX_UNIQUENESS_TEXT_LEN)

    def test_empty_narrative_values_skipped(self):
        article = {"title": "  ", "narrative": {"lead": "", "how_to_choose": []}}
        text = build_uniqueness_text(article)
        self.assertEqual(text, "")


# --------------------------------------------------------------------------
# cohort 判定
# --------------------------------------------------------------------------

class CohortForSlugTest(unittest.TestCase):
    def test_pre_v7_before_enforce_date(self):
        self.assertEqual(cohort_for_slug("2026-07-01-B0AAAAAAAA"), "pre_v7")

    def test_post_v7_on_enforce_date(self):
        self.assertEqual(cohort_for_slug("2026-07-16-B0AAAAAAAA"), "post_v7")

    def test_post_v7_after_enforce_date(self):
        self.assertEqual(cohort_for_slug("2026-08-01-B0AAAAAAAA"), "post_v7")

    def test_unparseable_slug_defaults_to_post_v7_safe_side(self):
        self.assertEqual(cohort_for_slug("not-a-slug"), "post_v7")
        self.assertEqual(cohort_for_slug(""), "post_v7")

    def test_slug_date_extracts_prefix(self):
        self.assertEqual(slug_date("2026-07-16-B0AAAAAAAA"), "2026-07-16")
        self.assertEqual(slug_date("garbage"), "")


# --------------------------------------------------------------------------
# compute_uniqueness_metrics
# --------------------------------------------------------------------------

class ComputeUniquenessMetricsTest(unittest.TestCase):
    def test_max_sim_nearest_and_mean_top5(self):
        asins = ["AAAAAAAAAA", "BBBBBBBBBB", "CCCCCCCCCC"]
        similarity = [
            [1.0, 0.9, 0.5],
            [0.9, 1.0, 0.4],
            [0.5, 0.4, 1.0],
        ]
        result = compute_uniqueness_metrics(asins, similarity, top_n=5)
        self.assertEqual(result["AAAAAAAAAA"]["max_sim"], 0.9)
        self.assertEqual(result["AAAAAAAAAA"]["nearest_asin"], "BBBBBBBBBB")
        # top5 だが候補は2件のみ -> 平均 (0.9+0.5)/2 = 0.7
        self.assertEqual(result["AAAAAAAAAA"]["mean_top5_sim"], 0.7)

    def test_single_article_corpus_has_no_neighbors(self):
        result = compute_uniqueness_metrics(["AAAAAAAAAA"], [[1.0]])
        self.assertEqual(result["AAAAAAAAAA"], {"max_sim": 0.0, "nearest_asin": None, "mean_top5_sim": 0.0})

    def test_tie_break_by_asin_ascending(self):
        asins = ["ZZZZZZZZZZ", "AAAAAAAAAA", "MMMMMMMMMM"]
        similarity = [
            [1.0, 0.8, 0.8],
            [0.8, 1.0, 0.8],
            [0.8, 0.8, 1.0],
        ]
        result = compute_uniqueness_metrics(asins, similarity)
        self.assertEqual(result["ZZZZZZZZZZ"]["nearest_asin"], "AAAAAAAAAA")


# --------------------------------------------------------------------------
# compute_centroid_similarities
# --------------------------------------------------------------------------

class ComputeCentroidSimilaritiesTest(unittest.TestCase):
    def test_identical_vectors_have_centroid_sim_one(self):
        vectors = [[1.0, 0.0], [1.0, 0.0], [1.0, 0.0]]
        sims = compute_centroid_similarities(vectors)
        for s in sims:
            self.assertAlmostEqual(s, 1.0, places=6)

    def test_orthogonal_pair_centroid_sim_symmetric(self):
        vectors = [[1.0, 0.0], [0.0, 1.0]]
        sims = compute_centroid_similarities(vectors)
        self.assertAlmostEqual(sims[0], sims[1], places=6)
        self.assertAlmostEqual(sims[0], 0.7071067811865476, places=6)

    def test_empty_input_returns_empty(self):
        self.assertEqual(compute_centroid_similarities([]), [])

    def test_numpy_and_pure_python_agree(self):
        import sys
        vectors = [[1.0, 2.0, 3.0], [3.0, 2.0, 1.0], [0.5, 0.5, 0.5], [2.0, 0.0, 1.0]]
        sims_numpy = compute_centroid_similarities(vectors)
        with mock.patch.dict(sys.modules, {"numpy": None}):
            sims_pure = compute_centroid_similarities(vectors)
        for a, b in zip(sims_numpy, sims_pure):
            self.assertAlmostEqual(a, b, places=9)


# --------------------------------------------------------------------------
# compute_cohort_stats / select_flagged
# --------------------------------------------------------------------------

class CohortStatsAndFlaggedTest(unittest.TestCase):
    def test_cohort_stats_counts_and_percentiles(self):
        entries = [
            {"asin": "A", "cohort": "pre_v7", "max_sim": 0.9, "centroid_sim": 0.8},
            {"asin": "B", "cohort": "pre_v7", "max_sim": 0.8, "centroid_sim": 0.7},
            {"asin": "C", "cohort": "post_v7", "max_sim": 0.5, "centroid_sim": 0.4},
        ]
        stats = compute_cohort_stats(entries)
        self.assertEqual(stats["pre_v7"]["count"], 2)
        self.assertEqual(stats["post_v7"]["count"], 1)
        self.assertIsNotNone(stats["pre_v7"]["max_sim_p50"])

    def test_cohort_with_no_entries_returns_none_stats(self):
        entries = [{"asin": "A", "cohort": "post_v7", "max_sim": 0.5, "centroid_sim": 0.4}]
        stats = compute_cohort_stats(entries)
        self.assertEqual(stats["pre_v7"]["count"], 0)
        self.assertIsNone(stats["pre_v7"]["max_sim_p50"])

    def test_select_flagged_filters_by_threshold_and_sorts_desc(self):
        entries = [
            {"asin": "A", "cohort": "post_v7", "max_sim": 0.96, "centroid_sim": 0.5},
            {"asin": "B", "cohort": "post_v7", "max_sim": 0.99, "centroid_sim": 0.5},
            {"asin": "C", "cohort": "post_v7", "max_sim": 0.10, "centroid_sim": 0.5},
        ]
        flagged = select_flagged(entries, max_sim_threshold=0.95, centroid_threshold=0.90, top_flagged=30)
        self.assertEqual([e["asin"] for e in flagged], ["B", "A"])
        self.assertIn("max_sim>0.95", flagged[0]["reasons"])

    def test_select_flagged_respects_top_flagged_cap(self):
        entries = [
            {"asin": f"A{i}", "cohort": "post_v7", "max_sim": 0.96 + i * 0.001, "centroid_sim": 0.5}
            for i in range(5)
        ]
        flagged = select_flagged(entries, max_sim_threshold=0.95, centroid_threshold=0.90, top_flagged=2)
        self.assertEqual(len(flagged), 2)

    def test_select_flagged_centroid_only_reason(self):
        entries = [{"asin": "A", "cohort": "post_v7", "max_sim": 0.1, "centroid_sim": 0.95}]
        flagged = select_flagged(entries, max_sim_threshold=0.95, centroid_threshold=0.90, top_flagged=30)
        self.assertEqual(flagged[0]["reasons"], ["centroid_sim>0.9"])


# --------------------------------------------------------------------------
# load_article_records
# --------------------------------------------------------------------------

class LoadArticleRecordsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.articles_dir = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_uses_slug_field_when_present(self):
        _write_article(self.articles_dir, "2026-05-01-B0AAAAAAAA.json",
                        {"slug": "2026-05-01-B0AAAAAAAA", "title": "商品A"})
        records = load_article_records(self.articles_dir)
        self.assertEqual(records[0]["slug"], "2026-05-01-B0AAAAAAAA")

    def test_falls_back_to_filename_stem_when_slug_missing(self):
        _write_article(self.articles_dir, "2026-05-01-B0AAAAAAAA.json", {"title": "商品A"})
        records = load_article_records(self.articles_dir)
        self.assertEqual(records[0]["slug"], "2026-05-01-B0AAAAAAAA")

    def test_limit_caps_records(self):
        _write_article(self.articles_dir, "2026-05-01-B0AAAAAAAA.json", {"title": "A"})
        _write_article(self.articles_dir, "2026-05-01-B0BBBBBBBB.json", {"title": "B"})
        records = load_article_records(self.articles_dir, limit=1)
        self.assertEqual(len(records), 1)


# --------------------------------------------------------------------------
# run() E2E
# --------------------------------------------------------------------------

class RunEndToEndTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.articles_dir = self.root / "articles"
        self.articles_dir.mkdir()
        self.out_path = self.root / "out" / "uniqueness_audit.json"

    def tearDown(self):
        self._tmp.cleanup()

    def _write_articles(self):
        _write_article(self.articles_dir, "2026-06-01-B0AAAAAAAA.json", {
            "slug": "2026-06-01-B0AAAAAAAA", "title": "商品A", "narrative": {"lead": "リードA"},
        })
        _write_article(self.articles_dir, "2026-07-20-B0BBBBBBBB.json", {
            "slug": "2026-07-20-B0BBBBBBBB", "title": "商品B", "narrative": {"lead": "リードB"},
        })
        _write_article(self.articles_dir, "2026-07-21-B0CCCCCCCC.json", {
            "slug": "2026-07-21-B0CCCCCCCC", "title": "商品C", "narrative": {"lead": "リードC"},
        })

    def test_output_shape_and_cohort_split(self):
        self._write_articles()
        embeddings = [[1.0, 0.0], [0.99, 0.14], [0.0, 1.0]]
        session = mock.Mock()
        session.post.return_value = _mock_ruri_response(embeddings)

        summary = run(
            self.articles_dir, self.out_path,
            backend="ruri", ruri_url="http://fake-ruri:8000",
            session=session, sleeper=lambda _s: None,
        )
        self.assertTrue(summary["written"])
        data = json.loads(self.out_path.read_text(encoding="utf-8"))
        self.assertEqual(data["corpus_size"], 3)
        self.assertIn("cohort_stats", data)
        self.assertEqual(data["cohort_stats"]["pre_v7"]["count"], 1)
        self.assertEqual(data["cohort_stats"]["post_v7"]["count"], 2)
        self.assertIn("flagged", data)
        self.assertIn("generated_at", data)
        self.assertIn("source_week", data)

    def test_dry_run_style_failure_aborts_without_writing(self):
        self._write_articles()
        session = mock.Mock()
        session.post.side_effect = requests.ConnectionError("boom")
        with self.assertRaises(EmbeddingBatchError):
            run(
                self.articles_dir, self.out_path,
                backend="ruri", ruri_url="http://fake-ruri:8000",
                session=session, sleeper=lambda _s: None,
            )
        self.assertFalse(self.out_path.exists())

    def test_no_articles_does_not_call_network(self):
        session = mock.Mock()
        summary = run(
            self.articles_dir, self.out_path,
            backend="ruri", session=session, sleeper=lambda _s: None,
        )
        self.assertFalse(summary["written"])
        session.post.assert_not_called()

    def test_unknown_backend_raises_value_error(self):
        self._write_articles()
        session = mock.Mock()
        with self.assertRaises(ValueError):
            run(
                self.articles_dir, self.out_path,
                backend="unknown", session=session, sleeper=lambda _s: None,
            )


if __name__ == "__main__":
    unittest.main()
