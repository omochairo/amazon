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
  (f) サイト内の移動 (nav_click の slot ごと。navi-brain#213)
  (g) トップの区画の到達 (home_section_view ÷ トップの page_view)
  (h) クリスマスページ (/xmas-age-N/) の押された位置と、ページごとの記事へ・店へ (navi-brain#214)

(a)〜(c)・(e)〜(h) は期間の終わりから --summary-days 日、(d) は取得した期間ぜんぶを
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

# navi-brain#213: hugo/assets/js/nav_click.js が送るイベント (fetch 側の NAV_EVENTS と同じ)。
# 行は eventName を持つので、イベントの行と明示的に分ける (購入クリックに混ぜない)。
NAV_EVENTS = ("nav_click", "home_section_view")
# トップの区画 (index.html の data-nav-section) の上からの並び。条件付きは再訪者のみ表示
HOME_SECTIONS = ("last-diagnosis", "spotlight", "xmas", "characters", "new-arrivals",
                 "age-timeline", "latest", "score", "tags", "brands", "recently-viewed", "reco")
HOME_SECTIONS_CONDITIONAL = ("last-diagnosis", "recently-viewed", "reco")
# 時期だけ表示する区画 (クリスマスは 10/1〜12/25。index.html の $xmasSeason)
HOME_SECTIONS_SEASONAL = ("xmas",)
NAV_LAUNCH = "2026-11-25"
NAV_TOP = 30

# navi-brain#214: 作り直したクリスマス × 年齢ページ (hugo/layouts/partials/xmas_page.html・
# xmas_card.html) の slot。記事へは nav_click、店へは affiliate_click で飛ぶ。
XMAS_LAUNCH = "2026-11-26"
XMAS_NAV_SLOTS = (
    ("xmas-top3", "迷ったらこの 3 つ"),
    ("xmas-table", "比較表"),
    ("xmas-card-article", "カード → 記事"),
    ("xmas-card-open", "カードの「〜を見る」を開く"),
)
XMAS_CTA_SLOTS = (("xmas-card-shop", "カード → 店"),)
# 記事 (レビュー) へ進む slot。xmas-card-open はページ内でたたみを開くだけなので入れない
XMAS_TO_ARTICLE = ("xmas-top3", "xmas-table", "xmas-card-article")
_XMAS_PATH_RE = re.compile(r"^/xmas-age-(\d+)/$")

NETWORKS = ("amazon", "rakuten", "yahoo")

PAGE_TYPES = ("記事", "ランキング", "予算別", "おトク", "値下げ", "診断", "年齢別",
              "クリスマス", "テーマ別", "タグ", "ブランド", "トップ", "その他")
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
    if re.fullmatch(r"xmas-age-\d+", first):  # navi-brain#166 F10
        return "クリスマス"
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
    if s.startswith("list-card") or s.startswith("ranking") or s.startswith("xmas-"):
        return "特集・一覧"
    return "その他"


# ---------------------------------------------------------------- 読み込み

def load_rows(path: pathlib.Path) -> tuple[list[dict], list[dict], list[dict]]:
    """JSONL を読み、(クリックの行, イベントの行, サイト内の移動の行) に分ける。

    nav の行も eventName を持つので、NAV_EVENTS を先に見る (見ないと events に入る)。
    クリックの行は eventName を持たない。
    """
    clicks, events, nav = [], [], []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("eventName") in NAV_EVENTS:
            nav.append(r)
        elif "eventName" in r:
            events.append(r)
        else:
            clicks.append(r)
    return clicks, events, nav


def _in(d: str, start: date, end: date) -> bool:
    try:
        x = date.fromisoformat(d)
    except (TypeError, ValueError):
        return False
    return start <= x <= end


def _is_iso_date(d) -> bool:
    try:
        date.fromisoformat(str(d))
    except (TypeError, ValueError):
        return False
    return True


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


def _type_breakdown(counts: dict[str, int], top: int = 3) -> str:
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], PAGE_TYPES.index(kv[0])))[:top]
    return "・".join(f"{t} {n}" for t, n in ranked)


def table_nav_click(nav, top: int = NAV_TOP) -> list[str]:
    by_slot: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in nav:
        if r.get("eventName") != "nav_click":
            continue
        by_slot[r.get("slot", "")][page_type(r.get("pagePath", ""))] += int(r.get("eventCount", 0))
    if not by_slot:
        return ["(nav_click なし)"]
    ranked = sorted(by_slot.items(), key=lambda kv: (-sum(kv[1].values()), kv[0]))[:top]
    rows = [[f"`{slot}`", sum(c.values()), _type_breakdown(c)] for slot, c in ranked]
    return _table(["slot", "件数", "押されたページの種類"], rows, align="lrl")


