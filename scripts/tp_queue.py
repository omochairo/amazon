"""tp_queue.py — 第三者ソースの収集待ち列 (候補供給 P2, 仕様 navi-candidate-supply §4.5)。

商品は 1 日 2 回 (09:00 / 18:00 JST) 取り込まれるが、出典の収集 (34-third-party-sources) は 06:30 の
1 回で、その時点の窓 (amazon.json + ranking_pool) だけを見る。09:00 に入って 18:00 に窓から落ちた商品は
収集の対象にならない。01-fetch-products の各回で「初めて入った ASIN」をこの列に足し、34 は窓から
落ちた後もこの列から拾う。拾い終えた・除外になった ASIN は 34 が列から外す。

列の置き場: data/raw/per_asin/_tp_queue.json  {"asins": {"<ASIN>": "<入った日 YYYY-MM-DD>"}}
(per_asin 直下のファイルなので glob("*/third_party_sources.json") には掛からない。_tavily_usage.json と同じ)

使い方 (01-fetch-products):
  python scripts/tp_queue.py snapshot --out "$RUNNER_TEMP/window_before.json"   # 取り込みの前
  python scripts/tp_queue.py add --before "$RUNNER_TEMP/window_before.json"     # 取り込みの後
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import pathlib
import re

logger = logging.getLogger("tp_queue")

QUEUE_PATH = pathlib.Path("data/raw/per_asin/_tp_queue.json")
PER_ASIN_DIR = pathlib.Path("data/raw/per_asin")
EXPIRE_DAYS = 14  # 拾えないまま残り続けない (34 の枠が足りない日が続いたとき)
_ASIN_RE = re.compile(r"^B0[A-Z0-9]{8}$")
WINDOW_FILES = ("data/raw/ranking_pool.json", "data/raw/first_party_pool.json")


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return None


def window_asins(root: str = ".") -> set[str]:
    """今の窓の ASIN (amazon.json の items + ranking_pool + first_party_pool)。"""
    out: set[str] = set()
    raw = _load(pathlib.Path(root) / "data/raw/amazon.json")
    if isinstance(raw, dict):
        for i in raw.get("items") or []:
            a = i.get("asin") if isinstance(i, dict) else None
            if isinstance(a, str) and _ASIN_RE.match(a):
                out.add(a)
    for f in WINDOW_FILES:
        d = _load(pathlib.Path(root) / f)
        if isinstance(d, dict):
            out.update(a for a in (d.get("asins") or []) if isinstance(a, str) and _ASIN_RE.match(a))
    return out


def load(path=QUEUE_PATH) -> dict[str, str]:
    d = _load(path)
    a = d.get("asins") if isinstance(d, dict) else None
    return {k: v for k, v in a.items() if _ASIN_RE.match(k) and isinstance(v, str)} if isinstance(a, dict) else {}


def save(queue: dict[str, str], path=QUEUE_PATH) -> None:
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"asins": dict(sorted(queue.items()))}, ensure_ascii=False, indent=1) + "\n",
                   encoding="utf-8")
    os.replace(tmp, p)


def expire(queue: dict[str, str], today: dt.date) -> dict[str, str]:
    out = {}
    for a, d in queue.items():
        try:
            age = (today - dt.date.fromisoformat(d[:10])).days
        except ValueError:
            continue
        if age < EXPIRE_DAYS:
            out[a] = d
    return out


def new_asins(before: set[str], now: set[str], per_asin: pathlib.Path = PER_ASIN_DIR) -> list[str]:
    """今回の取り込みで初めて窓に入った ASIN のうち、出典をまだ集めていないもの。"""
    return sorted(a for a in now - before if not (per_asin / a / "third_party_sources.json").exists())


def add(before: set[str], now: set[str], today: dt.date, path=QUEUE_PATH,
        per_asin: pathlib.Path = PER_ASIN_DIR) -> list[str]:
    q = expire(load(path), today)
    added = [a for a in new_asins(before, now, per_asin) if a not in q]
    for a in added:
        q[a] = today.isoformat()
    save(q, path)
    return added


def settle(path, keep: set[str], today: dt.date) -> int:
    """列のうち keep (まだ拾えていない・失敗して持ち越す ASIN) だけ残す。外した件数を返す。"""
    q = expire(load(path), today)
    left = {a: d for a, d in q.items() if a in keep}
    removed = len(load(path)) - len(left)
    if left != load(path):
        save(left, path)
    return removed


def _cli() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snapshot")
    s.add_argument("--out", required=True)
    a = sub.add_parser("add")
    a.add_argument("--before", required=True)
    args = ap.parse_args()
    if args.cmd == "snapshot":
        pathlib.Path(args.out).write_text(json.dumps(sorted(window_asins())), encoding="utf-8")
        return 0
    before = _load(args.before)
    if not isinstance(before, list):
        logger.warning("取り込み前の窓が読めない — 列に足さない")
        return 0
    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).date()
    added = add(set(before), window_asins(), today)
    logger.info("収集待ち列: 新しく入った %d 件を足した (列は %d 件)", len(added), len(load()))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
