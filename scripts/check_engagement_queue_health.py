"""check_engagement_queue_health.py

#4791: engagement queue の健全性を **harm ベース**で見る。

なぜ作り直すか (旧: 30-sns-engagement.yml のインライン heredoc):
  旧実装は在庫の生数を閾値にしていた。

    news  < 2  -> 直近 30 run 中 **28 run** で発火
    daily < 5  -> 約半分の run で発火

  news は「当日 2 件生成・当日 2 件消費」の JIT レーンで、在庫 0 が定常状態。
  daily は 7 件ずつ補充して 3 件/日消費する鋸歯で、下半分に閾値が置かれている。
  どちらも閾値が分布の中央付近にあり、**何も選別していない**。

  実害はこれで見逃された: 2026-08-08 に x の daily 投稿が 0/3 になったとき
  (Buffer が重複として拒否・run は continue-on-error で緑)、鳴っていたのは
  平常時と同じ `news queue low (0<2)` で、しかも daily の在庫は 5 件あり
  queue は原因ですらなかった。

較正の方針:
  - news: 在庫ではなく**補充レーンが動いているか**を見る。直近
    NEWS_REFILL_STALE_HOURS 以内に 1 件も created されていなければ
    33-jules-engagement-news.yml が止まっている。実測では 12 日連続で
    2 件/日 created なので平常時は鳴らない。
  - daily: 同じく在庫ではなく補充レーンの健全性を見る。在庫 0 は補充が届く
    直前の一瞬でも起きるので (replay 実測)、それ自体では鳴らさない。

  スロットを 1 つ落としたという実害は notify_engagement.py が穴開け時に出す
  ::warning:: が拾う (構造上 false positive ゼロ)。こちらは「補充レーンが
  死んでいないか」だけを見る網で、役割を重複させない。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

# daily の「在庫 0」は鳴らさない。replay (2026-07-28〜08-09 の 52 サンプル) で、
# 在庫 0 は **補充が届く直前の一瞬**でも起きており、そのとき実際にスロットは
# 落ちていなかった (07-28 の threads は結局 3 件配信済み)。
# スロットを落としたという実害は notify_engagement.py が穴開け時に出す
# ::warning:: が確実に拾う (構造上 false positive ゼロ) ので、ここで在庫を
# 見張るのは重複であり、鳴りっぱなし側に倒れるだけ。
#
# 補充レーンが完全に死んだ場合は、下の DAILY_REFILL_STALE_HOURS で拾う。
# 実測の daily 補充は 7 件ずつ 2〜4 日おきなので、余裕を取って 5 日に置く。
DAILY_REFILL_STALE_HOURS = 120
# news の補充が途絶えたとみなす時間。実測の補充は毎日 06:00 JST の 2 件で、
# 定常時の経過は常に 24h 未満。1 日飛ぶと news スロット 2 枠が落ちる実害なので、
# 24h の上に余裕を取りつつ 1 日飛べば鳴る 36h に置く。
NEWS_REFILL_STALE_HOURS = 36

CHANNELS: Sequence[Tuple[str, str]] = (
    ("X", "data/engagement_queue_x.jsonl"),
    ("Threads", "data/engagement_queue_threads.jsonl"),
)


def _parse_ts(value: Any) -> Optional[dt.datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        d = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def load_rows(path: pathlib.Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    out: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def latest_created(rows: Sequence[Dict[str, Any]], category: str) -> Optional[dt.datetime]:
    stamps = [_parse_ts(r.get("created_at")) for r in rows if r.get("category") == category]
    real = [s for s in stamps if s is not None]
    return max(real) if real else None


def assess(rows: Sequence[Dict[str, Any]], now: dt.datetime) -> Dict[str, Any]:
    """1 チャネル分の状態を返す。alerts は「手を打つべき」事象だけ。"""
    pending = [r for r in rows if not r.get("published_at")]
    by_cat: Dict[str, int] = {"news": 0, "daily": 0, "other": 0}
    for r in pending:
        c = r.get("category") or "other"
        by_cat[c if c in by_cat else "other"] += 1

    # cp932 な Windows ローカルでも落ちないよう、印字する文字列は ASCII に留める
    # (absorb_sns_published.py / pick_sns_target.py と同方針)。
    alerts: List[str] = []
    ages: Dict[str, Optional[float]] = {}
    for cat, limit, lane in (("news", NEWS_REFILL_STALE_HOURS, "33-jules-engagement-news.yml"),
                             ("daily", DAILY_REFILL_STALE_HOURS, "29-jules-engagement-refill.yml")):
        last = latest_created(rows, cat)
        if last is None:
            ages[cat] = None
            alerts.append(f"no {cat} rows have ever been generated - check {lane}")
            continue
        age_h = (now - last).total_seconds() / 3600.0
        ages[cat] = age_h
        if age_h > limit:
            alerts.append(
                "{} refill stalled for {:.0f}h (>{}h) - {} may be down".format(
                    cat, age_h, limit, lane))

    return {"pending_total": len(pending), "by_cat": by_cat,
            "news_age_hours": ages["news"], "daily_age_hours": ages["daily"],
            "alerts": alerts}


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--now", default=None, help="判定基準時刻 (ISO)。replay 検証用")
    p.add_argument("--channel", action="append", default=[], metavar="LABEL=PATH",
                   help="既定の 2 チャネルを差し替える (テスト用)")
    args = p.parse_args(argv)

    now = _parse_ts(args.now) or dt.datetime.now(dt.timezone.utc)
    channels: List[Tuple[str, str]] = []
    for raw in args.channel:
        label, _, path = raw.partition("=")
        channels.append((label, path))
    if not channels:
        channels = list(CHANNELS)

    for label, path in channels:
        rows = load_rows(pathlib.Path(path))
        if not rows:
            print(f"::warning::{label}: queue file missing or empty ({path})")
            continue
        st = assess(rows, now)
        def _age(key):
            v = st[key]
            return "-" if v is None else f"{v:.0f}h"
        print("{} pending - total={} news={} daily={} other={} "
              "(last created: news {} ago, daily {} ago)".format(
                  label, st["pending_total"], st["by_cat"]["news"],
                  st["by_cat"]["daily"], st["by_cat"]["other"],
                  _age("news_age_hours"), _age("daily_age_hours")))
        for a in st["alerts"]:
            print(f"::warning::{label} {a}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
