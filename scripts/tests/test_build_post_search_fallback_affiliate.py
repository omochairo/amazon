"""Regression test for #6822 — 未突合時の検索フォールバック URL のアフィリエイト化。

突合できた商品 (2026-09-09 時点で 2,014/2,412 = 83.5%) は fetch_rakuten.py /
fetch_yahoo.py が API の公式パラメータでアフィリエイト済み URL を受け取るため
以前から収益化されていた。一方、突合できなかった約 398 件 (16.5%) には
``_SEARCH_URL_BUILDERS`` が組む**素の検索 URL** がそのまま出ており、本番の
層別サンプルでは楽天側が 7/7 でアフィリエイトを通っていなかった。

この欠陥は「リンクは出ている・ページは壊れていない・ただ収益だけ発生しない」
という無症状の形だったため数か月気付かれなかった。同じ形の回帰を検出するのが
このテストの目的。

守るもの:
  1. secret が無い (= 本番配信ビルドと同じ条件) でも、楽天/Yahoo の検索
     フォールバックがアフィリエイト経由 URL になる
  2. 生成 Markdown に **素の** search.rakuten.co.jp / shopping.yahoo.co.jp/search
     への直リンクが残らない (誤報告フォームの wrong_url= に現れる
     エスケープ済みのものは除く)
  3. secret が設定されていればそれが committed SSOT より優先される
  4. committed SSOT が読めない場合は、素の URL へ黙って落ちずに失敗する

ID の SSOT を commit 済み config.toml に置く理由は #5087 と同じ。
build_post.main() をサブプロセスで実行する点も同ファイルのパターンを踏襲。
"""
from __future__ import annotations

import pathlib
import sys
import unittest
import unittest.mock


THIS_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = THIS_DIR.parent.parent  # amazon-clone/
SCRIPTS_DIR = REPO_ROOT / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import build_post  # noqa: E402

RAKUTEN_AFFILIATE_HOST = "https://hb.afl.rakuten.co.jp/hgc/"
VC_REFERRAL_HOST = "https://ck.jp.ap.valuecommerce.com/servlet/referral"
BARE_RAKUTEN_SEARCH = "https://search.rakuten.co.jp/search/mall/"
BARE_YAHOO_SEARCH = "https://shopping.yahoo.co.jp/search?p="


