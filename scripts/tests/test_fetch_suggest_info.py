"""scripts/fetch_suggest_info.py unit tests (#3332 N2 Lane A)。

カバレッジ:
1. シード定義読み込み (load_seed_config): 形状検証・不正形状の拒否
2. クエリ生成 (build_theme_queries / build_expansion_queries): 直積・重複除去・
   a-z/五十音 suffix の内容
3. 対象選定 (select_theme_targets): 未取得優先・fetched_at 昇順・min-age-days・
   limit cap (fetch_google_suggest.select_targets と同型)
4. Amazon completion フェッチ (fetch_amazon_completions_for_query): 成功/非200/
   パース失敗が google_suggest と同じ例外型で送出されること
5. dedupe (dedupe_info_suggestions): source 横断 dedup・source 保持・seed 除外
6. write_result: lane/seed_type タグ付きの出力形状
7. collect_theme / run(): 片方の source が HTTP エラーで無効化されてももう片方は
   継続すること、両方無効化で run 全体が打ち切られること、3連続パース失敗での
   無効化、dry-run が書き込み/通信をしないこと、--no-amazon 相当 (use_amazon=False)
   で amazon 側を一切呼ばないこと
"""
from __future__ import annotations

import json
import pathlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import requests
import yaml

from scripts import fetch_suggest_info as F
from scripts import fetch_google_suggest as google_suggest


def _resp(status: int = 200, json_list=None, text: str | None = None):
    r = mock.Mock()
    r.status_code = status
    if text is not None:
        r.text = text
    else:
        r.text = json.dumps(json_list if json_list is not None else ["seed", []])
    return r


