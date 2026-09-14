"""#4841 T2「成果指標を決めるための材料を作る」の計算スクリプト。

指標を決めるのは owner + 母艦 (本スクリプトの役割ではない)。ここでは
「どの指標なら、現実的な記事数(N)と期間(W週)で差を検出できるか」を、
リポジトリ内の実測データ (data/analytics/gsc_history/* 他) から計算する。

read-only。外部 API は呼ばない (リポジトリに committed 済みのファイルだけを読む)。

出すもの (Issue #4841 T2 の要求):
  1. ページ別の分布 (表示>0の割合、表示回数のp50/p90、打ち切りの影響)
  2. 検出力の計算 (CTR/表示回数/クリック数 x N=10/30/100 x W=4/8週の表)
  3. 代替指標候補の実測可否
  4. 推奨案

Issue: https://github.com/omochairo/amazon/issues/4841 (T2)
"""
from __future__ import annotations

import glob
import json
import pathlib
import re
import statistics
from datetime import date
from typing import Any

from scripts.compute_semantic_related import discover_articles

DEFAULT_HISTORY_DIR = "data/analytics/gsc_history"
DEFAULT_ARTICLES_DIR = "data/articles"
DEFAULT_INDEX_CENSUS = "data/analytics/gsc_index_census.json"
DEFAULT_INDEX_CENSUS_HISTORY = "data/analytics/history/gsc_index_census.jsonl"
DEFAULT_ANSWERABILITY_AUDIT = "data/analytics/answerability_audit.json"
DEFAULT_OUT = "docs/multistage-generation-eval/power_analysis.json"

_PAGE_ASIN_RE = re.compile(r"/products/([a-zA-Z0-9]+)/?(?:$|[?#])")
_ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")

# 標準正規分布の分位点。alpha=0.05 (両側)・power=0.80 は業界標準の既定値
# (Evan Miller の A/B テストサンプルサイズ計算機などと同じ選択)。scipy が
# 無い環境でも動くよう定数として埋め込む (норм.ppf(1-0.05/2), norm.ppf(0.80))。
Z_ALPHA_05_TWO_SIDED = 1.9599639845400545
Z_BETA_POWER_80 = 0.8416212335729143

N_VALUES = (10, 30, 100)
W_VALUES = (4, 8)


# --------------------------------------------------------------------------
# 1. ページ別の分布
# --------------------------------------------------------------------------

def extract_asin_from_page(page: str) -> str | None:
    m = _PAGE_ASIN_RE.search(page or "")
    if not m:
        return None
    asin = m.group(1).upper()
    return asin if _ASIN_RE.match(asin) else None


def load_weekly_history(history_dir: str | pathlib.Path = DEFAULT_HISTORY_DIR) -> list[dict[str, Any]]:
    """data/analytics/gsc_history/*.json を週ラベル昇順で読み込む。"""
    out = []
    for path in sorted(glob.glob(str(pathlib.Path(history_dir) / "*.json"))):
        try:
            data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        data["_week_label"] = pathlib.Path(path).stem
        out.append(data)
    return out


