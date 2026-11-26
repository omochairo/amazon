"""scripts/family_games_page.py のテスト (navi-brain#216 案 A: 家族で遊べるゲーム /family-games/)。

- 種類: 上から順に当てる (ことわざカードかるたはかるた)・版元「すごろくや」は双六にしない・
  トレカのパック・拡張・飾りは入れない
- 人数・遊ぶ時間: 記事の technical_specs に書いてあるものだけ読む
- 選び方: 1 商品は 1 つの年齢帯だけ・種類が偏らない (点の低い種類も 1 件は入る)・並びは点の順
- お正月遊び: 商品名に当たる物を、タグだけで当たる物より先に
- ページ: 件数の足りない年齢帯は出さない・FAQ はページの数字で答える
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.dirname(THIS_DIR)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import family_games_page as fg  # noqa: E402
from build_feature_lists import ArticleRecord, _record_to_payload_common  # noqa: E402


def _rec(asin, ivs_100=80, best_price=2000, name=None, ivs_score=4.5):
    return ArticleRecord(
        asin=asin, slug=asin, name=name or f"game-{asin}-x{asin}", image=None,
        ivs_score=ivs_score, ivs_100=ivs_100, best_price=best_price, best_platform="Amazon",
        amazon_url=f"https://www.amazon.co.jp/dp/{asin}",
    )


def _raw(name, tags=(), specs=None):
    d = {"product": {"name": name}, "tags": list(tags)}
    if specs is not None:
        d["technical_specs"] = specs
    return d


class KindTest(unittest.TestCase):
    def kind(self, name, tags=()):
        k = fg.kind_of(_raw(name, tags))
        return k.key if k else None

    def test_first_match_wins(self):
        self.assertEqual(self.kind("アーテック ことわざカードかるた", ["カードゲーム"]), "karuta")
        self.assertEqual(self.kind("楽しいすごろく3点セット", ["ボードゲーム"]), "sugoroku")
        self.assertEqual(self.kind("マテルゲーム ウノ(UNO)"), "card")
        self.assertEqual(self.kind("ハズブロ ジェンガ クラシック", ["ボードゲーム"]), "action")

    def test_publisher_sugorokuya_is_not_sugoroku(self):
        self.assertEqual(self.kind("すごろくや 音速飯店", ["すごろくや", "ボードゲーム"]), "board")

    def test_excludes_packs_expansions_and_decorations(self):
        self.assertIsNone(self.kind("ONE PIECE エクストラブースター EB-05", ["カードゲーム"]))
        self.assertIsNone(self.kind("カタン 5－6人拡張版", ["ボードゲーム"]))
        self.assertIsNone(self.kind("飾りミニ羽子板 新助六", ["羽子板"]))
        self.assertIsNone(self.kind("ポケモンカードゲーム カードホルダー", ["カードゲーム"]))

    def test_not_a_game(self):
        self.assertIsNone(self.kind("木製パズル どうぶつ", ["パズル"]))


class SpecsTest(unittest.TestCase):
    def test_players(self):
        cases = [
            ({"other": ["対象年齢4歳以上", "プレイ人数2～6人(2つ混ぜると最大12人)"]}, "2〜6人"),
            ({"other": ["対象年齢6歳以上", "2-4人用"]}, "2〜4人"),
            ({"other": ["プレイ人数: 何人でも"]}, "何人でも"),
            ({"other": ["3人以上で遊べます"]}, "3人以上"),
            ({"other": ["素材: 紙"]}, None),
        ]
        for specs, want in cases:
            got = fg.players_of(_raw("x", specs=specs))
            self.assertEqual(got["label"] if got else None, want, specs)

    def test_many_players(self):
        self.assertTrue(fg.many_players({"min": 2, "max": 6, "label": ""}))
        self.assertTrue(fg.many_players({"min": 1, "max": None, "label": ""}))
        self.assertFalse(fg.many_players({"min": 2, "max": 4, "label": ""}))
        self.assertFalse(fg.many_players(None))

    def test_play_time(self):
        self.assertEqual(fg.play_time_of(_raw("x", specs={"other": ["プレイ時間約10分"]})), "約10分")
        self.assertEqual(fg.play_time_of(_raw("x", specs={"other": ["プレイ時間：15〜30分"]})), "15〜30分")
        self.assertIsNone(fg.play_time_of(_raw("x", specs={"other": ["電池不要"]})))


class SelectTest(unittest.TestCase):
    def test_band_is_exclusive(self):
        self.assertEqual(fg.band_of(47)["key"], "age3")
        self.assertEqual(fg.band_of(48)["key"], "age4")
        self.assertEqual(fg.band_of(24)["key"], "age3")
        self.assertIsNone(fg.band_of(23))
        self.assertIsNone(fg.band_of(fg.MAX_AGE_MONTHS))
        self.assertIsNone(fg.band_of(None))

    def test_low_score_kind_still_gets_one_slot(self):
        recs, raw, mm = [], {}, {}
        for i in range(14):
            a = f"B{i:02d}"
            recs.append(_rec(a, ivs_100=95 - i))
            raw[a] = _raw(f"game-{a}", ["ボードゲーム"])
            mm[a] = 36
        recs.append(_rec("K01", ivs_100=40))
        raw["K01"] = _raw("どうぶつかるた", ["かるた"])
        mm["K01"] = 36
        recs.append(_rec("X01", ivs_100=99))          # 4 歳の帯の物は 3 歳に入らない
        raw["X01"] = _raw("game-X01", ["ボードゲーム"])
        mm["X01"] = 48
        band = fg.BANDS[0]
        picked = fg.select_band(recs, raw, mm, None, band, min_ivs=3.8)
        asins = [r.asin for r in picked]
        self.assertEqual(len(picked), fg.TOP_N)
        self.assertIn("K01", asins)
        self.assertNotIn("X01", asins)
        self.assertEqual(asins[-1], "K01")              # 並びは点の順 (かるたは最後)

    def test_kind_cap_when_other_kinds_exist(self):
        recs, raw, mm = [], {}, {}
        for i in range(10):
            for kind, tag in (("B", "ボードゲーム"), ("C", "カードゲーム")):
                a = f"{kind}{i:02d}"
                recs.append(_rec(a, ivs_100=(95 if kind == "B" else 60) - i))
                raw[a] = _raw(f"game-{a}", [tag])
                mm[a] = 60
        picked = fg.select_band(recs, raw, mm, None, fg.BANDS[1], min_ivs=3.8, top_n=8)
        boards = [r for r in picked if r.asin.startswith("B")]
        self.assertEqual(len(boards), fg.KIND_CAP)

    def test_budget_and_min_ivs(self):
        recs = [_rec("A01", best_price=12000), _rec("A02", ivs_score=3.0), _rec("A03", best_price=0)]
        raw = {r.asin: _raw(f"game-{r.asin}", ["ボードゲーム"]) for r in recs}
        mm = {r.asin: 36 for r in recs}
        self.assertEqual(fg.select_band(recs, raw, mm, None, fg.BANDS[0], min_ivs=3.8), [])


class OshogatsuTest(unittest.TestCase):
    def test_name_match_first(self):
        recs = [_rec("S01", ivs_100=90, name="かおシールブック"), _rec("F01", ivs_100=40, name="福笑い セット")]
        raw = {"S01": _raw("かおシールブック", ["福笑い", "カードゲーム"]),
               "F01": _raw("福笑い セット", ["福笑い"])}
        mm = {"S01": 36, "F01": 36}
        out = {o["key"]: o for o in fg.build_oshogatsu(recs, raw, mm)}
        self.assertEqual([i["name"] for i in out["fukuwarai"]["items"]], ["福笑い セット", "かおシールブック"])
        self.assertEqual(out["takoage"]["items"], [])
        self.assertTrue(all(o["how"] and o["where"] for o in out.values()))


class PayloadTest(unittest.TestCase):
    def test_bands_and_faq(self):
        recs, raw, mm = [], {}, {}
        for i in range(8):                               # 3 歳の帯だけ足りる
            a = f"A{i:02d}"
            recs.append(_rec(a, ivs_100=90 - i, best_price=800 + i * 400))
            tag = "かるた" if i < 2 else "ボードゲーム"
            raw[a] = _raw(f"game-{a}-{tag}", [tag], specs={"other": ["プレイ人数2～6人"]} if i == 2 else None)
            mm[a] = 36 if i else 24
        for i in range(3):                               # 小学生は MIN_ITEMS に届かない
            a = f"S{i:02d}"
            recs.append(_rec(a))
            raw[a] = _raw(f"game-{a}", ["ボードゲーム"])
            mm[a] = 84
        with tempfile.TemporaryDirectory() as td:
            payload = fg.build_payload(
                recs, raw, mm, None, min_ivs=3.8, generated_at="2026-11-26T03:00:00+00:00",
                rank_days_threshold=3, serialize=_record_to_payload_common,
                extras_kwargs={"price_watch_dir": Path(td), "price_history_dir": Path(td)})
        gp = payload["games_page"]
        bands = {b["key"]: b for b in gp["bands"]}
        self.assertEqual(bands["age3"]["count"], 8)
        self.assertEqual(bands["school"]["count"], 0)
        self.assertIn("too_few", bands["school"]["skipped"])
        self.assertEqual(payload["count"], 8)
        self.assertTrue(all(e["band"] == "age3" for e in payload["items"]))
        first = payload["items"][0]
        self.assertEqual(first["xmas"]["play_type"]["key"], "karuta")
        self.assertEqual([e["rank"] for e in payload["items"]], list(range(1, 9)))
        self.assertEqual(sum(1 for e in payload["items"] if e["xmas"]["players"] == "2〜6人"), 1)
        qs = {f["q"]: f["a"] for f in gp["faq"]}
        self.assertIn("このページのかるた2件の対象年齢は、いちばん下が2歳から", qs["かるたは何歳から遊べる？"])
        self.assertIn("人数が分かる1件のうち、5人以上で遊べるのは1件", qs["大人数（5人以上）で遊べるものは？"])
        self.assertIn("最安値が3,000円以下は6件、1,000円以下は1件", qs["お年玉で買える値段のものは？"])
        self.assertNotIn("4歳でも遊べるボードゲームは？", qs)  # 4〜5 歳の帯が無い
        self.assertEqual([k["key"] for k in gp["kinds"]], ["karuta", "board"])

    def test_no_band_means_empty_items(self):
        payload = fg.build_payload([], {}, {}, None, min_ivs=3.8, generated_at="2026-11-26T03:00:00+00:00",
                                   rank_days_threshold=3, serialize=_record_to_payload_common)
        self.assertEqual(payload["items"], [])  # Hugo は noindex にしてサイトマップから外す


if __name__ == "__main__":
    unittest.main()


class WordingTest(unittest.TestCase):
    def test_age_years_matches_age_label(self):
        self.assertEqual([fg._age_years(m) for m in (24, 30, 36, 40, 60)],
                         ["2歳", "2.5歳", "3歳", "3.3歳", "5歳"])

    def test_band_age_phrase_uses_band_width(self):
        phrase = fg.band_age_phrase(fg.BANDS[1])
        self.assertEqual(phrase(60, {}), "5歳から遊べて4〜5歳に合う")
        self.assertEqual(phrase(48, {}), "4歳から遊べて4〜5歳に合う")
        phrase3 = fg.band_age_phrase(fg.BANDS[0])
        self.assertEqual(phrase3(24, {}), "2歳から遊べて、3歳でも長く使える")
        self.assertIsNone(phrase3(None, {}))


class BandForArticleTest(unittest.TestCase):
    """商品ページのバナー (front matter の family_games_band) の判定。ページの選び方と同じ線引き。"""

    def test_game_gets_band_from_age(self):
        raw = _raw("どうぶつかるた", ["かるた"])
        raw["product"]["target_age"] = "3歳以上"
        self.assertEqual(fg.band_for_article(raw), "age3")
        raw["product"]["target_age"] = "6歳以上"
        self.assertEqual(fg.band_for_article(raw), "school")

    def test_not_a_game_or_out_of_range(self):
        toy = _raw("木製パズル どうぶつ", ["パズル"])
        toy["product"]["target_age"] = "3歳以上"
        self.assertIsNone(fg.band_for_article(toy))
        adult = _raw("大人のボードゲーム", ["ボードゲーム"])
        adult["product"]["target_age"] = "15歳以上"
        self.assertIsNone(fg.band_for_article(adult))
        self.assertIsNone(fg.band_for_article(_raw("年齢不明のすごろく", ["すごろく"])))
