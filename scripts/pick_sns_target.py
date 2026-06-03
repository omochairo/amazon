#!/usr/bin/env python3
"""SNS 配信対象 ASIN を 1 件選んで stdout に出す (Issue #1420 path α)。

data/articles/ から最新日付の未投稿 ASIN を選び、ASIN を 1 行で stdout に印字。
何も無い場合は exit 1 + stderr に理由出力 (workflow 側で skip 判定可)。

state file:  data/sns_published.json
    {"published": ["B0DBTLH8ZM", "B0XXXXXXXX", ...], "updated": "2026-06-03T..."}

呼び出し例:
    python scripts/pick_sns_target.py                       # 選定のみ (state 未更新)
    python scripts/pick_sns_target.py --mark B0DBTLH8ZM     # state に追記

workflow では先に「選定 → notify_buffer.py 成功 → --mark で commit」の順で
失敗時に同じ ASIN を再試行できる作りにする。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ARTICLES_DIR = REPO_ROOT / "data" / "articles"
STATE_FILE = REPO_ROOT / "data" / "sns_published.json"
ASIN_RE = re.compile(r"-([A-Z0-9]{10})\.(?:quality\.)?json$")


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"published": [], "updated": None}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or not isinstance(data.get("published"), list):
            print(f"[warn] {STATE_FILE.name} malformed, resetting", file=sys.stderr)
            return {"published": [], "updated": None}
        return data
    except json.JSONDecodeError as e:
        print(f"[warn] {STATE_FILE.name} JSON error: {e}, resetting", file=sys.stderr)
        return {"published": [], "updated": None}


def save_state(state: dict) -> None:
    state["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def list_articles_desc() -> list[tuple[str, str]]:
    """(asin, filename) を新しい順で返す。同一 ASIN の duplicate は最新側を残す。"""
    seen: dict[str, str] = {}
    for path in ARTICLES_DIR.glob("*.json"):
        m = ASIN_RE.search(path.name)
        if not m:
            continue
        asin = m.group(1)
        # filename prefix が日付なので文字列比較で降順 OK
        if asin not in seen or path.name > seen[asin]:
            seen[asin] = path.name
    return sorted(seen.items(), key=lambda kv: kv[1], reverse=True)


def pick_next(published: set[str]) -> str | None:
    for asin, _name in list_articles_desc():
        if asin not in published:
            return asin
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mark", metavar="ASIN", help="この ASIN を published に追記して終了")
    parser.add_argument("--limit", type=int, default=int(os.environ.get("SNS_HISTORY_LIMIT", "500")),
                        help="published 履歴の保持上限 (default 500)")
    args = parser.parse_args()

    state = load_state()
    published = set(state["published"])

    if args.mark:
        asin = args.mark.strip().upper()
        if asin in published:
            print(f"[skip] {asin} already in published", file=sys.stderr)
            return 0
        state["published"].append(asin)
        # 上限を超えたら古い側 (先頭) を削る
        if len(state["published"]) > args.limit:
            state["published"] = state["published"][-args.limit:]
        save_state(state)
        print(f"[mark] {asin} added (total={len(state['published'])})", file=sys.stderr)
        return 0

    asin = pick_next(published)
    if asin is None:
        print("[skip] no unpublished article found", file=sys.stderr)
        return 1
    print(asin)
    return 0


if __name__ == "__main__":
    sys.exit(main())
