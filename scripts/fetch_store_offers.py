"""トイザらスの楽天市場店・Yahoo!ショッピング店に、その商品があるかを JAN で確かめる。

なぜ要るか:
  キャラクター玩具の「どこで買える」ページには、店名 (トイザらス) を付けて探しに来る人がいる。
  横断検索 (fetch_cross_search.py) は検索結果の中央値の 1 件だけを選ぶので、転売の店が
  選ばれ、公式の店が在庫表に出ないことがある。トイザらスの楽天店・Yahoo 店に同じ JAN の
  商品があれば、在庫表に「トイザらス」の行として出す (where_to_buy_format.build_store_rows)。

確かめ方 (1 店 1 回):
  - 楽天  : IchibaItem/Search に shopCode=toysrus・keyword=JAN・availability=0
  - Yahoo : 商品検索 v3 に seller_id=toysrus-babierus・jan_code=JAN (完全一致)
  JAN の無い商品は確かめない (商品名で探すと別の商品を拾う)。
  同じ店の候補が複数返ったら、在庫があり価格の付いたものを優先する。
  横断検索ですでにトイザらスの出品が選ばれている ASIN も確かめる (その出品が消えた・
  品質の判定で隠れたときに出せるように)。在庫表で同じ店の行が重なるのは表示の側で防ぐ。

対象と周期:
  記事の date が where_to_buy_format.ROLLOUT_DATE 以降 (「どこで買える」型になりうる)
  で JAN のある ASIN。確かめていないもの・JAN が変わったものを先に、あとは古い順に
  対象数 / CYCLE_DAYS 件。在庫表に出すのは確かめてから DISPLAY_MAX_AGE_DAYS 日以内のもの。

上限は refresh_shop_prices.py と同じ (楽天 1.2 秒・Yahoo 2.5 秒の開始間隔、429 は 65 秒
待って 1 回だけやり直し、続けばその店は打ち切り)。同じキーを使うので、workflow では
refresh_shop_prices.py の後に順に動かす。

一時的な失敗 (通信エラー・401/403・429・5xx) では前の記録を変えない。

    python scripts/fetch_store_offers.py --write     # 本番 (data/raw/store_offers.json を書く)
    python scripts/fetch_store_offers.py --limit 5   # 既定は呼ぶだけで何も書かない
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import pathlib
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import fetch_cross_search as fcs  # noqa: E402
import refresh_shop_prices as rsp  # noqa: E402
from where_to_buy_format import ROLLOUT_DATE  # noqa: E402

logger = logging.getLogger("fetch_store_offers")

OUT_FILE = "store_offers.json"
CYCLE_DAYS = 10
MIN_AGE_DAYS = 9
DISPLAY_MAX_AGE_DAYS = 14

# 店の定義。key は store_offers.json の中の名前。
STORES = {
    "toysrus_rakuten": {"shop": "rakuten", "code": "toysrus"},
    "toysrus_yahoo": {"shop": "yahoo", "code": "toysrus-babierus"},
}

FOUND, MISSING, FAILED, SKIPPED = rsp.FOUND, rsp.MISSING, rsp.FAILED, rsp.SKIPPED


# --- 店ごとの確かめ方 ----------------------------------------------------------------

def check_rakuten_store(code: str, jan: str, keys: dict):
    params = {
        "applicationId": keys["rakuten_app_id"],
        "accessKey": keys["rakuten_access_key"],
        "shopCode": code,
        # Ichiba の keyword は自由文検索。店が商品ページに書いた JAN のまま投げる
        # (fetch_cross_search の Stage0b と同じ)
        "keyword": jan,
        "availability": 0,
        "formatVersion": 2,
        "hits": 5,
    }
    if keys.get("rakuten_aff_id"):
        params["affiliateId"] = keys["rakuten_aff_id"]
    status, body = rsp._request("rakuten_store_offer", rsp.ICHIBA_ITEM_URL,
                                params=params, headers=rsp._rakuten_headers())
    if rsp._is_failure(status, body):
        return FAILED, None
    if status == 404:
        return MISSING, None
    if status != 200:
        return SKIPPED, None
    cands = []
    for raw in body.get("Items") or []:
        item = fcs._parse_rakuten_item(raw, is_books=False)
        if not item or not str(item.get("itemCode", "")).startswith(code + ":"):
            continue
        if fcs._is_used_listing(item.get("title", "")):
            continue
        cands.append(item)
    best = _best(cands)
    return (FOUND, best) if best else (MISSING, None)


def _best(cands: list):
    """在庫あり・価格ありを優先して 1 件 (同じ店に旧ページと現行ページが並ぶことがある)。"""
    if not cands:
        return None
    return min(cands, key=lambda it: (not rsp._positive_price(it),
                                      it.get("availability") == "outofstock"))


def check_yahoo_store(code: str, jan: str, keys: dict):
    params = {"appid": keys["yahoo_client_id"], "seller_id": code,
              "jan_code": fcs._to_ean13(jan), "results": 5}
    sid, pid = keys.get("vc_sid", ""), keys.get("vc_pid", "")
    if sid and pid:
        params["affiliate_type"] = "vc"
        params["affiliate_id"] = f"{fcs.VC_REFERRAL_BASE}?sid={sid}&pid={pid}&vc_url="
    status, body = rsp._request("yahoo_store_offer", fcs.YAHOO_SEARCH_URL, params=params)
    if rsp._is_failure(status, body):
        return FAILED, None
    if status != 200:
        return SKIPPED, None
    cands = []
    for hit in body.get("hits") or []:
        raw_url = hit.get("url", "")
        if rsp.yahoo_seller_id(raw_url) != code:
            continue
        if fcs._is_used_listing(hit.get("name", "")):
            continue
        if "valuecommerce.com" not in raw_url and sid and pid:
            raw_url = (f"{fcs.VC_REFERRAL_BASE}?sid={sid}&pid={pid}"
                       f"&vc_url={urllib.parse.quote(raw_url, safe='')}")
        cands.append({
            "title": hit.get("name", ""),
            "price": hit.get("price", 0),
            "url": raw_url,
            "availability": "outofstock" if hit.get("inStock") is False else "instock",
        })
    best = _best(cands)
    return (FOUND, best) if best else (MISSING, None)


def check(store_key: str, jan: str, keys: dict):
    st = STORES[store_key]
    fn = check_rakuten_store if st["shop"] == "rakuten" else check_yahoo_store
    return fn(st["code"], jan, keys)


# --- 結果の反映 --------------------------------------------------------------------

def apply_result(prev: dict | None, outcome: str, item: dict | None, jan: str, now_iso: str):
    """新しい記録を返す。一時的な失敗・飛ばしたものは前の記録のまま (None なら記録しない)。"""
    if outcome == FOUND and rsp._positive_price(item):
        return {
            "status": "found",
            "title": item.get("title", ""),
            "price": int(item["price"]),
            "availability": item.get("availability") or "instock",
            "url": item.get("url", ""),
            "jan": jan,
            "checked_at": now_iso,
        }
    if outcome == MISSING:
        return {"status": "missing", "jan": jan, "checked_at": now_iso}
    return prev


# --- 対象 --------------------------------------------------------------------------

def collect_targets(articles_dir: pathlib.Path, per_asin_root: pathlib.Path,
                    rollout_date: str = ROLLOUT_DATE) -> dict:
    """{ASIN: JAN}。記事の date がロールアウト日以降で JAN のあるもの。"""
    out = {}
    if not articles_dir.exists():
        return out
    for p in articles_dir.glob("*.json"):
        if p.stem.endswith(fcs._SIDECAR_SUFFIXES):
            continue
        m = fcs._ASIN_FROM_FILENAME.search(p.stem)
        if not m:
            continue
        try:
            art = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(art, dict) or str(art.get("date") or "")[:10] < rollout_date:
            continue
        asin = m.group(1)
        jan = fcs._load_jan_from_per_asin(per_asin_root, asin)
        if jan:
            out[asin] = jan
    return out


def pick(records: dict, store_key: str, targets: dict, now: datetime,
         limit: int, min_age_days: float) -> list:
    """確かめる ASIN を limit 件。記録が無い・JAN が変わったものが先、あとは古い順。"""
    cutoff = now - timedelta(days=min_age_days)
    cands = []
    for asin, jan in targets.items():
        rec = (records.get(asin) or {}).get(store_key)
        if not rec or rec.get("jan") != jan:
            cands.append(("", asin))
            continue
        ts = fcs._parse_iso(rec.get("checked_at") or "")
        if ts is not None and ts > cutoff:
            continue
        cands.append((rec.get("checked_at") or "", asin))
    cands.sort()
    return [a for _ts, a in cands[:limit]]


# --- 実行 --------------------------------------------------------------------------

def run(queues: dict, records: dict, targets: dict, keys: dict, *, deadline: float,
        dry_run: bool, now_iso: str, clock=time.monotonic, sleep=time.sleep,
        progress: dict | None = None) -> dict:
    """店ごとの間隔を守りながら交互に呼ぶ。records を書き換える (dry_run では変えない)。

    ``progress["applied"]`` に、記録を書き換えた件数を足す (途中で落ちても残る)。
    """
    progress = progress if progress is not None else {"applied": 0}
    interval = {"rakuten": rsp.RAKUTEN_INTERVAL, "yahoo": rsp.YAHOO_INTERVAL}
    next_at = {k: 0.0 for k in queues}
    stats = {k: {} for k in queues}
    stopped = set()
    while True:
        live = [k for k in queues if queues[k] and k not in stopped]
        if not live:
            break
        key = min(live, key=lambda k: next_at[k])
        wait = next_at[key] - clock()
        if wait > 0:
            sleep(wait)
        if clock() > deadline:
            logger.info("時間の上限に達したので残りは次回に回す")
            break
        asin = queues[key].pop(0)
        jan = targets[asin]
        shop = STORES[key]["shop"]
        next_at[key] = clock() + interval[shop]
        try:
            outcome, item = check(key, jan, keys)
        except rsp.RateLimited:
            logger.warning(f"{key}: 429 が続いたので、この回の {key} は打ち切る")
            stopped.add(key)
            stats[key]["rate_limited"] = 1
            continue
        finally:
            next_at[key] = max(next_at[key], clock() + interval[shop])
        price = item.get("price") if item else None
        if dry_run:
            logger.info(f"[dry-run] {key} {asin}: {outcome} ￥{price}")
        else:
            prev = (records.get(asin) or {}).get(key)
            new = apply_result(prev, outcome, item, jan, now_iso)
            if new is not None and new is not prev:
                records.setdefault(asin, {})[key] = new
                progress["applied"] += 1
            logger.info(f"{key} {asin}: {outcome} ￥{price}")
        stats[key][outcome] = stats[key].get(outcome, 0) + 1
    for k in queues:
        stats[k]["left"] = len(queues[k])
    return stats


def load(path: pathlib.Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save(path: pathlib.Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--write", action="store_true",
                    help="store_offers.json を書く。付けなければ API を呼ぶだけで何も書かない")
    ap.add_argument("--limit", type=int, default=0, help="店ごとの件数 (0 = 対象数 / 10)")
    ap.add_argument("--max-minutes", type=float, default=12.0)
    ap.add_argument("--force", action="store_true", help="今日すでに実行していても動く")
    args = ap.parse_args(argv)

    raw = pathlib.Path("data/raw")
    out_path = raw / OUT_FILE
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    today_jst = now.astimezone(rsp.JST).date().isoformat()
    data = load(out_path)
    records = data.get("items") if isinstance(data.get("items"), dict) else {}
    dry_run = not args.write
    if not dry_run and not args.force and data.get("last_run_jst") == today_jst:
        logger.info(f"今日 ({today_jst}) はもう実行したので何もしない")
        return 0

    keys = {
        "rakuten_app_id": os.environ.get("RAKUTEN_APP_ID", ""),
        "rakuten_access_key": os.environ.get("RAKUTEN_ACCESS_KEY", ""),
        "rakuten_aff_id": os.environ.get("RAKUTEN_AFFILIATE_ID", ""),
        "yahoo_client_id": os.environ.get("YAHOO_CLIENT_ID", ""),
        "vc_sid": os.environ.get("VALUECOMMERCE_SID", ""),
        "vc_pid": os.environ.get("VALUECOMMERCE_PID", ""),
    }
    have_key = {
        "rakuten": bool(keys["rakuten_app_id"] and keys["rakuten_access_key"]),
        "yahoo": bool(keys["yahoo_client_id"]),
    }
    targets = collect_targets(pathlib.Path("data/articles"), raw / "per_asin")

    queues = {}
    for key, st in STORES.items():
        if not have_key[st["shop"]]:
            logger.warning(f"{key}: 鍵が無いので飛ばす")
            continue
        limit = args.limit or math.ceil(len(targets) / CYCLE_DAYS)
        queues[key] = pick(records, key, targets, now, limit, MIN_AGE_DAYS)
        logger.info(f"{key}: 対象 {len(targets)} 件・今回 {len(queues[key])} 件")

    deadline = time.monotonic() + args.max_minutes * 60
    stats = {}
    progress = {"applied": 0}
    try:
        stats = run(queues, records, targets, keys, deadline=deadline,
                    dry_run=dry_run, now_iso=now_iso, progress=progress)
    finally:
        # 1 件も確かめられなかった回 (鍵なし・全部失敗) は書かない。書くと実行日が残り、
        # 同じ日のもう 1 回 (01-fetch-products は 1 日 2 回) でやり直せなくなる
        if not dry_run and progress["applied"]:
            # 記事の対象から外れた ASIN と、知らない店の記録は消す
            for asin in list(records):
                if asin not in targets or not isinstance(records[asin], dict):
                    del records[asin]
                    continue
                for key in list(records[asin]):
                    if key not in STORES:
                        del records[asin][key]
                if not records[asin]:
                    del records[asin]
            save(out_path, {"last_run_jst": today_jst, "last_run_at": now_iso,
                            "stats": stats, "items": records})
    found = {k: sum(1 for r in records.values() if (r.get(k) or {}).get("status") == "found")
             for k in STORES}
    logger.info(f"結果 {stats}・見つかっている件数 {found}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
