"""scripts/format_affiliate_click_report.py の単体テスト (navi-brain#165 T6)。

数字はすべて架空。名前の引き当ては tmp_path に作った小さなリポジトリで確かめる。
"""
from __future__ import annotations

import json
from datetime import date

import pytest

from scripts import format_affiliate_click_report as f

ART = "/products/b0bsh34yr8/"


def click(d, path, slot, network="amazon", n=1, asin="B0BSH34YR8"):
    return {"date": d, "pagePath": path, "slot": slot, "network": network, "asin": asin,
            "eventCount": n}


def event(d, path, name, n=1):
    return {"date": d, "pagePath": path, "eventName": name, "eventCount": n}


META = {"range": {"start": "2026-09-19", "end": "2026-11-13"}, "warnings": [],
        "fetched_at": "2026-11-15T00:30:00+00:00"}


# ---------------------------------------------------------------- ページ種類・slot 群

@pytest.mark.parametrize("path,kind", [
    ("/", "トップ"),
    ("/products/b0bsh34yr8/", "記事"),
    ("/products/B0BSH34YR8/", "記事"),
    ("/ranking/", "ランキング"),
    ("/cospa/", "予算別"),
    ("/deals/", "おトク"),
    ("/price/", "値下げ"),
    ("/diagnosis/", "診断"),
    ("/toys-age-0/", "年齢別"),
    ("/toys-age-6/", "年齢別"),
    ("/xmas-age-0/", "クリスマス"),
    ("/xmas-age-6/", "クリスマス"),
    ("/montessori-toys/", "テーマ別"),
    ("/programming-toys/", "テーマ別"),
    ("/tags/0toshi/", "タグ"),
    ("/tags/0toshi/page/2/", "タグ"),
    ("/brands/artec/", "ブランド"),
    ("/about/", "その他"),
    ("/posts/", "その他"),
    ("(not set)", "その他"),
    ("", "その他"),
])
def test_page_type(path, kind):
    assert f.page_type(path) == kind


@pytest.mark.parametrize("slot,group", [
    ("buy-top", "記事の購入ボタン"), ("buy-mid-compare", "記事の購入ボタン"),
    ("buy-closing", "記事の購入ボタン"), ("buy-faq", "記事の購入ボタン"),
    ("buy-box", "記事の購入ボタン"),
    ("stock-table", "在庫表"),
    ("price-card", "価格カード"), ("price-card-search", "価格カード"),
    ("price-search-fallback", "価格カード"),
    ("sticky", "追従バー"),
    ("list-card", "特集・一覧"), ("list-card-thumb", "特集・一覧"), ("list-card-name", "特集・一覧"),
    ("ranking", "特集・一覧"), ("ranking-thumb", "特集・一覧"),
    ("diagnosis", "その他"), ("sale-notice", "その他"), ("body", "その他"),
    ("(not set)", "その他"),
])
def test_slot_group(slot, group):
    assert f.slot_group(slot) == group


def test_hugo_content_urls_match_page_types():
    """固定ページの url が page_type の想定どおりか (URL を変えたらここで気づく)。"""
    lookup_root = f.REPO_ROOT / "hugo" / "content"
    expect = {"ranking.md": "ランキング", "cospa.md": "予算別", "deals.md": "おトク",
              "diagnosis.md": "診断", "toys-age-0.md": "年齢別", "math-toys.md": "テーマ別",
              "xmas-age-3.md": "クリスマス"}
    for name, kind in expect.items():
        fm = f._front_matter((lookup_root / name).read_text(encoding="utf-8"))
        url = fm.get("url") or f"/{name[:-3]}/"
        assert f.page_type(url) == kind, name
    assert (lookup_root / "price" / "_index.md").is_file()


# ---------------------------------------------------------------- 週の窓

