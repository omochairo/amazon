"""resolve_news_urls.py — Google ニュースの転送 URL を元記事の URL に解いて覚える (navi-brain 候補供給 P1a)。

per_asin/<ASIN>/news.json の URL は news.google.com の転送 URL で、記事レーン (Web を使えない書き手)
はページを開いて確かめられない。元記事の URL に解けば、事前チェックが本文を開いて抜粋を取れる
(2026-11-22 の計測: 出典不足で外した商品のうち、解けた 21 件中 8 件が要るサイト数に届いた)。

Google は短い間に約 30 件解くと 429 を返し、40 分以上解けなくなる (同計測)。そこで
- 1 日の上限 (既定 20 件、JST の日付で数える) と、1 件ごとの間隔を置く
- 429 を受けたらその日はやめる (blocked を記録し、次の run も同じ日は解かない)
- 解いた結果は data/raw/news_resolved.json に持ち、同じ URL を二度解かない
per_asin/<ASIN>/news.json は filter_raw_per_asin.py が毎回書き直すので、そこには書かない。

対象は記事の無い商品だけ。1 周目は商品ごとに 1 件ずつ (多くの商品に早く届くように)、
ASIN の降順 (新しい商品ほど ASIN が大きい) で選ぶ。
"""

import argparse
import datetime as dt
import glob
import json
import logging
import os
import pathlib
import re
import time
import urllib.parse

import requests

import self_domain as _self_domain

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("resolve_news_urls")

JST = dt.timezone(dt.timedelta(hours=9))
CACHE_PATH = pathlib.Path("data/raw/news_resolved.json")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/130 Safari/537.36"}
_GID_RE = re.compile(r"news\.google\.com/(?:rss/)?articles/([^?/#]+)")
_ASIN_RE = re.compile(r"B[0-9A-Z]{9}")
# 失敗 (通信・形式) は 7 日たったら解き直す。「元の URL が無い」(none) は解き直さない
RETRY_ERROR_DAYS = 7


class Blocked(Exception):
    """Google が 429 を返した。その日はもう解かない。"""


def is_gnews(url: str) -> bool:
    return bool(_GID_RE.search(url or ""))


