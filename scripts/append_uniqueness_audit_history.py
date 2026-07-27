"""append_uniqueness_audit_history.py

#4098 (関連 #3203 Phase 3): 凡庸度の定量監査 (data/analytics/uniqueness_audit.json)
の週次スナップショットを data/analytics/history/uniqueness_audit_history.jsonl に
1 行 append する read-mostly スクリプト。scripts/append_census_history.py を手本にする。

なぜ必要か:
  uniqueness_audit.json は amazon-home-ops 側 (K8/Ruri) の週次 workflow が data PR
  で還流する「単一スナップショット」で、次週の run が来ると上書きされ時系列を
  保持しない。#3203 Phase 3 のしきい値/凡庸フラグ件数の推移を追跡するには、この
  history 蓄積レーンが必要 (scripts/append_census_history.py の B-3 と同型の欠落)。

入力:
  - data/analytics/uniqueness_audit.json (scripts/audit_uniqueness.py 出力を想定。
    実体は amazon-home-ops の K8/Ruri が data PR で還流する)

出力 (data/analytics/history/):
  - uniqueness_audit_history.jsonl
    {date, generated_at, model, corpus_size, flagged_total,
     max_sim_exceeded, centroid_sim_exceeded,
     threshold_mode, threshold_max_sim, threshold_centroid_sim,
     cohort_stats: {pre_v7, post_v7, all} それぞれ
       {count, max_sim_p25/p50/p75/p90, centroid_sim_p25/p50/p75/p90}}

date 列と ISO 週 (重要):
  uniqueness_audit.json は日次ではなく週次スナップショットで、日付ではなく
  `source_week` (例 "2026-W30") を持つ。history の "date" 列にはこの ISO 週
  文字列をそのまま入れる (実日付ではない点に注意。census 系との単純結合はできない)。

主指標 (進捗追跡):
  thresholds.absolute_reference.max_sim_exceeded / centroid_sim_exceeded は
  固定の絶対しきい値 (max_sim>0.95 / centroid_sim>0.9) を超えた件数で、
  percentile ベースの実効しきい値 (thresholds.max_sim / thresholds.centroid_sim
  は corpus 分布の95%点で毎回動く) と違って週をまたいで比較可能な唯一の指標。
  そのためこの2つを主指標として独立列に昇格させている。

unknown を pass に潰さない (#4098 指示):
  corpus_size / flagged_total / max_sim_exceeded / centroid_sim_exceeded /
  threshold_* は「欠損したら 0」にすると "0件で健全" という偽の成功シグナルに
  なる (集計不能 と 実際に0件 を混同する)。そのため欠損/型不正の場合は
  黙って 0 にせず None (JSON null) を入れ、warning を出す。
  cohort_stats はこれとは別の性質の欠損 (「その cohort が今週たまたま存在しない」)
  なので、列集合の安定性を優先して count は 0、percentile 系は null で埋める
  (census の COVERAGE_STATE_SLUGS 「既知の slug は毎行必ず出現させる」設計を踏襲)。

idempotency (D4 — census と同じ方針。共有サイドカーを使わない理由):
  append_analytics_history.py の seen_dates.json のような共有サイドカーは使わず、
  uniqueness_audit_history.jsonl 自体を都度スキャンして対象 source_week が既に
  存在するかで判定する。年間 ~52 行程度なので毎回全件スキャンしても軽量
  (census の D4 と同一の理由・同一の軽量性)。

入力欠損/破損時の扱い:
  ファイルが存在しない場合は census と同じく skip・exit 0。
  存在はするが JSON として壊れている場合も、このレーン (workflow の required
  check) を fail させず警告して exit 0 とする (#4098 の明示指示。census は
  「存在しない」場合のみ考慮しているが、本スクリプトは「壊れている」場合も
  同様に graceful にする)。

副作用:
  - data/analytics/history/uniqueness_audit_history.jsonl への append のみ
  - 共有サイドカーには触れない
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
logger = logging.getLogger("append_uniqueness_audit_history")

DEFAULT_UNIQUENESS_AUDIT = "data/analytics/uniqueness_audit.json"

# JSONL ファイル名 (schema.json と同期)
UNIQUENESS_HISTORY_FILE = "uniqueness_audit_history.jsonl"

# cohort_stats の既知 cohort 名。列集合を時系列で安定させるため、audit_uniqueness.py
# の出力に無い cohort でも毎行必ず出現させる (census の KNOWN_SLUGS と同型)。
KNOWN_COHORTS: tuple[str, ...] = ("pre_v7", "post_v7", "all")

# cohort_stats 内、cohort ごとの percentile 系フィールド名。
PERCENTILE_FIELDS: tuple[str, ...] = (
    "max_sim_p25", "max_sim_p50", "max_sim_p75", "max_sim_p90",
    "centroid_sim_p25", "centroid_sim_p50", "centroid_sim_p75", "centroid_sim_p90",
)


def _as_number(value: Any) -> float | int | None:
    """数値として扱える値のみ通す。欠損/型不正は None (unknown を 0 に潰さない)。"""
    if isinstance(value, bool):  # bool は int のサブクラスなので明示的に弾く
        return None
    if isinstance(value, (int, float)):
        return value
    return None


def _as_str(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def build_cohort_stats(cohort_stats_in: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """audit の cohort_stats を既知 cohort (KNOWN_COHORTS) ごとの固定フィールド集合に
    正規化する。

    count は cohort 自体が欠損していれば 0 (「その週たまたま該当 cohort が無い」)、
    percentile 系は欠損/型不正なら None (未計算の統計値を 0 に潰さない)。
    列集合を時系列で安定させるため、KNOWN_COHORTS 全てを必ず key に持つ。
    """
    cohort_stats_in = cohort_stats_in or {}
    result: dict[str, dict[str, Any]] = {}
    for name in KNOWN_COHORTS:
        src = cohort_stats_in.get(name)
        if not isinstance(src, dict):
            if name in cohort_stats_in:
                logger.warning(
                    "cohort_stats.%s has unexpected shape (%r) — treating as missing",
                    name, type(src).__name__,
                )
            src = {}
        entry: dict[str, Any] = {"count": _as_number(src.get("count")) or 0}
        for field in PERCENTILE_FIELDS:
            entry[field] = _as_number(src.get(field))
        result[name] = entry
    return result


def build_row(audit: dict) -> dict[str, Any] | None:
    """audit dict から history 行を 1 件構築する。source_week が読み取れなければ None。"""
    target_week = _as_str(audit.get("source_week"))
    if not target_week:
        return None

    thresholds = audit.get("thresholds")
    thresholds = thresholds if isinstance(thresholds, dict) else {}
    absolute_reference = thresholds.get("absolute_reference")
    absolute_reference = absolute_reference if isinstance(absolute_reference, dict) else {}

    max_sim_exceeded = _as_number(absolute_reference.get("max_sim_exceeded"))
    centroid_sim_exceeded = _as_number(absolute_reference.get("centroid_sim_exceeded"))
    if max_sim_exceeded is None or centroid_sim_exceeded is None:
        logger.warning(
            "uniqueness_audit %s: thresholds.absolute_reference.max_sim_exceeded/"
            "centroid_sim_exceeded missing or malformed — recording as null, NOT 0 "
            "(unknown != pass)", target_week,
        )

    row: dict[str, Any] = {
        "date": target_week,
        "generated_at": _as_str(audit.get("generated_at")),
        "model": _as_str(audit.get("model")),
        "corpus_size": _as_number(audit.get("corpus_size")),
        "flagged_total": _as_number(audit.get("flagged_total")),
        "max_sim_exceeded": max_sim_exceeded,
        "centroid_sim_exceeded": centroid_sim_exceeded,
        "threshold_mode": _as_str(thresholds.get("mode")),
        "threshold_max_sim": _as_number(thresholds.get("max_sim")),
        "threshold_centroid_sim": _as_number(thresholds.get("centroid_sim")),
        "cohort_stats": build_cohort_stats(audit.get("cohort_stats")),
    }
    return row


def existing_dates(history_path: pathlib.Path) -> set[str]:
    """既存 jsonl をスキャンして記録済み date (= source_week) の集合を返す。

    共有サイドカーを使わない idempotency (D4、append_census_history.py と同方針)。
    壊れた行 / 欠損ファイルは無視して寛容に扱う (このレーンを workflow が fail
    する理由にしない)。
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


