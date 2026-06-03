"""format_gsc_report.py

`data/analytics/gsc_weekly.json` を読み、Issue body 用の markdown を stdout に出す。

Issue: https://github.com/omochairo/amazon/issues/1316 (Phase 2)
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

TOP_QUERY_DEFAULT = 30
TOP_PAGE_DEFAULT = 20
TOP_OPP_DEFAULT = 20


def _fmt_int(v) -> str:
    try:
        return f"{int(v):,}"
    except (TypeError, ValueError):
        return str(v)


def _fmt_ctr(v) -> str:
    try:
        return f"{float(v) * 100:.1f}%"
    except (TypeError, ValueError):
        return "-"


def _fmt_pos(v) -> str:
    try:
        return f"{float(v):.1f}"
    except (TypeError, ValueError):
        return "-"


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 3] + "..."


def render(data: dict, top_query: int, top_page: int, top_opp: int) -> str:
    r = data.get("range", {})
    t = data.get("totals", {})
    by_query = data.get("by_query", [])[:top_query]
    by_page = data.get("by_page", [])[:top_page]
    by_device = data.get("by_device", [])
    opportunity = data.get("opportunity_pages", [])[:top_opp]

    lines: list[str] = []
    lines.append(f"## GSC 週次レポート ({r.get('start', '?')} 〜 {r.get('end', '?')})")
    lines.append("")
    lines.append(f"- 取得時刻: `{data.get('fetched_at', '')}`")
    lines.append(f"- Site: `{data.get('site_url', '')}`")
    lines.append(f"- 合計 clicks: **{_fmt_int(t.get('clicks_sum'))}**")
    lines.append(f"- 合計 impressions: **{_fmt_int(t.get('impressions_sum'))}**")
    lines.append(f"- GSC データ遅延: {r.get('delay_days', '?')} 日 (range は今日-{r.get('delay_days', '?')} 日まで)")
    lines.append("")

    if by_device:
        lines.append("### デバイス別")
        lines.append("| device | clicks | impressions | CTR | avg pos |")
        lines.append("|---|---:|---:|---:|---:|")
        for row in by_device:
            lines.append(
                f"| {row.get('device', '-')} "
                f"| {_fmt_int(row.get('clicks'))} "
                f"| {_fmt_int(row.get('impressions'))} "
                f"| {_fmt_ctr(row.get('ctr'))} "
                f"| {_fmt_pos(row.get('position'))} |"
            )
        lines.append("")

    if by_query:
        lines.append(f"### 検索クエリ Top {len(by_query)} (clicks 順)")
        lines.append("| # | query | clicks | impressions | CTR | avg pos |")
        lines.append("|---:|---|---:|---:|---:|---:|")
        for i, row in enumerate(by_query, 1):
            q = _truncate(row.get("query", "-"), 40)
            lines.append(
                f"| {i} | `{q}` "
                f"| {_fmt_int(row.get('clicks'))} "
                f"| {_fmt_int(row.get('impressions'))} "
                f"| {_fmt_ctr(row.get('ctr'))} "
                f"| {_fmt_pos(row.get('position'))} |"
            )
        lines.append("")

    if by_page:
        lines.append(f"### 検索流入ページ Top {len(by_page)}")
        lines.append("| # | page | clicks | impressions | CTR | avg pos |")
        lines.append("|---:|---|---:|---:|---:|---:|")
        for i, row in enumerate(by_page, 1):
            p = _truncate(row.get("page", "-"), 60)
            lines.append(
                f"| {i} | `{p}` "
                f"| {_fmt_int(row.get('clicks'))} "
                f"| {_fmt_int(row.get('impressions'))} "
                f"| {_fmt_ctr(row.get('ctr'))} "
                f"| {_fmt_pos(row.get('position'))} |"
            )
        lines.append("")

    if opportunity:
        lines.append(f"### 🎯 機会記事 Top {len(opportunity)} (2 ページ目 / position 11-20)")
        lines.append("> あと一押しで 1 ページ目に押し上がる可能性が高い記事。title / description / H2 強化対象。")
        lines.append("")
        lines.append("| # | page | impressions | CTR | avg pos |")
        lines.append("|---:|---|---:|---:|---:|")
        for i, row in enumerate(opportunity, 1):
            p = _truncate(row.get("page", "-"), 60)
            lines.append(
                f"| {i} | `{p}` "
                f"| {_fmt_int(row.get('impressions'))} "
                f"| {_fmt_ctr(row.get('ctr'))} "
                f"| {_fmt_pos(row.get('position'))} |"
            )
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("元データ: `data/analytics/gsc_weekly.json`")
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", default="data/analytics/gsc_weekly.json")
    p.add_argument("--top-query", type=int, default=TOP_QUERY_DEFAULT)
    p.add_argument("--top-page", type=int, default=TOP_PAGE_DEFAULT)
    p.add_argument("--top-opp", type=int, default=TOP_OPP_DEFAULT)
    args = p.parse_args()

    src = pathlib.Path(args.input)
    if not src.exists():
        sys.stderr.write(f"input not found: {src}\n")
        return 1
    data = json.loads(src.read_text(encoding="utf-8"))
    sys.stdout.write(render(data, args.top_query, args.top_page, args.top_opp))
    return 0


if __name__ == "__main__":
    sys.exit(main())