def _write_seeds_file(
    dir_path: pathlib.Path,
    themes=None,
    modifiers=None,
    expand_modifier="おもちゃ",
) -> pathlib.Path:
    themes = themes if themes is not None else [{"key": "age-2", "label": "2歳", "kind": "age"}]
    modifiers = modifiers if modifiers is not None else ["おもちゃ", "おもちゃ ランキング"]
    p = dir_path / "seeds.yaml"
    p.write_text(
        yaml.safe_dump(
            {"themes": themes, "modifiers": modifiers, "expand_modifier": expand_modifier},
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return p


class LoadSeedConfigTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_valid_config_loads(self):
        p = _write_seeds_file(self.root)
        config = F.load_seed_config(p)
        self.assertEqual(config["themes"][0]["key"], "age-2")
        self.assertEqual(config["expand_modifier"], "おもちゃ")

    def test_missing_themes_rejected(self):
        p = self.root / "bad.yaml"
        p.write_text(yaml.safe_dump({"modifiers": ["a"], "expand_modifier": "a"}), encoding="utf-8")
        with self.assertRaises(ValueError):
            F.load_seed_config(p)

    def test_empty_modifiers_rejected(self):
        p = self.root / "bad.yaml"
        p.write_text(
            yaml.safe_dump({"themes": [{"key": "k", "label": "L"}], "modifiers": [], "expand_modifier": "a"}),
            encoding="utf-8",
        )
        with self.assertRaises(ValueError):
            F.load_seed_config(p)

    def test_missing_expand_modifier_rejected(self):
        p = self.root / "bad.yaml"
        p.write_text(
            yaml.safe_dump({"themes": [{"key": "k", "label": "L"}], "modifiers": ["a"]}),
            encoding="utf-8",
        )
        with self.assertRaises(ValueError):
            F.load_seed_config(p)

    def test_non_dict_root_rejected(self):
        p = self.root / "bad.yaml"
        p.write_text(yaml.safe_dump(["not", "a", "dict"]), encoding="utf-8")
        with self.assertRaises(ValueError):
            F.load_seed_config(p)


class BuildQueriesTest(unittest.TestCase):
    def test_build_theme_queries_cartesian_dedup(self):
        out = F.build_theme_queries("2歳", ["おもちゃ", "おもちゃ", "おもちゃ ランキング"])
        self.assertEqual(out, ["2歳 おもちゃ", "2歳 おもちゃ ランキング"])

    def test_build_expansion_queries_covers_alpha_and_kana(self):
        out = F.build_expansion_queries("2歳", "おもちゃ")
        self.assertEqual(len(out), len(F.EXPANSION_SUFFIXES))
        self.assertIn("2歳 おもちゃ a", out)
        self.assertIn("2歳 おもちゃ ん", out)

    def test_expansion_suffixes_length(self):
        # a-z (26) + 現代仮名遣い五十音 (46)
        self.assertEqual(len(F.ALPHA_SUFFIXES), 26)
        self.assertEqual(len(F.KANA_SUFFIXES), 46)
        self.assertEqual(len(F.EXPANSION_SUFFIXES), 72)


class SelectThemeTargetsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.out_dir = self.root / "suggest_info"

    def tearDown(self):
        self._tmp.cleanup()

    def _themes(self, *keys):
        return [{"key": k, "label": k, "kind": "age"} for k in keys]

    def test_missing_output_is_top_priority(self):
        self.out_dir.mkdir()
        (self.out_dir / "age-1.json").write_text(
            json.dumps({"fetched_at": "2020-01-01T00:00:00Z"}), encoding="utf-8",
        )
        now = datetime(2026, 6, 1, tzinfo=timezone.utc)
        targets = F.select_theme_targets(self._themes("age-0", "age-1"), self.out_dir, limit=40, min_age_days=30, now=now)
        keys = [t["key"] for t in targets]
        self.assertEqual(keys[0], "age-0", "missing-output theme must come first")
        self.assertIn("age-1", keys)

    def test_fresh_output_excluded(self):
        self.out_dir.mkdir()
        now = datetime(2026, 6, 1, tzinfo=timezone.utc)
        (self.out_dir / "age-2.json").write_text(
            json.dumps({"fetched_at": (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")}),
            encoding="utf-8",
        )
        targets = F.select_theme_targets(self._themes("age-2"), self.out_dir, limit=40, min_age_days=30, now=now)
        self.assertEqual(targets, [])

    def test_stale_output_included_ordered_oldest_first(self):
        self.out_dir.mkdir()
        now = datetime(2026, 6, 1, tzinfo=timezone.utc)
        (self.out_dir / "age-3.json").write_text(json.dumps({"fetched_at": "2026-01-01T00:00:00Z"}), encoding="utf-8")
        (self.out_dir / "age-4.json").write_text(json.dumps({"fetched_at": "2026-02-01T00:00:00Z"}), encoding="utf-8")
        targets = F.select_theme_targets(self._themes("age-3", "age-4"), self.out_dir, limit=40, min_age_days=30, now=now)
        self.assertEqual([t["key"] for t in targets], ["age-3", "age-4"])

    def test_limit_caps_result(self):
        targets = F.select_theme_targets(self._themes(*[f"age-{i}" for i in range(5)]), self.out_dir, limit=2, min_age_days=30)
        self.assertEqual(len(targets), 2)

    def test_corrupt_existing_output_treated_as_missing(self):
        self.out_dir.mkdir()
        (self.out_dir / "age-5.json").write_text("{not json", encoding="utf-8")
        targets = F.select_theme_targets(self._themes("age-5"), self.out_dir, limit=40, min_age_days=30)
        self.assertEqual([t["key"] for t in targets], ["age-5"])


class FetchAmazonCompletionsTest(unittest.TestCase):
    def test_success_shape(self):
        session = mock.Mock()
        session.get.return_value = _resp(200, ["2歳 おもちゃ", ["2歳 おもちゃ 木製", "2歳 おもちゃ 女の子"]])
        out = F.fetch_amazon_completions_for_query("2歳 おもちゃ", session)
        self.assertEqual(out, ["2歳 おもちゃ 木製", "2歳 おもちゃ 女の子"])

    def test_non_200_raises_shared_exception_type(self):
        session = mock.Mock()
        session.get.return_value = _resp(403)
        with self.assertRaises(google_suggest.NonOkStatusError):
            F.fetch_amazon_completions_for_query("q", session)

    def test_parse_failure_raises_shared_exception_type(self):
        session = mock.Mock()
        session.get.return_value = _resp(text="not json")
        with self.assertRaises(google_suggest.SuggestParseError):
            F.fetch_amazon_completions_for_query("q", session)


class DedupeInfoSuggestionsTest(unittest.TestCase):
    def test_cross_source_dedup_keeps_first_source(self):
        raw = [
            ("2歳 おもちゃ 木製", "2歳 おもちゃ", F.GOOGLE_SOURCE),
            ("2歳 おもちゃ 木製", "2歳 おもちゃ", F.AMAZON_SOURCE),
            ("2歳 おもちゃ 知育", "2歳 おもちゃ", F.AMAZON_SOURCE),
        ]
        out = F.dedupe_info_suggestions(raw, ["2歳 おもちゃ"])
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["source"], F.GOOGLE_SOURCE)
        self.assertEqual(out[1]["source"], F.AMAZON_SOURCE)

    def test_seed_itself_excluded(self):
        raw = [("2歳 おもちゃ", "2歳 おもちゃ", F.GOOGLE_SOURCE), ("2歳 おもちゃ 木製", "2歳 おもちゃ", F.GOOGLE_SOURCE)]
        out = F.dedupe_info_suggestions(raw, ["2歳 おもちゃ"])
        queries = [d["query"] for d in out]
        self.assertNotIn("2歳 おもちゃ", queries)
        self.assertEqual(queries, ["2歳 おもちゃ 木製"])


class WriteResultTest(unittest.TestCase):
    def test_output_shape_tagged_with_lane_and_seed_type(self):
        with tempfile.TemporaryDirectory() as td:
            out_dir = pathlib.Path(td) / "suggest_info"
            now = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)
            theme = {"key": "age-2", "label": "2歳", "kind": "age"}
            path = F.write_result(
                out_dir, theme, ["2歳 おもちゃ"],
                [{"query": "2歳 おもちゃ 木製", "seed": "2歳 おもちゃ", "source": "google"}],
                now=now,
            )
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["theme_key"], "age-2")
            self.assertEqual(data["theme_label"], "2歳")
            self.assertEqual(data["lane"], "A")
            self.assertEqual(data["seed_type"], "informational")
            self.assertEqual(data["fetched_at"], "2026-07-18T12:00:00Z")
            self.assertEqual(data["suggestions"][0]["source"], "google")


class CollectAndRunTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.out_dir = self.root / "suggest_info"
        self.seeds_path = _write_seeds_file(
            self.root,
            themes=[{"key": "age-2", "label": "2歳", "kind": "age"}],
            modifiers=["おもちゃ"],
        )
        # 展開 suffix をテスト用に極小化 (実データは 72件で unit test には過大)。
        self._patch_suffixes = mock.patch.object(F, "EXPANSION_SUFFIXES", ("あ", "い"))
        self._patch_suffixes.start()

    def tearDown(self):
        self._patch_suffixes.stop()
        self._tmp.cleanup()

    def test_dry_run_does_not_call_network_or_write(self):
        session = mock.Mock()
        summary = F.run(
            self.seeds_path, self.out_dir, limit=40, min_age_days=30,
            sleep_min=0, sleep_max=0, dry_run=True, session=session,
        )
        session.get.assert_not_called()
        self.assertFalse(self.out_dir.exists())
        self.assertEqual(summary["selected"], 1)
        self.assertEqual(summary["written"], 0)

    def test_google_http_error_disables_google_only_amazon_continues(self):
        # queries: base "2歳 おもちゃ" + expansion "2歳 おもちゃ あ"/"...い" (suffix patched to 2件)
        # order per query: google then amazon.
        session = mock.Mock()
        session.get.side_effect = [
            _resp(403),  # base query, google -> HTTP error, disables google
            _resp(200, ["2歳 おもちゃ", ["2歳 おもちゃ AMZ1"]]),  # base query, amazon
            _resp(200, ["2歳 おもちゃ あ", ["2歳 おもちゃ AMZ2"]]),  # expansion1, amazon (google skipped)
            _resp(200, ["2歳 おもちゃ い", ["2歳 おもちゃ AMZ3"]]),  # expansion2, amazon
        ]
        summary = F.run(
            self.seeds_path, self.out_dir, limit=40, min_age_days=30,
            sleep_min=0, sleep_max=0, session=session, sleeper=lambda _s: None,
        )
        self.assertTrue(summary["google_aborted"])
        self.assertFalse(summary["amazon_aborted"])
        self.assertFalse(summary["aborted"])
        self.assertEqual(summary["written"], 1)
        data = json.loads((self.out_dir / "age-2.json").read_text(encoding="utf-8"))
        queries = [s["query"] for s in data["suggestions"]]
        self.assertIn("2歳 おもちゃ AMZ1", queries)
        self.assertIn("2歳 おもちゃ AMZ3", queries)

    def test_both_sources_disabled_aborts_run(self):
        session = mock.Mock()
        session.get.side_effect = [
            _resp(403),  # google -> disabled
            _resp(500),  # amazon -> disabled
        ]
        summary = F.run(
            self.seeds_path, self.out_dir, limit=40, min_age_days=30,
            sleep_min=0, sleep_max=0, session=session, sleeper=lambda _s: None,
        )
        self.assertTrue(summary["google_aborted"])
        self.assertTrue(summary["amazon_aborted"])
        self.assertTrue(summary["aborted"])

    def test_three_consecutive_parse_failures_disable_source(self):
        session = mock.Mock()
        # google always returns unparsable text; amazon always succeeds with no suggestions.
        def _side_effect(url, params=None, headers=None, timeout=None):
            if params.get("q") is not None and "method" in params:
                return _resp(200, ["seed", []])  # amazon call shape
            return _resp(text="not json at all")  # google call shape (no 'method' param)
        session.get.side_effect = _side_effect
        summary = F.run(
            self.seeds_path, self.out_dir, limit=40, min_age_days=30,
            sleep_min=0, sleep_max=0, session=session, sleeper=lambda _s: None,
        )
        self.assertTrue(summary["google_aborted"])
        self.assertFalse(summary["amazon_aborted"])

    def test_use_amazon_false_never_calls_amazon(self):
        session = mock.Mock()
        session.get.return_value = _resp(200, ["seed", ["result"]])
        F.run(
            self.seeds_path, self.out_dir, limit=40, min_age_days=30,
            sleep_min=0, sleep_max=0, use_amazon=False, session=session, sleeper=lambda _s: None,
        )
        for call in session.get.call_args_list:
            params = call.kwargs.get("params") or (call.args[1] if len(call.args) > 1 else {})
            self.assertNotIn("method", params)


if __name__ == "__main__":
    unittest.main()