def decode(url: str, session=None, timeout: int = 20) -> str | None:
    """news.google.com/rss/articles/<id> を元記事の URL に解く。元の URL が無ければ None。

    記事ページの data-n-a-sg / data-n-a-ts (署名と時刻) を読み、batchexecute (Fbv4je) に渡す。
    429 は Blocked、それ以外の HTTP の失敗は requests の例外として上げる。
    """
    m = _GID_RE.search(url or "")
    if not m:
        return None
    gid = m.group(1)
    s = session or requests
    r = s.get(f"https://news.google.com/articles/{gid}", headers=UA, timeout=timeout)
    if r.status_code == 429:
        raise Blocked(f"429 on article page {gid[:16]}")
    r.raise_for_status()
    sg = re.search(r'data-n-a-sg="([^"]+)"', r.text)
    ts = re.search(r'data-n-a-ts="([^"]+)"', r.text)
    if not (sg and ts):
        # 同意画面・一時的な応答でも署名が無い。恒久の「元の URL 無し」にせず、失敗として 7 日後に解き直す
        raise ValueError(f"no signature on article page {gid[:16]}")
    inner = ["garturlreq",
             [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1, None, None, None, None, None, 0, 1],
              "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0],
             gid, int(ts.group(1)), sg.group(1)]
    freq = [[["Fbv4je", json.dumps(inner, separators=(",", ":")), None, "generic"]]]
    body = urllib.parse.urlencode({"f.req": json.dumps(freq, separators=(",", ":"))})
    r2 = s.post("https://news.google.com/_/DotsSplashUi/data/batchexecute", data=body,
                headers={**UA, "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
                timeout=timeout)
    if r2.status_code == 429:
        raise Blocked(f"429 on batchexecute {gid[:16]}")
    r2.raise_for_status()
    return parse_batchexecute(r2.text)


def parse_batchexecute(text: str) -> str | None:
    """batchexecute の応答から garturlres の URL を取り出す。"""
    for line in (text or "").split("\n"):
        if "garturlres" not in line:
            continue
        try:
            arr = json.loads(line)
            payload = json.loads(arr[0][2])
            u = payload[1]
            if isinstance(u, str) and u.startswith("http"):
                return u
        except (ValueError, IndexError, TypeError):
            m = re.search(r'garturlres\\",\\"(https?://[^\\"]+)', line)
            if m:
                return m.group(1)
    return None


def load_cache(path: pathlib.Path = CACHE_PATH) -> dict:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        d = {}
    if not isinstance(d, dict):
        d = {}
    d.setdefault("urls", {})
    d.setdefault("day", {})
    return d


def save_cache(cache: dict, path: pathlib.Path = CACHE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cache["urls"] = dict(sorted(cache["urls"].items()))
    path.write_text(json.dumps(cache, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def resolved_url(cache: dict, url: str) -> str | None:
    """覚えている元記事の URL (解けて、自社ドメインでないもの)。無ければ None。"""
    e = (cache.get("urls") or {}).get(url)
    if isinstance(e, dict) and e.get("status") == "ok" and e.get("url"):
        return e["url"]
    return None


def _needs_resolve(entry, today: dt.date) -> bool:
    if not isinstance(entry, dict):
        return True
    if entry.get("status") != "error":
        return False
    try:
        at = dt.date.fromisoformat(entry.get("at") or "")
    except ValueError:
        return True
    return (today - at).days >= RETRY_ERROR_DAYS


def article_asins(articles_dir: str = "data/articles") -> set[str]:
    out = set()
    for p in glob.glob(os.path.join(articles_dir, "*.json")):
        m = _ASIN_RE.search(os.path.basename(p))
        if m:
            out.add(m.group(0))
    return out


def news_urls(per_asin_dir: str, asin: str) -> list[str]:
    try:
        d = json.loads(pathlib.Path(per_asin_dir, asin, "news.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return []
    items = (d.get("items") or []) if isinstance(d, dict) else (d if isinstance(d, list) else [])
    return [it.get("url") for it in items if isinstance(it, dict) and is_gnews(it.get("url") or "")]


def pick_targets(cache: dict, per_asin_dir: str, skip_asins: set[str], today: dt.date, limit: int) -> list[str]:
    """解く URL を選ぶ。記事の無い商品を ASIN の降順に、商品ごとに 1 件ずつ回す。"""
    per = {}
    for p in sorted(glob.glob(os.path.join(per_asin_dir, "*", "news.json")), reverse=True):
        asin = os.path.basename(os.path.dirname(p))
        if asin in skip_asins:
            continue
        todo = [u for u in news_urls(per_asin_dir, asin) if _needs_resolve(cache["urls"].get(u), today)]
        if todo:
            per[asin] = todo
    out, seen = [], set()
    depth = 0
    while len(out) < limit and per:
        progressed = False
        for asin in list(per):
            if depth < len(per[asin]):
                u = per[asin][depth]
                if u not in seen:
                    seen.add(u)
                    out.append(u)
                    if len(out) >= limit:
                        break
                progressed = True
        if not progressed:
            break
        depth += 1
    return out


def run(cache: dict, targets: list[str], today: dt.date, daily_limit: int, interval: float,
        decode_fn=None, sleep=time.sleep) -> dict:
    """targets を上限まで解いて cache を更新する。戻り値は数えた結果。"""
    decode_fn = decode_fn or decode
    day = cache["day"]
    if day.get("date") != today.isoformat():
        day.clear()
        day.update({"date": today.isoformat(), "count": 0, "blocked": False})
    stats = {"tried": 0, "ok": 0, "none": 0, "error": 0, "blocked": bool(day.get("blocked"))}
    if day.get("blocked"):
        logger.info("今日は 429 を受けたので解かない")
        return stats
    for i, u in enumerate(targets):
        if day["count"] >= daily_limit:
            logger.info("1 日の上限 %d 件に達した", daily_limit)
            break
        if i:
            sleep(interval)
        day["count"] += 1
        stats["tried"] += 1
        try:
            r = decode_fn(u)
        except Blocked as e:
            logger.warning("Google が止めてきた (%s)。今日はやめる", e)
            day["blocked"] = True
            stats["blocked"] = True
            day["count"] -= 1  # 解けていないので数えない (上限の判定は blocked で止まる)
            stats["tried"] -= 1
            break
        except Exception as e:  # noqa: BLE001 — 通信・形式の失敗は 7 日後に解き直す
            logger.warning("解けなかった: %s (%s)", u[:80], e)
            cache["urls"][u] = {"status": "error", "at": today.isoformat()}
            stats["error"] += 1
            continue
        if r and not _self_domain.is_self_domain(r):
            cache["urls"][u] = {"status": "ok", "url": r, "at": today.isoformat()}
            stats["ok"] += 1
        else:
            # 元の URL が無い、または自社の記事 (自分の記事を自分の根拠にしない)
            cache["urls"][u] = {"status": "none", "at": today.isoformat()}
            stats["none"] += 1
    return stats


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-asin-dir", default="data/raw/per_asin")
    ap.add_argument("--articles-dir", default="data/articles")
    ap.add_argument("--cache", default=str(CACHE_PATH))
    ap.add_argument("--daily-limit", type=int, default=20, help="JST の 1 日に解く上限 (run をまたいで数える)")
    ap.add_argument("--per-run", type=int, default=10, help="1 run で解く上限")
    ap.add_argument("--interval", type=float, default=8.0, help="1 件ごとの間隔 (秒)")
    args = ap.parse_args(argv)

    cache_path = pathlib.Path(args.cache)
    cache = load_cache(cache_path)
    today = dt.datetime.now(JST).date()
    targets = pick_targets(cache, args.per_asin_dir, article_asins(args.articles_dir), today, args.per_run)
    logger.info("候補 %d 件", len(targets))
    stats = run(cache, targets, today, args.daily_limit, args.interval)
    save_cache(cache, cache_path)
    logger.info("結果 %s / 今日の累計 %s", stats, cache["day"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
