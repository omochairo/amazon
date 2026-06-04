#!/usr/bin/env python3
"""SNS 投稿コピーの pre-fetch store 読み出し (#1580 Part 2)。

商品 SNS 投稿 (X / Threads) の本文 hook は従来 notify_buffer / notify_threads が
`meta_description` / `title` を機械的に切り詰めるだけだった。Part 2 では別 workflow
が「知育 × 価格」上位の未投稿 ASIN に対し Jules で訴求文を **事前生成** し、ここで
読む store に置く。store にコピーが有れば notify_* がそれを hook に使い、無ければ
従来 fallback。Jules は非同期 (session ベース) で publish cron をブロックできない
ため、配信時刻に間に合わせる手段は「事前生成 + store」に限られる。

store の置き場所は article 本体 (data/articles/) とは **別ディレクトリ**:
    data/sns_copy/<ASIN>.json
article 本体は Jules 記事再生成で頻繁に上書きされ衝突源になりやすいので分離する
(third_party_sources.json の pre-fetch store と同方針)。別 dir なのでサイドカー
shadow trap (.quality/.enrichment/.seo.json) の影響は受けない。

ファイル schema (全フィールド任意 — 欠落・空文字は「コピー無し」扱いで fallback):
    {
      "asin": "B0XXXXXXXX",
      "x":       "X 用 hook (URL を含まない本文。280 weighted char 以内推奨)",
      "threads": "Threads 用 hook (URL を含まない本文。480 char 以内推奨)",
      "rationale": "選定理由メモ (知育 × 価格)。生成側のトレース用、配信には未使用",
      "source": "jules",
      "generated_at": "2026-06-04T00:00:00Z"
    }

x / threads の片方しか無くても良い (有る側だけ store コピーを使う)。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STORE_DIR = REPO_ROOT / "data" / "sns_copy"

# 配信が扱う platform key。store ファイル内のキー名と一致させる。
PLATFORMS = ("x", "threads")
_ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")


def _store_path(asin: str) -> Path:
    return STORE_DIR / f"{asin}.json"


def load_copy(asin: str) -> dict | None:
    """data/sns_copy/<ASIN>.json を読む。無い / 壊れている / ASIN 不正なら None。

    例外は投げない — store は配信のオプション層であり、欠落・破損で配信全体を
    止めてはならない (graceful degrade して従来 fallback に任せる)。
    """
    if not isinstance(asin, str):
        return None
    asin_upper = asin.strip().upper()
    if not _ASIN_RE.match(asin_upper):
        return None
    path = _store_path(asin_upper)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def get_hook(asin: str, platform: str) -> str | None:
    """指定 platform ("x" / "threads") の hook 本文を返す。無ければ None。

    store ファイルが有っても該当 platform キーが欠落・空文字・非文字列なら None を
    返し、呼び出し側は従来の meta_description / title fallback に落ちる。
    """
    if platform not in PLATFORMS:
        return None
    data = load_copy(asin)
    if data is None:
        return None
    value = data.get(platform)
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


if __name__ == "__main__":  # 手元確認用: python scripts/sns_copy_store.py B0XXXXXXXX
    import sys

    if len(sys.argv) < 2:
        print("usage: python scripts/sns_copy_store.py <ASIN>", file=sys.stderr)
        sys.exit(2)
    target = sys.argv[1]
    for p in PLATFORMS:
        print(f"[{p}] {get_hook(target, p)!r}")
