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

在庫切れの記録 (#5130・オーナー判断済み):
  価格が取れない日を 1 行も残さないと、階段線が「最後に価格が取れた日の値が今日まで
  続いた」と嘘をつく。実測 (2026-08-13) で日次レーンの 123/1,890 ASIN (6.5%) が
  価格を取れておらず、うち 69 記事で価格履歴ブロックが描画されていた。
  そこで ``price=None`` + ``availability`` の行を残す。既存行のスキーマは変えない
  (``price`` が int の行はそのまま) ので、読み側 (build_post._load_price_history_points /
  where_to_buy_format._load_amazon_price_points / build_price_dashboard.load_merged_history)
  は 3 本とも ``price`` が正の int でない行を捨てる実装のままで壊れない。
  描画側で在庫切れ区間を区別するのは #5130 の項目 2 (別 PR)。

  **過去の在庫切れ期間は遡って復元できない** (記録が存在しない)。ここから先に
  発生した期間だけが正しく描ける。
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger("price_history")

# 重複抑制: 同一価格が続く場合にこの日数未満では追記しない。
# 在庫切れ (price=None) の連続にも同じ間隔を効かせる (#5130)。
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
        price: 価格 (円)。int でない、または 0 以下なら「価格なしの観測」として
            扱い、``availability`` があるときだけ ``price: null`` の行を残す (#5130)。
        availability: 在庫メッセージ文字列。無ければ null で記録する。
        ts: 観測時刻。省略時は呼び出し時点の UTC now。

    Returns:
        追記したら True。スキップ (根拠のない欠測 / 重複抑制) や例外時は False。
        価格履歴はベストエフォートであり、例外はここで catch して呼び出し元
        (フェッチ本体) を絶対に落とさない。
    """
    try:
        if not asin:
            return False

        has_price = isinstance(price, int) and not isinstance(price, bool) and price > 0
        # 価格が取れない観測は、在庫メッセージという**根拠がある場合だけ**記録する
        # (#5130)。メッセージも無い欠測は API エラーと区別できず、記録すると
        # 「在庫切れだった」という主張を捏造することになるので従来どおり捨てる。
        avail_text = availability.strip() if isinstance(availability, str) else ""
        if not has_price and not avail_text:
            return False

        now = ts if ts is not None else datetime.now(timezone.utc)
        path = os.path.join(price_history_dir, f"{asin.upper()}.jsonl")

        last = _read_last_line_for_source(path, source) if os.path.exists(path) else None
        # 状態が変わったら経過日数によらず即記録する。同じ状態が続く間だけ間引く。
        # 価格行どうしは価格の一致、価格なし行どうしは在庫メッセージの一致で見る
        # (「在庫切れ」→「お取り扱いできません」は状態変化なので残す)。
        if last is not None:
            last_price = last.get("price")
            last_has_price = (isinstance(last_price, int) and not isinstance(last_price, bool)
                              and last_price > 0)
            if has_price and last_has_price:
                unchanged = last_price == price
            elif not has_price and not last_has_price:
                last_avail = last.get("availability")
                unchanged = (last_avail.strip() if isinstance(last_avail, str) else "") == avail_text
            else:
                unchanged = False  # 在庫切れ ⇄ 価格あり の遷移
            if unchanged:
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
            "price": price if has_price else None,
            "availability": avail_text or None,
        }
        os.makedirs(price_history_dir, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return True
    except Exception as e:  # ベストエフォート: フェッチ本体を絶対に止めない
        logger.warning(f"price_history: append_price_point failed for {asin}: {e}")
        return False
