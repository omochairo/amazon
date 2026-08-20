"""#5490 信頼レーン: スキーマ未定義の v5 拡張フィールドの規律 soft チェック。

背景 (2026-08-19 に prompt を quality_gate の実装と突き合わせて判明):

- `review_signals` / `verdict` / `claims` / `persona_fit.not_recommended_for` /
  `technical_specs` は `data/schema/article.schema.json` に無く、gate にも
  チェックが無かった。プロンプト §5.C / §5.D / §5.E / §6.5.2 は件数・字数を
  「厳守」と書いているのに、**機械的には誰も見ていなかった**。
- さらにプロンプト §8 は「件数規律は gate が自動判定するので自己確認は不要
  (要件本体は … §5.D / §5.E に記載済)」と書いており、**gate が見ていない側を
  名指しで gate に任せていた**。§8 は amazon-navi-brain#12 で訂正済み。

字数レンジはプロンプト v7.3 に合わせている (旧規定はプロンプト自身の例と
実出力の双方が違反していたため、実測に合わせて改定された)。

すべて soft (passed=True, score<1.0)。#4826 項目2 と同じく、発火率が 0 に
近づいてから hard 昇格を検討する。
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quality_gate import (  # noqa: E402
    V5_EXTENSION_SOFT_SCORE,
    check_claims_discipline,
    check_persona_fit_counts,
    check_review_signals,
    check_verdict_headline,
)

RECENT = "2026-08-14T00:00:00Z"
LEGACY = "2026-05-01T00:00:00Z"


def _entry(n=1):
    return {"text": "x" * 60, "supporting_source_ids": [f"src-{n}"]}


def _review_signals(**over):
    rs = {
        "summary_one_line": "ギフト用途を中心に、サイズの存在感と安全認証の両立で評価されています。",
        "high_points": [_entry(i) for i in range(3)],
        "concerns": [_entry(i) for i in range(2)],
        "use_scenes": [_entry(i) for i in range(3)],
        "segment_voices": [_entry(1)],
    }
    rs.update(over)
    return rs


def _art(date=RECENT, **over):
    d = {
        "slug": "2026-08-14-B0XXXXXXXX",
        "date": date,
        "product": {"name": "テスト玩具"},
        "persona_fit": {"not_recommended_for": ["a", "b"]},
        "review_signals": _review_signals(),
        "verdict": {"headline": "ギフト用途なら買い、ピース紛失リスクが気になる家庭は一考"},
        "claims": [{"claim": f"c{i}", "cross_checked": i < 2} for i in range(4)],
    }
    d.update(over)
    return d


# --- review_signals (§5.D) -------------------------------------------------

def test_review_signals_ok():
    r = check_review_signals(_art())
    assert r.passed and r.score == 1.0


def test_review_signals_count_out_of_range():
    r = check_review_signals(_art(review_signals=_review_signals(high_points=[_entry()])))
    assert r.passed, "soft なので合否は変えない"
    assert r.score == V5_EXTENSION_SOFT_SCORE
    assert "high_points" in r.message


def test_review_signals_summary_length():
    # v7.3 で 35-70 字。旧規定 (50-100) の下限に届かない 45 字前後が実出力の中央値。
    short = check_review_signals(_art(review_signals=_review_signals(summary_one_line="短い")))
    assert short.score == V5_EXTENSION_SOFT_SCORE
    ok = check_review_signals(_art(review_signals=_review_signals(summary_one_line="あ" * 45)))
    assert ok.score == 1.0, "45 字は v7.3 では適合"


def test_review_signals_empty_supporting_ids():
    rs = _review_signals(concerns=[{"text": "x", "supporting_source_ids": []},
                                   {"text": "y", "supporting_source_ids": ["src-1"]}])
    r = check_review_signals(_art(review_signals=rs))
    assert r.score == V5_EXTENSION_SOFT_SCORE
    assert "supporting_source_ids" in r.message


def test_review_signals_missing_field_fires():
    r = check_review_signals(_art(review_signals=None))
    assert r.score == V5_EXTENSION_SOFT_SCORE


def test_review_signals_legacy_skipped():
    r = check_review_signals(_art(date=LEGACY, review_signals=None))
    assert r.passed and r.score == 1.0 and "legacy" in r.message


# --- verdict.headline (§5.E) ----------------------------------------------

def test_verdict_headline_ok():
    # プロンプト §5.E の実例 (28 字)。v7.3 の 25-50 字に適合する。
    r = check_verdict_headline(_art())
    assert r.passed and r.score == 1.0


def test_verdict_headline_too_long_is_a_description():
    long = "あ" * 60
    r = check_verdict_headline(_art(verdict={"headline": long}))
    assert r.passed and r.score == V5_EXTENSION_SOFT_SCORE
    assert "長い" in r.message


def test_verdict_headline_too_short():
    r = check_verdict_headline(_art(verdict={"headline": "買い"}))
    assert r.score == V5_EXTENSION_SOFT_SCORE
    assert "短い" in r.message


def test_verdict_headline_missing():
    r = check_verdict_headline(_art(verdict={}))
    assert r.score == V5_EXTENSION_SOFT_SCORE


# --- claims (§6.5.2) -------------------------------------------------------

def test_claims_discipline_ok():
    r = check_claims_discipline(_art())
    assert r.passed and r.score == 1.0


def test_claims_too_few():
    r = check_claims_discipline(_art(claims=[{"claim": "a", "cross_checked": True},
                                             {"claim": "b", "cross_checked": True}]))
    assert r.score == V5_EXTENSION_SOFT_SCORE
    assert "4 件未満" in r.message


def test_cross_checked_too_few():
    r = check_claims_discipline(
        _art(claims=[{"claim": f"c{i}", "cross_checked": False} for i in range(4)])
    )
    assert r.score == V5_EXTENSION_SOFT_SCORE
    assert "cross_checked" in r.message


def test_claims_absent_is_skipped():
    d = _art()
    del d["claims"]
    r = check_claims_discipline(d)
    assert r.passed and "skipped" in r.message


# --- persona_fit (§5.C) ----------------------------------------------------

def test_persona_fit_counts_ok():
    r = check_persona_fit_counts(_art())
    assert r.passed and r.score == 1.0


def test_persona_fit_counts_out_of_range():
    for nr in ([], ["a"], ["a", "b", "c", "d"]):
        r = check_persona_fit_counts(
            _art(persona_fit={"not_recommended_for": nr})
        )
        assert r.score == V5_EXTENSION_SOFT_SCORE, nr


def test_persona_fit_missing_fires():
    r = check_persona_fit_counts(_art(persona_fit={}))
    assert r.score == V5_EXTENSION_SOFT_SCORE


# --- 共通: 壊れた入力でも落ちない -------------------------------------------

def test_malformed_inputs_do_not_crash():
    for over in (
        {"review_signals": "not a dict"},
        {"claims": "not a list"},
        {"verdict": {"headline": 42}},
        {"persona_fit": {"not_recommended_for": "not a list"}},
    ):
        d = _art(**over)
        for fn in (check_review_signals, check_verdict_headline,
                   check_claims_discipline, check_persona_fit_counts):
            assert fn(d).passed


# --- persona_fit_counts の施行日ゲート (#5490 昇格 / brain#13 2-1) ------------
#
# 全 2,106 本での発火は 2 件のみ・直近 300 本で 0 だったため hard へ昇格した。
# 既存 2 件 (2026-06-15 / 2026-07-27) を巻き込まないよう施行日で切る。

def test_persona_fit_counts_is_hard_on_or_after_enforce_date():
    d = _art(persona_fit={"not_recommended_for": []})
    d["slug"] = "2026-08-20-B0XXXXXXXX"
    r = check_persona_fit_counts(d)
    assert not r.passed, "施行日以降は hard"
    assert r.score == V5_EXTENSION_SOFT_SCORE


def test_persona_fit_counts_stays_soft_before_enforce_date():
    d = _art(persona_fit={"not_recommended_for": []})
    d["slug"] = "2026-08-19-B0XXXXXXXX"
    r = check_persona_fit_counts(d)
    assert r.passed and r.score == V5_EXTENSION_SOFT_SCORE


def test_persona_fit_counts_ok_after_enforce_date():
    d = _art(persona_fit={"not_recommended_for": ["a", "b"]})
    d["slug"] = "2026-08-20-B0XXXXXXXX"
    r = check_persona_fit_counts(d)
    assert r.passed and r.score == 1.0


def test_other_three_stay_soft_after_enforce_date():
    # review_signals / verdict_headline / claims_discipline は据え置き。
    # 施行日以降の slug でも合否を変えないこと。
    d = _art(review_signals={"summary_one_line": "短い"},
             verdict={"headline": "短"},
             claims=[])
    d["slug"] = "2026-08-20-B0XXXXXXXX"
    for fn in (check_review_signals, check_verdict_headline, check_claims_discipline):
        r = fn(d)
        assert r.passed, fn.__name__
        assert r.score == V5_EXTENSION_SOFT_SCORE, fn.__name__
