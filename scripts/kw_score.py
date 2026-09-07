#!/usr/bin/env python3
"""kw_score.py — キーワード台帳からサイト別に候補を出す (omcha-ops#97 P3)。

## 台帳はスコアを持たない。スコアはここで、サイトごとに別々に付ける

omcha.jp は自 GSC が厚く (週次 18,990 語)、外部ボリュームが要るのは
**まだ記事が無い語**だけ。navi.omcha.jp は自 GSC が使えず (母数が薄くボットまみれ)、
外部 + WP 需要が主で、さらに **Amazon に商品があるか**という供給ゲートが要る。
式を1本にすると必ずどちらかの罠を踏むので、`omcha` と `navi` を別のサブコマンドに
分けてある。

## やらないこと — リライト候補の選定

**omcha レーンは「記事が無い語」しか出さない。** 既に GSC に出ている語の選定は
`omcha-ops/scripts/rewrite_radar.py` が正で、2026-08-21 に**一本化すると決めてある**。
ページ次元の position から期待 CTR を引く類の再実装をここでやると、その決定を
黙って巻き戻すことになる。GSC に出ている語は理由つきで落とす。

## 未測定を 0 として扱わない

台帳の `measured=false` は「測っていない」であって「需要が無い」ではない。
**スコアを付けずに `unmeasured` として別枠で出す。** 0 と見なして並べると、
実際には需要のある語が最下位に沈んで永久に候補に上がらない
(実測: `児童 生徒 違い` は外部 0 / GSC 1,750 impr)。

同じ理由で、CSV 由来の `measured_unknown` も別枠。

## 需要は足さない

WP の impressions と外部の SV は**同じ検索需要を別の経路で測ったもの**なので、
足すと二重に数える。`max()` を取り、どちらを採ったかを `demand_from` に残す。

## レーンの守備範囲は block で切る

台帳には両サイトぶんの語が混ざって入っている。トピック分類器を新たに作るのではなく、
**「どの調査で集めた語か」(`block`) で切る。** 出張記事のために集めた施設名を navi の
商品ページ候補として並べても意味が無い。`--block` を渡さないと全部が対象になるので、
ヘッダに block の内訳を必ず出して、取り違えたときに見えるようにしてある。

    cd path/to/omcha-ops
    python path/to/amazon/scripts/kw_score.py navi  --block navi-competitor --limit 30
    python path/to/amazon/scripts/kw_score.py omcha --block trip --limit 30
"""
from __future__ import annotations

import argparse
import collections
import io
import json
import os
import pathlib
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import kw_ledger as L  # noqa: E402

normalize_key = L.normalize_key

DEFAULT_EXTERNAL = "data/keywords/external.jsonl"
DEFAULT_WP_DEMAND = "../amazon-navi-brain/demand/wp_demand.jsonl"
DEFAULT_SUPPLY_PROBE = "../amazon/data/analytics/demand_supply_probe.json"
# navi の rank guard と同じ条件。ここだけ別の閾値にすると、片方が候補に出して
# もう片方が落とす状態になる。
DEFAULT_GUARD_POS_MAX = 3.0
DEFAULT_GUARD_MIN_CLICKS = 100.0
# 「omcha.jp が既にその語で出ている」とみなす下限。1〜2 imp は誤差なので、
# ここを 0 にすると裾のノイズで新規候補がほぼ全部落ちる。
DEFAULT_PRESENT_MIN_IMPRESSIONS = 10.0


def filter_blocks(rows: list[dict], blocks: str | None) -> list[dict]:
    """`--block` の部分一致で絞る。カンマ区切りで複数。

    完全一致にしないのは、block 名に日付が入る運用 (`navi-competitor-2026-08`) で
    毎月フラグを書き換えることになるため。
    """
    if not blocks:
        return rows
    keys = [b.strip() for b in blocks.split(",") if b.strip()]
    return [r for r in rows if any(k in (r.get("block") or "") for k in keys)]


def block_summary(rows: list[dict], top: int = 4) -> str:
    c = collections.Counter(r.get("block") or "(no block)" for r in rows)
    return " / ".join("%s=%d" % (k, v) for k, v in c.most_common(top))


def load_wp(path: pathlib.Path) -> dict[str, dict]:
    """ブリッジ (wp_demand.jsonl) を norm -> 行 で返す。meta 行は query を持たない。"""
    out: dict[str, dict] = {}
    for r in L.read_jsonl(path):
        norm = r.get("norm") or normalize_key(r.get("query"))
        if not norm or not r.get("query"):
            continue
        out[norm] = r
    return out


def load_supply(path: pathlib.Path) -> dict[str, int]:
    """供給 probe を norm -> Amazon のヒット件数で返す。

    需要があっても Amazon に商品が無ければ navi の記事型 (商品ページ) にならない。
    実例: メロジョイ は WP 需要 約 110,000 imp の最大クラスタだが Amazon で
    売っておらず、SearchItems に投げても 0 件で API 呼び出しを捨てるだけになる。
    """
    if not path.exists():
        return {}
    data = json.loads(io.open(path, encoding="utf-8").read())
    out: dict[str, int] = {}
    for r in data.get("results", []):
        norm = normalize_key(r.get("keyword"))
        if norm:
            out[norm] = int(r.get("hits") or 0)
    return out


