"""omcha-ops#19 P1 — Amazon リンクのタグ強制 (prevent + catch) の回帰テスト。

#5087 で「build_post.py が組み立てるリンク」は SSOT 化されたが、
**データに入っていた URL をそのまま出力する経路** が残っていた:

  - post.md.j2 の price-card (``product.prices.amazon.url``) / competitor
    カード (``c.url``) / 販売終了時の ``search_url``
  - Jules が本文に直接書いた素の Amazon リンク
  - build_price_dashboard.py / build_feature_lists.py が data に書いた
    ``amazon_url`` を href にする一覧系 partial

2026-08-28 の配信物実測で、Amazon リンク 18,021 本のうち 92 本がタグ無し、
89 本が omcha.jp 側の ID (別サイトへ計上) だった。

このテストが守るもの:
  1. ``_force_amazon_partner_tag`` がタグ無し・別 ID・壊れた URL のいずれも
     SSOT の値に揃える (かつ正しい URL は書き換えない)
  2. ``check_affiliate_tags`` が配信物からタグ無し・別 ID を検出する
"""
from __future__ import annotations

import pathlib
import sys
import unittest

THIS_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = THIS_DIR.parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from build_post import _force_amazon_partner_tag  # noqa: E402
import check_affiliate_tags  # noqa: E402

TAG = "chk01-22"


class ForceAmazonPartnerTagTest(unittest.TestCase):
    def _one(self, url: str) -> str:
        return _force_amazon_partner_tag(url, TAG)

    def test_appends_tag_when_missing(self):
        self.assertEqual(
            self._one("https://www.amazon.co.jp/dp/B0BSH34YR8/"),
            f"https://www.amazon.co.jp/dp/B0BSH34YR8/?tag={TAG}",
        )

    def test_appends_tag_without_trailing_slash(self):
        self.assertEqual(
            self._one("https://www.amazon.co.jp/dp/B0BSH34YR8"),
            f"https://www.amazon.co.jp/dp/B0BSH34YR8?tag={TAG}",
        )

    def test_keeps_correct_tag_untouched(self):
        url = f"https://www.amazon.co.jp/dp/B0BSH34YR8/?tag={TAG}"
        self.assertEqual(self._one(url), url)

    def test_rewrites_foreign_tag(self):
        # #5087 より前のデータに残っていた omcha.jp 側の ID。navi のページから
        # 出たクリックが別サイトに計上されるので SSOT へ寄せる。
        self.assertEqual(
            self._one("https://www.amazon.co.jp/dp/B0BSH34YR8/?tag=other-site-22"),
            f"https://www.amazon.co.jp/dp/B0BSH34YR8/?tag={TAG}",
        )

    def test_keeps_fragment_after_query(self):
        self.assertEqual(
            self._one("https://www.amazon.co.jp/dp/B0BSH34YR8/#customerReviews"),
            f"https://www.amazon.co.jp/dp/B0BSH34YR8/?tag={TAG}#customerReviews",
        )

    def test_joins_existing_query_with_ampersand(self):
        self.assertEqual(
            self._one("https://www.amazon.co.jp/s?k=magnet+block"),
            f"https://www.amazon.co.jp/s?k=magnet+block&tag={TAG}",
        )

    def test_handles_url_ending_with_question_mark(self):
        # Jules が本文に書いた壊れた markdown link: [名前](.../dp/ASIN? タイトル)
        self.assertEqual(
            self._one("https://www.amazon.co.jp/dp/B07C2YDD7S?"),
            f"https://www.amazon.co.jp/dp/B07C2YDD7S?tag={TAG}",
        )

    def test_does_not_swallow_sentence_punctuation(self):
        got = self._one("詳細は https://www.amazon.co.jp/dp/B0BSH34YR8/ で確認。")
        self.assertEqual(
            got, f"詳細は https://www.amazon.co.jp/dp/B0BSH34YR8/?tag={TAG} で確認。"
        )

    def test_leaves_non_amazon_and_image_cdn_urls_alone(self):
        text = (
            'https://www.rakuten.co.jp/x/ '
            'https://images-na.ssl-images-amazon.com/images/P/B0BSH34YR8.09.LZZZZZZZ.jpg '
            'https://completion.amazon.co.jp/api/2017/suggestions'
        )
        self.assertEqual(self._one(text), text)

    def test_rewrites_every_link_in_a_document(self):
        html = (
            '<a href="https://www.amazon.co.jp/dp/A/">a</a>'
            '<a href="https://www.amazon.co.jp/dp/B/?tag=other-site-22">b</a>'
            f'<a href="https://www.amazon.co.jp/dp/C/?tag={TAG}">c</a>'
        )
        got = _force_amazon_partner_tag(html, TAG)
        self.assertEqual(got.count(f"tag={TAG}"), 3)
        self.assertNotIn("other-site-22", got)

    def test_empty_tag_is_a_hard_failure(self):
        with self.assertRaises(RuntimeError):
            _force_amazon_partner_tag("https://www.amazon.co.jp/dp/A/", "")


