"""format_ga4_report.py

`data/analytics/ga4_weekly.json` を読み、週次 Issue 本文用の markdown を stdout に出す。
17-analytics-report.yml から呼ばれ、`gh issue create` の body にそのまま流し込む。

Issue: https://github.com/omochairo/amazon/issues/1316 (Phase 1)
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

TOP_PAGE_DEFAULT = 20
TOP_SOURCE_DEFAULT = 10


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
        lines.append("| # | pagePath | PV | engaged | avg dur | bounce |")
        lines.append("|---:|---|---:|---:|---:|---:|")
        for i, row in enumerate(by_page, 1):
            path = row.get("pagePath", "-")
            if len(path) > 60:
                path = path[:57] + "..."
            lines.append(
                f"| {i} | `{path}` "
                f"| {_fmt_int(row.get('screenPageViews'))} "
                f"| {_fmt_int(row.get('engagedSessions'))} "
                f"| {_fmt_dur(row.get('averageSessionDuration'))} "
                f"| {_fmt_rate(row.get('bounceRate'))} |"
            )
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("自動生成: `.github/workflows/17-analytics-report.yml`")
    lines.append("元データ: `data/analytics/ga4_weekly.json`")
    return "\n".join(lines)


def main() -> int:
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
