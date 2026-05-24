"""fetch_wiki_data.py

`hugo/data/wiki_mapping.json` を読み込み、`type: "wikipedia"` のエントリは
Wikipedia REST API (`/api/rest_v1/page/summary/{title}`) で要約を取得し、
`type: "local"` はそのまま転記して、最終的に `hugo/static/wiki_cache.json` へ書き出す。

設計方針:
- build_post から独立した fetch 層 (Issue #674 教訓: 描画は I/O しない)
- 冪等: 既存 cache を読み込み、`updated_at` が `--ttl-days` 以内のものは再取得しない
- 1 req/sec で polite (Wikimedia rate limit 限度より十分低い)
- User-Agent 必須 (Wikimedia API policy)
- 個別エラー (404/disambig/timeout) はスキップ、ログのみ。run 全体は止めない

Issue: https://github.com/omochairo/amazon/issues/683 (Phase 1)
ライセンス: Wikipedia 要約は CC BY-SA 4.0 (cache 内に明示)
"""
from __future__ import annotations

import argparse
import json
import logging
import pathlib
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("fetch_wiki_data")

USER_AGENT = "omochairo-wiki-bot/1.0 (https://omcha.jp; contact via GitHub omochairo/amazon)"
WIKI_API = "https://ja.wikipedia.org/api/rest_v1/page/summary/{title}"
LICENSE_NOTE = "Wikipedia content under CC BY-SA 4.0 (https://creativecommons.org/licenses/by-sa/4.0/)"
EXTRACT_MAX_CHARS = 280
DEFAULT_SLEEP_SEC = 1.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _is_fresh(entry: dict[str, Any], ttl: timedelta) -> bool:
    ts = entry.get("updated_at")
    if not isinstance(ts, str):
        return False
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return False
    return datetime.now(timezone.utc) - dt < ttl


def _trim_extract(text: str, limit: int = EXTRACT_MAX_CHARS) -> str:
    """Wikipedia の extract は数文段落のことが多いので余分な空白を整え、limit でカット。"""
    if not text:
        return ""
    s = " ".join(text.split())
    if len(s) <= limit:
        return s
    cut = s[:limit]
    last = max(cut.rfind("。"), cut.rfind("．"), cut.rfind("."))
    return (cut[: last + 1] if last >= 80 else cut.rstrip() + "…")


def fetch_summary(target: str, session: requests.Session, timeout: float = 8.0) -> dict[str, Any] | None:
    """Wikipedia 要約を 1 件取得。404/disambig/error は None を返す。"""
    url = WIKI_API.format(title=urllib.parse.quote(target, safe=""))
    try:
        resp = session.get(url, timeout=timeout, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    except requests.RequestException as e:
        logger.warning("net error for %r: %s", target, e)
        return None
    if resp.status_code == 404:
        logger.info("404 for %r", target)
        return None
    if resp.status_code != 200:
        logger.warning("HTTP %s for %r", resp.status_code, target)
        return None
    try:
        data = resp.json()
    except ValueError:
        logger.warning("invalid JSON for %r", target)
        return None
    if data.get("type") == "disambiguation":
        logger.info("disambig for %r, skipping", target)
        return None
    extract = _trim_extract(data.get("extract") or "")
    if not extract:
        logger.info("empty extract for %r", target)
        return None
    page_url = (data.get("content_urls") or {}).get("desktop", {}).get("page") or \
        f"https://ja.wikipedia.org/wiki/{urllib.parse.quote(target)}"
    thumb = (data.get("thumbnail") or {}).get("source")
    return {
        "type": "wikipedia",
        "title": data.get("title") or target,
        "extract": extract,
        "url": page_url,
        "thumbnail": thumb,
        "updated_at": _now_iso(),
    }


def build_cache(
    mapping: dict[str, Any],
    prior: dict[str, Any],
    ttl_days: int,
    sleep_sec: float,
    limit: int | None = None,
) -> tuple[dict[str, Any], dict[str, int]]:
    out: dict[str, Any] = {}
    stats = {"local": 0, "wiki_fresh": 0, "wiki_fetched": 0, "wiki_skipped": 0}
    ttl = timedelta(days=ttl_days)
    session = requests.Session()
    fetched = 0
    for key, spec in sorted(mapping.items()):
        kind = spec.get("type")
        if kind == "local":
            out[key] = {
                "type": "local",
                "title": spec.get("title") or key,
                "extract": _trim_extract(spec.get("extract") or ""),
                "url": spec.get("url") or "",
                "thumbnail": None,
                "updated_at": _now_iso(),
            }
            stats["local"] += 1
            continue
        if kind != "wikipedia":
            continue
        target = spec.get("target") or key
        cached = prior.get(key)
        if cached and cached.get("type") == "wikipedia" and _is_fresh(cached, ttl):
            out[key] = cached
            stats["wiki_fresh"] += 1
            continue
        if limit is not None and fetched >= limit:
            if cached:
                out[key] = cached
                stats["wiki_fresh"] += 1
            else:
                stats["wiki_skipped"] += 1
            continue
        result = fetch_summary(target, session)
        fetched += 1
        if result is None:
            stats["wiki_skipped"] += 1
        else:
            result["target"] = target
            out[key] = result
            stats["wiki_fetched"] += 1
        time.sleep(sleep_sec)
    return out, stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Build Wikipedia summary cache for client-side tooltips.")
    parser.add_argument("--mapping", default="hugo/data/wiki_mapping.json")
    parser.add_argument("--out", default="hugo/static/wiki_cache.json")
    parser.add_argument("--ttl-days", type=int, default=30, help="cached wiki entry TTL (default 30d)")
    parser.add_argument("--sleep", type=float, default=DEFAULT_SLEEP_SEC, help="seconds between API calls")
    parser.add_argument("--limit", type=int, default=None, help="max API fetches per run (rest reuse cache)")
    args = parser.parse_args()

    mapping_path = pathlib.Path(args.mapping)
    out_path = pathlib.Path(args.out)
    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    prior_entries: dict[str, Any] = {}
    if out_path.exists():
        try:
            prior_doc = json.loads(out_path.read_text(encoding="utf-8"))
            prior_entries = prior_doc.get("entries") or {}
        except (ValueError, OSError):
            logger.warning("could not read prior cache %s, starting fresh", out_path)

    entries, stats = build_cache(mapping, prior_entries, args.ttl_days, args.sleep, args.limit)
    doc = {
        "version": 1,
        "generated_at": _now_iso(),
        "license": LICENSE_NOTE,
        "entries": entries,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=False), encoding="utf-8")
    logger.info("wrote %d entries to %s | stats=%s", len(entries), out_path, stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