def page_level_distribution(
    weeks: list[dict[str, Any]], total_article_pages: int,
) -> dict[str, Any]:
    """記事ページ (by_page のうち /products/<asin>/ にマッチする行) の分布を出す。

    truncated_pages=True (rowLimit=100/週) のため、この分布は
    「その週のトップ100に入れた記事ページ」の分布であり、母集団 (全記事ページ)
    の分布ではない。上振れバイアスを明記した上で、それでも分かることを出す。
    """
    weeks_available = len(weeks)
    weeks_truncated = sum(1 for w in weeks if w.get("totals", {}).get("truncated_pages"))

    impressions_per_page_week: list[int] = []
    clicks_per_page_week: list[int] = []
    union_asins_with_impressions: set[str] = set()

    for w in weeks:
        for row in w.get("by_page", []):
            asin = extract_asin_from_page(row.get("page", ""))
            if not asin:
                continue
            impressions = row.get("impressions", 0)
            if impressions and impressions > 0:
                impressions_per_page_week.append(impressions)
                union_asins_with_impressions.add(asin)
            clicks_per_page_week.append(row.get("clicks", 0))

    lower_bound_fraction_with_impressions = (
        round(len(union_asins_with_impressions) / total_article_pages, 4) if total_article_pages else None
    )

    def _pct(values: list[int], pct: float) -> float | None:
        if not values:
            return None
        s = sorted(values)
        idx = min(len(s) - 1, max(0, round(pct / 100 * (len(s) - 1))))
        return float(s[idx])

    # totals.impressions_sitewide (dimensionless query による真のサイト全体集計) は
    # W31 以降にしか存在しない (それより前の週次ファイルはこのフィールド自体を
    # 持たない)。None を 0 として合算すると平均が大きく下振れするため、
    # 値が存在する週だけを対象にする。
    weeks_with_sitewide_totals = [
        w for w in weeks
        if w.get("totals", {}).get("impressions_sitewide") is not None
    ]
    sitewide_trend = [
        {
            "week": w["_week_label"],
            "impressions_sitewide": w["totals"]["impressions_sitewide"],
            "clicks_sitewide": w["totals"].get("clicks_sitewide"),
            "ctr_sitewide": w["totals"].get("ctr_sitewide"),
        }
        for w in weeks_with_sitewide_totals
    ]
    latest = weeks_with_sitewide_totals[-1] if weeks_with_sitewide_totals else None
    latest_impressions = latest["totals"]["impressions_sitewide"] if latest else None
    latest_clicks = latest["totals"].get("clicks_sitewide") if latest else None

    return {
        "weeks_available": weeks_available,
        "week_labels": [w["_week_label"] for w in weeks],
        "weeks_with_page_truncation": weeks_truncated,
        "weeks_with_sitewide_totals_field": len(weeks_with_sitewide_totals),
        "sitewide_totals_trend": sitewide_trend,
        "total_article_pages": total_article_pages,
        "distinct_article_pages_ever_with_impressions_top100": len(union_asins_with_impressions),
        "lower_bound_fraction_with_impressions": lower_bound_fraction_with_impressions,
        "truncation_caveat": (
            "by_page は週あたり rowLimit=100 で打ち切られている (GSC API既定でクリック数上位から返る)。"
            "distinct_article_pages_ever_with_impressions_top100 と lower_bound_fraction_with_impressions は"
            "そのため「トップ100に入れたことがある記事」の下限値であり、真の「表示>0の記事」の割合は"
            "これ以上 (打ち切りで見えていない低表示ページがあるため)"
        ),
        "impressions_per_page_week_observed": {
            "n": len(impressions_per_page_week),
            "p50": _pct(impressions_per_page_week, 50),
            "p90": _pct(impressions_per_page_week, 90),
            "caveat": "トップ100サンプルのみの分布 (上振れバイアスあり、母集団の中央値ではない)",
        },
        "avg_impressions_per_article_per_week_sitewide": (
            round(latest_impressions / total_article_pages, 4)
            if latest_impressions and total_article_pages else None
        ),
        "avg_clicks_per_article_per_week_sitewide": (
            round(latest_clicks / total_article_pages, 4)
            if latest_clicks and total_article_pages else None
        ),
        "note_avg_uses_sitewide_totals": (
            "avg_*_per_article_per_week は、直近1週 (totals.impressions_sitewide 等が存在する最新週、"
            "打ち切りの影響を受けない真の合計) を記事ページ総数で割った値。"
            "sitewide_totals_trend が示す通り表示回数は7週で約3倍に増加しており急成長中のため、"
            "複数週の平均ではなく直近週を使う (平均すると過去の低い時期に引かれて過小評価する)。"
            "検出力計算のベースラインはこちらを使う (トップ100サンプルの平均は逆に上振れするため使わない)"
        ),
    }


