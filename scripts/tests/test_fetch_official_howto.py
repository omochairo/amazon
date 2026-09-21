"""#7958 (#7955 設計2 改訂版 B): 公式取説・あそびかたの URL を取る。

Coverage:
1. バンダイ HTML 解析 (parse_bandai_manual_list): 実測フィクスチャで
   exactly-1 / zero / default-list (署名つき manual.php 混入) / multi を判定
2. fetch_bandai_howto: 空 JAN で問い合わせない・1 件確定で pdf.php を記録・
   0 件/複数件/デフォルト一覧では記録しない
3. レゴ: 品番抽出 (末尾優先) とページ確認 (200 かつ品番を含む)
4. たまごっち: 系列キーワード一致 + 200 確認
5. ルーティング (route_adapter): たまごっちキーワード優先 / レゴ / バンダイ (JAN 必須) / 該当無し
6. write 側: 既存 steps/reviewed_by を消さずにマージ・30 日以内は再問い合わせしない
"""
from __future__ import annotations

import json
import pathlib
import sys
from datetime import datetime, timedelta, timezone

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


# --- 3. レゴ ----------------------------------------------------------------

def test_extract_lego_set_numbers_prefers_trailing_number():
    numbers = fh.extract_lego_set_numbers("レゴ(LEGO) シティ つながる! ロードプレート 交差点 60304")
    assert numbers[0] == "60304"


def test_extract_lego_set_numbers_empty_when_no_digits():
    assert fh.extract_lego_set_numbers("レゴ(LEGO) デュプロ はじめてのデュプロ") == []


class _FakeLegoSession:
    def __init__(self, responses: dict[str, _FakeResponse]):
        self._responses = responses
        self.calls: list[str] = []

    def get(self, url, timeout=None):
        self.calls.append(url)
        return self._responses.get(url, _FakeResponse("", status_code=404))


def test_fetch_lego_howto_records_when_page_confirms_set_number():
    url = fh.LEGO_INSTRUCTIONS_URL.format(set_number="60304")
    session = _FakeLegoSession({url: _FakeResponse("<html>60304 building instructions</html>")})
    result = fh.fetch_lego_howto("レゴ(LEGO) シティ 交差点 60304", session, _NullLimiter())
    assert result is not None
    assert result["url"] == url
    assert result["publisher"] == "lego"
    assert result["matched_by"] == "set_number"
    assert result["matched_key"] == "60304"


def test_fetch_lego_howto_not_recorded_when_page_lacks_set_number():
    url = fh.LEGO_INSTRUCTIONS_URL.format(set_number="60304")
    # 200 だが品番の文字列が本文に無い (誤爆想定)。
    session = _FakeLegoSession({url: _FakeResponse("<html>not related</html>")})
    assert fh.fetch_lego_howto("レゴ(LEGO) シティ 交差点 60304", session, _NullLimiter()) is None


def test_fetch_lego_howto_not_recorded_on_404():
    session = _FakeLegoSession({})
    assert fh.fetch_lego_howto("レゴ(LEGO) シティ 交差点 60304", session, _NullLimiter()) is None


def test_fetch_lego_howto_no_candidates():
    assert fh.fetch_lego_howto("レゴ(LEGO) デュプロ", _FakeLegoSession({}), _NullLimiter()) is None


# --- 4. たまごっち ------------------------------------------------------------

_TAMA_CONFIG = {
    "tamagotchi": {
        "series": [
            {"keywords": ["たまごっちパラダイス", "Tamagotchi Paradise"],
             "url": "https://tamagotchi-official.com/jp/series/paradise/howto/"},
        ]
    }
}


def test_match_tamagotchi_series_hits_on_substring():
    m = fh.match_tamagotchi_series("[バンダイ(BANDAI)] Tamagotchi Paradise - White Glacier たまごっちパラダイス", _TAMA_CONFIG)
    assert m is not None
    assert m["url"] == "https://tamagotchi-official.com/jp/series/paradise/howto/"


def test_match_tamagotchi_series_none_when_not_in_table():
    assert fh.match_tamagotchi_series("シャインアルカナロッド", _TAMA_CONFIG) is None


def test_fetch_tamagotchi_howto_records_on_200():
    url = "https://tamagotchi-official.com/jp/series/paradise/howto/"
    session = _FakeLegoSession({url: _FakeResponse("<html>howto</html>")})
    result = fh.fetch_tamagotchi_howto("たまごっちパラダイス", _TAMA_CONFIG, session, _NullLimiter())
    assert result is not None
    assert result["publisher"] == "tamagotchi"
    assert result["url"] == url
    assert result["matched_by"] == "series_keyword"


def test_fetch_tamagotchi_howto_not_recorded_when_page_down():
    url = "https://tamagotchi-official.com/jp/series/paradise/howto/"
    session = _FakeLegoSession({url: _FakeResponse("", status_code=500)})
    assert fh.fetch_tamagotchi_howto("たまごっちパラダイス", _TAMA_CONFIG, session, _NullLimiter()) is None


def test_fetch_tamagotchi_howto_not_in_table():
    assert fh.fetch_tamagotchi_howto("シャインアルカナロッド", _TAMA_CONFIG, _FakeLegoSession({}), _NullLimiter()) is None


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


def test_route_none_for_unknown_brand():
    product = {"name": "TAETOE 知育玩具", "brand": "TAETOE", "jan": ""}
    assert fh.route_adapter(product, {}) is None


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