def season(monthly: list[dict]) -> dict | None:
    """月別推移からピーク月と山谷比を出す。

    旅行語のように季節性が大きい語は、**投入日を山の手前に置く**かどうかで結果が
    変わる (実測: `箱根 子連れ` は 7〜9月 1,900 → 12月 1,300)。単一の SV だけを
    見ていると、これが判断から落ちる。
    """
    vals = [(m.get("month"), m.get("volume")) for m in monthly or []
            if isinstance(m.get("volume"), (int, float))]
    vals = [(mo, v) for mo, v in vals if v]
    if len(vals) < 6:
        return None
    peak = max(vals, key=lambda x: x[1])
    low = min(vals, key=lambda x: x[1])
    return {"peak_month": peak[0], "peak": peak[1], "low": low[1],
            "ratio": round(peak[1] / low[1], 2) if low[1] else None}


def demand_of(row: dict, wp: dict | None) -> tuple[float, str]:
    """需要と、その出どころ。**足さない。**

    WP の impressions と外部 SV は同じ需要を別経路で測ったもの。合算すると
    二重に数える。大きい方を採り、どちらを採ったかを残す。
    """
    sv = row.get("sv") or 0
    wp_imp = (wp or {}).get("impressions") or 0
    if wp_imp >= sv:
        return float(wp_imp), "wp_gsc"
    return float(sv), "external"


