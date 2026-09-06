#!/usr/bin/env python3
"""自社ドメインの判定を 1 箇所に集める (#6593)。

## なぜ共通化するか

外部の Web から素材や出典を集めるレーンは、**自社の記事を「第三者の情報」として
取り込んではいけない**。自分の書いたものを自分の根拠にする循環になり、記事が
増えて検索順位が上がるほど自己強化する。

この判定は 2026-09-06 時点で 2 箇所に別実装で存在していた:

- `fetch_third_party_sources._OWN_SITE_SUBSTR` … URL 全体の部分一致
- `mine_experience.is_self_domain` … host の suffix 一致 (#6592)

リストも突き合わせ方も違うので、片方を直してももう片方に伝わらない。ここに寄せる。

## 部分一致ではなく host の suffix 一致にした理由

URL 全体の部分一致だと `https://notomcha.jp/...` のような **無関係の実在ドメインを
誤って落とす** (`"omcha.jp" in "notomcha.jp"` は真)。host の suffix 一致なら
`notomcha.jp` は通り、`navi.omcha.jp` は落ちる — 意図どおりになる。
`omcha.jp.evil.com` のような詐称ドメインも「自社ではない」として通るが、
それは自己参照の循環とは別の問題なのでここでは扱わない。

代わりに、プロキシや転送で URL の中に自社 URL が埋まっている形
(`https://example.com/?u=https://navi.omcha.jp/...`) は**捕まえられない**。
第三者ソースの収集側には小売・検索エンジンの除外が別にあり、実測 (#6593 の点検)
でも該当は 0 件だったので、この取りこぼしは許容する。
"""

from __future__ import annotations

import urllib.parse

# 自社が運営するサイト。suffix 一致なので www. / navi. / home. の各サブドメインを覆う。
#   navi.omcha.jp … 知育玩具比較サイト (本リポジトリ)
#   omcha.jp      … おもちゃいろ (WordPress 本家)
#   home.omcha.jp … おうちいろ
SELF_DOMAIN_SUFFIXES: tuple[str, ...] = ("omcha.jp",)


def self_host(url: str) -> str:
    """URL から host を小文字で取り出す (ポートは落とす)。判定不能なら空文字。"""
    try:
        netloc = urllib.parse.urlsplit(url).netloc
    except ValueError:
        return ""
    return netloc.lower().split("@")[-1].split(":")[0]


def is_self_domain(url: str, suffixes: tuple[str, ...] = SELF_DOMAIN_SUFFIXES) -> bool:
    """自社サイトの URL か。

    外部から集めた素材・出典・引用元がこれに該当したら **必ず落とすこと**。
    「参考リンク」として意図的に自社を混ぜる用途 (内部リンクの
    `omcha_related.json` など) には使わない — あれは設計どおりの自己参照。
    """
    host = self_host(url)
    if not host:
        return False
    return any(host == d or host.endswith("." + d) for d in suffixes)
