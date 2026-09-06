"""_marked_issue.py

自動起票した Issue を「本文のマーカー」で確実に同定するための共通ヘルパ。

## なぜ必要か

各監視スクリプトは `gh api search/issues` に `in:body "<marker>"` を投げ、
返ってきた **items[0] をそのまま自分の Issue とみなして** title / body を
上書き (`gh issue edit`) したり close したりしている。

ところが GitHub の全文検索はハイフンや大小文字を正規化するので、
`in:body "origin-failover"` は本文に「origin failover」と**書いてあるだけ**の
無関係な Issue にヒットする。検索は絞り込みでしかない。

実害 (2026-09-02): 本文に「監視 53 (origin failover) との分担」と書いた
omochairo/amazon#6415 (待機系 GitLab Pages 1GiB の監視) が failover_dns に
自分の Issue と誤認され、起票 10 分後に自動 close された。close だけでは
済まない — 障害時は update_issue() が走るため、**無関係な Issue のタイトルと
本文を上書きしうる**。

## 使い方

起票する本文の先頭に `<!-- <marker> -->` を置き、検索結果は必ず
``find_marked_issue`` に通す。マーカーを含まない候補は捨てる。
"""
from __future__ import annotations

from typing import Any, Iterable, Optional


def marker_html(marker: str) -> str:
    """本文に埋める実体 (`<!-- marker -->`)。"""
    return "<!-- {} -->".format(marker)


def find_marked_issue(items: Iterable[dict[str, Any]],
                      marker: str) -> Optional[dict[str, Any]]:
    """検索結果から、本文にマーカーを含む最初の Issue を返す。無ければ None。

    検索のヒット順は信用しない。**本文に実体が入っていることだけが根拠。**
    """
    needle = marker_html(marker)
    for item in items or []:
        if needle in (item.get("body") or ""):
            return item
    return None
