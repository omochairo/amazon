"""#7958 (#7955 設計2 改訂版 B): 公式取説・あそびかたの URL を取る。

Coverage:
1. バンダイ HTML 解析 (parse_bandai_manual_list): 実測フィクスチャで
   exactly-1 / zero / default-list (署名つき manual.php 混入) / multi を判定
2. fetch_bandai_howto: 空 JAN で問い合わせない・1 件確定で pdf.php を記録・
   0 件/複数件/デフォルト一覧では記録しない・ネットワーク断/想定外シェイプはエラー
3. レゴ: 品番抽出 (末尾優先・ピース数等の除外) と非空 buildingInstructions 確認
   (2026-09-22 レビュー: status==200 and 数字 in text だけでは存在しない品番も
   誤検出する false positive だったため、実測フィクスチャで固定)
4. たまごっち: 系列キーワード一致 + ブランド (バンダイ) + アクセサリー語除外 + 200 確認
   (2026-09-22 レビュー: サードパーティ保護ケース等への誤爆を防ぐ)
5. ルーティング (route_adapter): たまごっちキーワード優先 / レゴ / バンダイ (JAN 必須) / 該当無し
6. write 側: 既存 steps/reviewed_by を消さずにマージ・30 日以内は再問い合わせしない
7. discover_target_asins
8. エラー処理 (process_asin): AdapterFetchError は書き込まず action="error"・
   既存 found レコードを一時エラーで not_found に格下げしない・main の exit code
"""
from __future__ import annotations

import json
import pathlib
import sys
from datetime import datetime, timedelta, timezone

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import fetch_official_howto as fh  # noqa: E402

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"


def _read_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# --- 1. parse_bandai_manual_list ------------------------------------------

def test_parse_single_result_returns_pdf_url():
    html = _read_fixture("bandai_manuals_single.html")
    r = fh.parse_bandai_manual_list(html)
    assert r["status"] == "single"
    assert len(r["results"]) == 1
    assert r["results"][0]["url"] == "https://toy.bandai.co.jp/manuals/pdf.php?id=2852540"
    assert r["results"][0]["name"] == "DIGIVICE Ver.REVIVAL 石田ヤマトカラー"


def test_parse_zero_results():
    html = _read_fixture("bandai_manuals_zero.html")
    r = fh.parse_bandai_manual_list(html)
    assert r["status"] == "zero"
    assert r["results"] == []


def test_parse_default_listing_is_not_single_or_zero():
    # jan_code が空/不一致のとき返る、直近 20 件の既定一覧 (件数で判定する)。
    html = _read_fixture("bandai_manuals_default_list.html")
    r = fh.parse_bandai_manual_list(html)
    assert r["status"] == "default_listing"
    assert len(r["results"]) == 20


def test_parse_single_result_normalizes_signed_manual_php_url():
    # 実測: 「ちょうど 1 件」でも manual.php (署名つき) で返る商品がある
    # (B0HDB8KG6G / B0HDB6X1CN)。id を抜いて安定な pdf.php に正規化する。
    html = _read_fixture("bandai_manuals_single_signed.html")
    r = fh.parse_bandai_manual_list(html)
    assert r["status"] == "single"
    assert r["results"][0]["url"] == "https://toy.bandai.co.jp/manuals/pdf.php?id=2812202"
    assert "manual.php" not in r["results"][0]["url"]
    assert "sig=" not in r["results"][0]["url"]


def test_parse_multi_result_not_single():
    html = _read_fixture("bandai_manuals_multi.html")
    r = fh.parse_bandai_manual_list(html)
    assert r["status"] == "multi"
    assert len(r["results"]) == 2


def test_parse_excludes_header_row():
    html = _read_fixture("bandai_manuals_single.html")
    r = fh.parse_bandai_manual_list(html)
    names = [e["name"] for e in r["results"]]
    assert "商品名" not in names


# --- 2. fetch_bandai_howto (network stubbed) -------------------------------

class _FakeResponse:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise fh.requests.HTTPError(f"status {self.status_code}")


class _FakeSession:
    def __init__(self, text: str):
        self._text = text
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params or {}))
        return _FakeResponse(self._text)


class _NullLimiter:
    def wait(self, host):
        pass


def test_fetch_bandai_empty_jan_makes_no_request():
    session = _FakeSession(_read_fixture("bandai_manuals_zero.html"))
    result = fh.fetch_bandai_howto("", session, _NullLimiter())
    assert result is None
    assert session.calls == []


def test_fetch_bandai_single_result_recorded_with_pdf_url():
    session = _FakeSession(_read_fixture("bandai_manuals_single.html"))
    result = fh.fetch_bandai_howto("4582770021226", session, _NullLimiter())
    assert result is not None
    assert result["url"] == "https://toy.bandai.co.jp/manuals/pdf.php?id=2852540"
    assert result["publisher"] == "bandai"
    assert result["kind"] == "manual_pdf"
    assert result["matched_by"] == "jan"
    assert result["matched_key"] == "4582770021226"
    assert "manual.php" not in result["url"]


def test_fetch_bandai_zero_result_not_recorded():
    session = _FakeSession(_read_fixture("bandai_manuals_zero.html"))
    assert fh.fetch_bandai_howto("4582770018806", session, _NullLimiter()) is None


def test_fetch_bandai_multi_result_not_recorded():
    session = _FakeSession(_read_fixture("bandai_manuals_multi.html"))
    assert fh.fetch_bandai_howto("0000000000000", session, _NullLimiter()) is None


