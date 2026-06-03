"""build_brand_hub_stats.py

#731 Prep — ブランドハブ narrative / JSON-LD / hero 用の集計を出力する。

`data/articles/*.json` を `brand_normalizer.normalize()` の canonical 名で
グループ化し、ブランドごとに count / avg_ivs_100 / avg_age_min_months /
top_3_series / representative_asin / representative_image / lowest_price /
tier を `hugo/data/brand_hub.json` に書き出す。

記事数 < 3 のブランドは ``{"count": N}`` のみを出力する。template 側で
narrative を省略するシグナルとして使う。

CLI:
    python scripts/build_brand_hub_stats.py                       # 既定パス
    python scripts/build_brand_hub_stats.py --articles-dir X --out Y
"""
from __future__ import annotations

import argparse
import datetime as _dt
import glob
import json
import os
import pathlib
import re
from collections import Counter, defaultdict
from typing import Iterable

from brand_normalizer import normalize as normalize_brand

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_DEFAULT_ARTICLES = _REPO_ROOT / "data" / "articles"
_DEFAULT_OUT = _REPO_ROOT / "hugo" / "data" / "brand_hub.json"

NARRATIVE_MIN_COUNT = 3

GENERIC_TAG_STOPWORDS = {
    "プレゼント",
    "ギフト",
    "知育玩具",
    "知育",
    "おもちゃ",
    "玩具",
    "ブロック",
    "パズル",
    "カードゲーム",
    "ぬいぐるみ",
    "人形",
    # 2026-06-01 #731 Part A follow-up: Mamimami Home の top_3_series が
    # "出産祝い, 0歳, 1歳" になり narrative が generic tag を太字化した事例。
    # ライフイベント / 性別 / 「子供」系を除外し、年齢タグは下の正規表現で弾く。
    "出産祝い",
    "誕生日",
    "入園祝い",
    "入学祝い",
    "初節句",
    "ハーフバースデー",
    "クリスマス",
    "クリスマスプレゼント",
    "男の子",
    "女の子",
    "赤ちゃん",
    "子供",
    "子ども",
    "幼児",
    "乳児",
}

# 「0歳」「1歳半」「6ヶ月」等は商品シリーズではないので top_3_series から除外する。
_AGE_TAG_STOPWORD_RE = re.compile(r"^\d+\s*(?:歳(?:半)?|ヶ月|か月|カ月|ヵ月)$")

_AGE_RE_NEN = re.compile(r"(\d+)\s*歳(半)?")
_AGE_RE_MONTH = re.compile(r"(\d+)\s*(?:ヶ月|か月|カ月|ヵ月)")


def parse_age_min_months(age_range: str | None) -> int | None:
    """persona_fit.age_range から下限月齢 (整数) を返す。

    対応パターン: "3歳以上" / "5歳〜" / "1歳半〜" / "10ヶ月〜" / "0歳〜".
    解釈不能なら ``None``.
    """
    if not age_range:
        return None
    m = _AGE_RE_MONTH.search(age_range)
    if m:
        return int(m.group(1))
    m = _AGE_RE_NEN.search(age_range)
    if m:
        years = int(m.group(1))
        half = 6 if m.group(2) else 0
        return years * 12 + half
    return None


def _series_candidates(tags: Iterable[str] | None) -> list[str]:
    """tags から series-like な候補を返す。

    ``tags[0]`` はブランド名扱いで除外し、残りから汎用ストップワードを抜く。
    順序は元の tags 順を保つ。
    """
    if not tags:
        return []
    out: list[str] = []
    for t in list(tags)[1:]:
        t = (t or "").strip()
        if not t or t in GENERIC_TAG_STOPWORDS or _AGE_TAG_STOPWORD_RE.match(t):
            continue
        out.append(t)
    return out


def _load_article(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _is_primary_article(path: str) -> bool:
    return not path.endswith(
        (".enrichment.json", ".quality.json", ".seo.json")
    )


def aggregate(articles_dir: pathlib.Path | str) -> dict:
    """記事ディレクトリを走査し、ブランド別集計 dict を返す。"""
    files = sorted(
        f for f in glob.glob(str(pathlib.Path(articles_dir) / "*.json"))
        if _is_primary_article(f)
    )
    by_brand: dict[str, list[dict]] = defaultdict(list)

    for f in files:
        d = _load_article(f)
        if not d:
            continue
        product = d.get("product") or {}
        norm = normalize_brand(product.get("brand"))
        if norm.exclude_from_taxonomy:
            # ノーブランド系はハブを作らない
            continue
        ivs = (product.get("ivs_detail") or {}).get("total_100")
        age = parse_age_min_months(
            (d.get("persona_fit") or {}).get("age_range")
        )
        by_brand[norm.canonical].append(
            {
                "asin": product.get("asin"),
                "image": product.get("image"),
                "ivs_100": ivs,
                "best_price": product.get("best_price"),
                "tier": norm.tier,
                "age_min_months": age,
                "series_candidates": _series_candidates(d.get("tags")),
            }
        )

    brands_out: dict[str, dict] = {}
    for brand, items in sorted(by_brand.items()):
        n = len(items)
        if n < NARRATIVE_MIN_COUNT:
            brands_out[brand] = {"count": n}
            continue

        ivs_vals = [i["ivs_100"] for i in items if isinstance(i["ivs_100"], (int, float))]
        age_vals = [i["age_min_months"] for i in items if isinstance(i["age_min_months"], int)]
        price_vals = [i["best_price"] for i in items if isinstance(i["best_price"], int) and i["best_price"] > 0]

        # representative = 最高 IVS、同点は best_price 安い方
        def _rep_key(it: dict) -> tuple[float, float]:
            score = -float(it["ivs_100"]) if it["ivs_100"] is not None else 0.0
            price = float(it["best_price"]) if isinstance(it["best_price"], int) and it["best_price"] > 0 else float("inf")
            return (score, price)

        rep = min(items, key=_rep_key)

        series_counter: Counter[str] = Counter()
        for it in items:
            for s in it["series_candidates"]:
                series_counter[s] += 1
        top_series = [s for s, _ in series_counter.most_common(3)]

        brands_out[brand] = {
            "count": n,
            "tier": items[0]["tier"],
            "avg_ivs_100": round(sum(ivs_vals) / len(ivs_vals), 1) if ivs_vals else None,
            "avg_age_min_months": round(sum(age_vals) / len(age_vals), 1) if age_vals else None,
            "top_3_series": top_series,
            "representative_asin": rep["asin"],
            "representative_image": rep["image"],
            "lowest_price": min(price_vals) if price_vals else None,
        }

    return {
        "generated_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "narrative_min_count": NARRATIVE_MIN_COUNT,
        "brand_count_total": len(brands_out),
        "brand_count_narrative": sum(
            1 for v in brands_out.values() if v.get("count", 0) >= NARRATIVE_MIN_COUNT
        ),
        "brands": brands_out,
    }


def write_output(payload: dict, out_path: pathlib.Path | str) -> None:
    out_path = pathlib.Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--articles-dir", default=str(_DEFAULT_ARTICLES))
    p.add_argument("--out", default=str(_DEFAULT_OUT))
    args = p.parse_args(argv)

    payload = aggregate(args.articles_dir)
    write_output(payload, args.out)
    print(
        f"[brand_hub_stats] total={payload['brand_count_total']} "
        f"narrative={payload['brand_count_narrative']} "
        f"out={args.out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
