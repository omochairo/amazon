"""check_asset_delivery.py の unit tests (#5260).

成り立つべき条件:
  1. **2026-08-15 に本番で実測した壊れ方をそのまま食わせて鳴る**こと。
     すなわち `/assets/**` の 404 に `Cache-Control: max-age=31536000` が付く状態
     (HTML の 404 は max-age=300 なので、指紋付きパス向けの長期キャッシュ設定が
     404 にも当たっていた)。
  2. 直った状態 = 404 の max-age が短い、では鳴らない。導入日に鳴りっぱなしに
     しない。
  3. 「プレーンは 404 / キャッシュバスター付きなら 200」という報告そのものの形を
     `edge_stale` として区別できること (実体はあるので purge で直る)。
  4. 判定不能 (ページが取れない / 指紋付きアセットが 1 本も無い) を ok に潰さない。
  5. Hugo の --minify は属性のクォートを落とす。実際の本番 HTML の形
     (`href=/assets/css/stylesheet.<hash>.css`) から抽出できること。
"""
from __future__ import annotations

import datetime as dt

import pytest

from scripts.check_asset_delivery import (
    DEFAULT_MAX_NEGATIVE_TTL,
    FetchError,
    Probe,
    check,
    check_asset,
    check_negative_ttl,
    extract_assets,
    missing_asset_url,
    parse_max_age,
    render_body,
    title_for,
)

PAGE = "https://navi.omcha.jp/"
CSS = ("https://navi.omcha.jp/assets/css/stylesheet."
       "446a90604895a09b1b7bab4bf8c109def2be79de642567db32bcaf8cba762a40.css")
JS = ("https://navi.omcha.jp/js/compare.min."
      "cab3bb97838ec902d630995e07393b36d5a2afc393f424ab1ea2ce3c4b3b17cc.js")

# 2026-08-15 の本番 HTML から写した形 (Hugo --minify: クォート無し属性が混ざる)。
REAL_HTML = (
    "<link rel=canonical href=https://navi.omcha.jp/>"
    f'<link crossorigin=anonymous href={CSS} integrity="sha256-RGqQ" '
    'rel="preload stylesheet" as=style>'
    "<link rel=icon href=https://navi.omcha.jp/favicon.ico>"
    f"<script src={JS} defer></script>"
    "<script src=https://www.googletagmanager.com/gtag/js?id=G-D8ZQX1BT20></script>"
)


def css_ok(body: str = "body{color:red}") -> Probe:
    return Probe(200, {"Content-Type": "text/css; charset=utf-8",
                       "Cache-Control": "max-age=31536000"}, body)


def html_ok(body: str = REAL_HTML) -> Probe:
    return Probe(200, {"Content-Type": "text/html; charset=utf-8",
                       "Cache-Control": "max-age=600"}, body)


def sticky_404() -> Probe:
    """2026-08-15 実測: 指紋付きパスの 404 に 1 年の max-age が付いていた。"""
    return Probe(404, {"Content-Type": "text/html; charset=utf-8",
                       "Cache-Control": "max-age=31536000",
                       "cf-cache-status": "HIT"}, "not found")


def healthy_404() -> Probe:
    """直った状態: HTML の 404 と同じ短さ (実測 max-age=300)。"""
    return Probe(404, {"Content-Type": "text/html; charset=utf-8",
                       "Cache-Control": "max-age=300"}, "not found")


def fetcher(table, default=None):
    """URL → Probe の辞書で fetch を差し替える。未登録は default か FetchError。"""

    def fetch(url: str) -> Probe:
        if url in table:
            value = table[url]
            if isinstance(value, Exception):
                raise value
            return value
        if default is not None:
            return default
        raise FetchError(f"未登録の URL: {url}")

    return fetch


# --- 抽出 -------------------------------------------------------------------

def test_extracts_assets_from_minified_production_html():
    assert extract_assets(REAL_HTML, PAGE) == [CSS, JS]


def test_cross_origin_and_unfingerprinted_are_ignored():
    html = (
        "<script src=https://www.googletagmanager.com/gtag/js?id=G-X></script>"
        "<link rel=icon href=/favicon.ico>"
        "<link rel=stylesheet href=/assets/css/plain.css>"
    )
    assert extract_assets(html, PAGE) == []


def test_non_hex_suffix_is_not_a_fingerprint():
    html = '<link rel=stylesheet href="/assets/css/a.' + "z" * 64 + '.css">'
    assert extract_assets(html, PAGE) == []


def test_relative_url_is_absolutized():
    digest = "a1" * 32
    html = f'<link rel=stylesheet href="/assets/css/a.{digest}.css">'
    assert extract_assets(html, PAGE) == [
        f"https://navi.omcha.jp/assets/css/a.{digest}.css"
    ]


def test_probe_url_lives_in_the_same_directory_as_a_real_asset():
    url = missing_asset_url([CSS], PAGE)
    assert url.startswith("https://navi.omcha.jp/assets/css/")
    assert url.endswith(".css")
    assert url != CSS


