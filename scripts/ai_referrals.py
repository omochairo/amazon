"""ai_referrals.py

AI のチャット・検索 (ChatGPT・Perplexity・Gemini など) から navi へ来た訪問を、
GA4 から取り出して週次の表 (Markdown) にする。read-only。

  python scripts/ai_referrals.py fetch  --out rows.json     # GA4 から取る (要 GA4_SA_JSON / GA4_PROPERTY_ID)
  python scripts/ai_referrals.py format --rows rows.json --out report.md   # 表にする (API は呼ばない)

workflow 65 が週 1 回回し、表は private の navi-brain の issue にだけ出す。
このリポジトリは public なので、数字をコミット・ログ・job summary に出さない
(先例: workflow 23 と scripts/fetch_ga4_affiliate_clicks.py)。

取るもの (どれも hostName == navi.omcha.jp の完全一致で絞る):
  sources : date × sessionSource で sessions・engagedSessions (全部の参照元。分母にも使う)
  clicks  : date × sessionSource で affiliate_click の eventCount
  landing : landingPage × sessionSource で sessions (AI の参照元だけ。直近 --landing-days 日)

AI かどうかは sessionSource (参照元のドメイン、または utm_source) を AI_SERVICES と
照らして決める。リストに無い AI は「AI らしいが未分類の参照元」として表の最後に出す
(リストを足す手がかり)。

期間: 終わりは今日 (日本時間) の 2 日前 (当日・前日は流入元の処理が終わっておらず
sessionSource が `(data not available)` になる)。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import re
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import fetch_ga4_affiliate_clicks as ga  # noqa: E402  (認証・期間・フィルタの作り方を共有する)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ai_referrals")

DEFAULT_DAYS = 56
LANDING_DAYS = 28
CLICK_EVENT = "affiliate_click"

# (表に出す名前, sessionSource のドメイン (この名前か、そのサブドメインに当てる) または utm_source の値)
# 参照元が付くのは、AI の回答の中のリンクを押して来た訪問だけ。AI のアプリの中のブラウザや
# コピーした URL から来た訪問は direct になり、ここには出ない (少なめに出る)。
AI_SERVICES: tuple[tuple[str, tuple[str, ...]], ...] = (
    # "openai": utm_source=openai で来る ChatGPT の検索のリンク (2026-11-26 の初回の表で未分類に出た)
    ("ChatGPT", ("chatgpt.com", "chat.openai.com", "openai.com", "chatgpt", "openai")),
    ("Perplexity", ("perplexity.ai", "perplexity")),
    ("Gemini", ("gemini.google.com", "bard.google.com", "gemini")),
    # copilot.com: Microsoft Copilot の参照元 (2026-11-26 の初回の表で未分類に出た)
    ("Copilot", ("copilot.microsoft.com", "copilot.com", "copilot.cloud.microsoft", "edgeservices.bing.com",
                 "copilot")),
    ("Claude", ("claude.ai", "claude")),
    ("Grok", ("grok.com", "x.ai", "grok")),
    ("DeepSeek", ("chat.deepseek.com", "deepseek.com", "deepseek")),
    ("Meta AI", ("meta.ai",)),
    ("その他の AI", ("you.com", "phind.com", "poe.com", "felo.ai", "genspark.ai", "chat.mistral.ai",
                     "kimi.moonshot.cn", "kimi.com", "chat.qwen.ai", "duck.ai")),
)
SERVICE_NAMES = tuple(name for name, _ in AI_SERVICES)
# 未分類のうち「AI らしい」参照元を拾うための語 (リストを足す手がかりにだけ使う)
AI_HINT_RE = re.compile(r"(gpt|openai|llm|chat|copilot|gemini|claude|perplexity|\bai\b|\.ai$)")


# ---------------------------------------------------------------- 見分け

def _normalize(source: str) -> str:
    s = (source or "").strip().lower()
    return s[4:] if s.startswith("www.") else s


def classify(source: str) -> str | None:
    """sessionSource → AI サービスの名前。AI でなければ None。"""
    s = _normalize(source)
    if not s:
        return None
    for name, domains in AI_SERVICES:
        for d in domains:
            if s == d or s.endswith("." + d):
                return name
    return None


def looks_like_ai(source: str) -> bool:
    s = _normalize(source)
    return classify(s) is None and bool(AI_HINT_RE.search(s))


def ga4_source_regex() -> str:
    """landing の取得で sessionSource を絞る正規表現 (GA4 は RE2・FULL_REGEXP・大文字小文字を区別しない)。"""
    names = sorted({re.escape(d) for _, domains in AI_SERVICES for d in domains}, key=len, reverse=True)
    return r"(www\.)?((.+\.)?(" + "|".join(names) + "))"


# ---------------------------------------------------------------- 取得

def _regex_filter(field: str, pattern: str):
    from google.analytics.data_v1beta.types import Filter, FilterExpression
    return FilterExpression(filter=Filter(
        field_name=field,
        string_filter=Filter.StringFilter(
            value=pattern, match_type=Filter.StringFilter.MatchType.FULL_REGEXP, case_sensitive=False,
        ),
    ))


def _run(client, property_id: str, start: date, end: date, dims: list[str], metrics: list[str],
         dimension_filter) -> tuple[list[dict], list[str]]:
    """全ディメンションで並べて、limit を超える分は offset で取り切る。(行, 警告)。"""
    from google.analytics.data_v1beta.types import (
        DateRange, Dimension, Metric, OrderBy, RunReportRequest,
    )
    rows: list[dict] = []
    warns: list[str] = []
    offset = 0
    while True:
        req = RunReportRequest(
            property=f"properties/{property_id}",
            dimensions=[Dimension(name=d) for d in dims],
            metrics=[Metric(name=m) for m in metrics],
            date_ranges=[DateRange(start_date=start.isoformat(), end_date=end.isoformat())],
            dimension_filter=dimension_filter,
            order_bys=[OrderBy(dimension=OrderBy.DimensionOrderBy(dimension_name=d)) for d in dims],
            offset=offset,
            limit=ga.PAGE_SIZE,
        )
        res = client.run_report(req)
        page = []
        for r in res.rows:
            row: dict[str, Any] = {}
            for i, d in enumerate(dims):
                v = r.dimension_values[i].value
                row[d] = ga.parse_ga4_date(v) if d == "date" else v
            for i, m in enumerate(metrics):
                row[m] = ga._metric_int(r.metric_values[i].value)
            page.append(row)
        rows.extend(page)
        for w in ga.metadata_warnings(res):
            if w not in warns:
                warns.append(w)
        offset += len(page)
        if not page or offset >= int(getattr(res, "row_count", 0) or 0):
            break
    return rows, warns


def fetch(client, property_id: str, start: date, end: date, landing_days: int) -> dict[str, Any]:
    if landing_days < 1:
        raise ValueError("landing_days は 1 以上")
    host = ga._string_filter("hostName", ga.NAVI_HOST)
    sources, w1 = _run(client, property_id, start, end, ["date", "sessionSource"],
                       ["sessions", "engagedSessions"], host)
    clicks, w2 = _run(client, property_id, start, end, ["date", "sessionSource"], ["eventCount"],
                      ga._and(host, ga._string_filter("eventName", CLICK_EVENT)))
    l_start = max(start, end - timedelta(days=landing_days - 1))
    landing, w3 = _run(client, property_id, l_start, end, ["landingPage", "sessionSource"], ["sessions"],
                       ga._and(host, _regex_filter("sessionSource", ga4_source_regex())))
    warnings = list(dict.fromkeys(
        [f"参照元の取得: {w}" for w in w1] + [f"クリックの取得: {w}" for w in w2]
        + [f"着地ページの取得: {w}" for w in w3]))
    return {
        "meta": {"fetched_at": datetime.now(timezone.utc).isoformat(), "host": ga.NAVI_HOST,
                 "range": {"start": start.isoformat(), "end": end.isoformat()},
                 "landing_range": {"start": l_start.isoformat(), "end": end.isoformat()},
                 "warnings": warnings},
        "sources": sources, "clicks": clicks, "landing": landing,
    }


# ---------------------------------------------------------------- 表

def _weeks(start: date, end: date) -> list[tuple[date, date]]:
    """期間の終わりから 7 日ずつ区切る (7 日に満たない端の週は出さない)。古い順。"""
    out = []
    e = end
    while e - timedelta(days=6) >= start:
        out.append((e - timedelta(days=6), e))
        e -= timedelta(days=7)
    return list(reversed(out))


def _in(d: str, span: tuple[date, date]) -> bool:
    try:
        x = date.fromisoformat(d)
    except ValueError:
        return False
    return span[0] <= x <= span[1]


def _cell(text: str) -> str:
    """表のセルに入れる文字列。| と ` と改行で表が崩れないようにする。"""
    return (str(text).replace("\\", "\\\\").replace("|", "\\|").replace("`", "'")
            .replace("\r", " ").replace("\n", " "))


