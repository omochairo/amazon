"""scripts/probe_agy_sources.py unit tests.

probe 本体は agy と外部サイトを叩くので CI では回せない。**URL の抽出と
grounding redirect の判別は決定的**なので、そこだけ固定する。ここが壊れると
「出典 URL が取れた」という判定が黙って狂う。
"""
from __future__ import annotations

import requests

from scripts import probe_agy_sources as probe

_REDIRECT = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQ-abc_123"


def test_extract_urls_strips_markdown_and_japanese_punctuation():
    text = (
        "* 連動が好評です。出典: https://review.rakuten.co.jp/item/1/\n"
        "* [ボーネルンド](https://ec.bornelund.co.jp/shop/g/QR2341/)、他にも声があります。\n"
        "* 注意点もあります(https://kakaku.com/item/K0001/)。\n"
    )
    assert probe.extract_urls(text) == [
        "https://review.rakuten.co.jp/item/1/",
        "https://ec.bornelund.co.jp/shop/g/QR2341/",
        "https://kakaku.com/item/K0001/",
    ]


def test_extract_urls_dedupes_and_handles_empty():
    assert probe.extract_urls("") == []
    assert probe.extract_urls(None) == []
    dup = "a https://example.com/x b https://example.com/x"
    assert probe.extract_urls(dup) == ["https://example.com/x"]


def test_is_grounding_redirect_matches_vertex_only():
    """Gemini の検索は実 URL でなく不透明なリダイレクト URL を返す。

    これを素通しで source_url に保存すると後から辿れないので、収集時に解決する
    必要がある。判別を間違えると解決を飛ばしてしまう。
    """
    assert probe.is_grounding_redirect(_REDIRECT) is True
    assert probe.is_grounding_redirect("https://review.rakuten.co.jp/item/1/") is False
    # ホスト名の部分一致で誤判定しない
    assert probe.is_grounding_redirect(
        "https://evil.example.com/vertexaisearch.cloud.google.com/grounding-api-redirect/x"
    ) is False
    # 同ホストでも別パスなら redirect ではない
    assert probe.is_grounding_redirect(
        "https://vertexaisearch.cloud.google.com/other/x") is False
    assert probe.is_grounding_redirect("not a url") is False


class _FakeResp:
    def __init__(self, status, url):
        self.status_code = status
        self.url = url


class _FakeSession:
    def __init__(self, result):
        self._result = result
        self.calls = []

    def get(self, url, timeout=None, allow_redirects=None, headers=None):
        self.calls.append((url, allow_redirects, headers))
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def test_resolve_source_url_follows_redirect_to_real_page():
    final = "https://product.rakuten.co.jp/product/-/147c/"
    session = _FakeSession(_FakeResp(200, final))
    rec = probe.resolve_source_url(_REDIRECT, session)
    assert rec["is_redirect"] is True
    assert rec["status"] == 200
    assert rec["final_url"] == final
    assert rec["domain"] == "product.rakuten.co.jp"
    # リダイレクトを辿らないと実 URL に到達できない
    assert session.calls[0][1] is True


def test_resolve_source_url_records_failure_instead_of_raising():
    """200 で返らない URL はハルシネーションか失効。source_url に入れてはいけない。"""
    session = _FakeSession(requests.ConnectionError("boom"))
    rec = probe.resolve_source_url("https://nope.example.com/x", session)
    assert rec["status"] == 0
    assert rec["error"].startswith("ConnectionError")
    assert rec["final_url"] == ""


def test_legacy_control_prompt_is_frozen():
    """対照群は #6588 以前の文言のまま固定する。

    production に追従させると過去の実測値と比較できなくなり、
    「新プロンプトが対照より良い」という主張の根拠が消える。
    """
    legacy = probe.PROMPTS["summary_legacy"]("ケルチェッティ", "ボーネルンド")
    assert "注意点" not in legacy
    assert "出典" not in legacy
    assert legacy != probe.mine_experience.build_antigravity_prompt(
        "ケルチェッティ", "ボーネルンド")


def test_production_variant_tracks_the_real_prompt():
    """逆に production 版は本番と同じ文字列で測る (乖離したら意味が無い)。"""
    assert probe.PROMPTS["summary_url_balanced"] is         probe.mine_experience.build_antigravity_prompt


def test_prompts_cover_candidates():
    for name in ("summary_url", "excerpt_url"):
        p = probe.PROMPTS[name]("ケルチェッティ", "ボーネルンド")
        assert "ケルチェッティ" in p and "URL" in p
        # URL の作文を明示的に禁じておく (ハルシネーション対策)
        assert "作文" in p


def test_excerpt_prompt_forbids_opening_pages():
    """read_url は headless で auto-deny され、空応答になる (2026-09-06 実測)。

    検索結果の範囲で完結させる指示が消えると probe ごと成立しなくなる。
    """
    p = probe.PROMPTS["excerpt_url"]("ケルチェッティ", "ボーネルンド")
    assert "開かない" in p


def test_summarize_reports_url_yield_and_reachability():
    records = [
        {"variant": "a", "ok": True, "score": 0.9, "latency_s": 10.0, "urls": [
            {"is_redirect": True, "status": 200, "domain": "rakuten.co.jp"},
            {"is_redirect": True, "status": 404, "domain": ""},
        ]},
        {"variant": "a", "ok": True, "score": 0.9, "latency_s": 12.0, "urls": []},
        {"variant": "b", "ok": False, "score": 0.0, "latency_s": 0.0, "urls": []},
    ]
    rows = {r["variant"]: r for r in probe.summarize(records)}
    a = rows["a"]
    assert a["urls_per_call"] == 1.0     # 2 URL / 2 成功コール
    assert a["calls_with_url"] == 0.5    # URL が付いたのは 2 コール中 1
    assert a["http_ok"] == 0.5           # 2 本中 1 本しか到達しない
    assert a["domains"] == 1
    assert rows["b"]["success_rate"] == 0.0


def test_balanced_prompt_asks_for_caveats_and_urls():
    """出典 URL を足すと注意点が押し出される (balance 0.56 -> 0.11) ことへの対策。

    どちらの指示が欠けてもこの variant の意味が無くなる。
    """
    p = probe.PROMPTS["summary_url_balanced"]("ケルチェッティ", "ボーネルンド")
    assert "注意点" in p
    assert "出典" in p
    assert "作文" in p
