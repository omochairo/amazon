"""scripts/ai_referrals.py の見分けと表。GA4 は呼ばない。"""
from __future__ import annotations

import json
import pathlib
import re
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import ai_referrals as ar  # noqa: E402


@pytest.mark.parametrize("source,expected", [
    ("chatgpt.com", "ChatGPT"),
    ("www.chatgpt.com", "ChatGPT"),
    ("ChatGPT.com", "ChatGPT"),
    ("chat.openai.com", "ChatGPT"),
    ("chatgpt", "ChatGPT"),               # utm_source=chatgpt
    ("perplexity.ai", "Perplexity"),
    ("www.perplexity.ai", "Perplexity"),
    ("gemini.google.com", "Gemini"),
    ("copilot.microsoft.com", "Copilot"),
    ("edgeservices.bing.com", "Copilot"),
    ("claude.ai", "Claude"),
    ("grok.com", "Grok"),
    ("chat.deepseek.com", "DeepSeek"),
    ("meta.ai", "Meta AI"),
    ("you.com", "その他の AI"),
    ("google", None),
    ("bing", None),
    ("bing.com", None),                   # 普通の Bing 検索は AI に数えない
    ("(direct)", None),
    ("max.ai", None),                     # x.ai のサブドメインではない
    ("notchatgpt.com", None),
    ("", None),
])
def test_classify(source, expected):
    assert ar.classify(source) == expected


def test_ga4_regex_matches_every_listed_domain_and_not_others():
    rx = re.compile(ar.ga4_source_regex(), re.I)
    for _name, domains in ar.AI_SERVICES:
        for d in domains:
            assert rx.fullmatch(d), d
            assert rx.fullmatch("www." + d), d
    for s in ("google", "bing.com", "(direct)", "max.ai", "notchatgpt.com"):
        assert not rx.fullmatch(s), s
    # classify と GA4 の絞り込みが食い違わない (着地ページの表が欠けないように)
    for s in ("chatgpt.com", "foo.perplexity.ai", "kimi.com"):
        assert bool(rx.fullmatch(s)) == (ar.classify(s) is not None)


def test_looks_like_ai_only_for_unclassified():
    assert ar.looks_like_ai("newchat.ai")
    assert not ar.looks_like_ai("chatgpt.com")   # 分類済みは出さない
    assert not ar.looks_like_ai("google")


def test_weeks_align_to_end_and_drop_short_edge():
    from datetime import date
    weeks = ar._weeks(date(2026, 11, 1), date(2026, 11, 24))
    assert weeks[-1] == (date(2026, 11, 18), date(2026, 11, 24))
    assert weeks[0] == (date(2026, 11, 4), date(2026, 11, 10))
    assert len(weeks) == 3


def _data():
    return {
        "meta": {"range": {"start": "2026-11-11", "end": "2026-11-24"},
                 "landing_range": {"start": "2026-11-11", "end": "2026-11-24"},
                 "warnings": ["GA4 のしきい値が適用されている"]},
        "sources": [
            {"date": "2026-11-12", "sessionSource": "chatgpt.com", "sessions": 3, "engagedSessions": 2},
            {"date": "2026-11-12", "sessionSource": "google", "sessions": 97, "engagedSessions": 50},
            {"date": "2026-11-20", "sessionSource": "perplexity.ai", "sessions": 1, "engagedSessions": 1},
            {"date": "2026-11-20", "sessionSource": "chatgpt", "sessions": 2, "engagedSessions": 0},
            {"date": "2026-11-20", "sessionSource": "google", "sessions": 47, "engagedSessions": 20},
            {"date": "2026-11-20", "sessionSource": "newchat.ai", "sessions": 4, "engagedSessions": 1},
        ],
        "clicks": [
            {"date": "2026-11-20", "sessionSource": "chatgpt", "eventCount": 1},
            {"date": "2026-11-20", "sessionSource": "google", "eventCount": 9},
        ],
        "landing": [
            {"landingPage": "/products/b0aaa/", "sessionSource": "chatgpt.com", "sessions": 3},
            {"landingPage": "/products/b0aaa/", "sessionSource": "chatgpt", "sessions": 2},
            {"landingPage": "/toys-age-3/", "sessionSource": "perplexity.ai", "sessions": 1},
        ],
    }


def test_report_tables():
    md = ar.build_report(_data())
    weekly = [ln for ln in md.splitlines() if ln.startswith("| 11/")]
    assert len(weekly) == 2
    # 11/11〜11/17: ChatGPT 3 / 全体 100 / 3.0% / クリック 0
    assert weekly[0].startswith("| 11/11〜11/17 | 3 | 0 |")
    assert weekly[0].endswith("| 3 | 100 | 3.0% | 0 |")
    # 11/18〜11/24: ChatGPT 2・Perplexity 1 / 全体 54 (未分類の newchat.ai は AI に数えない)
    assert weekly[1].startswith("| 11/18〜11/24 | 2 | 1 |")
    assert weekly[1].endswith("| 3 | 54 | 5.6% | 1 |")
    assert "| ChatGPT | 5 | 2 | 40.0% | 1 | 20.0% |" in md
    assert "| `/products/b0aaa/` | ChatGPT | 5 |" in md
    assert "| `newchat.ai` | 4 |" in md
    assert "> ⚠️ GA4 のしきい値が適用されている" in md


