#!/usr/bin/env python3
"""未マージの sns-publish PR が持つ配信済み ASIN を、手元の state に取り込む。

Issue #4765 の水平展開 (#4769 は engagement 側、本スクリプトは記事リンク側):
  20-sns-publish.yml も「配信した」という事実 (data/sns_published.json) を PR 経由で
  しか main に書き戻せない。この PR が 1 回でもマージされないと、main 上では当該
  ASIN が未配信のままなので、**次の cron が同じ ASIN を再選定して同じ記事リンクを
  X / Threads / Bluesky へ再投稿する**。

  engagement 側 (#4769) より危険な点が 2 つある:
    - X が X_POST_MODE=now (shareNow) なので Buffer のキューを介さず即座に重複が出る。
    - pick_sns_target.py が published を set で扱うため、重複しても
      sns_published.json には痕跡が残らない (= データからの遡及検出ができない)。

  そこで publish 前に、open な sns-publish/* ブランチが持つ配信済み ASIN を取り込む。
  PR がマージされなくても次の run が bookkeeping を引き継ぐので、再投稿が起きない。

取り込み規則:
  - target の published に無い ASIN だけを末尾に追加する (union)。
  - target 側の既存要素は順序ごと保持し、削除も並べ替えもしない。
  - 追加後に --limit を超えたら、pick_sns_target.py と同じく古い側 (先頭) を削る。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STATE = REPO_ROOT / "data" / "sns_published.json"


def _load_state(path: Path) -> dict:
    """pick_sns_target.load_state と同じ緩さで読む (壊れていたら空 state)。"""
    if not path.exists():
        return {"published": [], "updated": None}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        print(f"[warn] {path.name} JSON error: {e}, treating as empty", file=sys.stderr)
        return {"published": [], "updated": None}
    if not isinstance(data, dict) or not isinstance(data.get("published"), list):
        print(f"[warn] {path.name} malformed, treating as empty", file=sys.stderr)
        return {"published": [], "updated": None}
    return data


def _save_state(path: Path, state: dict) -> None:
    state["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def absorb(target: list[str], source: list[str], limit: int | None = None) -> tuple[list[str], list[str]]:
    """source にあって target に無い ASIN を末尾に足し、(published, 追加した ASIN) を返す。

    target の既存要素は順序ごと保持する (配信順の履歴なので並べ替えない)。
    """
    have = set(target)
    added: list[str] = []
    out = list(target)
    for asin in source:
        if not isinstance(asin, str):
            continue
        a = asin.strip().upper()
        if not a or a in have:
            continue
        out.append(a)
        have.add(a)
        added.append(a)
    if limit is not None and len(out) > limit:
        out = out[-limit:]
    return out, added


def absorb_channel_results(target: dict, source: dict, alive: set[str]) -> dict:
    """channel_results を union する (#4783)。target 側の記録を優先する。

    滞留 PR を取り込むときに published だけを union すると、その ASIN の
    「どのチャネルに出たか」が落ちて痕跡が消える。published と同じ寿命に
    揃えるため alive (取り込み後の published) に無い ASIN は捨てる。
    """
    out: dict = {}
    for src in (source, target):
        if not isinstance(src, dict):
            continue
        for asin, res in src.items():
            if isinstance(asin, str) and isinstance(res, dict) and asin in alive:
                out[asin] = res
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target", type=Path, default=DEFAULT_STATE,
                    help="更新対象の sns_published.json (作業ツリー側)")
    ap.add_argument("--source", action="append", default=[], type=Path,
                    help="滞留ブランチ側の同ファイル。複数指定可")
    ap.add_argument("--limit", type=int, default=int(os.environ.get("SNS_HISTORY_LIMIT", "500")),
                    help="published 履歴の保持上限 (pick_sns_target.py と同じ既定 500)")
    ap.add_argument("--dry-run", action="store_true", help="書き込まずに取り込み予定だけ出力")
    args = ap.parse_args()

    state = _load_state(args.target)
    published = state["published"]
    all_added: list[str] = []
    src_channels: list[dict] = []
    for src_path in args.source:
        if not src_path.exists():
            print(f"::warning::source not found, skipping: {src_path}")
            continue
        src_state = _load_state(src_path)
        published, added = absorb(published, src_state["published"], args.limit)
        src_channels.append(src_state.get("channel_results") or {})
        if added:
            print(f"{src_path}: absorbed {len(added)}: {', '.join(added)}")
        all_added.extend(added)

    if not all_added:
        print(f"{args.target.name}: nothing to absorb.")
        return 0
    if args.dry_run:
        print(f"{args.target.name}: would absorb {len(all_added)} ASIN(s) (dry-run).")
        return 0

    state["published"] = published
    alive = set(published)
    merged = state.get("channel_results") or {}
    for sc in src_channels:
        merged = absorb_channel_results(merged, sc, alive)
    if merged:
        state["channel_results"] = merged
    _save_state(args.target, state)
    # cp932 な Windows ローカルでも落ちないよう、標準出力は ASCII に留める。
    print(f"::notice::{args.target.name}: absorbed {len(all_added)} published ASIN(s) "
          f"from stale sns-publish PR branches (duplicate post avoided)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
