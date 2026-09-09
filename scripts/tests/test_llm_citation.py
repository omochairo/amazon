"""_llm_citation.py の単体テスト。

LLM 回答内でのドメイン引用 (Citation) 検知、GSC クエリ選定、URL 正規化・抽出の検証。
"""
from __future__ import annotations

import pytest

from scripts._llm_citation import (
    SITES,
    build_record,
    detect_citation,
    extract_urls,
    normalize_host,
    select_queries,
)


# ==============================================================================
# select_queries
# ==============================================================================

def test_select_queries_empty_input():
    assert select_queries([]) == []
    assert select_queries([], days=28, top_n=10) == []


def test_select_queries_blank_query_rows_ignored():
    rows = [
        {"date": "2026-09-01", "query": "", "clicks": 10, "impressions": 100},
        {"date": "2026-09-01", "query": "   ", "clicks": 10, "impressions": 100},
        {"date": "2026-09-01", "query": None, "clicks": 10, "impressions": 100},
        {"date": "2026-09-01", "query": "知育 玩具", "clicks": 5, "impressions": 50, "position": 3.0},
    ]
    res = select_queries(rows)
    assert len(res) == 1
    assert res[0]["query"] == "知育 玩具"
    assert res[0]["clicks"] == 5
    assert res[0]["impressions"] == 50


def test_select_queries_distinct_date_window_with_gaps():
    # カレンダーの連続日数ではなく、実際に現れる最新 N 個の個別日付を使う
    rows = [
        {"date": "2026-01-01", "query": "old_q", "clicks": 50, "impressions": 500},
        {"date": "2026-05-15", "query": "mid_q", "clicks": 20, "impressions": 200},
        {"date": "2026-09-01", "query": "new_q", "clicks": 10, "impressions": 100},
    ]
    # days=2 なので 2026-09-01 と 2026-05-15 が選ばれ、2026-01-01 は除外される
    res = select_queries(rows, days=2)
    queries = [r["query"] for r in res]
    assert "old_q" not in queries
    assert set(queries) == {"mid_q", "new_q"}


def test_select_queries_aggregation_sums_across_dates():
    rows = [
        {"date": "2026-09-01", "query": "lego", "clicks": 3, "impressions": 50, "position": 2.0},
        {"date": "2026-09-02", "query": "lego", "clicks": 7, "impressions": 150, "position": 4.0},
    ]
    res = select_queries(rows, days=7)
    assert len(res) == 1
    assert res[0]["query"] == "lego"
    assert res[0]["clicks"] == 10
    assert res[0]["impressions"] == 200


def test_select_queries_days_seen():
    rows = [
        {"date": "2026-09-01", "query": "puzzle", "clicks": 1, "impressions": 10},
        {"date": "2026-09-01", "query": "puzzle", "clicks": 2, "impressions": 20},  # 同一日付の複数行
        {"date": "2026-09-03", "query": "puzzle", "clicks": 1, "impressions": 10},
        {"date": "2026-09-05", "query": "puzzle", "clicks": 1, "impressions": 10},
    ]
    res = select_queries(rows, days=10)
    assert len(res) == 1
    # 2026-09-01, 2026-09-03, 2026-09-05 の 3 日間
    assert res[0]["days_seen"] == 3


def test_select_queries_impressions_weighted_position():
    rows = [
        {"date": "2026-09-01", "query": "toy", "clicks": 1, "impressions": 100, "position": 1.0},
        {"date": "2026-09-02", "query": "toy", "clicks": 2, "impressions": 300, "position": 5.0},
    ]
    # 加重平均 = (1.0 * 100 + 5.0 * 300) / (100 + 300) = (100 + 1500) / 400 = 4.0
    res = select_queries(rows, days=7)
    assert len(res) == 1
    assert res[0]["position"] == 4.0


def test_select_queries_zero_impressions_position_none():
    rows = [
        {"date": "2026-09-01", "query": "zero_imp", "clicks": 0, "impressions": 0, "position": 10.0},
    ]
    res = select_queries(rows, min_impressions=0)
    assert len(res) == 1
    assert res[0]["position"] is None


