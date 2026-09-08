"""ASIN 出自台帳 (data/analytics/asin_origin.jsonl) への append 専用スクリプト。

#4964 観察項目3 (index 受理率の群比較) の材料として、ASIN が
demand / supply-random / ranking-sniper / rewrite-queue のどのプールから
選ばれたかを append-only の JSONL に記録する。**受理率などの比率計算はしない
(状態と事実だけを記録する)**。

呼び出し元:
  - .github/workflows/03-invoke-jules.yml の ASIN 選定ステップ (lock 取得後)

行のスキーマ (キー名固定):
  {"ts": ISO8601, "run_id": str, "workflow": str, "asin": str,
   "pool": "demand"|"supply-random"|"ranking-sniper"|"rewrite-queue",
   "source_keyword": str|None}
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys

POOLS = {"demand", "supply-random", "ranking-sniper", "rewrite-queue"}


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_record(*, ts: str, run_id: str, workflow: str, asin: str,
                 pool: str, source_keyword: str | None) -> dict:
    """1 行分のレコードを組み立てる。pool が不正なら ValueError。"""
    if pool not in POOLS:
        raise ValueError(f"pool must be one of {sorted(POOLS)}, got {pool!r}")
    if not asin:
        raise ValueError("asin must be non-empty")
    return {
        "ts": ts,
        "run_id": run_id,
        "workflow": workflow,
        "asin": asin,
        "pool": pool,
        "source_keyword": source_keyword or None,
    }


def record_key(record: dict) -> tuple:
    return (record.get("run_id"), record.get("asin"))


def read_existing_keys(path: str) -> set:
    """既存 jsonl から (run_id, asin) の集合を読む。ファイル無し/壊れた行は無視。"""
    keys = set()
    if not os.path.exists(path):
        return keys
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            keys.add((row.get("run_id"), row.get("asin")))
    return keys


def dedupe_new(existing_keys: set, records: list[dict]) -> list[dict]:
    """existing_keys および records 内部での重複 (run_id, asin) を落とす。

    先勝ち (最初に出現したものを残す)。
    """
    seen = set(existing_keys)
    out = []
    for rec in records:
        key = record_key(rec)
        if key in seen:
            continue
        seen.add(key)
        out.append(rec)
    return out


def append_records(path: str, records: list[dict]) -> int:
    """新規レコードのみ jsonl に append する。書いた件数を返す。"""
    new_records = dedupe_new(read_existing_keys(path), records)
    if not new_records:
        return 0
    out_dir = os.path.dirname(path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for rec in new_records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return len(new_records)


def _read_batch_input(path: str) -> list[dict]:
    """--input からレコード候補 (asin/pool/source_keyword) を読む。"""
    text = sys.stdin.read() if path == "-" else open(path, encoding="utf-8").read()
    items = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        items.append(json.loads(line))
    return items


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="data/analytics/asin_origin.jsonl")
    parser.add_argument("--run-id", default=os.environ.get("GITHUB_RUN_ID", "local"))
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--asin", help="単発モード: 記録する ASIN 1 件")
    parser.add_argument("--pool", choices=sorted(POOLS), help="単発モード: プール")
    parser.add_argument("--source-keyword", default=None, help="単発モード: 検索語 (demand/supply-random のみ)")
    parser.add_argument("--input", help="バッチモード: {asin,pool,source_keyword} の JSONL パス ('-' で stdin)")
    args = parser.parse_args()

    ts = now_iso()

    if args.input:
        candidates = _read_batch_input(args.input)
        records = [
            make_record(ts=ts, run_id=args.run_id, workflow=args.workflow,
                        asin=c["asin"], pool=c["pool"],
                        source_keyword=c.get("source_keyword"))
            for c in candidates
        ]
    elif args.asin:
        if not args.pool:
            parser.error("--asin には --pool が必須")
        records = [make_record(ts=ts, run_id=args.run_id, workflow=args.workflow,
                                asin=args.asin, pool=args.pool,
                                source_keyword=args.source_keyword)]
    else:
        parser.error("--asin または --input のどちらかが必須")

    written = append_records(args.out, records)
    print(f"asin_origin: {written} record(s) appended to {args.out} "
          f"({len(records) - written} duplicate(s) skipped)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