def test_probe_url_works_without_any_known_asset():
    assert missing_asset_url([], PAGE).startswith("https://navi.omcha.jp/assets/css/")


# --- Cache-Control ----------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("max-age=31536000", 31536000),
    ("public, max-age=300, immutable", 300),
    ("no-store", 0),
    ("no-cache, max-age=31536000", 0),  # no-cache が勝つ
    ("", None),
    ("public", None),
])
def test_parse_max_age(value, expected):
    assert parse_max_age(value) == expected


# --- R4: 焼き付きの回帰ガード ----------------------------------------------

def test_measured_one_year_negative_ttl_fires():
    row = check_negative_ttl("https://navi.omcha.jp/assets/css/nope.css",
                             fetcher({}, default=sticky_404()),
                             DEFAULT_MAX_NEGATIVE_TTL)
    assert row["status"] == "sticky_404"
    assert row["ttl"] == 31536000


def test_short_negative_ttl_does_not_fire():
    row = check_negative_ttl("https://navi.omcha.jp/assets/css/nope.css",
                             fetcher({}, default=healthy_404()),
                             DEFAULT_MAX_NEGATIVE_TTL)
    assert row["status"] == "ok"


def test_404_without_cache_control_does_not_fire():
    probe = Probe(404, {"Content-Type": "text/html"}, "not found")
    row = check_negative_ttl("https://navi.omcha.jp/assets/css/nope.css",
                             fetcher({}, default=probe), DEFAULT_MAX_NEGATIVE_TTL)
    assert row["status"] == "ok"


def test_probe_url_returning_200_is_abnormal():
    row = check_negative_ttl("https://navi.omcha.jp/assets/css/nope.css",
                             fetcher({}, default=css_ok()), DEFAULT_MAX_NEGATIVE_TTL)
    assert row["status"] == "unexpected_200"


# --- R1〜R3: 参照アセット ---------------------------------------------------

def test_served_css_is_ok():
    assert check_asset(CSS, fetcher({CSS: css_ok()}))["status"] == "ok"


def test_html_body_under_fingerprint_url_is_caught():
    row = check_asset(CSS, fetcher({CSS: html_ok("<html>404</html>")}))
    assert row["status"] == "html_body"


def test_plain_404_but_cache_busted_200_is_edge_stale():
    busted = f"{CSS}?cb=asset-delivery-monitor"
    row = check_asset(CSS, fetcher({CSS: sticky_404(), busted: css_ok()}))
    assert row["status"] == "edge_stale"


def test_404_on_both_attempts_is_missing():
    busted = f"{CSS}?cb=asset-delivery-monitor"
    row = check_asset(CSS, fetcher({CSS: sticky_404(), busted: sticky_404()}))
    assert row["status"] == "missing"


# --- 全体 -------------------------------------------------------------------

def test_healthy_production_shape_does_not_fire():
    result = check([PAGE], fetcher({PAGE: html_ok(), CSS: css_ok(), JS: css_ok()},
                                   default=healthy_404()))
    assert result["status"] == "ok"
    assert [r["status"] for r in result["assets"]] == ["ok", "ok"]


def test_long_negative_ttl_fires_even_when_all_assets_are_served():
    """#5260 の本丸。障害が起きていない平常時でも設定の戻りを検出する。"""
    result = check([PAGE], fetcher({PAGE: html_ok(), CSS: css_ok(), JS: css_ok()},
                                   default=sticky_404()))
    assert result["status"] == "broken"
    assert result["negative"]["status"] == "sticky_404"
    assert title_for(result) == (
        "[delivery] アセットの 404 が長期キャッシュされる設定になっています"
    )


def test_unfetchable_page_is_unreachable():
    result = check([PAGE], fetcher({PAGE: FetchError("ConnectionError")},
                                   default=healthy_404()))
    assert result["status"] == "unreachable"
    assert result["page_errors"][0]["status"] == "unreachable"


def test_page_without_fingerprinted_assets_is_not_collapsed_to_ok():
    result = check([PAGE], fetcher({PAGE: html_ok("<html><body>hi</body></html>")},
                                   default=healthy_404()))
    assert result["status"] == "unreachable"
    assert result["page_errors"][0]["status"] == "no_assets"


def test_shared_stylesheet_is_probed_once():
    page2 = "https://navi.omcha.jp/ranking/"
    calls = []

    def fetch(url: str) -> Probe:
        calls.append(url)
        if url in (PAGE, page2):
            return html_ok()
        if url in (CSS, JS):
            return css_ok()
        return healthy_404()

    check([PAGE, page2], fetch)
    assert calls.count(CSS) == 1


def test_body_carries_marker_and_headline():
    result = check([PAGE], fetcher({PAGE: html_ok(), CSS: css_ok(), JS: css_ok()},
                                   default=sticky_404()))
    body = render_body(result, dt.datetime(2026, 8, 15, tzinfo=dt.timezone.utc))
    assert "<!-- asset-delivery-monitor -->" in body
    assert "sticky_404" in body
    assert "#5260" in body
