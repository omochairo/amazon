"""build_jules_prompt._sources_note の抜粋 (navi-brain#117 段階 3)。"""

import json

import build_jules_prompt as bjp
import score_per_asin_info as sc
from source_pages import Page

ASIN = "B0G4WWHWC2"


def _setup(tmp_path, monkeypatch, prior=()):
    monkeypatch.chdir(tmp_path)
    raw = tmp_path / "data" / "raw"
    (raw / "per_asin" / ASIN).mkdir(parents=True)
    (raw / "amazon.json").write_text(json.dumps({"items": [
        {"asin": ASIN, "title": "くもん出版 ロジカルルートパズル 知育玩具 プレゼント", "jan_code": "4524144000000"},
    ]}, ensure_ascii=False), encoding="utf-8")
    (raw / "per_asin" / ASIN / "third_party_sources.json").write_text(json.dumps({"sources": [
        {"url": "https://www.kumon.ne.jp/press/1", "host": "www.kumon.ne.jp", "title": "プレス"},
        {"url": "https://blog.example.jp/x", "host": "blog.example.jp", "title": "別の話"},
        {"url": "https://blocked.example.com/y", "host": "blocked.example.com"},
        {"url": "https://gone.example.org/z", "host": "gone.example.org",
         "link": {"status": "dead", "checked_at": "2026-10-17", "http": 404}},
    ]}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(sc, "prior_article_urls", lambda asin: list(prior))


def _fake(url, timeout):
    if "kumon" in url or "prior" in url:
        return Page("ok", 200, "新商品のお知らせ。くもん出版はロジカルルートパズルを発売します。4歳から。")
    if "blocked" in url:
        return Page("unknown", 403)
    if "weak" in url:
        return Page("ok", 200, "知育玩具の選び方。出版社ごとの特徴をまとめました。")
    return Page("ok", 200, "まったく関係のない料理の記事")


def test_excerpts_attached_and_dead_candidates_not_listed(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    note = bjp._sources_note(ASIN, fetch_excerpts=True, fetch=_fake)
    assert "gone.example.org" not in note  # リンク切れは並べない
    assert "抜粋 (システムが取得。本文に商品名の語「ロジカルルートパズル」" in note
    assert "ロジカルルートパズルを発売" in note
    assert "HTTP 403" in note
    assert "商品名・型番の語が見つからなかった" in note
    assert "閲覧ツールで開けない・本文が読めないときも" in note


def test_rewrite_prior_sources_get_excerpt_and_keep_instruction(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, prior=["https://prior.example.net/review"])
    note = bjp._sources_note(ASIN, fetch_excerpts=True, fetch=_fake)
    i = note.index("https://prior.example.net/review")
    assert "原則として残し" in note[:i]
    assert "抜粋 (システムが取得" in note[i:]


def test_weak_match_is_marked_reference_not_excerpt(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, prior=["https://weak.example.net/guide"])
    note = bjp._sources_note(ASIN, fetch_excerpts=True, fetch=_fake)
    i = note.index("https://weak.example.net/guide")
    line = note[i:].split("\n")[1]
    assert line.lstrip().startswith("参考 (本文に出たのは「出版」だけ")


def test_dead_candidate_is_not_revived_as_prior_source(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, prior=["https://gone.example.org/z", "https://prior.example.net/review"])
    note = bjp._sources_note(ASIN)
    assert "gone.example.org" not in note
    assert "https://prior.example.net/review" in note


def test_without_fetch_keeps_previous_wording_and_never_fetches(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, prior=["https://prior.example.net/review"])

    def boom(url, timeout):
        raise AssertionError("must not fetch")

    note = bjp._sources_note(ASIN, fetch=boom)
    assert "抜粋" not in note
    assert "前の記事が出典にしていた非販売のページ (同じく開いて確認してから採用する):" in note
    assert "https://www.kumon.ne.jp/press/1 (プレス)" in note


def test_fetch_failure_falls_back_to_url_list(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    def broken(url, timeout):
        raise RuntimeError("network down")

    note = bjp._sources_note(ASIN, fetch_excerpts=True, fetch=broken)
    assert "https://www.kumon.ne.jp/press/1" in note and "抜粋" not in note
