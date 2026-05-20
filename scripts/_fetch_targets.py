"""Stale-first per-ASIN target picker for fetch_{youtube,yahoo_news,books}.py.

なぜ必要か:
  fetch_*.py は従来 `data/raw/amazon.json` (trend pool, 25 ASIN 程度) のみを
  per-ASIN query 対象にしていた。Article 化済み 218 ASIN は amazon.json から
  外れるため新規 query されず、per_asin/<ASIN>/<type>.json の充足率が
  常に低いまま (session 38 計測で youtube 27.1% 頭打ち)。

  全 ASIN を毎 run query すれば fulfillment は上がるが API quota が枯渇する
  (YouTube Data API v3 search.list = 100 units/call、4 key で 40,000/day)。

解決策:
  (A) target = union(amazon.json, data/articles/*.json) で広げる
  (B) `data/raw/_fetch_state.json` に {type: {asin: queried_at}} を永続化
  (C) 各 run で `(now - queried_at) > stale_after_days` の ASIN のみ候補化、
      古い順 sort → 上限 (default 50/run) で cap

これで 243 ASIN を 7 日サイクルで巡回しつつ、当該 run の quota を平準化。
"""

from __future__ import annotations

import json
import logging
import pathlib
from datetime import datetime, timedelta, timezone
from typing import Iterable

logger = logging.getLogger("fetch_targets")

_STATE_FILENAME = "_fetch_state.json"


def _state_path(out_dir: pathlib.Path) -> pathlib.Path:
    return out_dir / _STATE_FILENAME


def load_state(out_dir: pathlib.Path) -> dict:
    """{source: {asin: ISO8601_str}} を返す。欠損は空 dict。"""
    p = _state_path(out_dir)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logger.warning(f"State file {p} unreadable; starting fresh")
        return {}


def save_state(out_dir: pathlib.Path, state: dict) -> None:
    p = _state_path(out_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _parse_iso(s: str) -> datetime:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return datetime.fromtimestamp(0, tz=timezone.utc)


def _collect_article_targets(articles_dir: pathlib.Path) -> dict:
    """data/articles/<YYYY-MM-DD>-<ASIN>.json から {asin: title} を作る。"""
    out: dict = {}
    if not articles_dir.exists():
        return out
    for art_path in articles_dir.glob("*.json"):
        stem = art_path.stem
        if stem.endswith(".quality") or stem.endswith(".enrichment") or stem.endswith(".seo"):
            continue
        parts = stem.split("-")
        if len(parts) < 4:
            continue
        asin = parts[-1]
        try:
            art = json.loads(art_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        title = ""
        if isinstance(art.get("product"), dict):
            title = art["product"].get("name") or art["product"].get("title") or ""
        if not title:
            title = art.get("title") or ""
        if title and asin not in out:
            out[asin] = title
    return out


def pick_target_asins(
    out_dir: pathlib.Path,
    source: str,
    amazon_items: Iterable[dict],
    articles_dir: pathlib.Path,
    max_per_run: int = 50,
    stale_after_days: int = 7,
    now: datetime | None = None,
) -> list[tuple[str, str]]:
    """この run で query すべき (asin, title) のリストを返す。

    順序: stale-first (古い queried_at から)。`max_per_run` で cap。
    `(now - queried_at) <= stale_after_days` の ASIN は完全に除外。
    state に entry が無い ASIN は最優先 (epoch 0 扱い)。
    """
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=stale_after_days)

    targets: dict = {}
    for amz in amazon_items:
        asin = amz.get("asin") or ""
        title = amz.get("title") or ""
        if asin and title:
            targets[asin] = title
    for asin, title in _collect_article_targets(articles_dir).items():
        targets.setdefault(asin, title)

    state = load_state(out_dir).get(source, {})

    candidates: list[tuple[datetime, str, str]] = []
    fresh = 0
    for asin, title in targets.items():
        ts_str = state.get(asin)
        if ts_str:
            ts = _parse_iso(ts_str)
            if ts > cutoff:
                fresh += 1
                continue
        else:
            ts = datetime.fromtimestamp(0, tz=timezone.utc)
        candidates.append((ts, asin, title))

    candidates.sort(key=lambda x: x[0])
    picked = candidates[:max_per_run]

    logger.info(
        f"[{source}] targets={len(targets)} fresh<={stale_after_days}d={fresh} "
        f"stale={len(candidates)} picked={len(picked)}/{max_per_run}"
    )
    return [(asin, title) for _, asin, title in picked]


def mark_queried(out_dir: pathlib.Path, source: str, asins: Iterable[str],
                 now: datetime | None = None) -> None:
    """state を更新して disk に書き戻す。Empty result でも mark することで
    繰り返し空 query の retry loop を防ぐ。"""
    if now is None:
        now = datetime.now(timezone.utc)
    state = load_state(out_dir)
    src_state = state.setdefault(source, {})
    ts = now.isoformat()
    for asin in asins:
        if asin:
            src_state[asin] = ts
    save_state(out_dir, state)


def write_per_asin_raw(
    out_dir: pathlib.Path,
    source: str,
    asin: str,
    query: str,
    items: list,
    now: datetime | None = None,
) -> None:
    """data/raw/per_asin/<ASIN>/<source>.raw.json を書く。

    filter_raw_per_asin がこれを per-ASIN pool として読む。当 run で query
    しなかった ASIN の raw は残るので、filter は古いデータでも使える
    (= 7 日 stale 閾値 + 50 ASIN/run cap でも全 ASIN がカバーされる)。
    """
    if now is None:
        now = datetime.now(timezone.utc)
    per_asin_dir = out_dir / "per_asin" / asin
    per_asin_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "queried_at": now.isoformat(),
        "query": query,
        "items": items,
    }
    (per_asin_dir / f"{source}.raw.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_per_asin_raw_items(out_dir: pathlib.Path, source: str, asin: str) -> list:
    """filter 側用。per_asin/<ASIN>/<source>.raw.json から items を取り出す。
    無ければ []。"""
    p = out_dir / "per_asin" / asin / f"{source}.raw.json"
    if not p.exists():
        return []
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        items = d.get("items")
        return items if isinstance(items, list) else []
    except json.JSONDecodeError:
        return []