def test_fetch_bandai_default_listing_not_recorded():
    # jan_code が不一致で既定一覧 (署名つき) が返ってきたケース。記録しない。
    session = _FakeSession(_read_fixture("bandai_manuals_default_list.html"))
    assert fh.fetch_bandai_howto("9999999999999", session, _NullLimiter()) is None


class _RaisingSession:
    def get(self, url, params=None, timeout=None):
        raise fh.requests.ConnectionError("boom")


def test_fetch_bandai_network_error_raises_adapter_fetch_error():
    with pytest.raises(fh.AdapterFetchError):
        fh.fetch_bandai_howto("4582770021226", _RaisingSession(), _NullLimiter())


class _HttpErrorResponse(_FakeResponse):
    def __init__(self, status_code):
        super().__init__("", status_code=status_code)


class _HttpErrorSession:
    def __init__(self, status_code):
        self._status_code = status_code

    def get(self, url, params=None, timeout=None):
        return _HttpErrorResponse(self._status_code)


def test_fetch_bandai_5xx_raises_adapter_fetch_error_not_not_found():
    session = _HttpErrorSession(503)
    with pytest.raises(fh.AdapterFetchError):
        fh.fetch_bandai_howto("4582770021226", session, _NullLimiter())


def test_fetch_bandai_unrecognized_shape_raises_adapter_fetch_error():
    # manualListContent 自体が見つからない (サイト変更を想定)。not_found にしない。
    session = _FakeSession("<html><body>totally different page</body></html>")
    with pytest.raises(fh.AdapterFetchError):
        fh.fetch_bandai_howto("4582770021226", session, _NullLimiter())


# --- 3. レゴ ----------------------------------------------------------------

def test_extract_lego_set_numbers_prefers_trailing_number():
    numbers = fh.extract_lego_set_numbers("レゴ(LEGO) シティ つながる! ロードプレート 交差点 60304")
    assert numbers[0] == "60304"


def test_extract_lego_set_numbers_empty_when_no_digits():
    assert fh.extract_lego_set_numbers("レゴ(LEGO) デュプロ はじめてのデュプロ") == []


def test_extract_lego_set_numbers_excludes_piece_count():
    # レビュー指摘 (2026-09-22): 「1000ピース」の 1000 を品番候補にしない。
    numbers = fh.extract_lego_set_numbers("レゴ クラシック 11717 1000ピース")
    assert numbers == ["11717"]


def test_extract_lego_set_numbers_excludes_year_and_age():
    numbers = fh.extract_lego_set_numbers("レゴ(LEGO) シティ 2021年 交差点 60304 5歳")
    assert numbers == ["60304"]


class _FakeLegoSession:
    def __init__(self, responses: dict[str, _FakeResponse]):
        self._responses = responses
        self.calls: list[str] = []

    def get(self, url, timeout=None):
        self.calls.append(url)
        return self._responses.get(url, _FakeResponse("", status_code=404))


def test_fetch_lego_howto_records_when_page_has_non_empty_building_instructions():
    # 実測フィクスチャ (60304): buildingInstructions":[{ ... } ] を含む本物の応答。
    url = fh.LEGO_INSTRUCTIONS_URL.format(set_number="60304")
    session = _FakeLegoSession({url: _FakeResponse(_read_fixture("lego_building_instructions_60304.html"))})
    result = fh.fetch_lego_howto("レゴ(LEGO) シティ 交差点 60304", session, _NullLimiter())
    assert result is not None
    assert result["url"] == url
    assert result["publisher"] == "lego"
    assert result["matched_by"] == "set_number"
    assert result["matched_key"] == "60304"


def test_fetch_lego_howto_not_recorded_when_building_instructions_empty():
    # レビュー指摘 (2026-09-22, 重大): 存在しない品番 (99999) でも curl で 200 かつ
    # ページ内に "99999" の文字列自体 (URL エコー) を含むことを実測で確認済み。
    # status==200 and set_number in resp.text だけでは false positive になる。
    # 実測フィクスチャ (99999): buildingInstructions":[] (空配列) で「無し」が判定できる。
    url = fh.LEGO_INSTRUCTIONS_URL.format(set_number="99999")
    session = _FakeLegoSession({url: _FakeResponse(_read_fixture("lego_building_instructions_99999.html"))})
    assert fh.fetch_lego_howto("レゴ ダミー品番 99999", session, _NullLimiter()) is None


def test_fetch_lego_howto_only_tries_real_set_number_not_piece_count():
    # 「レゴ クラシック 11717 1000ピース」→ 11717 だけを試す (1000 は候補にしない)。
    url_11717 = fh.LEGO_INSTRUCTIONS_URL.format(set_number="11717")
    session = _FakeLegoSession({
        url_11717: _FakeResponse(_read_fixture("lego_building_instructions_60304.html").replace("60304", "11717")),
    })
    result = fh.fetch_lego_howto("レゴ クラシック 11717 1000ピース", session, _NullLimiter())
    assert result is not None
    assert result["matched_key"] == "11717"
    tried = {u.rsplit("/", 1)[-1] for u in session.calls}
    assert tried == {"11717"}


def test_fetch_lego_howto_not_recorded_on_404():
    session = _FakeLegoSession({})
    assert fh.fetch_lego_howto("レゴ(LEGO) シティ 交差点 60304", session, _NullLimiter()) is None


def test_fetch_lego_howto_no_candidates():
    assert fh.fetch_lego_howto("レゴ(LEGO) デュプロ", _FakeLegoSession({}), _NullLimiter()) is None


def test_fetch_lego_howto_network_error_raises_adapter_fetch_error():
    with pytest.raises(fh.AdapterFetchError):
        fh.fetch_lego_howto("レゴ(LEGO) シティ 交差点 60304", _RaisingSession(), _NullLimiter())