def test_week_windows_align_to_end_and_skip_partial():
    wins = f.week_windows(date(2026, 9, 19), date(2026, 11, 13), 8)
    assert len(wins) == 8
    assert wins[-1] == (date(2026, 11, 7), date(2026, 11, 13))
    assert wins[0] == (date(2026, 9, 19), date(2026, 9, 25))
    # 28 日しか無ければ 4 週まで
    assert len(f.week_windows(date(2026, 10, 17), date(2026, 11, 13), 8)) == 4
    assert f.week_windows(date(2026, 11, 10), date(2026, 11, 13), 8) == []


# ---------------------------------------------------------------- 読み込み

def test_load_rows_splits_kinds(tmp_path):
    p = tmp_path / "rows.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in [
        click("2026-11-10", ART, "buy-top"), event("2026-11-10", ART, "page_view")]) + "\n\n",
        encoding="utf-8")
    clicks, events, nav = f.load_rows(p)
    assert len(clicks) == 1 and len(events) == 1 and nav == []


META_NAV = {"range": {"start": "2026-11-05", "end": "2026-12-02"}, "warnings": [],
            "fetched_at": "2026-12-03T00:30:00+00:00"}


def nav_row(d, path, name, slot, n=1):
    return {"date": d, "pagePath": path, "eventName": name, "slot": slot, "eventCount": n}


def test_load_rows_splits_nav_from_events_and_clicks(tmp_path):
    p = tmp_path / "rows.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in [
        click("2026-11-10", ART, "buy-top", n=4),
        event("2026-11-10", ART, "page_view"),
        nav_row("2026-11-26", "/", "nav_click", "hero:diagnosis", 3),
        nav_row("2026-11-26", "/", "home_section_view", "sec:latest", 2)]) + "\n",
        encoding="utf-8")
    clicks, events, nav = f.load_rows(p)
    assert [r["slot"] for r in clicks] == ["buy-top"]
    assert [r["eventName"] for r in events] == ["page_view"]
    assert sorted(r["eventName"] for r in nav) == ["home_section_view", "nav_click"]


def test_nav_rows_do_not_change_affiliate_click_totals():
    clicks = [click("2026-11-10", ART, "buy-top", n=5), click("2026-11-11", "/", "sticky", n=2)]
    events = [event("2026-11-10", ART, "page_view", 70)]
    nav = [nav_row("2026-11-12", "/", "nav_click", "hero:diagnosis", 9),
           nav_row("2026-11-12", ART, "nav_click", "menu:home", 4)]
    base = f.build_report(clicks, events, META)
    with_nav = f.build_report(clicks, events, META, nav=nav)
    for h in ("### (a)", "### (b)", "### (c)", "### (d)", "### (e)"):
        assert _rows_of(base, h) == _rows_of(with_nav, h), h
    assert _rows_of(with_nav, "### (b)")[-1] == "| **計** | 7 | 0 | 0 | 7 |"


# ---------------------------------------------------------------- 表の中身

def _rows_of(md: str, heading: str) -> list[str]:
    part = md.split(heading, 1)[1].split("\n### ", 1)[0]
    return [ln for ln in part.splitlines() if ln.startswith("| ") and "---" not in ln]


