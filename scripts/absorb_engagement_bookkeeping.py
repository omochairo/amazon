#!/usr/bin/env python3
"""未マージの publish PR が持つ配信済みマーカーを、手元の queue に取り込む。

Issue #4765 (元 #4469):
  30-sns-engagement.yml は「配信した」という事実 (published_at / post_id) を
  PR 経由でしか main に書き戻せない (main の branch protection で直接 push 不可)。
  この PR が 1 回でもマージされないと、main 上では当該 row が published_at=null の
  ままなので、**次のスロットが同じ本文を再選定して本番アカウントへ二重投稿する**。

  実例: 2026-08-08 の PR #4715 が auto-merge されず滞留 → eng-th-2026-08-06-005 が
  Threads へ 2 回 (02:30 / 06:55)、eng-x-2026-08-06-003 が X+Bluesky へ 2 回
  (08-08 02:29 / 08-09 02:36) 投稿された。滞留 PR は後続の再投稿が同じ行を書き換えた
  ことで dirty 化し、恒久的にマージ不能になる。

  そこで publish 前に、open な sns-engagement-publish/* ブランチが持つ配信済み
  マーカーを取り込んでから row を選定する。PR がマージされなくても次の run が
  bookkeeping を引き継ぐので、重複投稿が原理的に起きない (滞留 PR は新しい PR に
  吸収されて close できる)。

取り込み規則:
  - id 一致で照合。source 側に published_at があり、target 側が未設定の row だけ更新。
  - 更新するキーは published_at / post_id / bluesky_post_id のうち source にあるもの。
  - target 側に既に published_at がある row は絶対に上書きしない
    (滞留 PR の古い post_id で main の新しい値を潰さないため)。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 取り込む「配信済み」を表すキー。published_at 以外は source にあるものだけ写す。
MARKER_KEYS = ("published_at", "post_id", "bluesky_post_id")


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as e:
                print(f"::warning::Skipping malformed line in {path.name}: {e}", file=sys.stderr)
    return out


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    tmp.replace(path)


def absorb(target_rows: list[dict], source_rows: list[dict]) -> tuple[list[dict], list[str]]:
    """source の配信済みマーカーを target へ取り込み、(rows, 取り込んだ id) を返す。

    target_rows は in-place で更新する (呼び出し側の list と同一オブジェクト)。
    """
    by_id = {r.get("id"): r for r in target_rows if r.get("id")}
    absorbed: list[str] = []
    for src in source_rows:
        rid = src.get("id")
        if not rid or not src.get("published_at"):
            continue
        dst = by_id.get(rid)
        if dst is None:
            # target に無い row (削除済み等) は取り込まない。
            continue
        if dst.get("published_at"):
            # 既に main 側が配信済み。古い値で上書きしない。
            continue
        for key in MARKER_KEYS:
            if key in src:
                dst[key] = src[key]
        absorbed.append(rid)
    return target_rows, absorbed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target", required=True, type=Path,
                    help="更新対象の queue jsonl (作業ツリー側)")
    ap.add_argument("--source", action="append", default=[], type=Path,
                    help="滞留ブランチ側の同名 jsonl。複数指定可")
    ap.add_argument("--dry-run", action="store_true",
                    help="書き込まずに取り込み予定だけ出力")
    args = ap.parse_args()

    if not args.target.exists():
        print(f"::warning::target not found: {args.target}")
        return 0

    rows = _load_jsonl(args.target)
    all_absorbed: list[str] = []
    for src_path in args.source:
        if not src_path.exists():
            print(f"::warning::source not found, skipping: {src_path}")
            continue
        rows, absorbed = absorb(rows, _load_jsonl(src_path))
        if absorbed:
            print(f"{src_path}: absorbed {len(absorbed)}: {', '.join(absorbed)}")
        all_absorbed.extend(absorbed)

    if not all_absorbed:
        print(f"{args.target.name}: nothing to absorb.")
        return 0

    if args.dry_run:
        print(f"{args.target.name}: would absorb {len(all_absorbed)} row(s) (dry-run).")
        return 0

    _write_jsonl(args.target, rows)
    # cp932 な Windows ローカルでも落ちないよう、標準出力は ASCII に留める。
    print(f"::notice::{args.target.name}: absorbed {len(all_absorbed)} published marker(s) "
          f"from stale publish PR branches (duplicate post avoided)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
