"""scripts/self_domain.py と、各レーンへの適用のテスト (#6593)。

自社記事を「第三者の情報」として取り込む循環を止めるガード。
**判定が壊れても収集は成功し続ける**ので、ここが唯一の検知経路になる。
"""
from __future__ import annotations

import self_domain
from scripts import self_domain as pkg_self_domain


def test_matches_own_sites_and_subdomains():
    for url in (
        "https://navi.omcha.jp/products/b00000dmd2/",
        "https://omcha.jp/learning-resources-popular-toys/",
        "https://home.omcha.jp/a/",
        "https://www.omcha.jp/a/",
        "http://OMCHA.JP/a/",          # 大文字
        "https://omcha.jp:443/a/",     # ポート付き
    ):
        assert self_domain.is_self_domain(url) is True, url


def test_does_not_match_unrelated_domains():
    """URL 全体の部分一致だとここが落ちる (`"omcha.jp" in "notomcha.jp"` は真)。

    #6593 以前の fetch_third_party_sources はこの誤除外をしていた。
    """
    for url in (
        "https://notomcha.jp/a/",
        "https://myomcha.jp/a/",
        "https://example.com/?ref=omcha.jp",   # host は他人
        "https://review.rakuten.co.jp/item/1/",
        "",
        "not a url",
    ):
        assert self_domain.is_self_domain(url) is False, url


def test_self_host_normalises():
    assert self_domain.self_host("https://User@NAVI.Omcha.JP:8443/x") == "navi.omcha.jp"
    assert self_domain.self_host("") == ""


def test_importable_both_ways():
    """素の兄弟 import と package 形式の両方で使える (#5003 の import 木の罠)。

    sys.path の張り方の違いで別モジュールオブジェクトになりうるので、
    同一性ではなく **同じ挙動・同じ定数**であることを見る。
    """
    assert pkg_self_domain.SELF_DOMAIN_SUFFIXES == self_domain.SELF_DOMAIN_SUFFIXES
    assert pkg_self_domain.is_self_domain("https://navi.omcha.jp/x") is True
    assert pkg_self_domain.is_self_domain("https://notomcha.jp/x") is False


# --------------------------------------------------------------------------
# 各レーンへの適用
# --------------------------------------------------------------------------

def test_third_party_collector_excludes_own_sites():
    from fetch_third_party_sources import _is_excluded

    assert _is_excluded("https://navi.omcha.jp/products/x/") is True
    assert _is_excluded("https://omcha.jp/a/") is True
    assert _is_excluded("https://home.omcha.jp/a/") is True
    # 検索エンジン・小売の除外は生きたまま
    assert _is_excluded("https://www.google.com/search?q=x") is True
    # 無関係の実在ドメインは通す (#6593 以前は誤除外していた)
    assert _is_excluded("https://notomcha.jp/a/") is False
    assert _is_excluded("https://blog.example.jp/a/") is False


def test_news_collector_drops_own_domain(monkeypatch):
    """Google News RSS は**検索**なので、自社記事が返る余地がある。"""
    import fetch_yahoo_news

    xml = """<?xml version="1.0"?><rss><channel>
      <item><title>他社の記事</title><link>https://news.example.jp/1</link><pubDate>x</pubDate></item>
      <item><title>自社の記事</title><link>https://omcha.jp/a/</link><pubDate>x</pubDate></item>
    </channel></rss>"""

    class _Resp:
        status_code = 200
        content = xml.encode("utf-8")

    monkeypatch.setattr(fetch_yahoo_news.requests, "get", lambda *a, **k: _Resp())
    got = fetch_yahoo_news.gnews_search("知育玩具", max_results=10)
    assert [g["url"] for g in got] == ["https://news.example.jp/1"]


def test_experience_lane_reuses_the_shared_guard():
    """mine_experience が自前実装に戻っていないこと (#6592 -> #6593 の統合)。"""
    from scripts import mine_experience

    assert mine_experience.SELF_DOMAIN_SUFFIXES == self_domain.SELF_DOMAIN_SUFFIXES
    assert mine_experience.is_self_domain("https://navi.omcha.jp/products/x/") is True
    assert mine_experience.is_self_domain("https://notomcha.jp/x") is False


# --------------------------------------------------------------------------
# audit_self_domain (catch 側の計器)
# --------------------------------------------------------------------------

def _write(path, obj):
    import json as _json
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def test_audit_finds_self_urls_in_any_key(tmp_path):
    """キー名を決め打ちにしない — 新しいキーが増えたときに黙って見落とす方が怖い。"""
    from scripts import audit_self_domain as audit

    _write(tmp_path / "B0AAAAAAAA" / "third_party_sources.json",
           {"sources": [{"url": "https://blog.example.jp/1"},
                        {"url": "https://navi.omcha.jp/products/b0aaaaaaaa/"}]})
    _write(tmp_path / "B0BBBBBBBB" / "experience.json",
           {"snippets": [{"source_url": "", "source_urls": ["https://omcha.jp/a/"]}]})

    result = audit.audit(tmp_path)
    assert result["total_hits"] == 2
    hits = result["per_file"]["third_party_sources.json"]["hits"]
    assert hits[0]["asin"] == "B0AAAAAAAA"
    assert result["per_file"]["experience.json"]["hits"][0]["url"] == "https://omcha.jp/a/"


def test_audit_reports_clean_data_as_zero(tmp_path):
    from scripts import audit_self_domain as audit

    _write(tmp_path / "B0AAAAAAAA" / "news.json",
           {"items": [{"url": "https://news.example.jp/1"}]})
    result = audit.audit(tmp_path)
    assert result["total_hits"] == 0
    assert result["per_file"]["news.json"]["urls"] == 1


def test_audit_distinguishes_clean_from_unmeasured(tmp_path):
    """走査 0 件は「きれい」ではなく「測れていない」。混同すると嘘の安心になる。"""
    from scripts import audit_self_domain as audit

    result = audit.audit(tmp_path)
    assert result["total_hits"] == 0
    report = audit.format_report(result)
    assert "走査対象が 0 件だった" in report


def test_audit_does_not_scan_intentional_self_reference():
    """omcha_related.json は内部リンク用で自社 URL しか入らない。対象に入れない。"""
    from scripts import audit_self_domain as audit

    assert "omcha_related.json" not in audit.TARGET_FILES