def test_select_queries_min_impressions_filter():
    rows = [
        {"date": "2026-09-01", "query": "rare", "clicks": 1, "impressions": 4},
        {"date": "2026-09-01", "query": "common", "clicks": 1, "impressions": 10},
    ]
    res = select_queries(rows, min_impressions=5)
    assert len(res) == 1
    assert res[0]["query"] == "common"


def test_select_queries_deterministic_tie_break_by_query_name():
    rows = [
        {"date": "2026-09-01", "query": "cherry", "clicks": 10, "impressions": 100},
        {"date": "2026-09-01", "query": "apple", "clicks": 10, "impressions": 100},
        {"date": "2026-09-01", "query": "banana", "clicks": 10, "impressions": 100},
    ]
    res = select_queries(rows)
    assert [r["query"] for r in res] == ["apple", "banana", "cherry"]


def test_select_queries_top_n_truncation():
    rows = [
        {"date": "2026-09-01", "query": f"q_{i:02d}", "clicks": i, "impressions": i * 10}
        for i in range(1, 30)
    ]
    res = select_queries(rows, top_n=5)
    assert len(res) == 5
    # clicks 降順なので q_29, q_28, ...
    assert res[0]["query"] == "q_29"
    assert res[4]["query"] == "q_25"


# ==============================================================================
# normalize_host
# ==============================================================================

def test_normalize_host_uppercase():
    assert normalize_host("HTTPS://NAVI.OMCHA.JP/products/123") == "navi.omcha.jp"


def test_normalize_host_www_prefix():
    assert normalize_host("http://www.omcha.jp/article") == "omcha.jp"
    assert normalize_host("https://WWW.NAVI.OMCHA.JP/") == "navi.omcha.jp"


def test_normalize_host_explicit_port():
    assert normalize_host("https://omcha.jp:8080/path?query=1") == "omcha.jp"
    assert normalize_host("http://navi.omcha.jp:443/products") == "navi.omcha.jp"


def test_normalize_host_non_http_scheme():
    assert normalize_host("ftp://omcha.jp/file.txt") is None
    assert normalize_host("mailto:info@omcha.jp") is None
    assert normalize_host("javascript:alert(1)") is None
    assert normalize_host("file:///C:/path/file.txt") is None


def test_normalize_host_garbage_input():
    assert normalize_host("not-a-url") is None
    assert normalize_host("") is None
    assert normalize_host("   ") is None
    assert normalize_host(None) is None  # type: ignore[arg-type]
    assert normalize_host("http://") is None
    assert normalize_host("https://[invalid-ipv6") is None
    assert normalize_host("http://www.") is None


# ==============================================================================
# extract_urls
# ==============================================================================

def test_extract_urls_markdown_link():
    text = "おすすめは[知育玩具ナビ](https://navi.omcha.jp/products/b0gc4mql8n/)をご覧ください。"
    assert extract_urls(text) == ["https://navi.omcha.jp/products/b0gc4mql8n/"]


def test_extract_urls_japanese_full_stop():
    text = "参考リンク: https://navi.omcha.jp/products/123。次の文です。"
    assert extract_urls(text) == ["https://navi.omcha.jp/products/123"]


def test_extract_urls_inside_parentheses():
    text = "詳細はこちら (https://navi.omcha.jp/products/123) を参照してください。"
    assert extract_urls(text) == ["https://navi.omcha.jp/products/123"]

    # 全角括弧
    text_zenkaku = "詳細はこちら（https://navi.omcha.jp/products/123）を参照。"
    assert extract_urls(text_zenkaku) == ["https://navi.omcha.jp/products/123"]


def test_extract_urls_duplicates_collapsed():
    text = (
        "1: https://navi.omcha.jp/p/1 "
        "2: https://navi.omcha.jp/p/2 "
        "3: https://navi.omcha.jp/p/1 "
        "4: [link](https://navi.omcha.jp/p/2)"
    )
    assert extract_urls(text) == [
        "https://navi.omcha.jp/p/1",
        "https://navi.omcha.jp/p/2",
    ]