def test_fetch_lego_howto_5xx_raises_adapter_fetch_error():
    url = fh.LEGO_INSTRUCTIONS_URL.format(set_number="60304")
    session = _FakeLegoSession({url: _FakeResponse("", status_code=503)})
    with pytest.raises(fh.AdapterFetchError):
        fh.fetch_lego_howto("レゴ(LEGO) シティ 交差点 60304", session, _NullLimiter())


# --- 4. たまごっち ------------------------------------------------------------

_TAMA_CONFIG = {
    "tamagotchi": {
        "accessory_exclude_words": [
            "ケース", "カバー", "フィルム", "キャリー", "ストラップ", "保護", "ホルダー", "ポーチ", "充電",
        ],
        "series": [
            {"keywords": ["たまごっちパラダイス", "Tamagotchi Paradise", "Tama gotchi Paradise"],
             "url": "https://tamagotchi-official.com/jp/series/paradise/howto/"},
        ]
    }
}

_BANDAI_BRAND = "バンダイ(BANDAI)"


def test_match_tamagotchi_series_hits_on_substring():
    m = fh.match_tamagotchi_series(
        "Tamagotchi Paradise - White Glacier たまごっちパラダイス", _BANDAI_BRAND, "", _TAMA_CONFIG,
    )
    assert m is not None
    assert m["url"] == "https://tamagotchi-official.com/jp/series/paradise/howto/"


def test_match_tamagotchi_series_matches_spaced_variant_like_b0h7mkjq9g():
    # B0H7MKJQ9G の実際の商品名 (単語間にスペースが入る表記ゆれ)。
    name = "Tama gotchi Paradise - White Glacier たま ごっちパラダイス【日本おもちゃ大賞2025デジタル部門大賞】 対象年齢 6 才以上"
    m = fh.match_tamagotchi_series(name, _BANDAI_BRAND, "", _TAMA_CONFIG)
    assert m is not None
    assert m["keyword"] == "Tama gotchi Paradise"


def test_match_tamagotchi_series_none_when_not_in_table():
    assert fh.match_tamagotchi_series("シャインアルカナロッド", _BANDAI_BRAND, "", _TAMA_CONFIG) is None


def test_match_tamagotchi_series_none_when_brand_is_not_bandai():
    # レビュー指摘 (2026-09-22): GOKEI/ミヤビックス/PDA工房 のサードパーティ
    # アクセサリーは系列キーワードに一致するが、ブランドがバンダイでない。
    third_party_names = [
        ("GOKEI", "GOKEI 【2026年新登場 豪華7点セット】 Tamagotchi Paradise ケース（窓開きタイプ） "
                  "通信対応 たまごっちパラダイス 保護ケース"),
        ("GOKEI", "GOKEI 【2026新登場】 Tamagotchi Paradise ケース カバー 通信に対応 たまごっちパラダイス "
                  "保護ケース 窓開きタイプ クリアラメ （ビーズブレスレット付き）"),
        ("ミヤビックス", "ミヤビックス Tamagotchi Paradise たまごっちパラダイス 対応 保護 フィルム 光沢 防指紋 防気泡 日本製"),
        ("PDA工房", "PDA工房 Tamagotchi Paradise(たまごっちパラダイス) 対応 黒影[AR低反射・光沢] 保護 フィルム 日本製"),
    ]
    for brand, name in third_party_names:
        assert fh.match_tamagotchi_series(name, brand, "", _TAMA_CONFIG) is None, name


def test_match_tamagotchi_series_none_for_bandai_carry_case_accessory():
    # レビュー指摘 (2026-09-22): B0H1KQQ7J4「おでかけキャリー おこじょっち」は
    # ブランドがバンダイでも本体ではなくキャリーケース。アクセサリー語で除外する。
    name = "Tamagotchi Paradise おでかけキャリー おこじょっち 対象年齢 6 才以上 たまごっちパラダイス"
    assert fh.match_tamagotchi_series(name, _BANDAI_BRAND, "", _TAMA_CONFIG) is None


def test_match_tamagotchi_series_uses_amazon_title_prefix_when_brand_missing():
    # brand が空でも Amazon タイトルの [バンダイ(BANDAI)] 接頭辞で判定できる。
    amazon_title = "[バンダイ(BANDAI)] Tamagotchi Paradise - White Glacier 対象年齢 6 才以上 たまごっちパラダイス"
    m = fh.match_tamagotchi_series("たまごっちパラダイス", "", amazon_title, _TAMA_CONFIG)
    assert m is not None


def test_fetch_tamagotchi_howto_records_on_200():
    url = "https://tamagotchi-official.com/jp/series/paradise/howto/"
    session = _FakeLegoSession({url: _FakeResponse("<html>howto</html>")})
    product = {"name": "たまごっちパラダイス", "brand": _BANDAI_BRAND, "amazon_title": ""}
    result = fh.fetch_tamagotchi_howto(product, _TAMA_CONFIG, session, _NullLimiter())
    assert result is not None
    assert result["publisher"] == "tamagotchi"
    assert result["url"] == url
    assert result["matched_by"] == "series_keyword"


def test_fetch_tamagotchi_howto_raises_adapter_fetch_error_when_page_down():
    # レビュー指摘: 系列一致済みなのにページが取れないのは一時障害。not_found にしない。
    url = "https://tamagotchi-official.com/jp/series/paradise/howto/"
    session = _FakeLegoSession({url: _FakeResponse("", status_code=500)})
    product = {"name": "たまごっちパラダイス", "brand": _BANDAI_BRAND, "amazon_title": ""}
    with pytest.raises(fh.AdapterFetchError):
        fh.fetch_tamagotchi_howto(product, _TAMA_CONFIG, session, _NullLimiter())


