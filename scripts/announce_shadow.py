#!/usr/bin/env python3
"""新商品の発表を起点に Amazon の商品を探す影運転 (navi-brain#210)。

記事レーンの新規は 1 日 4 本前後で頭打ちになっている。出典不足で外した商品は、判定をゆるめても
ほぼ救えない (仕様 v2 §11)。そこで入口を逆にする案を、本番の取り込みを変えずに測る:

1. メーカーの PR TIMES 企業別 RSS・玩具系メディアの RSS から、直近の発表を読む
   (取り元は data/announce_sources.json)
2. 題名の「」『』から商品名を取り出す
3. 商品名で Amazon (Creators API の SearchItems, Toys) を検索し、いちばん合う商品を記録する

**記録だけ**: data/ には何も書かない (amazon.json に入れないので記事レーンの候補にならない)。
結果は JSON (workflow の artifact) と step summary に出す。2 週間分を集めて、
「1 日に何件の発表が Amazon の商品に結びつくか・そのうち今の取り込みに無いものは何件か」を数える。

SearchItems は 1 回の実行で --max-search 回まで・1.1 秒間隔 (fetch_amazon と同じ)。
"""
from __future__ import annotations

import argparse
import email.utils
import glob
import json
import logging
import os
import pathlib
import re
import sys
import time
import unicodedata
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

logger = logging.getLogger("announce_shadow")

SOURCES_PATH = pathlib.Path("data/announce_sources.json")
UA = "Mozilla/5.0 (compatible; navi-announce-shadow/1.0; +https://navi.omcha.jp/)"
FEED_TIMEOUT = 20
FEED_INTERVAL = 1.2   # 取り元ごとの間隔 (秒)
SEARCH_INTERVAL = 1.1  # SearchItems の間隔 (fetch_amazon と同じ)
MATCH_OK = 0.6        # 商品名の語のうち Amazon の商品名に出た割合がこれ以上なら「見つかった」

# 題名がこの語を含めば新商品の発表とみなす (含まない発表も記録はするが、検索の優先を下げる)
_RELEASE_RE = re.compile(r"発売|新発売|新商品|新製品|登場|予約|新作|リニューアル|ラインナップ|シリーズ")
# 括弧の中身がこれだけなら商品名ではない (イベント・キャンペーン・番組・店の名前など)
_NOT_PRODUCT_RE = re.compile(
    r"キャンペーン|フェア|イベント|ポップアップ|POP ?UP|ストア|ショップ|カフェ|展示|開催|大賞|受賞|"
    r"コンテスト|調査|アンケート|プレゼント|放送|番組|配信|公式サイト|特設サイト|アプリ|セミナー|募集",
    re.I)
# 【】は社名・「新商品」などの札に使われるので見ない (2026-11-24 の実データ)
_BRACKET_RE = re.compile(r"[「『]([^」』]{3,60})[」』]")
_DROP_CHARS_RE = re.compile(r"[™®©]|\(R\)|\(TM\)", re.I)


# ---------------------------------------------------------------- フィード

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_date(text: str | None) -> datetime | None:
    """dc:date (ISO 8601)・pubDate (RFC 822)・Atom の updated を UTC の datetime にする。"""
    if not text:
        return None
    text = text.strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = email.utils.parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_feed(raw: bytes) -> list[dict[str, Any]]:
    """RSS 1.0 / 2.0・Atom の記事を [{title, link, date, desc}] にする。読めなければ []。"""
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []
    out = []
    for el in root.iter():
        tag = _local(el.tag)
        if tag not in ("item", "entry"):
            continue
        d: dict[str, str] = {}
        link = ""
        for c in el:
            ct = _local(c.tag)
            if ct == "link":
                link = (c.text or "").strip() or c.attrib.get("href", "")
            elif ct in ("title", "date", "pubDate", "updated", "published", "description", "summary"):
                d.setdefault(ct, (c.text or "").strip())
        date = d.get("date") or d.get("pubDate") or d.get("published") or d.get("updated")
        desc = re.sub(r"<[^>]+>", " ", d.get("description") or d.get("summary") or "")
        out.append({"title": d.get("title", ""), "link": link, "date": parse_date(date),
                    "desc": " ".join(desc.split())[:300]})
    return out