# --------------------------------------------------------------------------
# 2. 検出力の計算
# --------------------------------------------------------------------------

def _required_n_two_proportion(p1: float, p2: float) -> float:
    """両側 alpha=0.05・power=0.80 で p1 vs p2 の差を検出するために必要な、群あたりの n。

    標準的な2標本比率検定のサンプルサイズ公式 (pooled variance for alpha 項、
    unpooled variance for beta 項)。
    """
    p1 = min(max(p1, 1e-9), 1 - 1e-9)
    p2 = min(max(p2, 1e-9), 1 - 1e-9)
    pbar = (p1 + p2) / 2
    numerator = (
        Z_ALPHA_05_TWO_SIDED * (2 * pbar * (1 - pbar)) ** 0.5
        + Z_BETA_POWER_80 * (p1 * (1 - p1) + p2 * (1 - p2)) ** 0.5
    )
    return (numerator ** 2) / ((p2 - p1) ** 2)


def mde_two_proportion(p1: float, n_per_group: float, *, max_relative: float = 5.0) -> float | None:
    """群あたり n_per_group (試行数) が固定のとき、検出できる最小の相対差 (|p2-p1|/p1) を二分探索で求める。

    p2 を p1 から遠ざけるほど required_n は小さくなる (単調減少) ので、
    required_n(p2) == n_per_group となる点を二分探索で探す。
    ``max_relative`` (既定 500%) まで探しても見つからなければ None
    (この n では検出不能なほど大きい効果しか検出できない、という意味)。
    """
    if n_per_group <= 0 or p1 <= 0:
        return None

    lo, hi = 0.0, max_relative
    # hi 側で required_n が n_per_group を下回っているか確認 (下回らなければ探索範囲外)
    p2_hi = min(p1 * (1 + hi), 1 - 1e-9)
    if _required_n_two_proportion(p1, p2_hi) > n_per_group:
        return None

    for _ in range(60):
        mid = (lo + hi) / 2
        p2 = min(p1 * (1 + mid), 1 - 1e-9)
        if _required_n_two_proportion(p1, p2) > n_per_group:
            lo = mid
        else:
            hi = mid
    return round(hi, 4)


def mde_poisson_mean(mean1: float, n_per_group: float) -> float | None:
    """Poisson近似 (分散=平均) での、群あたり n_per_group 観測 (page-week単位) のときの最小検出相対差。

    平均の差の検定: (mean2-mean1) = (z_a+z_b) * sqrt(var1/n + var2/n)、
    var≈mean (Poisson)、mean2≈mean1*(1+e) の小効果近似から
    e = (z_a+z_b) * sqrt(2/(mean1*n)) の閉形式で求まる。
    """
    if mean1 <= 0 or n_per_group <= 0:
        return None
    e = (Z_ALPHA_05_TWO_SIDED + Z_BETA_POWER_80) * (2 / (mean1 * n_per_group)) ** 0.5
    return round(e, 4)


