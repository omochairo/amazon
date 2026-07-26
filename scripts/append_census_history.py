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

副作用:
  - data/analytics/history/gsc_index_census.jsonl への append のみ
  - 共有サイドカー (seen_dates.json) には触れない
  - 記事生成 / score / narrative には触れない
"""
from __future__ import annotations

import argparse
import json
import logging
import pathlib
import sys
from typing import Any

from scripts.append_analytics_history import DEFAULT_HISTORY_DIR, append_jsonl

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("append_census_history")

DEFAULT_CENSUS = "data/analytics/gsc_index_census.json"

# JSONL ファイル名 (schema.json と同期)
CENSUS_HISTORY_FILE = "gsc_index_census.jsonl"

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


def run(census: dict, history_dir: pathlib.Path) -> tuple[bool, str | None]:
    """1 件分の census を history へ append する。

    戻り値は (appended: bool, date: str | None)。date が None のときは
    fetched_at が読み取れず何もしなかった (呼び出し側で警告)。
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

    append_jsonl(history_path, [row])
    logger.info(
        "gsc_index_census %s: appended 1 row (inspected=%d, indexed=%d, "
        "indexed_rate=%.4f, circuit_breaker_tripped=%s)",
        target_date, row["inspected"], row["indexed"], row["indexed_rate"],
        row["circuit_breaker_tripped"],
    )
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
    args = p.parse_args()

    census_path = pathlib.Path(args.census)
    if not census_path.exists():
        logger.info("census input not found: %s — skip", census_path)
        return 0

    census = json.loads(census_path.read_text(encoding="utf-8"))
    history_dir = pathlib.Path(args.history_dir)
    appended, target_date = run(census, history_dir)

    if target_date is None:
        return 0
    logger.info("done. appended=%s date=%s", appended, target_date)
    return 0


if __name__ == "__main__":
    sys.exit(main())