def test_report_without_ai_traffic():
    d = _data()
    d["sources"] = [r for r in d["sources"] if r["sessionSource"] == "google"]
    d["clicks"], d["landing"] = [], []
    md = ar.build_report(d)
    assert "（AI からのセッションなし）" in md
    assert "（AI から来たセッションはありません）" in md
    assert "未分類" not in md


def test_format_cli(tmp_path):
    rows = tmp_path / "rows.json"
    rows.write_text(json.dumps(_data(), ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "r.md"
    assert ar.main(["format", "--rows", str(rows), "--out", str(out)]) == 0
    assert out.read_text(encoding="utf-8").startswith("## AI 経由の訪問")


def test_fetch_requires_credentials(monkeypatch, tmp_path):
    monkeypatch.delenv("GA4_PROPERTY_ID", raising=False)
    monkeypatch.delenv("GA4_SA_JSON", raising=False)
    assert ar.main(["fetch", "--out", str(tmp_path / "x.json")]) == 2


def test_cell_escapes_table_breakers():
    assert ar._cell("a|b`c\nd\\e") == r"a\|b'c d\\e"


def test_report_escapes_page_and_source():
    d = _data()
    d["landing"].append({"landingPage": "/x|y/", "sessionSource": "claude.ai", "sessions": 9})
    d["sources"].append({"date": "2026-11-20", "sessionSource": "bad|chat.ai", "sessions": 1,
                         "engagedSessions": 0})
    md = ar.build_report(d)
    assert r"| `/x\|y/` | Claude | 9 |" in md
    assert r"| `bad\|chat.ai` | 1 |" in md
    assert "未分類の参照元（2026-11-11〜2026-11-24" in md


def test_fetch_rejects_bad_landing_days(tmp_path):
    rc = ar.main(["fetch", "--property-id", "1", "--sa-json", "{}", "--landing-days", "0",
                  "--out", str(tmp_path / "x.json")])
    assert rc == 2


class _V:
    def __init__(self, value):
        self.value = value


class _Row:
    def __init__(self, dims, mets):
        self.dimension_values = [_V(v) for v in dims]
        self.metric_values = [_V(str(v)) for v in mets]


class _Res:
    def __init__(self, rows, total, thresholded=False):
        self.rows = rows
        self.row_count = total
        self.metadata = type("M", (), {"subject_to_thresholding": thresholded,
                                       "sampling_metadatas": [], "data_loss_from_other_row": False})()


class _FakeClient:
    """dims の組で返す行を変え、1 回目と 2 回目でページを分ける。"""

    def __init__(self):
        self.requests = []

    def run_report(self, req):
        self.requests.append(req)
        dims = [d.name for d in req.dimensions]
        if dims == ["date", "sessionSource"] and [m.name for m in req.metrics] == ["sessions", "engagedSessions"]:
            if req.offset == 0:
                return _Res([_Row(["20261120", "chatgpt.com"], [3, 2])], total=2, thresholded=True)
            return _Res([_Row(["20261120", "google"], [40, 20])], total=2)
        if dims == ["date", "sessionSource"]:
            return _Res([_Row(["20261120", "chatgpt.com"], [1])], total=1)
        return _Res([_Row(["/products/b0aaa/", "chatgpt.com"], [3])], total=1)


def test_fetch_pages_and_shapes_rows():
    from datetime import date
    pytest.importorskip("google.analytics.data_v1beta")
    client = _FakeClient()
    out = ar.fetch(client, "123", date(2026, 10, 1), date(2026, 11, 24), 28)
    assert out["sources"] == [
        {"date": "2026-11-20", "sessionSource": "chatgpt.com", "sessions": 3, "engagedSessions": 2},
        {"date": "2026-11-20", "sessionSource": "google", "sessions": 40, "engagedSessions": 20},
    ]
    assert out["clicks"] == [{"date": "2026-11-20", "sessionSource": "chatgpt.com", "eventCount": 1}]
    assert out["landing"] == [{"landingPage": "/products/b0aaa/", "sessionSource": "chatgpt.com", "sessions": 3}]
    assert out["meta"]["landing_range"] == {"start": "2026-10-28", "end": "2026-11-24"}
    assert any("しきい値" in w for w in out["meta"]["warnings"])
    # 参照元は 2 ページに分けて取り切る
    src_reqs = [r for r in client.requests if [m.name for m in r.metrics] == ["sessions", "engagedSessions"]]
    assert [r.offset for r in src_reqs] == [0, 1]
    # 全部 navi.omcha.jp で絞っている
    for r in client.requests:
        assert "navi.omcha.jp" in str(r.dimension_filter)
