"""check_wp_navi_link_gate.py

#3988 C-1 / 対象 #3333: WP omcha.jp (~トラフィックの97%) から navi.omcha.jp
(Hugo, Amazon アフィリエイト自動生成ページ) への文脈リンクを追加してよいかの
ゲート判定スクリプト。

なぜ必要か:
  #3333 単体は妥当な提案だが、navi のコーパスは近似重複が多い (#3203)。今それを
  やると、価値ある資産 (WP) と低品質コーパス (navi) の結合を意図的に強めることに
  なる。したがって意思決定は navi の品質が測定可能な基準に達したかどうかにゲート
  される。本スクリプトはそのゲート判定を計算する。

判定する 2 基準 (両方 pass のときのみ実施 GO):

  C1 (コーパス脱凡庸化): data/analytics/uniqueness_audit.json の
     cohort_stats.all.centroid_sim_p50 が --centroid-max (既定 0.88) 未満か。
     cohort_stats.all が無い/None の JSON は PR #3987 より前のスキーマ
     (pre_v7/post_v7 のみ) であり、その場合は status "unknown"。**pre_v7/post_v7
     の中央値へは絶対にフォールバックしない** — サブグループの中央値を合成して
     コーパス全体の中央値として扱うのは統計的に不当であり、誤った数値でこの
     ハイリスクな変更を green-light しかねないため。

  C2 (クロール品質トレンド): data/analytics/history/gsc_index_census.jsonl
     (#3989 で追加、1 行 1 JSON) の crawled_not_indexed
     (coverageState "クロール済み - インデックス未登録" = Google が取得したが
     index を見送ったページ数。Google 自身の品質判定に最も近いシグナル) を見る。
     circuit_breaker_tripped な行 (#3372, 部分実行) は比較不能なので除外する。
     残った行を date 昇順に並べ、直近 --trend-window (既定 3) 件を window として
     window[-1] < window[0] (窓内で正味減少) なら pass。使える行が --min-points
     (既定 3) 未満なら "unknown"。3 点への傾き回帰は偽の精密さになるため意図的に
     行わない — 単純な「窓の最初と最後を比べる」に留める。ファイルが存在しない
     場合も "unknown" (#3989 マージ後の最初の census cron で作られる)。

verdict 解決 (最重要):
  - go: 両基準が pass
  - hold: どちらかが fail (fail は unknown より情報量が多いので unknown より優先)
  - insufficient_data: fail は無いが、どちらかが unknown

  **unknown は絶対に go に寄与しない。** navi は WP (~97%トラフィック) という
  価値ある資産と結合されようとしている対象であり、「証拠が無い」ことを
  「安全である証拠」として読んではならない、というのがこのスクリプトの核。

exit code:
  既定 (非 --strict) は verdict に関わらず常に 0 — これは観察レーンであり、
  workflow を fail させてはならない。--strict を渡したときのみ、verdict が
  "go" でなければ exit 1。

呼び出し元: .github/workflows/17-analytics-report.yml (C-1, #3988)
Issue: https://github.com/omochairo/amazon/issues/3988 (C-1) / #3333 (対象) /
       #3203 (uniqueness_audit) / #3372 (circuit breaker) / #3987・#3989 (前提PR)
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

DEFAULT_UNIQUENESS = "data/analytics/uniqueness_audit.json"
DEFAULT_CENSUS_HISTORY = "data/analytics/history/gsc_index_census.jsonl"
DEFAULT_CENTROID_MAX = 0.88
DEFAULT_TREND_WINDOW = 3
DEFAULT_MIN_POINTS = 3

STATUS_EMOJI = {"go": "✅", "hold": "⏸", "insufficient_data": "❓"}
VERDICT_LABEL = {
    "go": "GO — WP → navi リンク追加を実施してよい",
    "hold": "HOLD — 基準未達。リンク追加は見送り",
    "insufficient_data": "INSUFFICIENT DATA — 判定に必要なデータが不足",
}


# --------------------------------------------------------------------------
# 基準判定 (pure function, unit-testable)
# --------------------------------------------------------------------------

def evaluate_c1(
    uniqueness_data: dict[str, Any] | None, centroid_max: float = DEFAULT_CENTROID_MAX
) -> dict[str, Any]:
    """C1 (コーパス脱凡庸化) を判定する。

    cohort_stats.all.centroid_sim_p50 が無い/None のときは絶対に
    pre_v7/post_v7 へフォールバックしない (docstring 冒頭の説明を参照)。
    """
    required = f"< {centroid_max}"
    if not isinstance(uniqueness_data, dict):
        return {
            "status": "unknown", "actual": None, "required": required,
            "reason": "uniqueness_audit.json が読み込めない",
        }

    cohort_stats = uniqueness_data.get("cohort_stats")
    all_stats = cohort_stats.get("all") if isinstance(cohort_stats, dict) else None
    centroid = all_stats.get("centroid_sim_p50") if isinstance(all_stats, dict) else None

    if centroid is None:
        return {
            "status": "unknown", "actual": None, "required": required,
            "reason": (
                "cohort_stats.all.centroid_sim_p50 が無い (PR #3987 より前の旧スキーマの"
                "可能性。pre_v7/post_v7 中央値へはフォールバックしない)"
            ),
        }

    status = "pass" if centroid < centroid_max else "fail"
    return {"status": status, "actual": centroid, "required": required, "reason": None}


def filter_usable_census_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """circuit_breaker_tripped な行 (#3372, 部分実行) を除いた行を返す。"""
    return [r for r in rows if isinstance(r, dict) and not r.get("circuit_breaker_tripped")]


def select_trend_window(rows: list[dict[str, Any]], trend_window: int) -> list[dict[str, Any]]:
    """date 昇順に並べ、直近 trend_window 件 (0以下なら全件) を返す。"""
    ordered = sorted(rows, key=lambda r: r.get("date") or "")
    if trend_window and trend_window > 0:
        return ordered[-trend_window:]
    return ordered


def evaluate_c2(
    census_rows: list[dict[str, Any]],
    *,
    trend_window: int = DEFAULT_TREND_WINDOW,
    min_points: int = DEFAULT_MIN_POINTS,
) -> dict[str, Any]:
    """C2 (クロール品質トレンド) を判定する。

    3 点への傾き回帰はしない (docstring 冒頭の説明を参照) — window の最初と最後の
    crawled_not_indexed を単純比較するだけの、意図的に保守的な判定。
    """
    required = "直近ウィンドウで crawled_not_indexed が正味減少"
    usable = filter_usable_census_rows(census_rows)
    if len(usable) < min_points:
        return {
            "status": "unknown", "actual": None, "required": required,
            "reason": (
                f"circuit_breaker_tripped を除いた使用可能な行が {len(usable)} 件"
                f" (--min-points {min_points} 未満)"
            ),
        }

    window = select_trend_window(usable, trend_window)
    first = window[0]
    last = window[-1]
    first_val = first.get("crawled_not_indexed")
    last_val = last.get("crawled_not_indexed")

    if first_val is None or last_val is None:
        return {
            "status": "unknown", "actual": None, "required": required,
            "reason": "window 内に crawled_not_indexed が欠けている行がある",
        }

    status = "pass" if last_val < first_val else "fail"
    return {
        "status": status,
        "actual": {
            "first_date": first.get("date"), "first_value": first_val,
            "last_date": last.get("date"), "last_value": last_val,
            "window_size": len(window),
        },
        "required": required,
        "reason": None,
    }


def resolve_verdict(c1_status: str, c2_status: str) -> str:
    """2 基準の status から overall verdict を決める。

    - go: 両方 pass
    - hold: どちらかが fail (fail は unknown より優先 — 定義済みの fail の方が
      unknown より情報量が多い)
    - insufficient_data: fail は無いが、どちらかが unknown

    unknown は絶対に go に寄与しない。
    """
    statuses = (c1_status, c2_status)
    if any(s == "fail" for s in statuses):
        return "hold"
    if any(s == "unknown" for s in statuses):
        return "insufficient_data"
    if all(s == "pass" for s in statuses):
        return "go"
    # 到達しないはずだが、未知の status 値が紛れ込んだ場合は安全側 (hold) に倒す。
    return "hold"


# --------------------------------------------------------------------------
# context (verdict に影響しない、人間可読の参考情報)
# --------------------------------------------------------------------------

def build_context(
    uniqueness_data: dict[str, Any] | None, census_rows: list[dict[str, Any]]
) -> dict[str, Any]:
    """人間可読の参考メトリクスを組み立てる。verdict には一切影響しない。"""
    ctx: dict[str, Any] = {
        "corpus_size": None, "centroid_sim_p50": None,
        "census_date": None, "indexed": None, "inspected": None,
        "indexed_rate": None, "not_found_404": None,
    }
    if isinstance(uniqueness_data, dict):
        ctx["corpus_size"] = uniqueness_data.get("corpus_size")
        cohort_stats = uniqueness_data.get("cohort_stats")
        all_stats = cohort_stats.get("all") if isinstance(cohort_stats, dict) else None
        if isinstance(all_stats, dict):
            ctx["centroid_sim_p50"] = all_stats.get("centroid_sim_p50")

    usable = filter_usable_census_rows(census_rows)
    if usable:
        latest = sorted(usable, key=lambda r: r.get("date") or "")[-1]
        ctx["census_date"] = latest.get("date")
        ctx["indexed"] = latest.get("indexed")
        ctx["inspected"] = latest.get("inspected")
        ctx["indexed_rate"] = latest.get("indexed_rate")
        ctx["not_found_404"] = latest.get("not_found_404")
    return ctx


# --------------------------------------------------------------------------
# 全体計算 (pure function、IO と分離)
# --------------------------------------------------------------------------

def compute_gate(
    uniqueness_data: dict[str, Any] | None,
    census_rows: list[dict[str, Any]],
    *,
    centroid_max: float = DEFAULT_CENTROID_MAX,
    trend_window: int = DEFAULT_TREND_WINDOW,
    min_points: int = DEFAULT_MIN_POINTS,
) -> dict[str, Any]:
    c1 = evaluate_c1(uniqueness_data, centroid_max)
    c2 = evaluate_c2(census_rows, trend_window=trend_window, min_points=min_points)
    verdict = resolve_verdict(c1["status"], c2["status"])
    return {
        "verdict": verdict,
        "criteria": {
            "c1_corpus_degenericization": c1,
            "c2_crawl_quality_trend": c2,
        },
        "context": build_context(uniqueness_data, census_rows),
    }


# --------------------------------------------------------------------------
# IO
# --------------------------------------------------------------------------

def load_uniqueness(path: pathlib.Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def load_census_rows(path: pathlib.Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


# --------------------------------------------------------------------------
# レンダリング (Markdown, $GITHUB_STEP_SUMMARY 向け)
# --------------------------------------------------------------------------

def _fmt_ratio(v: Any) -> str:
    if isinstance(v, (int, float)):
        return f"{v:.4f}"
    return "n/a"

def _fmt_pct(v: Any) -> str:
    if isinstance(v, (int, float)):
        return f"{v * 100:.1f}%"
    return "n/a"

def _fmt_int(v: Any) -> str:
    if isinstance(v, int):
        return f"{v:,}"
    return "n/a"


def _render_c1_actual(c1: dict[str, Any]) -> str:
    if c1["status"] == "unknown":
        return "n/a"
    return _fmt_ratio(c1["actual"])


def _render_c2_actual(c2: dict[str, Any]) -> str:
    if c2["status"] == "unknown":
        return "n/a"
    a = c2["actual"]
    return f"{a['first_value']} ({a['first_date']}) → {a['last_value']} ({a['last_date']})"


def render_markdown(result: dict[str, Any]) -> str:
    verdict = result["verdict"]
    c1 = result["criteria"]["c1_corpus_degenericization"]
    c2 = result["criteria"]["c2_crawl_quality_trend"]
    ctx = result["context"]

    lines: list[str] = []
    lines.append("## 🔗 WP → navi リンクゲート判定 (#3988 C-1 / 対象 #3333)")
    lines.append("")
    lines.append(
        f"### {STATUS_EMOJI.get(verdict, '')} verdict: `{verdict}` — {VERDICT_LABEL.get(verdict, '')}"
    )
    lines.append("")
    lines.append(
        "WP omcha.jp (~トラフィックの97%) から navi.omcha.jp への文脈リンク追加は、"
        "navi コーパスの品質が以下 2 基準を満たすまで見送る運用ゲートです。"
    )
    lines.append("")
    lines.append("### 判定基準")
    lines.append("")
    lines.append("| 基準 | status | actual | required |")
    lines.append("|---|---|---|---|")
    lines.append(
        f"| C1: コーパス脱凡庸化 (centroid_sim_p50) | {c1['status']} | "
        f"{_render_c1_actual(c1)} | {c1['required']} |"
    )
    lines.append(
        f"| C2: クロール品質トレンド (crawled_not_indexed) | {c2['status']} | "
        f"{_render_c2_actual(c2)} | {c2['required']} |"
    )
    lines.append("")

    reasons = [c["reason"] for c in (c1, c2) if c.get("reason")]
    if reasons:
        lines.append("### 判定できなかった理由")
        lines.append("")
        for r in reasons:
            lines.append(f"- {r}")
        lines.append("")

    lines.append("### context (参考値・verdict には影響しません)")
    lines.append("")
    lines.append(f"- コーパスサイズ (uniqueness_audit): {_fmt_int(ctx['corpus_size'])} 記事")
    lines.append(f"- centroid_sim_p50 (生値): {_fmt_ratio(ctx['centroid_sim_p50'])}")
    if ctx["census_date"]:
        lines.append(
            f"- 直近 census ({ctx['census_date']}): "
            f"index済み {_fmt_int(ctx['indexed'])} / 検査 {_fmt_int(ctx['inspected'])} "
            f"({_fmt_pct(ctx['indexed_rate'])}), "
            f"404 (未検出, #3331 回復数): {_fmt_int(ctx['not_found_404'])}"
        )
    else:
        lines.append("- census 履歴: n/a")
    lines.append("")

    if verdict != "go":
        lines.append("### 次に必要なこと")
        lines.append("")
        needs: list[str] = []
        if c1["status"] != "pass":
            needs.append(
                f"C1: centroid_sim_p50 を {c1['required']} まで下げる "
                "(uniqueness_audit.json の cohort_stats.all が揃うのを待つ、または凡庸度改善)"
            )
        if c2["status"] != "pass":
            needs.append(
                "C2: crawled_not_indexed の減少トレンドを複数週分観測する "
                "(gsc_index_census.jsonl の蓄積を待つ)"
            )
        for n in needs:
            lines.append(f"- {n}")
        lines.append("")

    return "\n".join(lines)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--uniqueness", default=DEFAULT_UNIQUENESS,
                   help="uniqueness_audit.json のパス")
    p.add_argument("--census-history", default=DEFAULT_CENSUS_HISTORY,
                   help="gsc_index_census.jsonl のパス")
    p.add_argument("--centroid-max", type=float, default=DEFAULT_CENTROID_MAX,
                   help="C1 のしきい値 (centroid_sim_p50 がこの値未満で pass、既定 0.88)")
    p.add_argument("--trend-window", type=int, default=DEFAULT_TREND_WINDOW,
                   help="C2 のトレンド判定に使う直近行数 (既定 3)")
    p.add_argument("--min-points", type=int, default=DEFAULT_MIN_POINTS,
                   help="C2 判定に必要な最小使用可能行数 (未満なら unknown、既定 3)")
    p.add_argument("--json-out", default=None,
                   help="機械可読な verdict dict の書き出し先 (任意)")
    p.add_argument("--strict", action="store_true",
                   help="verdict が go でなければ exit 1 (既定は常に exit 0)")
    args = p.parse_args()

    uniqueness_data = load_uniqueness(pathlib.Path(args.uniqueness))
    census_rows = load_census_rows(pathlib.Path(args.census_history))

    result = compute_gate(
        uniqueness_data, census_rows,
        centroid_max=args.centroid_max,
        trend_window=args.trend_window,
        min_points=args.min_points,
    )

    print(render_markdown(result))

    if args.json_out:
        out_path = pathlib.Path(args.json_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.strict and result["verdict"] != "go":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
