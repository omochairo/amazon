"""scripts/experimental/search_query_trial/asin_selection.py の単体テスト (#4841 V2)。"""
from __future__ import annotations

import json
import pathlib
import unittest

from scripts.experimental.search_query_trial.asin_selection import (
    find_candidates,
    load_v1_denominator,
    select_target_asins,
    select_v2_asins,
)


def _write_v1_results(tmp_path: pathlib.Path, asins: list[str]) -> pathlib.Path:
    path = tmp_path / "v1_results.json"
    path.write_text(json.dumps({"denominator": {"tried_asins_list": asins}}), encoding="utf-8")
    return path


def _write_article(articles_dir: pathlib.Path, asin: str, category: str, date: str = "2026-05-01") -> None:
    articles_dir.mkdir(parents=True, exist_ok=True)
    (articles_dir / f"{date}-{asin}.json").write_text(
        json.dumps({"product": {"edu_domains": [category]}}), encoding="utf-8",
    )


def _write_third_party(per_asin_dir: pathlib.Path, asin: str) -> None:
    d = per_asin_dir / asin
    d.mkdir(parents=True, exist_ok=True)
    (d / "third_party_sources.json").write_text(json.dumps({"sources": []}), encoding="utf-8")


class LoadV1DenominatorFuncTest(unittest.TestCase):
    def test_reads_tried_asins_list(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = _write_v1_results(pathlib.Path(d), ["B0000000AA", "B0000000BB"])
            self.assertEqual(load_v1_denominator(path), ["B0000000AA", "B0000000BB"])

    def test_missing_file_returns_empty(self):
        self.assertEqual(load_v1_denominator(pathlib.Path("/nonexistent/v1.json")), [])


class FindCandidatesTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = pathlib.Path(self._tmpdir.name)
        self.addCleanup(self._tmpdir.cleanup)
        self.articles_dir = self.tmp_path / "articles"
        self.per_asin_dir = self.tmp_path / "per_asin"

    def test_requires_article_and_third_party_sources(self):
        v1 = _write_v1_results(self.tmp_path, ["B0000000AA", "B0000000BB", "B0000000CC"])
        # AA: 両方あり -> 候補
        _write_article(self.articles_dir, "B0000000AA", "STEM")
        _write_third_party(self.per_asin_dir, "B0000000AA")
        # BB: 記事のみ -> 候補外
        _write_article(self.articles_dir, "B0000000BB", "STEM")
        # CC: third_party のみ -> 候補外
        _write_third_party(self.per_asin_dir, "B0000000CC")

        candidates = find_candidates(v1, self.per_asin_dir, self.articles_dir)
        self.assertEqual([c["asin"] for c in candidates], ["B0000000AA"])

    def test_candidate_carries_category(self):
        v1 = _write_v1_results(self.tmp_path, ["B0000000AA"])
        _write_article(self.articles_dir, "B0000000AA", "想像")
        _write_third_party(self.per_asin_dir, "B0000000AA")
        candidates = find_candidates(v1, self.per_asin_dir, self.articles_dir)
        self.assertEqual(candidates[0]["category"], "想像")

    def test_asin_outside_v1_denominator_is_excluded(self):
        v1 = _write_v1_results(self.tmp_path, ["B0000000AA"])
        _write_article(self.articles_dir, "B0000000ZZ", "STEM")
        _write_third_party(self.per_asin_dir, "B0000000ZZ")
        candidates = find_candidates(v1, self.per_asin_dir, self.articles_dir)
        self.assertEqual(candidates, [])


class SelectTargetAsinsTest(unittest.TestCase):
    def _candidates(self, n_per_category=5):
        out = []
        for cat in ("STEM", "想像", "運動"):
            for i in range(n_per_category):
                out.append({"asin": f"B{cat}{i:08d}"[:10], "category": cat})
        return out

    def test_deterministic_with_fixed_seed(self):
        candidates = self._candidates()
        a = select_target_asins(candidates, target_count=6, seed=1)
        b = select_target_asins(candidates, target_count=6, seed=1)
        self.assertEqual(a, b)

    def test_spreads_across_categories_when_possible(self):
        candidates = self._candidates()
        selected = select_target_asins(candidates, target_count=6, seed=1)
        categories = {c["category"] for c in selected}
        self.assertEqual(categories, {"STEM", "想像", "運動"})

    def test_stops_when_candidates_exhausted(self):
        candidates = self._candidates(n_per_category=1)
        selected = select_target_asins(candidates, target_count=20, seed=1)
        self.assertEqual(len(selected), 3)


class SelectV2AsinsIntegrationTest(unittest.TestCase):
    def test_reports_candidate_count_and_category_coverage(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            tmp_path = pathlib.Path(d)
            articles_dir = tmp_path / "articles"
            per_asin_dir = tmp_path / "per_asin"
            asins = []
            for i, cat in enumerate(["STEM", "想像", "運動"] * 3):
                asin = f"B{i:09d}"
                _write_article(articles_dir, asin, cat)
                _write_third_party(per_asin_dir, asin)
                asins.append(asin)
            v1 = _write_v1_results(tmp_path, asins)

            result = select_v2_asins(
                v1_results_path=v1, per_asin_dir=per_asin_dir, articles_dir=articles_dir,
                target_count=9, seed=1,
            )
            self.assertEqual(result["candidate_count"], 9)
            self.assertEqual(len(result["selected"]), 9)
            self.assertTrue(result["meets_min_categories"])


if __name__ == "__main__":
    unittest.main()
