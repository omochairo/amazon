"""build_jules_prompt の Web を使えない書き手向けの書き方 (navi-brain#170 A3) のテスト。

ネットワークには出ない (抜粋は偽物を渡す)。従来の出力 (no_web=False) が変わらないことは
要所の文で確かめる (全文の一致は K8 で実データ 30 商品に当てて確認済み)。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import build_jules_prompt as bjp  # noqa: E402
import score_per_asin_info as sc  # noqa: E402

ASIN = "B0TEST0001"
OK_STRONG = ("ok", "ブリオ レールウェイ 33506 の紹介", ["33506", "ブリオ"], 200)
OK_WEAK = ("ok", "おもちゃ の話", ["おもちゃ"], 200)
OK_NOSNIP = ("ok", "", [], 200)
DEAD = ("dead", "", [], 404)
UNKNOWN = ("unknown", "", [], 403)


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    d = tmp_path / "data" / "raw" / "per_asin" / ASIN
    d.mkdir(parents=True)
    sources = [
        {"url": "https://review.example.jp/a", "host": "review.example.jp", "title": "レビュー A"},
        {"url": "https://blog.example.com/b", "host": "blog.example.com", "title": "ブログ B"},
        {"url": "https://weak.example.net/c", "host": "weak.example.net", "title": "C"},
        {"url": "https://gone.example.org/d", "host": "gone.example.org", "title": "D"},
    ]
    (d / "third_party_sources.json").write_text(json.dumps({"sources": sources}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sc, "prior_article_urls", lambda asin, *a, **k: ())
    monkeypatch.setattr(sc, "first_party_posts", lambda asin, *a, **k: ["https://omcha.jp/1234"])
    return tmp_path


def excerpts():
    return {
        "https://omcha.jp/1234": OK_STRONG,
        "https://review.example.jp/a": OK_STRONG,
        "https://blog.example.com/b": OK_NOSNIP,
        "https://weak.example.net/c": OK_WEAK,
        "https://gone.example.org/d": UNKNOWN,
    }


def test_source_candidates_adds_first_party_only_for_no_web(repo):
    plain = bjp.source_candidates(ASIN)
    assert [u for u, _ in plain["third"]][0] == "https://review.example.jp/a"
    assert plain["mall"][0] == ("Amazon 商品ページ", f"https://www.amazon.co.jp/dp/{ASIN}/")
    no_web = bjp.source_candidates(ASIN, no_web=True)
    assert [u for u, _ in no_web["third"]][0] == "https://omcha.jp/1234"
    assert bjp.excerpt_targets(no_web)[:2] == ["https://omcha.jp/1234", "https://review.example.jp/a"]


def test_citable_urls_only_strong_ok_excerpts(repo):
    cand = bjp.source_candidates(ASIN, no_web=True)
    ex = excerpts()
    ex["https://gone.example.org/d"] = DEAD
    assert bjp.citable_urls(cand, ex) == ["https://omcha.jp/1234", "https://review.example.jp/a"]
    assert bjp.citable_urls(cand, {}) == []


def test_sources_note_no_web_wording(repo):
    note = bjp._sources_note(ASIN, no_web=True, excerpts=excerpts())
    assert "「抜粋」が付いたもの" in note and "あなたは Web 検索・URL 閲覧を使えない" in note
    assert "足りなければ検索で探す" not in note
    assert "閲覧ツールで開けない・本文が読めないときも" not in note
    assert "抜粋の付いていない URL・架空の URL" in note
    assert "抜粋 (システムが取得" in note  # 強い一致の候補に抜粋が付く
    assert f"https://www.amazon.co.jp/dp/{ASIN}/" in note


def test_sources_note_does_not_fetch_when_excerpts_given(repo, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("should not fetch")

    monkeypatch.setattr(bjp, "_fetch_excerpts", boom)
    bjp._sources_note(ASIN, no_web=True, excerpts=excerpts())


def test_sources_note_legacy_wording_unchanged(repo):
    note = bjp._sources_note(ASIN)
    assert ("1. 非販売 (第三者) の出典を、別々のサイトから 2 件以上確保する。まず下の候補を"
            "閲覧ツールで開き、対象商品について書かれているものを採用する。足りなければ検索で探す。") in note
    assert "   - Amazon 商品ページ: https://www.amazon.co.jp/dp/B0TEST0001/" in note
    assert "omcha.jp/1234" not in note  # first-party を候補に混ぜるのは no_web だけ
    assert "2 未満なら 1 に戻って足す" in note


def test_build_prompt_no_web_swaps_rules(repo, monkeypatch):
    (repo / "jules").mkdir()
    (repo / "jules" / "PROMPT_TEMPLATE.md").write_text("TEMPLATE", encoding="utf-8")
    (repo / "AGENTS.md").write_text("AGENTS", encoding="utf-8")
    monkeypatch.setattr(bjp, "_amazon_item", lambda asin: {"asin": asin, "title": "ブリオ 33506"})
    for name in ("_info_note", "_gsc_note", "_audit_note", "_experience_note", "_score_note"):
        monkeypatch.setattr(bjp, name, lambda asin: "")
    p = bjp.build_prompt(ASIN, today="2026-11-15", no_web=True, excerpts=excerpts())
    assert "【sources のルール (Web を使えない書き手向け・必読)】" in p
    assert "必ず検索・URL閲覧で裏取りを試みてください" not in p
    assert "【品質ゲートで機械検査される項目】" in p
    assert "抜粋が付いていれば" in p  # first-party の注記も Web 無しの文面
    legacy = bjp.build_prompt(ASIN, today="2026-11-15")
    assert "必ず検索・URL閲覧で裏取りを試みてください" in legacy
    assert "Web を使えない書き手向け" not in legacy
    assert "内容を閲覧ツールで確認したうえで" in legacy


def test_generic_category_page_is_not_citable_for_no_web(repo):
    # 2026-11-14 の試運転: ブランドの無い商品で、説明の語だけ一致したカテゴリのランキング記事
    generic = ("ok", "家庭用プラネタリウムのおすすめ", ["星空プロジェクター", "プラネタリウム", "2way"], 200)
    ex = excerpts()
    ex["https://review.example.jp/a"] = generic
    cand = bjp.source_candidates(ASIN, no_web=True)
    assert bjp.citable_urls(cand, ex) == ["https://omcha.jp/1234"]
    assert "出典にしない" in bjp._excerpt_line(generic, strict=True)
    assert bjp._excerpt_line(generic).lstrip().startswith("抜粋")  # 従来の経路は変えない
    assert bjp.identifies_product(["4901234567894"])  # JAN
    assert bjp.identifies_product(["PX-120"]) and bjp.identifies_product(["ブリオ"])
    assert not bjp.identifies_product(["2way", "星空プロジェクター"])


def test_amazon_other_domains_are_not_citable(repo, monkeypatch):
    cand = {"mall": [], "third": [("https://www.amazon.jp/dp/B0TEST0001?psc=1", ""),
                                  ("https://review.example.jp/a", "")], "prior": [], "dead": set()}
    ex = {"https://www.amazon.jp/dp/B0TEST0001?psc=1": OK_STRONG, "https://review.example.jp/a": OK_STRONG}
    assert bjp.citable_urls(cand, ex) == ["https://review.example.jp/a"]


def test_shop_and_price_list_hosts_are_not_third_party():
    # 2026-11-15 の試運転で非販売の出典に入った通販モール・価格比較 (読み物の kinarino.jp は第三者のまま)
    assert sc.host_kind("mall.kinarino.jp") == "retail"
    assert sc.host_kind("www.saiyasune.com") == "retail"
    assert sc.host_kind("www.amazon.jp") == "retail"
    assert sc.host_kind("kinarino.jp") == "third_party"
