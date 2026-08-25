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

レーンは 2 本ある。母集合が違うだけで、収集・保存の処理は共通:

  --pool     新規候補レーン。母集合は「まだ記事になっていない ASIN」(_pickable_pool)
             で、そこから band が薄いものを拾う。既存の挙動。
  --from-gsc 既存記事レーン (#5490 案B / brain#13 2-3)。母集合を GSC の需要
             (直近 4 週の imp) で差し替える。_pickable_pool は `cand - existing` で
             既存記事を除外しているため、リライト対象には Tavily が一度も走らない
             状態だった。ここで単に `- existing` を外すと母集合が 1 本のプールに
             融合し、既存記事が --max-queries を食い尽くして新規候補レーンが飢える
             (デッドロックではないので気づきにくい)。なので母集合ごと差し替える
             別フラグにし、--max-queries も別に持たせる。

  band を抽出条件にしないのは、thin がコーパスの 3/4 を占めていて選別になって
  いないため。需要 (imp) で並べれば zero も自然に上位へ入る。

quota: Tavily 無料枠は 1,000 query/月 (≈ 33/日)。freshness skip (既定 30日) で
定常呼び出しは thin/zero ASIN 数に収まる。--max-queries で日次上限を切る。
secret (TAVILY_API_KEY) 未設定時は inert (no-op, exit 0)。

env:
  TAVILY_API_KEY  Tavily Search API キー (https://app.tavily.com — CC 不要・無料枠)

Usage:
  python scripts/fetch_third_party_sources.py B0FX2RNS7J          # 単一 ASIN
  python scripts/fetch_third_party_sources.py --pool --max-queries 30
  python scripts/fetch_third_party_sources.py --from-gsc --max-queries 20
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
from typing import Optional

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from fetch_cross_search import extract_search_keyword  # noqa: E402
import score_per_asin_info as _sc  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("fetch_third_party_sources")

PER_ASIN_DIR = pathlib.Path("data/raw/per_asin")
TAVILY_ENDPOINT = "https://api.tavily.com/search"
OUT_NAME = "third_party_sources.json"
GSC_BY_PAGE = pathlib.Path("data/analytics/history/gsc_by_page.jsonl")

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
# 商品ページ URL から ASIN を復元する (slug は小文字)。ハブ/一覧は対象外。
_PRODUCT_PAGE_RE = re.compile(r"/products/(b0[a-z0-9]{8})/?(?:[?#]|$)", re.IGNORECASE)


def _host(url: str) -> str:
    """URL から host を取り出し、先頭の "www." だけを落とす。

    `lstrip("www.")` は**文字集合**を削るので、"w" や "." で始まる host の
    先頭文字まで食う (walmart.com → almart.com / watch.impress.co.jp →
    atch.impress.co.jp)。ここで作った host は third_party_sources.json に
    保存され、build_post の source_highlights が読者に出典として表示し、
    _HIGHLIGHT_HOST_DENY の照合にも使われるので、削るのは接頭辞だけにする。
    score_per_asin_info._third_party_hosts と同じ正規化。
    """
    try:
        return urllib.parse.urlparse(url).netloc.lower().removeprefix("www.")
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


def month_usage(base: pathlib.Path, now: Optional[_dt.datetime] = None) -> int:
    """今月ぶんの取得件数を、書き出し済み `fetched_at` から数える。

    Tavily 無料枠は 1,000/月。レーンが 2 本 (新規候補 / 既存記事) あり**別プロセス**で
    走るので、プロセス内のカウンタでは合算を見張れない。両者が書き出す
    `third_party_sources.json` の `fetched_at` が唯一の共有記録なので、そこを数える。

    **これは下限の推定であって正確な API 消費ではない**:
      - 失敗した呼び出し (429 や 0 件) はファイルを残さないが credit は消えている
      - 同じ ASIN を同月に 2 回引くとファイルが上書きされるので 1 件にしか見えない
        (`--max-age-days` が 30 以上なら実質起きない)
    どちらも**過小**に出るので、budget を「これ以上は投げない」の上限として使うぶんには
    安全側に倒れない。**枠に張り付いてからでは遅い**ので、閾値は余裕を持たせること。
    """
    now = now or _dt.datetime.now(_dt.timezone.utc)
    prefix = now.strftime("%Y-%m")
    used = 0
    for path in base.glob("*/" + OUT_NAME):
        data = _load(path)
        if not isinstance(data, dict):
            continue
        ts = data.get("fetched_at")
        if isinstance(ts, str) and ts.startswith(prefix):
            used += 1
    return used


def _notice(level: str, message: str) -> None:
    """Actions の run ログに annotation を出す (ローカルでは素の log のみ)。

    #4793: 枠の枯渇や縮退が「緑のまま収集数だけ減る」形で進むと誰も気づかない。
    ログ行は 1 日 40 行に埋もれるので、UI に出る annotation を併せて出す。
    """
    getattr(logger, "warning" if level == "warning" else "info")("%s", message)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print("::{}::{}".format(level, message), flush=True)


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


def _gsc_page_impressions(
    history: pathlib.Path, days: int, anchor: str | None = None,
) -> dict[str, int]:
    """gsc_by_page.jsonl の直近 `days` 日を ASIN 単位で imp 合計する。

    窓の右端は「今日」ではなくデータ側の最新日 (GSC は 2〜3 日遅れて届くので、
    今日を基準にすると窓の右端が常に空になる)。`anchor` で明示指定もできる。
    """
    rows: list[dict] = []
    try:
        with open(history, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(r, dict) and isinstance(r.get("date"), str):
                    rows.append(r)
    except FileNotFoundError:
        logger.warning("GSC history が無い: %s", history)
        return {}
    if not rows:
        return {}
    end = anchor or max(r["date"] for r in rows)
    try:
        start = (_dt.date.fromisoformat(end) - _dt.timedelta(days=max(1, days) - 1)).isoformat()
    except ValueError:
        logger.warning("anchor 日付が不正: %s", end)
        return {}
    imps: dict[str, int] = {}
    for r in rows:
        if not (start <= r["date"] <= end):
            continue
        m = _PRODUCT_PAGE_RE.search(r.get("page") or "")
        if not m:
            continue
        try:
            imp = int(r.get("impressions") or 0)
        except (TypeError, ValueError):
            continue
        asin = m.group(1).upper()
        imps[asin] = imps.get(asin, 0) + imp
    logger.info("GSC 窓 %s〜%s: 商品ページ %d 件", start, end, len(imps))
    return imps


def _gsc_demand_pool(
    base: pathlib.Path,
    history: pathlib.Path = GSC_BY_PAGE,
    days: int = 28,
    min_impressions: int = 10,
    anchor: str | None = None,
) -> list[str]:
    """需要 (GSC imp) を持ち、まだ第三者ソースを持っていない ASIN を imp 降順で返す。

    band では絞らない (thin がコーパスの 3/4 で選別になっていない)。imp で並べれば
    zero 帯も上位に入るし、tp_hosts>=2 になった zero は #5499 の配線で thin へ上がる。
    """
    imps = _gsc_page_impressions(history, days, anchor=anchor)
    ranked = sorted(
        ((a, v) for a, v in imps.items() if v >= min_impressions),
        key=lambda kv: (-kv[1], kv[0]),
    )
    out = [a for a, _ in ranked
           if _sc.score_asin(a, base).get("third_party_hosts", 0) < _sc._THIRD_PARTY_MIN_HOSTS]
    logger.info("GSC 需要 (imp>=%d, 直近 %d 日): %d 件 / うち第三者ソース未保有 %d 件",
                min_impressions, days, len(ranked), len(out))
    return out


def _cli() -> int:
    ap = argparse.ArgumentParser(description="per_asin 第三者ソース pre-fetch (#1600 Phase 2)")
    ap.add_argument("asin", nargs="?", help="単一 ASIN")
    ap.add_argument("--pool", action="store_true",
                    help="pick 母集合のうち band が薄い ASIN をまとめて収集")
    ap.add_argument("--bands", default="zero,thin",
                    help="--pool 対象 band (カンマ区切り, 既定 zero,thin)")
    ap.add_argument("--from-gsc", action="store_true",
                    help="母集合を GSC 需要 (既存記事) に差し替える。--pool とは排他")
    ap.add_argument("--gsc-history", default=str(GSC_BY_PAGE),
                    help="--from-gsc が読む gsc_by_page.jsonl")
    ap.add_argument("--gsc-days", type=int, default=28,
                    help="--from-gsc の集計窓 (日, 既定 28 = 直近 4 週)")
    ap.add_argument("--gsc-min-impressions", type=int, default=10,
                    help="--from-gsc の imp しきい値 (既定 10)")
    ap.add_argument("--gsc-anchor", default=None,
                    help="--from-gsc の窓の右端 (既定: データ側の最新日)")
    ap.add_argument("--max-queries", type=int, default=30,
                    help="API 呼び出し日次上限 (Tavily 無料=1000/月≈33/日, 既定 30)")
    ap.add_argument("--max-sources", type=int, default=8, help="ASIN あたり保存 host 数")
    ap.add_argument("--max-age-days", type=int, default=30,
                    help="この日数以内に取得済なら skip (--force で無視)")
    ap.add_argument("--monthly-budget", type=int, default=900,
                    help="今月ぶんの取得上限 (Tavily 無料=1000/月。0 で無効)。"
                         "2 本のレーンで共有し、書き出し済み fetched_at から実消費を数える")
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

    if args.pool and args.from_gsc:
        # 母集合が融合すると、既存記事が --max-queries を食い尽くして新規候補レーンが
        # 飢える。レーンは必ず別々に (別 --max-queries で) 起動する。
        ap.error("--pool と --from-gsc は同時に指定できません (レーンを分けてください)")

    if args.asin:
        targets = [args.asin]
    elif args.from_gsc:
        targets = _gsc_demand_pool(
            base,
            history=pathlib.Path(args.gsc_history),
            days=args.gsc_days,
            min_impressions=args.gsc_min_impressions,
            anchor=args.gsc_anchor,
        )
    elif args.pool:
        want = {b.strip() for b in args.bands.split(",") if b.strip()}
        targets = [a for a in _pickable_pool()
                   if _sc.score_asin(a, base).get("band") in want]
        logger.info("pool 対象 (band in %s): %d 件", sorted(want), len(targets))
    else:
        ap.error("ASIN / --pool / --from-gsc のいずれかが必要です")

    # 月次バジェット。レーンは別プロセスなので、共有記録 (fetched_at) から実消費を
    # 数えて残枠を出す。ここで絞らないと 新規 30 + 既存 20 = 50/日 = 1,500/月 が
    # 名目上の上限になり、無料枠 1,000/月 を月末前に使い切る。
    limit = args.max_queries
    if args.monthly_budget > 0 and not args.dry_run:
        used = month_usage(base)
        remaining = args.monthly_budget - used
        pct = used / args.monthly_budget * 100
        if remaining <= 0:
            _notice("warning",
                    "Tavily 月次バジェット到達: 今月 {} 件 / budget {} — 今回は 0 件で終了 "
                    "(枠切れで無言縮退させないため、意図的に何も投げない)"
                    .format(used, args.monthly_budget))
            logger.info("完了: 0 件処理 (budget 到達)")
            return 0
        if pct >= 80:
            _notice("warning",
                    "Tavily 月次バジェット {:.0f}% 消費 (今月 {} 件 / budget {}, 残 {})"
                    .format(pct, used, args.monthly_budget, remaining))
        else:
            logger.info("月次バジェット: 今月 %d 件 / budget %d (残 %d)",
                        used, args.monthly_budget, remaining)
        if remaining < limit:
            logger.info("残枠 %d < max-queries %d — 今回は残枠に合わせる", remaining, limit)
            limit = remaining

    done = 0
    for asin in targets:
        if done >= limit:
            logger.info("上限 (%d) 到達 — 残りは次回", limit)
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
                # #4793 と同じ形: ここを log だけで抜けると、レーンは緑のまま
                # 収集数だけ静かに減る。UI に出して気づけるようにする。
                _notice("warning",
                        "Tavily quota/plan limit (HTTP {}) で中断 — {} 件処理した時点。"
                        "月次バジェットが実消費より緩い可能性があるので見直すこと"
                        .format(e.code, done))
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
