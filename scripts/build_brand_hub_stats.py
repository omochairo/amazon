"""build_brand_hub_stats.py

#731 Prep — ブランドハブ narrative / JSON-LD / hero 用の集計を出力する。

`data/articles/*.json` を `brand_normalizer.normalize()` の canonical 名で
グループ化し、ブランドごとに count / avg_ivs_100 / avg_age_min_months /
top_3_series / representative_asin / representative_image / lowest_price /
tier を `hugo/data/brand_hub.json` に書き出す。

記事数 < 3 のブランドは ``{"count": N}`` のみを出力する。template 側で
narrative を省略するシグナルとして使う。

記事数 >= 3 のブランドには ``seo_title`` / ``seo_description`` も出力する
(#5322)。head.html が printf で 1 種類のテンプレを組んでいた結果、93 ページの
description が「ブランド名と数値だけ違う同じ 1 文」になっていたため、文型の
選択をここ (テストできる場所) に移した。文型は手元にある事実 (top_3_series /
avg_age_min_months / count) の有無で分岐する。

**価格は description に入れない。** best_price は日次更新だが meta は full
rebuild 時点で凍るため、SERP のスニペットと実ページの価格が食い違う。価格の
鮮度を主張しないほうが安全 (#5322)。

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

from brand_normalizer import (
    _fold,
    name_variants as brand_name_variants,
    normalize as normalize_brand,
)

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


def _series_candidates(tags: Iterable[str] | None,
                       canonical: str | None = None) -> list[str]:
    """tags から series-like な候補を返す。

    ``tags[0]`` はブランド名扱いで除外し、残りから汎用ストップワードを抜く。
    順序は元の tags 順を保つ。

    ``canonical`` を渡すと、2 番目以降に再出現したブランド名も落とす (#5322)。
    tags[0] だけを見る実装だと「タカラトミー」がタグ列の後方にも入っている記事で
    top_3_series が ``["タカラトミー", "ミニカー", ...]`` になり、hero の
    「人気シリーズ」や description が「タカラトミーの知育玩具…タカラトミー・
    ミニカーなどのシリーズ」と自己言及していた。

    突き合わせは ``_fold`` した**表記の一致**で行う (全半角・大小の差は吸収する)。
    normalize の canonical 一致にすると「プラレール」のようにブランドの alias
    としても登録されているシリーズ名まで落ちてしまうため。

    #5343 follow-up: 表記一致だけだと **同じ名前の別スクリプト表記**が残る
    (BabyBus の "ベビーバス"、学研の "Gakken" など。実測で 32 件)。これも自己言及
    でしかないので落としたいが、alias 照合に広げると「トミカ」「ナノブロック」
    「ナーフ」「ラキュー」のような**実在シリーズ名**を巻き添えにする (これらは
    正規化のために意図して alias に入っている)。文字列からは判別できないので、
    brand_taxonomy.yaml の ``name_variants`` に**人が明示したものだけ**を落とす。
    キーが無いブランドでは canonical だけが返るため、従来の挙動と変わらない。
    """
    if not tags:
        return []
    # taxonomy に載っていないブランド (ノーブランド等) では name_variants が空に
    # なるので、canonical 自身は常に足しておく (#5322 の挙動を落とさない)。
    variants = (brand_name_variants(canonical) | {_fold(canonical)}) if canonical else frozenset()
    out: list[str] = []
    for t in list(tags)[1:]:
        t = (t or "").strip()
        if not t or t in GENERIC_TAG_STOPWORDS or _AGE_TAG_STOPWORD_RE.match(t):
            continue
        if canonical and _fold(t) in variants:
            continue
        out.append(t)
    return out


def age_band_label(avg_age_min_months: float | int | None) -> str | None:
    """avg_age_min_months (下限月齢の平均) を SERP 向けの年齢帯ラベルにする。

    平均値なので「N歳から」と言い切らず、呼び出し側で「〜の商品が中心」と
    幅のある表現にして使う。12ヶ月未満は "0歳"、24ヶ月未満は "1歳" に丸める
    (0.8歳/1.3歳のような偽の精度を出さないため)。
    """
    if not isinstance(avg_age_min_months, (int, float)):
        return None
    m = float(avg_age_min_months)
    if m < 0:
        return None
    if m < 12:
        return "0歳"
    if m < 24:
        return "1歳"
    return f"{int(m // 12)}歳"


# count がこの値以上のときだけ <title> に件数を出す。少ない件数を title に
# 出すのは「品揃えが N 件しかない」という自己申告になり CTR に効かないため
# (#5322)。24 ブランドが該当する規模で線を引いている。
TITLE_COUNT_MIN = 20


def build_seo_title(brand: str, entry: dict) -> str:
    """/brands/<brand>/ の <title> 本体 (サイト名サフィックスは template 側)。

    日本語 SERP の title 表示は全角 30〜35 文字程度で切れるので、サフィックス
    込みで収まる長さに抑える。件数は TITLE_COUNT_MIN 以上のときだけ出す。
    """
    n = int(entry.get("count") or 0)
    series = list(entry.get("top_3_series") or [])
    age = age_band_label(entry.get("avg_age_min_months"))

    if n >= TITLE_COUNT_MIN:
        return f"{brand}の知育玩具{n}選｜スコアと価格で比較"
    if age:
        return f"{brand}の知育玩具レビュー｜{age}から選ぶ比較ガイド"
    if series:
        return f"{brand}の知育玩具レビュー｜{series[0]}をスコアで比較"
    return f"{brand}の知育玩具レビュー｜スコアと価格で比較"


def build_seo_description(brand: str, entry: dict) -> str:
    """/brands/<brand>/ の meta description。

    top_3_series / avg_age_min_months / count の有無で文型を選ぶ。シリーズ名は
    ブランドごとに固有で、"ブランド名 シリーズ名" の検索意図に直接当たるため
    最優先で入れる。価格は入れない (module docstring 参照)。
    """
    n = int(entry.get("count") or 0)
    series = [s for s in (entry.get("top_3_series") or []) if s]
    age = age_band_label(entry.get("avg_age_min_months"))

    # 件数帯で訴求を変える。品揃えの広いブランドは「横断比較できること」が、
    # 数点しかないブランドは「1点ずつ見ていること」が読者にとっての価値なので、
    # 文型を分けるのは体裁合わせではなく中身の違いに対応している。
    if series and age:
        detail = f"{'・'.join(series[:2])}など、{age}〜"
    elif series:
        detail = f"{'・'.join(series[:2])}など"
    elif age:
        detail = f"{age}〜"
    else:
        detail = ""

    if n >= TITLE_COUNT_MIN:
        head = f"{brand}の知育玩具{n}件を横断比較。"
        tail = "教育性・安全性・コスパの知育スコアで並べ替えて選べます。"
        body = f"{detail}の商品を掲載しています。" if detail else ""
    elif n >= 10:
        head = f"{brand}の知育玩具{n}件をレビュー。"
        tail = "知育スコアと価格で並べ替えて比較できます。"
        body = f"{detail}の商品が中心です。" if detail else ""
    else:
        head = f"{brand}の知育玩具{n}件を1点ずつレビュー。"
        tail = "教育性・安全性・コスパを独自スコアで採点しています。"
        body = f"{detail}の商品を掲載。" if detail else ""

    return f"{head}{body}{tail}"


RELATED_BRANDS_MAX = 3

# related_brands のスコア重み。シリーズの重なりが最も強い関連 (同じ遊びの
# カテゴリ) で、次に年齢帯、tier は最後の tie-break 程度に効かせる。
_REL_W_SERIES = 10
_REL_W_TIER = 3
_REL_AGE_NEAR_MONTHS = 12


def _related_score(a: dict, b: dict) -> tuple[int, str]:
    """ブランド a から見た b の関連スコアと、UI に出す理由ラベルを返す。"""
    score = 0
    reason = ""

    shared = [s for s in (a.get("top_3_series") or [])
              if s in (b.get("top_3_series") or [])]
    if shared:
        score += _REL_W_SERIES * len(shared)
        reason = f"{shared[0]}つながり"

    a_age, b_age = a.get("avg_age_min_months"), b.get("avg_age_min_months")
    if isinstance(a_age, (int, float)) and isinstance(b_age, (int, float)):
        diff = abs(float(a_age) - float(b_age))
        if diff <= _REL_AGE_NEAR_MONTHS:
            # 12ヶ月差で 0、同じなら満点。年齢帯の近さを線形に効かせる。
            score += int(round(6 * (1 - diff / _REL_AGE_NEAR_MONTHS)))
            if not reason:
                label = age_band_label(b_age)
                reason = f"{label}〜が中心" if label else "対象年齢が近い"

    if a.get("tier") and a.get("tier") == b.get("tier"):
        score += _REL_W_TIER
        if not reason:
            reason = "近いポジション"

    return score, reason


def build_related_brands(brands: dict[str, dict]) -> None:
    """各 brand entry に ``related_brands`` を書き込む (in-place)。

    ブランドハブから出る内部リンクが商品カードだけで、ハブ同士が繋がって
    いなかったため回遊もクロール導線も途切れていた (#5330)。

    リンク先は **noindex でない count>=3 のブランドに限る**。noindex ブランドへ
    リンクしても読者には薄いページ、クローラには行き止まりにしかならない。
    """
    pool = {
        name: e for name, e in brands.items()
        if int(e.get("count") or 0) >= NARRATIVE_MIN_COUNT and not e.get("noindex")
    }
    for name, entry in brands.items():
        if int(entry.get("count") or 0) < NARRATIVE_MIN_COUNT:
            continue
        scored = []
        for other, cand in pool.items():
            if other == name:
                continue
            score, reason = _related_score(entry, cand)
            if score <= 0:
                continue
            # 決定的な順序: スコア降順 → 記事数降順 → 名前昇順
            scored.append((-score, -int(cand.get("count") or 0), other, reason))
        scored.sort()
        entry["related_brands"] = [
            {"name": other, "reason": reason}
            for _, _, other, reason in scored[:RELATED_BRANDS_MAX]
        ]


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
                "noindex": norm.noindex,
                "age_min_months": age,
                "series_candidates": _series_candidates(
                    d.get("tags"), norm.canonical
                ),
            }
        )

    brands_out: dict[str, dict] = {}
    for brand, items in sorted(by_brand.items()):
        n = len(items)
        # noindex はブランド単位で一定 (taxonomy 由来)。head.html が
        # site.Data.brand_hub.brands[<brand>].noindex を見て /brands/ ページを
        # noindex 化する (epic #2126 P4)。True のときだけ出力して JSON を軽く保つ。
        brand_noindex = bool(items[0].get("noindex"))
        if n < NARRATIVE_MIN_COUNT:
            entry = {"count": n}
            if brand_noindex:
                entry["noindex"] = True
            brands_out[brand] = entry
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
        # 「プログラミングおもちゃ」と「プログラミング」のように片方が他方の
        # 部分文字列になる tag を 2 枠使わせない (#5322)。先に来た = 出現数の
        # 多いほうを残す。
        top_series: list[str] = []
        for s, _ in series_counter.most_common():
            if len(top_series) >= 3:
                break
            if any(s in kept or kept in s for kept in top_series):
                continue
            top_series.append(s)

        entry = {
            "count": n,
            "tier": items[0]["tier"],
            "avg_ivs_100": round(sum(ivs_vals) / len(ivs_vals), 1) if ivs_vals else None,
            "avg_age_min_months": round(sum(age_vals) / len(age_vals), 1) if age_vals else None,
            "top_3_series": top_series,
            "representative_asin": rep["asin"],
            "representative_image": rep["image"],
            "lowest_price": min(price_vals) if price_vals else None,
        }
        # seo_* は上の集計結果から導出するので entry を組んだ後に足す。
        entry["seo_title"] = build_seo_title(brand, entry)
        entry["seo_description"] = build_seo_description(brand, entry)
        brands_out[brand] = entry
        if brand_noindex:
            brands_out[brand]["noindex"] = True

    # related_brands は全ブランドの集計が出そろってからでないと決まらない。
    build_related_brands(brands_out)

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
