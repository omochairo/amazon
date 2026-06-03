#!/usr/bin/env python3
"""X (Twitter) weighted character count の共有ヘルパ。

twitter-text の DEFAULT weighted length 仕様に基づき、CJK / emoji を 2、
Latin / 一部 punct を 1 として数える。X の 280 文字制限はこの weighted 値で
評価されるため、純粋な `len()` 切りだと日本語本文が実質 2 倍に数えられて
配信前に弾かれる ([[feedback-omochairo-x-cjk-weight]])。

もともと scripts/notify_buffer.py 内に定義されていたが、notify_engagement.py
の X 直ポスト (Buffer shareNow) 経路でも weighted 判定が必須になったため
共有モジュールに切り出した (session 95)。

直接実行されるスクリプト (`python scripts/notify_buffer.py`) からは
`scripts/` が sys.path[0] になるので `from _x_text import ...` で import 可能。
"""
from __future__ import annotations

# X の単発投稿上限 (weighted)
X_LIMIT = 280


def x_weight(c: str) -> int:
    """1 文字の weighted character count。CJK / emoji は 2、Latin / punct 系は 1。

    参考: twitter-text の DEFAULT ranges。U+0000-U+10FF と一部 punct
    (U+2000-U+200D / U+2010-U+201F / U+2032-U+2037) が weight 1。それ以外は 2。
    """
    cp = ord(c)
    if cp <= 0x10FF:
        return 1
    if 0x2000 <= cp <= 0x200D:
        return 1
    if 0x2010 <= cp <= 0x201F:
        return 1
    if 0x2032 <= cp <= 0x2037:
        return 1
    return 2


def weighted_len(s: str) -> int:
    """文字列全体の weighted character count。"""
    return sum(x_weight(c) for c in s)


def truncate_to_weight(s: str, max_weight: int) -> tuple[str, bool]:
    """s を weight max_weight 以下に切る。切ったかどうかも返す。"""
    weight = 0
    out = []
    for c in s:
        w = x_weight(c)
        if weight + w > max_weight:
            return "".join(out), True
        out.append(c)
        weight += w
    return s, False
