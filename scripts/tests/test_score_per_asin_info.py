"""Unit tests for score_per_asin_info.score_asin (#1600 Phase 1).

band 判定の境界:
  - unfetched : news/youtube/books ファイル自体が無い (fetch 未実行) → defer しない
  - zero      : fetch 済みで第三者材料ゼロ かつ tier=D → defer 対象 (真ゼロ)
  - thin / ok : 材料の多寡で分岐
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import unittest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.dirname(THIS_DIR)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import score_per_asin_info as S  # noqa: E402


def _write(d: pathlib.Path, name: str, obj) -> None:
    with open(d / name, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


class ScorePerAsinInfoTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _mk(self, asin: str) -> pathlib.Path:
        d = self.base / asin
        d.mkdir(parents=True)
        return d

    def test_unfetched_no_source_files(self):
        # amazon + competitors のみ (filter_raw_per_asin 未実行) → unfetched, defer 対象外
        d = self._mk("B00000DMD2")
        _write(d, "amazon.json", {"item": {"title": "謎ブランドのおもちゃ"}})
        _write(d, "competitors.json", {"competitors": [{"asin": "X"}] * 5})
        r = S.score_asin("B00000DMD2", self.base)
        self.assertEqual(r["band"], "unfetched")
        self.assertFalse(r["evidence_fetched"])
        self.assertEqual(r["evidence_score"], 0)

    def test_true_zero_fetched_empty_dtier(self):
        # fetch 済み (空ファイル) かつ D-tier → zero (真ゼロ, defer 対象)
        d = self._mk("B0FAKEZERO1")
        _write(d, "amazon.json", {"item": {"title": "Bajoy 知育マット"}})
        _write(d, "news.json", {"items": []})
        _write(d, "youtube.json", {"items": []})
        _write(d, "books.json", {"items": []})
        r = S.score_asin("B0FAKEZERO1", self.base)
        self.assertEqual(r["band"], "zero")
        self.assertTrue(r["evidence_fetched"])
        self.assertEqual(r["evidence_score"], 0)
        self.assertEqual(r["brand_tier"], "D")

    def test_fetched_empty_but_known_brand_not_zero(self):
        # fetch 済みで材料ゼロでも S/A tier は google_search フォールバックが効く → zero にしない
        d = self._mk("B0KNOWNBRND")
        _write(d, "amazon.json", {"item": {"title": "レゴ クラシック 黄色のアイデアボックス"}})
        _write(d, "news.json", {"items": []})
        r = S.score_asin("B0KNOWNBRND", self.base)
        self.assertNotEqual(r["band"], "zero")
        self.assertEqual(r["brand_tier"], "S")

    def test_distinct_news_sources_from_title_suffix(self):
        # Google News RSS は url が固定リダイレクトのため title 末尾の媒体名で distinct 集計
        d = self._mk("B0NEWSRICH1")
        _write(d, "amazon.json", {"item": {"title": "謎ブランド おもちゃ"}})
        _write(d, "news.json", {"items": [
            {"title": "記事A - 朝日新聞"},
            {"title": "記事B - 朝日新聞"},          # 同媒体 → 重複しない
            {"title": "記事C - Rolling Stone Japan(ローリングストーン)"},  # (...) は畳む
            {"title": "媒体名なし見出し"},            # セパレータ無し → 無視
        ]})
        r = S.score_asin("B0NEWSRICH1", self.base)
        self.assertEqual(r["news_sources"], 2)

    def test_rich_asin_is_ok(self):
        d = self._mk("B0RICHASIN1")
        _write(d, "amazon.json", {"item": {"title": "謎ブランド 知育"}})
        _write(d, "news.json", {"items": [
            {"title": f"記事{i} - 媒体{i}"} for i in range(5)
        ]})
        _write(d, "youtube.json", {"items": [{"title": "v"}] * 4})
        r = S.score_asin("B0RICHASIN1", self.base)
        self.assertEqual(r["band"], "ok")
        self.assertGreaterEqual(r["evidence_score"], 40)


class ThirdPartySourcesBandTest(unittest.TestCase):
    """#5490 案B: third_party_sources.json を band 判定に配線した分の境界。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _mk_zero_asin(self, asin: str, sources: list | None = None) -> pathlib.Path:
        """band=zero になる素材 (fetch 済み・空・D tier) を作る。"""
        d = self.base / asin
        d.mkdir(parents=True)
        _write(d, "amazon.json", {"item": {"title": "Bajoy 知育マット"}})
        for name in ("news.json", "youtube.json", "books.json"):
            _write(d, name, {"items": []})
        if sources is not None:
            _write(d, "third_party_sources.json", {"sources": sources})
        return d

    def test_no_third_party_stays_zero(self):
        self._mk_zero_asin("B0TP000000")
        r = S.score_asin("B0TP000000", self.base)
        self.assertEqual(r["band"], "zero")
        self.assertEqual(r["third_party_hosts"], 0)

    def test_one_host_stays_zero(self):
        # §6.5.1 は非販売 2 件必須。1 件では zero を外さない。
        self._mk_zero_asin("B0TP000001", [
            {"url": "https://note.com/a/n/1", "host": "note.com"},
        ])
        r = S.score_asin("B0TP000001", self.base)
        self.assertEqual(r["band"], "zero")
        self.assertEqual(r["third_party_hosts"], 1)

    def test_two_hosts_escape_zero_to_thin(self):
        self._mk_zero_asin("B0TP000002", [
            {"url": "https://note.com/a/n/1", "host": "note.com"},
            {"url": "https://mokutopia.com/products/x", "host": "mokutopia.com"},
        ])
        r = S.score_asin("B0TP000002", self.base)
        self.assertEqual(r["band"], "thin")
        self.assertEqual(r["third_party_hosts"], 2)
        # evidence は動かさない (thin/ok の境界を third_party で越えさせない)
        self.assertEqual(r["evidence_score"], 0)

    def test_same_host_twice_is_one(self):
        self._mk_zero_asin("B0TP000003", [
            {"url": "https://note.com/a/n/1", "host": "note.com"},
            {"url": "https://www.note.com/a/n/2"},  # host 欠落 + www → 同一に畳む
        ])
        r = S.score_asin("B0TP000003", self.base)
        self.assertEqual(r["third_party_hosts"], 1)
        self.assertEqual(r["band"], "zero")

    def test_search_result_pages_do_not_count(self):
        # 既に書かれた JSON には search.kakaku.com が 409 件残っている (2026-08-18 実測)。
        # fetch 側を直しても過去分は残るので、採点側でも落ちること。
        self._mk_zero_asin("B0TP000004", [
            {"url": "https://search.kakaku.com/gravitrax", "host": "search.kakaku.com"},
            {"url": "https://www.yamada-denkiweb.com/search/x", "host": "yamada-denkiweb.com"},
            {"url": "https://www.biccamera.com/bc/category?q=x", "host": "biccamera.com"},
        ])
        r = S.score_asin("B0TP000004", self.base)
        self.assertEqual(r["third_party_hosts"], 0)
        self.assertEqual(r["band"], "zero")

    def test_third_party_alone_never_reaches_ok(self):
        # host を上限まで積んでも evidence は 0 のままなので ok にはならない。
        self._mk_zero_asin("B0TP000005", [
            {"url": f"https://ex{i}.com/a", "host": f"ex{i}.com"} for i in range(8)
        ])
        r = S.score_asin("B0TP000005", self.base)
        self.assertEqual(r["band"], "thin")
        self.assertEqual(r["third_party_hosts"], 8)
        self.assertEqual(r["info_score"], 4 * 3 + 4)  # third_party 上限 12 + tier D 4

    def test_unfetched_is_unchanged_by_third_party(self):
        # news/youtube/books が未収集なら third_party があっても enrich 待ち (defer 対象外)。
        d = self.base / "B0TP000006"
        d.mkdir(parents=True)
        _write(d, "amazon.json", {"item": {"title": "謎ブランドのおもちゃ"}})
        _write(d, "third_party_sources.json", {"sources": [
            {"url": "https://note.com/a/n/1", "host": "note.com"},
            {"url": "https://mokutopia.com/products/x", "host": "mokutopia.com"},
        ]})
        r = S.score_asin("B0TP000006", self.base)
        self.assertEqual(r["band"], "unfetched")

    def test_known_brand_zero_evidence_still_thin(self):
        # tier が D でなければ third_party の有無に関係なく従来どおり thin。
        d = self.base / "B0TP000007"
        d.mkdir(parents=True)
        _write(d, "amazon.json", {"item": {"title": "レゴ クラシック アイデアボックス"}})
        for name in ("news.json", "youtube.json", "books.json"):
            _write(d, name, {"items": []})
        r = S.score_asin("B0TP000007", self.base)
        self.assertEqual(r["band"], "thin")

    def test_malformed_third_party_file_is_ignored(self):
        self._mk_zero_asin("B0TP000008")
        (self.base / "B0TP000008" / "third_party_sources.json").write_text(
            "{ not json", encoding="utf-8")
        r = S.score_asin("B0TP000008", self.base)
        self.assertEqual(r["third_party_hosts"], 0)
        self.assertEqual(r["band"], "zero")


class IsSearchResultUrlTest(unittest.TestCase):
    def test_search_pages(self):
        for u in (
            "https://search.kakaku.com/gravitrax",
            "https://www.yamada-denkiweb.com/search/%E3%82%A2",
            "https://www.biccamera.com/bc/category?q=x",
            "https://giftmall.co.jp/search/x",
            "https://example.com/x?keyword=y",
        ):
            self.assertTrue(S.is_search_result_url(u), u)

    def test_editorial_pages_kept(self):
        for u in (
            "https://note.com/monte/n/n146db59af44e",
            "https://mokutopia.com/products/rocket-puzzle-box",
            "https://review.kakaku.com/review/K0001/",
            "https://ameblo.jp/x/entry-12855648969.html",
            "https://research-toys.example.jp/report",
        ):
            self.assertFalse(S.is_search_result_url(u), u)

    def test_garbage_is_not_counted(self):
        for u in ("", "not a url", "ftp:///"):
            self.assertTrue(S.is_search_result_url(u), u)


if __name__ == "__main__":
    unittest.main()
