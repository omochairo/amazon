"""マーカーで「自分の状態 issue」を特定するレーン共通の照合 (#6622)。

## なぜ要るか

複数のレーンが「本文にマーカーを埋めた issue を 1 件だけ upsert する」流儀で
書かれていて、どれも探し方が同じだった:

    query = f'repo:{repo} is:issue is:open in:body "{MARKER}"'
    items = json.loads(out).get("items", [])
    return items[0]["number"] if items else None

これには 2 つ穴がある。

**1. 検索しているのはマーカーではない。** 本文に埋めているのは
`<!-- delivery-freshness-monitor -->` のような HTML コメントだが、検索語は裸の
`delivery-freshness-monitor`。GitHub の検索はトークン分割するので、
**そのレーン名に言及しただけの issue が全部ヒットする。**

**2. 先頭 1 件を無条件に採用している。** 検索結果は「候補」でしかないのに、
検証せずに自分のものとして扱う。

2026-09-06、`53-origin-failover` がこの経路で無関係な epic (#6602) を掴んで
close した。close より怖いのは update で、body を丸ごと差し替えるレーンでは
**他人の issue が中身ごと消える**。

## 使い方

検索や label で候補を集めたら、採用の直前に必ずここを通す:

    from scripts._marker_issue import verified_matches

    items = json.loads(out).get("items", [])
    matches = verified_matches(items, MARKER)
    number = matches[0]["number"] if matches else None

「複数あったとき何をするか」はレーンごとに違う (監視系は重複起票を避けたい、
failover は誤爆を避けたい) ので、ここでは決めない。**検証済みを番号昇順で
返すところまで**が共通の仕事。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence


def marker_comment(marker: str) -> str:
    """本文に埋める / 本文から探すマーカーの実体。

    裸の ``marker`` を本文検索に使わないこと。それが #6622 の本体。
    """
    return "<!-- {} -->".format(marker)


def verified_matches(
    items: Sequence[Dict[str, Any]], marker: str,
) -> List[Dict[str, Any]]:
    """マーカーコメントを**実際に本文へ持っている** issue だけを番号昇順で返す。

    ``items`` は search API / ``gh issue list --json number,body`` のどちらの
    形でもよい (``number`` と ``body`` があればいい)。``body`` が取れていない
    要素は**採用しない** — 「確認できなかった」を「一致した」に倒さない。
    """
    needle = marker_comment(marker)
    out = [
        i for i in items
        if isinstance(i, dict) and isinstance(i.get("body"), str) and needle in i["body"]
    ]
    # 番号昇順。同じ入力なら毎回同じものを選ぶ (検索の並び順に依存しない)
    return sorted(out, key=lambda i: i.get("number", 0))


def sole_match(
    items: Sequence[Dict[str, Any]], marker: str,
) -> Optional[Dict[str, Any]]:
    """検証済みが 1 件だけならそれを返す。0 件でも複数でも None。

    「どれか分からないなら触らない」を選ぶレーン向け。
    """
    matches = verified_matches(items, marker)
    return matches[0] if len(matches) == 1 else None