def parse_news_sitemap(raw: bytes, categories: list[str]) -> list[dict[str, Any]]:
    """Google ニュース形式のサイトマップ (PR TIMES の sitemap-news.xml) から、news:keywords に
    categories のどれかがある記事だけを返す。PR TIMES は公式カテゴリ (例「おもちゃ・遊具・人形」) を
    keywords に入れている。"""
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []
    want = set(categories or [])
    out = []
    for url in root.iter():
        if _local(url.tag) != "url":
            continue
        loc, title, date, kws = "", "", None, []
        for el in url.iter():
            t = _local(el.tag)
            if t == "loc":
                loc = (el.text or "").strip()
            elif t == "title":
                title = (el.text or "").strip()
            elif t == "publication_date":
                date = parse_date(el.text)
            elif t == "keywords":
                kws = [k.strip() for k in (el.text or "").split(",") if k.strip()]
        if want and not want.intersection(kws):
            continue
        out.append({"title": title, "link": loc, "date": date, "desc": ",".join(kws)[:300]})
    return out


def fetch_feed(url: str, opener=None) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    opener = opener or urllib.request.urlopen
    with opener(req, timeout=FEED_TIMEOUT) as r:
        return r.read()


# ---------------------------------------------------------------- 商品名

def _norm(s: str) -> str:
    return unicodedata.normalize("NFKC", s or "")


def extract_names(title: str) -> list[str]:
    """発表の題名から商品名らしい括弧の中身を返す (出た順・重複なし)。"""
    out = []
    # ™ は NFKC で "TM" になるので、正規化の前に記号を落とす
    for m in _BRACKET_RE.findall(_norm(_DROP_CHARS_RE.sub(" ", title or ""))):
        name = " ".join(_DROP_CHARS_RE.sub(" ", m).split())
        if len(name) < 3 or _NOT_PRODUCT_RE.search(name):
            continue
        if name not in out:
            out.append(name)
    return out


def search_name(title: str, names: list[str]) -> str | None:
    """検索に使う商品名を 1 つ選ぶ。「作品」より「商品」の形は、より の後ろの括弧が商品名。
    それ以外は先頭の括弧 (たいてい商品名)。"""
    if not names:
        return None
    t = _norm(title)
    if len(names) > 1 and "より" in t:
        cut = t.index("より")
        after = [n for n in names if t.find(n) > cut]
        if after:
            return after[0]
    return names[0]


def is_release(title: str) -> bool:
    return bool(_RELEASE_RE.search(_norm(title)))


def name_tokens(name: str) -> list[str]:
    import source_pages
    return [t for t in source_pages.identity_tokens(name) if t not in source_pages._GENERIC_TOKENS]


def match_score(name: str, amazon_title: str) -> float:
    """商品名の識別語のうち、Amazon の商品名に出た割合 (0〜1)。語が無ければ 0。"""
    toks = name_tokens(name)
    if not toks:
        return 0.0
    t = _norm(amazon_title).lower()
    return sum(1 for x in toks if _norm(x).lower() in t) / len(toks)


# ---------------------------------------------------------------- 対象の選び方

def collect(feeds: list[dict], since: datetime, fetch=fetch_feed, sleeper=time.sleep) -> tuple[list[dict], list[dict]]:
    """取り元を全部読み、since 以降の発表を [{feed, kind, title, link, date, names, release}] で返す。"""
    entries, feed_stats = [], []
    for i, f in enumerate(feeds):
        if i:
            sleeper(FEED_INTERVAL)
        stat = {"id": f["id"], "items": 0, "recent": 0, "error": None}
        try:
            raw = fetch(f["url"])
            items = (parse_news_sitemap(raw, f.get("categories") or []) if f.get("kind") == "sitemap"
                     else parse_feed(raw))
        except Exception as e:  # noqa: BLE001 — 1 つの取り元の失敗で全体を止めない
            stat["error"] = f"{type(e).__name__}: {e}"[:200]
            feed_stats.append(stat)
            continue
        stat["items"] = len(items)
        for it in items:
            if it["date"] is None or it["date"] < since:
                continue
            stat["recent"] += 1
            entries.append({"feed": f["id"], "kind": f.get("kind", ""), "title": it["title"],
                            "link": it["link"], "date": it["date"].isoformat(),
                            "names": extract_names(it["title"]), "release": is_release(it["title"])})
        feed_stats.append(stat)
    return entries, feed_stats