def test_fetch_tamagotchi_howto_network_error_raises_adapter_fetch_error():
    product = {"name": "たまごっちパラダイス", "brand": _BANDAI_BRAND, "amazon_title": ""}
    with pytest.raises(fh.AdapterFetchError):
        fh.fetch_tamagotchi_howto(product, _TAMA_CONFIG, _RaisingSession(), _NullLimiter())


def test_fetch_tamagotchi_howto_not_in_table():
    product = {"name": "シャインアルカナロッド", "brand": _BANDAI_BRAND, "amazon_title": ""}
    assert fh.fetch_tamagotchi_howto(product, _TAMA_CONFIG, _FakeLegoSession({}), _NullLimiter()) is None


def test_fetch_tamagotchi_howto_not_recorded_for_accessory_even_if_url_reachable():
    # アクセサリーは match しない (None) ので、そもそもページ取得すら発生しない。
    url = "https://tamagotchi-official.com/jp/series/paradise/howto/"
    session = _FakeLegoSession({url: _FakeResponse("<html>howto</html>")})
    product = {
        "name": "GOKEI Tamagotchi Paradise ケース たまごっちパラダイス 保護ケース",
        "brand": "GOKEI", "amazon_title": "",
    }
    assert fh.fetch_tamagotchi_howto(product, _TAMA_CONFIG, session, _NullLimiter()) is None
    assert session.calls == []


# --- 3b. タカラトミー (#8002) ------------------------------------------------

def test_parse_takaratomy_category_links_from_index_page():
    html = _read_fixture("takaratomy_manual_index.html")
    links = fh.parse_takaratomy_category_links(html)
    assert links == [
        "/support/manual/tomica/",
        "/support/manual/plarail/",
        "/support/manual/beyblade/",
        "/support/manual/other/",
    ]


def test_parse_takaratomy_category_links_excludes_breadcrumb_style_link():
    # ナビの「/support/manual/」への単なるリンク (末尾スラッシュのみ、slug 無し) は拾わない。
    html = _read_fixture("takaratomy_manual_index.html")
    links = fh.parse_takaratomy_category_links(html)
    assert "/support/manual/" not in links


def test_parse_takaratomy_category_page_extracts_jan_and_next_page():
    html = _read_fixture("takaratomy_category_beyblade_page1.html")
    parsed = fh.parse_takaratomy_category_page(html)
    jans = {e["jan"] for e in parsed["entries"]}
    assert jans == {"4904810080640", "4904810080626", "4904810085546", "4904810080657"}
    entry = next(e for e in parsed["entries"] if e["jan"] == "4904810085546")
    assert entry["href"] == "/support/manual/beyblade/2026081916129.html"
    assert entry["name"] == "ＵＸ－２１　ヘルズネザーデッキセット"
    assert parsed["next_href"] == "/support/manual/beyblade/index_2.html"


def test_parse_takaratomy_category_page_last_page_has_no_next_href():
    # レビュー観点 (#8002): 「次へ」リンクが無い = ページング終端。
    html = _read_fixture("takaratomy_category_beyblade_last_page.html")
    parsed = fh.parse_takaratomy_category_page(html)
    assert parsed["next_href"] is None
    assert len(parsed["entries"]) == 3


def test_parse_takaratomy_pdf_link_from_detail_page():
    html = _read_fixture("takaratomy_detail_page.html")
    href = fh.parse_takaratomy_pdf_link(html)
    assert href is not None
    assert href.startswith("/support/manual/items/4904810085546_")
    assert href.endswith(".pdf")


def test_parse_takaratomy_pdf_link_none_when_missing():
    html = _read_fixture("takaratomy_detail_page_no_pdf.html")
    assert fh.parse_takaratomy_pdf_link(html) is None


class _FakeTakaratomySession:
    """URL → _FakeResponse の固定応答。build_takaratomy_manual_index のページング巡回用。"""

    def __init__(self, responses: dict[str, "_FakeResponse"]):
        self._responses = responses
        self.calls: list[str] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append(url)
        if url not in self._responses:
            return _FakeResponse("", status_code=404)
        return self._responses[url]

    def head(self, url, timeout=None, allow_redirects=True):
        self.calls.append(("HEAD", url))
        return self._responses.get(url, _FakeResponse("", status_code=404))


def test_build_takaratomy_manual_index_follows_pagination_and_counts_requests():
    index_url = fh.TAKARATOMY_MANUAL_INDEX_URL
    beyblade_p1 = "https://www.takaratomy.co.jp/support/manual/beyblade/"
    beyblade_p2 = "https://www.takaratomy.co.jp/support/manual/beyblade/index_2.html"
    # 索引ページには beyblade だけを含む簡略版 (テストの焦点をページングに絞る)。
    mini_index_html = (
        '<li class="imgOver01"><a href="/support/manual/beyblade/">'
        '<span class="text01">ベイブレード</span></a></li>'
    )
    session = _FakeTakaratomySession({
        index_url: _FakeResponse(mini_index_html),
        beyblade_p1: _FakeResponse(_read_fixture("takaratomy_category_beyblade_page1.html")),
        beyblade_p2: _FakeResponse(_read_fixture("takaratomy_category_beyblade_last_page.html")),
    })
    index, request_count = fh.build_takaratomy_manual_index(session, _NullLimiter())
    # index page (1) + beyblade page1 (1) + beyblade page2/last (1) = 3
    assert request_count == 3
    assert set(index.keys()) == {
        "4904810080640", "4904810080626", "4904810085546", "4904810080657",
        "4904810919124", "4904810939528", "4904810956969",
    }
    assert index["4904810085546"][0]["category"] == "beyblade"
    assert index["4904810085546"][0]["detail_url"] == (
        "https://www.takaratomy.co.jp/support/manual/beyblade/2026081916129.html"
    )