def test_report_tables():
    clicks = [
        click("2026-11-10", ART, "buy-top", n=5),
        click("2026-11-11", ART, "price-card", "rakuten", n=2),
        click("2026-11-11", "/ranking/", "ranking", "rakuten", n=3),
        click("2026-11-12", "/diagnosis/", "diagnosis", n=1),
        click("2026-11-12", "/deals/", "(not set)", "amazon", n=1),
        click("2026-09-20", ART, "buy-box", n=7),  # 推移にだけ入る (28 日より前)
    ]
    events = [
        event("2026-11-10", ART, "page_view", 70),
        event("2026-11-11", "/ranking/", "page_view", 30),
        event("2026-11-12", "/diagnosis/", "diagnosis_start", 10),
        event("2026-11-12", "/diagnosis/", "diagnosis_complete", 4),
        event("2026-11-12", "/diagnosis/", "m_sticky_tap", 99),  # 数えない
        event("2026-09-20", ART, "page_view", 100),
    ]
    md = f.build_report(clicks, events, META, name_of=lambda p: "テスト玩具" if p == ART else None)

    assert "**2026-10-17〜2026-11-13**" in md
    assert "`affiliate_click` だけを数える" in md
    assert "`buy-box` / `body`" in md

    top = _rows_of(md, "### (a)")
    assert top[1] == f"| `{ART}` | テスト玩具 | 記事 | 7 | 70 | 10.0% |"
    assert top[2] == "| `/ranking/` | — | ランキング | 3 | 30 | 10.0% |"
    assert top[-1].startswith("| `/diagnosis/` |") and top[-1].endswith("| 0 | — |")

    sn = _rows_of(md, "### (b)")
    assert sn[0] == "| slot | amazon | rakuten | yahoo | 計 |"
    assert "| `buy-top` | 5 | 0 | 0 | 5 |" in sn
    assert "| `(not set)` | 1 | 0 | 0 | 1 |" in sn
    assert sn[-1] == "| **計** | 7 | 5 | 0 | 12 |"

    pts = _rows_of(md, "### (c)")
    assert pts[1] == "| 記事 | `buy-top` | 5 | 71.4% |"
    assert "| おトク | `(not set)` | 1 | 100.0% |" in pts
    assert [r.split(" | ")[0] for r in pts[1:]] == ["| 記事", "| 記事", "| ランキング", "| おトク", "| 診断"]

    wk = _rows_of(md, "### (d)")
    assert len(wk) == 1 + 8
    assert wk[-1].startswith("| 11-07〜11-13 ※ | 5 | 0 | 2 | 0 | 3 | 2 | 12 | 7 | 70 | 10.0% |")
    assert wk[1] == "| 09-19〜09-25 | 7 | 0 | 0 | 0 | 0 | 0 | 7 | 7 | 100 | 7.0% |"

    dg = _rows_of(md, "### (e)")
    assert dg[1:] == ["| diagnosis_start | 10 | — |",
                      "| diagnosis_complete | 4 | 40.0% |",
                      "| affiliate_click (slot=diagnosis) | 1 | 25.0% |"]


def test_nav_tables():
    nav = [
        nav_row("2026-11-26", "/", "nav_click", "hero:diagnosis", 5),
        nav_row("2026-11-26", ART, "nav_click", "hero:diagnosis", 2),
        nav_row("2026-11-27", "/ranking/", "nav_click", "menu:ranking", 9),
        nav_row("2026-11-27", ART, "nav_click", "menu:ranking", 12),
        nav_row("2026-11-27", "/cospa/", "nav_click", "menu:ranking", 3),
        nav_row("2026-11-27", "/deals/", "nav_click", "menu:ranking", 1),
        nav_row("2026-11-27", "/", "home_section_view", "sec:latest", 40),
        nav_row("2026-11-27", "/", "home_section_view", "sec:spotlight", 90),
        nav_row("2026-11-27", ART, "home_section_view", "sec:latest", 7),  # トップ以外は数えない
        nav_row("2026-09-01", "/", "home_section_view", "sec:latest", 100),  # 期間外
    ]
    events = [event("2026-11-26", "/", "page_view", 100), event("2026-11-27", "/", "page_view", 100),
              event("2026-11-27", ART, "page_view", 500)]
    md = f.build_report([], events, META_NAV, nav=nav)

    fr = _rows_of(md, "### (f)")
    assert fr[0] == "| slot | 件数 | 押されたページの種類 |"
    # 件数の多い順。内訳は上位 3 種類 (同数は PAGE_TYPES の順)
    assert fr[1] == "| `menu:ranking` | 25 | 記事 12・ランキング 9・予算別 3 |"
    assert fr[2] == "| `hero:diagnosis` | 7 | トップ 5・記事 2 |"

    gr = _rows_of(md, "### (g)")
    assert gr[0] == "| 区画 | 到達 | 到達率 | 備考 |"
    assert [r.split(" | ")[0] for r in gr[1:]] == [
        f"| `sec:{s}`" for s in f.HOME_SECTIONS]
    assert gr[1] == "| `sec:last-diagnosis` | 0 | 0.0% | 条件付き（再訪者のみ表示） |"
    assert gr[2] == "| `sec:spotlight` | 90 | 90.0% |  |"
    assert gr[3] == "| `sec:xmas` | 0 | 0.0% | 時期のみ表示（10/1〜12/25） |"
    assert gr[4] == "| `sec:characters` | 0 | 0.0% |  |"
    assert gr[5] == "| `sec:new-arrivals` | 0 | 0.0% |  |"
    assert gr[7] == "| `sec:latest` | 40 | 40.0% |  |"
    assert gr[11] == "| `sec:recently-viewed` | 0 | 0.0% | 条件付き（再訪者のみ表示） |"
    assert gr[12] == "| `sec:reco` | 0 | 0.0% | 条件付き（再訪者のみ表示） |"
    # 分母はトップの home_section_view が最初に出た日 (11-27) から。11-26 の nav_click では決めない
    assert "トップ (`/`) の page_view 100（2026-11-27 以降）" in md