def pick_targets(entries: list[dict], max_search: int) -> list[tuple[dict, str]]:
    """検索する (発表, 商品名) を選ぶ。1 発表 1 商品名・商品名は重複させない。

    並び: 玩具のカテゴリ・メーカーの発表 → メディア (HOBBY Watch はフィギュア・模型が多く、知育玩具の
    サイトの対象から外れやすい) の順。その中は新商品の語がある発表を先に、新しい順。
    """
    def key(e):
        return (e["kind"] == "media", not e["release"], -datetime.fromisoformat(e["date"]).timestamp())
    out, seen = [], set()
    for e in sorted(entries, key=key):
        n = search_name(e["title"], e["names"])
        if not n or n in seen:
            continue
        seen.add(n)
        out.append((e, n))
        if len(out) >= max_search:
            break
    return out


# ---------------------------------------------------------------- 検索

def _search_items(response: Any) -> list[dict]:
    """SearchItems の応答から items を取り出す (searchResult.items。probe_demand_supply と同じ形)。"""
    if not isinstance(response, dict):
        return []
    sr = response.get("searchResult")
    items = sr.get("items") if isinstance(sr, dict) else None
    if not isinstance(items, list):
        items = response.get("items")
    return [it for it in items or [] if isinstance(it, dict)]


def load_catalog() -> tuple[set[str], set[str]]:
    """(per_asin に商品データがある ASIN, 記事がある ASIN)。"""
    cat = {pathlib.Path(p).parent.name for p in glob.glob("data/raw/per_asin/*/amazon.json")}
    art = set()
    for p in glob.glob("data/articles/*.json"):
        m = re.search(r"(B0[0-9A-Z]{8}|\d{9}[\dX])\.json$", p)
        if m:
            art.add(m.group(1))
    return cat, art


def search_one(api, name: str, catalog: set[str], articles: set[str]) -> dict[str, Any]:
    import fetch_amazon
    import genre_gate
    import source_rules
    try:
        res = api.search_items(keywords=name, search_index="Toys", item_count=5, item_page=1,
                               resources=fetch_amazon.SEARCH_ITEM_RESOURCES)
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"[:200], "hits": []}
    hits = []
    for it in _search_items(res):
        n = fetch_amazon.normalize_api_item(it, tag="", source="announce-shadow")
        asin = n.get("asin") or ""
        if not asin:
            continue
        hits.append({
            "asin": asin,
            "title": (n.get("title") or "")[:120],
            "score": round(match_score(name, n.get("title") or ""), 2),
            "price": n.get("price"),
            "trusted_seller": fetch_amazon.is_trusted_seller(n.get("seller") or ""),
            "genre": genre_gate.classify_genre(n.get("browse_nodes") or [], asin)[0],
            "in_catalog": asin in catalog,
            "has_article": asin in articles,
            "need_sites": source_rules.required_non_sales_sites(asin),
        })
    hits.sort(key=lambda h: h["score"], reverse=True)
    return {"error": None, "hits": hits[:3]}


