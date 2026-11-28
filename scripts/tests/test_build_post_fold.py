"""商品ページを短くする折りたたみ (2026-11-28)。

固定すること:
  - 見出し (H2・H3) は出したまま、中身だけを <details> に入れる
  - 主な特徴は先頭 3 件を出したまま、4 件目以降を畳む (3 件以下なら畳まない)
  - 基本スペックの表は畳む
  - 関連動画は 1 本目を埋め込みのまま、2 本目以降は畳んでサムネイルのリンクにする (iframe にしない)
  - FAQ は 1 問ずつ <details> にし、1 問目だけ開いておく
  - 参考ソースは先頭 3 件を出したまま、4 件目以降を畳む
  - <details> の開き・閉じタグは空行で区切った独立の行にする (Markdown の HTML ブロックを壊さない)
"""
from __future__ import annotations

import pathlib
import re
import sys
import unittest

THIS_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR.parent))
sys.path.insert(0, str(THIS_DIR))

from test_build_post_buy_offer import AMZ, _data, _prices, _render  # noqa: E402


def _md(**extra) -> str:
    return _render(_data(_prices(AMZ, None, None), **extra))


class FeaturesFoldTest(unittest.TestCase):
    def test_first_three_visible_rest_folded(self):
        md = _md()
        self.assertNotIn("features-more", md)  # 特徴が無いときは何も出さない

        feats = [f"特徴{i}" for i in range(1, 6)]
        d = _data(_prices(AMZ, None, None))
        d["product"]["features"] = feats
        md = _render(d)
        head, _, tail = md.partition('<details class="fold-more features-more"')
        self.assertTrue(tail, "4 件目以降が畳まれていない")
        for f in feats[:3]:
            self.assertIn(f"- {f}", head)
        for f in feats[3:]:
            self.assertIn(f"- {f}", tail.split("</details>")[0])
        self.assertIn("ほか 2 件", tail)
        # 閉じタグの前は空行 (箇条書きの続きと読まれない)
        self.assertRegex(md, r"- 特徴5\n\n+</details>")

    def test_three_or_less_not_folded(self):
        d = _data(_prices(AMZ, None, None))
        d["product"]["features"] = ["a", "b", "c"]
        self.assertNotIn("features-more", _render(d))


class SpecFoldTest(unittest.TestCase):
    def test_spec_table_inside_details(self):
        md = _md(technical_specs={"weight": "500g"})
        self.assertRegex(md, r'<details class="fold-more spec-more"[^>]*><summary>[^<]+</summary><table class="spec-table">')
        self.assertIn("</tbody></table></details>", md)


class VideoFoldTest(unittest.TestCase):
    def test_first_embedded_rest_are_thumbnail_links(self):
        vids = [{"url": f"https://www.youtube.com/watch?v=VID{i}aaaaaa", "title": f"動画{i}"} for i in range(1, 4)]
        md = _md(youtube_embeds=vids)
        self.assertEqual(md.count("<iframe"), 1)
        self.assertIn("embed/VID1aaaaaa", md)
        fold = md.split('<details class="fold-more videos-more"')[1].split("</details>")[0]
        self.assertIn("（2 本）", fold)
        for i in (2, 3):
            self.assertIn(f'href="https://www.youtube.com/watch?v=VID{i}aaaaaa"', fold)
            self.assertIn(f"i.ytimg.com/vi/VID{i}aaaaaa/", fold)
        self.assertNotIn("<iframe", fold)

    def test_non_youtube_and_escaped_title(self):
        vids = [{"url": "https://youtu.be/FIRSTaaaaaa", "title": "1"},
                {"url": "https://example.com/v", "title": "外部の動画"},
                {"url": "https://youtu.be/THIRDaaaaaa", "title": "a <b> & c"}]
        md = _md(youtube_embeds=vids)
        fold = md.split('<details class="fold-more videos-more"')[1].split("</details>")[0]
        # 開きタグの後に空行があり、2 本目の Markdown のリンクが HTML ブロックに飲まれない
        self.assertRegex(fold, r"</summary>\n\n+- \[外部の動画\]\(https://example.com/v\)")
        self.assertIn("<span>a &lt;b&gt; &amp; c</span>", fold)
        self.assertNotIn("<iframe", fold)

    def test_single_video_not_folded(self):
        md = _md(youtube_embeds=[{"url": "https://youtu.be/ONLYaaaaaaa", "title": "t"}])
        self.assertNotIn("videos-more", md)
        self.assertEqual(md.count("<iframe"), 1)


class FaqFoldTest(unittest.TestCase):
    def test_each_question_is_details_and_first_is_open(self):
        faq = [{"question": "Q1 <b>", "answer": "答え1"}, {"question": "Q2", "answer": "答え2"}]
        md = _md(faq=faq)
        items = re.findall(r'<details class="faq-item"[^>]*?( open)?><summary>(.*?)</summary>\n\n(.*?)\n\n</details>', md)
        self.assertEqual([(o, q, a) for o, q, a in items],
                         [(" open", "Q1 &lt;b&gt;", "答え1"), ("", "Q2", "答え2")])
        self.assertNotIn("### Q1", md)


class SourcesFoldTest(unittest.TestCase):
    def test_first_three_visible_rest_folded(self):
        srcs = [{"name": f"ソース{i}", "url": f"https://example.com/{i}"} for i in range(1, 6)]
        md = _md(sources=srcs)
        visible, _, folded = md.partition('<details class="fold-more sources-more"')
        for i in (1, 2, 3):
            self.assertIn(f"ソース{i}</a>", visible)
        for i in (4, 5):
            self.assertIn(f"ソース{i}</a>", folded.split("</details>")[0])
        self.assertIn("（2 件）", folded)
        # 本文の件数は全件のまま
        self.assertIn("以下の5件のソース", md)

    def test_three_or_less_not_folded(self):
        md = _md(sources=[{"name": "s", "url": "https://example.com/"}])
        self.assertNotIn("sources-more", md)


if __name__ == "__main__":
    unittest.main()
