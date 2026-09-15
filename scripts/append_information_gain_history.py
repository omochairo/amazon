"""append_information_gain_history.py

#4841 S3: scripts/audit_information_gain.py の出力
(``data/analytics/information_gain_audit.json``) を
``data/analytics/information_gain_history.jsonl`` に1行 append する read-mostly
スクリプト。scripts/append_uniqueness_audit_history.py を手本にする。

なぜ要るか:
  information_gain_audit.json は amazon-home-ops 側 (K8) の週次 workflow が
  data PR で還流する「単一スナップショット」で、次週の run が来ると上書きされ
  時系列を保持しない。素材供給の変化が情報利得に効いているかを定点観測する
  には、この history 蓄積レーンが要る。

date 列 (重要):
  information_gain_audit.json は日次ではなく週次スナップショットで、日付ではなく
  ``source_week`` (例 "2026-W38") を持つ。history の "date" 列にはこの ISO 週
  文字列をそのまま入れる。

置き場所:
  data/analytics/history/ の**外** (data/analytics/ 直下)。#4841 実装依頼 S3 の
  明示指定。scripts/check_history_freshness.py の SINGLE_FILE_LANES に登録する
  (通常の LANES は data/analytics/history/ 配下しか見ないため別枠が要る)。

unknown を pass に潰さない (append_uniqueness_audit_history.py の D4 と同じ方針):
  target_count / processed_count / failed_count / summary の各値は欠損/型不正
  の場合 None (JSON null) を入れ、warning を出す。0 に潰すと「0件で健全」という
  偽の成功シグナルになる。

idempotency:
  共有サイドカーは使わず、jsonl 自体を都度スキャンして対象 source_week が
  既に存在するかで判定する (append_uniqueness_audit_history.py の D4 と同型)。

副作用:
  - data/analytics/information_gain_history.jsonl への append のみ
"""
from __future__ import annotations

import argparse
import json
import logging
import pathlib
import sys
from typing import Any

from scripts.append_analytics_history import append_jsonl

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("append_information_gain_history")

DEFAULT_INFORMATION_GAIN_AUDIT = "data/analytics/information_gain_audit.json"
DEFAULT_HISTORY_PATH = "data/analytics/information_gain_history.jsonl"


def _as_number(value: Any) -> float | int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    return None


def _as_str(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def build_row(audit: dict) -> dict[str, Any] | None:
    """audit dict から history 行を1件構築する。source_week が読み取れなければ None。"""
    target_week = _as_str(audit.get("source_week"))
    if not target_week:
        return None

    summary = audit.get("summary")
    summary = summary if isinstance(summary, dict) else {}
    by_material = summary.get("by_experience_material")
    by_material = by_material if isinstance(by_material, dict) else {}
    classification = summary.get("unsupported_classification_totals")
    classification = classification if isinstance(classification, dict) else {}

    target_count = _as_number(summary.get("target_count"))
    failed_count = _as_number(summary.get("failed_count"))
    if target_count is None or failed_count is None:
        logger.warning(
            "information_gain_audit %s: summary.target_count/failed_count missing or "
            "malformed — recording as null, NOT 0 (unknown != pass)", target_week,
        )

    def _material_group(name: str) -> dict[str, Any]:
        g = by_material.get(name)
        g = g if isinstance(g, dict) else {}
        return {
            "count": _as_number(g.get("count")) or 0,
            "median_unique_and_supported_count": _as_number(g.get("median_unique_and_supported_count")),
            "median_unique_and_supported_rate": _as_number(g.get("median_unique_and_supported_rate")),
        }

    row: dict[str, Any] = {
        "date": target_week,
        "generated_at": _as_str(audit.get("generated_at")),
        "model": _as_str(audit.get("model")),
        "target_count": target_count,
        "processed_count": _as_number(summary.get("processed_count")),
        "failed_count": failed_count,
        "failure_ratio": _as_number(summary.get("failure_ratio")),
        "median_unique_and_supported_count": _as_number(summary.get("median_unique_and_supported_count")),
        "median_unique_and_supported_rate": _as_number(summary.get("median_unique_and_supported_rate")),
        "with_material": _material_group("with_material"),
        "without_material": _material_group("without_material"),
        "unsupported_classification_totals": {
            "rhetorical_or_time_dependent": _as_number(classification.get("rhetorical_or_time_dependent")) or 0,
            "factual_claim": _as_number(classification.get("factual_claim")) or 0,
            "unresolved": _as_number(classification.get("unresolved")) or 0,
        },
    }
    return row


def existing_dates(history_path: pathlib.Path) -> set[str]:
    """既存 jsonl をスキャンして記録済み date (= source_week) の集合を返す。"""
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


def run(audit: dict, history_path: pathlib.Path) -> tuple[bool, str | None]:
    """1件分の audit を history へ append する。戻り値は (appended, source_week)。"""
    row = build_row(audit)
    if row is None:
        logger.warning("information_gain_audit input has no usable source_week — skipping")
        return False, None

    target_week = row["date"]
    if target_week in existing_dates(history_path):
        logger.info("information_gain_audit source_week %s already in history — skip", target_week)
        return False, target_week

    append_jsonl(history_path, [row])
    logger.info(
        "information_gain_audit %s: appended 1 row (target=%s, failed=%s, median_count=%s)",
        target_week, row["target_count"], row["failed_count"], row["median_unique_and_supported_count"],
    )
    return True, target_week


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--information-gain-audit", default=DEFAULT_INFORMATION_GAIN_AUDIT,
                   help="audit_information_gain.py 出力 JSON path (存在しない場合 skip・exit 0)")
    p.add_argument("--history-path", default=DEFAULT_HISTORY_PATH)
    args = p.parse_args()

    audit_path = pathlib.Path(args.information_gain_audit)
    if not audit_path.exists():
        logger.info("information_gain_audit input not found: %s — skip", audit_path)
        return 0

    try:
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("could not read/parse %s (%s) — skipping without failing the lane", audit_path, e)
        return 0

    if not isinstance(audit, dict):
        logger.warning("%s does not contain a JSON object — skipping", audit_path)
        return 0

    history_path = pathlib.Path(args.history_path)
    appended, target_week = run(audit, history_path)

    if target_week is None:
        return 0
    logger.info("done. appended=%s source_week=%s", appended, target_week)
    return 0


if __name__ == "__main__":
    sys.exit(main())