def test_build_takaratomy_manual_index_raises_on_no_categories():
    session = _FakeTakaratomySession({fh.TAKARATOMY_MANUAL_INDEX_URL: _FakeResponse("<html>変更後のページ</html>")})
    with pytest.raises(fh.AdapterFetchError):
        fh.build_takaratomy_manual_index(session, _NullLimiter())


def test_build_takaratomy_manual_index_network_error_raises():
    with pytest.raises(fh.AdapterFetchError):
        fh.build_takaratomy_manual_index(_RaisingSession(), _NullLimiter())


def _write_takaratomy_index_cache(path, entries, fetched_at="2026-09-22T00:00:00Z"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"fetched_at": fetched_at, "entries": entries}, ensure_ascii=False), encoding="utf-8")


def test_fetch_takaratomy_howto_records_when_exactly_one_match(tmp_path):
    index_path = tmp_path / "takaratomy_manual_index.json"
    detail_url = "https://www.takaratomy.co.jp/support/manual/beyblade/2026081916129.html"
    pdf_url = "https://www.takaratomy.co.jp/support/manual/items/4904810085546_test.pdf"
    _write_takaratomy_index_cache(index_path, {
        "4904810085546": [{"detail_url": detail_url, "name": "ヘルズネザーデッキセット", "category": "beyblade"}],
    })
    detail_html = _read_fixture("takaratomy_detail_page.html")
    session = _FakeTakaratomySession({
        detail_url: _FakeResponse(detail_html),
        "https://www.takaratomy.co.jp" + fh.parse_takaratomy_pdf_link(detail_html): _FakeResponse(
            "", status_code=200,
        ),
    })
    # HEAD 応答に Content-Type を持たせるため headers 属性を追加する。
    pdf_absolute = "https://www.takaratomy.co.jp" + fh.parse_takaratomy_pdf_link(detail_html)
    session._responses[pdf_absolute].headers = {"Content-Type": "application/pdf"}

    result = fh.fetch_takaratomy_howto(
        "4904810085546", session, _NullLimiter(), index_path=index_path,
        now=datetime(2026, 9, 22, tzinfo=timezone.utc),
    )
    assert result is not None
    assert result["publisher"] == "takaratomy"
    assert result["kind"] == "manual_pdf"
    assert result["url"] == pdf_absolute
    assert result["matched_by"] == "jan"
    assert result["matched_key"] == "4904810085546"
    assert result["official_name"] == "ヘルズネザーデッキセット"


def test_fetch_takaratomy_howto_zero_match_not_recorded(tmp_path):
    index_path = tmp_path / "takaratomy_manual_index.json"
    _write_takaratomy_index_cache(index_path, {})
    session = _FakeTakaratomySession({})
    assert fh.fetch_takaratomy_howto(
        "0000000000000", session, _NullLimiter(), index_path=index_path,
        now=datetime(2026, 9, 22, tzinfo=timezone.utc),
    ) is None


def test_fetch_takaratomy_howto_multi_match_not_recorded(tmp_path):
    # 同一 JAN が (誤登録等で) 複数エントリに割れているケース。一意特定できないので記録しない。
    index_path = tmp_path / "takaratomy_manual_index.json"
    _write_takaratomy_index_cache(index_path, {
        "4904810085546": [
            {"detail_url": "https://www.takaratomy.co.jp/a.html", "name": "A", "category": "beyblade"},
            {"detail_url": "https://www.takaratomy.co.jp/b.html", "name": "B", "category": "plarail"},
        ],
    })
    session = _FakeTakaratomySession({})
    result = fh.fetch_takaratomy_howto(
        "4904810085546", session, _NullLimiter(), index_path=index_path,
        now=datetime(2026, 9, 22, tzinfo=timezone.utc),
    )
    assert result is None
    assert session.calls == []  # 一意特定できない時点で詳細ページすら取りに行かない


def test_fetch_takaratomy_howto_empty_jan_returns_none_without_index_build(tmp_path, monkeypatch):
    index_path = tmp_path / "does_not_exist.json"

    def _boom(*args, **kwargs):
        raise AssertionError("empty JAN で索引を構築してはいけない")

    monkeypatch.setattr(fh, "ensure_takaratomy_index", _boom)
    assert fh.fetch_takaratomy_howto("", _FakeTakaratomySession({}), _NullLimiter(), index_path=index_path) is None


def test_fetch_takaratomy_howto_detail_page_network_error_raises(tmp_path):
    index_path = tmp_path / "takaratomy_manual_index.json"
    _write_takaratomy_index_cache(index_path, {
        "4904810085546": [{"detail_url": "https://www.takaratomy.co.jp/x.html", "name": "X", "category": "beyblade"}],
    })
    with pytest.raises(fh.AdapterFetchError):
        fh.fetch_takaratomy_howto(
            "4904810085546", _RaisingSession(), _NullLimiter(), index_path=index_path,
            now=datetime(2026, 9, 22, tzinfo=timezone.utc),
        )


