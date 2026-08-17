"""append_census_history.py

B-3 (#3988 / 関連 #3331, #3333, #2701): GSC index census
(data/analytics/gsc_index_census.json) の週次スナップショットを
data/analytics/history/gsc_index_census.jsonl に 1 行 append する read-mostly スクリプト。

なぜ必要か:
  gsc_index_census.json は inspect_gsc_index.py が毎週上書きする「単一スナップショット」で
  時系列を保持しない。そのため #3331 が前提としていた「週次 census の by_coverage_state
  推移で自動的に効果判定できる」は成立せず、#3333 で予定していた品質ゲートも計算できない。
  本スクリプトはその欠落を埋めるための history 蓄積レーンを追加する。

入力:
  - data/analytics/gsc_index_census.json (inspect_gsc_index.py 出力を想定)

出力 (data/analytics/history/):
  - gsc_index_census.jsonl  {date, sitemap_urls, inspected, indexed, not_indexed, errors,
                             indexed_rate, <known coverage slugs...>, other,
                             circuit_breaker_tripped, unmapped}

coverageState の安定化 (D1):
  inspect_gsc_index.py の coverage_state は GSC API が返す「ローカライズされた表示文字列」
  そのまま (inspect_gsc_index.py:215 coverageState)。日本語文字列はロケール依存で
  Google 側の言い回し変更に弱く、これをそのまま jsonl の列名に使うと将来無言で壊れる。
  そのため既知の raw 文字列 (.strip() 後の完全一致) を ASCII slug にマッピングするテーブルを
  ここに持つ。未知の raw 文字列は `other` カウンタに集約しつつ、`unmapped` に
  {raw_string: count} として原文のまま保存する (無言で捨てない。将来 Google が言い回しを
  変えた際に retroactive にマッピングし直せるように)。

  既知の slug は毎行必ず出現させる (値がなければ 0)。時系列として列集合を安定させるため。

partial run の扱い (D3):
  circuit_breaker.tripped == true の census は quota エラーで打ち切られた「部分集計」であり、
  完全実行分と単純比較すると偽の改善/悪化として誤検出される (#3372)。行自体は捨てずに
  記録し、circuit_breaker_tripped で consumer 側がフィルタできるようにする。

idempotency (D4 — 共有サイドカーを使わない理由):
  append_analytics_history.py は seen_dates.json という共有サイドカーで idempotency を
  管理しているが、このレーンでは使わない。workflow 22 (日曜 22:00 UTC) と
  workflow 18 (毎日 21:00 UTC) は 1 時間しか離れておらず、共有サイドカーへの
  read-modify-write が競合すると mark を取りこぼす恐れがある。代わりに
  gsc_index_census.jsonl 自体を都度スキャンして対象 date が既に存在するかで判定する。
  年間 ~52 行程度なので毎回全件スキャンしても軽量。

コホート遷移 (#2687):
  sitemap は週 +100 URL 超のペースで増えており、新規記事が上流ステージ (未認識/
  検出-未登録) に流入し続けるため、coverage_state 別の絶対件数だけを見ると
  「既存 URL の回収」が新規流入に打ち消されて誤った判定 (悪化に見える) を出す。
  そのため直近 run 時点の not-indexed URL 状態を data/analytics/history/
  census_url_states.json に保存し、次回 run でその URL 集合を突き合わせて
  URL 単位の遷移 (前進/後退/滞留/exit) を `cohort` として jsonl の行に追加する。
  前回スナップショットが無い初回は `{"previous_date": None}` のみを記録し、
  このレーンを fail させない。

  exit (前回 not-indexed だったが今回 not_indexed_urls に居ない URL) は
  data/articles/ の記事ファイル存在で indexed 到達 (exited_indexed) と
  sitemap 離脱 (exited_dropped) に分類する。articles dir が読めない場合は
  不明を indexed に潰さず、両キーとも省略して警告する。

副作用:
  - data/analytics/history/gsc_index_census.jsonl への append のみ
  - data/analytics/history/census_url_states.json の上書き (append が
    idempotency でスキップされた場合は上書きしない)
  - 共有サイドカー (seen_dates.json) には触れない
  - 記事生成 / score / narrative には触れない
"""
from __future__ import annotations

import argparse
import json
import logging
import pathlib
import re
import sys
from typing import Any

from scripts.append_analytics_history import DEFAULT_HISTORY_DIR, append_jsonl

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("append_census_history")

DEFAULT_CENSUS = "data/analytics/gsc_index_census.json"
DEFAULT_ARTICLES_DIR = "data/articles"

