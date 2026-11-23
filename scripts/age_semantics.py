"""対象年齢の読み取りで、数字以外の手がかりを扱う共有ルール (#9186)。

build_post.py (front matter の age_min_months) と build_feature_lists.py
(特集リスト・価格ダッシュボード・年齢別 hub) の両方から使う。2 つの読み取り
処理で結果が食い違わないよう、規則はここにだけ書く。

- min_months_from_words: 数字の無い年齢表記 (「小学生以上」「大人向け」) を月齢にする
- is_seasonal_decoration: 節句・正月の飾り物か。「0歳〜」は遊ぶ年齢ではなく初節句を
  迎える赤ちゃんを指すので、対象年齢を持たない (年齢で絞る機能に出さない) 扱いにする
- override_age_months: data/age_overrides.json に人が確かめて書いた対象年齢。
  記事 json の年齢表記より優先する
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

AGE_OVERRIDES_PATH = Path(__file__).resolve().parent.parent / "data" / "age_overrides.json"

# 18 歳。これ以上は大人向け (年齢別 hub・診断の対象外)。
ADULT_AGE_MONTHS = 216

# 数字の無い表記だけに使う (数字があればそちらを優先する)。複数当たれば最小を取る
# (「幼児〜小学生」は 36)。「子供から大人まで」は大人向けとは読まない。
_AGE_WORDS: tuple[tuple[re.Pattern[str], int], ...] = (
    (re.compile(r"大人向け|大人用|成人向け"), ADULT_AGE_MONTHS),
    (re.compile(r"高校生(?!未満)"), 180),
    (re.compile(r"中学生(?!未満)"), 144),
    (re.compile(r"小学生(?!未満)"), 72),
    (re.compile(r"幼児(?!未満)"), 36),
)

# 商品名で判定する。Amazon のカテゴリ (兜飾り・こいのぼり・つるし雛 等) は、
# 「正月飾り」に福笑い、「季節用品」に水鉄砲のような遊び道具も入っているので使わない。
# 「ひな祭り」の工作キットや福笑い・かるた・こまは遊び道具なので当てない。
_SEASONAL_DECORATION_RE = re.compile(
    r"五月人形|兜飾り|鎧飾り|雛飾り|雛人形|ひな人形|つるし雛|つるし飾り"
    r"|こいのぼり|鯉のぼり|鯉飾り|鏡餅|陣羽織|破魔弓|弓太刀|節句飾り"
    r"|飾り.{0,3}羽子板|羽子板飾り|しめ飾り|門松"
)


def min_months_from_words(text: Any) -> int | None:
    """数字の無い年齢表記から最小月齢を読む。読めなければ None。"""
    if not text:
        return None
    s = str(text)
    found = [months for rx, months in _AGE_WORDS if rx.search(s)]
    return min(found) if found else None


def is_seasonal_decoration(*names: Any) -> bool:
    """商品名 (name / name_full 等) のどれかが節句・正月の飾り物を指しているか。"""
    return any(n and _SEASONAL_DECORATION_RE.search(str(n)) for n in names)


@lru_cache(maxsize=None)
def _load_overrides(path: str) -> dict[str, int]:
    p = Path(path)
    if not p.exists():
        return {}
    data = json.loads(p.read_text(encoding="utf-8"))
    out: dict[str, int] = {}
    for asin, entry in (data.get("overrides") or {}).items():
        months = entry.get("age_min_months") if isinstance(entry, dict) else None
        # 出典の無い値は使わない (推測で埋めないための約束。#9186)
        if isinstance(months, int) and months >= 0 and entry.get("source"):
            out[str(asin).upper()] = months
    return out


def override_age_months(raw: dict[str, Any], path: Path | None = None) -> int | None:
    """記事 json の ASIN に、data/age_overrides.json の確認済み年齢があれば返す。"""
    product = raw.get("product") or {}
    asin = product.get("asin") if isinstance(product, dict) else None
    if not asin:
        return None
    return _load_overrides(str(path or AGE_OVERRIDES_PATH)).get(str(asin).upper())


# FAQ の答えで「公式の対象年齢の記載は無い」と言っている文 (#9217)。
# 例: 「明確な対象年齢の記載はありませんが」「公式の対象年齢は明記されていませんが」
#     「対象年齢は特に明記されていませんが」「明確な対象年齢の下限は記載されていませんが」
# 誤検知よけ: 「上限年齢の記載はありません」(下限は書いてある) は拾わない。主語と「記載」の
# 間に数字があれば年齢を言い切っている文 (「3歳以上と明記されており、…ありません」) なので
# 拾わない。「記載」と否定の間に読点を挟ませない (「記載があり、問題はありません」)。
_NO_OFFICIAL_AGE_RE = re.compile(
    r"(?<!上限)(?<!下限)(対象年齢|推奨年齢|年齢)(?!別)[^。\d０-９]{0,20}"
    r"(記載|明記|表記|公表|明示|設定|指定|定め)[^。、]{0,10}"
    r"(なし|無し|ありません|ございません|されていません|されておらず|されていない|がない|が無い|はない|は無い|はなく)"
)


def faq_says_no_official_age(raw: dict[str, Any]) -> bool:
    """記事 json の FAQ (faq / faq_extended) が「公式の対象年齢の記載は無い」と答えているか。"""
    for key in ("faq", "faq_extended"):
        for item in raw.get(key) or []:
            if isinstance(item, dict) and _NO_OFFICIAL_AGE_RE.search(str(item.get("answer") or "")):
                return True
    return False


def months_label(months: int) -> str:
    """月齢を「6歳以上」「18ヶ月以上」の表記にする。"""
    if months % 12 == 0:
        return f"{months // 12}歳以上"
    return f"{months}ヶ月以上"


def override_age_label(raw: dict[str, Any], path: Path | None = None) -> str | None:
    """data/age_overrides.json の確認済み年齢を表示用の文字列で返す (無ければ None)。

    override は age_min_months (一覧・年齢で絞る機能) だけでなく、記事ページの
    「対象年齢」の表示と知育スコアにも効かせる (#9217。片方だけ直っていた)。
    """
    months = override_age_months(raw, path)
    return months_label(months) if months is not None else None


def age_is_estimate(raw: dict[str, Any], path: Path | None = None) -> bool:
    """記事 json の対象年齢が、メーカーの表記ではなく生成時の目安か (#9217)。

    FAQ が「公式の記載は無い」と答えていて、data/age_overrides.json に出典付きの
    値も無いとき真。真なら、ページの「対象年齢」は目安と書き、知育スコアも明記扱い
    にしない (推測で埋めないための約束。#9186)。
    """
    return override_age_months(raw, path) is None and faq_says_no_official_age(raw)


def seasonal_names(raw: dict[str, Any]) -> tuple[Any, ...]:
    """記事 json から、is_seasonal_decoration に渡す商品名を集める。"""
    product = raw.get("product") or {}
    if not isinstance(product, dict):
        product = {}
    return (product.get("name"), product.get("name_full"), raw.get("product_name"))
