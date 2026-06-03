"""
Issue #1526 — 過去 48h に配信済の news draft を集計し、Jules prompt 注入用の
「重複避けるべき topic 一覧」を生成する。

source: data/engagement_queue_{x,threads}.jsonl の中で category == "news"
filter: created_at (or published_at) が now - 48h 以内

出力:
  {
    "window_hours": 48,
    "since": "...",
    "recent_news_topics": [
      {"topic": "台風", "count": 3, "last_titles": ["...", "..."], "last_created_at": "..."},
      ...
    ]
  }

Jules prompt にこの dict を注入して「同じ topic + 同じ角度の投稿は禁止」と指示する。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

JST = timezone(timedelta(hours=9))

# subcategory に乗ってる topic word を集計対象とする。
# Jules が draft 生成時に subcategory に台風/休校/インフル などを書く前提。
QUEUE_FILES = [
    "data/engagement_queue_x.jsonl",
    "data/engagement_queue_threads.jsonl",
]


def parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        # JSONL に入る ISO 8601 は "+09:00" 含む / Z 含む 両方ありうる
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def scan_recent(window_hours: int, now: datetime, base_dir: Path) -> list[dict[str, Any]]:
    cutoff = now - timedelta(hours=window_hours)
    rows: list[dict[str, Any]] = []
    for qfile in QUEUE_FILES:
        p = base_dir / qfile
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("category") != "news":
                continue
            # created_at 優先、無ければ published_at
            ts = parse_dt(row.get("created_at")) or parse_dt(row.get("published_at"))
            if ts is None:
                continue
            if ts < cutoff:
                continue
            rows.append(row)
    return rows


def summarize(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """subcategory (topic word) 別に集計。各 topic の最近タイトル最大 3 件を保持。"""
    counter: Counter[str] = Counter()
    titles: dict[str, list[tuple[str, str]]] = defaultdict(list)  # topic -> [(created_at, title)]
    for r in rows:
        topic = (r.get("subcategory") or "").strip()
        if not topic:
            continue
        counter[topic] += 1
        title_snippet = (r.get("text") or "")[:80]
        titles[topic].append((r.get("created_at") or "", title_snippet))

    out: list[dict[str, Any]] = []
    for topic, count in counter.most_common():
        sorted_titles = sorted(titles[topic], reverse=True)[:3]
        out.append({
            "topic": topic,
            "count": count,
            "last_titles": [t for _, t in sorted_titles],
            "last_created_at": sorted_titles[0][0] if sorted_titles else None,
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window-hours", type=int, default=48)
    ap.add_argument("--base-dir", default=".", help="amazon-clone repo root")
    ap.add_argument("--out", default=None, help="出力先 JSON。省略時は stdout")
    args = ap.parse_args()

    now = datetime.now(JST)
    base_dir = Path(args.base_dir)
    rows = scan_recent(args.window_hours, now, base_dir)
    topics = summarize(rows)

    payload = {
        "window_hours": args.window_hours,
        "since": (now - timedelta(hours=args.window_hours)).isoformat(),
        "now": now.isoformat(),
        "total_recent_news_drafts": len(rows),
        "recent_news_topics": topics,
    }

    out_str = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(out_str, encoding="utf-8")
        print(f"[OK] wrote {args.out}  (topics={len(topics)} drafts={len(rows)})")
    else:
        print(out_str)
    return 0


if __name__ == "__main__":
    sys.exit(main())