# JSONL ファイル名 (schema.json と同期)
CENSUS_HISTORY_FILE = "gsc_index_census.jsonl"
# コホート追跡用の前回 not-indexed URL 状態スナップショット (#2687)。
# history として積まず、毎 run 上書きする (常に直近 run 分のみ保持)。
CENSUS_URL_STATES_FILE = "census_url_states.json"

# 記事本体ファイル名は YYYY-MM-DD-{ASIN}.json。sidecar (enrichment/quality/seo) の
# 正準3種は本体と誤認しないよう明示的に除外する。
ARTICLE_SIDECAR_SUFFIXES: tuple[str, ...] = (".enrichment.json", ".quality.json", ".seo.json")
_ARTICLE_ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")

# coverageState 生文字列 (.strip() 後、完全一致) -> 安定 ASCII slug
# 2026-07-19 census (data/analytics/gsc_index_census.json) から採取した実測値。
# 見つかりませんでした（404） の括弧は全角 (U+FF08 / U+FF09)。
COVERAGE_STATE_SLUGS: dict[str, str] = {
    "送信して登録されました": "submitted_and_indexed",
    "検出 - インデックス未登録": "discovered_not_indexed",
    "URL が Google に認識されていません": "unknown_to_google",
    "クロール済み - インデックス未登録": "crawled_not_indexed",
    "見つかりませんでした（404）": "not_found_404",
    "noindex タグによって除外されました": "excluded_by_noindex",
    # 2026-08-09 census で初出。#2370 A-3 の canonical 統合 (build_post.py が
    # data/analytics/canonical_overrides.json から .Params.canonicalURL を付ける) を
    # Google 側が受け入れると、統合元の URL がこの状態になる。登録前は "other" に
    # 落ちて report_census_404.py が毎回「未知の coverage_state」と警告していた。
    # 括弧は全角 (U+FF08 / U+FF09)、canonical の前後は半角スペース。
    "代替ページ（適切な canonical タグあり）": "alternate_page_with_canonical",
}

KNOWN_SLUGS: tuple[str, ...] = tuple(COVERAGE_STATE_SLUGS.values())


def map_coverage_states(by_coverage_state: dict[str, Any]) -> tuple[dict[str, int], dict[str, int]]:
    """census の by_coverage_state を既知 slug ごとのカウントに変換する。

    戻り値は (slug_counts, unmapped) のタプル。slug_counts は KNOWN_SLUGS 全てを
    key に持つ (未出現は 0) + "other" (未知 raw 文字列の合計)。unmapped は
    未知の raw 文字列をそのまま key として保持する (再マッピング用)。
    """
    slug_counts: dict[str, int] = {slug: 0 for slug in KNOWN_SLUGS}
    slug_counts["other"] = 0
    unmapped: dict[str, int] = {}

    for raw, count in (by_coverage_state or {}).items():
        key = (raw or "").strip()
        slug = COVERAGE_STATE_SLUGS.get(key)
        try:
            count = int(count)
        except (TypeError, ValueError):
            count = 0
        if slug is not None:
            slug_counts[slug] += count
        else:
            slug_counts["other"] += count
            unmapped[raw] = unmapped.get(raw, 0) + count

    if unmapped:
        logger.warning(
            "unmapped coverageState value(s) encountered — preserved in 'unmapped' "
            "for retroactive remapping: %s", unmapped
        )

    return slug_counts, unmapped


def _slug_for_raw_state(raw: str) -> str:
    """1 件分の coverageState raw 文字列を ASCII slug に変換する (未知は other)。"""
    key = (raw or "").strip()
    return COVERAGE_STATE_SLUGS.get(key, "other")


def build_current_url_states(not_indexed_urls: list[dict[str, Any]]) -> dict[str, str]:
    """census の not_indexed_urls ({"url":..., "coverage_state":...} の配列) から
    {url: slug} の状態スナップショットを作る。"""
    states: dict[str, str] = {}
    for item in not_indexed_urls or []:
        url = item.get("url")
        if not url:
            continue
        states[url] = _slug_for_raw_state(item.get("coverage_state", ""))
    return states


def load_url_states_snapshot(path: pathlib.Path) -> dict[str, Any] | None:
    """前回の census_url_states.json を読む。存在しない/壊れている場合は None
    (= 初回扱い。このレーンを fail させない)。"""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("could not read %s (%s) — treating as no previous snapshot", path, e)
        return None
    if not isinstance(data, dict) or not isinstance(data.get("states"), dict):
        logger.warning("%s has unexpected shape — treating as no previous snapshot", path)
        return None
    return data


