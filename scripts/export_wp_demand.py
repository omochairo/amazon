#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""export_wp_demand.py — navi (navi.omcha.jp) へ渡す WP 需要データの派生物を作る。

なぜ要るか (#97 P2 / omochairo/amazon-navi-brain#34):
  2026-08-07 の #4654 で omcha.jp の GSC 収集を public の `omochairo/amazon` から
  この private リポへ移した。その結果 public 側に残った
  `data/analytics/history/gsc_wp_by_query.jsonl` は 2026-08-04 で**凍結**し、
  navi の `build_demand_keywords.py` は鮮度ガード (fail-closed) で止まっている。
  = navi の需要駆動レーンは 2026-08-12 から動いていない。

  直すべきは navi 側のガードではなく **private → public の供給路**。本スクリプトは
  その派生物を作る係で、生の by_query 全量ではなく navi の3つの消費者が実際に読む
  ぶんだけを出す。

  受け渡しは private の `amazon-navi-brain` を経由する (public に順位データを
  出さない = #4654 の判断を戻さない)。navi 側の workflow は既にこのリポジトリを
  `NAVI_BRAIN_PAT` で overlay checkout しているので、新しい配線は要らない。

navi 側の消費者 (2026-09-07 時点):
  - `build_demand_keywords.py` : host crowding ガード (pos<=3 かつ clicks>=100)
  - `ingest_ubersuggest.py`    : 同じ集計を再利用
  - `detect_demand_gaps.py`    : 需要源そのもの (imp と**クエリ表層**が要る)
  クエリ表層が要るのは3番目だけだが、norm だけ渡すと埋め込みが作れないので載せる。

出力 (JSONL・1行目が meta)。**日次レーンと同じフィールド名にしてある**ので、
navi 側の3つの消費者はコードを変えずにそのまま読める:
  {"kind":"wp_query_window","date":"2026-08-30","window_weeks":13,...}
  {"query":"トミカ収納","norm":"トミカ収納","clicks":812.0,"impressions":21365.0,
   "position":4.7}

  - meta 行に `query` を持たせない → 集計側 (load_wp_rank_stats /
    load_gsc_impressions) は「クエリが無い行」として既存の分岐でスキップする
  - meta 行にだけ `date` を持たせる → `wp_history_last_date` は最大の date を返すので、
    **窓の最終日がそのまま鮮度になる**。データ行に date を撒くと 5,758 行ぶんの
    重複になるうえ、週次には日付が無い
  - 1 norm = 1 行なので、加重平均の分母が 1 になり `position` はそのまま通る

週次を土台にする理由:
  日次レーン (`data/gsc/gsc_wp_by_query.jsonl`) は各行が **2日窓**で、連続する行を
  足すと日を二重に数える (CLAUDE.md)。さらに public 側に凍結している旧ファイルは
  日次 top-N 打ち切りのせいで **884 語**しか無い。週次から同じ 13 週窓で作ると
  **6,986 語**になる (2026-09-07 実測)。集計の正しさと母数の両方で週次が勝つ。

  代償は鮮度で、週次レーンは週 1 回しか進まないため `window_last_date` は
  常に 7〜13 日前になる。navi 側の `--wp-history-max-age-days` は日次レーン用の
  8 日なので、そのままでは必ず fail-closed で止まる。**navi 側で 17 日へ広げる**
  (13週窓の最終週末 + 収集遅延 + 実行間隔)。ガードの目的は「収集が生きているか」で
  あって「窓が昨日まで伸びているか」ではない。

正規化キーは navi の `build_demand_keywords.normalize_key` と**同一でなければ
ならない** (NFKC → 小文字 → 空白を完全除去)。コピーせず **import して使う** —
同じリポジトリにあるのに再実装すると、片方だけ直したときに照合が静かに外れる
(例外は出ず、ガードが当たらなくなるだけ)。**ここで正規化して渡し、navi 側は
正規化しない。**

    python scripts/export_wp_demand.py --out <path>/wp_demand.jsonl
"""
from __future__ import annotations

import argparse
import collections
import datetime
import io
import json
import pathlib

import build_demand_keywords

# 本スクリプトは **omochairo/amazon (public) に置き、omcha-ops (private) の
# workflow が checkout して omcha-ops を作業ディレクトリにして実行する**
# (10-gsc-wp-daily.yml と同じパターン。機密なのは出力であってコードではない)。
# したがって既定パスはスクリプト位置ではなく **カレントディレクトリ相対**で持つ。
DEFAULT_SRC = "data/gsc/weekly/by_query.jsonl"
DEFAULT_TOTALS = "data/gsc/weekly/totals.jsonl"
DEFAULT_WEEKS = 13
# imp>=3 で 5,758 語 / impressions の 99.86% が残る (2026-09-07 実測)。
# 1〜2 imp の裾は navi のどの消費者も閾値 (guard clicks>=100 / gaps imp>=50) に
# 届かないので、行数だけ増やして差分を汚す。
DEFAULT_MIN_IMPRESSIONS = 3

# 再実装しない (docstring 参照)。build_demand_keywords が唯一の定義。
normalize_key = build_demand_keywords.normalize_key


def load_weeks(src: pathlib.Path) -> list[str]:
    weeks = set()
    with io.open(src, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                weeks.add(json.loads(line)["week"])
    return sorted(weeks)


def week_bounds(totals: pathlib.Path) -> dict[str, tuple[str, str]]:
    """week -> (week_start, week_end)。日付は totals.jsonl にしか無い。"""
    out: dict[str, tuple[str, str]] = {}
    if not totals.exists():
        return out
    with io.open(totals, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if r.get("week") and r.get("week_start") and r.get("week_end"):
                out[r["week"]] = (r["week_start"], r["week_end"])
    return out


def aggregate(src: pathlib.Path, keep: set[str]) -> dict[str, dict]:
    """norm 単位に imp / clicks を合算し、position は **impression 加重平均**で出す。

    週次 position の単純平均は誤り (週によって imp が偏る)。navi の
    load_wp_rank_stats と同じ計算にしてある。

    表層 (query) は norm グループ内で **impressions 最大の表記**を代表にする。
    detect_demand_gaps.py がこの文字列を埋め込むので、分かち書きの揺れた表記を
    代表にすると意味がずれる。
    """
    agg: dict[str, dict] = {}
    surface: dict[str, collections.Counter] = {}
    with io.open(src, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if r.get("week") not in keep:
                continue
            norm = normalize_key(r.get("query"))
            if not norm:
                continue
            imp = float(r.get("impressions") or 0)
            e = agg.setdefault(norm, {"imp": 0.0, "clicks": 0.0, "pw": 0.0})
            e["imp"] += imp
            e["clicks"] += float(r.get("clicks") or 0)
            e["pw"] += float(r.get("position") or 0) * imp
            surface.setdefault(norm, collections.Counter())[r.get("query") or ""] += imp
    out: dict[str, dict] = {}
    for norm, e in agg.items():
        out[norm] = {
            # フィールド名は日次レーン (gsc_wp_by_query.jsonl) に合わせる。
            "query": surface[norm].most_common(1)[0][0],
            "norm": norm,
            "clicks": round(e["clicks"], 1),
            "impressions": round(e["imp"], 1),
            "position": round(e["pw"] / e["imp"], 2) if e["imp"] else 0.0,
        }
    return out


def build(src: pathlib.Path, totals: pathlib.Path, weeks: int,
          min_impressions: int) -> tuple[dict, list[dict]]:
    all_weeks = load_weeks(src)
    if not all_weeks:
        raise SystemExit(f"{src} に週が1つも無い")
    window = all_weeks[-weeks:]
    keep = set(window)
    rows = [r for r in aggregate(src, keep).values()
            if r["impressions"] >= min_impressions]
    # norm 順で固定する。imp 順にすると毎回ほぼ全行が動いて差分が読めなくなる。
    rows.sort(key=lambda r: r["norm"])
    bounds = week_bounds(totals)
    first_b = bounds.get(window[0])
    last_b = bounds.get(window[-1])
    meta = {
        "kind": "wp_query_window",
        "site": "omcha.jp",
        # wp_history_last_date が読むのはこの1つだけ (docstring 参照)。
        "date": last_b[1] if last_b else None,
        "window_weeks": len(window),
        "first_week": window[0],
        "last_week": window[-1],
        "window_start": first_b[0] if first_b else None,
        "window_last_date": last_b[1] if last_b else None,
        "min_impressions": min_impressions,
        "rows": len(rows),
        "source": "omcha-ops:data/gsc/weekly/by_query.jsonl",
        "generated_at": datetime.datetime.now(datetime.timezone.utc)
                        .strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if meta["window_last_date"] is None:
        # 鮮度の判定材料が無い派生物を渡すと、navi 側は「壊れている」ではなく
        # 「古いだけ」を検出できなくなる (#5107 と同じ型)。黙って出さない。
        raise SystemExit(f"{window[-1]} の week_end が {totals} に無い — 鮮度を証明できない")
    return meta, rows


def main() -> int:
    ap = argparse.ArgumentParser(description="navi へ渡す WP 需要の派生物を作る (#97 P2)")
    ap.add_argument("--src", default=DEFAULT_SRC)
    ap.add_argument("--totals", default=DEFAULT_TOTALS)
    ap.add_argument("--weeks", type=int, default=DEFAULT_WEEKS)
    ap.add_argument("--min-impressions", type=int, default=DEFAULT_MIN_IMPRESSIONS)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    meta, rows = build(pathlib.Path(args.src), pathlib.Path(args.totals),
                       args.weeks, args.min_impressions)
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with io.open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(meta, ensure_ascii=False) + "\n")
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print("%s: %d rows (%s..%s, last_date=%s)"
          % (out, len(rows), meta["first_week"], meta["last_week"],
             meta["window_last_date"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