def _pct(n: int, d: int) -> str:
    return f"{n / d * 100:.1f}%" if d else "—"


def build_report(data: dict[str, Any], *, landing_top: int = 20) -> str:
    meta = data.get("meta") or {}
    rng = meta.get("range") or {}
    start, end = date.fromisoformat(rng["start"]), date.fromisoformat(rng["end"])
    sources, clicks, landing = data.get("sources", []), data.get("clicks", []), data.get("landing", [])
    weeks = _weeks(start, end)
    lines = [f"## AI 経由の訪問（{start}〜{end}・navi.omcha.jp）", ""]
    lines += ["AI の回答の中のリンクを押して来たセッションだけを数えています（参照元が付かない訪問は"
              "「direct」になり、ここには入りません）。セッションは navi のページを開いたセッション"
              "（GA4 の hostName = navi.omcha.jp）の数です。クリックは `affiliate_click` の数です。", ""]

    # (1) 週ごと
    lines += ["### 週ごとの推移", ""]
    head = ["週"] + list(SERVICE_NAMES) + ["AI 計", "全セッション", "AI の割合", "AI からのクリック"]
    lines += ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for span in weeks:
        per = defaultdict(int)
        total = 0
        for r in sources:
            if not _in(r.get("date", ""), span):
                continue
            total += int(r.get("sessions", 0))
            name = classify(r.get("sessionSource", ""))
            if name:
                per[name] += int(r.get("sessions", 0))
        ai = sum(per.values())
        ai_clicks = sum(int(r.get("eventCount", 0)) for r in clicks
                        if _in(r.get("date", ""), span) and classify(r.get("sessionSource", "")))
        cells = [f"{span[0]:%m/%d}〜{span[1]:%m/%d}"] + [str(per[n]) for n in SERVICE_NAMES]
        cells += [str(ai), str(total), _pct(ai, total), str(ai_clicks)]
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")

    # (2) 直近 4 週のサービス別 (最後の 4 週。週が 4 つ無ければあるだけ)
    recent = weeks[-4:]
    if recent:
        span = (recent[0][0], recent[-1][1])
        lines += [f"### サービス別（{span[0]}〜{span[1]}）", ""]
        lines += ["| サービス | セッション | エンゲージ | エンゲージ率 | クリック | クリック / セッション |",
                  "|---|---|---|---|---|---|"]
        agg = defaultdict(lambda: [0, 0, 0])
        for r in sources:
            name = classify(r.get("sessionSource", ""))
            if name and _in(r.get("date", ""), span):
                agg[name][0] += int(r.get("sessions", 0))
                agg[name][1] += int(r.get("engagedSessions", 0))
        for r in clicks:
            name = classify(r.get("sessionSource", ""))
            if name and _in(r.get("date", ""), span):
                agg[name][2] += int(r.get("eventCount", 0))
        rows = [(n, *agg[n]) for n in SERVICE_NAMES if any(agg[n])]
        if rows:
            for n, s, e, c in rows:
                lines.append(f"| {n} | {s} | {e} | {_pct(e, s)} | {c} | {_pct(c, s)} |")
        else:
            lines.append("| （AI からのセッションなし） | 0 | 0 | — | 0 | — |")
        lines.append("")

    # (3) 着地ページ
    lr = meta.get("landing_range") or {}
    lines += [f"### AI から来た着地ページ（{lr.get('start', '?')}〜{lr.get('end', '?')}・上位 {landing_top}）", ""]
    page_agg: dict[tuple[str, str], int] = defaultdict(int)
    for r in landing:
        name = classify(r.get("sessionSource", ""))
        if name:
            page_agg[(r.get("landingPage", ""), name)] += int(r.get("sessions", 0))
    top = sorted(page_agg.items(), key=lambda kv: (-kv[1], kv[0]))[:landing_top]
    if top:
        lines += ["| 着地ページ | サービス | セッション |", "|---|---|---|"]
        lines += [f"| `{_cell(p)}` | {n} | {s} |" for (p, n), s in top]
    else:
        lines.append("（AI から来たセッションはありません）")
    lines.append("")

    # (4) 未分類で AI らしい参照元
    hints: dict[str, int] = defaultdict(int)
    for r in sources:
        if looks_like_ai(r.get("sessionSource", "")):
            hints[_normalize(r["sessionSource"])] += int(r.get("sessions", 0))
    if hints:
        lines += [f"### AI らしいが未分類の参照元（{start}〜{end}・scripts/ai_referrals.py の AI_SERVICES に"
                  "足すか判断する）", ""]
        lines += ["| 参照元 | セッション |", "|---|---|"]
        lines += [f"| `{_cell(s)}` | {n} |" for s, n in sorted(hints.items(), key=lambda kv: (-kv[1], kv[0]))[:20]]
        lines.append("")

    for w in meta.get("warnings") or []:
        lines.append(f"> ⚠️ {w}")
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------- CLI

