"""report_census_404.py

Refs #3331, #3988 — GSC index census の「404 認識 URL」棚卸しレポート。

背景 (#3331): navi.omcha.jp の sitemap 上の一部 URL が Google に
「見つかりませんでした（404）」と認識されているが、実機は 200 で生きている。
打ち手は 3 案あった:
  - 案1: sitemap の lastmod 更新 → 2026-07-17 に no-op と判明
    (lastmod は既に出力済み・Google の lastCrawlTime より新しいのに再クロールされない)
  - 案2: GSC で手動インデックス登録リクエスト → 2026-07-17 に実施済み
  - 案3: 内部リンクで孤立を解消して再クロール優先度を上げる → 未着手

本スクリプトは案3 の要否と対象を判断するための **read-only** の棚卸しレポートを
出す。data/ 配下は一切書き換えない。既存の cron / workflow には組み込まない
(owner が手で叩く棚卸しツール)。

出力 (stdout に人間可読、``--json <path>`` 指定時は機械可読 JSON も書く):
  1. 404 認識件数の推移 (data/analytics/history/gsc_index_census.jsonl の
     not_found_404 列を date 昇順で。circuit_breaker_tripped == true の行は
     partial run だが除外せずフラグ付きで表示する — #3372 の偽改善検出回避)
  2. 現行 census (data/analytics/gsc_index_census.json) の 404 認識 URL 全件
     (URL / last_crawl_time / google_canonical) + last_crawl_time の年月別分布
  3. 各 URL への内部被リンク数 (少ない順)。scripts/generate_internal_links.py が
     書く sidecar (``data/articles/<stem>.seo.json`` の
     ``internal_link_suggestions``) を数える — build_post.py の
     ``_inject_internal_links`` が本文へ注入する実リンクの供給源であり、
     これがそのまま「現在張られている(張られる予定の)内部被リンク数」に対応する。
     リンク生成ロジック自体は再実装せず、scripts/audit_query_entailment.py の
     discover_articles / extract_asin_from_page と
     scripts/_seo_sidecar.py の load_sidecar / sidecar_path を import して使う
     (scripts/generate_internal_links.py も同じヘルパを使っている)。
     算出不能な URL (ASIN が URL から抽出できない等) は 0 と断定せず
     "unknown" として区別する。

coverage_state の扱い:
  census の coverage_state はロケール依存の日本語表示文字列。
  scripts/append_census_history.py が既に持つ既知 raw 文字列 -> ASCII slug の
  マッピングテーブル (COVERAGE_STATE_SLUGS) を import して再利用する
  (新たに日本語文字列をハードコードしない)。未知の文字列が来たら黙って捨てず
  "unknown_coverage_state" として出力に残す。

Issue: https://github.com/omochairo/amazon/issues/3331, #3988
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from collections import Counter
from typing import Any

from scripts._seo_sidecar import load_sidecar, sidecar_path
from scripts.append_census_history import (
    COVERAGE_STATE_SLUGS,
    CENSUS_HISTORY_FILE,
    DEFAULT_ARTICLES_DIR,
    DEFAULT_CENSUS,
    _slug_for_raw_state,
)
from scripts.append_analytics_history import DEFAULT_HISTORY_DIR
from scripts.audit_query_entailment import discover_articles, extract_asin_from_page

NOT_FOUND_404_SLUG = "not_found_404"


# --------------------------------------------------------------------------
# 1. 404 認識件数の推移 (history jsonl)
# --------------------------------------------------------------------------

def load_history_rows(history_path: pathlib.Path) -> list[dict[str, Any]]:
    """gsc_index_census.jsonl を読み、date 昇順のリストを返す。

    壊れた行は無視して寛容に扱う (append_census_history.existing_dates と同じ
    流儀)。ファイルが無い/空でも空リストを返し、呼び出し側を落とさない。
    """
    if not history_path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        text = history_path.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("date"):
            rows.append(obj)
    rows.sort(key=lambda r: r.get("date", ""))
    return rows


def summarize_404_trend(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """history 行から 404 認識件数の推移サマリを作る (履歴 1 行でも壊れない)。"""
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append({
            "date": row.get("date"),
            "not_found_404": row.get(NOT_FOUND_404_SLUG, 0),
            "circuit_breaker_tripped": bool(row.get("circuit_breaker_tripped", False)),
        })
    return out


# --------------------------------------------------------------------------
# 2. 現行 census の 404 URL 全件 + last_crawl_time 年月分布
# --------------------------------------------------------------------------

def extract_404_urls(census: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """census の not_indexed_urls から coverage_state == 404 の行だけを抜き出す。

    戻り値は (404 URL のリスト, unknown coverage_state raw文字列 -> 件数)。
    unknown は「404 判定できたはずが未知の文字列だったため黙って捨てた」件数を
    可視化するためのもの (通常は空)。
    """
    not_indexed_urls = census.get("not_indexed_urls")
    not_indexed_urls = not_indexed_urls if isinstance(not_indexed_urls, list) else []

    out: list[dict[str, Any]] = []
    unknown_states: Counter[str] = Counter()
    for row in not_indexed_urls:
        if not isinstance(row, dict):
            continue
        raw_state = row.get("coverage_state") or ""
        slug = _slug_for_raw_state(raw_state)
        if slug == "other" and raw_state.strip() not in COVERAGE_STATE_SLUGS:
            # 404 かどうか判定できない未知の状態。他 slug の可能性もあるので
            # 404 集合には入れず、可視化のためだけに記録する。
            unknown_states[raw_state.strip()] += 1
            continue
        if slug != NOT_FOUND_404_SLUG:
            continue
        out.append({
            "url": row.get("url"),
            "last_crawl_time": row.get("last_crawl_time"),
            "google_canonical": row.get("google_canonical"),
        })
    return out, dict(unknown_states)


def _year_month_bucket(last_crawl_time: Any) -> str:
    """last_crawl_time (ISO8601 or '(none)') から 'YYYY-MM' バケットを作る。

    未クロール ('(none)' や空文字) は 'never_crawled' に分類する。パース不能な
    値は 'unparsable' に分類する (無言で捨てない)。
    """
    if not isinstance(last_crawl_time, str) or not last_crawl_time.strip():
        return "never_crawled"
    value = last_crawl_time.strip()
    if value == "(none)":
        return "never_crawled"
    # ISO8601 の先頭 7 文字が YYYY-MM (inspect_gsc_index.py / GSC API の
    # lastCrawlTime は常に UTC の 'YYYY-MM-DDTHH:MM:SSZ' 形式)。
    if len(value) >= 7 and value[4] == "-":
        bucket = value[:7]
        try:
            year, month = bucket.split("-")
            int(year)
            m = int(month)
        except ValueError:
            return "unparsable"
        if 1 <= m <= 12:
            return bucket
    return "unparsable"


def summarize_last_crawl_distribution(urls: list[dict[str, Any]]) -> dict[str, int]:
    """404 URL の last_crawl_time を年月バケットで集計する (件数降順 → key 昇順)。"""
    counts: Counter[str] = Counter()
    for row in urls:
        counts[_year_month_bucket(row.get("last_crawl_time"))] += 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


# --------------------------------------------------------------------------
# 3. 内部被リンク数 (少ない順)
# --------------------------------------------------------------------------

def count_inbound_links(articles_dir: pathlib.Path) -> tuple[dict[str, int] | None, str | None]:
    """全記事の sidecar (internal_link_suggestions) から target_asin ごとの
    被リンク数を数える。

    scripts/generate_internal_links.py が書く sidecar キー
    ``internal_link_suggestions`` (各要素 {"target_asin": ..., ...}) を数える。
    生成ロジック自体は再実装せず、discover_articles (記事一覧) +
    load_sidecar/sidecar_path (sidecar 読み込み) という既存ヘルパのみを使う。

    戻り値は (asin -> count, error_reason)。articles_dir が読めない場合は
    (None, 理由) を返し、呼び出し側は 0 に潰さず "unknown" 扱いする。
    """
    article_index = discover_articles(articles_dir)
    if not article_index:
        return None, f"記事ディレクトリ {articles_dir} が読めないか記事が 0 件だったため、被リンク数を算出できません"

    counts: dict[str, int] = {asin: 0 for asin in article_index}
    for stem_path in article_index.values():
        sc_path = sidecar_path(articles_dir, stem_path.stem)
        sidecar = load_sidecar(sc_path)
        suggestions = sidecar.get("internal_link_suggestions")
        if not isinstance(suggestions, list):
            continue
        for sugg in suggestions:
            if not isinstance(sugg, dict):
                continue
            target_asin = sugg.get("target_asin")
            if not isinstance(target_asin, str) or not target_asin.strip():
                continue
            asin = target_asin.strip().upper()
            counts[asin] = counts.get(asin, 0) + 1
    return counts, None


def build_inbound_report(
    urls: list[dict[str, Any]], inbound_counts: dict[str, int] | None,
) -> list[dict[str, Any]]:
    """404 URL ごとに被リンク数を突き合わせ、少ない順にソートする。

    ASIN が URL から抽出できない、または inbound_counts が None (算出不能) の
    場合は "unknown" とし 0 断定を避ける。ソートは (unknown を最後、それ以外は
    昇順) → url 昇順。
    """
    out: list[dict[str, Any]] = []
    for row in urls:
        asin = extract_asin_from_page(row.get("url") or "")
        if asin is None or inbound_counts is None:
            inbound = "unknown"
        else:
            inbound = inbound_counts.get(asin, 0)
        out.append({
            "url": row.get("url"),
            "asin": asin,
            "inbound_links": inbound,
        })

    def sort_key(r: dict[str, Any]) -> tuple[int, int, str]:
        v = r["inbound_links"]
        if v == "unknown":
            return (1, 0, r["url"] or "")
        return (0, v, r["url"] or "")

    out.sort(key=sort_key)
    return out


# --------------------------------------------------------------------------
# レンダリング (人間可読)
# --------------------------------------------------------------------------

def render_report(
    trend: list[dict[str, Any]],
    urls: list[dict[str, Any]],
    unknown_states: dict[str, int],
    crawl_distribution: dict[str, int],
    inbound_report: list[dict[str, Any]],
    inbound_error: str | None,
) -> str:
    lines: list[str] = []
    lines.append("# GSC index census — 404 認識 URL 棚卸しレポート")
    lines.append("Refs #3331, #3988")
    lines.append("")

    lines.append("## 1. 404 認識件数の推移")
    if not trend:
        lines.append("(history が空 — data/analytics/history/gsc_index_census.jsonl に行がありません)")
    else:
        lines.append("| date | not_found_404 | circuit_breaker_tripped |")
        lines.append("|---|---:|---|")
        for row in trend:
            flag = "⚠️ partial run" if row["circuit_breaker_tripped"] else ""
            lines.append(f"| {row['date']} | {row['not_found_404']} | {flag} |")
    lines.append("")

    lines.append("## 2. 現行 census の 404 認識 URL")
    lines.append(f"件数: {len(urls)}")
    if unknown_states:
        lines.append("")
        lines.append(f"⚠️ 未知の coverage_state を {sum(unknown_states.values())} 件検出 "
                      "(404 集合には含めていません。COVERAGE_STATE_SLUGS への追記が必要な可能性):")
        for raw, cnt in sorted(unknown_states.items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"  - {raw!r}: {cnt}")
    lines.append("")
    lines.append("### last_crawl_time 年月別分布")
    if not crawl_distribution:
        lines.append("(404 URL が 0 件のため分布なし)")
    else:
        lines.append("| bucket | count |")
        lines.append("|---|---:|")
        for bucket, cnt in crawl_distribution.items():
            lines.append(f"| {bucket} | {cnt} |")
    lines.append("")
    lines.append("### URL 一覧 (先頭200件まで表示、全件は --json 参照)")
    lines.append("| url | last_crawl_time | google_canonical |")
    lines.append("|---|---|---|")
    for row in urls[:200]:
        lines.append(f"| {row.get('url')} | {row.get('last_crawl_time')} | {row.get('google_canonical')} |")
    if len(urls) > 200:
        lines.append(f"(...他 {len(urls) - 200} 件、--json で全件出力)")
    lines.append("")

    lines.append("## 3. 内部被リンク数 (少ない順)")
    if inbound_error:
        lines.append(f"⚠️ 被リンク数は算出できませんでした: {inbound_error}")
    elif not inbound_report:
        lines.append("(404 URL が 0 件のため対象なし)")
    else:
        unknown_count = sum(1 for r in inbound_report if r["inbound_links"] == "unknown")
        if unknown_count:
            lines.append(f"⚠️ {unknown_count} 件は ASIN 抽出不能のため unknown (0 扱いにしていません)")
        lines.append("")
        lines.append("| url | asin | inbound_links |")
        lines.append("|---|---|---:|")
        for row in inbound_report[:200]:
            lines.append(f"| {row['url']} | {row['asin'] or '(unknown)'} | {row['inbound_links']} |")
        if len(inbound_report) > 200:
            lines.append(f"(...他 {len(inbound_report) - 200} 件、--json で全件出力)")

    return "\n".join(lines)


# --------------------------------------------------------------------------
# 実行本体
# --------------------------------------------------------------------------

def run(
    census_path: pathlib.Path,
    history_dir: pathlib.Path,
    articles_dir: pathlib.Path,
) -> dict[str, Any]:
    history_rows = load_history_rows(history_dir / CENSUS_HISTORY_FILE)
    trend = summarize_404_trend(history_rows)

    if census_path.exists():
        census = json.loads(census_path.read_text(encoding="utf-8"))
    else:
        census = {}
    urls, unknown_states = extract_404_urls(census)
    crawl_distribution = summarize_last_crawl_distribution(urls)

    inbound_counts, inbound_error = count_inbound_links(articles_dir)
    inbound_report = build_inbound_report(urls, inbound_counts)

    result: dict[str, Any] = {
        "trend": trend,
        "count_404": len(urls),
        "urls_404": urls,
        "unknown_coverage_states": unknown_states,
        "last_crawl_time_distribution": crawl_distribution,
        "inbound_links": inbound_report,
        "inbound_links_error": inbound_error,
    }
    return result


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--census", default=DEFAULT_CENSUS,
                   help="GSC index census JSON のパス (data/analytics/gsc_index_census.json)")
    p.add_argument("--history-dir", default=DEFAULT_HISTORY_DIR,
                   help="週次 history ディレクトリ (data/analytics/history)")
    p.add_argument("--articles-dir", default=DEFAULT_ARTICLES_DIR,
                   help="記事ディレクトリ (data/articles) — 被リンク数算出用")
    p.add_argument("--json", default=None, help="機械可読 JSON の出力先パス (任意)")
    args = p.parse_args()

    result = run(
        pathlib.Path(args.census),
        pathlib.Path(args.history_dir),
        pathlib.Path(args.articles_dir),
    )

    print(render_report(
        result["trend"],
        result["urls_404"],
        result["unknown_coverage_states"],
        result["last_crawl_time_distribution"],
        result["inbound_links"],
        result["inbound_links_error"],
    ))

    if args.json:
        out_path = pathlib.Path(args.json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    return 0


if __name__ == "__main__":
    sys.exit(main())
