"""Q2 (`include_domains`) のドメインリスト。着手前に固定し、実行後に足さない。

#4841 owner の V2 依頼上書き点2: 「ブログサービスのドメインだけでは、実際に
体験談を出している独自ドメインの個人ブログを取りこぼす。
`docs/experience-source-yield/v1_results.json` の `snippet_source_hosts` から
個人ブログのドメインも入れる」。

`snippet_source_hosts` で category="other" の8ホストを実際に fetch (2026-09-16、
`curl -sL -A "Mozilla/5.0"` でタイトル/リダイレクト先を確認) して判定した:

  - niko-shufublog.com  -> 個人ブログ (title「おでかけ暮らし」、ドメイン名に
    "shufublog" を含む主婦ブログ)                                    [採用]
  - oyakame.com         -> 個人ブログ (title「親かめブログ」)          [採用]
  - suisui-oekaki.com   -> **メーカー(パイロット)の商品公式サイト**
    (title「スイスイおえかき｜パイロットのおもちゃ」。個人ブログではない
    — V1 の "other" 分類は誤分類だが、分類表自体は変更しない指示のため
    ここでは Q2 の対象から外すだけに留める)                            [除外]
  - lettuceclub.net     -> メディアブランド (title「レタスクラブ」)     [除外]
  - mama-no-wa.jp       -> コミュニティ/メディアポータル
    (title「ママノワ」。個人が書いているブログではない)                [除外]
  - denkichi.com        -> 家電量販店 (title「デンキチWeb」)            [除外]
  - kids-world.com      -> EC (osCsid クッキー、welcome.php — osCommerce 系の
    通販サイト)                                                        [除外]
  - platetsu.com        -> プラレール情報サイトだが運営者情報が確認できず
    判定材料不足のため保留 (安全側で除外)                                [除外]

採用したのは niko-shufublog.com / oyakame.com の 2 件のみ。
"""
from __future__ import annotations

from scripts.analyze_third_party_yield import HOST_CATEGORIES

# 2026-09-16 に着手前で固定。実行後に追加しない。
PERSONAL_BLOG_DOMAINS: tuple[str, ...] = (
    "niko-shufublog.com",
    "oyakame.com",
)


def build_q2_include_domains() -> list[str]:
    """Q2 の `include_domains` (ブログサービスのドメイン + 個人ブログのドメイン)。"""
    return sorted({*HOST_CATEGORIES["blog"], *PERSONAL_BLOG_DOMAINS})
