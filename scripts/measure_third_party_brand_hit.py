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
  python scripts/measure_third_party_brand_hit.py --json                # 機械可読
"""

from __future__ import annotations

import argparse
import collections
import json
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

    result: dict[str, dict] = {}
    for label, asins in cohorts.items():
        result[label] = {
            "cohort_size": len(asins),
            "title_snippet": measure(asins, base, with_url=False),
            "title_snippet_url": measure(asins, base, with_url=True),
        }

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    for label, r in result.items():
        print(f"\n=== {label} (コホート {r['cohort_size']} 件) ===")
        for fld in ("title_snippet", "title_snippet_url"):
            m = r[fld]
            print(f"  [{fld}] 収集済 {m['fetched']} 件 / ブランド名トークン有り "
                  f"{m['with_brand_token']} 件 / 出現 {m['hits']} 件")
            print(f"      ヒット率: 全体分母 {m['rate_all']:.1f}% / "
                  f"トークン有り分母 {m['rate_with_token']:.1f}%")
        if args.list:
            for d in r["title_snippet"]["details"]:
                mark = "o" if d["hit"] else "-"
                print(f"      {mark} {d['asin']}  {d['brand_token'] or '(トークン無し)'}")
    print("\n注: 2 つの分母と 2 つの照合フィールドは、片方だけ見て結論を出さないために"
          "並べて出している。コホート間の比較は同じ行同士で行うこと。")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
