"""Issue #600: 楽天ランキング日次履歴アキュムレータ。

hugo/data/ranking/weekly.json (rematch 済) を読み、matched_asin がある item を
hugo/data/ranking/history.json に append する。同日 re-run は r を上書き。
90 日 (90 entries/ASIN) を超えたら古い順から pop。

設計は #600 著者コメントの「軽量データモデル」案そのまま:
{
  "updated_at": ISO8601,
  "history": {
    "<ASIN>": {"name": "...", "history": [{"d": "MM-DD", "r": <int>}, ...]}
  }
}

MM-DD は JST。年境界の前後関係は配列の挿入順 (= chronological) で表現する。

navi-brain#166 F8: ASIN が付かない商品も前回比を出せるよう、楽天の itemCode ごとの
履歴 by_item を同じ形で持ち、--movement に今日の順位・前回順位・連続日数を書く。
"""
from __future__ import annotations

import argparse
import datetime
import json
import logging
import pathlib
import sys
from zoneinfo import ZoneInfo

from ranking_dates import resolve_entry_dates

logger = logging.getLogger(__name__)
MAX_HISTORY = 90
JST = ZoneInfo("Asia/Tokyo")


def _today_jst() -> tuple[str, str]:
    """Returns (mm_dd, iso_with_tz) in JST."""
    now = datetime.datetime.now(JST)
    return now.strftime("%m-%d"), now.isoformat()


def _load_weekly(weekly_path: pathlib.Path) -> list[dict]:
    if not weekly_path.exists():
        raise FileNotFoundError(f"weekly.json not found: {weekly_path}")
    return json.loads(weekly_path.read_text(encoding="utf-8")).get("items", [])


def _load_history(history_path: pathlib.Path) -> dict:
    if not history_path.exists():
        return {"updated_at": None, "history": {}}
    return json.loads(history_path.read_text(encoding="utf-8"))


def _upsert_today(entries: list[dict], today_mm_dd: str, rank: int) -> None:
    """Update today's rank if present, else append. Rotate to MAX_HISTORY.

    同日 re-run かどうかは **末尾の 1 件だけ** で判定する。entries は挿入順
    (= chronological) で、今日の entry があるなら必ず末尾にあるため。
    全件を MM-DD で探すと、ランキングから外れて戻ってきた ASIN の
    「1 年前の同じ MM-DD」の entry を今日の値で上書きしてしまい、
    今日の点が配列の途中に入って時系列が壊れる (rotation は append 時にしか
    走らないので、外れていた ASIN の古い entry は残り続ける)。
    """
    if entries and entries[-1].get("d") == today_mm_dd:
        entries[-1]["r"] = rank
        return
    entries.append({"d": today_mm_dd, "r": rank})
    while len(entries) > MAX_HISTORY:
        entries.pop(0)


def _date_for_mm_dd(mm_dd: str, today: datetime.date) -> datetime.date:
    """今日の MM-DD を日付にする (今日の点にだけ使う。過去の点は resolve_entry_dates)。"""
    return resolve_entry_dates([{"d": mm_dd}], today)[0] or today


def _movement_entry(entries: list[dict], rank: int, today_mm_dd: str,
                    today: datetime.date, first_run: bool) -> dict:
    dates = resolve_entry_dates(entries, today)
    prior = [(d, item) for d, item in zip(dates, entries) if d is not None and d < today]
    latest = max(prior, key=lambda pair: pair[0]) if prior else None
    prev_rank = int(latest[1]["r"]) if latest else None
    prev_d = latest[1]["d"] if latest else None
    recent = [d for d, _ in prior if 0 < (today - d).days <= 30]
    streak = 1
    dated = {d for d in dates if d is not None}
    while today - datetime.timedelta(days=streak) in dated:
        streak += 1
    return {
        "rank": rank,
        "prev_rank": prev_rank,
        "prev_d": prev_d,
        "delta": prev_rank - rank if prev_rank is not None else None,
        "is_new": False if first_run else not recent,
        "streak_days": streak,
    }


def append_history(weekly_path: pathlib.Path, history_path: pathlib.Path, *,
                   movement_path: pathlib.Path | None = None,
                   today_mm_dd: str | None = None, now_iso: str | None = None) -> dict:
    """Append one snapshot and write movement data; retain legacy stats keys."""
    if today_mm_dd is None or now_iso is None:
        actual_today, actual_iso = _today_jst()
        today_mm_dd = today_mm_dd or actual_today
        now_iso = now_iso or actual_iso
    items = _load_weekly(weekly_path)
    state = _load_history(history_path)
    history = state.setdefault("history", {})
    by_item = state.setdefault("by_item", {})
    asins, skipped = 0, 0
    movements = {}
    today = _date_for_mm_dd(today_mm_dd, datetime.date.fromisoformat(now_iso[:10]))
    # 初回 (by_item に今日より前の点がまだ無い) は前回が分からないので、全部を「初登場」にしない。
    # 同じ日に 2 回走ったときも、前の日の点が無ければ初回扱いのまま。
    first_run = not any(
        d is not None and d < today
        for entry in by_item.values() if isinstance(entry, dict)
        for d in resolve_entry_dates(entry.get("history") or [], today)
    )
    for it in items:
        asin, rank, code = it.get("matched_asin"), it.get("rank"), it.get("itemCode")
        if rank is None:
            skipped += 1
            continue
        rank = int(rank)
        if asin:
            entry = history.setdefault(asin, {"name": "", "history": []})
            entry["name"] = it.get("title") or entry.get("name") or ""
            _upsert_today(entry["history"], today_mm_dd, rank)
            asins += 1
        else:
            skipped += 1
        if code:
            entry = by_item.setdefault(code, {"name": "", "history": []})
            entry["name"] = it.get("title") or entry.get("name") or ""
            _upsert_today(entry["history"], today_mm_dd, rank)
            movements[code] = _movement_entry(entry["history"], rank, today_mm_dd, today, first_run)
    state["updated_at"] = now_iso
    history_path.parent.mkdir(parents=True, exist_ok=True)
    history_path.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    if movement_path is not None:
        movement_path.parent.mkdir(parents=True, exist_ok=True)
        movement_path.write_text(json.dumps({"generated_at": now_iso, "today": today_mm_dd, "items": movements},
                                            ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return {"matched": asins, "skipped": skipped, "total_items": len(items),
            "tracked_asins": len(history), "today": today_mm_dd,
            "tracked_items": len(by_item), "movement_items": len(movements)}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--weekly", default="hugo/data/ranking/weekly.json")
    parser.add_argument("--history", default="hugo/data/ranking/history.json")
    parser.add_argument("--movement", default="hugo/data/ranking/movement.json")
    args = parser.parse_args()
    stats = append_history(pathlib.Path(args.weekly), pathlib.Path(args.history), movement_path=pathlib.Path(args.movement))
    logger.info("ranking-history append: matched=%d skipped=%d total=%d tracked=%d today=%s",
                stats["matched"], stats["skipped"], stats["total_items"], stats["tracked_asins"], stats["today"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
