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
  3. 各 URL への sidecar 内部リンク候補の被参照数 (少ない順)。
     scripts/generate_internal_links.py が書く sidecar
     (``data/articles/<stem>.seo.json`` の ``internal_link_suggestions``) を
     数える。リンク生成ロジック自体は再実装せず、
     scripts/audit_query_entailment.py の discover_articles /
     extract_asin_from_page と scripts/_seo_sidecar.py の
     load_sidecar / sidecar_path を import して使う
     (scripts/generate_internal_links.py も同じヘルパを使っている)。

     **注意 (重要・誤読しやすい点)**: これは build_post.py が実際にページへ
     描画した内部リンクグラフではない。26-faq-seo-lane (generate_internal_links.py)
     が出した「リンク候補 (suggestions)」の被参照数に過ぎない。かつ実測の
     結果、この sidecar 供給源はコーパスの一部しかカバーしていない
     (2026-08 時点で all articles 中 internal_link_suggestions を持つのは
     3.6% 程度)。カバレッジが低い状態では個別 ASIN の「0」は「被リンクが
     無い」ではなく「未測定」であり、閾値未満のときは 0 を "unknown" として
     出す (INBOUND_SUGGESTION_COVERAGE_LOW_THRESHOLD 参照)。
     算出不能な URL (ASIN が URL から抽出できない等) も "unknown" とする。
     navi の実際の内部リンクグラフ (rendered graph) はリポジトリ内に
     永続化されていない (amazon-home-ops の site-audit が r5_orphans 算出の
     ためにクロール時にグラフを作っているが保存していない)。案3 の対象選定
     には link graph の永続化が別途必要 — 本レポートの被参照数はその代替には
     ならない。

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

# 供給源カバレッジの下限閾値 (§3 コメント参照)。実測 (2026-08-02):
# data/articles/*.json 全 2,359 件のうち sidecar 保有 564 件、
# internal_link_suggestions を実際に持つのは 84 件 (≒3.6%)。distinct
# target_asin は 76 件のみ。この状態でカバレッジ比が低いまま個別 ASIN の
# カウントが 0 でも「被リンクが無い」と断定できない (単に生成 lane がまだ
# 対象記事を処理していないだけの可能性が高い)。10% を閾値とする根拠は
# 「サイト全体の 1 割未満しか候補生成が回っていない状態では、0 の大半が
# 未処理由来になる」という判断 (実測 3.6% はこれを大きく下回る)。値は
# 恣意的だが、コーパスの大半をカバーしない限り 0 を信頼しないという
# 保守的な側に倒す意図のみ確定させている。
INBOUND_SUGGESTION_COVERAGE_LOW_THRESHOLD = 0.10


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
# 3. sidecar 内部リンク候補の被参照数 (少ない順)
# --------------------------------------------------------------------------

def count_inbound_suggestions(
    articles_dir: pathlib.Path,
) -> tuple[dict[str, int] | None, dict[str, Any], str | None]:
    """全記事の sidecar (internal_link_suggestions) から target_asin ごとの
    被参照数を数え、供給源カバレッジ統計も併せて返す。

    scripts/generate_internal_links.py が書く sidecar キー
    ``internal_link_suggestions`` (各要素 {"target_asin": ..., ...}) を数える。
    生成ロジック自体は再実装せず、discover_articles (記事一覧) +
    load_sidecar/sidecar_path (sidecar 読み込み) という既存ヘルパのみを使う。

    これは「実際にページへ描画された内部リンクグラフ」ではなく、あくまで
    lane が出した候補の被参照数である (モジュール docstring 参照)。

    戻り値は (asin -> count, coverage_stats, error_reason)。
    articles_dir が読めない場合は (None, {}, 理由) を返し、呼び出し側は
    0 に潰さず "unknown" 扱いする。

    coverage_stats:
      - total_articles: 記事総数
      - sidecars_with_suggestions: internal_link_suggestions を実際に持つ記事数
      - suggestion_total: suggestion の総数 (延べ)
      - distinct_target_asins: target_asin の distinct 件数
      - coverage_ratio: sidecars_with_suggestions / total_articles
      - low_coverage: coverage_ratio < INBOUND_SUGGESTION_COVERAGE_LOW_THRESHOLD
    """
    article_index = discover_articles(articles_dir)
    if not article_index:
        return (
            None, {},
            f"記事ディレクトリ {articles_dir} が読めないか記事が 0 件だったため、"
            "被参照数を算出できません",
        )

    counts: dict[str, int] = {asin: 0 for asin in article_index}
    sidecars_with_suggestions = 0
    suggestion_total = 0
    distinct_targets: set[str] = set()

    for stem_path in article_index.values():
        sc_path = sidecar_path(articles_dir, stem_path.stem)
        sidecar = load_sidecar(sc_path)
        suggestions = sidecar.get("internal_link_suggestions")
        if not isinstance(suggestions, list) or not suggestions:
            continue
        sidecars_with_suggestions += 1
        for sugg in suggestions:
            if not isinstance(sugg, dict):
                continue
            target_asin = sugg.get("target_asin")
            if not isinstance(target_asin, str) or not target_asin.strip():
                continue
            asin = target_asin.strip().upper()
            counts[asin] = counts.get(asin, 0) + 1
            suggestion_total += 1
            distinct_targets.add(asin)

    total_articles = len(article_index)
    coverage_ratio = (sidecars_with_suggestions / total_articles) if total_articles else 0.0
    coverage_stats: dict[str, Any] = {
        "total_articles": total_articles,
        "sidecars_with_suggestions": sidecars_with_suggestions,
        "suggestion_total": suggestion_total,
        "distinct_target_asins": len(distinct_targets),
        "coverage_ratio": round(coverage_ratio, 4),
        "low_coverage": coverage_ratio < INBOUND_SUGGESTION_COVERAGE_LOW_THRESHOLD,
    }
    return counts, coverage_stats, None


