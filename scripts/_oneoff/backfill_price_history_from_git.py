"""backfill_price_history_from_git.py (one-off, #2953 C案・訂正版)

price_history.append_price_point の導入 (fetch_amazon.write_per_asin_snapshot
への hook) は 2026-07-12 に入ったため、それ以前に per_asin/<ASIN>/amazon.json
へ書き込まれていた観測点は data/price_history/ に一切残っていない。

本スクリプトは git 履歴から 2026-07-01 以降に data/raw/per_asin を触った
コミットを古い順に辿り、各コミットで変更された */amazon.json の内容を
`git show <hash>:<path>` で読んで {asin, fetched_at, item.price,
item.availability} を抽出し、append_price_point へ時系列順に流すことで
data/price_history/ を事後生成するバックフィルである (重複抑制は
append_price_point 側がそのまま効く)。

Usage:
    cd amazon-clone && python scripts/_oneoff/backfill_price_history_from_git.py [--since 2026-07-01] [--dry-run]

Output:
    - data/price_history/<ASIN>.jsonl (実行時、--dry-run 指定時は書かない)
    - scripts/_oneoff/backfill_price_history_from_git.report.json (統計)
    - stdout に ASIN 数・総ポイント数を出力
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
from datetime import datetime, timezone

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))

import price_history  # noqa: E402

ROOT = SCRIPTS_DIR.parent  # amazon-clone/
PER_ASIN_REL = "data/raw/per_asin"
DEFAULT_SINCE = "2026-07-01"


def _run_git(args: list[str]) -> str:
    # Windows のデフォルト cp932 では git show の UTF-8 出力 (日本語商品名) が
    # decode 失敗するため encoding を明示する。
    res = subprocess.run(
        ["git", *args], cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=True)
    return res.stdout


def _list_commits(since: str) -> list[str]:
    """``since`` 以降に PER_ASIN_REL を触ったコミットを古い順に返す。"""
    out = _run_git([
        "log", "--format=%H", f"--since={since}", "--reverse", "--", PER_ASIN_REL,
    ])
    return [h.strip() for h in out.splitlines() if h.strip()]


def _commit_date(hash_: str) -> datetime:
    out = _run_git(["show", "-s", "--format=%cI", hash_]).strip()
    return datetime.fromisoformat(out)


def _touched_amazon_json_paths(hash_: str) -> list[str]:
    out = _run_git([
        "diff-tree", "-r", "--name-only", "--no-commit-id", hash_, "--", PER_ASIN_REL,
    ])
    return [p.strip() for p in out.splitlines() if p.strip().endswith("/amazon.json")]


def _show_file(hash_: str, path: str) -> str | None:
    try:
        res = subprocess.run(
            ["git", "show", f"{hash_}:{path}"], cwd=str(ROOT),
            capture_output=True, text=True,
            encoding="utf-8", errors="replace", check=True)
        return res.stdout
    except subprocess.CalledProcessError:
        return None


def _extract_asin_from_path(path: str) -> str:
    # data/raw/per_asin/<ASIN>/amazon.json
    parts = path.split("/")
    return parts[-2] if len(parts) >= 2 else ""


def _parse_snapshot(raw: str) -> dict | None:
    try:
        snap = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(snap, dict):
        return None
    return snap


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", default=DEFAULT_SINCE)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--price-history-dir", default=str(ROOT / "data" / "price_history"))
    args = parser.parse_args()

    price_history_dir = args.price_history_dir

    commits = _list_commits(args.since)
    print(f"commits touching {PER_ASIN_REL} since {args.since}: {len(commits)}")

    total_files_seen = 0
    total_points_appended = 0
    skipped_no_item = 0
    skipped_bad_json = 0
    skipped_bad_price = 0
    skipped_dedup = 0
    asins_seen: set[str] = set()
    asins_appended: set[str] = set()

    for i, hash_ in enumerate(commits, 1):
        commit_dt = None
        touched = _touched_amazon_json_paths(hash_)
        if not touched:
            continue
        for path in touched:
            total_files_seen += 1
            asin = _extract_asin_from_path(path)
            if not asin:
                continue
            asins_seen.add(asin)
            raw = _show_file(hash_, path)
            if raw is None:
                skipped_bad_json += 1
                continue
            snap = _parse_snapshot(raw)
            if snap is None:
                skipped_bad_json += 1
                continue
            item = snap.get("item") if isinstance(snap.get("item"), dict) else snap
            if not isinstance(item, dict):
                skipped_no_item += 1
                continue
            price = item.get("price")
            availability = item.get("availability")

            fetched_at_raw = snap.get("fetched_at")
            ts = None
            if isinstance(fetched_at_raw, str) and fetched_at_raw:
                try:
                    ts = datetime.fromisoformat(fetched_at_raw.replace("Z", "+00:00"))
                except ValueError:
                    ts = None
            if ts is None:
                if commit_dt is None:
                    commit_dt = _commit_date(hash_)
                ts = commit_dt

            if args.dry_run:
                if isinstance(price, int) and not isinstance(price, bool) and price > 0:
                    total_points_appended += 1
                    asins_appended.add(asin)
                else:
                    skipped_bad_price += 1
                continue

            appended = price_history.append_price_point(
                price_history_dir, asin, "amazon", price, availability, ts=ts)
            if appended:
                total_points_appended += 1
                asins_appended.add(asin)
            elif isinstance(price, int) and not isinstance(price, bool) and price > 0:
                skipped_dedup += 1  # 妥当な価格だが重複抑制でスキップされた
            else:
                skipped_bad_price += 1
        if i % 5 == 0 or i == len(commits):
            print(f"  processed {i}/{len(commits)} commits "
                  f"(files={total_files_seen}, appended={total_points_appended})")

    report = {
        "since": args.since,
        "dry_run": args.dry_run,
        "commits_scanned": len(commits),
        "amazon_json_files_seen": total_files_seen,
        "asins_seen": len(asins_seen),
        "asins_with_points_appended": len(asins_appended),
        "points_appended": total_points_appended,
        "skipped_bad_json": skipped_bad_json,
        "skipped_no_item": skipped_no_item,
        "skipped_bad_price": skipped_bad_price,
        "skipped_dedup": skipped_dedup,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    report_path = pathlib.Path(__file__).with_suffix(".report.json")
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")

    print(f"ASINs seen: {len(asins_seen)} / ASINs with points appended: {len(asins_appended)}")
    print(f"Points appended: {total_points_appended}")
    print(f"Skipped: bad_json={skipped_bad_json} no_item={skipped_no_item} "
          f"bad_price={skipped_bad_price} dedup={skipped_dedup}")
    print(f"report: {report_path}")
    if args.dry_run:
        print("(dry-run: no files written)")


if __name__ == "__main__":
    main()