def test_home_section_denominator_starts_with_first_nav_day():
    """計測前の日の page_view を分母に混ぜない (導入直後に到達率が低く出ないように)。"""
    nav = [nav_row("2026-11-27", "/", "home_section_view", "sec:spotlight", 50)]
    events = [event("2026-11-20", "/", "page_view", 300),  # 計測前
              event("2026-11-26", "/", "page_view", 300),  # 計測前 (配信が遅れた日)
              event("2026-11-27", "/", "page_view", 100)]
    md = f.build_report([], events, META_NAV, nav=nav)
    gr = _rows_of(md, "### (g)")
    assert gr[2] == "| `sec:spotlight` | 50 | 50.0% |  |"
    assert "トップ (`/`) の page_view 100（2026-11-27 以降）" in md


def test_home_section_denominator_ignores_other_nav_dates():
    """分母の開始日は トップの home_section_view で決める。別ページの nav_click や
    "(other)" の日付に引っ張られない。"""
    nav = [nav_row("2026-11-25", "/posts/a/", "nav_click", "header:ranking", 5),  # 別ページ
           nav_row("(other)", "/", "home_section_view", "sec:spotlight", 1),
           nav_row("2026-11-27", "/", "home_section_view", "sec:spotlight", 50)]
    events = [event("2026-11-25", "/", "page_view", 300),
              event("2026-11-27", "/", "page_view", 100)]
    md = f.table_home_sections(nav, events)
    assert "トップ (`/`) の page_view 100（2026-11-27 以降）" in md[-1]


def test_nav_tables_top_30_limit():
    nav = [nav_row("2026-11-26", "/", "nav_click", f"s:{i:02d}", i + 1) for i in range(40)]
    md = f.build_report([], [], META_NAV, nav=nav)
    assert len(_rows_of(md, "### (f)")) == 1 + 30


def test_no_nav_rows_says_no_data_yet():
    md = f.build_report([click("2026-11-10", ART, "buy-top")], [], META)
    assert "まだデータがありません（計測を入れた日: 2026-11-25 以降）" in md
    assert "### (f)(g)" in md
    assert "### (f) " not in md and "### (g) " not in md
    # 期間外の nav だけでも同じ
    md2 = f.build_report([], [], META, nav=[nav_row("2026-09-01", "/", "nav_click", "x")])
    assert "まだデータがありません" in md2


def test_unknown_network_gets_its_own_column():
    md = f.build_report([click("2026-11-10", ART, "body", "(not set)")], [], META)
    assert "| slot | amazon | rakuten | yahoo | (not set) | 計 |" in md


def test_top_limit_and_pipe_escape():
    clicks = [click("2026-11-10", f"/products/b0000000{i:02d}/", "buy-top", n=100 - i)
              for i in range(25)]
    md = f.build_report(clicks, [], META, top=20, name_of=lambda p: "A|B")
    top = _rows_of(md, "### (a)")
    assert len(top) == 1 + 20
    assert "A\\|B" in top[1]


