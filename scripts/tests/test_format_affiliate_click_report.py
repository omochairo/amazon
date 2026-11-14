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
    clicks, events = f.load_rows(p)
    assert len(clicks) == 1 and len(events) == 1


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