def split_by_measurement(rows: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """(測れた, 未測定, 判定していない) に分ける。**混ぜない。**"""
    measured, unmeasured, unknown = [], [], []
    for r in rows:
        if r.get("measured_unknown"):
            unknown.append(r)
        elif L.measured(r):
            measured.append(r)
        else:
            unmeasured.append(r)
    return measured, unmeasured, unknown


def score_navi(rows: list[dict], wp: dict[str, dict], supply: dict[str, int],
               guard_pos_max: float, guard_min_clicks: float) -> tuple[list[dict], dict]:
    """navi の新規商品ページ候補。

    ゲートは2枚:
      1. host crowding — WP が pos<=guard かつ clicks>=guard で取っている語は外す。
         同一ホスト族なので、そこで navi を出しても露出は増えず枠を食い合う
      2. 供給 — Amazon に商品が無い語は外す。probe に無い語は「まだ調べていない」
         であって「無い」ではないので、落とさず `supply=unknown` を付けて残す

    navi 自身の GSC はスコアに使わない (母数 652 語・アクセスの 98% がボット)。
    評価 (打った後どうなったか) にだけ使う。
    """
    out = []
    dropped = collections.Counter()
    for r in rows:
        norm = r.get("norm")
        w = wp.get(norm)
        if w and (w.get("position") or 99) <= guard_pos_max \
                and (w.get("clicks") or 0) >= guard_min_clicks:
            dropped["wp_owned"] += 1
            continue
        hits = supply.get(norm)
        if hits == 0:
            dropped["no_supply"] += 1
            continue
        demand, src = demand_of(r, w)
        if demand <= 0:
            dropped["no_demand"] += 1
            continue
        out.append({
            "keyword": r.get("keyword"),
            "norm": norm,
            "demand": demand,
            "demand_from": src,
            "sv": r.get("sv"),
            "sd": r.get("sd"),
            "wp_impressions": (w or {}).get("impressions"),
            "wp_position": (w or {}).get("position"),
            "supply_hits": hits if hits is not None else "unknown",
            "season": season(r.get("monthly_searches")),
            "fetched_at": r.get("fetched_at"),
        })
    out.sort(key=lambda r: -r["demand"])
    return out, dict(dropped)


def score_omcha(rows: list[dict], wp: dict[str, dict],
                present_min_impressions: float) -> tuple[list[dict], dict]:
    """omcha.jp の**新規テーマ**候補。リライト候補は出さない。

    既に GSC に出ている語 (= 受け皿のページがある語) は落とす。そこは
    rewrite_radar の領分で、外部ボリュームも要らない (2ページ目までは
    impressions を捕捉率で割り戻せる)。
    """
    out = []
    dropped = collections.Counter()
    for r in rows:
        norm = r.get("norm")
        w = wp.get(norm)
        if w and (w.get("impressions") or 0) >= present_min_impressions:
            dropped["already_ranking"] += 1
            continue
        sv = r.get("sv") or 0
        if sv <= 0:
            dropped["no_demand"] += 1
            continue
        out.append({
            "keyword": r.get("keyword"),
            "norm": norm,
            "sv": sv,
            "sd": r.get("sd"),
            "search_intent": r.get("search_intent"),
            "wp_impressions": (w or {}).get("impressions"),
            "season": season(r.get("monthly_searches")),
            "fetched_at": r.get("fetched_at"),
        })
    out.sort(key=lambda r: -r["sv"])
    return out, dict(dropped)


def _fmt_season(s: dict | None) -> str:
    if not s:
        return "-"
    return "%s x%.1f" % (s["peak_month"], s["ratio"] or 0)


def cmd_navi(args) -> int:
    rows = filter_blocks(
        list(L.latest(L.read_jsonl(pathlib.Path(args.external))).values()),
        args.block)
    measured, unmeasured, unknown = split_by_measurement(rows)
    wp = load_wp(pathlib.Path(args.wp_demand))
    supply = load_supply(pathlib.Path(args.supply_probe))
    scored, dropped = score_navi(measured, wp, supply,
                                 args.guard_pos_max, args.guard_min_clicks)

    print("# navi 新規候補 — 台帳 %d 語 / 測れた %d 語 / 候補 %d 語"
          % (len(rows), len(measured), len(scored)))
    print("# 落とした: %s" % (dropped or "(無し)"))
    print("# block: %s" % block_summary(measured))
    print("# 未測定 %d 語 / 判定していない %d 語 は **スコアを付けない** "
          "(0 ではない)" % (len(unmeasured), len(unknown)))
    if not wp:
        print("# 注意: WP ブリッジが空。host crowding ガードが効いていない (%s)"
              % args.wp_demand)
    print()
    print("%-28s %10s %-9s %8s %6s %s" %
          ("keyword", "demand", "from", "supply", "sd", "season"))
    for r in scored[:args.limit]:
        print("%-28s %10.0f %-9s %8s %6s %s"
              % (str(r["keyword"])[:28], r["demand"], r["demand_from"],
                 r["supply_hits"], r["sd"] if r["sd"] is not None else "-",
                 _fmt_season(r["season"])))
    if args.out:
        pathlib.Path(args.out).write_text(
            json.dumps({"lane": "navi", "dropped": dropped,
                        "unmeasured": len(unmeasured), "unknown": len(unknown),
                        "candidates": scored}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        print("\nwrote %s (%d 件)" % (args.out, len(scored)))
    return 0


def cmd_omcha(args) -> int:
    rows = filter_blocks(
        list(L.latest(L.read_jsonl(pathlib.Path(args.external))).values()),
        args.block)
    measured, unmeasured, unknown = split_by_measurement(rows)
    wp = load_wp(pathlib.Path(args.wp_demand))
    scored, dropped = score_omcha(measured, wp, args.present_min_impressions)

    print("# omcha.jp 新規テーマ候補 — 台帳 %d 語 / 測れた %d 語 / 候補 %d 語"
          % (len(rows), len(measured), len(scored)))
    print("# 落とした: %s" % (dropped or "(無し)"))
    print("# **リライト候補は出さない。** 既に GSC に出ている語は rewrite_radar の領分")
    print("# block: %s" % block_summary(measured))
    print("# 未測定 %d 語 / 判定していない %d 語 は スコアを付けない (0 ではない)"
          % (len(unmeasured), len(unknown)))
    print()
    print("%-28s %9s %6s %-14s %s" % ("keyword", "sv", "sd", "intent", "season"))
    for r in scored[:args.limit]:
        print("%-28s %9.0f %6s %-14s %s"
              % (str(r["keyword"])[:28], r["sv"],
                 r["sd"] if r["sd"] is not None else "-",
                 (r["search_intent"] or "-")[:14], _fmt_season(r["season"])))
    if args.out:
        pathlib.Path(args.out).write_text(
            json.dumps({"lane": "omcha", "dropped": dropped,
                        "unmeasured": len(unmeasured), "unknown": len(unknown),
                        "candidates": scored}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        print("\nwrote %s (%d 件)" % (args.out, len(scored)))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="台帳からサイト別に候補を出す (#97 P3)")
    ap.add_argument("--external", default=DEFAULT_EXTERNAL)
    ap.add_argument("--wp-demand", default=DEFAULT_WP_DEMAND)
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--block", default=None,
                    help="block の部分一致で絞る (カンマ区切り)。"
                         "レーンの守備範囲はこれで切る")
    ap.add_argument("--out", default=None, help="JSON で書き出す")
    sub = ap.add_subparsers(dest="lane", required=True)

    p = sub.add_parser("navi", help="navi の新規商品ページ候補")
    p.add_argument("--supply-probe", default=DEFAULT_SUPPLY_PROBE)
    p.add_argument("--guard-pos-max", type=float, default=DEFAULT_GUARD_POS_MAX)
    p.add_argument("--guard-min-clicks", type=float, default=DEFAULT_GUARD_MIN_CLICKS)
    p.set_defaults(func=cmd_navi)

    p = sub.add_parser("omcha", help="omcha.jp の新規テーマ候補 (リライトは出さない)")
    p.add_argument("--present-min-impressions", type=float,
                   default=DEFAULT_PRESENT_MIN_IMPRESSIONS)
    p.set_defaults(func=cmd_omcha)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
