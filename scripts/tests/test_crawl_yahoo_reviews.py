"""scripts/crawl_yahoo_reviews.py unit tests (#3203 Phase 2 Lane 2)."""
from __future__ import annotations

import json
import pathlib

import pytest
import requests

from scripts.crawl_yahoo_reviews import (
    BotWallDetected,
    RequestBudget,
    crawl_asin,
    default_raw_dir,
    extract_reviews,
    fetch_with_retry,
    get_jan_code,
    is_fresh,
    lookup_yahoo_review,
    run,
)


class _FakeResponse:
    def __init__(self, json_body=None, status=200, text=""):
        self._json = json_body
        self.status_code = status
        self.text = text

    def json(self):
        if self._json is None:
            raise ValueError("no json body")
        return self._json


class _FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls += 1
        resp = self._responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp


# --------------------------------------------------------------------------
# get_jan_code
# --------------------------------------------------------------------------

def _write_amazon_json(base: pathlib.Path, asin: str, payload: dict) -> None:
    d = base / asin
    d.mkdir(parents=True)
    (d / "amazon.json").write_text(json.dumps(payload), encoding="utf-8")


def test_get_jan_code_from_flat_item(tmp_path):
    _write_amazon_json(tmp_path, "B0JANFLAT1", {"asin": "B0JANFLAT1", "jan_code": "4901234567890"})
    assert get_jan_code("B0JANFLAT1", tmp_path) == "4901234567890"


def test_get_jan_code_from_nested_item(tmp_path):
    _write_amazon_json(tmp_path, "B0JANNEST1", {"item": {"asin": "B0JANNEST1", "jan_code": "4909876543210"}})
    assert get_jan_code("B0JANNEST1", tmp_path) == "4909876543210"


def test_get_jan_code_none_when_empty_or_missing(tmp_path):
    _write_amazon_json(tmp_path, "B0JANEMPTY", {"asin": "B0JANEMPTY", "jan_code": ""})
    assert get_jan_code("B0JANEMPTY", tmp_path) is None
    assert get_jan_code("B0NOFILE01", tmp_path) is None


# --------------------------------------------------------------------------
# lookup_yahoo_review (itemSearch v3, mocked)
# --------------------------------------------------------------------------

def test_lookup_yahoo_review_returns_rate_count_url():
    body = {"hits": [{"name": "商品", "review": {"rate": 4.35, "count": 12,
                                                "url": "https://shopping.yahoo.co.jp/review/xyz"}}]}
    session = _FakeSession([_FakeResponse(body)])
    result = lookup_yahoo_review("4901234567890", "client-id", session, sleeper=lambda s: None)
    assert result == {"rate": 4.35, "count": 12, "url": "https://shopping.yahoo.co.jp/review/xyz"}


def test_lookup_yahoo_review_none_on_http_error():
    session = _FakeSession([_FakeResponse(status=429, text="rate limited")])
    assert lookup_yahoo_review("4901234567890", "cid", session, sleeper=lambda s: None) is None


def test_lookup_yahoo_review_none_on_network_error():
    session = _FakeSession([requests.ConnectionError("boom")])
    assert lookup_yahoo_review("4901234567890", "cid", session, sleeper=lambda s: None) is None


def test_lookup_yahoo_review_none_when_no_hits():
    session = _FakeSession([_FakeResponse({"hits": []})])
    assert lookup_yahoo_review("4901234567890", "cid", session, sleeper=lambda s: None) is None


def test_lookup_yahoo_review_none_when_hit_has_no_review():
    session = _FakeSession([_FakeResponse({"hits": [{"name": "商品"}]})])
    assert lookup_yahoo_review("4901234567890", "cid", session, sleeper=lambda s: None) is None


def test_lookup_yahoo_review_sleeps_for_api_politeness():
    sleeps = []
    body = {"hits": [{"review": {"rate": 4.0, "count": 1, "url": "https://x/r"}}]}
    session = _FakeSession([_FakeResponse(body)])
    lookup_yahoo_review("4901234567890", "cid", session, sleeper=lambda s: sleeps.append(s))
    assert sleeps == [1.0]


