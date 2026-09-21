"""official_howto.py

#7955 (#2686 設計2 改訂版) — 公式の取扱説明書・あそびかた (``official_howto.json``)
の読み込みと、「手順を約束する表現」の判定を 1 か所に集める。

呼び出し元:
  - ``quality_gate.check_howto_title_promise`` (#7957 A-1: タイトルゲート)
  - ``build_post`` (#7957 A-4: 手順を問う FAQ をビルド時に落とす)
  - ``generate_faq_seo`` (#7957 A-4: 手順を問う FAQ を生成時に落とす)

``data/raw/per_asin/<ASIN>/official_howto.json`` は #7958 (B) が ``url`` を、
#7960 (D) が ``steps`` を書く。このモジュールは読むだけ。

なぜ要るか: 全ページが「実際の使い方・遊び方」見出しを持ち、298 本のタイトルが
「遊び方」を約束していたが、どれも手順を持っていなかった (2026-09-21 実測)。
B0H4PQ29JS (デジヴァイス) はタイトルで「遊び方」を約束して手順ゼロのまま、
CTR を記録した後に順位を落とした。約束してよいのは公式の答えがあるページだけ。
"""
from __future__ import annotations

import json
import pathlib
import re
from typing import Any

# タイトル・FAQ で「手順を教える」と読める語。「電池」「対象年齢」のような事実の
# 問いは含めない (FAQ サイドカーが事実として答えられている。#7955)。
HOWTO_PROMISE_WORDS: tuple[str, ...] = (
    "遊び方", "あそびかた", "使い方", "つかいかた", "やり方",
    "説明書", "取説", "取扱説明", "ルール", "作り方", "組み立て方", "操作方法",
)

_HOWTO_PROMISE_RE = re.compile("|".join(re.escape(w) for w in HOWTO_PROMISE_WORDS))


def find_howto_promise(text: Any) -> str | None:
    """text に手順を約束する語があれば最初の 1 語を返す。"""
    if not isinstance(text, str):
        return None
    m = _HOWTO_PROMISE_RE.search(text)
    return m.group(0) if m else None


def load(asin: str | None, per_asin_root: pathlib.Path | str = "data/raw/per_asin") -> dict | None:
    """``official_howto.json`` を読む。無い・壊れている・dict でなければ None。"""
    if not asin or not isinstance(asin, str):
        return None
    path = pathlib.Path(per_asin_root) / asin.strip().upper() / "official_howto.json"
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def has_official_url(obj: dict | None) -> bool:
    """公式の取説・あそびかたの URL を取れているか (#7958 が書く)。"""
    if not isinstance(obj, dict):
        return False
    url = obj.get("url")
    return isinstance(url, str) and url.startswith("https://")


def has_reviewed_steps(obj: dict | None) -> bool:
    """人が読んだ手順の要約があるか (#7960 が書く)。

    ``steps`` が非空リストで、かつ ``reviewed_by`` が入っているときだけ True。
    機械検査を通っただけの要約は公開しない (#7955 の規律)。
    """
    if not has_official_url(obj):
        return False
    steps = obj.get("steps")
    reviewed = obj.get("reviewed_by")
    return (
        isinstance(steps, list) and len(steps) > 0
        and isinstance(reviewed, str) and reviewed.strip() != ""
    )


def _question_text(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    q = item.get("question") or item.get("q") or ""
    return q if isinstance(q, str) else ""


def filter_howto_faq(items: Any, obj: dict | None) -> tuple[list, int]:
    """手順を問う FAQ を、手順の要約が無いページでは落とす。

    戻り値は (残した items, 落とした件数)。items がリストでなければ
    そのまま (items, 0) を返す (呼び出し側の型を壊さない)。
    """
    if not isinstance(items, list):
        return items, 0
    if has_reviewed_steps(obj):
        return items, 0
    kept = [it for it in items if find_howto_promise(_question_text(it)) is None]
    return kept, len(items) - len(kept)