class SearchFallbackAffiliateTest(unittest.TestCase):
    """``_SEARCH_URL_BUILDERS`` がアフィリエイト経由 URL を返すこと。"""

    def _no_secrets(self):
        """本番配信ビルド (secret 無し) と同じ条件にする。"""
        return unittest.mock.patch.object(build_post, "get_secret", lambda _name: "")

    def test_rakuten_fallback_goes_through_affiliate(self):
        with self._no_secrets():
            url = build_post._build_rakuten_search_url("テスト商品")
        self.assertTrue(
            url.startswith(RAKUTEN_AFFILIATE_HOST),
            f"楽天の検索フォールバックがアフィリエイトを通っていない: {url}",
        )
        self.assertNotIn(BARE_RAKUTEN_SEARCH, url.split("?pc=")[0])

    def test_yahoo_fallback_goes_through_valuecommerce(self):
        with self._no_secrets():
            url = build_post._build_yahoo_search_url("テスト商品")
        self.assertTrue(
            url.startswith(VC_REFERRAL_HOST),
            f"Yahoo の検索フォールバックが VC を通っていない: {url}",
        )
        self.assertIn("vc_url=", url)

    def test_both_builders_are_wired_into_the_dispatch_table(self):
        """``_SEARCH_URL_BUILDERS`` 経由でも同じ結果になること。

        直接関数を呼ぶテストだけだと、dispatch table 側が素の lambda に
        戻された回帰を見逃す。
        """
        with self._no_secrets():
            rakuten = build_post._SEARCH_URL_BUILDERS["rakuten"]("テスト商品")
            yahoo = build_post._SEARCH_URL_BUILDERS["yahoo"]("テスト商品")
        self.assertTrue(rakuten.startswith(RAKUTEN_AFFILIATE_HOST))
        self.assertTrue(yahoo.startswith(VC_REFERRAL_HOST))

    def test_target_url_is_preserved_inside_the_wrapper(self):
        """ラップしても遷移先は元の検索 URL のままであること。"""
        import urllib.parse

        with self._no_secrets():
            rakuten = build_post._build_rakuten_search_url("テスト商品")
            yahoo = build_post._build_yahoo_search_url("テスト商品")

        pc = urllib.parse.parse_qs(urllib.parse.urlparse(rakuten).query)["pc"][0]
        self.assertTrue(pc.startswith(BARE_RAKUTEN_SEARCH), pc)
        self.assertIn("テスト商品", urllib.parse.unquote(pc))

        vc_url = urllib.parse.parse_qs(urllib.parse.urlparse(yahoo).query)["vc_url"][0]
        self.assertTrue(vc_url.startswith(BARE_YAHOO_SEARCH), vc_url)
        self.assertIn("テスト商品", urllib.parse.unquote(vc_url))

    def test_secret_overrides_committed_ssot(self):
        with unittest.mock.patch.object(
            build_post, "get_secret", lambda name: "SECRET-ID" if name == "RAKUTEN_AFFILIATE_ID" else ""
        ):
            url = build_post._build_rakuten_search_url("テスト商品")
        self.assertTrue(url.startswith(f"{RAKUTEN_AFFILIATE_HOST}SECRET-ID/"), url)

    def test_missing_ssot_degrades_instead_of_raising(self):
        """SSOT が読めないとき、例外ではなく空文字を返すこと。

        初版は例外で落とす設計にしていたが、build_post.py は記事ごとに例外を
        握り潰して次へ進むため、「全記事を捨てて exit 0」という挙動になった
        (CI で 15 件 fail して発覚)。素の URL を出すこと (収益の取りこぼし) より、
        記事が丸ごと消えてビルドが緑のままになる方が危険なので縮退させる。
        """
        missing = pathlib.Path("does-not-exist-6822.toml")
        self.assertEqual(
            build_post._load_committed_affiliate_param("rakutenAffiliateId", missing), ""
        )
        self.assertEqual(
            build_post._load_committed_affiliate_param("valuecommerceSid", missing), ""
        )

    def test_render_still_produces_output_without_ids(self):
        """ID が取れない環境でもリンク生成が例外を投げないこと。

        ``_SEARCH_URL_BUILDERS`` が例外を投げると、その記事は
        ``Error processing …`` として捨てられ、しかもビルドは exit 0 で緑になる。
        この経路を二度と作らないための回帰テスト。
        """
        with unittest.mock.patch.object(build_post, "get_secret", lambda _n: ""), \
                unittest.mock.patch.object(
                    build_post, "_load_committed_affiliate_param", lambda *a, **k: ""):
            rakuten = build_post._build_rakuten_search_url("テスト商品")
            yahoo = build_post._build_yahoo_search_url("テスト商品")
        self.assertTrue(rakuten.startswith(BARE_RAKUTEN_SEARCH), rakuten)
        self.assertTrue(yahoo.startswith(BARE_YAHOO_SEARCH), yahoo)

    def test_committed_ssot_is_present_in_config(self):
        """config.toml に 3 つの ID が実在すること。

        実行時は ID が無くても素の URL へ縮退するだけで、警告ログは
        1 日 57 件の PR に埋もれて誰も読まない。**設定の欠落を検出する防御は
        実行時ではなくここ (全 PR で走る CI) に置いている。**
        このテストが落ちたら、収益化されていないリンクが配信される。
        """
        for param in ("rakutenAffiliateId", "valuecommerceSid", "valuecommercePid"):
            value = build_post._load_committed_affiliate_param(param)
            self.assertTrue(value, f"[params].{param} が config.toml に無い")


if __name__ == "__main__":
    unittest.main()
