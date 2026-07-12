"""自前 Amazon 価格履歴の append-only 蓄積 (#2953 C案・訂正版設計)。

背景: fetch_amazon.refresh_article_snapshots (2026-07-07〜) が既掲載 ASIN を
週1周期で GetItems 巡回し per_asin/<ASIN>/amazon.json を "上書き" しているため、
週1の価格観測点が発生しているのに履歴として残らない。Amazon の視覚的価格推移は
Keepa 画像 (post.md.j2 の .keepa-graph) で提供済みだが画像なのでクロール可能な
テキストにならない。本モジュールは write_per_asin_snapshot の書き込み成功時に
呼ばれ、価格変化があったときだけ ``data/price_history/<ASIN>.jsonl`` に1行追記する。
追加 API 呼び出しはゼロ (既存巡回の書き込み点に hook するだけ)。

設計要点:
  - 重複抑制: 同一 source の最終行と価格が同じ かつ 最終 ts から 6 日未満なら
    追記しない (価格が変わっていれば経過日数によらず即追記)。1 ASIN あたり
    週1点程度の想定 (年 ~50 行) のため、ファイル全読みで実装を簡素にしている。
  - ベストエフォート: 価格履歴の書き込み失敗はフェッチ本体を絶対に止めない。
    例外は内部で catch して logger.warning、False を返す。
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger("price_history")

# 重複抑制: 同一価格が続く場合にこの日数未満では追記しない。
_DEDUPE_MIN_DAYS = 6


def _read_last_line_for_source(path: str, source: str) -> Optional[dict]:
    """``path`` (jsonl) から ``source`` に一致する最終行を読んで返す。無ければ None。

    壊れた行 (JSON decode 不能) は無視して読み進める。ファイルサイズが小さい
    (1 ASIN 週1点想定) うちは全行読みで十分なため、末尾からの逆読み最適化はしない。
    """
    last: Optional[dict] = None
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict) and rec.get("source") == source:
                    last = rec
    except OSError as e:
        logger.warning(f"price_history: failed to read {path}: {e}")
        return None
    return last


def append_price_point(
    price_history_dir: str,
    asin: str,
    source: str,
    price: Any,
    availability: Any,
    ts: Optional[datetime] = None,
) -> bool:
    """価格観測点を ``<price_history_dir>/<ASIN大文字>.jsonl`` に追記する。

    Args:
        price_history_dir: 出力先ディレクトリ (例: ``data/price_history``)。
        asin: 対象 ASIN (ファイル名は大文字化して使う)。
        source: 観測元 (例: ``"amazon"``)。
        price: 価格 (円)。int でない、または 0 以下なら何もしない。
        availability: 在庫メッセージ文字列。無ければ null で記録する。
        ts: 観測時刻。省略時は呼び出し時点の UTC now。

    Returns:
        追記したら True。スキップ (無効な price / 重複抑制) や例外時は False。
        価格履歴はベストエフォートであり、例外はここで catch して呼び出し元
        (フェッチ本体) を絶対に落とさない。
    """
    try:
        if not isinstance(price, int) or isinstance(price, bool) or price <= 0:
            return False
        if not asin:
            return False

        now = ts if ts is not None else datetime.now(timezone.utc)
        path = os.path.join(price_history_dir, f"{asin.upper()}.jsonl")

        last = _read_last_line_for_source(path, source) if os.path.exists(path) else None
        if last is not None and last.get("price") == price:
            last_ts_raw = last.get("ts")
            if isinstance(last_ts_raw, str):
                try:
                    last_ts = datetime.fromisoformat(last_ts_raw.replace("Z", "+00:00"))
                    if now - last_ts < timedelta(days=_DEDUPE_MIN_DAYS):
                        return False
                except ValueError:
                    pass  # 壊れた ts はスキップ判定せず素通し (安全側 = 追記する)

        record = {
            "ts": now.isoformat(),
            "source": source,
            "price": price,
            "availability": availability if availability else None,
        }
        os.makedirs(price_history_dir, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return True
    except Exception as e:  # ベストエフォート: フェッチ本体を絶対に止めない
        logger.warning(f"price_history: append_price_point failed for {asin}: {e}")
        return False
