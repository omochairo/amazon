"""
measure_third_party_brand_hit.py  (#1600 Phase 2 の効果測定)

`third_party_sources.json` (Tavily pre-fetch) の収集結果に、その商品の**ブランド名
トークンが 1 回でも出るか**を数える。出なければ「その商品について書かれた記事」では
なくカテゴリ一般論を拾っただけなので、裏取り素材としては効いていない。

なぜ必要か:
  band=zero に Tavily を回したときのブランド名ヒット率は約 1/3 という実測がある。
  一方、需要 (GSC imp) で選んだ既存記事レーン (fetch_third_party_sources --from-gsc)
  で同じ率しか出ないなら、**Tavily は梃子ではない**という判断になる。
  そのため zero 帯と対象レーンを **同一の測り方で並べて出す** のがこの script の役目。
  記憶の中の数字と突き合わせるのではなく、両コホートをここで測り直して比較すること。

  ただし **素の比較だけで結論を出さないこと** (2026-08-25 実測)。cohort_zero は
  evidence 0 かつ brand_tier D で定義されており 100% tier D になるため、tier 混在の
  レーンと素で並べるとブランド構成の差がレーンの効果に見える。実際 GSC 需要レーンは
  素で 68.6% vs 32.8% と大差だが、同じ tier で比べると全 tier で CI が重なり差は消えた
  (直接標準化で +6.5pt)。--by-tier がこの層別を出す。

測り方 (意図的に単純):
  1. amazon.json の title からラテン系トークンを取る (先頭のものをブランド名とみなす。
     Amazon JP のタイトルはブランド始まりが大半)。汎用語・単位・型番だけの語は除く
  2. third_party_sources.json の sources を連結し、そのトークンが含まれるか見る
  3. 分母を 2 通り出す:
       - トークン有り分母: ブランド名トークンを取れた ASIN だけ
       - 全体分母       : 取れなかった ASIN も「出現しない」として数える
     どちらか片方だけだと、無名 OEM (ラテン名すら無い) の扱いで率が動いてしまう

照合フィールドも 2 通り出す (title+snippet / title+snippet+url)。url を含めると
スラッグにブランド名が入るぶん率が上がるので、片方だけ見て話をしないこと。

Usage:
  python scripts/measure_third_party_brand_hit.py                       # 全コホート比較
  python scripts/measure_third_party_brand_hit.py --cohort gsc --list   # 明細つき
  python scripts/measure_third_party_brand_hit.py --by-tier             # brand_tier で層別
  python scripts/measure_third_party_brand_hit.py --json                # 機械可読
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import score_per_asin_info as _sc  # noqa: E402
import fetch_third_party_sources as _f  # noqa: E402

PER_ASIN_DIR = pathlib.Path("data/raw/per_asin")

# ラテン系トークン (2 文字以上の英字始まり)。ハイフン/アポストロフィ/数字を含む
# ブランド名 (e.g. "Ed-Inter" / "B.Toys" / "Kid O") を 1 語として拾う。
_LATIN_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9&'\-\.]{1,}")

# ブランド名として採らない語。
#   - 汎用英単語 (タイトルの装飾に出るだけ)
#   - 単位・規格・素材の略号
#   - 対象年齢/個数の表記
# ここを増やしすぎると「ブランド名が取れない」側へ倒れて率が下がるので、
# 実際にタイトル先頭に出た語だけを入れている。
_GENERIC_TOKENS = frozenset("""
new the and for with set kit toy toys kids kid baby boys girls play game games
pcs pack size color colour ver version type model made japan
cm mm kg ml led usb abs pvc eva pet iso sale gift diy fsc ce st
""".split())


def _load(path: pathlib.Path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def brand_tokens(title: str) -> list[str]:
    """タイトルからブランド名候補トークンを返す (先頭 1 語、無ければ空)。

    Amazon JP のタイトルはブランド始まりが大半なので、**最初に出るラテン系
    トークン**を採る。数字だけ・型番だけの語 (e.g. "RD-10" / "SC-500") は
    ブランド名ではないので飛ばす。
    """
    for tok in _LATIN_TOKEN_RE.findall(title or ""):
        low = tok.lower().strip(".-")
        if len(low) < 3 or low in _GENERIC_TOKENS:
            continue
        if not re.search(r"[A-Za-z]{3}", tok):   # 型番 (英字 1-2 + 数字) を除く
            continue
        return [tok]
    return []


def _sources_blob(asin: str, base: pathlib.Path, with_url: bool) -> str | None:
    """third_party_sources.json の収集結果を 1 本の文字列にする (無ければ None)。"""
    data = _load(base / asin / _f.OUT_NAME)
    srcs = data.get("sources") if isinstance(data, dict) else None
    if not isinstance(srcs, list):
        return None
    parts: list[str] = []
    for s in srcs:
        if not isinstance(s, dict):
            continue
        parts.append(str(s.get("title") or ""))
        parts.append(str(s.get("snippet") or ""))
        if with_url:
            parts.append(str(s.get("url") or ""))
    return " ".join(parts).lower()


def measure(asins: list[str], base: pathlib.Path, with_url: bool) -> dict:
    """ヒット率を数える。戻り値は集計 dict (details に ASIN 単位の内訳)。"""
    details = []
    for asin in asins:
        blob = _sources_blob(asin, base, with_url)
        if blob is None:
            continue  # 未収集 = 測る対象ではない (分母に入れない)
        amazon = _load(base / asin / "amazon.json") or {}
        item = amazon.get("item") if isinstance(amazon.get("item"), dict) else amazon
        title = item.get("title", "") if isinstance(item, dict) else ""
        toks = brand_tokens(title)
        hit = any(t.lower() in blob for t in toks) if toks else False
        details.append({"asin": asin, "brand_token": toks[0] if toks else None, "hit": hit})
    n = len(details)
    with_tok = [d for d in details if d["brand_token"]]
    hits = sum(1 for d in details if d["hit"])
    return {
        "fetched": n,
        "with_brand_token": len(with_tok),
        "hits": hits,
        "rate_all": (hits / n * 100) if n else 0.0,
        "rate_with_token": (hits / len(with_tok) * 100) if with_tok else 0.0,
        "details": details,
    }


def wilson_ci(k: int, n: int) -> tuple[float, float]:
    """二項比率の 95% Wilson 信頼区間 (%) を返す。

    正規近似 (p +- 1.96*sqrt(p(1-p)/n)) を使わないのは、tier 別のように
    n が 10-20 のセルで区間が [0, 1] を飛び出すため。判定は「コホート間で
    CI が重なるか」で行うので、小さい n で素直に効く形でないと使えない。
    """
    if n <= 0:
        return (0.0, 0.0)
    p = k / n
    z = 1.96
    d = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (100 * max(0.0, centre - half), 100 * min(1.0, centre + half))


def _tier_of(asin: str, base: pathlib.Path, cache: dict) -> str:
    """brand_tier を 1 回だけ引いてキャッシュする (score_asin は ASIN あたり数ファイル読む)。"""
    if asin not in cache:
        cache[asin] = _sc.score_asin(asin, base).get("brand_tier") or "D"
    return cache[asin]


def _rate_row(rows: list[dict]) -> dict:
    k = sum(1 for r in rows if r["hit"])
    n = len(rows)
    lo, hi = wilson_ci(k, n)
    return {"n": n, "hits": k, "rate": (100 * k / n) if n else 0.0,
            "ci_low": lo, "ci_high": hi}


def by_tier(details: list[dict], base: pathlib.Path, cache: dict) -> dict:
    """measure() の details を brand_tier で割る。

    brand_tier は brand_normalizer が amazon.json の title / seller を引くだけで、
    third_party_sources からは一切導出されない (score_per_asin_info._brand_tier)。
    したがって収集結果に対して pre-treatment な共変量であり、層別してよい。
    collider ではないので、ここで割っても選択バイアスは入らない。
    """
    groups: dict[str, list[dict]] = collections.defaultdict(list)
    for d in details:
        groups[_tier_of(d["asin"], base, cache)].append(d)
    return {t: _rate_row(rows) for t, rows in sorted(groups.items())}


def standardize(rows: dict) -> tuple[float, int]:
    """focus 側の tier 別ヒット率を、focus 外の tier 構成で重みづけ直す (直接標準化)。

    戻り値は (標準化後のヒット率 %, 使った重みの合計 n)。

    focus 側が 1 件も無い tier は重みから **外す**。0 件のセルに率は無いので
    0% として混ぜると標準化後の値が不当に下がる。除いたぶんは分母 (重みの
    合計) にも反映するので、「残った tier の中での標準化」であることは崩れない。
    重みの合計を返しているのは、どれだけの母集団に対する標準化なのかを
    出力側で明示できるようにするため。
    """
    weight_total = 0
    weighted = 0.0
    for row in rows.values():
        w = row["outside"]["n"]
        if w == 0 or row["inside"]["n"] == 0:
            continue
        weight_total += w
        weighted += w * row["inside"]["rate"]
    return ((weighted / weight_total) if weight_total else 0.0, weight_total)


def tier_crosstab(base: pathlib.Path, focus: set, with_url: bool, cache: dict) -> dict:
    """収集済み全 ASIN を brand_tier x (focus コホートに入るか) で割る。

    なぜ必要か (2026-08-25 実測): cohort_zero は evidence 0 かつ brand_tier D で
    定義されているため、基準線は 100% tier D になる。tier 混在のレーンと素で
    並べると、ヒット率の差が「レーンの効果」ではなく「ブランド構成の差」を
    映してしまう。実際 GSC 需要レーンは素で 68.6% vs 32.8% と大差に見えるが、
    同じ tier の中で比べると全 tier で CI が重なり、差は消える。

    direct: focus 側の tier 別ヒット率を **focus 外の tier 構成** で重みづけ
    直した値 (直接標準化)。focus 外の粗率と直接比較してよい 1 対の数字になる。
    """
    collected = [d.name for d in sorted(base.iterdir())
                 if d.is_dir() and (d / _f.OUT_NAME).exists()]
    details = measure(collected, base, with_url)["details"]
    inside: dict[str, list[dict]] = collections.defaultdict(list)
    outside: dict[str, list[dict]] = collections.defaultdict(list)
    for d in details:
        tier = _tier_of(d["asin"], base, cache)
        (inside if d["asin"] in focus else outside)[tier].append(d)

    tiers = sorted(set(inside) | set(outside))
    rows = {}
    for t in tiers:
        rows[t] = {"inside": _rate_row(inside.get(t, [])),
                   "outside": _rate_row(outside.get(t, []))}

    std_rate, weight_total = standardize(rows)
    all_outside = [d for t in tiers for d in outside.get(t, [])]
    return {
        "collected": len(details),
        "focus_size": sum(rows[t]["inside"]["n"] for t in tiers),
        "by_tier": rows,
        "standardized_focus_rate": std_rate,
        "outside_crude_rate": _rate_row(all_outside)["rate"],
        "standardization_weight_n": weight_total,
    }


def cohort_zero(base: pathlib.Path) -> list[str]:
    """band=zero 相当 (evidence 0 かつ brand_tier D) の ASIN。比較の基準線。

    現在の band ではなく `evidence_score == 0 and brand_tier == "D"` で取る。
    third_party が 2 host 以上あると #5499 の配線で band が thin へ上がるため、
    **収集に成功した ASIN だけが zero から抜ける**。band で取ると成功例が
    コホートから消えて、ヒット率が下向きに偏る。
    """
    out = []
    for d in sorted(base.iterdir()):
        # 未収集の ASIN は measure() の分母に入らないので、先に落として
        # score_asin (1 ASIN あたり数ファイル読む) を呼ばずに済ませる。
        if not d.is_dir() or not (d / _f.OUT_NAME).exists():
            continue
        s = _sc.score_asin(d.name, base)
        if s.get("evidence_score") == 0 and s.get("brand_tier") == "D":
            out.append(d.name)
    return out


def cohort_gsc(base: pathlib.Path, min_impressions: int, days: int) -> list[str]:
    """GSC 需要レーン (--from-gsc) の対象 ASIN。収集済みかどうかは問わない。"""
    imps = _f._gsc_page_impressions(_f.GSC_BY_PAGE, days)
    return [a for a, v in sorted(imps.items(), key=lambda kv: (-kv[1], kv[0]))
            if v >= min_impressions]


def cohort_all(base: pathlib.Path) -> list[str]:
    return sorted(d.name for d in base.iterdir() if d.is_dir())


def _cli() -> int:
    ap = argparse.ArgumentParser(description="Tavily 収集結果のブランド名ヒット率")
    ap.add_argument("--cohort", default="compare",
                    choices=("compare", "gsc", "zero", "all"),
                    help="compare = zero とGSC需要レーンを並べて出す (既定)")
    ap.add_argument("--base", default=str(PER_ASIN_DIR))
    ap.add_argument("--gsc-min-impressions", type=int, default=10)
    ap.add_argument("--gsc-days", type=int, default=28)
    ap.add_argument("--by-tier", action="store_true",
                    help="brand_tier で層別する。zero 基準線は 100%% tier D なので、"
                         "素の比較はブランド構成の差を映す (tier_crosstab の docstring 参照)")
    ap.add_argument("--list", action="store_true", help="ASIN 単位の内訳も出す")
    ap.add_argument("--json", action="store_true", help="JSON で出力")
    args = ap.parse_args()
    base = pathlib.Path(args.base)

    cohorts: dict[str, list[str]] = {}
    if args.cohort in ("compare", "zero"):
        cohorts["zero (基準線)"] = cohort_zero(base)
    if args.cohort in ("compare", "gsc"):
        cohorts[f"GSC imp>={args.gsc_min_impressions}"] = cohort_gsc(
            base, args.gsc_min_impressions, args.gsc_days)
    if args.cohort == "all":
        cohorts["全 per_asin"] = cohort_all(base)

    tier_cache: dict[str, str] = {}
    result: dict[str, dict] = {}
    for label, asins in cohorts.items():
        entry = {
            "cohort_size": len(asins),
            "title_snippet": measure(asins, base, with_url=False),
            "title_snippet_url": measure(asins, base, with_url=True),
        }
        for fld in ("title_snippet", "title_snippet_url"):
            m = entry[fld]
            m["ci_low"], m["ci_high"] = wilson_ci(m["hits"], m["fetched"])
            if args.by_tier:
                m["by_tier"] = by_tier(m["details"], base, tier_cache)
        result[label] = entry

    crosstab: dict[str, dict] = {}
    if args.by_tier and args.cohort in ("compare", "gsc"):
        focus = set(cohorts[f"GSC imp>={args.gsc_min_impressions}"])
        for fld, with_url in (("title_snippet", False), ("title_snippet_url", True)):
            crosstab[fld] = tier_crosstab(base, focus, with_url, tier_cache)
        result["_tier_crosstab (収集済み全 ASIN)"] = crosstab

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    for label, r in result.items():
        if label.startswith("_"):
            continue  # crosstab はコホートとは形が違うので下で別に出す
        print(f"\n=== {label} (コホート {r['cohort_size']} 件) ===")
        for fld in ("title_snippet", "title_snippet_url"):
            m = r[fld]
            print(f"  [{fld}] 収集済 {m['fetched']} 件 / ブランド名トークン有り "
                  f"{m['with_brand_token']} 件 / 出現 {m['hits']} 件")
            print(f"      ヒット率: 全体分母 {m['rate_all']:.1f}% "
                  f"CI[{m['ci_low']:.1f}, {m['ci_high']:.1f}] / "
                  f"トークン有り分母 {m['rate_with_token']:.1f}%")
            for tier, row in (m.get("by_tier") or {}).items():
                print(f"        tier {tier}: {row['hits']}/{row['n']} = "
                      f"{row['rate']:.1f}% CI[{row['ci_low']:.1f}, {row['ci_high']:.1f}]")
        if args.list:
            for d in r["title_snippet"]["details"]:
                mark = "o" if d["hit"] else "-"
                print(f"      {mark} {d['asin']}  {d['brand_token'] or '(トークン無し)'}")
    for fld, ct in crosstab.items():
        print(f"\n=== brand_tier x 需要コホート [{fld}] "
              f"(収集済み {ct['collected']} 件 / うち需要側 {ct['focus_size']} 件) ===")
        print(f"  {'tier':<6}{'需要コホート内':<26}{'コホート外':<26}{'差':>9}")
        for tier, row in ct["by_tier"].items():
            i, o = row["inside"], row["outside"]
            print(f"  {tier:<6}{i['hits']:>4}/{i['n']:<5}{i['rate']:5.1f}% "
                  f"CI[{i['ci_low']:4.1f},{i['ci_high']:4.1f}]  "
                  f"{o['hits']:>4}/{o['n']:<5}{o['rate']:5.1f}% "
                  f"CI[{o['ci_low']:4.1f},{o['ci_high']:4.1f}] "
                  f"{i['rate'] - o['rate']:+8.1f}pt")
        print(f"  需要側をコホート外の tier 構成へ直接標準化: "
              f"{ct['standardized_focus_rate']:.1f}%  "
              f"(コホート外の粗率 {ct['outside_crude_rate']:.1f}%, "
              f"重み n={ct['standardization_weight_n']})")
        print("  注: 同じ tier の中で需要側が上回らなければ、素の差はブランド構成の差である。")

    print("\n注: 2 つの分母と 2 つの照合フィールドは、片方だけ見て結論を出さないために"
          "並べて出している。コホート間の比較は同じ行同士で行うこと。")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