def build_power_table(
    *,
    baseline_ctr: float,
    avg_impressions_per_article_week: float,
    avg_clicks_per_article_week: float,
) -> dict[str, Any]:
    """N (記事数) x W (週数) の表を、CTR/表示回数/クリック数それぞれについて作る。

    - CTR: n_per_group (2値試行数) = N * W * avg_impressions_per_article_week
      (その群の記事が生む表示回数の合計を、クリックの成否を占う試行数として使う)
    - 表示回数: n_per_group (観測単位数) = N * W (記事x週の観測数)、平均は
      avg_impressions_per_article_week (1記事1週あたりの表示回数、Poisson近似)
    - クリック数: 表示回数と同じ n_per_group、平均は avg_clicks_per_article_week
    """
    table: dict[str, list[dict[str, Any]]] = {"ctr": [], "impressions": [], "clicks": []}
    for n in N_VALUES:
        for w in W_VALUES:
            n_trials_ctr = n * w * avg_impressions_per_article_week
            n_obs_count = n * w
            table["ctr"].append({
                "N": n, "W": w, "n_impressions_per_group": round(n_trials_ctr, 1),
                "mde_relative": mde_two_proportion(baseline_ctr, n_trials_ctr),
            })
            table["impressions"].append({
                "N": n, "W": w, "n_page_weeks_per_group": n_obs_count,
                "mde_relative": mde_poisson_mean(avg_impressions_per_article_week, n_obs_count),
            })
            table["clicks"].append({
                "N": n, "W": w, "n_page_weeks_per_group": n_obs_count,
                "mde_relative": mde_poisson_mean(avg_clicks_per_article_week, n_obs_count),
            })
    return table


# --------------------------------------------------------------------------
# 3. 代替指標候補の実測可否
# --------------------------------------------------------------------------

def check_index_census_candidate(
    census_path: str | pathlib.Path = DEFAULT_INDEX_CENSUS,
    history_path: str | pathlib.Path = DEFAULT_INDEX_CENSUS_HISTORY,
) -> dict[str, Any]:
    p = pathlib.Path(census_path)
    if not p.exists():
        return {"name": "indexされたか", "measurable": False, "reason": "gsc_index_census.json が無い"}
    census = json.loads(p.read_text(encoding="utf-8"))
    totals = census.get("totals", {})
    indexed_rate = (
        round(totals["indexed"] / totals["inspected"], 4)
        if totals.get("inspected") else None
    )

    history_rates = []
    hp = pathlib.Path(history_path)
    if hp.exists():
        for line in hp.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row.get("indexed_rate"), (int, float)):
                history_rates.append({"date": row.get("date"), "indexed_rate": row["indexed_rate"]})

    return {
        "name": "indexされたか (URL Inspection API)",
        "measurable": True,
        "baseline_indexed_rate": indexed_rate,
        "totals": totals,
        "history_rate_trend": history_rates,
        "note": (
            "記事単位のYES/NOで、公開直後にURL Inspection APIで個別ASINを指定して取得できる"
            "(既存のcensusはwatchlistのローテーション対象のみを回しているが、"
            "実験群のN件のURLを明示指定すれば待たずに測れる)。"
            "週次のトラフィック蓄積が要らないため、他候補よりW週の制約を受けにくい"
        ),
    }


def check_time_to_first_impression_candidate(
    weeks: list[dict[str, Any]], articles_dir: str | pathlib.Path = DEFAULT_ARTICLES_DIR,
    recent_cutoff: str | None = None,
) -> dict[str, Any]:
    """直近公開記事のうち、観測期間中に一度でも by_page (トップ100) に載ったものの割合を実測する。"""
    article_paths = discover_articles(pathlib.Path(articles_dir))
    publish_dates: dict[str, str] = {}
    for asin, path in article_paths.items():
        try:
            article = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        d = article.get("date") if isinstance(article, dict) else None
        if isinstance(d, str) and d:
            publish_dates[asin] = d[:10]

    if recent_cutoff is None and weeks:
        week_labels = sorted(w["_week_label"] for w in weeks)
        recent_cutoff = _iso_week_label_to_monday(week_labels[0])

    recent_asins = {a for a, d in publish_dates.items() if recent_cutoff and d >= recent_cutoff}

    union_asins_with_impressions: set[str] = set()
    for w in weeks:
        for row in w.get("by_page", []):
            asin = extract_asin_from_page(row.get("page", ""))
            if asin and row.get("impressions", 0) > 0:
                union_asins_with_impressions.add(asin)

    observable = recent_asins & union_asins_with_impressions
    hit_rate = round(len(observable) / len(recent_asins), 4) if recent_asins else None

    return {
        "name": "表示が出始めるまでの日数",
        "measurable": False,
        "recent_articles_in_window": len(recent_asins),
        "recent_articles_ever_observed_in_top100": len(observable),
        "observable_hit_rate": hit_rate,
        "reason": (
            f"観測期間中に公開された記事 {len(recent_asins)} 件のうち、"
            f"by_page トップ100に一度でも載ったのは {len(observable)} 件 ({hit_rate:.1%} 相当) しかなく"
            if recent_asins else "観測期間中に公開された記事が無い"
        ) + (
            "、残りは『表示が0だった』のか『表示はあったがトップ100に入らなかった』のか"
            "区別できない (左側打ち切り)。一般的な記事について代表性のある分布を計算できない"
            if recent_asins else ""
        ),
    }


