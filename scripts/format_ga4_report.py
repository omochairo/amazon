"""format_ga4_report.py

`data/analytics/ga4_weekly.json` を読み、週次 Issue 本文用の markdown を stdout に出す。
17-analytics-report.yml から呼ばれ、`gh issue create` の body にそのまま流し込む。

Issue: https://github.com/omochairo/amazon/issues/1316 (Phase 1)
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

TOP_PAGE_DEFAULT = 20
TOP_SOURCE_DEFAULT = 10
XMAS_PAGES_KEY = "/xmas-age-*/"
_XMAS_PATH_RE = re.compile(r"/xmas-age-\d+/")
FEATURE_PAGES = {
    "/diagnosis/": "診断",
    "/ranking/": "ランキング",
    "/cospa/": "予算別",
    "/deals/": "おトク",
    "/price/": "値下げ",
    # navi-brain#166 F10: クリスマス × 年齢の 7 ページ (/xmas-age-N/) はまとめて 1 列
    XMAS_PAGES_KEY: "クリスマス",
}
DIAGNOSIS_EVENTS = ("diagnosis_start", "diagnosis_complete",
                    "diagnosis_pick_click", "diagnosis_share")


def _normalize_page_path(path: str) -> str:
    path = (path or "").split("?", 1)[0]
    if path.endswith("index.html"):
        path = path[:-len("index.html")]
    if path and not path.endswith("/"):
        path += "/"
    return path


def _month_label(year_month: str) -> str:
    """GA4 の yearMonth (YYYYMM) を YYYY-MM にする。形が違えばそのまま返す。"""
    ym = str(year_month)
    return f"{ym[:4]}-{ym[4:]}" if len(ym) == 6 and ym.isdigit() else ym


def summarize_feature_events(rows: list[dict]) -> list[dict]:
    """GA4 event rows を月別の診断ファネル・特集ページクリックへ集約する。"""
    months: dict[str, dict] = {}
    for row in rows:
        month = str(row.get("yearMonth", ""))
        if not month:
            continue
        summary = months.setdefault(month, {
            "yearMonth": month, **{event: 0 for event in DIAGNOSIS_EVENTS},
            "affiliate": {path: 0 for path in FEATURE_PAGES},
            "affiliate_total": 0,
        })
        count = int(row.get("eventCount", 0) or 0)
        path = _normalize_page_path(row.get("pagePath", ""))
        event = row.get("eventName", "")
        if event in DIAGNOSIS_EVENTS and path == "/diagnosis/":
            summary[event] += count
        elif event == "affiliate_click":
            key = XMAS_PAGES_KEY if _XMAS_PATH_RE.fullmatch(path) else path
            if key in FEATURE_PAGES:
                summary["affiliate"][key] += count
                summary["affiliate_total"] += count
    return [months[key] for key in sorted(months)]


def _fmt_int(v) -> str:
    try:
        return f"{int(v):,}"
    except (TypeError, ValueError):
        return str(v)


def _fmt_dur(sec) -> str:
    try:
        s = float(sec)
    except (TypeError, ValueError):
        return "-"
    if s < 60:
        return f"{s:.1f}s"
    m, s = divmod(s, 60)
    return f"{int(m)}m{int(s):02d}s"


def _fmt_rate(v) -> str:
    try:
        return f"{float(v) * 100:.1f}%"
    except (TypeError, ValueError):
        return "-"


def render(data: dict, top_page: int, top_source: int) -> str:
    r = data.get("range", {})
    t = data.get("totals", {})
    by_host = data.get("by_host", [])
    by_page = data.get("by_page", [])[:top_page]
    by_device = data.get("by_device", [])
    by_source = data.get("by_source", [])[:top_source]

    lines: list[str] = []
    lines.append(f"## GA4 週次レポート ({r.get('start', '?')} 〜 {r.get('end', '?')})")
    lines.append("")
    lines.append(f"- 取得時刻: `{data.get('fetched_at', '')}`")
    lines.append(f"- Property: `{data.get('property_id', '')}`")
    lines.append(f"- 合計 PV: **{_fmt_int(t.get('screenPageViews_sum'))}**")
    lines.append(f"- 合計 engagedSessions: **{_fmt_int(t.get('engagedSessions_sum'))}**")
    lines.append("")

    if by_host:
        lines.append("### サイト別合計 (cross-domain)")
        lines.append("| hostName | PV | engaged | avg dur | bounce |")
        lines.append("|---|---:|---:|---:|---:|")
        for row in by_host:
            lines.append(
                f"| `{row.get('hostName', '-')}` "
                f"| {_fmt_int(row.get('screenPageViews'))} "
                f"| {_fmt_int(row.get('engagedSessions'))} "
                f"| {_fmt_dur(row.get('averageSessionDuration'))} "
                f"| {_fmt_rate(row.get('bounceRate'))} |"
            )
        lines.append("")
        lines.append("> 注: omcha.jp (WP 本体) と navi.omcha.jp (Hugo 知育玩具ナビ) は同一 GA4 計測 ID を共有。Phase 4 score 連携時は navi のみ filter する。")
        lines.append("")

    if by_device:
        lines.append("### デバイス別")
        lines.append("| device | PV | engagedSessions |")
        lines.append("|---|---:|---:|")
        for row in by_device:
            lines.append(
                f"| {row.get('deviceCategory', '-')} "
                f"| {_fmt_int(row.get('screenPageViews'))} "
                f"| {_fmt_int(row.get('engagedSessions'))} |"
            )
        lines.append("")

    if by_source:
        lines.append(f"### 流入元 Top {len(by_source)}")
        lines.append("| source / medium | PV | engagedSessions |")
        lines.append("|---|---:|---:|")
        for row in by_source:
            lines.append(
                f"| `{row.get('sessionSourceMedium', '-')}` "
                f"| {_fmt_int(row.get('screenPageViews'))} "
                f"| {_fmt_int(row.get('engagedSessions'))} |"
            )
        lines.append("")

    if by_page:
        lines.append(f"### 人気ページ Top {len(by_page)}")
        lines.append("| # | host | pagePath | PV | engaged | avg dur | bounce |")
        lines.append("|---:|---|---|---:|---:|---:|---:|")
        for i, row in enumerate(by_page, 1):
            host = row.get("hostName", "-")
            # host を短縮: navi.omcha.jp → navi, omcha.jp → wp で識別性アップ
            host_short = "navi" if host == "navi.omcha.jp" else ("wp" if host == "omcha.jp" else host)
            path = row.get("pagePath", "-")
            if len(path) > 50:
                path = path[:47] + "..."
            lines.append(
                f"| {i} | {host_short} | `{path}` "
                f"| {_fmt_int(row.get('screenPageViews'))} "
                f"| {_fmt_int(row.get('engagedSessions'))} "
                f"| {_fmt_dur(row.get('averageSessionDuration'))} "
                f"| {_fmt_rate(row.get('bounceRate'))} |"
            )
        lines.append("")

    if "feature_events" in data:
        feature = data.get("feature_events") or {}
        lines.append("## 特集ページ・診断（月ごと）")
        lines.append("")
        fr = feature.get("range") or {}
        lines.append(f"対象期間: {fr.get('start', '?')} 〜 {fr.get('end', '?')}")
        rows = feature.get("rows") or []
        summaries = summarize_feature_events(rows)
        if not summaries:
            lines.append("")
            lines.append("（データなし）")
        else:
            lines.append("")
            lines.append("| 月 | 診断 開始 | 完了 | 完了率 | 結果クリック | シェア |")
            lines.append("|---|---:|---:|---:|---:|---:|")
            for month in summaries:
                starts = month["diagnosis_start"]
                completes = month["diagnosis_complete"]
                rate = _fmt_rate(completes / starts) if starts else "—"
                lines.append(
                    f"| {_month_label(month['yearMonth'])} | {_fmt_int(starts)} | {_fmt_int(completes)} "
                    f"| {rate} | {_fmt_int(month['diagnosis_pick_click'])} "
                    f"| {_fmt_int(month['diagnosis_share'])} |"
                )
            lines.append("")
            lines.append("| 月 | " + " | ".join(FEATURE_PAGES.values()) + " | 合計 |")
            lines.append("|---|" + "---:|" * (len(FEATURE_PAGES) + 1))
            for month in summaries:
                affiliate = month["affiliate"]
                vals = [affiliate[path] for path in FEATURE_PAGES]
                lines.append(
                    f"| {_month_label(month['yearMonth'])} | " + " | ".join(_fmt_int(v) for v in vals)
                    + f" | {_fmt_int(month['affiliate_total'])} |"
                )
        lines.append("")
        lines.append("※ 件数が少ないため月ごとに見る。アフィリエイトクリックは GA4 の affiliate_click（Amazon・楽天・Yahoo のリンク）をページ別に数えたもの。今月は途中まで。")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("自動生成: `.github/workflows/17-analytics-report.yml`")
    lines.append("元データ: `data/analytics/ga4_weekly.json`")
    return "\n".join(lines)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", default="data/analytics/ga4_weekly.json")
    p.add_argument("--top-page", type=int, default=TOP_PAGE_DEFAULT)
    p.add_argument("--top-source", type=int, default=TOP_SOURCE_DEFAULT)
    args = p.parse_args()

    src = pathlib.Path(args.input)
    if not src.exists():
        sys.stderr.write(f"input not found: {src}\n")
        return 1
    data = json.loads(src.read_text(encoding="utf-8"))
    sys.stdout.write(render(data, args.top_page, args.top_source))
    return 0


if __name__ == "__main__":
    sys.exit(main())
