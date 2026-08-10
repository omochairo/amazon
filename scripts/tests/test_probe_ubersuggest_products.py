"""Ubersuggest 需要語の Amazon 実査 (観測のみ, #2686 PR-D) の検査。"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import probe_ubersuggest_products as P  # noqa: E402


TOY_NODE = {"id": "1", "name": "おもちゃ", "root": "おもちゃ"}
NON_TOY_NODE = {"id": "999", "name": "文房具", "root": "文房具"}


def _item(asin, title, nodes=None):
    return {
        "asin": asin,
        "itemInfo": {"title": {"displayValue": title}},
        "browseNodeInfo": {"browseNodes": nodes if nodes is not None else [
            {"id": "1", "displayName": "おもちゃ", "ancestor": {"displayName": "おもちゃ"}},
        ]},
    }


def _response(*items):
    """Creators API SearchItems の実レスポンス構造 (searchResult.items)。"""
    return {"searchResult": {"items": list(items)}}


class FakeAPI:
    """search_items を差し替える偽 API。呼ばれた keyword (=raw_query) を記録する。"""

    def __init__(self, responses: dict, raises: dict | None = None):
        self.responses = responses
        self.raises = raises or {}
        self.calls: list[str] = []

    def search_items(self, keywords=None, search_index=None, item_count=None, item_page=None,
                     resources=None):
        self.calls.append(keywords)
        if keywords in self.raises:
            raise self.raises[keywords]
        return self.responses.get(keywords, _response())


def _demand_file(tmp_path, entries):
    p = tmp_path / "ubersuggest_demand.json"
    p.write_text(json.dumps({"keywords": entries}, ensure_ascii=False), encoding="utf-8")
    return p


# --------------------------------------------------------------------------
# raw_query が検索語に使われること (回帰テスト)
# --------------------------------------------------------------------------

def test_raw_query_used_as_search_term_not_query(tmp_path):
    """query は空白除去済みの重複排除キーなので検索語にしてはいけない。"""
    kw = _demand_file(tmp_path, [
        {"query": "保育園シール貼り", "raw_query": "保育園 シール貼り", "volume": 100, "sites": ["a"]},
    ])
    api = FakeAPI({"保育園 シール貼り": _response(_item("B0X", "保育園 シール貼りセット"))})
    P.run(kw, tmp_path / "out.json", 0, "Toys", 10, dry_run=False, api=api, sleeper=lambda s: None)
    assert api.calls == ["保育園 シール貼り"]


def test_query_itself_is_never_sent_to_api(tmp_path):
    kw = _demand_file(tmp_path, [
        {"query": "たまごっちみみっち", "raw_query": "たまごっち みみっち", "volume": 50, "sites": ["a"]},
    ])
    api = FakeAPI({})
    P.run(kw, tmp_path / "out.json", 0, "Toys", 10, dry_run=False, api=api, sleeper=lambda s: None)
    assert "たまごっちみみっち" not in api.calls
    assert api.calls == ["たまごっち みみっち"]


# --------------------------------------------------------------------------
# レスポンス解釈・ジャンル判定
# --------------------------------------------------------------------------

def test_extract_items_reads_real_search_response_shape():
    res = _response(_item("B0REAL0001", "レゴ デュプロ"))
    items = P.extract_items(res)
    assert len(items) == 1
    assert items[0]["asin"] == "B0REAL0001"
    assert items[0]["title"] == "レゴ デュプロ"
    assert items[0]["browse_nodes"] == [{"id": "1", "name": "おもちゃ", "root": "おもちゃ"}]


@pytest.mark.parametrize("response", [
    None, {}, {"items": None}, "文字列",
    {"searchResult": None}, {"searchResult": {}}, {"searchResult": {"items": None}},
])
def test_extract_items_malformed_response_yields_empty_without_raising(response):
    assert P.extract_items(response) == []


def test_flattened_items_shape_still_works():
    res = {"items": [_item("B0FLAT0001", "つみき")]}
    assert P.extract_items(res)[0]["asin"] == "B0FLAT0001"


def test_non_toy_genre_item_not_counted_in_genre_pass_hits(tmp_path):
    """ジャンル外商品が genre_pass_hits に数えられないこと (genre_gate 再利用)。"""
    kw = _demand_file(tmp_path, [
        {"query": "つみき木製", "raw_query": "つみき 木製", "volume": 100, "sites": ["a"]},
    ])
    api = FakeAPI({"つみき 木製": _response(
        _item("B0TOY0001", "つみき 木製セット", nodes=[
            {"id": "1", "displayName": "おもちゃ", "ancestor": {"displayName": "おもちゃ"}}]),
        _item("B0OFF0001", "つみき 木製 文房具", nodes=[
            {"id": "999", "displayName": "文房具", "ancestor": {"displayName": "文房具"}}]),
    )})
    rep = P.run(kw, tmp_path / "out.json", 0, "Toys", 10, dry_run=False, api=api,
               sleeper=lambda s: None)
    r = rep["results"][0]
    assert r["hits"] == 2
    assert r["genre_pass_hits"] == 1


# --------------------------------------------------------------------------
# タイトル照合 / verdict
# --------------------------------------------------------------------------

def test_full_title_overlap_is_product():
    """正当な商品語: クエリの全トークンがタイトルに現れれば product。"""
    coverage = P.compute_title_overlap("レゴ デュプロ", ["レゴ デュプロ はじめてセット"])
    assert coverage == 1.0
    assert P.judge_verdict(hits=1, genre_pass_hits=1, coverage=coverage) == \
        ("product", "full_title_overlap")


def test_tamagotchi_shurui_type_is_non_product():
    """「たまごっち 種類」型: 主題 (たまごっち) はタイトルに現れるが「種類」は
    現れない → 一部一致 (0 < coverage < 1) は non_product にする。"""
    coverage = P.compute_title_overlap(
        "たまごっち 種類", ["バンダイ たまごっち スペシャルセット", "たまごっち にじいろ"])
    assert 0 < coverage < 1
    assert P.judge_verdict(hits=2, genre_pass_hits=2, coverage=coverage) == \
        ("non_product", "partial_title_overlap")


def test_no_overlap_at_all_is_ambiguous_not_product_nor_non_product():
    """ジャンルは通ったがタイトルにクエリ語が1つも現れない → 断定材料が無いので
    ambiguous (product にも non_product にも潰さない)。"""
    coverage = P.compute_title_overlap("みみっち", ["バンダイ たまごっち スペシャルセット"])
    assert coverage == 0.0
    assert P.judge_verdict(hits=1, genre_pass_hits=1, coverage=coverage) == \
        ("ambiguous", "zero_title_overlap")


def test_ambiguous_is_a_distinct_value_not_collapsed_into_product():
    verdict, _ = P.judge_verdict(hits=3, genre_pass_hits=2, coverage=0.0)
    assert verdict == "ambiguous"
    assert verdict != "product"


def test_zero_hits_is_non_product():
    assert P.judge_verdict(hits=0, genre_pass_hits=0, coverage=0.0) == \
        ("non_product", "no_hits")


def test_hits_but_no_genre_pass_is_non_product():
    assert P.judge_verdict(hits=3, genre_pass_hits=0, coverage=0.0) == \
        ("non_product", "no_genre_pass")


def test_end_to_end_verdict_via_probe_keyword_tamagotchi_shurui():
    api = FakeAPI({"たまごっち 種類": _response(
        _item("B0T0001", "バンダイ たまごっち スペシャルセット"),
    )})
    r = P.probe_keyword(api, "たまごっち種類", "たまごっち 種類", 18100, ["czech.hatenablog.com"],
                        "Toys", 10)
    assert r["verdict"] == "non_product"
    assert r["verdict_reason"] == "partial_title_overlap"


def test_end_to_end_verdict_via_probe_keyword_legit_product():
    api = FakeAPI({"レゴ デュプロ": _response(
        _item("B0L0001", "レゴ デュプロ はじめてのブロックセット"),
    )})
    r = P.probe_keyword(api, "レゴデュプロ", "レゴ デュプロ", 20000, ["toysrus"], "Toys", 10)
    assert r["verdict"] == "product"


# --------------------------------------------------------------------------
# API 例外 / dry-run / limit
# --------------------------------------------------------------------------

def test_api_exception_is_captured_per_keyword_and_verdict_is_ambiguous(tmp_path):
    kw = _demand_file(tmp_path, [
        {"query": "壊れる語", "raw_query": "壊れる 語", "volume": 10, "sites": ["a"]},
        {"query": "次の語", "raw_query": "次の 語", "volume": 9, "sites": ["a"]},
    ])
    api = FakeAPI({"次の 語": _response(_item("B0N0001", "次の 語 商品"))},
                 raises={"壊れる 語": RuntimeError("boom")})
    rep = P.run(kw, tmp_path / "out.json", 0, "Toys", 10, dry_run=False, api=api,
               sleeper=lambda s: None)
    assert len(rep["results"]) == 2, "1 語の失敗で全体を止めない"
    failed = [r for r in rep["results"] if r["query"] == "壊れる語"][0]
    assert failed["error"].startswith("RuntimeError")
    assert failed["verdict"] == "ambiguous"
    assert failed["verdict_reason"] == "api_error"
    ok = [r for r in rep["results"] if r["query"] == "次の語"][0]
    assert ok["error"] is None


def test_dry_run_makes_no_api_calls(tmp_path):
    kw = _demand_file(tmp_path, [
        {"query": "たまごっちみみっち", "raw_query": "たまごっち みみっち", "volume": 50, "sites": ["a"]},
    ])
    api = FakeAPI({})
    out = tmp_path / "out.json"
    rep = P.run(kw, out, 0, "Toys", 10, dry_run=True, api=api, sleeper=lambda s: None)
    assert api.calls == []
    assert not out.exists()
    assert rep["targets"][0]["raw_query"] == "たまごっち みみっち"


def test_limit_caps_targets_and_api_calls(tmp_path):
    kw = _demand_file(tmp_path, [
        {"query": f"語{i}", "raw_query": f"語 {i}", "volume": 100 - i, "sites": ["a"]}
        for i in range(5)
    ])
    api = FakeAPI({})
    rep = P.run(kw, tmp_path / "out.json", 2, "Toys", 10, dry_run=False, api=api,
               sleeper=lambda s: None)
    assert len(rep["results"]) == 2
    assert api.calls == ["語 0", "語 1"]


def test_targets_are_ordered_by_volume_desc(tmp_path):
    kw = _demand_file(tmp_path, [
        {"query": "小さい", "raw_query": "小さい", "volume": 10, "sites": ["a"]},
        {"query": "大きい", "raw_query": "大きい", "volume": 9999, "sites": ["a"]},
    ])
    api = FakeAPI({})
    rep = P.run(kw, tmp_path / "out.json", 0, "Toys", 10, dry_run=False, api=api,
               sleeper=lambda s: None)
    assert [r["query"] for r in rep["results"]] == ["大きい", "小さい"]


def test_sleep_between_calls_but_not_after_last(tmp_path):
    kw = _demand_file(tmp_path, [
        {"query": f"語{i}", "raw_query": f"語{i}", "volume": 1, "sites": ["a"]} for i in range(3)
    ])
    slept: list[float] = []
    P.run(kw, tmp_path / "out.json", 0, "Toys", 10, dry_run=False, api=FakeAPI({}),
         sleeper=slept.append)
    assert slept == [P.SLEEP_SECONDS, P.SLEEP_SECONDS]


# --------------------------------------------------------------------------
# 観測専用であること (raw/articles を一切触らない)
# --------------------------------------------------------------------------

def test_run_never_touches_amazon_raw_pool(tmp_path, monkeypatch):
    """data/raw/amazon.json を書かないこと。カレントディレクトリを見ない設計
    なので、cwd を tmp_path に切り替えても data/raw が作られないことで担保する。"""
    monkeypatch.chdir(tmp_path)
    kw = _demand_file(tmp_path, [
        {"query": "つみき", "raw_query": "つみき", "volume": 10, "sites": ["a"]},
    ])
    api = FakeAPI({"つみき": _response(_item("B0X", "つみき"))})
    P.run(kw, tmp_path / "out" / "probe.json", 0, "Toys", 10, dry_run=False, api=api,
         sleeper=lambda s: None)
    assert not (tmp_path / "data").exists()


# --------------------------------------------------------------------------
# ネットワークに出ないこと
# --------------------------------------------------------------------------

def test_module_does_not_import_network_libraries_at_call_time(tmp_path, monkeypatch):
    """dry-run / FakeAPI 経路では requests 等の実 HTTP レイヤーが呼ばれないこと。
    creators_api_client を遅延 import している設計を、api 未指定 dry-run では
    import すら発生しないことで確認する。"""
    import builtins
    orig_import = builtins.__import__
    blocked = []

    def _blocking_import(name, *args, **kwargs):
        if name == "creators_api_client":
            blocked.append(name)
            raise AssertionError("creators_api_client must not be imported in --dry-run")
        return orig_import(name, *args, **kwargs)

    kw = _demand_file(tmp_path, [
        {"query": "つみき", "raw_query": "つみき", "volume": 10, "sites": ["a"]},
    ])
    monkeypatch.setattr(builtins, "__import__", _blocking_import)
    rep = P.run(kw, tmp_path / "unused.json", 0, "Toys", 10,
               dry_run=True, api=None, sleeper=lambda s: None)
    assert blocked == []
    assert rep["summary"]["keywords_probed"] == 0