def test_fetch_takaratomy_howto_missing_pdf_link_raises_adapter_fetch_error(tmp_path):
    index_path = tmp_path / "takaratomy_manual_index.json"
    detail_url = "https://www.takaratomy.co.jp/support/manual/other/dummy.html"
    _write_takaratomy_index_cache(index_path, {
        "0000000000000": [{"detail_url": detail_url, "name": "ダミー商品", "category": "other"}],
    })
    session = _FakeTakaratomySession({detail_url: _FakeResponse(_read_fixture("takaratomy_detail_page_no_pdf.html"))})
    with pytest.raises(fh.AdapterFetchError):
        fh.fetch_takaratomy_howto(
            "0000000000000", session, _NullLimiter(), index_path=index_path,
            now=datetime(2026, 9, 22, tzinfo=timezone.utc),
        )


def test_fetch_takaratomy_howto_pdf_head_wrong_content_type_raises(tmp_path):
    index_path = tmp_path / "takaratomy_manual_index.json"
    detail_url = "https://www.takaratomy.co.jp/support/manual/beyblade/x.html"
    detail_html = _read_fixture("takaratomy_detail_page.html")
    pdf_absolute = "https://www.takaratomy.co.jp" + fh.parse_takaratomy_pdf_link(detail_html)
    _write_takaratomy_index_cache(index_path, {
        "4904810085546": [{"detail_url": detail_url, "name": "X", "category": "beyblade"}],
    })
    session = _FakeTakaratomySession({
        detail_url: _FakeResponse(detail_html),
        pdf_absolute: _FakeResponse("", status_code=200),
    })
    session._responses[pdf_absolute].headers = {"Content-Type": "text/html"}
    with pytest.raises(fh.AdapterFetchError):
        fh.fetch_takaratomy_howto(
            "4904810085546", session, _NullLimiter(), index_path=index_path,
            now=datetime(2026, 9, 22, tzinfo=timezone.utc),
        )


def test_fetch_takaratomy_howto_uses_fresh_cache_without_rebuilding(tmp_path, monkeypatch):
    # キャッシュが週内なら索引再構築 (build_takaratomy_manual_index) を一切呼ばない。
    index_path = tmp_path / "takaratomy_manual_index.json"
    detail_url = "https://www.takaratomy.co.jp/support/manual/beyblade/x.html"
    _write_takaratomy_index_cache(
        index_path,
        {"4904810085546": [{"detail_url": detail_url, "name": "X", "category": "beyblade"}]},
        fetched_at="2026-09-20T00:00:00Z",
    )

    def _boom(*args, **kwargs):
        raise AssertionError("新しいキャッシュがあるのに再構築してはいけない")

    monkeypatch.setattr(fh, "build_takaratomy_manual_index", _boom)
    detail_html = _read_fixture("takaratomy_detail_page.html")
    pdf_absolute = "https://www.takaratomy.co.jp" + fh.parse_takaratomy_pdf_link(detail_html)
    session = _FakeTakaratomySession({
        detail_url: _FakeResponse(detail_html),
        pdf_absolute: _FakeResponse("", status_code=200),
    })
    session._responses[pdf_absolute].headers = {"Content-Type": "application/pdf"}
    result = fh.fetch_takaratomy_howto(
        "4904810085546", session, _NullLimiter(), index_path=index_path,
        now=datetime(2026, 9, 22, tzinfo=timezone.utc),
    )
    assert result is not None


def test_takaratomy_index_stale_after_max_age():
    now = datetime(2026, 9, 22, tzinfo=timezone.utc)
    fresh = {"fetched_at": "2026-09-20T00:00:00Z", "entries": {}}
    stale = {"fetched_at": "2026-09-10T00:00:00Z", "entries": {}}
    assert fh._takaratomy_index_is_stale(fresh, now) is False
    assert fh._takaratomy_index_is_stale(stale, now) is True
    assert fh._takaratomy_index_is_stale(None, now) is True


# --- 3c. タカラトミー ブランド判定 (アーツ除外) --------------------------------

def test_is_takaratomy_branded_matches_plain_and_suffixed_forms():
    assert fh._is_takaratomy_branded("タカラトミー") is True
    assert fh._is_takaratomy_branded("タカラトミー(TAKARA TOMY)") is True
    assert fh._is_takaratomy_branded("TAKARA TOMY") is True


def test_is_takaratomy_branded_excludes_takaratomy_arts():
    # #7956: タカラトミーアーツは別法人・別サイト。brand の前方一致で誤合算しない。
    assert fh._is_takaratomy_branded("タカラトミーアーツ") is False
    assert fh._is_takaratomy_branded("タカラトミーアーツ(T-ARTS)") is False


def test_is_takaratomy_branded_false_for_other_or_empty():
    assert fh._is_takaratomy_branded("") is False
    assert fh._is_takaratomy_branded("バンダイ(BANDAI)") is False
    assert fh._is_takaratomy_branded(None) is False  # type: ignore[arg-type]


# --- 5. route_adapter --------------------------------------------------------

def test_route_prefers_tamagotchi_over_bandai_brand_tag():
    product = {
        "name": "[バンダイ(BANDAI)] Tamagotchi Paradise - White Glacier たまごっちパラダイス",
        "brand": "バンダイ(BANDAI)",
        "jan": "4582770018806",
    }
    assert fh.route_adapter(product, _TAMA_CONFIG) == "tamagotchi"


def test_route_lego_by_brand():
    product = {"name": "レゴ(LEGO) シティ 交差点 60304", "brand": "レゴ(LEGO)", "jan": ""}
    assert fh.route_adapter(product, {}) == "lego"


def test_route_bandai_requires_jan():
    product = {"name": "[バンダイ(BANDAI)] シャインアルカナロッド", "brand": "バンダイ(BANDAI)", "jan": ""}
    assert fh.route_adapter(product, {}) is None


