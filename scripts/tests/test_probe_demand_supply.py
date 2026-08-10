"""需要キーワードの Amazon 供給観測 (#2686 shadow レーン) の検査。"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import probe_demand_supply as P  # noqa: E402


class FakeAPI:
    """search_items を差し替えるだけの偽 API。呼ばれた keyword を記録する。"""

    def __init__(self, responses: dict, raises: dict | None = None):
        self.responses = responses
        self.raises = raises or {}
        self.calls: list[str] = []

    def search_items(self, keywords=None, search_index=None, item_count=None, item_page=None,
                     resources=None):
        self.calls.append(keywords)
        if keywords in self.raises:
            raise self.raises[keywords]
        return self.responses.get(keywords, {"items": []})


def _items(*asins):
    return {"items": [{"asin": a, "itemInfo": {"title": {"displayValue": a}}} for a in asins]}


def _keywords_file(tmp_path, entries):
    p = tmp_path / "demand_keywords.json"
    p.write_text(json.dumps({"keywords": entries}, ensure_ascii=False), encoding="utf-8")
    return p


def _articles_dir(tmp_path, slugs):
    d = tmp_path / "articles"
    d.mkdir()
    for s in slugs:
        (d / f"{s}.json").write_text("{}", encoding="utf-8")
    return d


# --------------------------------------------------------------------------
# 既存 ASIN の収集
# --------------------------------------------------------------------------

def test_existing_asins_come_from_slugs_and_skip_sidecars(tmp_path):
    d = _articles_dir(tmp_path, ["2026-08-10-B01234ABCD", "2026-08-10-B09999ZZZZ"])
    (d / "2026-08-10-B01234ABCD.quality.json").write_text("{}", encoding="utf-8")
    (d / "2026-08-10-B01234ABCD.seo.json").write_text("{}", encoding="utf-8")
    (d / "not-a-slug.json").write_text("{}", encoding="utf-8")
    assert P.load_existing_asins(d) == {"B01234ABCD", "B09999ZZZZ"}


def test_missing_articles_dir_returns_empty(tmp_path):
    assert P.load_existing_asins(tmp_path / "nope") == set()


# --------------------------------------------------------------------------
# レスポンス解釈
# --------------------------------------------------------------------------

@pytest.mark.parametrize("response", [None, {}, {"items": None}, {"items": [1, 2]}, "文字列"])
def test_malformed_response_yields_no_asins_without_raising(response):
    assert P.extract_asins(response) == []


def test_asins_are_extracted_in_order():
    assert P.extract_asins(_items("B0A", "B0B")) == ["B0A", "B0B"]


# --------------------------------------------------------------------------
# 1 キーワードの観測
# --------------------------------------------------------------------------

def test_new_and_known_asins_are_separated():
    api = FakeAPI({"トミカ収納": _items("B0NEW00001", "B0OLD00001")})
    r = P.probe_keyword(api, "トミカ収納", {"B0OLD00001"}, "Toys", 10)
    assert r["hits"] == 2
    assert r["new_asins"] == ["B0NEW00001"]
    assert r["known_asins"] == ["B0OLD00001"]
    assert r["error"] is None


def test_api_error_is_captured_per_keyword_not_raised():
    api = FakeAPI({}, raises={"壊れる語": RuntimeError("boom")})
    r = P.probe_keyword(api, "壊れる語", set(), "Toys", 10)
    assert r["error"].startswith("RuntimeError")
    assert r["hits"] == 0


def test_zero_hit_keyword_is_recorded_as_zero_not_error():
    """メロジョイ型 (Amazon に商品が無い) と API 障害を混同しないこと。"""
    api = FakeAPI({"供給なし": {"items": []}})
    r = P.probe_keyword(api, "供給なし", set(), "Toys", 10)
    assert r["hits"] == 0 and r["error"] is None


# --------------------------------------------------------------------------
# 集計
# --------------------------------------------------------------------------

def test_summary_separates_zero_hit_from_all_known():
    """「供給が無い」と「供給はあるが全部既出」は打ち手が違うので区別する。"""
    results = [
        {"keyword": "a", "error": None, "hits": 0, "new_asins": [], "known_asins": []},
        {"keyword": "b", "error": None, "hits": 2, "new_asins": [], "known_asins": ["B0X", "B0Y"]},
        {"keyword": "c", "error": None, "hits": 2, "new_asins": ["B0Z"], "known_asins": ["B0X"]},
        {"keyword": "d", "error": "boom", "hits": 0, "new_asins": [], "known_asins": []},
    ]
    s = P.summarize(results)
    assert s == {"keywords_probed": 4, "zero_hit": 1, "hits_but_all_known": 1,
                 "usable": 1, "error_count": 1, "new_asin_total": 1}


def test_new_asin_total_is_deduped_across_keywords():
    results = [
        {"keyword": "a", "error": None, "hits": 1, "new_asins": ["B0SAME"], "known_asins": []},
        {"keyword": "b", "error": None, "hits": 1, "new_asins": ["B0SAME"], "known_asins": []},
    ]
    assert P.summarize(results)["new_asin_total"] == 1


# --------------------------------------------------------------------------
# run (観測のみであることの担保)
# --------------------------------------------------------------------------

def test_run_writes_report_and_never_touches_amazon_pool(tmp_path):
    """data/raw/amazon.json を書かない = Jules の生成プールに入らないこと。"""
    kw = _keywords_file(tmp_path, [{"keyword": "トミカ収納", "wp_impressions": 29421}])
    arts = _articles_dir(tmp_path, ["2026-08-10-B0OLD00001"])
    out = tmp_path / "out" / "probe.json"
    raw = tmp_path / "raw"
    raw.mkdir()
    api = FakeAPI({"トミカ収納": _items("B0NEW00001", "B0OLD00001")})

    rep = P.run(kw, arts, out, 0, "Toys", 10, dry_run=False, api=api, sleeper=lambda s: None)

    assert out.exists()
    assert list(raw.iterdir()) == [], "raw/ には何も書かないこと"
    assert rep["summary"]["usable"] == 1
    assert rep["results"][0]["new_asins"] == ["B0NEW00001"]


def test_dry_run_makes_no_api_calls(tmp_path):
    kw = _keywords_file(tmp_path, [{"keyword": "トミカ収納", "wp_impressions": 1}])
    arts = _articles_dir(tmp_path, [])
    out = tmp_path / "probe.json"
    api = FakeAPI({})
    P.run(kw, arts, out, 0, "Toys", 10, dry_run=True, api=api, sleeper=lambda s: None)
    assert api.calls == []
    assert not out.exists()


def test_limit_caps_api_calls(tmp_path):
    kw = _keywords_file(tmp_path, [
        {"keyword": f"語{i}", "wp_impressions": 100 - i} for i in range(5)])
    arts = _articles_dir(tmp_path, [])
    api = FakeAPI({})
    P.run(kw, arts, tmp_path / "o.json", 2, "Toys", 10, False, api=api, sleeper=lambda s: None)
    assert api.calls == ["語0", "語1"]


def test_results_are_ordered_by_demand(tmp_path):
    kw = _keywords_file(tmp_path, [
        {"keyword": "小さい需要", "wp_impressions": 10},
        {"keyword": "大きい需要", "wp_impressions": 9999},
    ])
    arts = _articles_dir(tmp_path, [])
    api = FakeAPI({})
    rep = P.run(kw, arts, tmp_path / "o.json", 0, "Toys", 10, False,
                api=api, sleeper=lambda s: None)
    assert [r["keyword"] for r in rep["results"]] == ["大きい需要", "小さい需要"]


def test_sleep_between_calls_but_not_after_last(tmp_path):
    """レート配慮。最後の 1 回のあとに無駄に待たない。"""
    kw = _keywords_file(tmp_path, [{"keyword": f"語{i}", "wp_impressions": 1} for i in range(3)])
    arts = _articles_dir(tmp_path, [])
    slept: list[float] = []
    P.run(kw, arts, tmp_path / "o.json", 0, "Toys", 10, False,
          api=FakeAPI({}), sleeper=slept.append)
    assert slept == [P.SLEEP_SECONDS, P.SLEEP_SECONDS]
