"""
fetch_third_party_sources.py  (#1600 Phase 2)

Tavily Search API で per_asin の商品名キーワードを検索し、
非販売 (= レビュー / 解説 / メディア) の第三者ソース URL を pre-fetch して
data/raw/per_asin/<ASIN>/third_party_sources.json に書き出す。

#1600 の根本原因は、公式/レビューメディアの一部 ASIN が per_asin に第三者材料を
持たず、Jules が google_search 任せで出典の薄い記事 (#1599 sources floor 割れ) を
書くこと。Phase 1 (score_per_asin_info) は真ゼロ品を defer するだけだが、本 Phase 2 は
「実在し検証可能な非販売候補 URL」を先回りで収集し、Jules の裏取り精度を底上げする。

検索 backend は当初 Google CSE を採用したが、2026-01-20 の仕様変更で新規エンジンの
「ウェブ全体検索」が廃止されたため Tavily に切替 (#1600 session 102)。Tavily は
エージェント/RAG 用途前提で GitHub Actions の共有 IP からも安定して叩ける。

quota: Tavily 無料枠は 1,000 query/月 (≈ 33/日)。freshness skip (既定 30日) で
定常呼び出しは thin/zero ASIN 数に収まる。--max-queries で日次上限を切る。
secret (TAVILY_API_KEY) 未設定時は inert (no-op, exit 0)。

env:
  TAVILY_API_KEY  Tavily Search API キー (https://app.tavily.com — CC 不要・無料枠)

Usage:
  python scripts/fetch_third_party_sources.py B0FX2RNS7J          # 単一 ASIN
  python scripts/fetch_third_party_sources.py --pool --max-queries 30
  python scripts/fetch_third_party_sources.py B0... --dry-run     # API を叩かず計画表示
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import os
import pathlib
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from fetch_cross_search import extract_search_keyword  # noqa: E402
import score_per_asin_info as _sc  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("fetch_third_party_sources")

PER_ASIN_DIR = pathlib.Path("data/raw/per_asin")
TAVILY_ENDPOINT = "https://api.tavily.com/search"
OUT_NAME = "third_party_sources.json"

# 販売/マーケットプレイス host (= 価格・購入ページ)。第三者「出典」には使わないので除外。
# レビュー価値のある価格比較 (kakaku 等) は残す: ユーザーレビューが裏取りに有用。
_RETAIL_HOST_SUBSTR = (
    "amazon.co.jp", "amazon.com", "amzn.to", "amzn.asia",
    "rakuten.co.jp", "rakuten.com", "r10.to",
    "shopping.yahoo.co.jp", "store.shopping.yahoo.co.jp", "paypaymall",
    "mercari.com", "jp.mercari", "fril.jp", "rakuma",
    "aupay.wowma.jp", "wowma.jp", "qoo10.jp", "dmm.com",
    "shop", "store.", "cart",  # 汎用 EC サブドメイン (緩め)
)
# 検索エンジンの結果ページ (#1599 で弾く対象)。出典 URL にしてはいけない。
#
# #5490 案B: この tuple は汎用エンジンしか列挙しておらず、価格比較/EC の検索 URL が
# 素通りしていた (実測 2026-08-18: 収集済み 6,577 URL のうち search.kakaku.com が
# 409 件で全 host 中 3 位、path/query 型と併せて約 510 件 = 7.7%)。構造で判定する
# score_per_asin_info.is_search_result_url を併用して塞ぐ (判定の SSOT は向こう側)。
_SEARCH_ENGINE_SUBSTR = (
    "google.com/search", "google.co.jp/search", "bing.com/search",
    "search.yahoo", "duckduckgo.com",
)
# 自サイト (第三者ではない)。
_OWN_SITE_SUBSTR = ("navi.omcha.jp", "omcha.jp")

_ASIN_RE = re.compile(r"^B0[A-Z0-9]{8}$")


def _host(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).netloc.lower().lstrip("www.")
    except ValueError:
        return ""


def _is_excluded(url: str) -> bool:
    low = (url or "").lower()
    if not low.startswith("http"):
        return True
    if _sc.is_search_result_url(low):  # #5490 案B: 検索結果ページを構造で弾く
        return True
    for grp in (_RETAIL_HOST_SUBSTR, _SEARCH_ENGINE_SUBSTR, _OWN_SITE_SUBSTR):
        for sub in grp:
            if sub in low:
                return True
    return False


def _load(path: pathlib.Path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _product_title(asin: str, base: pathlib.Path) -> str:
    amazon = _load(base / asin / "amazon.json")
    if isinstance(amazon, dict):
        item = amazon.get("item") if isinstance(amazon.get("item"), dict) else amazon
        if isinstance(item, dict) and isinstance(item.get("title"), str):
            return item["title"]
    return ""


def tavily_search(query: str, api_key: str, num: int = 10) -> list[dict]:
    """Tavily Search API を 1 回呼び、items(raw) を返す。429/4xx は例外送出。

    戻り値は CSE 時代と同じ shape ({"link","title","snippet"}) に正規化し、
    下流の _filter_sources / 既存テストを無改修で再利用する。
    """
    body = json.dumps({
        "query": query,
        "max_results": max(1, min(num, 20)),
        "search_depth": "basic",
        "topic": "general",
        "include_answer": False,
        "include_raw_content": False,
    }).encode("utf-8")
    req = urllib.request.Request(
        TAVILY_ENDPOINT, data=body, method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.load(resp)
    items: list[dict] = []
    for r in data.get("results", []) or []:
        if not isinstance(r, dict):
            continue
        items.append({
            "link": r.get("url", ""),
            "title": r.get("title", ""),
            "snippet": r.get("content", ""),
        })
    return items


def _filter_sources(raw_items: list[dict], max_sources: int) -> list[dict]:
    """検索 raw items から非販売 distinct host を抽出 (host あたり 1 件、上位 max_sources)。"""
    seen_hosts: set[str] = set()
    out: list[dict] = []
    for it in raw_items:
        if not isinstance(it, dict):
            continue
        link = it.get("link", "")
        if _is_excluded(link):
            continue
        h = _host(link)
        if not h or h in seen_hosts:
            continue
        seen_hosts.add(h)
        out.append({
            "title": (it.get("title") or "").strip(),
            "url": link,
            "snippet": (it.get("snippet") or "").strip(),
            "host": h,
        })
        if len(out) >= max_sources:
            break
    return out


def _is_fresh(path: pathlib.Path, max_age_days: int) -> bool:
    data = _load(path)
    if not isinstance(data, dict):
        return False
    ts = data.get("fetched_at")
    if not isinstance(ts, str):
        return False
    try:
        when = _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return False
    now = _dt.datetime.now(_dt.timezone.utc)
    return (now - when).days < max_age_days


def fetch_for_asin(
    asin: str, api_key: str, base: pathlib.Path,
    max_sources: int = 8, dry_run: bool = False,
) -> dict:
    """1 ASIN 分の第三者ソースを収集して書き出す。戻り値は要約 dict。"""
    title = _product_title(asin, base)
    if not title:
        return {"asin": asin, "status": "skip_no_title", "sources": 0}
    query = extract_search_keyword(title)
    if dry_run:
        return {"asin": asin, "status": "dry_run", "query": query, "sources": 0}
    raw = tavily_search(query, api_key, num=10)
    sources = _filter_sources(raw, max_sources)
    payload = {
        "asin": asin,
        "fetched_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "query": query,
        "engine": "tavily",
        "raw_count": len(raw),
        "sources": sources,
    }
    out_path = base / asin / OUT_NAME
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return {"asin": asin, "status": "ok", "query": query, "sources": len(sources)}


def _pickable_pool() -> list[str]:
    """03-invoke-jules と同じ候補母集合 (amazon.json items + ranking_pool − 既存記事)。"""
    cand: set[str] = set()
    raw = _load(pathlib.Path("data/raw/amazon.json"))
    if isinstance(raw, dict):
        for i in raw.get("items", []):
            a = i.get("asin") if isinstance(i, dict) else None
            if isinstance(a, str) and _ASIN_RE.match(a):
                cand.add(a)
    rp = _load(pathlib.Path("data/raw/ranking_pool.json"))
    if isinstance(rp, dict):
        for a in rp.get("asins", []):
            if isinstance(a, str) and _ASIN_RE.match(a):
                cand.add(a)
    existing: set[str] = set()
    for p in pathlib.Path("data/articles").glob("*.json"):
        m = re.search(r"(B0[A-Z0-9]{8})", p.name)
        if m:
            existing.add(m.group(1))
    return sorted(cand - existing)


def _cli() -> int:
    ap = argparse.ArgumentParser(description="per_asin 第三者ソース pre-fetch (#1600 Phase 2)")
    ap.add_argument("asin", nargs="?", help="単一 ASIN")
    ap.add_argument("--pool", action="store_true",
                    help="pick 母集合のうち band が薄い ASIN をまとめて収集")
    ap.add_argument("--bands", default="zero,thin",
                    help="--pool 対象 band (カンマ区切り, 既定 zero,thin)")
    ap.add_argument("--max-queries", type=int, default=30,
                    help="API 呼び出し日次上限 (Tavily 無料=1000/月≈33/日, 既定 30)")
    ap.add_argument("--max-sources", type=int, default=8, help="ASIN あたり保存 host 数")
    ap.add_argument("--max-age-days", type=int, default=30,
                    help="この日数以内に取得済なら skip (--force で無視)")
    ap.add_argument("--force", action="store_true", help="freshness を無視して再取得")
    ap.add_argument("--dry-run", action="store_true", help="API を叩かず計画のみ表示")
    ap.add_argument("--base", default=str(PER_ASIN_DIR))
    args = ap.parse_args()
    base = pathlib.Path(args.base)

    api_key = os.environ.get("TAVILY_API_KEY", "").strip()
    if not args.dry_run and not api_key:
        # secret 未設定: inert に no-op で正常終了 (cron が落ちないように)
        logger.warning("TAVILY_API_KEY 未設定 — no-op で終了 (inert)")
        return 0

    if args.asin:
        targets = [args.asin]
    elif args.pool:
        want = {b.strip() for b in args.bands.split(",") if b.strip()}
        targets = [a for a in _pickable_pool()
                   if _sc.score_asin(a, base).get("band") in want]
        logger.info("pool 対象 (band in %s): %d 件", sorted(want), len(targets))
    else:
        ap.error("ASIN または --pool が必要です")

    done = 0
    for asin in targets:
        if done >= args.max_queries:
            logger.info("max-queries (%d) 到達 — 残りは次回", args.max_queries)
            break
        out_path = base / asin / OUT_NAME
        if not args.force and not args.dry_run and _is_fresh(out_path, args.max_age_days):
            logger.info("%s: fresh (<%dd) skip", asin, args.max_age_days)
            continue
        try:
            r = fetch_for_asin(asin, api_key, base,
                               max_sources=args.max_sources, dry_run=args.dry_run)
        except urllib.error.HTTPError as e:
            if e.code in (429, 432, 433):
                logger.warning("HTTP %s (Tavily quota/plan limit) — 中断", e.code)
                break
            logger.warning("%s: HTTP %s skip", asin, e.code)
            continue
        except Exception as e:  # noqa: BLE001 — 1 件失敗で全体を止めない
            logger.warning("%s: %s skip", asin, e)
            continue
        logger.info("%s", json.dumps(r, ensure_ascii=False))
        if r.get("status") in ("ok", "dry_run"):
            done += 1
        if not args.dry_run:
            time.sleep(1)  # Tavily への礼儀 (秒間 1 query)
    logger.info("完了: %d 件処理 (max %d)", done, args.max_queries)
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