def test_lookup_yahoo_review_coerces_malformed_rate_count():
    body = {"hits": [{"review": {"rate": "not a number", "count": None, "url": "https://x/r"}}]}
    session = _FakeSession([_FakeResponse(body)])
    result = lookup_yahoo_review("4901234567890", "cid", session, sleeper=lambda s: None)
    assert result == {"rate": 0.0, "count": 0, "url": "https://x/r"}


# --------------------------------------------------------------------------
# extract_reviews (schema.org JSON-LD)
# --------------------------------------------------------------------------

def test_extract_reviews_parses_ldjson_review_list():
    html = """
    <html><head>
    <script type="application/ld+json">
    {"@type": "Product", "review": [
        {"@type": "Review", "reviewBody": "とても満足しています", "name": "満足",
         "reviewRating": {"ratingValue": "5"}, "datePublished": "2026-06-01"},
        {"@type": "Review", "reviewBody": "普通でした", "reviewRating": {"ratingValue": "3"}}
    ]}
    </script>
    </head><body></body></html>
    """
    reviews = extract_reviews(html)
    assert len(reviews) == 2
    assert reviews[0]["rating"] == 5.0
    assert reviews[0]["body"] == "とても満足しています"
    assert reviews[0]["posted_at"] == "2026-06-01"
    assert reviews[1]["rating"] == 3.0


def test_extract_reviews_empty_when_no_ldjson():
    assert extract_reviews("<html><body>no structured data</body></html>") == []


def test_extract_reviews_handles_malformed_json_gracefully():
    html = '<script type="application/ld+json">{not valid json</script>'
    assert extract_reviews(html) == []


def test_extract_reviews_skips_review_without_body():
    html = """
    <script type="application/ld+json">
    {"@type": "Review", "name": "タイトルのみ", "reviewRating": {"ratingValue": "4"}}
    </script>
    """
    assert extract_reviews(html) == []


# --------------------------------------------------------------------------
# RequestBudget
# --------------------------------------------------------------------------

def test_request_budget_exhausts_at_max():
    budget = RequestBudget(2)
    assert budget.try_consume() is True
    assert budget.try_consume() is True
    assert budget.try_consume() is False
    assert budget.exhausted() is True


def test_request_budget_unlimited_when_zero():
    budget = RequestBudget(0)
    for _ in range(100):
        assert budget.try_consume() is True
    assert budget.exhausted() is False


# --------------------------------------------------------------------------
# fetch_with_retry: レート sleep が 15 秒以上で呼ばれること
# --------------------------------------------------------------------------

def test_fetch_with_retry_sleeps_between_15_and_30_seconds():
    sleep_calls = []
    session = _FakeSession([_FakeResponse(status=200, text="<html>ok</html>")])
    budget = RequestBudget(10)
    result = fetch_with_retry(
        "https://shopping.yahoo.co.jp/review/xyz", session, budget,
        sleeper=lambda s: sleep_calls.append(s),
    )
    assert result == "<html>ok</html>"
    assert len(sleep_calls) == 1
    assert 15 <= sleep_calls[0] <= 30


def test_fetch_with_retry_retries_once_then_gives_up():
    session = _FakeSession([
        requests.ConnectionError("boom"),
        requests.ConnectionError("boom again"),
    ])
    budget = RequestBudget(10)
    result = fetch_with_retry("https://x/y", session, budget, sleeper=lambda s: None)
    assert result is None
    assert session.calls == 2  # 初回 + リトライ1回 = 最大2回


def test_fetch_with_retry_raises_on_bot_wall():
    session = _FakeSession([_FakeResponse(status=403, text="captcha challenge")])
    budget = RequestBudget(10)
    with pytest.raises(BotWallDetected):
        fetch_with_retry("https://x/y", session, budget, sleeper=lambda s: None)


def test_fetch_with_retry_returns_none_on_404():
    session = _FakeSession([_FakeResponse(status=404, text="not found")])
    budget = RequestBudget(10)
    result = fetch_with_retry("https://x/y", session, budget, sleeper=lambda s: None)
    assert result is None


def test_fetch_with_retry_stops_when_budget_exhausted():
    session = _FakeSession([])
    budget = RequestBudget(1)
    budget.consumed = 1
    result = fetch_with_retry("https://x/y", session, budget, sleeper=lambda s: None)
    assert result is None
    assert session.calls == 0


# --------------------------------------------------------------------------
# crawl_asin: jan 無し skip / count 0 skip / API エラー skip / api_review のみ保存
# --------------------------------------------------------------------------

