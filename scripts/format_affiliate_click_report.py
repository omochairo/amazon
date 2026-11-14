"""format_affiliate_click_report.py

scripts/fetch_ga4_affiliate_clicks.py が書き出した行 (JSONL) から、購入リンクの
クリックの週次の表 (Markdown) を作る (navi-brain#165 T6)。API は呼ばない。

出力は private の navi-brain の issue のコメントにだけ出す。public のリポジトリ・
Actions のログ・job summary には出さない (workflow 23 の冒頭を参照)。

表:
  (a) クリックの多いページ (上位 --top)
  (b) 押された位置 (slot × network)
  (c) ページ種類 × slot
  (d) 週ごとの推移 (直近 --trend-weeks 週。主要な slot 群と記事ページのクリック率)
  (e) 診断の流れ (diagnosis_start → diagnosis_complete → slot=diagnosis のクリック)

(a)〜(c)・(e) は期間の終わりから --summary-days 日、(d) は取得した期間ぜんぶを
7 日ずつ区切る (期間の終わりにそろえる。7 日に満たない端の週は出さない)。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from collections import defaultdict
from datetime import date, timedelta
from typing import Any, Iterable

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
CLICK_EVENT = "affiliate_click"
# 記事の冒頭・中盤・まとめの後・FAQ の後に購入ボタンの箱を置いた日 (#11672)
BUY_BOX_LAUNCH = date(2026, 11, 13)

NETWORKS = ("amazon", "rakuten", "yahoo")

PAGE_TYPES = ("記事", "ランキング", "予算別", "おトク", "値下げ", "診断", "年齢別",
              "テーマ別", "タグ", "ブランド", "トップ", "その他")
_ARTICLE_RE = re.compile(r"^/products/([a-z0-9]{10})/", re.IGNORECASE)

SLOT_GROUPS = ("記事の購入ボタン", "在庫表", "価格カード", "追従バー", "特集・一覧", "その他")


# ---------------------------------------------------------------- 分類

def page_type(path: str) -> str:
    """pagePath からページの種類を決める (URL は hugo/content/ と config.toml による)。"""
    p = path or ""
    if p == "/":
        return "トップ"
    if _ARTICLE_RE.match(p):
        return "記事"
    first = p.strip("/").split("/", 1)[0]
    if first == "ranking":
        return "ランキング"
    if first == "cospa":
        return "予算別"
    if first == "deals":
        return "おトク"
    if first == "price":
        return "値下げ"
    if first == "diagnosis":
        return "診断"
    if re.fullmatch(r"toys-age-\d+", first):
        return "年齢別"
    if first.endswith("-toys"):
        return "テーマ別"
    if first == "tags":
        return "タグ"
    if first == "brands":
        return "ブランド"
    return "その他"


def slot_group(slot: str) -> str:
    """slot を推移の表の群にまとめる。

    buy-box は購入ボタンの箱の class から推測した slot (affiliate_click.js の
    後方互換)。記事の購入ボタンに入れる。
    """
    s = slot or ""
    if s.startswith("buy-"):
        return "記事の購入ボタン"
    if s == "stock-table":
        return "在庫表"
    if s.startswith("price-"):
        return "価格カード"
    if s == "sticky":
        return "追従バー"
    if s.startswith("list-card") or s.startswith("ranking"):
        return "特集・一覧"
    return "その他"


# ---------------------------------------------------------------- 読み込み

def load_rows(path: pathlib.Path) -> tuple[list[dict], list[dict]]:
    """JSONL を読み、(クリックの行, イベントの行) に分ける。"""
    clicks, events = [], []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        (events if "eventName" in r else clicks).append(r)
    return clicks, events


def _in(d: str, start: date, end: date) -> bool:
    try:
        x = date.fromisoformat(d)
    except (TypeError, ValueError):
        return False
    return start <= x <= end


def _sum(rows: Iterable[dict]) -> int:
    return sum(int(r.get("eventCount", 0)) for r in rows)


# ---------------------------------------------------------------- 名前

def _front_matter(text: str) -> dict[str, str]:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    out = {}
    for line in text[3:end].splitlines():
        m = re.match(r'''^(title|url):\s*["']?(.*?)["']?\s*$''', line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def build_name_lookup(repo_root: pathlib.Path):
    """pagePath → 表示名 の関数を返す。引けなければ None。

    記事は data/articles/ の最新の本文の商品名、無ければ data/products/ の商品名。
    固定ページは hugo/content/*.md の title。読むのは表に出る行の分だけ。
    """
    pages: dict[str, str] = {}
    content = repo_root / "hugo" / "content"
    if content.is_dir():
        # 直下の *.md と、section の _index.md (例: price/_index.md → /price/)
        for md in list(content.glob("*.md")) + list(content.glob("*/_index.md")):
            try:
                fm = _front_matter(md.read_text(encoding="utf-8"))
            except OSError:
                continue
            title = fm.get("title")
            if not title:
                continue
            stem = md.parent.name if md.name == "_index.md" else md.stem
            url = fm.get("url") or f"/{stem}/"
            pages[url] = title
    articles = repo_root / "data" / "articles"
    products = repo_root / "data" / "products"

    def lookup(path: str) -> str | None:
        m = _ARTICLE_RE.match(path or "")
        if not m:
            return pages.get(path)
        asin = m.group(1).upper()
        bodies = sorted(f for f in articles.glob(f"*-{asin}.json")) if articles.is_dir() else []
        for f in reversed(bodies):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            name = (d.get("product") or {}).get("name") or d.get("title")
            if name:
                return name
        pf = products / f"{asin}.json"
        if pf.is_file():
            try:
                d = json.loads(pf.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
            return d.get("title") or d.get("name")
        return None

    return lookup


# ---------------------------------------------------------------- 表

def _cell(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _rate(num: int, den: int) -> str:
    return f"{num / den * 100:.1f}%" if den else "—"


def _table(header: list[str], rows: list[list[Any]], align: str | None = None) -> list[str]:
    align = align or ("l" + "r" * (len(header) - 1))
    sep = ["---:" if a == "r" else "---" for a in align]
    out = ["| " + " | ".join(header) + " |", "| " + " | ".join(sep) + " |"]
    out += ["| " + " | ".join(_cell(c) for c in r) + " |" for r in rows]
    return out


def table_top_pages(clicks, events, top: int, name_of) -> list[str]:
    by_page: dict[str, int] = defaultdict(int)
    for r in clicks:
        by_page[r.get("pagePath", "")] += int(r.get("eventCount", 0))
    pv: dict[str, int] = defaultdict(int)
    for r in events:
        if r.get("eventName") == "page_view":
            pv[r.get("pagePath", "")] += int(r.get("eventCount", 0))
    ranked = sorted(by_page.items(), key=lambda kv: (-kv[1], kv[0]))[:top]
    if not ranked:
        return ["(クリックなし)"]
    rows = []
    for path, n in ranked:
        name = (name_of(path) if name_of else None) or "—"
        rows.append([f"`{path}`", name, page_type(path), n, pv.get(path, 0), _rate(n, pv.get(path, 0))])
    return _table(["ページ", "名前", "種類", "affiliate_click", "page_view", "クリック率"], rows,
                  align="lllrrr")


def table_slot_network(clicks) -> list[str]:
    grid: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    others: set[str] = set()
    for r in clicks:
        net = r.get("network", "")
        if net not in NETWORKS:
            others.add(net)
        grid[r.get("slot", "")][net] += int(r.get("eventCount", 0))
    if not grid:
        return ["(クリックなし)"]
    nets = list(NETWORKS) + sorted(others)
    rows = []
    for slot, cells in sorted(grid.items(), key=lambda kv: (-sum(kv[1].values()), kv[0])):
        rows.append([f"`{slot}`"] + [cells.get(n, 0) for n in nets] + [sum(cells.values())])
    totals = [sum(grid[s].get(n, 0) for s in grid) for n in nets]
    rows.append(["**計**"] + totals + [sum(totals)])
    return _table(["slot"] + nets + ["計"], rows)


def table_page_type_slot(clicks) -> list[str]:
    grid: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in clicks:
        grid[page_type(r.get("pagePath", ""))][r.get("slot", "")] += int(r.get("eventCount", 0))
    if not grid:
        return ["(クリックなし)"]
    rows = []
    for pt in PAGE_TYPES:
        if pt not in grid:
            continue
        total = sum(grid[pt].values())
        for slot, n in sorted(grid[pt].items(), key=lambda kv: (-kv[1], kv[0])):
            rows.append([pt, f"`{slot}`", n, _rate(n, total)])
    return _table(["ページ種類", "slot", "affiliate_click", "種類の中の割合"], rows, align="llrr")


def week_windows(start: date, end: date, weeks: int) -> list[tuple[date, date]]:
    """end にそろえた 7 日ずつの窓 (古い順)。start より前にはみ出す週は出さない。"""
    out = []
    for i in range(weeks):
        w_end = end - timedelta(days=7 * i)
        w_start = w_end - timedelta(days=6)
        if w_start < start:
            break
        out.append((w_start, w_end))
    return list(reversed(out))


def table_weekly(clicks, events, start: date, end: date, weeks: int) -> list[str]:
    wins = week_windows(start, end, weeks)
    if not wins:
        return ["(7 日に満たないため推移なし)"]
    rows = []
    for ws, we in wins:
        groups = {g: 0 for g in SLOT_GROUPS}
        article_clicks = 0
        for r in clicks:
            if not _in(r.get("date"), ws, we):
                continue
            n = int(r.get("eventCount", 0))
            groups[slot_group(r.get("slot", ""))] += n
            if page_type(r.get("pagePath", "")) == "記事":
                article_clicks += n
        article_pv = _sum(r for r in events
                          if r.get("eventName") == "page_view" and _in(r.get("date"), ws, we)
                          and page_type(r.get("pagePath", "")) == "記事")
        label = f"{ws:%m-%d}〜{we:%m-%d}"
        if ws <= BUY_BOX_LAUNCH <= we:
            label += " ※"
        rows.append([label] + [groups[g] for g in SLOT_GROUPS] + [sum(groups.values()),
                     article_clicks, article_pv, _rate(article_clicks, article_pv)])
    return _table(["週"] + list(SLOT_GROUPS) + ["計", "記事のクリック", "記事の page_view",
                                                 "記事のクリック率"], rows)


def table_diagnosis(clicks, events) -> list[str]:
    def ev(name):
        return _sum(r for r in events if r.get("eventName") == name
                    and page_type(r.get("pagePath", "")) == "診断")
    start = ev("diagnosis_start")
    complete = ev("diagnosis_complete")
    click = _sum(r for r in clicks if r.get("slot") == "diagnosis"
                 and page_type(r.get("pagePath", "")) == "診断")
    rows = [["diagnosis_start", start, "—"],
            ["diagnosis_complete", complete, _rate(complete, start)],
            ["affiliate_click (slot=diagnosis)", click, _rate(click, complete)]]
    return _table(["段", "件数", "前の段からの割合"], rows, align="lrr")


# ---------------------------------------------------------------- 全体

def build_report(clicks: list[dict], events: list[dict], meta: dict, *,
                 summary_days: int = 28, trend_weeks: int = 8, top: int = 20,
                 name_of=None) -> str:
    rng = meta.get("range") or {}
    fetched_start = date.fromisoformat(rng["start"])
    end = date.fromisoformat(rng["end"])
    s_start = max(fetched_start, end - timedelta(days=summary_days - 1))
    s_clicks = [r for r in clicks if _in(r.get("date"), s_start, end)]
    s_events = [r for r in events if _in(r.get("date"), s_start, end)]

    out = [
        "## 購入リンクのクリック (navi.omcha.jp)",
        "",
        f"- 期間: **{s_start}〜{end}** (日本時間・両端込み)。推移の表は {fetched_start}〜{end}",
        "- 数え方: `affiliate_click` だけを数える。追従バー (`m_sticky_tap`) とセール告知"
        " (`sale_notice_click`) は押すと `affiliate_click` も飛ぶので、それらは足さない",
        f"- {BUY_BOX_LAUNCH:%m-%d} より前の HTML (`data-cta-slot` の無いもの) のクリックは"
        " `buy-box` / `body` として数えられる。推移の表の ※ は記事の購入ボタンを入れた週",
        "- page_view と診断のイベントは hostName が navi.omcha.jp のものだけ",
    ]
    for w in meta.get("warnings") or []:
        out.append(f"- ⚠️ {w}")
    if not s_clicks and not s_events:
        out += ["", "この期間のデータはありません。"]
    out += ["", f"### (a) クリックの多いページ 上位 {top}", ""]
    out += table_top_pages(s_clicks, s_events, top, name_of)
    out += ["", "### (b) 押された位置 (slot × network)", ""]
    out += table_slot_network(s_clicks)
    out += ["", "### (c) ページ種類 × slot", ""]
    out += table_page_type_slot(s_clicks)
    out += ["", f"### (d) 週ごとの推移 (直近 {trend_weeks} 週)", "",
            "記事の購入ボタン = `buy-*`・`buy-box` / 価格カード = `price-*` / "
            "特集・一覧 = `list-card*`・`ranking*`。記事のクリック率 = 記事ページの"
            " affiliate_click ÷ 記事ページの page_view", ""]
    out += table_weekly(clicks, events, fetched_start, end, trend_weeks)
    out += ["", "### (e) 診断の流れ (診断ページの分)", ""]
    out += table_diagnosis(s_clicks, s_events)
    out += ["", f"<sub>fetched_at: {meta.get('fetched_at', '')}</sub>", ""]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--rows", required=True, help="fetch_ga4_affiliate_clicks.py の --out")
    p.add_argument("--meta", required=True, help="fetch_ga4_affiliate_clicks.py の --meta-out")
    p.add_argument("--out", required=True, help="Markdown の書き出し先")
    p.add_argument("--summary-days", type=int, default=28)
    p.add_argument("--trend-weeks", type=int, default=8)
    p.add_argument("--top", type=int, default=20)
    p.add_argument("--repo-root", default=str(REPO_ROOT),
                   help="記事・固定ページの名前を引くリポジトリの場所")
    args = p.parse_args(argv)

    clicks, events = load_rows(pathlib.Path(args.rows))
    meta = json.loads(pathlib.Path(args.meta).read_text(encoding="utf-8"))
    md = build_report(clicks, events, meta, summary_days=args.summary_days,
                      trend_weeks=args.trend_weeks, top=args.top,
                      name_of=build_name_lookup(pathlib.Path(args.repo_root)))
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="\n") as f:
        f.write(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