def save_url_states_snapshot(path: pathlib.Path, date: str, states: dict[str, str]) -> None:
    """今回の not-indexed URL 状態でスナップショットを上書きする。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"date": date, "states": states}, indent=2, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )


def discover_existing_asins(articles_dir: pathlib.Path) -> set[str] | None:
    """data/articles/ 配下の記事本体ファイルから存在する ASIN の集合を作る。

    sidecar (ARTICLE_SIDECAR_SUFFIXES) は本体と誤認しないよう除外する。
    ディレクトリが存在しない/読めない場合は None を返す (「不明」と「記事なし」を
    混同しないため — 呼び出し側で exited_indexed/exited_dropped を省略させる)。
    """
    if not articles_dir.exists() or not articles_dir.is_dir():
        return None
    try:
        entries = list(articles_dir.iterdir())
    except OSError as e:
        logger.warning("could not read articles dir %s (%s)", articles_dir, e)
        return None

    asins: set[str] = set()
    for f in entries:
        if not f.is_file():
            continue
        name = f.name
        if not name.endswith(".json"):
            continue
        if any(name.endswith(suf) for suf in ARTICLE_SIDECAR_SUFFIXES):
            continue
        stem = name[: -len(".json")]
        parts = stem.split("-")
        asin = parts[-1] if parts else ""
        if _ARTICLE_ASIN_RE.match(asin):
            asins.add(asin)
    return asins


def _asin_from_url(url: str) -> str:
    return url.rstrip("/").split("/")[-1].upper()


def compute_cohort(
    current_states: dict[str, str],
    previous_snapshot: dict[str, Any] | None,
    existing_asins: set[str] | None,
) -> dict[str, Any]:
    """前回スナップショットと今回の not-indexed URL 状態を突き合わせ、コホート
    遷移 (#2687) を計算する。前回スナップショットが無ければ
    {"previous_date": None} のみを返す (初回・正常終了)。
    """
    if previous_snapshot is None:
        logger.warning(
            "no previous census_url_states snapshot — skipping cohort transition "
            "calc (initial run, #2687)"
        )
        return {"previous_date": None}

    previous_date = previous_snapshot.get("date")
    previous_states: dict[str, str] = previous_snapshot.get("states") or {}

    transition_counts: dict[str, int] = {}
    exit_urls: list[str] = []

    for url, prev_slug in previous_states.items():
        cur_slug = current_states.get(url)
        if cur_slug is not None:
            key = f"{prev_slug}->{cur_slug}"
            transition_counts[key] = transition_counts.get(key, 0) + 1
        else:
            exit_urls.append(url)

    entered_not_indexed = sum(1 for url in current_states if url not in previous_states)

    # 件数降順 → キー昇順
    sorted_transitions = dict(
        sorted(transition_counts.items(), key=lambda kv: (-kv[1], kv[0]))
    )

    cohort: dict[str, Any] = {
        "previous_date": previous_date,
        "previous_not_indexed": len(previous_states),
        "transitions": sorted_transitions,
        "exited_not_indexed": len(exit_urls),
        "entered_not_indexed": entered_not_indexed,
    }

    if existing_asins is None:
        logger.warning(
            "articles dir unavailable — cannot classify %d exited URL(s) as "
            "indexed/dropped; omitting exited_indexed/exited_dropped (#2687)",
            len(exit_urls),
        )
    else:
        exited_indexed = 0
        exited_dropped = 0
        for url in exit_urls:
            asin = _asin_from_url(url)
            if asin in existing_asins:
                exited_indexed += 1
            else:
                exited_dropped += 1
        cohort["exited_indexed"] = exited_indexed
        cohort["exited_dropped"] = exited_dropped

    return cohort


def _date_from_fetched_at(census: dict) -> str | None:
    fetched_at = census.get("fetched_at")
    if not fetched_at or not isinstance(fetched_at, str):
        return None
    # ISO8601 の先頭 10 文字が UTC date 部分 (fetched_at は常に +00:00 で書かれる想定。
    # inspect_gsc_index.py は datetime.now(timezone.utc).isoformat() で生成している)
    return fetched_at[:10]


def build_row(census: dict) -> dict[str, Any] | None:
    """census dict から history 行を 1 件構築する。fetched_at が無ければ None。"""
    target_date = _date_from_fetched_at(census)
    if not target_date:
        return None

    totals = census.get("totals", {}) or {}
    sitemap_urls = int(totals.get("sitemap_urls", 0) or 0)
    inspected = int(totals.get("inspected", 0) or 0)
    indexed = int(totals.get("indexed", 0) or 0)
    not_indexed = int(totals.get("not_indexed", 0) or 0)
    errors = int(totals.get("errors", 0) or 0)
    indexed_rate = round(indexed / inspected, 4) if inspected else 0.0

    slug_counts, unmapped = map_coverage_states(census.get("by_coverage_state", {}) or {})

    circuit_breaker = census.get("circuit_breaker", {}) or {}
    tripped = bool(circuit_breaker.get("tripped", False))
    if tripped:
        logger.warning(
            "census %s: circuit_breaker.tripped=true — this row is a PARTIAL run "
            "(#3372). Flagging circuit_breaker_tripped=true; exclude from trend "
            "comparisons.", target_date
        )

    row: dict[str, Any] = {
        "date": target_date,
        "sitemap_urls": sitemap_urls,
        "inspected": inspected,
        "indexed": indexed,
        "not_indexed": not_indexed,
        "errors": errors,
        "indexed_rate": indexed_rate,
        "circuit_breaker_tripped": tripped,
        "unmapped": unmapped,
    }
    row.update(slug_counts)
    return row


def existing_dates(history_path: pathlib.Path) -> set[str]:
    """既存 jsonl をスキャンして記録済み date の集合を返す。

    共有サイドカーを使わない idempotency (D4)。壊れた行 / 欠損ファイルは
    無視して寛容に扱う (このレーンを workflow が fail する理由にしない)。
    """
    if not history_path.exists():
        return set()
    dates: set[str] = set()
    try:
        text = history_path.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("could not read %s (%s) — treating as empty", history_path, e)
        return set()
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("skipping corrupt line in %s", history_path)
            continue
        d = obj.get("date")
        if d:
            dates.add(d)
    return dates


def run(
    census: dict,
    history_dir: pathlib.Path,
    articles_dir: pathlib.Path | str = DEFAULT_ARTICLES_DIR,
) -> tuple[bool, str | None]:
    """1 件分の census を history へ append する。

    戻り値は (appended: bool, date: str | None)。date が None のときは
    fetched_at が読み取れず何もしなかった (呼び出し側で警告)。

    #2687: コホート遷移計算とスナップショット更新は「append を実際に行った
    場合のみ」実施する。idempotency (同一 date 既存) で append をスキップした
    場合はスナップショットも触らない (二重実行で前回状態が壊れると次回の遷移
    計算が無意味になるため)。
    """
    row = build_row(census)
    if row is None:
        logger.warning("census input has no usable fetched_at — skipping")
        return False, None

    target_date = row["date"]
    history_path = history_dir / CENSUS_HISTORY_FILE
    if target_date in existing_dates(history_path):
        logger.info("gsc_index_census date %s already in history — skip", target_date)
        return False, target_date

    current_states = build_current_url_states(census.get("not_indexed_urls") or [])
    states_path = history_dir / CENSUS_URL_STATES_FILE
    previous_snapshot = load_url_states_snapshot(states_path)
    existing_asins = discover_existing_asins(pathlib.Path(articles_dir))
    row["cohort"] = compute_cohort(current_states, previous_snapshot, existing_asins)

    append_jsonl(history_path, [row])
    logger.info(
        "gsc_index_census %s: appended 1 row (inspected=%d, indexed=%d, "
        "indexed_rate=%.4f, circuit_breaker_tripped=%s)",
        target_date, row["inspected"], row["indexed"], row["indexed_rate"],
        row["circuit_breaker_tripped"],
    )

    save_url_states_snapshot(states_path, target_date, current_states)

    return True, target_date


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--census", default=DEFAULT_CENSUS,
                   help="inspect_gsc_index.py 出力 JSON path (存在しない場合 skip・exit 0)")
    p.add_argument("--history-dir", default=DEFAULT_HISTORY_DIR)
    p.add_argument("--articles-dir", default=DEFAULT_ARTICLES_DIR,
                   help="コホート exit 判定 (indexed/dropped) 用の記事ディレクトリ (#2687)")
    args = p.parse_args()

    census_path = pathlib.Path(args.census)
    if not census_path.exists():
        logger.info("census input not found: %s — skip", census_path)
        return 0

    census = json.loads(census_path.read_text(encoding="utf-8"))
    history_dir = pathlib.Path(args.history_dir)
    appended, target_date = run(census, history_dir, pathlib.Path(args.articles_dir))

    if target_date is None:
        return 0
    logger.info("done. appended=%s date=%s", appended, target_date)
    return 0


if __name__ == "__main__":
    sys.exit(main())