def test_crawl_asin_skips_when_no_jan(tmp_path):
    _write_amazon_json(tmp_path, "B0NOJAN001", {"asin": "B0NOJAN001"})
    result = crawl_asin("B0NOJAN001", client_id="cid", base=tmp_path,
                        session=_FakeSession([]), budget=RequestBudget(10),
                        sleeper=lambda s: None)
    assert result is None


def test_crawl_asin_skips_when_api_lookup_fails(tmp_path):
    _write_amazon_json(tmp_path, "B0APIERR01", {"asin": "B0APIERR01", "jan_code": "490111"})
    session = _FakeSession([requests.ConnectionError("api down")])
    result = crawl_asin("B0APIERR01", client_id="cid", base=tmp_path,
                        session=session, budget=RequestBudget(10), sleeper=lambda s: None)
    assert result is None


def test_crawl_asin_count_zero_saves_api_review_without_crawl(tmp_path):
    _write_amazon_json(tmp_path, "B0ZEROCNT1", {"asin": "B0ZEROCNT1", "jan_code": "490222"})
    body = {"hits": [{"review": {"rate": 0.0, "count": 0, "url": "https://x/review"}}]}
    session = _FakeSession([_FakeResponse(body)])  # crawl fetch は積まない = 呼ばれない
    budget = RequestBudget(10)
    result = crawl_asin("B0ZEROCNT1", client_id="cid", base=tmp_path,
                        session=session, budget=budget, sleeper=lambda s: None)
    assert result is not None
    assert result["api_review"]["count"] == 0
    assert result["reviews"] == []
    assert budget.consumed == 0  # レビューページを crawl していない


def test_crawl_asin_bot_wall_keeps_api_review(tmp_path, monkeypatch):
    import scripts.crawl_yahoo_reviews as mod
    _write_amazon_json(tmp_path, "B0BOTWALL1", {"asin": "B0BOTWALL1", "jan_code": "490333"})
    monkeypatch.setattr(mod, "robots_allowed", lambda url, ua=mod.HONEST_UA: True)
    body = {"hits": [{"review": {"rate": 4.5, "count": 3, "url": "https://x/review"}}]}
    session = _FakeSession([
        _FakeResponse(body),                              # itemSearch v3
        _FakeResponse(status=403, text="captcha here"),   # crawl → bot wall
    ])
    result = crawl_asin("B0BOTWALL1", client_id="cid", base=tmp_path,
                        session=session, budget=RequestBudget(10), sleeper=lambda s: None)
    assert result is not None
    assert result["api_review"] == {"rate": 4.5, "count": 3, "url": "https://x/review"}
    assert result["reviews"] == []  # 本文は取れなくても api_review が成果


def test_crawl_asin_crawls_and_extracts_when_count_positive(tmp_path, monkeypatch):
    import scripts.crawl_yahoo_reviews as mod
    _write_amazon_json(tmp_path, "B0CRAWLOK1", {"asin": "B0CRAWLOK1", "jan_code": "490444"})
    monkeypatch.setattr(mod, "robots_allowed", lambda url, ua=mod.HONEST_UA: True)
    body = {"hits": [{"review": {"rate": 4.0, "count": 2, "url": "https://x/review"}}]}
    html = ('<script type="application/ld+json">'
            '{"@type": "Review", "reviewBody": "実際に良かったです", "reviewRating": {"ratingValue": "5"}}'
            '</script>')
    session = _FakeSession([
        _FakeResponse(body),
        _FakeResponse(status=200, text=html),
    ])
    result = crawl_asin("B0CRAWLOK1", client_id="cid", base=tmp_path,
                        session=session, budget=RequestBudget(10), sleeper=lambda s: None)
    assert result is not None
    assert len(result["reviews"]) == 1
    assert result["reviews"][0]["body"] == "実際に良かったです"
    assert result["jan_code"] == "490444"


