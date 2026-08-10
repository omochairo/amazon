"""Unit tests for fetch_amazon_suggest (#2686 PR-B)。

カバレッジ:
1. シード選定: brand_taxonomy.yaml canonical (exclude_from_taxonomy 除外・term_slugs.yaml
   読み取り専用参照 + fallback)、suggest_info_seeds.yaml のテーマ (label+expand_modifier)、
   --seeds brands/themes/all の切り替え
2. select_targets (未取得優先・fetched_at 昇順・min-age-days・limit cap)
3. レスポンスパース (_extract_keyword_values): type!="KEYWORD" 除外・rank 付与
4. マージ (_rank_and_merge): 末尾スペース prefix の結果マージ、重複時は最小 rank
5. 関連性フィルタ (_is_relevant): seed 非包含候補を落とす、空白揺れ・大小文字違いは落とさない
6. ブランド seed だけにカテゴリ語つき prefix ("<seed> おもちゃ") が追加されること
7. run(): alias の既定値が aps であること、HTTP エラーで打ち切り + 部分成果保存、
   --max-requests 上限、--min-age-days による skip、dry-run が書き込みしないこと、
   dropped_irrelevant が出力に載ること

ネットワークには一切出ない (requests.Session をモックする)。偽装レスポンスは実測した
実際のレスポンス構造 (suggType/type/value/refTag/candidateSources/strategyId/
strategyApiType/prior/ghost/help) に合わせる。
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import requests

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.dirname(THIS_DIR)
REPO_ROOT = os.path.dirname(SCRIPTS_DIR)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import fetch_amazon_suggest as F  # noqa: E402
from scripts.term_slug import TermSlugMap  # noqa: E402


def _write_taxonomy(path: pathlib.Path, brands: list[dict]) -> None:
    import yaml
    path.write_text(yaml.safe_dump({"version": 1, "brands": brands}, allow_unicode=True), encoding="utf-8")


def _write_seeds_file(path: pathlib.Path, themes: list[dict], modifiers=None, expand_modifier="おもちゃ") -> None:
    import yaml
    path.write_text(
        yaml.safe_dump({
            "themes": themes,
            "modifiers": modifiers or ["おもちゃ"],
            "expand_modifier": expand_modifier,
        }, allow_unicode=True),
        encoding="utf-8",
    )


def _amazon_payload(values_with_types: list[tuple[str, str]]) -> dict:
    """[(value, type), ...] から実測レスポンス形状の dict を作る。"""
    return {
        "alias": "aps",
        "prefix": "",
        "suffix": "",
        "suggestions": [
            {
                "suggType": "KeywordSuggestion",
                "type": t,
                "value": v,
                "refTag": "nb_sb_ss_ts-doa-p_1_5",
                "candidateSources": "local",
                "strategyId": "ts-doa-p",
                "strategyApiType": "RANK",
                "prior": 0.0,
                "ghost": False,
                "help": False,
            }
            for v, t in values_with_types
        ],
    }


def _resp(status: int = 200, payload: dict | None = None, text: str | None = None):
    r = mock.Mock()
    r.status_code = status
    if text is not None:
        r.text = text
    else:
        r.text = json.dumps(payload if payload is not None else _amazon_payload([]))
    return r


class LoadBrandSeedCandidatesTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_excludes_exclude_from_taxonomy(self):
        taxonomy_path = self.root / "brand_taxonomy.yaml"
        _write_taxonomy(taxonomy_path, [
            {"canonical": "バンダイ"},
            {"canonical": "ノーブランド", "exclude_from_taxonomy": True},
        ])
        slugs = self.root / "term_slugs.yaml"
        slugs.write_text("バンダイ: bandai\n", encoding="utf-8")
        slug_map = TermSlugMap(path=slugs)
        out = F.load_brand_seed_candidates(taxonomy_path, slug_map=slug_map)
        self.assertEqual(out, [("バンダイ", "bandai")])

    def test_sorted_by_canonical(self):
        taxonomy_path = self.root / "brand_taxonomy.yaml"
        _write_taxonomy(taxonomy_path, [
            {"canonical": "レゴ"},
            {"canonical": "タカラトミー"},
        ])
        slugs = self.root / "term_slugs.yaml"
        slugs.write_text("レゴ: lego\nタカラトミー: takara-tomy\n", encoding="utf-8")
        slug_map = TermSlugMap(path=slugs)
        out = F.load_brand_seed_candidates(taxonomy_path, slug_map=slug_map)
        self.assertEqual([s for s, _ in out], ["タカラトミー", "レゴ"])

    def test_missing_slug_uses_fallback_without_writing(self):
        taxonomy_path = self.root / "brand_taxonomy.yaml"
        _write_taxonomy(taxonomy_path, [{"canonical": "未登録ブランド"}])
        slugs = self.root / "term_slugs.yaml"
        slug_map = TermSlugMap(path=slugs)
        out = F.load_brand_seed_candidates(taxonomy_path, slug_map=slug_map)
        self.assertEqual(len(out), 1)
        seed, key = out[0]
        self.assertEqual(seed, "未登録ブランド")
        self.assertTrue(key)  # fallback key is non-empty
        self.assertFalse(slugs.exists(), "must not persist a new slug as a side effect")


class LoadThemeSeedCandidatesTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_builds_seed_as_label_plus_expand_modifier(self):
        seeds_file = self.root / "suggest_info_seeds.yaml"
        _write_seeds_file(seeds_file, [
            {"key": "age-1", "label": "1歳", "kind": "age"},
            {"key": "english", "label": "英語", "kind": "category"},
        ], expand_modifier="おもちゃ")
        out = F.load_theme_seed_candidates(seeds_file)
        self.assertEqual(out, [("1歳 おもちゃ", "age-1"), ("英語 おもちゃ", "english")])

    def test_theme_missing_key_or_label_skipped(self):
        seeds_file = self.root / "suggest_info_seeds.yaml"
        _write_seeds_file(seeds_file, [
            {"key": "age-1", "label": "1歳", "kind": "age"},
            {"key": "", "label": "壊れたテーマ", "kind": "age"},
        ])
        out = F.load_theme_seed_candidates(seeds_file)
        self.assertEqual(len(out), 1)


class LoadSeedCandidatesModeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.taxonomy_path = self.root / "brand_taxonomy.yaml"
        self.seeds_file = self.root / "suggest_info_seeds.yaml"
        _write_taxonomy(self.taxonomy_path, [{"canonical": "ブランドA"}])
        _write_seeds_file(self.seeds_file, [{"key": "age-1", "label": "1歳", "kind": "age"}])
        self.slugs = self.root / "term_slugs.yaml"
        self.slugs.write_text("ブランドA: brand-a\n", encoding="utf-8")
        self.slug_map = TermSlugMap(path=self.slugs)

    def tearDown(self):
        self._tmp.cleanup()

    def test_brands_mode_only_returns_brand_kind(self):
        out = F.load_seed_candidates(self.taxonomy_path, self.seeds_file, "brands", self.slug_map)
        self.assertEqual(out, [("ブランドA", "brand-a", F.SEED_KIND_BRAND)])

    def test_themes_mode_only_returns_theme_kind(self):
        out = F.load_seed_candidates(self.taxonomy_path, self.seeds_file, "themes", self.slug_map)
        self.assertEqual(out, [("1歳 おもちゃ", "age-1", F.SEED_KIND_THEME)])

    def test_all_mode_returns_both(self):
        out = F.load_seed_candidates(self.taxonomy_path, self.seeds_file, "all", self.slug_map)
        kinds = {k for _, _, k in out}
        self.assertEqual(kinds, {F.SEED_KIND_BRAND, F.SEED_KIND_THEME})
        self.assertEqual(len(out), 2)

    def test_default_mode_is_all(self):
        self.assertEqual(F.DEFAULT_SEEDS_MODE, "all")


class SelectTargetsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.out_dir = self.root / "amazon_suggest"

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_output_is_top_priority(self):
        seeds = [("ブランドA", "brand-a", F.SEED_KIND_BRAND), ("ブランドB", "brand-b", F.SEED_KIND_BRAND)]
        self.out_dir.mkdir()
        (self.out_dir / "brand-b.json").write_text(
            json.dumps({"fetched_at": "2020-01-01T00:00:00Z"}), encoding="utf-8",
        )
        now = datetime(2026, 6, 1, tzinfo=timezone.utc)
        targets = F.select_targets(seeds, self.out_dir, limit=40, min_age_days=30, now=now)
        keys = [k for _, k, _ in targets]
        self.assertEqual(keys[0], "brand-a")
        self.assertIn("brand-b", keys)

    def test_fresh_output_excluded(self):
        seeds = [("ブランドC", "brand-c", F.SEED_KIND_BRAND)]
        self.out_dir.mkdir()
        now = datetime(2026, 6, 1, tzinfo=timezone.utc)
        (self.out_dir / "brand-c.json").write_text(
            json.dumps({"fetched_at": (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")}),
            encoding="utf-8",
        )
        targets = F.select_targets(seeds, self.out_dir, limit=40, min_age_days=30, now=now)
        self.assertEqual(targets, [])

    def test_stale_output_included_and_ordered_oldest_first(self):
        seeds = [("ブランドD", "brand-d", F.SEED_KIND_BRAND), ("1歳 おもちゃ", "age-1", F.SEED_KIND_THEME)]
        self.out_dir.mkdir()
        now = datetime(2026, 6, 1, tzinfo=timezone.utc)
        (self.out_dir / "brand-d.json").write_text(json.dumps({"fetched_at": "2026-01-01T00:00:00Z"}), encoding="utf-8")
        (self.out_dir / "age-1.json").write_text(json.dumps({"fetched_at": "2026-02-01T00:00:00Z"}), encoding="utf-8")
        targets = F.select_targets(seeds, self.out_dir, limit=40, min_age_days=30, now=now)
        self.assertEqual([k for _, k, _ in targets], ["brand-d", "age-1"])

    def test_limit_caps_result(self):
        seeds = [(f"ブランド{i}", f"brand-{i}", F.SEED_KIND_BRAND) for i in range(5)]
        targets = F.select_targets(seeds, self.out_dir, limit=2, min_age_days=30)
        self.assertEqual(len(targets), 2)


class ExtractKeywordValuesTest(unittest.TestCase):
    def test_filters_non_keyword_types(self):
        payload = _amazon_payload([
            ("スクイーズ", "KEYWORD"),
            ("おもちゃ売れ筋ランキング > スクイーズ", "CATEGORY"),
            ("スクイーズの皮", "KEYWORD"),
        ])
        out = F._extract_keyword_values(payload)
        self.assertEqual(out, ["スクイーズ", "スクイーズの皮"])

    def test_unexpected_shape_raises(self):
        with self.assertRaises(F.google_suggest.SuggestParseError):
            F._extract_keyword_values(["not", "a", "dict"])
        with self.assertRaises(F.google_suggest.SuggestParseError):
            F._extract_keyword_values({"alias": "aps"})  # no 'suggestions' key


class RankAndMergeTest(unittest.TestCase):
    def test_ranks_assigned_in_array_order_starting_at_1(self):
        fetches = [("スクイーズ", ["スクイーズ", "スクイーズの皮", "スクイーズ 抗菌"])]
        out = F._rank_and_merge(fetches, depth=1)
        ranks = {e["query"]: e["rank"] for e in out}
        self.assertEqual(ranks["スクイーズ"], 1)
        self.assertEqual(ranks["スクイーズの皮"], 2)
        self.assertEqual(ranks["スクイーズ 抗菌"], 3)

    def test_duplicate_across_prefixes_keeps_min_rank(self):
        fetches = [
            ("スクイーズ", ["スクイーズの皮", "スクイーズ 抗菌"]),  # rank1, rank2
            ("スクイーズ ", ["スクイーズ 抗菌", "スクイーズの皮"]),  # rank1, rank2 (order flipped)
        ]
        out = F._rank_and_merge(fetches, depth=1)
        by_query = {e["query"]: e for e in out}
        self.assertEqual(len(out), 2, "duplicate normalized query must not appear twice")
        self.assertEqual(by_query["スクイーズ 抗菌"]["rank"], 1)
        self.assertEqual(by_query["スクイーズの皮"]["rank"], 1)

    def test_empty_values_ignored(self):
        out = F._rank_and_merge([("x", [])], depth=1)
        self.assertEqual(out, [])


class RelevanceFilterTest(unittest.TestCase):
    def test_drops_candidate_not_containing_seed(self):
        # real observed case: seed "4M" -> candidate "4枚組" has no relation
        self.assertFalse(F._is_relevant("4M", "4枚組"))

    def test_keeps_candidate_containing_seed(self):
        self.assertTrue(F._is_relevant("4M", "4M 布"))

    def test_whitespace_variation_not_dropped(self):
        # extra / differently placed whitespace must not cause a drop
        self.assertTrue(F._is_relevant("All Bright", "All  Bright   ソニッケアー"))
        self.assertTrue(F._is_relevant("All Bright", "AllBrightソニッケアー"))

    def test_case_variation_not_dropped(self):
        self.assertTrue(F._is_relevant("All Bright", "all bright ソニッケアー"))
        self.assertTrue(F._is_relevant("all bright", "ALL BRIGHT ソニッケアー"))

    def test_empty_seed_never_drops(self):
        self.assertTrue(F._is_relevant("", "何でも"))


class PrefixVariantsTest(unittest.TestCase):
    def test_brand_seed_gets_category_hint_variant_at_depth1(self):
        variants = F._prefix_variants("All Bright", 1, "All Bright", F.SEED_KIND_BRAND)
        self.assertEqual(variants, ["All Bright", "All Bright ", "All Bright おもちゃ"])

    def test_theme_seed_does_not_get_category_hint_variant(self):
        variants = F._prefix_variants("1歳 おもちゃ", 1, "1歳 おもちゃ", F.SEED_KIND_THEME)
        self.assertEqual(variants, ["1歳 おもちゃ", "1歳 おもちゃ "])

    def test_brand_seed_expansion_beyond_depth1_does_not_get_hint(self):
        # a candidate found at depth 1, being expanded further, is not the
        # original seed itself -> no category-hint variant added.
        variants = F._prefix_variants("バンダイ ぷにるんず", 2, "バンダイ", F.SEED_KIND_BRAND)
        self.assertEqual(variants, ["バンダイ ぷにるんず", "バンダイ ぷにるんず "])


class RunHttpErrorAbortTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.taxonomy_path = self.root / "brand_taxonomy.yaml"
        self.seeds_file = self.root / "suggest_info_seeds.yaml"
        self.out_dir = self.root / "amazon_suggest"
        self.slugs_path = self.root / "term_slugs.yaml"

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, session, **kwargs):
        _write_taxonomy(self.taxonomy_path, [
            {"canonical": "ブランドA"},
            {"canonical": "ブランドB"},
        ])
        _write_seeds_file(self.seeds_file, [{"key": "age-1", "label": "1歳", "kind": "age"}])
        self.slugs_path.write_text("ブランドA: brand-a\nブランドB: brand-b\n", encoding="utf-8")
        slug_map = TermSlugMap(path=self.slugs_path)
        params = dict(
            taxonomy_path=self.taxonomy_path, out_dir=self.out_dir,
            limit=40, min_age_days=30, sleep_min=0, sleep_max=0,
            seeds_file=self.seeds_file, seeds_mode="brands",
            session=session, sleeper=lambda _s: None, slug_map=slug_map,
        )
        params.update(kwargs)
        return F.run(**params)

    def test_non_200_aborts_after_first_seed_succeeds(self):
        session = mock.Mock()
        # brand-a: all 3 requests (prefix, prefix+space, prefix+category-hint) succeed.
        # brand-b: first request returns 403 -> loop-wide abort.
        session.get.side_effect = [
            _resp(200, _amazon_payload([("ブランドA サジェスト1", "KEYWORD")])),
            _resp(200, _amazon_payload([("ブランドA サジェスト2", "KEYWORD")])),
            _resp(200, _amazon_payload([("ブランドA おもちゃ 3", "KEYWORD")])),
            _resp(403),
        ]
        summary = self._run(session)
        self.assertTrue(summary["aborted"])
        self.assertEqual(summary["written"], 1)
        written_files = list(self.out_dir.glob("*.json"))
        self.assertEqual(len(written_files), 1)
        self.assertEqual(written_files[0].name, "brand-a.json")

    def test_connection_error_aborts(self):
        session = mock.Mock()
        session.get.side_effect = requests.ConnectionError("boom")
        summary = self._run(session)
        self.assertTrue(summary["aborted"])
        self.assertEqual(summary["written"], 0)

    def test_parse_failure_aborts(self):
        session = mock.Mock()
        session.get.return_value = _resp(text="not json at all")
        summary = self._run(session)
        self.assertTrue(summary["aborted"])
        self.assertEqual(summary["written"], 0)


class RunDroppedIrrelevantTest(unittest.TestCase):
    def test_dropped_irrelevant_recorded_in_output(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            taxonomy_path = root / "brand_taxonomy.yaml"
            seeds_file = root / "suggest_info_seeds.yaml"
            out_dir = root / "amazon_suggest"
            slugs_path = root / "term_slugs.yaml"
            _write_taxonomy(taxonomy_path, [{"canonical": "4M"}])
            _write_seeds_file(seeds_file, [])
            slugs_path.write_text("4M: 4m\n", encoding="utf-8")

            session = mock.Mock()
            session.get.return_value = _resp(200, _amazon_payload([
                ("4枚組", "KEYWORD"),  # irrelevant, must be dropped
                ("4M 布", "KEYWORD"),  # relevant
            ]))
            summary = F.run(
                taxonomy_path=taxonomy_path, out_dir=out_dir,
                limit=1, min_age_days=30, sleep_min=0, sleep_max=0,
                seeds_file=seeds_file, seeds_mode="brands",
                session=session, sleeper=lambda _s: None,
                slug_map=TermSlugMap(path=slugs_path),
            )
            self.assertGreater(summary["dropped_irrelevant_total"], 0)
            data = json.loads((out_dir / "4m.json").read_text(encoding="utf-8"))
            self.assertEqual(data["dropped_irrelevant"], summary["dropped_irrelevant_total"])
            queries = [s["query"] for s in data["suggestions"]]
            self.assertNotIn("4枚組", queries)
            self.assertIn("4M 布", queries)


class RunMaxRequestsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.taxonomy_path = self.root / "brand_taxonomy.yaml"
        self.seeds_file = self.root / "suggest_info_seeds.yaml"
        self.out_dir = self.root / "amazon_suggest"
        self.slugs_path = self.root / "term_slugs.yaml"
        _write_taxonomy(self.taxonomy_path, [
            {"canonical": "ブランドA"},
            {"canonical": "ブランドB"},
            {"canonical": "ブランドC"},
        ])
        _write_seeds_file(self.seeds_file, [])
        self.slugs_path.write_text(
            "ブランドA: brand-a\nブランドB: brand-b\nブランドC: brand-c\n", encoding="utf-8",
        )
        self.slug_map = TermSlugMap(path=self.slugs_path)

    def tearDown(self):
        self._tmp.cleanup()

    def test_max_requests_caps_total_network_calls(self):
        session = mock.Mock()
        session.get.return_value = _resp(200, _amazon_payload([("サジェスト", "KEYWORD")]))
        summary = F.run(
            taxonomy_path=self.taxonomy_path, out_dir=self.out_dir,
            limit=40, min_age_days=30, sleep_min=0, sleep_max=0,
            seeds_file=self.seeds_file, seeds_mode="brands",
            max_requests=2, session=session, sleeper=lambda _s: None,
            slug_map=self.slug_map,
        )
        self.assertLessEqual(session.get.call_count, 2)
        self.assertEqual(summary["requests_used"], session.get.call_count)

    def test_depth_expansion_increases_request_count(self):
        session = mock.Mock()
        session.get.return_value = _resp(200, _amazon_payload([("ブランドA 次の候補", "KEYWORD")]))
        summary = F.run(
            taxonomy_path=self.taxonomy_path, out_dir=self.out_dir,
            limit=1, min_age_days=30, sleep_min=0, sleep_max=0,
            seeds_file=self.seeds_file, seeds_mode="brands",
            depth=2, max_requests=100, session=session, sleeper=lambda _s: None,
            slug_map=self.slug_map,
        )
        # depth=1: 3 requests for the brand seed (prefix, prefix+space, prefix+category-hint),
        # all returning the same single candidate "ブランドA 次の候補" -> merges to 1 new
        # candidate for depth 2. depth=2: that 1 candidate is expanded with 2 more requests
        # (no category-hint at depth>1) -> total 5 requests.
        self.assertEqual(summary["requests_used"], 5)


class RunMinAgeDaysSkipTest(unittest.TestCase):
    def test_fresh_seed_is_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            taxonomy_path = root / "brand_taxonomy.yaml"
            seeds_file = root / "suggest_info_seeds.yaml"
            out_dir = root / "amazon_suggest"
            slugs_path = root / "term_slugs.yaml"
            _write_taxonomy(taxonomy_path, [{"canonical": "ブランドF"}])
            _write_seeds_file(seeds_file, [])
            slugs_path.write_text("ブランドF: brand-f\n", encoding="utf-8")
            out_dir.mkdir()
            out_dir.joinpath("brand-f.json").write_text(
                json.dumps({"fetched_at": F._now_iso()}), encoding="utf-8",
            )
            session = mock.Mock()
            summary = F.run(
                taxonomy_path=taxonomy_path, out_dir=out_dir,
                limit=40, min_age_days=30, sleep_min=0, sleep_max=0,
                seeds_file=seeds_file, seeds_mode="brands",
                session=session, sleeper=lambda _s: None,
                slug_map=TermSlugMap(path=slugs_path),
            )
            session.get.assert_not_called()
            self.assertEqual(summary["selected"], 0)
            self.assertEqual(summary["written"], 0)


class RunDryRunTest(unittest.TestCase):
    def test_dry_run_does_not_call_network_or_write(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            taxonomy_path = root / "brand_taxonomy.yaml"
            seeds_file = root / "suggest_info_seeds.yaml"
            out_dir = root / "amazon_suggest"
            slugs_path = root / "term_slugs.yaml"
            _write_taxonomy(taxonomy_path, [{"canonical": "ブランドG"}])
            _write_seeds_file(seeds_file, [])
            slugs_path.write_text("ブランドG: brand-g\n", encoding="utf-8")
            session = mock.Mock()
            summary = F.run(
                taxonomy_path=taxonomy_path, out_dir=out_dir,
                limit=40, min_age_days=30, sleep_min=0, sleep_max=0,
                seeds_file=seeds_file, seeds_mode="brands",
                dry_run=True, session=session,
                slug_map=TermSlugMap(path=slugs_path),
            )
            session.get.assert_not_called()
            self.assertFalse(out_dir.exists())
            self.assertEqual(summary["selected"], 1)
            self.assertEqual(summary["written"], 0)


class DefaultAliasTest(unittest.TestCase):
    def test_default_alias_is_aps(self):
        self.assertEqual(F.DEFAULT_ALIAS, "aps")

    def test_build_params_uses_given_alias(self):
        params = F._build_params("テスト", "aps")
        self.assertEqual(params["alias"], "aps")
        params2 = F._build_params("テスト", "toys")
        self.assertEqual(params2["alias"], "toys")


class WriteResultTest(unittest.TestCase):
    def test_output_shape(self):
        with tempfile.TemporaryDirectory() as td:
            out_dir = pathlib.Path(td) / "amazon_suggest"
            now = datetime(2026, 7, 12, 12, 34, 56, tzinfo=timezone.utc)
            path = F.write_result(
                out_dir, "テストブランド", "test-brand", "aps",
                [{"query": "テストブランド サジェスト", "rank": 1, "prefix": "テストブランド", "depth": 1}],
                dropped_irrelevant=3,
                now=now,
            )
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["seed"], "テストブランド")
            self.assertEqual(data["alias"], "aps")
            self.assertEqual(data["fetched_at"], "2026-07-12T12:34:56Z")
            self.assertEqual(data["dropped_irrelevant"], 3)
            self.assertEqual(data["suggestions"][0]["query"], "テストブランド サジェスト")
            self.assertEqual(path.name, "test-brand.json")


if __name__ == "__main__":
    unittest.main()