def _cmd_fetch(args) -> int:
    if not args.property_id:
        logger.error("GA4_PROPERTY_ID が未設定")
        return 2
    sa_json = args.sa_json or (pathlib.Path(args.sa_json_file).read_text(encoding="utf-8")
                               if args.sa_json_file else None)
    if not sa_json:
        logger.error("GA4_SA_JSON も --sa-json-file も未指定")
        return 2
    if args.landing_days < 1:
        logger.error("--landing-days は 1 以上")
        return 2
    try:
        start, end = ga.resolve_window(start=None, end=args.end, days=args.days, today=ga.today_jst())
    except ValueError as e:
        logger.error("%s", e)
        return 2
    try:
        from google.analytics.data_v1beta import BetaAnalyticsDataClient
        client = BetaAnalyticsDataClient(credentials=ga._load_credentials(sa_json))
        result = fetch(client, args.property_id, start, end, args.landing_days)
    except Exception as e:  # noqa: BLE001
        # API のエラー文はリクエストの誤り (ディメンション名など) で、数字は含まない
        logger.error("GA4 の取得に失敗: %s: %s", type(e).__name__, e)
        return 1
    ga.write_json(pathlib.Path(args.out), result)
    for w in result["meta"]["warnings"]:
        logger.warning("%s", w)
        if os.environ.get("GITHUB_ACTIONS") == "true":
            print(f"::warning::{w}")
    # 件数は出さない (public の Actions のログに残るため)
    logger.info("range: %s .. %s / wrote %s", start, end, args.out)
    return 0


def _cmd_format(args) -> int:
    if args.top < 1:
        logger.error("--top は 1 以上")
        return 2
    data = json.loads(pathlib.Path(args.rows).read_text(encoding="utf-8"))
    report = build_report(data, landing_top=args.top)
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8", newline="\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[2],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="GA4 から取る")
    f.add_argument("--property-id", default=os.environ.get("GA4_PROPERTY_ID"))
    f.add_argument("--sa-json", default=os.environ.get("GA4_SA_JSON"))
    f.add_argument("--sa-json-file")
    f.add_argument("--end", help="期間の終わり (YYYY-MM-DD・日本時間。既定は今日の 2 日前)")
    f.add_argument("--days", type=int, default=DEFAULT_DAYS)
    f.add_argument("--landing-days", type=int, default=LANDING_DAYS)
    f.add_argument("--out", required=True)
    f.set_defaults(func=_cmd_fetch)
    m = sub.add_parser("format", help="表にする (API は呼ばない)")
    m.add_argument("--rows", required=True)
    m.add_argument("--out", required=True)
    m.add_argument("--top", type=int, default=20)
    m.set_defaults(func=_cmd_format)
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