def table_home_sections(nav, events) -> list[str]:
    views: dict[str, int] = defaultdict(int)
    for r in nav:
        if r.get("eventName") == "home_section_view" and r.get("pagePath") == "/":
            views[r.get("slot", "")] += int(r.get("eventCount", 0))
    # 分母は計測が始まった日以降の page_view だけにする。計測前の日の page_view を
    # 混ぜると、導入後の数週間は到達率が実際より低く出る。開始日は NAV_LAUNCH ではなく
    # トップの区画の到達 (home_section_view) が最初に現れた日（マージ・配信が遅れた分も除くため）。
    # 別のページの nav_click の日付や、ISO 日付でない値 ("(other)" 等) では決めない。
    nav_dates = [str(r.get("date")) for r in nav
                 if r.get("eventName") == "home_section_view" and r.get("pagePath") == "/"
                 and _is_iso_date(r.get("date"))]
    first = min(nav_dates) if nav_dates else None
    pv = _sum(r for r in events if r.get("eventName") == "page_view" and r.get("pagePath") == "/"
              and (first is None or str(r.get("date")) >= first))
    known = [f"sec:{s}" for s in HOME_SECTIONS]
    extra = sorted(k for k in views if k not in known)
    rows = []
    for slot in known + extra:
        name = slot[4:] if slot.startswith("sec:") else slot
        if name in HOME_SECTIONS_CONDITIONAL:
            note = "条件付き（再訪者のみ表示）"
        elif name in HOME_SECTIONS_SEASONAL:
            note = "時期のみ表示（10/1〜12/25）"
        else:
            note = ""
        rows.append([f"`{slot}`", views.get(slot, 0), _rate(views.get(slot, 0), pv), note])
    out = _table(["区画", "到達", "到達率", "備考"], rows, align="lrrl")
    since = f"（{first} 以降）" if first else ""
    return out + ["", f"到達率の分母 = トップ (`/`) の page_view {pv}{since}"]


def _xmas_first_day(clicks, nav) -> str | None:
    """作り直したクリスマスページの slot (xmas-*) が最初に現れた日。

    作り直す前のページ (slot は list-card*) の page_view を分母に混ぜないため。
    (g) と同じく、計測を入れた日ではなく実データに最初に出た日で決める (配信の遅れも除く)。
    集計の窓 (28 日) ではなく取得した期間ぜんぶの行で決める。窓の初日にクリックが
    0 件だっただけで、その日の page_view が分母から落ちないように。
    """
    days = [str(r.get("date")) for r in list(clicks) + list(nav)
            if str(r.get("slot", "")).startswith("xmas-")
            and _XMAS_PATH_RE.match(r.get("pagePath") or "")
            and _is_iso_date(r.get("date"))]
    return min(days) if days else None