def run(audit: dict, history_dir: pathlib.Path) -> tuple[bool, str | None]:
    """1 件分の audit を history へ append する。

    戻り値は (appended: bool, source_week: str | None)。source_week が None
    のときは source_week が読み取れず何もしなかった (呼び出し側で警告)。
    """
    row = build_row(audit)
    if row is None:
        logger.warning("uniqueness_audit input has no usable source_week — skipping")
        return False, None

    target_week = row["date"]
    history_path = history_dir / UNIQUENESS_HISTORY_FILE
    if target_week in existing_dates(history_path):
        logger.info("uniqueness_audit source_week %s already in history — skip", target_week)
        return False, target_week

    append_jsonl(history_path, [row])
    logger.info(
        "uniqueness_audit %s: appended 1 row (corpus_size=%s, flagged_total=%s, "
        "max_sim_exceeded=%s, centroid_sim_exceeded=%s)",
        target_week, row["corpus_size"], row["flagged_total"],
        row["max_sim_exceeded"], row["centroid_sim_exceeded"],
    )
    return True, target_week


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--uniqueness-audit", default=DEFAULT_UNIQUENESS_AUDIT,
                   help="audit_uniqueness.py 出力 JSON path (存在しない場合 skip・exit 0)")
    p.add_argument("--history-dir", default=DEFAULT_HISTORY_DIR)
    args = p.parse_args()

    audit_path = pathlib.Path(args.uniqueness_audit)
    if not audit_path.exists():
        logger.info("uniqueness_audit input not found: %s — skip", audit_path)
        return 0

    try:
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(
            "could not read/parse %s (%s) — skipping without failing the lane",
            audit_path, e,
        )
        return 0

    if not isinstance(audit, dict):
        logger.warning("%s does not contain a JSON object — skipping", audit_path)
        return 0

    history_dir = pathlib.Path(args.history_dir)
    appended, target_week = run(audit, history_dir)

    if target_week is None:
        return 0
    logger.info("done. appended=%s source_week=%s", appended, target_week)
    return 0


if __name__ == "__main__":
    sys.exit(main())