def test_extract_urls_empty_text():
    assert extract_urls("") == []
    assert extract_urls("   ") == []
    assert extract_urls(None) == []


# ==============================================================================
# detect_citation
# ==============================================================================

def test_detect_citation_cited_via_citation_urls():
    res = detect_citation(
        answer_text="テキスト本文中にリンクはありません",
        citation_urls=["https://navi.omcha.jp/products/123"],
        host="navi.omcha.jp",
    )
    assert res["cited"] is True
    assert res["matched_urls"] == ["https://navi.omcha.jp/products/123"]
    assert res["mention_without_link"] is False


def test_detect_citation_cited_via_url_only_in_answer_text():
    res = detect_citation(
        answer_text="詳細は https://navi.omcha.jp/products/123 をご覧ください",
        citation_urls=None,
        host="navi.omcha.jp",
    )
    assert res["cited"] is True
    assert res["matched_urls"] == ["https://navi.omcha.jp/products/123"]
    assert res["mention_without_link"] is False


def test_detect_citation_navi_url_does_not_count_for_omcha():
    res = detect_citation(
        answer_text="[リンク](https://navi.omcha.jp/products/123)",
        citation_urls=["https://navi.omcha.jp/faq/"],
        host="omcha.jp",
    )
    assert res["cited"] is False
    assert res["matched_urls"] == []


def test_detect_citation_omcha_url_does_not_count_for_navi():
    res = detect_citation(
        answer_text="[リンク](https://omcha.jp/article/1)",
        citation_urls=["https://omcha.jp/category/toy"],
        host="navi.omcha.jp",
    )
    assert res["cited"] is False
    assert res["matched_urls"] == []


def test_detect_citation_home_omcha_jp_does_not_count_for_either():
    # home.omcha.jp は navi にも omcha にもカウントされない
    res_omcha = detect_citation(
        answer_text="https://home.omcha.jp/post",
        citation_urls=None,
        host="omcha.jp",
    )
    assert res_omcha["cited"] is False
    assert res_omcha["matched_urls"] == []

    res_navi = detect_citation(
        answer_text="https://home.omcha.jp/post",
        citation_urls=None,
        host="navi.omcha.jp",
    )
    assert res_navi["cited"] is False
    assert res_navi["matched_urls"] == []


def test_detect_citation_mention_without_link_true_when_bare_host_in_prose():
    # リンク無しで地文にホスト名が含まれる（大文字混ざり含む）
    res = detect_citation(
        answer_text="詳しくは Navi.Omcha.JP の知育玩具レビューを参考にしてください。",
        citation_urls=[],
        host="navi.omcha.jp",
    )
    assert res["cited"] is False
    assert res["matched_urls"] == []
    assert res["mention_without_link"] is True


def test_detect_citation_mention_without_link_false_when_cited():
    # 引用が成立している場合、地文にホスト名が書かれていても mention_without_link は False
    res = detect_citation(
        answer_text="navi.omcha.jp より引用: https://navi.omcha.jp/products/1",
        citation_urls=None,
        host="navi.omcha.jp",
    )
    assert res["cited"] is True
    assert res["matched_urls"] == ["https://navi.omcha.jp/products/1"]
    assert res["mention_without_link"] is False


def test_detect_citation_citation_urls_none_and_answer_text_empty():
    res = detect_citation(
        answer_text="",
        citation_urls=None,
        host="navi.omcha.jp",
    )
    assert res == {
        "cited": False,
        "matched_urls": [],
        "mention_without_link": False,
        "candidate_hosts": [],
    }


# ==============================================================================
# build_record
# ==============================================================================