class CheckAffiliateTagsTest(unittest.TestCase):
    def _write(self, tmp: pathlib.Path, name: str, body: str) -> None:
        p = tmp / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")

    def test_detects_missing_and_foreign_tags(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._write(
                root,
                "products/a/index.html",
                f'<a href="https://www.amazon.co.jp/dp/A/?tag={TAG}">ok</a>'
                '<a href="https://www.amazon.co.jp/dp/B/">missing</a>'
                '<a href="https://www.amazon.co.jp/dp/C/?tag=other-site-22">foreign</a>'
                '<img src="https://images-na.ssl-images-amazon.com/images/P/A.jpg">'
                '<a href="https://www.rakuten.co.jp/x/">other ec</a>',
            )
            total, violations = check_affiliate_tags.scan(root, TAG)
            self.assertEqual(total, 3)
            reasons = sorted(v[2] for v in violations)
            self.assertEqual(len(violations), 2)
            self.assertTrue(any(r.startswith("no tag=") for r in reasons))
            self.assertTrue(any("other-site-22" in r for r in reasons))

    def test_tag_inside_fragment_is_a_violation(self):
        # `#customerReviews?tag=...` は Amazon に tag が届かない。文字列に
        # `tag=` が含まれるだけで合格にしていると、この個体を見逃す。
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._write(
                root,
                "index.html",
                f'<a href="https://www.amazon.co.jp/dp/A/#customerReviews?tag={TAG}">x</a>',
            )
            total, violations = check_affiliate_tags.scan(root, TAG)
            self.assertEqual(total, 1)
            self.assertEqual(len(violations), 1)
            self.assertIn("no tag=", violations[0][2])

    def test_clean_tree_has_no_violations(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._write(
                root,
                "index.html",
                f'<a href="https://www.amazon.co.jp/dp/A/?tag={TAG}">ok</a>',
            )
            total, violations = check_affiliate_tags.scan(root, TAG)
            self.assertEqual((total, violations), (1, []))


class AffiliateUrlMacroTest(unittest.TestCase):
    """post.md.j2 の affiliate_url マクロ (sources 一覧のリンクを組む側)。"""

    def _render(self, url: str) -> str:
        import jinja2

        env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(SCRIPTS_DIR / "templates")),
            keep_trailing_newline=False,
        )
        src = (SCRIPTS_DIR / "templates" / "post.md.j2").read_text(encoding="utf-8")
        start = src.index("{%- macro affiliate_url(url) -%}")
        end = src.index("{%- endmacro -%}", start) + len("{%- endmacro -%}")
        tmpl = env.from_string(src[start:end] + "{{ affiliate_url(url) }}")
        return tmpl.render(url=url, amazon_partner_tag=TAG)

    def test_query_goes_before_the_fragment(self):
        self.assertEqual(
            self._render("https://www.amazon.co.jp/dp/A/#customerReviews"),
            f"https://www.amazon.co.jp/dp/A/?tag={TAG}#customerReviews",
        )

    def test_plain_url_gets_the_tag(self):
        self.assertEqual(
            self._render("https://www.amazon.co.jp/dp/A/"),
            f"https://www.amazon.co.jp/dp/A/?tag={TAG}",
        )

    def test_existing_query_is_joined_with_ampersand(self):
        self.assertEqual(
            self._render("https://www.amazon.co.jp/s?k=x"),
            f"https://www.amazon.co.jp/s?k=x&tag={TAG}",
        )

    def test_non_amazon_url_is_passed_through(self):
        url = "https://www.rakuten.co.jp/x/"
        self.assertEqual(self._render(url), url)


class PartnerTagSsotAssertTest(unittest.TestCase):
    """secret と commit 済み SSOT の突き合わせ (fetch_amazon の書き込み側ガード)。"""

    def _config(self, tmp: pathlib.Path, tag_line: str) -> pathlib.Path:
        p = tmp / "config.toml"
        p.write_text(
            'baseURL = "https://navi.omcha.jp/"\n[params]\n' + tag_line,
            encoding="utf-8",
        )
        return p

    def test_matching_tag_passes(self):
        import tempfile

        from fetch_amazon import _assert_partner_tag_matches_ssot

        with tempfile.TemporaryDirectory() as td:
            cfg = self._config(pathlib.Path(td), f'  amazonPartnerTag = "{TAG}"\n')
            _assert_partner_tag_matches_ssot(TAG, cfg)  # does not raise

    def test_mismatched_tag_raises(self):
        import tempfile

        from fetch_amazon import _assert_partner_tag_matches_ssot

        with tempfile.TemporaryDirectory() as td:
            cfg = self._config(pathlib.Path(td), f'  amazonPartnerTag = "{TAG}"\n')
            with self.assertRaises(RuntimeError):
                _assert_partner_tag_matches_ssot("other-site-22", cfg)

    def test_missing_ssot_raises(self):
        import tempfile

        from fetch_amazon import _assert_partner_tag_matches_ssot

        with tempfile.TemporaryDirectory() as td:
            cfg = self._config(pathlib.Path(td), "")
            with self.assertRaises(RuntimeError):
                _assert_partner_tag_matches_ssot(TAG, cfg)


if __name__ == "__main__":
    unittest.main()
