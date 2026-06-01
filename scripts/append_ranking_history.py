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
"""
from __future__ import annotations

import argparse
import datetime
import json
import logging
import pathlib
import sys
from zoneinfo import ZoneInfo

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
    payload = json.loads(weekly_path.read_text(encoding="utf-8"))
    return payload.get("items", [])


def _load_history(history_path: pathlib.Path) -> dict:
    if not history_path.exists():
        return {"updated_at": None, "history": {}}
    return json.loads(history_path.read_text(encoding="utf-8"))


def _upsert_today(entries: list[dict], today_mm_dd: str, rank: int) -> None:
    """Update today's rank if present, else append. Rotate to MAX_HISTORY."""
    for e in entries:
        if e.get("d") == today_mm_dd:
            e["r"] = rank
            return
    entries.append({"d": today_mm_dd, "r": rank})
    while len(entries) > MAX_HISTORY:
        entries.pop(0)


def append_history(
    weekly_path: pathlib.Path,
    history_path: pathlib.Path,
    *,
    today_mm_dd: str | None = None,
    now_iso: str | None = None,
) -> dict:
    """Run one append cycle. Returns stats dict for logging/tests."""
    if today_mm_dd is None or now_iso is None:
        today_mm_dd, now_iso = _today_jst()

    items = _load_weekly(weekly_path)
    state = _load_history(history_path)
    history = state.setdefault("history", {})

    matched, skipped = 0, 0
    for it in items:
        asin = it.get("matched_asin")
        rank = it.get("rank")
        if not asin or rank is None:
            skipped += 1
            continue
        entry = history.setdefault(asin, {"name": "", "history": []})
        name = it.get("title") or entry.get("name") or ""
        entry["name"] = name
        _upsert_today(entry["history"], today_mm_dd, int(rank))
        matched += 1

    state["updated_at"] = now_iso
    history_path.parent.mkdir(parents=True, exist_ok=True)
    history_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return {
        "matched": matched,
        "skipped": skipped,
        "total_items": len(items),
        "tracked_asins": len(history),
        "today": today_mm_dd,
    }


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--weekly",
        default="hugo/data/ranking/weekly.json",
        help="Source weekly.json with rematched matched_asin",
    )
    parser.add_argument(
        "--history",
        default="hugo/data/ranking/history.json",
        help="Target history.json (created if missing)",
    )
    args = parser.parse_args()

    stats = append_history(pathlib.Path(args.weekly), pathlib.Path(args.history))
    logger.info(
        "ranking-history append: matched=%d skipped=%d total=%d tracked=%d today=%s",
        stats["matched"],
        stats["skipped"],
        stats["total_items"],
        stats["tracked_asins"],
        stats["today"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
