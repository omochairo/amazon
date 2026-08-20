"""#5490 信頼レーン: claims の認証主張が certifications に申告されているかの検査。

背景 (実測 2026-08-19, 記事 2,064 件):
- `check_certifications` は certifications が空だと "OK (empty)" で抜ける。
  つまり **申告しなければ裏取りを免れる**抜け道が残っていた。
- claims 側で認証を主張しながら未申告の記事が 26 件 (1.3%)。うち band=zero の
  無名ブランド品が 4 件。子ども向け玩具の安全性主張が裏取りされないまま配信される
  のは E-E-A-T 上いちばん出してはいけない類のもの。

まだ soft (passed=True, score<1.0)。#4826 項目2 と同じく、発火率が 0 に近づいて
から hard へ昇格する。
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quality_gate import (  # noqa: E402
    CERT_CLAIM_UNDECLARED_SOFT_SCORE,
    check_cert_claims_declared,
)


def _article(claims, certifications=None, date="2026-08-14T00:00:00Z"):
    product = {"name": "テスト玩具"}
    if certifications is not None:
        product["certifications"] = certifications
    return {
        "slug": "2026-08-14-B0XXXXXXXX",
        "date": date,
        "product": product,
        "claims": claims,
    }


def _claim(text):
    return {"claim": text, "category": "safety", "supporting_source_ids": ["src-1"]}


def test_no_cert_mention_is_ok():
    r = check_cert_claims_declared(_article([_claim("木製で角が丸く仕上げてある")]))
    assert r.passed and r.score == 1.0


def test_undeclared_claim_is_soft_deduction():
    # 実データ B0DC6GCTTN (band=zero) の形。certifications 無しで PSC を主張。
    r = check_cert_claims_declared(_article([_claim("舐めても安心な安全設計とPSC基準適合")]))
    assert r.passed, "soft なので合否は変えない"
    assert r.score == CERT_CLAIM_UNDECLARED_SOFT_SCORE
    assert "PSC" in r.message


def test_empty_certifications_still_fires():
    # certifications=[] は check_certifications が "OK (empty)" で抜ける経路。
    # ここを塞ぐのが本チェックの目的なので、[] でも発火すること。
    r = check_cert_claims_declared(
        _article([_claim("STマーク取得済で安全")], certifications=[])
    )
    assert r.score == CERT_CLAIM_UNDECLARED_SOFT_SCORE


def test_declared_claim_is_ok():
    # 申告されていれば check_certifications / check_cert_sources_content が裏取りする。
    r = check_cert_claims_declared(
        _article([_claim("STマーク取得済で安全")], certifications=["ST"])
    )
    assert r.passed and r.score == 1.0


def test_partially_declared_still_fires_for_the_missing_one():
    r = check_cert_claims_declared(
        _article(
            [_claim("CEマークと食品衛生法の両方をクリアしている")],
            certifications=["CE"],
        )
    )
    assert r.score == CERT_CLAIM_UNDECLARED_SOFT_SCORE
    # 申告済みの CE は挙げず、未申告の 食品衛生法 だけを挙げる
    assert "['食品衛生法']" in r.message


def test_en71_is_counted_as_en71_not_ce():
    # _CERT_HTML_TOKENS は CE の alias に "EN71" を持つ (裏取り側で「CE の根拠として
    # EN71 の記述を認める」ため)。検出でそれを使うと EN71 申告済みの記事が CE 未申告
    # として誤発火する。実測で 3 件がこれだった。
    r = check_cert_claims_declared(_article([_claim("EN71をクリアしている")]))
    assert "['EN71']" in r.message

    ok = check_cert_claims_declared(
        _article([_claim("EN71をクリアしている")], certifications=["EN71"])
    )
    assert ok.score == 1.0, "EN71 を申告済みなら CE 未申告として鳴らさない"

    ce = check_cert_claims_declared(_article([_claim("CEマーク取得済")]))
    assert "['CE']" in ce.message, "CE 自身の alias は従来どおり効く"


def test_generic_standard_phrase_is_not_an_st_claim():
    # 「欧米の玩具安全基準に合わせて作られており」は欧州/米国の規格を指す一般表現で、
    # 日本玩具協会の ST マーク取得の主張ではない。_CERT_HTML_TOKENS は裏取り用に
    # "玩具安全基準" を ST の alias に持つが、検出に使うと誤検出になる
    # (実測: 発火 41 件のうち 15 件 = 37% がこれだった)。
    for text in (
        "欧米の厳しい玩具安全基準に合わせて作られており耐久性が高い",
        "欧州または米国の玩具安全基準を満たし、赤ちゃんが口に入れても安全",
        "海外の玩具安全基準に準拠して作られており安全に配慮されている",
    ):
        r = check_cert_claims_declared(_article([_claim(text)]))
        assert r.score == 1.0, text

    # 一方 ST を名指しする語や、発行団体名は従来どおり主張として数える。
    for text in ("STマーク取得済み", "日本玩具協会のST基準に適合"):
        r = check_cert_claims_declared(_article([_claim(text)]))
        assert "['ST']" in r.message, text


def test_negation_is_not_a_claim():
    # 実データ B0DRBKTTCS の形。「記載はありません」を主張と誤認しないこと。
    for text in (
        "STマークなどの明確な安全基準の記載は公式ページにありません",
        "PSCマークの取得は確認できませんでした",
        "食品衛生法に基づく検査の有無は不明です",
    ):
        r = check_cert_claims_declared(_article([_claim(text)]))
        assert r.score == 1.0, text


def test_legacy_article_is_skipped():
    r = check_cert_claims_declared(
        _article([_claim("STマーク取得済")], date="2026-05-01T00:00:00Z")
    )
    assert r.passed and r.score == 1.0
    assert "skipped" in r.message


def test_missing_claims_field_is_skipped():
    r = check_cert_claims_declared({"slug": "2026-08-14-B0X", "date": "2026-08-14T00:00:00Z"})
    assert r.passed and "skipped" in r.message


def test_malformed_claims_do_not_crash():
    for claims in ("not a list", [None, 42, {"claim": None}, {}], []):
        r = check_cert_claims_declared(_article(claims))
        assert r.passed


# --- 施行日ゲート (#5490 昇格 / amazon-navi-brain#13 2-1) ---------------------
#
# 既存 26 件はすべて 2026-08-07 以前の slug。施行日 2026-08-20 以降の slug でだけ
# hard にして、既存記事を触る一括修正 PR を落とさない (#4826 項目2 と同じ設計)。

def test_undeclared_claim_is_hard_on_or_after_enforce_date():
    # 施行日ゲートの入力は slug 先頭 10 文字なので slug 側を施行日以降にする
    d = _article([_claim("STマーク取得済で安全")], date="2026-08-20T00:00:00Z")
    d["slug"] = "2026-08-20-B0XXXXXXXX"
    r = check_cert_claims_declared(d)
    assert not r.passed, "施行日以降は hard"
    assert r.score == CERT_CLAIM_UNDECLARED_SOFT_SCORE
    assert "soft" not in r.message


def test_undeclared_claim_stays_soft_before_enforce_date():
    d = _article([_claim("STマーク取得済で安全")], date="2026-08-19T00:00:00Z")
    d["slug"] = "2026-08-19-B0XXXXXXXX"
    r = check_cert_claims_declared(d)
    assert r.passed, "施行日前の既存記事は soft のまま"
    assert r.score == CERT_CLAIM_UNDECLARED_SOFT_SCORE


def test_declared_claim_is_ok_after_enforce_date():
    d = _article([_claim("STマーク取得済で安全")], certifications=["ST"],
                 date="2026-08-20T00:00:00Z")
    d["slug"] = "2026-08-20-B0XXXXXXXX"
    r = check_cert_claims_declared(d)
    assert r.passed and r.score == 1.0


def test_missing_slug_falls_back_to_enforcing():
    # slug が無い/短すぎて日付を判定できない場合は安全側 (施行する) に倒す
    d = _article([_claim("STマーク取得済で安全")], date="2026-08-14T00:00:00Z")
    d["slug"] = ""
    assert not check_cert_claims_declared(d).passed