def table_xmas(clicks, events, nav, first: str | None = None) -> list[str]:
    """first は _xmas_first_day を取得した期間ぜんぶで求めた日 (省略時は渡された行で求める)。"""
    first = first or _xmas_first_day(clicks, nav)
    if first is None:
        return [f"まだデータがありません（作り直したページの計測を入れた日: {XMAS_LAUNCH} 以降）"]

    def on_xmas(r) -> bool:
        return (bool(_XMAS_PATH_RE.match(r.get("pagePath") or "")) and _is_iso_date(r.get("date"))
                and str(r.get("date")) >= first)

    pv_by_page: dict[str, int] = defaultdict(int)
    for r in events:
        if r.get("eventName") == "page_view" and on_xmas(r):
            pv_by_page[r["pagePath"]] += int(r.get("eventCount", 0))
    nav_by_slot: dict[str, int] = defaultdict(int)
    article_by_page: dict[str, int] = defaultdict(int)
    for r in nav:
        if r.get("eventName") != "nav_click" or not on_xmas(r):
            continue
        n = int(r.get("eventCount", 0))
        nav_by_slot[r.get("slot", "")] += n
        if r.get("slot") in XMAS_TO_ARTICLE:
            article_by_page[r["pagePath"]] += n
    cta_by_slot: dict[str, int] = defaultdict(int)
    shop_by_page: dict[str, int] = defaultdict(int)
    for r in clicks:
        if not on_xmas(r):
            continue
        n = int(r.get("eventCount", 0))
        cta_by_slot[r.get("slot", "")] += n
        shop_by_page[r["pagePath"]] += n
    pv = sum(pv_by_page.values())

    rows = []
    for kind, known, counts in (("サイト内", XMAS_NAV_SLOTS, nav_by_slot),
                                ("店へ", XMAS_CTA_SLOTS, cta_by_slot)):
        names = {s for s, _ in known}
        for slot, label in known:
            rows.append([f"`{slot}`", label, kind, counts.get(slot, 0), _rate(counts.get(slot, 0), pv)])
        rest = sum(n for s, n in counts.items() if s not in names)
        if rest:
            label = "ヘッダー・ページ内のほかのリンク" if kind == "サイト内" else "ほかの購入リンク"
            rows.append(["そのほか", label, kind, rest, _rate(rest, pv)])
    out = _table(["slot", "位置", "行き先", "件数", "page_view あたり"], rows, align="lllrr")

    pages = sorted(set(pv_by_page) | set(article_by_page) | set(shop_by_page),
                   key=lambda p: int(_XMAS_PATH_RE.match(p).group(1)))
    prow = []
    for p in pages:
        a, s, v = article_by_page.get(p, 0), shop_by_page.get(p, 0), pv_by_page.get(p, 0)
        prow.append([f"`{p}`", v, a, _rate(a, v), s, _rate(s, v)])
    ta, ts = sum(article_by_page.values()), sum(shop_by_page.values())
    prow.append(["**計**", pv, ta, _rate(ta, pv), ts, _rate(ts, pv)])
    out += ["", "ページごと（記事へ = `xmas-top3`・`xmas-table`・`xmas-card-article`、"
            "店へ = そのページの affiliate_click ぜんぶ）", ""]
    out += _table(["ページ", "page_view", "記事へ", "記事へ率", "店へ", "店へ率"], prow)
    out += ["", f"分母 = クリスマスページの page_view {pv}（{first} 以降）。"
            "`xmas-card-open` はたたみを閉じるときも数える"]
    if not any(r.get("eventName") == "nav_click" for r in nav):
        out.append("⚠️ この期間の nav_click の行がありません（取得の失敗を含む）。サイト内の列は 0 になっています")
    return out


# ---------------------------------------------------------------- 全体

def build_report(clicks: list[dict], events: list[dict], meta: dict, *,
                 summary_days: int = 28, trend_weeks: int = 8, top: int = 20,
                 name_of=None, nav: list[dict] | None = None) -> str:
    rng = meta.get("range") or {}
    fetched_start = date.fromisoformat(rng["start"])
    end = date.fromisoformat(rng["end"])
    s_start = max(fetched_start, end - timedelta(days=summary_days - 1))
    s_clicks = [r for r in clicks if _in(r.get("date"), s_start, end)]
    s_events = [r for r in events if _in(r.get("date"), s_start, end)]
    s_nav = [r for r in (nav or []) if _in(r.get("date"), s_start, end)]

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
            "特集・一覧 = `list-card*`・`ranking*`・`xmas-*`。記事のクリック率 = 記事ページの"
            " affiliate_click ÷ 記事ページの page_view", ""]
    out += table_weekly(clicks, events, fetched_start, end, trend_weeks)
    out += ["", "### (e) 診断の流れ (診断ページの分)", ""]
    out += table_diagnosis(s_clicks, s_events)
    if s_nav:
        out += ["", "### (f) サイト内の移動 (nav_click)", "",
                f"slot ごとの件数の上位 {NAV_TOP}。ヘッダー = `menu:*`・年齢ピル/カテゴリ = "
                "`catnav:*`・トップの商品カードは `home:<区画>`", ""]
        out += table_nav_click(s_nav)
        out += ["", "### (g) トップの区画の到達 (home_section_view)", "",
                "区画の見出しが初めて画面に入った数 ÷ 同期間のトップの page_view", ""]
        out += table_home_sections(s_nav, s_events)
    else:
        out += ["", "### (f)(g) サイト内の移動・トップの区画の到達", "",
                f"まだデータがありません（計測を入れた日: {NAV_LAUNCH} 以降）"]
    out += ["", "### (h) クリスマスページ (`/xmas-age-*/`)", ""]
    out += table_xmas(s_clicks, s_events, s_nav, first=_xmas_first_day(clicks, nav or []))
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

    clicks, events, nav = load_rows(pathlib.Path(args.rows))
    meta = json.loads(pathlib.Path(args.meta).read_text(encoding="utf-8"))
    md = build_report(clicks, events, meta, summary_days=args.summary_days,
                      trend_weeks=args.trend_weeks, top=args.top,
                      name_of=build_name_lookup(pathlib.Path(args.repo_root)), nav=nav)
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="\n") as f:
        f.write(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