def test_route_bandai_with_jan():
    product = {"name": "[バンダイ(BANDAI)] シャインアルカナロッド", "brand": "バンダイ(BANDAI)", "jan": "4582769908774"}
    assert fh.route_adapter(product, {}) == "bandai"


def test_route_takaratomy_requires_jan():
    product = {"name": "タカラトミー(TAKARA TOMY) BEYBLADE X UX-21", "brand": "タカラトミー", "jan": ""}
    assert fh.route_adapter(product, {}) is None


def test_route_takaratomy_with_jan():
    product = {
        "name": "タカラトミー(TAKARA TOMY) BEYBLADE X UX-21", "brand": "タカラトミー(TAKARA TOMY)",
        "jan": "4904810085546",
    }
    assert fh.route_adapter(product, {}) == "takaratomy"


def test_route_takaratomy_arts_excluded_even_with_jan():
    # #7956: タカラトミーアーツは別法人・別サイト。JAN があってもタカラトミー
    # アダプタにはルーティングしない (対象外)。
    product = {"name": "ポケモンフレンダ フレンダフォルダー2", "brand": "タカラトミーアーツ", "jan": "1234567890123"}
    assert fh.route_adapter(product, {}) is None


def test_route_none_for_unknown_brand():
    product = {"name": "TAETOE 知育玩具", "brand": "TAETOE", "jan": ""}
    assert fh.route_adapter(product, {}) is None


def test_route_accessory_falls_back_to_none_not_tamagotchi():
    # レビュー指摘 (2026-09-22): GOKEI の保護ケースはたまごっちアダプタに
    # ルーティングしない (ブランドが GOKEI で、かつアクセサリー語を含む)。
    # JAN も無いのでバンダイにも落ちず、対象外 (None) になる。
    product = {
        "name": "GOKEI Tamagotchi Paradise ケース たまごっちパラダイス 保護ケース",
        "brand": "GOKEI", "jan": "", "amazon_title": "",
    }
    assert fh.route_adapter(product, _TAMA_CONFIG) is None


def test_route_bandai_carry_case_accessory_falls_back_to_bandai_jan_adapter():
    # B0H1KQQ7J4 はブランドがバンダイで JAN があるため、たまごっちアダプタでは
    # 除外されつつ、バンダイ JAN アダプタには (JAN があれば) 通常どおり乗る。
    # JAN 完全一致は商品固有なので、この経路まで塞ぐ必要はない (レビュー指摘)。
    product = {
        "name": "Tamagotchi Paradise おでかけキャリー おこじょっち たまごっちパラダイス",
        "brand": "バンダイ", "jan": "4582770099999", "amazon_title": "",
    }
    assert fh.route_adapter(product, _TAMA_CONFIG) == "bandai"


# --- 6. write 側: merge / 30 日据え置き ---------------------------------------

def test_build_record_preserves_existing_steps_and_reviewed_by_when_found():
    existing = {
        "asin": "B0H4PQ29JS", "url": "https://old.example/", "publisher": "bandai",
        "steps": [{"text": "リセットスイッチを押す"}], "reviewed_by": "iromama",
    }
    found = {
        "publisher": "bandai", "kind": "manual_pdf",
        "url": "https://toy.bandai.co.jp/manuals/pdf.php?id=2852540",
        "official_name": "DIGIVICE Ver.REVIVAL 石田ヤマトカラー",
        "matched_by": "jan", "matched_key": "4582770021226",
    }
    record = fh.build_record("B0H4PQ29JS", found, existing, "2026-09-21T00:00:00Z")
    assert record["steps"] == existing["steps"]
    assert record["reviewed_by"] == "iromama"
    assert record["url"] == found["url"]


def test_build_record_preserves_steps_even_on_not_found():
    existing = {"asin": "X", "url": "https://old.example/", "steps": [{"text": "a"}], "reviewed_by": "iromama"}
    record = fh.build_record("X", None, existing, "2026-09-21T00:00:00Z")
    assert record["status"] == "not_found"
    assert record["steps"] == existing["steps"]
    assert record["reviewed_by"] == "iromama"


def test_build_record_not_found_has_no_steps_when_none_existed():
    record = fh.build_record("X", None, None, "2026-09-21T00:00:00Z")
    assert record == {"asin": "X", "status": "not_found", "checked_at": "2026-09-21T00:00:00Z"}


def test_is_stale_true_when_no_existing_record():
    assert fh._is_stale(None, 30, datetime.now(timezone.utc)) is True


def test_is_stale_false_within_recheck_window():
    now = datetime(2026, 9, 21, tzinfo=timezone.utc)
    existing = {"checked_at": "2026-09-10T00:00:00Z"}
    assert fh._is_stale(existing, 30, now) is False


def test_is_stale_true_after_recheck_window():
    now = datetime(2026, 9, 21, tzinfo=timezone.utc)
    existing = {"checked_at": "2026-08-01T00:00:00Z"}
    assert fh._is_stale(existing, 30, now) is True


def test_is_stale_uses_fetched_at_for_found_records():
    now = datetime(2026, 9, 21, tzinfo=timezone.utc)
    existing = {"fetched_at": "2026-09-20T00:00:00Z", "url": "https://x/"}
    assert fh._is_stale(existing, 30, now) is False


def test_write_official_howto_writes_json(tmp_path):
    record = {"asin": "B0TEST0001", "status": "not_found", "checked_at": "2026-09-21T00:00:00Z"}
    out = fh.write_official_howto("B0TEST0001", record, tmp_path)
    assert out.exists()
    assert json.loads(out.read_text(encoding="utf-8")) == record