def test_empty_data():
    md = f.build_report([], [], META)
    assert "この期間のデータはありません" in md
    assert "(クリックなし)" in md
    dg = _rows_of(md, "### (e)")
    assert dg[1] == "| diagnosis_start | 0 | — |"
    assert "| — |" in dg[2]


def test_warnings_are_shown():
    meta = dict(META, warnings=["イベントの取得: GA4 のしきい値が適用されている"])
    assert "⚠️ イベントの取得: GA4 のしきい値" in f.build_report([], [], meta)


def test_short_fetch_has_no_trend():
    meta = dict(META, range={"start": "2026-11-11", "end": "2026-11-13"})
    md = f.build_report([click("2026-11-12", ART, "buy-top")], [], meta)
    assert "(7 日に満たないため推移なし)" in md
    assert "**2026-11-11〜2026-11-13**" in md


# ---------------------------------------------------------------- 名前

def test_name_lookup(tmp_path):
    (tmp_path / "data" / "articles").mkdir(parents=True)
    (tmp_path / "data" / "products").mkdir(parents=True)
    (tmp_path / "hugo" / "content").mkdir(parents=True)
    arts = tmp_path / "data" / "articles"
    (arts / "2026-05-01-B0BSH34YR8.json").write_text(
        json.dumps({"title": "古い", "product": {"name": "古い名前"}}), encoding="utf-8")
    (arts / "2026-11-01-B0BSH34YR8.json").write_text(
        json.dumps({"title": "新しい記事", "product": {"name": "新しい名前"}}), encoding="utf-8")
    (tmp_path / "data" / "products" / "B000000001.json").write_text(
        json.dumps({"title": "商品だけ"}), encoding="utf-8")
    (tmp_path / "hugo" / "content" / "cospa.md").write_text(
        '---\ntitle: "予算別おすすめ"\nurl: /cospa/\n---\n本文', encoding="utf-8")
    (tmp_path / "hugo" / "content" / "diagnosis.md").write_text(
        "---\ntitle: '診断'\n---\n", encoding="utf-8")
    (tmp_path / "hugo" / "content" / "price").mkdir()
    (tmp_path / "hugo" / "content" / "price" / "_index.md").write_text(
        '---\ntitle: "値下がり中"\n---\n', encoding="utf-8")
    lookup = f.build_name_lookup(tmp_path)
    assert lookup("/products/b0bsh34yr8/") == "新しい名前"
    assert lookup("/products/b000000001/") == "商品だけ"
    assert lookup("/products/b000000002/") is None
    assert lookup("/cospa/") == "予算別おすすめ"
    assert lookup("/diagnosis/") == "診断"
    assert lookup("/price/") == "値下がり中"
    assert lookup("/tags/x/") is None


def test_main_writes_markdown(tmp_path):
    rows = tmp_path / "rows.jsonl"
    rows.write_text(json.dumps(click("2026-11-10", ART, "buy-top")) + "\n", encoding="utf-8")
    meta = tmp_path / "meta.json"
    meta.write_text(json.dumps(META), encoding="utf-8")
    out = tmp_path / "report.md"
    assert f.main(["--rows", str(rows), "--meta", str(meta), "--out", str(out),
                   "--repo-root", str(tmp_path)]) == 0
    raw = out.read_bytes()
    assert b"\r\n" not in raw
    assert "### (a)" in raw.decode("utf-8")


def test_main_passes_nav_rows(tmp_path):
    rows = tmp_path / "rows.jsonl"
    rows.write_text("\n".join(json.dumps(r) for r in [
        click("2026-11-10", ART, "buy-top"),
        nav_row("2026-11-12", "/", "nav_click", "hero:posts", 2)]) + "\n", encoding="utf-8")
    meta = tmp_path / "meta.json"
    meta.write_text(json.dumps(META), encoding="utf-8")
    out = tmp_path / "report.md"
    assert f.main(["--rows", str(rows), "--meta", str(meta), "--out", str(out),
                   "--repo-root", str(tmp_path)]) == 0
    assert "`hero:posts`" in out.read_text(encoding="utf-8")