def test_crawl_asin_robots_disallow_keeps_api_review(tmp_path, monkeypatch):
    import scripts.crawl_yahoo_reviews as mod
    _write_amazon_json(tmp_path, "B0ROBOTS01", {"asin": "B0ROBOTS01", "jan_code": "490555"})
    monkeypatch.setattr(mod, "robots_allowed", lambda url, ua=mod.HONEST_UA: False)
    body = {"hits": [{"review": {"rate": 3.8, "count": 7, "url": "https://x/review"}}]}
    session = _FakeSession([_FakeResponse(body)])
    budget = RequestBudget(10)
    result = crawl_asin("B0ROBOTS01", client_id="cid", base=tmp_path,
                        session=session, budget=budget, sleeper=lambda s: None)
    assert result is not None
    assert result["api_review"]["count"] == 7
    assert result["reviews"] == []
    assert budget.consumed == 0


# --------------------------------------------------------------------------
# is_fresh / default_raw_dir
# --------------------------------------------------------------------------

def test_is_fresh_true_within_window(tmp_path):
    from datetime import datetime, timezone
    p = tmp_path / "B0X.json"
    p.write_text(json.dumps({"fetched_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}),
                 encoding="utf-8")
    assert is_fresh(p, 30) is True


def test_is_fresh_false_when_missing(tmp_path):
    assert is_fresh(tmp_path / "nope.json", 30) is False


def test_default_raw_dir_uses_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("EXPERIENCE_RAW_DIR", str(tmp_path / "custom"))
    assert default_raw_dir() == tmp_path / "custom"


# --------------------------------------------------------------------------
# run(): YAHOO_CLIENT_ID 無し lane skip / EXPERIENCE_RAW_DIR 外に書かない /
#        refresh-days skip
# --------------------------------------------------------------------------

def test_run_skips_whole_lane_without_client_id(tmp_path, monkeypatch):
    monkeypatch.delenv("YAHOO_CLIENT_ID", raising=False)
    raw_dir = tmp_path / "raw"
    summary = run(["B0A0000001", "B0B0000001"], raw_dir=raw_dir, client_id="",
                  sleeper=lambda s: None)
    assert summary == {"targets": 2, "written": 0, "skipped": 2, "requests_made": 0}
    assert not raw_dir.exists()


def test_run_writes_only_under_raw_dir(tmp_path, monkeypatch):
    import scripts.crawl_yahoo_reviews as mod

    raw_dir = tmp_path / "raw_only_here"
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    monkeypatch.chdir(repo_dir)

    payload = {"asin": "B0CRAWL0001", "jan_code": "4901", "fetched_at": "2026-07-01T00:00:00Z",
               "api_review": {"rate": 4.2, "count": 5, "url": "https://x/r"},
               "reviews": [{"rating": 5, "title": "t", "body": "b", "posted_at": "2026-06-01"}]}
    monkeypatch.setattr(mod, "crawl_asin", lambda asin, **kw: payload)

    summary = run(["B0CRAWL0001"], raw_dir=raw_dir, client_id="cid", sleeper=lambda s: None)
    assert summary["written"] == 1
    out_path = raw_dir / "B0CRAWL0001.json"
    assert out_path.exists()
    # リポジトリ配下 (data/raw 等) には一切書かれていない
    assert not (repo_dir / "data").exists()


def test_run_skips_fresh_asin(tmp_path, monkeypatch):
    import scripts.crawl_yahoo_reviews as mod
    from datetime import datetime, timezone

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "B0FRESH0001.json").write_text(json.dumps({
        "asin": "B0FRESH0001",
        "fetched_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "api_review": {"rate": 4.0, "count": 1, "url": ""},
        "reviews": [],
    }), encoding="utf-8")

    called = []
    monkeypatch.setattr(mod, "crawl_asin", lambda asin, **kw: called.append(asin))

    summary = run(["B0FRESH0001"], raw_dir=raw_dir, client_id="cid",
                  refresh_days=30, sleeper=lambda s: None)
    assert called == []
    assert summary["written"] == 0


def test_run_processes_all_targets_within_budget(tmp_path, monkeypatch):
    import scripts.crawl_yahoo_reviews as mod

    calls = []

    def _fake_crawl(asin, **kw):
        calls.append(asin)
        return None

    monkeypatch.setattr(mod, "crawl_asin", _fake_crawl)
    raw_dir = tmp_path / "raw"
    summary = run(["B0A0000001", "B0B0000001"], raw_dir=raw_dir, client_id="cid",
                  max_requests=100, sleeper=lambda s: None)
    assert calls == ["B0A0000001", "B0B0000001"]
    assert summary["targets"] == 2


# --------------------------------------------------------------------------
# DOM 経路 (#6641)
# --------------------------------------------------------------------------
#
# JSON-LD が廃止されたことに抽出器が追随できず、**2 ヶ月 65 ASIN で 0 件**の
# まま緑で回っていた。fixture は実際のレビューページから切り出した 3 件。
#
# ここで守りたいのは 2 つ:
#   1. DOM から取れること
#   2. **class 名のハッシュを足場にしていないこと** — `__uzkwW` は Yahoo の
#      ビルドごとに変わる。完全一致で書くと次のデプロイでまた黙って 0 になる

import re as _re

from scripts import crawl_yahoo_reviews as _cyr

_FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "yahoo_review_page.html"


def _fixture_html() -> str:
    return _FIXTURE.read_text(encoding="utf-8")


def test_dom_extraction_finds_reviews():
    reviews = _cyr.extract_reviews(_fixture_html())
    assert len(reviews) == 3
    assert all(r["body"] for r in reviews)


def test_dom_extraction_reads_rating_from_aria_label():
    reviews = _cyr.extract_reviews(_fixture_html())
    assert [r["rating"] for r in reviews] == [5.0, 5.0, 5.0]


def test_dom_extraction_prefers_iso_timestamp_attribute():
    """表示用の "2026/3/20" ではなく data 属性の ISO8601 を採ること。"""
    reviews = _cyr.extract_reviews(_fixture_html())
    for r in reviews:
        assert _re.match(r"^\d{4}-\d{2}-\d{2}T", r["posted_at"]), r["posted_at"]


def test_class_selectors_carry_no_build_hash():
    """照合に使う class 名にビルドハッシュが入っていないこと。

    `style_ReviewDetail__reviewBody__uzkwW` の `__uzkwW` を焼き込むと、Yahoo の
    次のデプロイでまた黙って 0 件になる。#6641 の再発条件そのもの。
    """
    for name in (_cyr._BODY_CLASS, _cyr._TITLE_CLASS, _cyr._TIME_CLASS):
        # `Block__element` までは持ってよいが、その先の `__hash` は持たない
        assert name.count("__") == 1, name
        assert not name.endswith("__"), name


def test_extraction_survives_class_hash_change():
    """ハッシュが変わっても取れること (部分一致で見ている証拠)。"""
    html = _fixture_html().replace("reviewBody__uzkwW", "reviewBody__ZZZZZ")
    assert len(_cyr.extract_reviews(html)) == 3


def test_ldjson_path_still_works():
    """JSON-LD が戻ってきたときのために経路を残していること。"""
    html = """<html><body><script type="application/ld+json">
    {"@type": "Product", "review": [
      {"@type": "Review", "reviewBody": "とても良い", "name": "満足",
       "datePublished": "2026-01-02", "reviewRating": {"ratingValue": 4}}]}
    </script></body></html>"""
    reviews = _cyr.extract_reviews(html)
    assert len(reviews) == 1
    assert reviews[0]["body"] == "とても良い"
    assert reviews[0]["rating"] == 4.0


def test_duplicate_bodies_are_collapsed():
    """ピックアップと一覧で同じレビューが二重に出るのを 1 件にする。"""
    one = _fixture_html()
    doubled = one.replace("</body>", "") + one.split("<body>", 1)[1]
    assert len(_cyr.extract_reviews(doubled)) == 3


def test_empty_page_returns_empty_list():
    assert _cyr.extract_reviews("<html><body>なにもない</body></html>") == []


def test_dom_wins_when_ldjson_is_gone():
    """実際に起きた状況: JSON-LD が 0 ブロック、DOM に本文がある。"""
    html = _fixture_html()
    assert _cyr._reviews_from_ldjson(html) == []
    assert len(_cyr._reviews_from_dom(html)) == 3
    assert len(_cyr.extract_reviews(html)) == 3


# --------------------------------------------------------------------------
# 「取れるはずが 0」の検知 (#6641)
# --------------------------------------------------------------------------
#
# セレクタを直すだけでは同じことがまた起きる。2 ヶ月気づかなかったのは
# 「API はあると言っているのに 0 件だった」を誰も数えていなかったから。

def _payload(asin, api_count, n_bodies):
    return {"asin": asin, "jan_code": "4901", "fetched_at": "2026-09-01T00:00:00Z",
            "api_review": {"rate": 4.2, "count": api_count, "url": "https://x/r"},
            "reviews": [{"rating": 5, "title": "t", "body": f"b{i}", "posted_at": ""}
                        for i in range(n_bodies)]}


def _run_with(monkeypatch, tmp_path, payloads):
    import scripts.crawl_yahoo_reviews as mod
    by_asin = {p["asin"]: p for p in payloads}
    monkeypatch.setattr(mod, "crawl_asin", lambda asin, **kw: by_asin[asin])
    return run(list(by_asin), raw_dir=tmp_path / "raw", client_id="cid",
               sleeper=lambda s: None)


def test_summary_counts_empty_bodies(tmp_path, monkeypatch):
    summary = _run_with(monkeypatch, tmp_path, [
        _payload("B00000000A", api_count=5, n_bodies=3),
        _payload("B00000000B", api_count=2, n_bodies=0),
    ])
    assert summary["with_api_reviews"] == 2
    assert summary["empty_bodies"] == 1
    assert summary["empty_body_rate"] == 0.5


def test_asin_without_api_reviews_is_not_counted(tmp_path, monkeypatch):
    """レビューが無い商品は分母に入れない (0 件で当然なので)。"""
    summary = _run_with(monkeypatch, tmp_path, [
        _payload("B00000000A", api_count=0, n_bodies=0),
        _payload("B00000000B", api_count=4, n_bodies=2),
    ])
    assert summary["with_api_reviews"] == 1
    assert summary["empty_bodies"] == 0


def test_extractor_suspect_flag_when_everything_is_empty(tmp_path, monkeypatch):
    """#6641 の状態 (65/65 が 0 件) を再現したら旗が立つこと。"""
    summary = _run_with(monkeypatch, tmp_path, [
        _payload(f"B0000000{i:02d}", api_count=3, n_bodies=0) for i in range(5)
    ])
    assert summary["empty_body_rate"] == 1.0
    assert summary.get("extractor_suspect") is True


def test_single_empty_asin_does_not_raise_the_flag(tmp_path, monkeypatch):
    """1 件の 0 件は普通に起こる。割合で見ているので鳴らない。"""
    payloads = [_payload(f"B0000000{i:02d}", api_count=3, n_bodies=2) for i in range(4)]
    payloads.append(_payload("B0000000ZZ", api_count=3, n_bodies=0))
    summary = _run_with(monkeypatch, tmp_path, payloads)
    assert summary["empty_body_rate"] == 0.2
    assert "extractor_suspect" not in summary


# --------------------------------------------------------------------------
# 0 件ファイルの自己修復 (#6641)
# --------------------------------------------------------------------------

def _write_raw(tmp_path, *, days_ago, api_count, n_reviews):
    import datetime as _dt
    when = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days_ago)
    path = tmp_path / "B0000000AA.json"
    path.write_text(json.dumps({
        "asin": "B0000000AA", "jan_code": "4901",
        "fetched_at": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "api_review": {"rate": 4.0, "count": api_count, "url": "https://x/r"},
        "reviews": [{"body": f"b{i}"} for i in range(n_reviews)],
    }, ensure_ascii=False), encoding="utf-8")
    return path


def test_successful_fetch_stays_fresh_for_the_full_window(tmp_path):
    path = _write_raw(tmp_path, days_ago=10, api_count=5, n_reviews=3)
    assert _cyr.is_fresh(path, 30) is True


def test_empty_result_expires_early(tmp_path):
    """本文 0 件は取得成功ではない。30 日ふさがない (#6641 の 65 件がこれ)。"""
    path = _write_raw(tmp_path, days_ago=10, api_count=5, n_reviews=0)
    assert _cyr.is_fresh(path, 30) is False


def test_empty_result_is_not_retried_every_night(tmp_path):
    """低速クロール規律。短くはするが毎晩は叩かない。"""
    path = _write_raw(tmp_path, days_ago=1, api_count=5, n_reviews=0)
    assert _cyr.is_fresh(path, 30) is True


def test_product_with_no_reviews_at_all_is_not_retried_early(tmp_path):
    """レビューが元から無い商品は 0 件が正しい結果。早く取り直す理由がない。"""
    path = _write_raw(tmp_path, days_ago=10, api_count=0, n_reviews=0)
    assert _cyr.is_fresh(path, 30) is True
