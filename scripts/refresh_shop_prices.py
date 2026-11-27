"""マッチ済みの楽天・Yahoo の商品を、古い順に 1 件 1 回で確かめ直す (#12305)。

fetch_cross_search.py は「その日の amazon.json の ASIN」しか価格を取り直さないので、
記事の ASIN の楽天・Yahoo の価格は一度マッチすると古いまま残っていた
(2026-11-27 時点で楽天の 30 日超が約 77%)。ここでは照合 (どの商品か) はやり直さず、
すでにマッチしている商品ページの価格・在庫だけを確かめ直す。

確かめ方 (どれも 1 件 1 回):
  - 楽天 市場  : IchibaItem/Search に itemCode と availability=0 (在庫切れも返す)
  - 楽天 ブックス: BooksTotal/Search に isbnjan。itemCode が無いので URL で同じ本か確かめる
  - Yahoo      : 商品検索 v3 に seller_id (URL のストア ID) と jan_code (無ければ商品名)。
                 返った中から URL が一致するものを選ぶ (v1 itemLookup は使わない)

見つからなかった (楽天 404 / 0 件・一致する URL が無い) が 2 回続いたら ``_gone_at`` を付ける。
一時的な失敗 (通信エラー・401/403・429・5xx・壊れた JSON) では何も変えない。

上限 (#12305 で確認):
  - 楽天: アプリ ID ごとに 1 秒 1 回以下 (市場・ブックス合算) → 開始間隔 1.2 秒
  - Yahoo: アプリ ID ごとに 1 分 30 回 → 開始間隔 2.5 秒 (24 回/分)
  楽天と Yahoo は枠が別なので、交互に呼んで待ち時間を重ねる。

1 日 1 回だけ動く: ``data/raw/_shop_refresh_state.json`` に日本時間の実行日を残し、
同じ日の 2 回目は何もしない (01-fetch-products は 1 日 2 回走るため)。

    python scripts/refresh_shop_prices.py --write         # 本番 (matched JSON を書き換える)
    python scripts/refresh_shop_prices.py --limit 3       # 既定は呼ぶだけで何も書かない
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import pathlib
import re
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import _api_health  # noqa: E402
import fetch_cross_search as fcs  # noqa: E402

logger = logging.getLogger("refresh_shop_prices")

JST = timezone(timedelta(hours=9))
RAKUTEN_INTERVAL = 1.2
YAHOO_INTERVAL = 2.5
RATE_LIMIT_WAIT = 65.0
CYCLE_DAYS = 7
MIN_AGE_DAYS = 6
GONE_AFTER_MISSES = 2
STATE_FILE = "_shop_refresh_state.json"
ICHIBA_ITEM_URL = "https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search/20260701"

# 結果の種類
FOUND = "found"
MISSING = "missing"      # 商品ページが見つからない (消えた候補)
FAILED = "failed"        # 一時的な失敗。何も変えない
SKIPPED = "skipped"      # 確かめる手がかりが無い・400 など


class RateLimited(Exception):
    """429 が待ってやり直しても続いた。そのショップはこの回は打ち切る。"""


# --- URL の正規化 ----------------------------------------------------------------

_AFFILIATE_WRAPPERS = {"hb.afl.rakuten.co.jp": "pc", "ck.jp.ap.valuecommerce.com": "vc_url"}


def _unwrap(url: str) -> str:
    """アフィリエイトの包み (楽天 hb.afl の pc=・VC の vc_url=) を外した商品 URL。

    包みのホストのときだけ外す。商品ページ自身のクエリは触らない。
    """
    if not url:
        return ""
    parsed = urllib.parse.urlparse(url)
    key = _AFFILIATE_WRAPPERS.get(parsed.netloc.lower())
    if key:
        inner = urllib.parse.parse_qs(parsed.query).get(key)
        if inner:
            return inner[0]
    return url


def url_key(url: str) -> str:
    """同じ商品ページかを比べるための鍵。ホスト (小文字) + パス (大小文字はそのまま・末尾の / なし)。

    保存してある URL も同じ API の応答から取ったものなので、パスの大小文字は一致する。
    大小文字だけ違う別商品を同じとみなさないよう、パスは小文字にしない。
    """
    p = urllib.parse.urlparse(_unwrap(url))
    return f"{p.netloc.lower()}{p.path.rstrip('/')}"


_YAHOO_STORE_RE = re.compile(r"^https?://store\.shopping\.yahoo\.co\.jp/([^/?#]+)/", re.I)


def yahoo_seller_id(url: str) -> str:
    m = _YAHOO_STORE_RE.match(_unwrap(url))
    return m.group(1) if m else ""


# --- HTTP -------------------------------------------------------------------------

def _request(api: str, url: str, *, params: dict, headers: dict | None = None):
    """1 回呼ぶ。429 は RATE_LIMIT_WAIT 秒待って 1 回だけやり直す。

    返り値: (status, json or None)。通信エラーは (None, None)。
    429 が続いたら RateLimited を投げる。
    """
    for attempt in range(2):
        try:
            resp = requests.get(url, params=params, headers=headers or {}, timeout=15)
        except requests.exceptions.RequestException as e:
            _api_health.record(api, None)
            logger.warning(f"  {api}: 通信エラー {type(e).__name__}")
            return None, None
        _api_health.record(api, resp.status_code)
        if resp.status_code == 429:
            if attempt:
                raise RateLimited(api)
            logger.warning(f"  {api}: 429。{RATE_LIMIT_WAIT:.0f} 秒待ってやり直す")
            time.sleep(RATE_LIMIT_WAIT)
            continue
        try:
            body = resp.json()
        except ValueError:
            body = None
        return resp.status_code, body
    raise RateLimited(api)  # 到達しない


def _is_failure(status, body) -> bool:
    if status is None or status in (401, 403) or status >= 500:
        return True
    return status == 200 and not isinstance(body, dict)


# --- ショップ別の確かめ方 -----------------------------------------------------------

def _rakuten_headers() -> dict:
    return {
        "Referer": "https://github.com/omochairo/amazon",
        "Origin": "https://github.com/omochairo/amazon",
    }


def check_rakuten_ichiba(entry: dict, keys: dict):
    params = {
        "applicationId": keys["rakuten_app_id"],
        "accessKey": keys["rakuten_access_key"],
        "itemCode": entry["itemCode"],
        "availability": 0,
        "formatVersion": 2,
        "hits": 1,
    }
    if keys.get("rakuten_aff_id"):
        params["affiliateId"] = keys["rakuten_aff_id"]
    status, body = _request("rakuten_ichiba_refresh", ICHIBA_ITEM_URL,
                            params=params, headers=_rakuten_headers())
    if _is_failure(status, body):
        return FAILED, None
    if status == 404:
        return MISSING, None
    if status != 200:
        return SKIPPED, None
    for raw in body.get("Items") or []:
        item = fcs._parse_rakuten_item(raw, is_books=False)
        if item and item.get("itemCode") == entry["itemCode"]:
            return FOUND, item
    return MISSING, None


def check_rakuten_books(entry: dict, keys: dict):
    url, params, headers = fcs._rakuten_books_params(
        keys["rakuten_app_id"], keys["rakuten_access_key"], keys.get("rakuten_aff_id", ""))
    params.update({"isbnjan": fcs._to_ean13(entry.get("jan_code", "")), "formatVersion": 2, "hits": 5})
    status, body = _request("rakuten_books_refresh", url, params=params, headers=headers)
    if _is_failure(status, body):
        return FAILED, None
    if status == 404:
        return MISSING, None
    if status != 200:
        return SKIPPED, None
    want = url_key(entry.get("url", ""))
    for raw in body.get("Items") or []:
        item = fcs._parse_rakuten_item(raw, is_books=True)
        if item and url_key(item.get("url", "")) == want:
            return FOUND, item
    return MISSING, None


def check_yahoo(entry: dict, keys: dict):
    seller = yahoo_seller_id(entry.get("url", ""))
    params = {"appid": keys["yahoo_client_id"], "seller_id": seller, "results": 20}
    # JAN で引くのは、JAN で照合したものだけ。JAN を持っていても照合が商品名だったもの
    # (JAN の検索が 0 件だった) は、JAN で引いても 0 件なので「消えた」と誤る
    jan = fcs._to_ean13(entry.get("jan_code", "")) if entry.get("_match_method") == "jan" else ""
    if jan:
        params["jan_code"] = jan
    else:
        params["query"] = (entry.get("title") or "")[:100]
    sid, pid = keys.get("vc_sid", ""), keys.get("vc_pid", "")
    if sid and pid:
        params["affiliate_type"] = "vc"
        params["affiliate_id"] = f"{fcs.VC_REFERRAL_BASE}?sid={sid}&pid={pid}&vc_url="
    status, body = _request("yahoo_shopping_refresh", fcs.YAHOO_SEARCH_URL, params=params)
    if _is_failure(status, body):
        return FAILED, None
    if status != 200:
        return SKIPPED, None
    want = url_key(entry.get("url", ""))
    for hit in body.get("hits") or []:
        raw_url = hit.get("url", "")
        if url_key(raw_url) != want:
            continue
        if "valuecommerce.com" not in raw_url and sid and pid:
            raw_url = f"{fcs.VC_REFERRAL_BASE}?sid={sid}&pid={pid}&vc_url={urllib.parse.quote(raw_url, safe='')}"
        return FOUND, {
            "title": hit.get("name", ""),
            "price": hit.get("price", 0),
            "url": raw_url,
            "availability": "outofstock" if hit.get("inStock") is False else "instock",
            "free_shipping": (hit.get("shipping") or {}).get("code") == 2,
        }
    return MISSING, None


def method_for(shop: str, entry: dict):
    """この entry を確かめる関数。手がかりが無ければ None。"""
    if entry.get("_gone_at"):
        return None  # 消えたものは fetch_cross_search の再検索に任せる
    if shop == "rakuten":
        if entry.get("itemCode"):
            return check_rakuten_ichiba
        # ブックスは isbnjan で照合したものだけ (商品名で照合した本は isbnjan で同じ本が返る保証がない)
        if (entry.get("_match_method") == "jan_books" and entry.get("jan_code")
                and "books.rakuten.co.jp" in _unwrap(entry.get("url", ""))):
            return check_rakuten_books
        return None
    if yahoo_seller_id(entry.get("url", "")) and (entry.get("jan_code") or entry.get("title")):
        return check_yahoo
    return None


# --- 結果の反映 --------------------------------------------------------------------

_UPDATE_FIELDS = ("price", "availability", "free_shipping", "asuraku", "title")


def _positive_price(item: dict | None) -> bool:
    try:
        return bool(item) and int(item.get("price") or 0) > 0
    except (TypeError, ValueError):
        return False


def apply_result(entry: dict, outcome: str, item: dict | None, now_iso: str) -> str:
    """entry を書き換える。返り値はログ用の一言。"""
    if outcome == FOUND and not _positive_price(item):
        # 見つかったのに金額が無い・0。古い価格を新しい日付で出さないよう何も変えない
        return "no_price"
    if outcome == FOUND:
        for k in _UPDATE_FIELDS:
            if k in item and item[k] not in (None, ""):
                entry[k] = item[k]
        if item.get("url"):  # アフィリエイト URL を空で上書きしない
            entry["url"] = item["url"]
        entry["_refreshed_at"] = now_iso
        entry["_refresh_method"] = "recheck"
        entry.pop("_recheck_misses", None)
        entry.pop("_gone_at", None)
        return "found"
    if outcome == MISSING:
        n = int(entry.get("_recheck_misses", 0) or 0) + 1
        entry["_recheck_misses"] = n
        if n >= GONE_AFTER_MISSES:
            entry["_gone_at"] = now_iso
            return "gone"
        return f"missing({n})"
    return outcome


# --- 対象の選び方 ------------------------------------------------------------------

def _age_key(entry: dict) -> str:
    return entry.get("_refreshed_at") or ""


def pick(entries: dict, shop: str, targets, now: datetime, limit: int, min_age_days: float):
    """記事の対象 ASIN のマッチを古い順に limit 件。最近確かめたものは除く。"""
    cutoff = (now - timedelta(days=min_age_days)).isoformat()
    cands = []
    for asin, e in entries.items():
        if asin not in targets or method_for(shop, e) is None:
            continue
        ts = _age_key(e)
        if ts and fcs._parse_iso(ts) and fcs._parse_iso(ts) > fcs._parse_iso(cutoff):
            continue
        cands.append((ts, asin))
    cands.sort()
    return [a for _ts, a in cands[:limit]]


def default_limit(entries: dict, shop: str, targets) -> int:
    n = sum(1 for a, e in entries.items() if a in targets and method_for(shop, e) is not None)
    return math.ceil(n / CYCLE_DAYS)


# --- 実行 --------------------------------------------------------------------------

def run(queues: dict, entries_by_shop: dict, keys: dict, *, deadline: float, dry_run: bool,
        now_iso: str, clock=time.monotonic, sleep=time.sleep, progress: dict | None = None) -> dict:
    """楽天と Yahoo を、それぞれの間隔を守りながら交互に呼ぶ。

    ``progress["applied"]`` に、見つかった・見つからなかったを entry に反映した件数を足す
    (一時的な失敗・飛ばしたものは数えない)。
    """
    progress = progress if progress is not None else {"applied": 0}
    interval = {"rakuten": RAKUTEN_INTERVAL, "yahoo": YAHOO_INTERVAL}
    next_at = {s: 0.0 for s in queues}
    stats = {s: {} for s in queues}
    stopped = set()
    while True:
        live = [s for s in queues if queues[s] and s not in stopped]
        if not live:
            break
        shop = min(live, key=lambda s: next_at[s])
        wait = next_at[shop] - clock()
        if wait > 0:
            sleep(wait)
        if clock() > deadline:
            logger.info("時間の上限に達したので残りは次回に回す")
            break
        asin = queues[shop].pop(0)
        entry = entries_by_shop[shop][asin]
        fn = method_for(shop, entry)
        next_at[shop] = clock() + interval[shop]
        try:
            outcome, item = fn(entry, keys)
        except RateLimited:
            logger.warning(f"{shop}: 429 が続いたので、この回の {shop} は打ち切る")
            stopped.add(shop)
            stats[shop]["rate_limited"] = 1
            continue
        finally:
            # 429 のあと 65 秒待ってやり直した呼び出しは、開始が上の next_at より後になる。
            # 終わった時刻からも間隔を空け、やり直しの直後に次を投げないようにする
            next_at[shop] = max(next_at[shop], clock() + interval[shop])
        if dry_run:
            label = outcome
            price = item.get("price") if item else None
            logger.info(f"[dry-run] {shop} {asin}: {outcome} 旧￥{entry.get('price')} 新￥{price} "
                        f"({fn.__name__})")
        else:
            label = apply_result(entry, outcome, item, now_iso)
            if label != "no_price" and outcome in (FOUND, MISSING):
                progress["applied"] += 1
            logger.info(f"{shop} {asin}: {label}")
        key = label.split("(")[0]
        stats[shop][key] = stats[shop].get(key, 0) + 1
    for s in queues:
        stats[s]["left"] = len(queues[s])
    return stats


def age_summary(entries: dict, targets, now: datetime) -> dict:
    """記事の対象のマッチの古さの分布。"""
    out = {"<=7d": 0, "<=14d": 0, "<=30d": 0, ">30d": 0, "none": 0, "gone": 0}
    for asin, e in entries.items():
        if asin not in targets:
            continue
        if e.get("_gone_at"):
            out["gone"] += 1
            continue
        ts = fcs._parse_iso(e.get("_refreshed_at") or "")
        if ts is None:
            out["none"] += 1
            continue
        d = (now - ts).total_seconds() / 86400
        out["<=7d" if d <= 7 else "<=14d" if d <= 14 else "<=30d" if d <= 30 else ">30d"] += 1
    return out


def _load(path: pathlib.Path) -> tuple[dict, list]:
    """matched JSON を読む。無い・壊れているときは ValueError (そのショップは飛ばす)。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ValueError(f"{path} を読めない: {e}") from e
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ValueError(f"{path} に items の一覧が無い")
    index = {}
    for it in items:
        if isinstance(it, dict) and it.get("matched_asin"):
            index[it["matched_asin"]] = it
    return index, items


