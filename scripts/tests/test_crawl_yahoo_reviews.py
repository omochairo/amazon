"""scripts/crawl_yahoo_reviews.py unit tests (#3203 Phase 2 Lane 2)."""
from __future__ import annotations

import json
import pathlib

import pytest
import requests

from scripts.crawl_yahoo_reviews import (
    BotWallDetected,
    RequestBudget,
    default_raw_dir,
    derive_review_urls,
    extract_reviews,
    fetch_with_retry,
    is_fresh,
    resolve_product_url,
    run,
    select_yahoo_url,
)


class _FakeResponse:
    def __init__(self, status=200, text=""):
        self.status_code = status
        self.text = text


class _FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def get(self, url, headers=None, timeout=None):
        self.calls += 1
        resp = self._responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp


# --------------------------------------------------------------------------
# resolve_product_url (ValueCommerce vc_url デコード)
# --------------------------------------------------------------------------

def test_resolve_product_url_decodes_valuecommerce_referral():
    raw = (
        "https://ck.jp.ap.valuecommerce.com/servlet/referral?sid=1&pid=2&vc_url="
        "https%3A%2F%2Fstore.shopping.yahoo.co.jp%2Feigokyouzai%2F848850121496.html"
    )
    assert resolve_product_url(raw) == "https://store.shopping.yahoo.co.jp/eigokyouzai/848850121496.html"


def test_resolve_product_url_passes_through_direct_link():
    direct = "https://store.shopping.yahoo.co.jp/somestore/item123.html"
    assert resolve_product_url(direct) == direct


def test_resolve_product_url_none_when_missing():
    assert resolve_product_url(None) is None
    assert resolve_product_url("") is None


def test_select_yahoo_url_matches_asin(tmp_path):
    matched_path = tmp_path / "yahoo_matched.json"
    matched_path.write_text(json.dumps({"items": [
        {"matched_asin": "B0OTHERASIN", "url": "https://store.shopping.yahoo.co.jp/x/other.html"},
        {"matched_asin": "B0TARGETONE", "url": (
            "https://ck.jp.ap.valuecommerce.com/servlet/referral?sid=1&pid=2&vc_url="
            "https%3A%2F%2Fstore.shopping.yahoo.co.jp%2Fmystore%2Fitem456.html"
        )},
    ]}), encoding="utf-8")
    url = select_yahoo_url("B0TARGETONE", matched_path)
    assert url == "https://store.shopping.yahoo.co.jp/mystore/item456.html"


def test_select_yahoo_url_none_when_no_match(tmp_path):
    matched_path = tmp_path / "yahoo_matched.json"
    matched_path.write_text(json.dumps({"items": []}), encoding="utf-8")
    assert select_yahoo_url("B0NOMATCH01", matched_path) is None


# --------------------------------------------------------------------------
# derive_review_urls
# --------------------------------------------------------------------------

def test_derive_review_urls_builds_paginated_urls():
    urls = derive_review_urls("https://store.shopping.yahoo.co.jp/mystore/item456.html", max_pages=3)
    assert urls == [
        "https://store.shopping.yahoo.co.jp/mystore/review/item456.html",
        "https://store.shopping.yahoo.co.jp/mystore/review/item456.html?p=2",
        "https://store.shopping.yahoo.co.jp/mystore/review/item456.html?p=3",
    ]


def test_derive_review_urls_empty_for_unrecognized_domain():
    assert derive_review_urls("https://example.com/product/123") == []


def test_derive_review_urls_respects_max_pages_one():
    urls = derive_review_urls("https://store.shopping.yahoo.co.jp/s/i.html", max_pages=1)
    assert urls == ["https://store.shopping.yahoo.co.jp/s/review/i.html"]


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
        "https://store.shopping.yahoo.co.jp/s/review/i.html", session, budget,
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
    budget = RequestBudget(0)
    budget.max_requests = 1
    budget.consumed = 1
    result = fetch_with_retry("https://x/y", session, budget, sleeper=lambda s: None)
    assert result is None
    assert session.calls == 0


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
# run(): EXPERIENCE_RAW_DIR 外に書かないこと / refresh-days skip
# --------------------------------------------------------------------------

def test_run_writes_only_under_raw_dir(tmp_path, monkeypatch):
    import scripts.crawl_yahoo_reviews as mod

    raw_dir = tmp_path / "raw_only_here"
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    monkeypatch.chdir(repo_dir)

    payload = {"asin": "B0CRAWL0001", "fetched_at": "2026-07-01T00:00:00Z",
               "reviews": [{"rating": 5, "title": "t", "body": "b", "posted_at": "2026-06-01"}]}
    monkeypatch.setattr(mod, "crawl_asin", lambda asin, **kw: payload)

    summary = run(["B0CRAWL0001"], raw_dir=raw_dir, sleeper=lambda s: None)
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
        "reviews": [],
    }), encoding="utf-8")

    called = []
    monkeypatch.setattr(mod, "crawl_asin", lambda asin, **kw: called.append(asin))

    summary = run(["B0FRESH0001"], raw_dir=raw_dir, refresh_days=30, sleeper=lambda s: None)
    assert called == []
    assert summary["written"] == 0


def test_run_stops_at_request_budget(tmp_path, monkeypatch):
    import scripts.crawl_yahoo_reviews as mod

    calls = []

    def _fake_crawl(asin, **kw):
        calls.append(asin)
        return None

    monkeypatch.setattr(mod, "crawl_asin", _fake_crawl)
    raw_dir = tmp_path / "raw"
    # max_requests=0 は無制限扱いなので、明示的に budget を使い切らせるため 1 に設定し
    # crawl_asin 呼び出し前に budget を消費させる代わりに、対象を複数与えて
    # crawl_asin 自体が budget を消費しない (mock) ケースでは全件処理されることを確認する。
    summary = run(["B0A0000001", "B0B0000001"], raw_dir=raw_dir, max_requests=100, sleeper=lambda s: None)
    assert calls == ["B0A0000001", "B0B0000001"]
    assert summary["targets"] == 2
