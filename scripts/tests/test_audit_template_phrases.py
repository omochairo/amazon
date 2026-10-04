"""scripts/audit_template_phrases.py unit tests (amazon-navi-brain#39 Step 1 prep)."""
from __future__ import annotations

import json
import pathlib
import tempfile
import unittest

from scripts.audit_template_phrases import (
    KNOWN_PHRASES,
    append_history,
    build_phrase_text,
    count_hits,
    history_row,
    run,
)


def _write_article(dir_path: pathlib.Path, filename: str, data: dict) -> None:
    (dir_path / filename).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


class BuildPhraseTextTest(unittest.TestCase):
    def test_concatenates_narrative_sections_only(self):
        article = {
            "title": "無視されるべきタイトル",
            "tags": ["無視されるべきタグ"],
            "narrative": {
                "lead": "リード文",
                "how_to_choose": ["観点1", "観点2"],
            },
        }
        text = build_phrase_text(article)
        self.assertIn("リード文", text)
        self.assertIn("観点1", text)
        self.assertIn("観点2", text)
        self.assertNotIn("無視されるべき", text)

    def test_missing_fields_do_not_crash(self):
        self.assertEqual(build_phrase_text({}), "")
        self.assertEqual(build_phrase_text(None), "")  # type: ignore[arg-type]


class CountHitsTest(unittest.TestCase):
    def test_detects_known_phrase(self):
        hits = count_hits("...本記事の結論は、この商品が最適です...")
        self.assertTrue(hits["hook_a_conclusion"])
        self.assertFalse(hits["hook_c_ahead"])

    def test_all_ids_present_even_when_no_hits(self):
        hits = count_hits("何の変哲もない文章")
        self.assertEqual(set(hits.keys()), {p["id"] for p in KNOWN_PHRASES})
        self.assertFalse(any(hits.values()))


class RunEndToEndTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.articles_dir = self.root / "articles"
        self.articles_dir.mkdir()
        self.out_path = self.root / "out" / "template_phrase_audit.json"
        # 存在しないパスを明示し、本物の rewrite_ledger.jsonl を誤って読まない
        self.ledger_path = self.root / "no_such_ledger.jsonl"

    def tearDown(self):
        self._tmp.cleanup()

    def test_counts_by_cohort(self):
        _write_article(self.articles_dir, "2026-06-01-B0AAAAAAAA.json", {
            "slug": "2026-06-01-B0AAAAAAAA",  # pre_v7 (施行日前)
            "narrative": {"lead": "本記事の結論は、これが一番です"},
        })
        _write_article(self.articles_dir, "2026-08-01-B0BBBBBBBB.json", {
            "slug": "2026-08-01-B0BBBBBBBB",  # post_v7_new (ledger に無い)
            "narrative": {"lead": "普通のリード文です"},
        })
        payload = run(self.articles_dir, self.out_path, rewrite_ledger_path=self.ledger_path)

        self.assertEqual(payload["corpus_size"], 2)
        self.assertEqual(payload["cohort_sizes"], {"pre_v7": 1, "post_v7_new": 1, "post_v7_rewrite": 0})

        hook_a = next(p for p in payload["phrases"] if p["id"] == "hook_a_conclusion")
        self.assertEqual(hook_a["hits"], {"pre_v7": 1, "post_v7_new": 0, "post_v7_rewrite": 0})
        self.assertEqual(hook_a["rate"]["pre_v7"], 1.0)
        self.assertEqual(hook_a["rate"]["post_v7_new"], 0.0)
        self.assertIsNone(hook_a["rate"]["post_v7_rewrite"], "count=0 の cohort は rate=None (0 に潰さない)")

        self.assertTrue(self.out_path.exists())
        written = json.loads(self.out_path.read_text(encoding="utf-8"))
        self.assertEqual(written["corpus_size"], 2)

    def test_no_articles_writes_zero_corpus(self):
        payload = run(self.articles_dir, self.out_path, rewrite_ledger_path=self.ledger_path)
        self.assertEqual(payload["corpus_size"], 0)
        for p in payload["phrases"]:
            self.assertTrue(all(v == 0 for v in p["hits"].values()))

    def test_limit_caps_processed_articles(self):
        _write_article(self.articles_dir, "2026-08-01-B0AAAAAAAA.json",
                        {"slug": "2026-08-01-B0AAAAAAAA", "narrative": {}})
        _write_article(self.articles_dir, "2026-08-01-B0BBBBBBBB.json",
                        {"slug": "2026-08-01-B0BBBBBBBB", "narrative": {}})
        payload = run(self.articles_dir, self.out_path, rewrite_ledger_path=self.ledger_path, limit=1)
        self.assertEqual(payload["corpus_size"], 1)



class TestHistory(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.history = self.root / "history" / "template_phrase_audit.jsonl"

    def tearDown(self):
        self._tmp.cleanup()

    def _payload(self, week: str, corpus: int) -> dict:
        return {
            "generated_at": "2026-10-05T02:00:00Z",
            "source_week": week,
            "corpus_size": corpus,
            "cohort_sizes": {"pre_v7": corpus, "post_v7_new": 0, "post_v7_rewrite": 0},
            "phrases": [{**p, "hits": {}, "rate": {"pre_v7": 0.5}} for p in KNOWN_PHRASES],
        }

    def _rows(self) -> list[dict]:
        return [json.loads(ln) for ln in self.history.read_text(encoding="utf-8").splitlines()]

    def test_row_keeps_rate_per_phrase_id(self):
        row = history_row(self._payload("2026-W41", 10))
        self.assertEqual(set(row["rate"]), {p["id"] for p in KNOWN_PHRASES})
        self.assertEqual(row["corpus_size"], 10)
        self.assertNotIn("phrases", row, "history には text/origin を複製しない")

    def test_appends_new_week(self):
        append_history(self.history, history_row(self._payload("2026-W40", 10)))
        append_history(self.history, history_row(self._payload("2026-W41", 11)))
        self.assertEqual([r["source_week"] for r in self._rows()], ["2026-W40", "2026-W41"])

    def test_same_week_is_replaced(self):
        append_history(self.history, history_row(self._payload("2026-W41", 10)))
        append_history(self.history, history_row(self._payload("2026-W41", 12)))
        rows = self._rows()
        self.assertEqual(len(rows), 1, "再実行で同じ週の行が増えない")
        self.assertEqual(rows[0]["corpus_size"], 12)

    def test_main_skips_history_when_limited(self):
        from unittest import mock

        from scripts import audit_template_phrases as mod

        articles = self.root / "articles"
        articles.mkdir()
        base = ["prog", "--articles-dir", str(articles), "--out", str(self.root / "out.json"),
                "--history", str(self.history), "--rewrite-ledger", str(self.root / "none.jsonl")]
        with mock.patch("sys.argv", base + ["--limit", "1"]):
            mod.main()
        self.assertFalse(self.history.exists(), "--limit の部分集計は history に混ぜない")
        with mock.patch("sys.argv", base):
            mod.main()
        self.assertEqual(len(self._rows()), 1)

    def test_broken_line_is_kept(self):
        self.history.parent.mkdir(parents=True)
        self.history.write_text("not json\n", encoding="utf-8")
        append_history(self.history, history_row(self._payload("2026-W41", 10)))
        lines = self.history.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0], "not json")
        self.assertEqual(len(lines), 2)


if __name__ == "__main__":
    unittest.main()