def build_inbound_report(
    urls: list[dict[str, Any]],
    inbound_counts: dict[str, int] | None,
    low_coverage: bool,
) -> list[dict[str, Any]]:
    """404 URL ごとに sidecar 被参照数を突き合わせ、少ない順にソートする。

    "unknown" (0 と区別する) になるケース:
      - ASIN が URL から抽出できない
      - inbound_counts が None (算出不能)
      - low_coverage=True かつ実測値が 0 (供給源がコーパスをほぼカバーして
        いない状態での 0 は「被リンクが無い」ではなく「未測定」— #4381
        レビュー指摘)。実測値が 1 件以上ある場合はカバレッジが低くても
        そのまま実数を採用する (少なくとも 1 件の候補生成という事実は
        観測されているため)。

    ソートは (unknown を最後、それ以外は昇順) → url 昇順。
    """
    out: list[dict[str, Any]] = []
    for row in urls:
        asin = extract_asin_from_page(row.get("url") or "")
        if asin is None or inbound_counts is None:
            inbound: int | str = "unknown"
        else:
            raw_count = inbound_counts.get(asin, 0)
            if low_coverage and raw_count == 0:
                inbound = "unknown"
            else:
                inbound = raw_count
        out.append({
            "url": row.get("url"),
            "asin": asin,
            "inbound_suggestions": inbound,
        })

    def sort_key(r: dict[str, Any]) -> tuple[int, int, str]:
        v = r["inbound_suggestions"]
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
    inbound_coverage: dict[str, Any],
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

    lines.append("## 3. sidecar 内部リンク候補の被参照数 (少ない順)")
    lines.append("⚠️ これは build_post.py が実際にページへ描画した内部リンクグラフ**ではない**。"
                 "26-faq-seo-lane (scripts/generate_internal_links.py) が出したリンク候補 "
                 "(internal_link_suggestions) の被参照数。navi の実際の内部リンクグラフは"
                 "リポジトリ内に永続化されていない。")
    lines.append("")
    lines.append("### 供給源カバレッジ")
    if inbound_error:
        lines.append(f"⚠️ 算出できませんでした: {inbound_error}")
    elif not inbound_coverage:
        lines.append("(カバレッジ統計なし)")
    else:
        c = inbound_coverage
        lines.append(f"- 記事総数: {c.get('total_articles')}")
        lines.append(f"- internal_link_suggestions を持つ sidecar 数: "
                     f"{c.get('sidecars_with_suggestions')} "
                     f"({c.get('coverage_ratio', 0) * 100:.1f}%)")
        lines.append(f"- suggestion 総数 (延べ): {c.get('suggestion_total')}")
        lines.append(f"- distinct target ASIN 数: {c.get('distinct_target_asins')}")
        if c.get("low_coverage"):
            lines.append(
                f"- ⚠️ カバレッジ閾値 ({INBOUND_SUGGESTION_COVERAGE_LOW_THRESHOLD * 100:.0f}%) 未満。"
                "個別 ASIN の被参照数が 0 の場合は「被リンクが無い」ではなく「未測定」と"
                "みなし unknown として出力する (実測値が 1 件以上ある場合はそのまま採用)。"
            )
    lines.append("")

    if inbound_error:
        pass
    elif not inbound_report:
        lines.append("(404 URL が 0 件のため対象なし)")
    else:
        unknown_count = sum(1 for r in inbound_report if r["inbound_suggestions"] == "unknown")
        if unknown_count:
            lines.append(f"⚠️ {unknown_count} 件は unknown (ASIN 抽出不能、または低カバレッジ下の 0)")
        lines.append("")
        lines.append("| url | asin | inbound_suggestions (sidecar) |")
        lines.append("|---|---|---:|")
        for row in inbound_report[:200]:
            lines.append(f"| {row['url']} | {row['asin'] or '(unknown)'} | {row['inbound_suggestions']} |")
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

    inbound_counts, inbound_coverage, inbound_error = count_inbound_suggestions(articles_dir)
    low_coverage = bool(inbound_coverage.get("low_coverage")) if inbound_coverage else False
    inbound_report = build_inbound_report(urls, inbound_counts, low_coverage)

    result: dict[str, Any] = {
        "trend": trend,
        "count_404": len(urls),
        "urls_404": urls,
        "unknown_coverage_states": unknown_states,
        "last_crawl_time_distribution": crawl_distribution,
        "inbound_suggestions": inbound_report,
        "inbound_suggestions_coverage": inbound_coverage,
        "inbound_suggestions_error": inbound_error,
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
        result["inbound_suggestions"],
        result["inbound_suggestions_error"],
        result["inbound_suggestions_coverage"],
    ))

    if args.json:
        out_path = pathlib.Path(args.json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    return 0


if __name__ == "__main__":
    sys.exit(main())
