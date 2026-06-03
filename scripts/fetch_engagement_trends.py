"""
Issue #1526 — engagement news 用の trend 取得 & スコアリング。

毎日 05:30 JST cron で 7 source を fetch、NEGATIVE_BANK 除外 / POSITIVE_BANK_TIERS スコアリングし、
data/engagement_trends/YYYY-MM-DD.json に top 候補を出力する。

source 別の採用モード:
- trusted_full (asahi_edu): bank match なしでも候補入り (NEGATIVE 通過のみ必須)
- filter_required (NHK/Google News/Yahoo): POSITIVE bank match のみ候補入り
- raw_signal (google_trends_new): score なし、生語 list で別出力 (Jules が活用判断)
- variety_inject (dowjones_japanlife): 週 1 (火曜のみ) 1 件アクセント候補
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import feedparser  # type: ignore
import requests

from _engagement_bank import (  # type: ignore
    SOURCE_WEIGHT,
    contains_negative,
    freshness_weight,
    score_positive,
)


UA = "Mozilla/5.0 (compatible; omochairo-engagement-trend/1.0; +https://navi.omcha.jp/)"
JST = timezone(timedelta(hours=9))

SOURCES: dict[str, dict[str, str]] = {
    # trusted_full: bank match なしでも候補
    "asahi_edu": {
        "url": "https://www.asahi.com/rss/asahi/edu.rdf",
        "mode": "trusted_full",
    },
    # filter_required: POSITIVE bank match のみ
    "nhk_main": {
        "url": "https://www.nhk.or.jp/rss/news/cat0.xml",
        "mode": "filter_required",
    },
    "nhk_social": {
        "url": "https://www.nhk.or.jp/rss/news/cat1.xml",
        "mode": "filter_required",
    },
    "nhk_life": {
        "url": "https://www.nhk.or.jp/rss/news/cat6.xml",
        "mode": "filter_required",
    },
    "google_news_topic": {
        "url": "https://news.google.com/rss/topics/CAAqIQgKIhtDQkFTRGdvSUwyMHZNRE5mTTJRU0FtcGhLQUFQAQ?hl=ja&gl=JP&ceid=JP:ja",
        "mode": "filter_required",
    },
    "yahoo_domestic": {
        "url": "https://news.yahoo.co.jp/rss/categories/domestic.xml",
        "mode": "filter_required",
    },
    "yahoo_life": {
        "url": "https://news.yahoo.co.jp/rss/categories/life.xml",
        "mode": "filter_required",
    },
    # raw_signal: score なし、生語のまま (top 10)
    "google_trends_new": {
        "url": "https://trends.google.co.jp/trending/rss?geo=JP",
        "mode": "raw_signal",
    },
    # variety_inject: 週 1 (火曜) のみアクセント
    "dowjones_japanlife": {
        "url": "https://feeds.content.dowjones.io/public/rss/RSSJapanLife",
        "mode": "variety_inject",
    },
}


def fetch_feed(url: str, label: str) -> list[dict[str, Any]]:
    try:
        resp = requests.get(url, headers={"User-Agent": UA}, timeout=15, allow_redirects=True)
        resp.raise_for_status()
    except Exception as e:
        print(f"[WARN] {label} fetch failed: {e}", file=sys.stderr)
        return []
    feed = feedparser.parse(resp.content)
    entries: list[dict[str, Any]] = []
    for e in feed.entries:
        title = (e.get("title") or "").strip()
        if not title:
            continue
        published_parsed = e.get("published_parsed") or e.get("updated_parsed")
        published_dt = None
        if published_parsed:
            try:
                published_dt = datetime(*published_parsed[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                pass
        entries.append({
            "title": title,
            "summary": (e.get("summary") or e.get("description") or "").strip(),
            "link": e.get("link"),
            "published_at": published_dt.isoformat() if published_dt else None,
            "_published_dt": published_dt,
            "traffic": e.get("ht_approx_traffic") or e.get("approx_traffic"),
        })
    return entries


def candidate_for(source: str, mode: str, entry: dict[str, Any], now: datetime) -> dict[str, Any] | None:
    haystack = entry["title"] + " " + entry.get("summary", "")
    negatives = contains_negative(haystack)
    if negatives:
        return None  # NEGATIVE 含む → 即除外

    pos_score, matched_tiers = score_positive(haystack)

    # mode 別の採用判定
    if mode == "filter_required" and pos_score == 0:
        return None  # bank match 無し → drop
    # trusted_full は bank match 不要、score=0 でも採用
    # variety_inject も同上 (週 1 抽選で 1 件選ぶ)

    # freshness
    pub_dt: datetime | None = entry["_published_dt"]
    if pub_dt is None:
        age_h = 24.0  # 不明時は中央値 1.0 weight
    else:
        age_h = (now - pub_dt).total_seconds() / 3600.0
    fresh = freshness_weight(age_h)
    if fresh == 0.0:
        return None  # >72h 古すぎ

    src_w = SOURCE_WEIGHT.get(source, 1.0)
    # trusted_full / variety_inject で pos_score=0 のときは base 1.0 で源泉スコア
    base = pos_score if pos_score > 0 else 1.0
    final_score = round(base * src_w * fresh, 3)

    return {
        "source": source,
        "mode": mode,
        "title": entry["title"],
        "summary": entry.get("summary"),
        "link": entry.get("link"),
        "published_at": entry.get("published_at"),
        "age_hours": round(age_h, 2),
        "score_breakdown": {
            "positive": pos_score,
            "source_weight": src_w,
            "freshness": fresh,
            "final": final_score,
        },
        "matched_tiers": matched_tiers,
    }


def collect_candidates(now: datetime) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    raw_trends: list[dict[str, Any]] = []
    fetch_summary: dict[str, dict[str, int]] = {}

    for source, cfg in SOURCES.items():
        entries = fetch_feed(cfg["url"], source)
        mode = cfg["mode"]
        fetch_summary[source] = {"entries": len(entries), "kept": 0}

        if mode == "raw_signal":
            for e in entries[:10]:
                raw_trends.append({
                    "source": source,
                    "title": e["title"],
                    "traffic": e.get("traffic"),
                    "link": e.get("link"),
                })
            fetch_summary[source]["kept"] = len(raw_trends)
            continue

        for entry in entries:
            cand = candidate_for(source, mode, entry, now)
            if cand:
                candidates.append(cand)
                fetch_summary[source]["kept"] += 1
        time.sleep(0.3)  # rate-limit courtesy

    # dedup by title (after all sources collected)
    seen_titles: set[str] = set()
    unique: list[dict[str, Any]] = []
    for c in sorted(candidates, key=lambda x: -x["score_breakdown"]["final"]):
        if c["title"] in seen_titles:
            continue
        seen_titles.add(c["title"])
        unique.append(c)

    return {
        "candidates": unique,
        "raw_trends": raw_trends,
        "fetch_summary": fetch_summary,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default="data/engagement_trends")
    ap.add_argument("--top-n", type=int, default=10, help="出力する top 候補数")
    ap.add_argument("--date", default=None, help="出力日 (YYYY-MM-DD)、省略時は JST 今日")
    args = ap.parse_args()

    now = datetime.now(JST)
    today = args.date or now.strftime("%Y-%m-%d")
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{today}.json"

    result = collect_candidates(now)
    top_candidates = result["candidates"][: args.top_n]

    payload = {
        "fetched_at": now.isoformat(),
        "date_jst": today,
        "fetch_summary": result["fetch_summary"],
        "top_candidates": top_candidates,
        "raw_trends": result["raw_trends"],
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[OK] wrote {out_path}")
    print(f"     top_candidates={len(top_candidates)}, raw_trends={len(result['raw_trends'])}")
    if top_candidates:
        print(f"     #1 score={top_candidates[0]['score_breakdown']['final']}  '{top_candidates[0]['title'][:50]}'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