# --- 7. discover_target_asins -------------------------------------------------

def test_discover_target_asins_picks_latest_stem(tmp_path):
    (tmp_path / "2026-01-01-B0AAAAAAAA.json").write_text("{}", encoding="utf-8")
    (tmp_path / "2026-06-01-B0AAAAAAAA.json").write_text("{}", encoding="utf-8")
    (tmp_path / "2026-01-01-B0AAAAAAAA.seo.json").write_text("{}", encoding="utf-8")
    targets = fh.discover_target_asins(tmp_path)
    assert targets["B0AAAAAAAA"].name == "2026-06-01-B0AAAAAAAA.json"


# --- 8. エラー処理 (process_asin / main の exit code) --------------------------

def test_process_asin_bandai_error_writes_nothing_and_reports_error(tmp_path, monkeypatch):
    def _raise(*args, **kwargs):
        raise fh.AdapterFetchError("boom")

    monkeypatch.setattr(fh, "fetch_bandai_howto", _raise)
    product = {"name": "[バンダイ(BANDAI)] シャインアルカナロッド", "brand": "バンダイ(BANDAI)", "jan": "4582769908774"}
    result = fh.process_asin("B0TEST0002", product, {}, _NullLimiter(), _NullLimiter(), tmp_path)
    assert result["action"] == "error"
    assert not (tmp_path / "B0TEST0002" / "official_howto.json").exists()


def test_process_asin_error_never_downgrades_existing_found_record(tmp_path, monkeypatch):
    # レビュー指摘 (2026-09-22, 重大): 一時エラーで found → not_found に
    # 格下げしてはいけない。recheck_days=0 で「据え置き期間を過ぎた」状態を
    # 作ったうえでエラーを起こし、既存ファイルが一切変わらないことを確認する。
    asin = "B0TEST0003"
    out_dir = tmp_path / asin
    out_dir.mkdir()
    existing_record = {
        "asin": asin, "publisher": "bandai", "kind": "manual_pdf",
        "url": "https://toy.bandai.co.jp/manuals/pdf.php?id=1234567",
        "official_name": "テスト商品", "matched_by": "jan", "matched_key": "4582769908774",
        "fetched_at": "2026-01-01T00:00:00Z",
    }
    out_path = out_dir / "official_howto.json"
    out_path.write_text(json.dumps(existing_record, ensure_ascii=False), encoding="utf-8")

    def _raise(*args, **kwargs):
        raise fh.AdapterFetchError("boom")

    monkeypatch.setattr(fh, "fetch_bandai_howto", _raise)
    product = {"name": "[バンダイ(BANDAI)] シャインアルカナロッド", "brand": "バンダイ(BANDAI)", "jan": "4582769908774"}
    result = fh.process_asin(asin, product, {}, _NullLimiter(), _NullLimiter(), tmp_path, recheck_days=0)
    assert result["action"] == "error"
    # ファイルは一切書き換わっていない (found のまま)。
    assert json.loads(out_path.read_text(encoding="utf-8")) == existing_record


def test_process_asin_lego_error_does_not_write_not_found(tmp_path, monkeypatch):
    def _raise(*args, **kwargs):
        raise fh.AdapterFetchError("boom")

    monkeypatch.setattr(fh, "fetch_lego_howto", _raise)
    product = {"name": "レゴ(LEGO) シティ 交差点 60304", "brand": "レゴ(LEGO)", "jan": ""}
    result = fh.process_asin("B0TEST0004", product, {}, _NullLimiter(), _NullLimiter(), tmp_path)
    assert result["action"] == "error"
    assert not (tmp_path / "B0TEST0004" / "official_howto.json").exists()


def test_exit_code_zero_when_no_errors():
    assert fh._exit_code_for_counts({"found": 3, "not_found": 2, "error": 0}) == 0


def test_exit_code_zero_for_isolated_error_below_threshold():
    # 10 件中 1 件の一時エラー (10%) では赤くしない。
    assert fh._exit_code_for_counts({"found": 8, "not_found": 1, "error": 1}) == 0


def test_exit_code_nonzero_when_error_rate_exceeds_threshold():
    # 3 件中 2 件がエラー (67%) — サイト側の系統的な障害を想定。
    assert fh._exit_code_for_counts({"found": 1, "not_found": 0, "error": 2}) == 1


def test_exit_code_zero_when_nothing_attempted():
    # 全件 skip_recent/skip_no_adapter/dry_run で、判定材料が無い。
    assert fh._exit_code_for_counts({"found": 0, "not_found": 0, "error": 0}) == 0


# --- UrllibSession (lego.com は requests を 403 で弾く。2026-09-22 実測) -------

def test_urllib_session_maps_http_error_to_status(monkeypatch):
    import urllib.error

    def _raise(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr(fh.urllib.request, "urlopen", _raise)
    resp = fh.UrllibSession("UA").get("https://www.lego.com/ja-jp/service/building-instructions/1")
    assert resp.status_code == 404 and resp.text == ""


def test_urllib_session_network_error_becomes_request_exception(monkeypatch):
    import urllib.error

    def _raise(req, timeout):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(fh.urllib.request, "urlopen", _raise)
    with pytest.raises(fh.requests.RequestException):
        fh.UrllibSession("UA").get("https://www.lego.com/")


def test_takaratomy_arts_english_brand_excluded():
    # レビュー指摘 (#8002): 英字表記のアーツも別法人として除外する
    assert not fh._is_takaratomy_branded("TAKARA TOMY ARTS")
    assert fh._is_takaratomy_branded("TAKARA TOMY")