def _iso_week_label_to_monday(label: str) -> str:
    """"2026-W23" のようなラベルから、そのISO週の月曜日 (YYYY-MM-DD) を返す。"""
    year_s, week_s = label.split("-W")
    return date.fromisocalendar(int(year_s), int(week_s), 1).isoformat()


def check_answerability_coverage_candidate(
    path: str | pathlib.Path = DEFAULT_ANSWERABILITY_AUDIT,
) -> dict[str, Any]:
    p = pathlib.Path(path)
    if not p.exists():
        return {"name": "#2995 回答性カバレッジ", "measurable": False, "reason": "answerability_audit.json が無い"}
    data = json.loads(p.read_text(encoding="utf-8"))
    pages = data.get("pages", [])
    covs = [pg["page_coverage_avg"] for pg in pages if isinstance(pg.get("page_coverage_avg"), (int, float))]

    return {
        "name": "#2995 回答性カバレッジ (gemma judge の coverage)",
        "measurable": False,
        "current_sample_size": len(covs),
        "current_mean": round(statistics.mean(covs), 4) if covs else None,
        "reason": (
            f"現状は detect_low_ctr_pages が検出した低CTRページ {len(covs)} 件にしか計算されておらず、"
            "任意のN件の記事に汎用的に使えるわけではない。"
            "判定対象のクエリ (top_queries) はそのページに既に蓄積したGSCクリック/表示データから取るため、"
            "公開直後で表示がほぼ無い実験群の記事には適用できない (鶏と卵)。"
            "audit_query_entailment.judge_query 自体は任意の(本文,クエリ)ペアに使えるので、"
            "『クエリの代わりにkeywordsフィールドを使う』等の改修をすれば汎用化できる可能性はあるが、未実装"
        ),
    }


def build_report(
    *,
    history_dir: str | pathlib.Path = DEFAULT_HISTORY_DIR,
    articles_dir: str | pathlib.Path = DEFAULT_ARTICLES_DIR,
) -> dict[str, Any]:
    weeks = load_weekly_history(history_dir)
    total_article_pages = len(discover_articles(pathlib.Path(articles_dir)))

    distribution = page_level_distribution(weeks, total_article_pages)
    baseline_ctr = weeks[-1]["totals"]["ctr_sitewide"] if weeks else None
    power_table = build_power_table(
        baseline_ctr=baseline_ctr,
        avg_impressions_per_article_week=distribution["avg_impressions_per_article_per_week_sitewide"],
        avg_clicks_per_article_week=distribution["avg_clicks_per_article_per_week_sitewide"],
    ) if baseline_ctr else None

    candidates = [
        check_index_census_candidate(),
        check_time_to_first_impression_candidate(weeks, articles_dir),
        check_answerability_coverage_candidate(),
    ]

    return {
        "assumptions": {"alpha_two_sided": 0.05, "power": 0.80},
        "page_level_distribution": distribution,
        "baseline_ctr_sitewide_latest_week": baseline_ctr,
        "power_table": power_table,
        "alternative_metric_candidates": candidates,
    }


def main() -> int:
    report = build_report()
    out_path = pathlib.Path(DEFAULT_OUT)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