def summarize(entries: list[dict], results: list[dict]) -> dict[str, int]:
    found = [r for r in results if r["hits"] and r["hits"][0]["score"] >= MATCH_OK]
    top = [r["hits"][0] for r in found]
    return {
        "entries": len(entries),
        "with_name": sum(1 for e in entries if e["names"]),
        "release": sum(1 for e in entries if e["release"]),
        "searched": len(results),
        "search_errors": sum(1 for r in results if r.get("error")),
        "found": len(found),
        "found_new_release": sum(1 for h in top if h["need_sites"] < 2),
        "found_not_in_catalog": sum(1 for h in top if not h["in_catalog"]),
        "found_no_article": sum(1 for h in top if not h["has_article"]),
        "found_writable_shape": sum(1 for h in top if not h["has_article"] and h["genre"] != "flag"
                                    and h["trusted_seller"]),
    }


def run(since_hours: float, max_search: int, sources_path: pathlib.Path | None = None, api=None,
        fetch=fetch_feed, sleeper=time.sleep, now: datetime | None = None, dry_run: bool = False) -> dict:
    now = now or datetime.now(timezone.utc)
    feeds = json.loads((sources_path or SOURCES_PATH).read_text(encoding="utf-8"))["feeds"]
    entries, feed_stats = collect(feeds, now - timedelta(hours=since_hours), fetch=fetch, sleeper=sleeper)
    targets = pick_targets(entries, max_search)
    results = []
    if not dry_run and targets:
        if api is None:
            from creators_api_client import CreatorsAPIClient  # 遅延 import (dry-run では不要)
            api = CreatorsAPIClient()
        catalog, articles = load_catalog()
        for i, (e, name) in enumerate(targets):
            if i:
                sleeper(SEARCH_INTERVAL)
            r = search_one(api, name, catalog, articles)
            results.append({"feed": e["feed"], "title": e["title"], "link": e["link"], "date": e["date"],
                            "name": name, **r})
    return {"generated_at": now.isoformat(), "since_hours": since_hours, "dry_run": dry_run,
            "feeds": feed_stats, "entries": entries, "targets": [n for _, n in targets],
            "results": results, "summary": summarize(entries, results)}


def step_summary(report: dict) -> str:
    s = report["summary"]
    lines = ["## 発表起点の影運転 (navi-brain#210)", "",
             f"- 発表 {s['entries']}（商品名あり {s['with_name']}・新商品の語 {s['release']}）",
             f"- 検索 {s['searched']}（失敗 {s['search_errors']}）→ 見つかった {s['found']}"
             f"（新発売 {s['found_new_release']}・取り込み外 {s['found_not_in_catalog']}・"
             f"記事なし {s['found_no_article']}・記事にできる形 {s['found_writable_shape']}）", ""]
    errs = [f for f in report["feeds"] if f["error"]]
    if errs:
        lines.append("取り元の失敗: " + ", ".join(f"{f['id']}" for f in errs))
        lines.append("")
    if report["results"]:
        lines += ["| 商品名 | 一致 | ASIN | 取り込み | 記事 |", "|---|---|---|---|---|"]
        for r in report["results"]:
            h = r["hits"][0] if r["hits"] else None
            if h:
                lines.append(f"| {r['name'][:40]} | {h['score']} | {h['asin']} | "
                             f"{'済' if h['in_catalog'] else '外'} | {'有' if h['has_article'] else '無'} |")
            else:
                lines.append(f"| {r['name'][:40]} | – | {'失敗' if r.get('error') else '無し'} | | |")
    return "\n".join(lines) + "\n"


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--since-hours", type=float, default=12.5,
                    help="この時間以内の発表だけ見る (既定 12.5。workflow は 12 時間おき。PR TIMES の"
                         "ニュース用サイトマップは直近約 16 時間分しか載らない)")
    ap.add_argument("--max-search", type=int, default=20, help="1 回の SearchItems の上限 (既定 20)")
    ap.add_argument("--out", default="announce_shadow.json")
    ap.add_argument("--dry-run", action="store_true", help="Amazon を検索せず、発表と商品名だけ出す")
    args = ap.parse_args()
    report = run(args.since_hours, args.max_search, dry_run=args.dry_run)
    pathlib.Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    md = step_summary(report)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(md)
    print(md)
    # 取り元が全部読めなかったら赤にする (黙って 0 件で緑にしない, #4793)
    if report["feeds"] and all(f["error"] for f in report["feeds"]):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
