"""#11926 項目 1: 冒頭 (narrative.lead) から生成時の金額と店の比較を外し、価格は価格カードに任せる。

固定すること:
  - 金額の句を落とし、後ろに続く商品の話は文として残す
  - 金額を書かずに「価格は Amazon が最も手頃で」とだけ書いた文も同じに扱う
  - 「Yahoo!ショッピング」の「!」で文を切らない
  - 残した句の後ろにまた金額の句が来る文は、途中で切らずに文ごと落とす
  - 価格の話の続きだけの句 (「在庫優先で選んで問題ありません」など) は残さない
  - 金額の無い文・商品名の「Amazonベーシック」には触らない
  - 二度かけても変わらない
"""
from __future__ import annotations

import pathlib
import sys
import unittest

THIS_DIR = pathlib.Path(__file__).resolve().parent
SCRIPTS_DIR = THIS_DIR.parent
sys.path.insert(0, str(SCRIPTS_DIR))

import build_post as bp  # noqa: E402

HEAD = "木のパズルは、指先の発達を促す3歳からの知育玩具として最適な一品です。"
TAIL = "初めてのパズル選びで迷う保護者の方には、外しにくい選択肢といえます。"


class StripLeadPricesTest(unittest.TestCase):
    def strip(self, s: str) -> str:
        return bp._strip_lead_prices(s)

    def test_keeps_tail_after_price_clause(self) -> None:
        lead = HEAD + "価格は本記事執筆時点でAmazonが1387円と最も手頃で、木製の温かみも高く評価されています。" + TAIL
        self.assertEqual(self.strip(lead), HEAD + "木製の温かみも高く評価されています。" + TAIL)

    def test_drops_price_only_sentence(self) -> None:
        lead = HEAD + "価格は本記事執筆時点でAmazonが2273円と最も手頃で、楽天とは約900円の差がありました。" + TAIL
        self.assertEqual(self.strip(lead), HEAD + TAIL)

    def test_cheapest_claim_without_amount(self) -> None:
        lead = HEAD + "価格はAmazonが最も手頃で、親子で一緒に楽しみながら学べる点も高く評価されています。" + TAIL
        self.assertEqual(self.strip(lead), HEAD + "親子で一緒に楽しみながら学べる点も高く評価されています。" + TAIL)

    def test_yahoo_exclamation_is_not_a_sentence_end(self) -> None:
        lead = HEAD + "価格は本記事執筆時点でYahoo!ショッピングが3600円と最も手頃で、Amazonの3914円と比べてもお得に手に入ります。" + TAIL
        out = self.strip(lead)
        self.assertEqual(out, HEAD + TAIL)
        self.assertNotIn("Yahoo", out)

    def test_price_clause_after_kept_clause_drops_sentence(self) -> None:
        lead = HEAD + "価格はAmazonが2508円と最も手頃で、多彩な遊び方が評価されており、楽天とは約1300円差でした。" + TAIL
        self.assertEqual(self.strip(lead), HEAD + TAIL)

    def test_price_context_only_tail_is_dropped(self) -> None:
        for s in (
            "価格は本記事執筆時点でAmazonとYahooショッピングが8800円で並んでおり、在庫優先で選んで問題ありません。",
            "価格は本記事執筆時点でAmazonが1980円で最安値となっており、お求めやすい設定です。",
            "価格は本記事執筆時点でAmazonが2980円と最も手頃で、普段利用しているポイント還元率の高いショップで選んで問題ありません。",
        ):
            with self.subTest(s=s):
                self.assertEqual(self.strip(HEAD + s + TAIL), HEAD + TAIL)

    def test_other_shapes(self) -> None:
        lead = HEAD + "Amazonで約3400円で購入でき、厚手のカードと裏面の説明付きで長持ちする点が高く評価されています。" + TAIL
        self.assertEqual(self.strip(lead), HEAD + "厚手のカードと裏面の説明付きで長持ちする点が高く評価されています。" + TAIL)
        lead = HEAD + "本記事執筆時点で、Amazonにて6270円で販売されているのを確認しています。" + TAIL
        self.assertEqual(self.strip(lead), HEAD + TAIL)

    def test_untouched(self) -> None:
        for lead in (
            HEAD + TAIL,
            "Amazonベーシック テンペラクレヨンは、窓ガラスにも描ける水で落としやすい仕様が最適な一品です。" + TAIL,
            "",
        ):
            with self.subTest(lead=lead):
                self.assertEqual(self.strip(lead), lead)
        self.assertIsNone(self.strip(None))

    def test_idempotent(self) -> None:
        lead = HEAD + "価格は本記事執筆時点でAmazonが1387円と最も手頃で、木製の温かみも高く評価されています。" + TAIL
        once = self.strip(lead)
        self.assertEqual(self.strip(once), once)


if __name__ == "__main__":
    unittest.main()
