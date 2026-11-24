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
    _snippet_plain_text,
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

    def test_markdown_snippet_is_flattened(self):
        # #10091: 空行入りの Markdown スニペットが <details> の HTML ブロックを
        # 途中で終わらせ、外部サイトの相対リンク (/news/products_info/hoph.html)
        # が navi.omcha.jp 上のリンクとして描画されて 404 になっていた。
        snippet = (
            "### 最新の記事\n\n 2025.03.10 充放電装置を追加！\n"
            " [2024.11.25 新しいアンプを発売しました。[HOPHシリーズ]](/news/products_info/hoph.html)\n\n"
            "  + 新製品情報\n\n ![img](//img.example.com/a.jpg) 最高クラスのパワーを実現した電源モジュール登場"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self._write(root, "B4", [
                {"title": "新製品情報", "url": "https://www.matsusada.co.jp/news/products_info",
                 "snippet": snippet, "host": "matsusada.co.jp"},
            ])
            data = {"product": {"asin": "B4"}}
            _attach_source_highlights(data, root)
            out = data["source_highlights"][0]["snippet"]
        self.assertNotIn("\n", out)
        self.assertNotIn("/news/products_info", out)
        self.assertNotIn("img.example.com", out)
        for ch in "#[]()":
            self.assertNotIn(ch, out)
        self.assertNotIn("+ 新製品情報", out)
        self.assertIn("最新の記事 2025.03.10 充放電装置を追加！", out)
        self.assertIn("HOPHシリーズ", out)

    def test_plain_text_keeps_prose(self):
        s = "子どもが夢中で遊んでいます。対象年齢は3-5歳で、全長30cmほどです。"
        self.assertEqual(_snippet_plain_text(s), s)

    def test_preserves_existing(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = {"product": {"asin": "B1"}, "source_highlights": [{"x": 1}]}
            _attach_source_highlights(data, pathlib.Path(tmp))
            self.assertEqual(data["source_highlights"], [{"x": 1}])


class ReaderJunkTests(unittest.TestCase):
    """2026-11-24: 本文抽出の残り・サイトのメニュー・別商品の引用が読者に出ていた。"""

    def _attach(self, name: str, sources: list[dict]) -> list[dict]:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            AttachTests()._write(root, "BX", sources)
            data = {"product": {"asin": "BX", "name": name}}
            _attach_source_highlights(data, root)
            return data.get("source_highlights") or []

    def test_title_line_inline_heading_bold_and_url_are_removed(self):
        raw = (
            "Title: 変身ベルトのブログ\n# 変身ベルト. 発売日は12月5日. ## 遊んでみた感想. "
            "**音声がとても**かっこいいです。 "
            "[![[BANDAI] バンダイ 変身ベルト](https://m.media-amazon.com/a.jpg)]"
            "(https://www.amazon.co.jp/dp/B0X?tag=other-22 psc=1&tag=other2-22"
        )
        out = _snippet_plain_text(raw)
        self.assertNotIn("Title:", out)
        self.assertNotIn("#", out)
        self.assertNotIn("**", out)
        self.assertNotIn("http", out)
        self.assertNotIn("tag=", out)
        self.assertIn("遊んでみた感想.", out)
        self.assertIn("音声がとてもかっこいいです。", out)

    def test_rejects_site_navigation_and_video_frame(self):
        for s in (
            "木のおもちゃ専門店 マイページログイン カートを見る おもちゃカテゴリー 日本製の木のおもちゃ",
            "BRIO Toys Sign up for our newsletter for the latest news and offers from BRIO",
            "DXライダーエグズセット02 仮面ライダーマイス. Channel: MAX HOBBY CHANNEL. Description.",
        ):
            self.assertFalse(_highlight_snippet_ok(s), s)

    def test_rejects_mojibake_and_unreadable_script(self):
        self.assertFalse(_highlight_snippet_ok(
            "OYOBALEۥ˥å˥åȤä(-1388) 5,000(ǹ5,500) 145 21-40 ɽ 1 2 3 4 5 6 7 8 ڡξ | Ķ"
        ))
        self.assertFalse(_highlight_snippet_ok(
            "How to change the face plate for tamagotchi paradise มาดูๆ พามาแกะเครื่องกัน"
        ))
        # フランス語のアクセント程度は残す
        self.assertTrue(_highlight_snippet_ok(
            "Vilac est une marque française de jouets en bois fabriqués dans le Jura depuis 1911."
        ))

    def test_drops_quotes_about_other_products(self):
        hl = self._attach("DX Xライズスラッシャー", [
            {"title": "仮面ライダーマイス", "url": "https://a.example.jp/1", "host": "a.example.jp",
             "snippet": "仮面ライダーマイス「DX Xライズスラッシャー」が12/5発売。ライドエグズ5が付属します。"},
            {"title": "変身ベルト DXマイスドライバー", "url": "https://b.example.jp/2", "host": "b.example.jp",
             "snippet": "変身ベルト DXマイスドライバーを紹介します。音声はとてもかっこいいです。"},
        ])
        self.assertEqual([h["host"] for h in hl], ["a.example.jp"])

    def test_word_order_and_script_boundaries_still_match(self):
        hl = self._attach("パンどろぼう着脱式ぬいぐるみ", [
            {"title": "パンどろぼう ぬいぐるみ着脱式", "url": "https://c.example.jp/3", "host": "c.example.jp",
             "snippet": "パンを脱がしたり着せたりできる楽しいぬいぐるみです。サイズは17cmで贈り物にも。"},
        ])
        self.assertEqual(len(hl), 1)
        hl = self._attach("ZipString替え紐 3色セット", [
            {"title": "ZipString（ジップストリング）の使い方", "url": "https://d.example.jp/4",
             "host": "d.example.jp",
             "snippet": "まず、付属のひもを本体上部の溝へセットします。ひもの上下が交差しにくくなります。"},
        ])
        self.assertEqual(len(hl), 1)

    def test_url_removal_does_not_eat_following_prose(self):
        out = _snippet_plain_text("公式（https://example.com/page）によると、子どもが楽しく遊べます。")
        self.assertIn("子どもが楽しく遊べます。", out)
        out = _snippet_plain_text("詳細(https://example.com/a 以降のレビューでは音がやさしいと好評です。")
        self.assertIn("音がやさしいと好評です。", out)
        self.assertNotIn("**", _snippet_plain_text("**## 見出し** のあとに本文が続きます。"))
        self.assertNotIn("#", _snippet_plain_text("**## 見出し** のあとに本文が続きます。"))

    def test_cookie_toy_review_is_not_navigation(self):
        self.assertTrue(_highlight_snippet_ok(
            "クッキー型で生地を抜いて、チョコチップクッキーを作るおままごとが楽しめました。"
        ))
        self.assertFalse(_highlight_snippet_ok(
            "当サイトではCookieを使用しています。サイトの利用を続けると同意したものとみなします。"
        ))

    def test_login_counts_as_navigation_only_with_shop_words(self):
        self.assertFalse(_highlight_snippet_ok(
            "木のおもちゃ ウッディモンキー 本店 検索 ログイン 会員登録 0 カート 0 臨時休業のお知らせ"
        ))
        self.assertTrue(_highlight_snippet_ok(
            "アプリはログイン不要で、届いたその日から親子ですぐに遊び始められるのが便利でした。"
        ))

    def test_kana_proper_noun_and_digit_tokens(self):
        hl = self._attach("パンどろぼう着脱式ぬいぐるみ", [
            {"title": "絵本のキャラクター", "url": "https://h.example.jp/8", "host": "h.example.jp",
             "snippet": "パンどろぼうのぬいぐるみは、絵本の世界そのままの表情でかわいいと評判です。"},
        ])
        self.assertEqual(len(hl), 1)
        hl = self._attach("ジグソーパズル 500ピース", [
            {"title": "別の商品", "url": "https://i.example.jp/9", "host": "i.example.jp",
             "snippet": "送料は全国一律で、税込1,500円以上のご注文で無料になります。お届けまで3日です。"},
        ])
        self.assertEqual(hl, [])

    def test_two_char_name_still_checks_relevance(self):
        hl = self._attach("将棋セット", [
            {"title": "将棋の駒の並べ方", "url": "https://f.example.jp/6", "host": "f.example.jp",
             "snippet": "将棋の駒は木製で、初めての子どもでも並べ方を覚えやすいように駒に動きが書いてあります。"},
            {"title": "別の店", "url": "https://g.example.jp/7", "host": "g.example.jp",
             "snippet": "当店ではクリスマスのギフト包装を無料で承っております。お気軽にご相談ください。"},
        ])
        self.assertEqual([h["host"] for h in hl], ["f.example.jp"])

    def test_generic_words_alone_do_not_count_as_about(self):
        hl = self._attach("木製 知育玩具 セット", [
            {"title": "別の店", "url": "https://e.example.jp/5", "host": "e.example.jp",
             "snippet": "子どもが夢中になって遊んでいます。説明書もわかりやすく親子で楽しめました。"},
        ])
        # 商品名が一般語だけで語が作れないときは判定しない (従来どおり残す)
        self.assertEqual(len(hl), 1)


if __name__ == "__main__":
    unittest.main()
