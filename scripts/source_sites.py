#!/usr/bin/env python3
"""出典の「サイト」と種別の判定を 1 箇所に集める (navi-brain#92 案 C)。

品質ゲート (quality_gate.check_sources_v5) と生成前の判定
(score_per_asin_info.awaiting_sources) は、どちらも「非販売の出典が別々のサイトで
2 件以上あるか」を数える。数え方が両者で食い違うと、生成前に「揃った」と判定して
生成に回した記事が gate で落ちる (#9239 の 5 件問題と同じ形) ので、ここに寄せる。

## host ではなくサイト (登録ドメイン) で数える理由

host 単位だと ja.wikipedia.org と en.wikipedia.org、toy.bandai.co.jp と bandai.co.jp
が別々に数えられ、同じ運営者のページ 2 枚で「独立した 2 件」になっていた
(新条件で落ちる記事の主な原因の 1 つ)。

Public Suffix List は持たない (依存を増やさない)。日本の属性型 (co.jp 等) と、
主要国の 2 段 suffix、サブドメインごとに書き手が違うブログサービスだけを持つ。
ここに無い 2 段 suffix は 1 段上でまとめられる = 少なめに数える側に倒れる。

## SNS はまとめて 1 サイト

YouTube・X・Instagram 等は誰でも投稿でき、第三者の裏付けとしては弱い。
SNS 同士 (YouTube 2 本、YouTube + X) は合わせて 1 サイトに数える。
"""

from __future__ import annotations

import urllib.parse

# 通販サイト (モール含む)。商品ページは「その店が売っている」記載で、第三者の評価では
# ない。host の完全一致か、その subdomain に当てる (部分一致にすると無関係の実在
# ドメインまで落とす。#6593 の notomcha.jp と同じ罠)。
#
# 以前は score_per_asin_info._RETAIL_SITE_HOSTS (#9239) にあった。gate も同じ集合で
# 「非販売に数えない」を判定するのでここへ移した。
RETAIL_SITE_HOSTS = frozenset({
    "amazon.co.jp", "amazon.com", "rakuten.co.jp", "shopping.yahoo.co.jp",
    "mercari.com", "qoo10.jp", "wowma.jp",
    "yodobashi.com", "biccamera.com", "yamada-denkiweb.com", "askul.co.jp",
    "lohaco.yahoo.co.jp", "monotaro.com", "kaunet.com", "dcm-ekurashi.com",
    "joshinweb.jp", "edion.com", "kojima.net", "nojima.co.jp", "ksdenki.com",
    "sofmap.com", "toysrus.co.jp", "aeonretail.com", "irisplaza.co.jp",
    "cainz.com", "hands.net", "amiami.jp", "happinetonline.com", "giftmall.co.jp",
    "superdelivery.com", "as-1.co.jp", "furusato-tax.jp", "pmall.gpoint.co.jp",
    "paypayfleamarket.yahoo.co.jp", "auctions.yahoo.co.jp", "creema.jp", "minne.com",
    "ebay.com", "walmart.com", "target.com", "etsy.com", "aliexpress.com",
    "temu.com", "shein.com",
})

# SNS / 動画共有。合わせて 1 サイトまでしか数えない。
SNS_HOSTS = frozenset({
    "youtube.com", "youtu.be", "x.com", "twitter.com", "instagram.com",
    "threads.com", "threads.net", "tiktok.com", "facebook.com",
})

# この 2 段の直下が 1 サイトになる suffix (3 段の suffix は持たない)。
_MULTI_LABEL_SUFFIXES = frozenset({
    # 日本の属性型
    "co.jp", "ne.jp", "or.jp", "ac.jp", "go.jp", "gr.jp", "ed.jp", "lg.jp", "ad.jp",
    # 主要国の 2 段 suffix
    "co.uk", "org.uk", "ac.uk", "com.au", "net.au", "org.au", "co.nz",
    "com.cn", "com.tw", "com.hk", "com.sg", "co.kr", "com.br", "co.in",
    # サブドメインごとに書き手が違うブログ・ホスティング
    "blogspot.com", "hatenablog.com", "hatenablog.jp", "hateblo.jp",
    "hatenadiary.com", "hatenadiary.jp", "github.io", "wordpress.com",
    "livedoor.blog", "blog.jp", "seesaa.net", "jugem.jp", "exblog.jp",
})

SNS_SITE = "(sns)"


def host_of(url: str) -> str:
    """URL から host を小文字で取り出す (www. とポートは落とす)。判定不能なら空文字。"""
    try:
        netloc = urllib.parse.urlsplit((url or "").strip()).netloc
    except ValueError:
        return ""
    host = netloc.lower().split("@")[-1].split(":")[0].rstrip(".")
    return host[4:] if host.startswith("www.") else host


def host_in(host: str, hosts: frozenset) -> bool:
    """host が集合のいずれかと一致するか、その subdomain か。"""
    return any(host == h or host.endswith("." + h) for h in hosts)


def registered_domain(host: str) -> str:
    """host の登録ドメイン (例: toy.bandai.co.jp → bandai.co.jp, ja.wikipedia.org → wikipedia.org)。"""
    host = (host or "").strip().lower().rstrip(".")
    labels = [p for p in host.split(".") if p]
    if len(labels) <= 2:
        return ".".join(labels)
    if ".".join(labels[-2:]) in _MULTI_LABEL_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def is_retail(host: str) -> bool:
    return host_in(host, RETAIL_SITE_HOSTS)


def is_sns(host: str) -> bool:
    return host_in(host, SNS_HOSTS)


def site_key(host: str) -> str:
    """出典を数えるときの「サイト」の鍵。SNS は全部同じ鍵になる。"""
    host = (host or "").strip().lower()
    if host.startswith("www."):
        host = host[4:]
    if is_sns(host):
        return SNS_SITE
    return registered_domain(host)