def _save(path: pathlib.Path, items: list) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"items": items}, f, ensure_ascii=False, indent=4)
    os.replace(tmp, path)


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--write", action="store_true",
                    help="matched JSON を書き換える。付けなければ API を呼ぶだけで何も書かない")
    ap.add_argument("--limit", type=int, default=0, help="ショップごとの件数 (0 = 対象数 / 7)")
    ap.add_argument("--max-minutes", type=float, default=25.0)
    ap.add_argument("--min-age-days", type=float, default=MIN_AGE_DAYS)
    ap.add_argument("--shops", default="rakuten,yahoo")
    ap.add_argument("--force", action="store_true", help="今日すでに実行していても動く")
    args = ap.parse_args(argv)

    raw = pathlib.Path("data/raw")
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    today_jst = now.astimezone(JST).date().isoformat()
    state_path = raw / STATE_FILE
    state = {}
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            state = {}
        if not isinstance(state, dict):
            state = {}
    dry_run = not args.write
    if not dry_run and not args.force and state.get("last_run_jst") == today_jst:
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
    amazon_path = raw / "amazon.json"
    amazon_items = []
    if amazon_path.exists():
        amazon_items = json.loads(amazon_path.read_text(encoding="utf-8")).get("items", [])
    targets = set(fcs._collect_targets(amazon_items, pathlib.Path("data/articles"), raw / "per_asin"))

    shops = [s for s in args.shops.split(",") if s]
    files = {"rakuten": raw / "rakuten_matched.json", "yahoo": raw / "yahoo_matched.json"}
    have_key = {
        "rakuten": bool(keys["rakuten_app_id"] and keys["rakuten_access_key"]),
        "yahoo": bool(keys["yahoo_client_id"]),
    }
    entries_by_shop, items_by_shop, queues = {}, {}, {}
    for s in shops:
        if not have_key.get(s):
            logger.warning(f"{s}: 鍵が無いので飛ばす")
            continue
        try:
            entries_by_shop[s], items_by_shop[s] = _load(files[s])
        except ValueError as e:
            logger.warning(f"{s}: {e}。飛ばす")
            continue
        limit = args.limit or default_limit(entries_by_shop[s], s, targets)
        queues[s] = pick(entries_by_shop[s], s, targets, now, limit, args.min_age_days)
        logger.info(f"{s}: 今回 {len(queues[s])} 件 (上限 {limit})・古さ {age_summary(entries_by_shop[s], targets, now)}")

    deadline = time.monotonic() + args.max_minutes * 60
    stats = {}
    progress = {"applied": 0}
    try:
        stats = run(queues, entries_by_shop, keys, deadline=deadline, dry_run=dry_run,
                    now_iso=now_iso, progress=progress)
    finally:
        # 途中で落ちても、確かめ終えた分は残す (次回は残りの古い順から)
        if not dry_run and progress["applied"]:
            for s in entries_by_shop:
                _save(files[s], items_by_shop[s])
            # first_write_at: 書き込みで動き始めた時刻。market_prices.stale_cutoff は、ここから
            # SHOP_REFRESH_WARMUP_DAYS たつまで「確認日が古い価格を隠す」を始めない。
            # 1 件も確かめられなかった回 (鍵なし・全部失敗) は記録しない。記録すると、
            # 確かめ直せていないのに古い価格が隠れ始め、同じ日のやり直しも止まるため
            state = {"first_write_at": state.get("first_write_at") or now_iso,
                     "last_run_jst": today_jst, "last_run_at": now_iso, "stats": stats}
            state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        elif not dry_run:
            logger.warning("1 件も確かめられなかったので、matched JSON も実行日も書かない")
    for s in entries_by_shop:
        logger.info(f"{s}: 結果 {stats.get(s)}・古さ {age_summary(entries_by_shop[s], targets, now)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