def test_build_record_cited_case():
    rec = build_record(
        date="2026-09-09",
        site="navi",
        engine="perplexity",
        model="sonar-pro",
        query="知育玩具 2歳 おすすめ",
        answer_text="2歳児にはブロックが最適です。[知育ナビ](https://navi.omcha.jp/products/b001/)",
        citation_urls=["https://navi.omcha.jp/products/b001/"],
        latency_ms=1520,
    )
    assert rec == {
        "date": "2026-09-09",
        "site": "navi",
        "engine": "perplexity",
        "model": "sonar-pro",
        "query": "知育玩具 2歳 おすすめ",
        "cited": True,
        "matched_urls": ["https://navi.omcha.jp/products/b001/"],
        "mention_without_link": False,
        "urls_found": 1,
        "other_hosts": [],
        "answer_chars": 59,
        "citation_count": 1,
        "latency_ms": 1520,
    }


def test_build_record_uncited_case():
    rec = build_record(
        date="2026-09-09",
        site="omcha",
        engine="google",
        model="gemini-2.5-flash",
        query="木のおもちゃ 手入れ",
        answer_text="木のおもちゃのお手入れ方法は omcha.jp で解説されています。",
        citation_urls=[],
        latency_ms=None,
    )
    assert rec == {
        "date": "2026-09-09",
        "site": "omcha",
        "engine": "google",
        "model": "gemini-2.5-flash",
        "query": "木のおもちゃ 手入れ",
        "cited": False,
        "matched_urls": [],
        "mention_without_link": True,
        "urls_found": 0,
        "other_hosts": [],
        "answer_chars": 34,
        "citation_count": 0,
        "latency_ms": None,
    }


def test_build_record_unknown_site_raises_key_error():
    with pytest.raises(KeyError, match="Unknown site 'invalid'"):
        build_record(
            date="2026-09-09",
            site="invalid",
            engine="google",
            model="gemini",
            query="q",
            answer_text="",
            citation_urls=[],
        )


# --- mention_without_link のホスト境界 (母艦レビューで検出した実測欠陥の回帰テスト) ---
# 単純な部分文字列一致だと host "omcha.jp" が "navi.omcha.jp" に一致し、
# URL 側で分離した 2 系列が言及判定で混ざる。


@pytest.mark.parametrize(
    "text,host,expected",
    [
        ("navi.omcha.jp が詳しいです", "omcha.jp", False),
        ("home.omcha.jp を参照", "omcha.jp", False),
        ("omcha.jp を参照", "omcha.jp", True),
        ("詳しくは omcha.jp/awappy-bath-review/ へ", "omcha.jp", True),
        ("omcha.jpn という別サイト", "omcha.jp", False),
        ("x-omcha.jp は無関係", "omcha.jp", False),
        ("navi.omcha.jp が詳しいです", "navi.omcha.jp", True),
        ("omcha.jp だけ", "navi.omcha.jp", False),
        ("OMCHA.JP を参照", "omcha.jp", True),
    ],
)
def test_mention_without_link_host_boundary(text, host, expected):
    got = detect_citation(
        answer_text=text, citation_urls=None, host=host
    )
    assert got["mention_without_link"] is expected
    assert got["cited"] is False


# --- candidate_hosts / other_hosts (cited=False の解釈可能性) ---


def test_detect_citation_reports_candidate_hosts():
    got = detect_citation(
        answer_text="https://example.com/a と https://navi.omcha.jp/b を参照",
        citation_urls=["https://Example.com/a"],
        host="navi.omcha.jp",
    )
    assert got["cited"] is True
    assert got["candidate_hosts"] == ["example.com", "navi.omcha.jp"]


def test_build_record_distinguishes_no_urls_from_other_sites():
    silent = build_record(
        date="2026-09-09", site="navi", engine="fixture", model="m",
        query="q", answer_text="URL のない回答", citation_urls=None,
    )
    assert silent["urls_found"] == 0
    assert silent["other_hosts"] == []

    others = build_record(
        date="2026-09-09", site="navi", engine="fixture", model="m",
        query="q", answer_text="https://example.com/a",
        citation_urls=["https://omcha.jp/x"],
    )
    assert others["cited"] is False
    assert others["urls_found"] == 2
    assert others["other_hosts"] == ["omcha.jp", "example.com"]
