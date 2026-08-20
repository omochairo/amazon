"""Unit tests for #1370 — honest external-source highlights block.

Covers the pure logic of build_post's source-highlights attach step:
    - snippet quality gate (junk / digit-dense / stub rejection)
    - host deny (commerce listings excluded) / prefer ranking
    - Japanese-prose preference and one-snippet-per-host diversity
    - graceful skip when no per-ASIN snapshot or no usable snippet survives
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

THIS_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = THIS_DIR.parent.parent  # amazon-clone/
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from build_post import (  # type: ignore[import-not-found]
    _attach_source_highlights,
    _highlight_score,
    _highlight_snippet_ok,
)


class SnippetGateTests(unittest.TestCase):
    def test_rejects_short_stub(self):
        self.assertFalse(_highlight_snippet_ok("短すぎ"))

    def test_rejects_parts_list_markers(self):
        self.assertFalse(
            _highlight_snippet_ok("Qty. ID Name Symbol Part # 1 Base Grid 6SCBG 1 Red LED")
        )

    def test_rejects_digit_dense(self):
        self.assertFalse(_highlight_snippet_ok("6SC01 6SC02 6SC03 6SC04 6SC05 6SC06 6SCB1 123456"))

    def test_accepts_prose(self):
        self.assertTrue(
            _highlight_snippet_ok(
                "子どもが夢中になって遊んでいます。説明書もわかりやすく親子で楽しめました。"
            )
        )

    def test_accepts_prose_with_incidental_numbers(self):
        # #4150 regression guard: age ranges / dimensions must not be
        # mistaken for phone numbers or trip the digit-density gate.
        self.assertTrue(
            _highlight_snippet_ok(
                "対象年齢は3-5歳向けで、組み立てると全長30cmほどになる大きめのブロック作品です。"
            )
        )

    def test_rejects_real_contact_block(self):
        # #4150: actual dougukan.com snippet that produced the 404 on
        # /cdn-cgi/l/email-protection/ in the site audit.
        self.assertFalse(
            _highlight_snippet_ok(
                "ベビーギフトフルセットの内容 具館へのご連絡は 電話 03-3744-0909 / "
                "FAX 03-3744-0988 / Eメール mail@dougukan.com"
            )
        )

    def test_rejects_email_only(self):
        self.assertFalse(
            _highlight_snippet_ok(
                "ご質問やご不明点がございましたら遠慮なくご連絡ください support@example.co.jp"
            )
        )

    def test_rejects_phone_only(self):
        self.assertFalse(
            _highlight_snippet_ok(
                "お問い合わせは受付時間内にお願いいたします。077-555-1234 までどうぞ。"
            )
        )


class ScoreTests(unittest.TestCase):
    def test_japanese_prose_outranks_english_listing(self):
        ja = {"host": "reddit.com", "snippet": "親子で長く遊べてとても満足しています。知育にも良いと感じました。"}
        en = {"host": "example.com", "snippet": "A short english product blurb about the kit."}
        self.assertGreater(_highlight_score(ja), _highlight_score(en))


class AttachTests(unittest.TestCase):
    def _write(self, root: pathlib.Path, asin: str, sources: list[dict]) -> None:
        d = root / asin
        d.mkdir(parents=True, exist_ok=True)
        (d / "third_party_sources.json").write_text(
            json.dumps({"asin": asin, "sources": sources}, ensure_ascii=False),
            encoding="utf-8",
        )

    def test_attaches_filtered_diverse_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self._write(root, "B1", [
                # commerce listing — excluded by host deny
                {"title": "eBay listing", "url": "https://ebay.com/itm/1",
                 "snippet": "Snap Circuits 203 Kit complete, accurate description, ships fast worldwide.",
                 "host": "ebay.com"},
                # Japanese forum prose — top pick
                {"title": "Reddit", "url": "https://reddit.com/r/x/1",
                 "snippet": "子どもが夢中で遊んでいます。知育効果も高く親子で長く楽しめています。",
                 "host": "reddit.com"},
                # institutional English prose — kept
                {"title": "Library", "url": "https://nlc.nebraska.gov/x",
                 "snippet": "Snap Circuits is a fun and easy way to learn about electronics with projects.",
                 "host": "nlc.nebraska.gov"},
                # second reddit — dropped by one-per-host diversity
                {"title": "Reddit2", "url": "https://reddit.com/r/x/2",
                 "snippet": "別のスレッドでも評判が良く、買って良かったという声が多いです。",
                 "host": "reddit.com"},
            ])
            data = {"product": {"asin": "B1"}}
            _attach_source_highlights(data, root)
            hl = data.get("source_highlights")
            self.assertTrue(hl)
            hosts = [h["host"] for h in hl]
            self.assertNotIn("ebay.com", hosts)          # commerce excluded
            self.assertEqual(len(hosts), len(set(hosts)))  # diverse hosts
            self.assertEqual(hl[0]["host"], "reddit.com")  # JP prose ranked first

    def test_skips_when_no_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = {"product": {"asin": "MISSING"}}
            _attach_source_highlights(data, pathlib.Path(tmp))
            self.assertNotIn("source_highlights", data)

    def test_skips_when_only_junk(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self._write(root, "B2", [
                {"title": "PDF", "url": "https://x.com/p.pdf",
                 "snippet": "Qty. ID Name Symbol Part # 1 Base Grid 6SCBG 1 Red LED 6SCD1",
                 "host": "x.com"},
            ])
            data = {"product": {"asin": "B2"}}
            _attach_source_highlights(data, root)
            self.assertNotIn("source_highlights", data)

    def test_search_result_pages_are_not_sources(self):
        # #5490 案B: 検索語を URL に埋めただけのページは出典ではない。収集側は
        # 塞いだが、それ以前に集めた行が store に残っており、描画側に防波堤が
        # 無かった (2026-08-20 実測: 配信物 157 ページが表示していた)。
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self._write(root, "B3", [
                {"title": "kakaku 検索", "url": "https://search.kakaku.com/gravitrax",
                 "snippet": "グラビトラックスの検索結果です。価格や在庫を比較して選べます。",
                 "host": "search.kakaku.com"},
                {"title": "レビュー本体", "url": "https://review.kakaku.com/review/K0001/",
                 "snippet": "子どもが夢中で遊んでいます。知育効果も高く親子で長く楽しめています。",
                 "host": "review.kakaku.com"},
            ])
            data = {"product": {"asin": "B3"}}
            _attach_source_highlights(data, root)
            hosts = [h["host"] for h in data.get("source_highlights") or []]
            self.assertNotIn("search.kakaku.com", hosts)
            self.assertIn("review.kakaku.com", hosts)  # レビュー本体は残す

    def test_preserves_existing(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = {"product": {"asin": "B1"}, "source_highlights": [{"x": 1}]}
            _attach_source_highlights(data, pathlib.Path(tmp))
            self.assertEqual(data["source_highlights"], [{"x": 1}])


if __name__ == "__main__":
    unittest.main()
