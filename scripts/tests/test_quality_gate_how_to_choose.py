"""narrative.how_to_choose (#3203 Phase 1-A 比較・選び分け) の quality_gate 検査。"""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quality_gate import (  # noqa: E402
    HOW_TO_CHOOSE_ENFORCE_FROM,
    HOW_TO_CHOOSE_INLINE_ASIN_SOFT_SCORE,
    check_how_to_choose,
)


HOW_TO_CHOOSE_OK = [
    "似た商品が多い中で、どれを選ぶべきか迷う方は多いはずです。定番ブランドと何が違うのかが気になるポイントでしょう。",
    "本品は完成形の自由度が高く、創作の幅を重視するご家庭に向いています。ピースの組み合わせ次第で遊び方が広がります。",
    "一方で、組みやすさを重視するなら定番ブランドの大粒タイプが向いています。年齢が低いお子さまには扱いやすさが優先されます。",
    "創作の幅を求めるご家庭には本品、初めてのブロック遊びには定番ブランドをおすすめします。",
]


def _article(*, slug="2026-07-16-B0XXXXXXXX", how_to_choose=HOW_TO_CHOOSE_OK, asin="B0XXXXXXXX"):
    return {
        "slug": slug,
        "narrative": {"how_to_choose": how_to_choose},
        "product": {"asin": asin},
    }


def test_pre_enforce_slug_skips():
    article = _article(slug="2026-07-15-B0XXXXXXXX", how_to_choose=None)
    result = check_how_to_choose(article)
    assert result.passed
    assert "skipped" in result.message
    assert "pre-v7" in result.message


def test_enforce_date_missing_how_to_choose_fails():
    assert HOW_TO_CHOOSE_ENFORCE_FROM == "2026-07-16"
    article = _article(slug="2026-07-16-B0XXXXXXXX", how_to_choose=None)
    result = check_how_to_choose(article)
    assert not result.passed
    assert "missing" in result.message


def test_enforce_date_empty_how_to_choose_fails():
    article = _article(slug="2026-07-20-B0XXXXXXXX", how_to_choose=[])
    result = check_how_to_choose(article)
    assert not result.passed


def test_enforce_date_short_how_to_choose_fails_char_count():
    article = _article(slug="2026-07-16-B0XXXXXXXX", how_to_choose=["短い文です。"])
    result = check_how_to_choose(article)
    assert not result.passed
    assert "chars" in result.message


def test_normal_150_chars_passes_no_asin_mentions():
    article = _article()
    result = check_how_to_choose(article)
    assert result.passed
    assert result.score == 1.0
    assert result.message == "OK"


def test_own_asin_mention_allowed_without_competitors_file(tmp_path, monkeypatch):
    """自商品 ASIN は hard では落とさない (捏造ではない) が soft では減点する。"""
    monkeypatch.chdir(tmp_path)
    how_to_choose = HOW_TO_CHOOSE_OK + ["B0XXXXXXXX は完成形の自由度が高い代表例です。"]
    article = _article(how_to_choose=how_to_choose, asin="B0XXXXXXXX")
    result = check_how_to_choose(article)
    assert result.passed
    assert result.score == HOW_TO_CHOOSE_INLINE_ASIN_SOFT_SCORE
    assert "B0XXXXXXXX" in result.message


def test_competitor_asin_not_in_competitors_json_fails(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    comp_dir = tmp_path / "data" / "raw" / "per_asin" / "B0XXXXXXXX"
    comp_dir.mkdir(parents=True)
    (comp_dir / "competitors.json").write_text(
        json.dumps({"asin": "B0XXXXXXXX", "competitors": [{"asin": "B0REALCOMP", "name": "実在競合"}]}),
        encoding="utf-8",
    )
    how_to_choose = HOW_TO_CHOOSE_OK + ["競合の B0FAKECOMP は組みやすさ重視です。"]
    article = _article(how_to_choose=how_to_choose, asin="B0XXXXXXXX")
    result = check_how_to_choose(article)
    assert not result.passed
    assert "B0FAKECOMP" in result.message


def test_competitor_asin_in_competitors_json_passes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    comp_dir = tmp_path / "data" / "raw" / "per_asin" / "B0XXXXXXXX"
    comp_dir.mkdir(parents=True)
    (comp_dir / "competitors.json").write_text(
        json.dumps({"asin": "B0XXXXXXXX", "competitors": [{"asin": "B0REALCOMP", "name": "実在競合"}]}),
        encoding="utf-8",
    )
    how_to_choose = HOW_TO_CHOOSE_OK + ["競合の B0REALCOMP は組みやすさ重視です。"]
    article = _article(how_to_choose=how_to_choose, asin="B0XXXXXXXX")
    result = check_how_to_choose(article)
    # #4826 項目2: 実在競合なので hard (捏造検査) は通す。表記規律の soft だけ減点。
    assert result.passed
    assert result.score == HOW_TO_CHOOSE_INLINE_ASIN_SOFT_SCORE
    assert "B0REALCOMP" in result.message


def test_inline_asin_soft_does_not_flip_passed(tmp_path, monkeypatch):
    """施行日 (2026-08-11) より前の slug では合否を変えない。

    #4826 項目2 の hard 昇格後も、既存 94 本 (2026-07-16〜08-10 = v7 施行後・
    プロンプト v7.2 前の窓) はこの経路に残る。census の「減点のみ」で消化の
    進み方を追い続けるため、soft のまま観測対象にしておく。
    """
    monkeypatch.chdir(tmp_path)
    comp_dir = tmp_path / "data" / "raw" / "per_asin" / "B0XXXXXXXX"
    comp_dir.mkdir(parents=True)
    (comp_dir / "competitors.json").write_text(
        json.dumps({"asin": "B0XXXXXXXX", "competitors": [{"asin": "B0REALCOMP", "name": "実在競合"}]}),
        encoding="utf-8",
    )
    article = _article(
        how_to_choose=HOW_TO_CHOOSE_OK + ["B0XXXXXXXX と B0REALCOMP を比べると…。"],
        asin="B0XXXXXXXX",
    )
    result = check_how_to_choose(article)
    assert result.passed is True
    assert 0.0 < result.score < 1.0
    # 自商品・競合の両方が soft の対象 (読者に意味がないのは同じ)
    assert "B0XXXXXXXX" in result.message
    assert "B0REALCOMP" in result.message


def test_hallucinated_asin_still_hard_fails_not_softened():
    """soft 化で捏造検出が緩まないこと (competitors.json 不在は従来どおり hard fail)。"""
    article = _article(
        how_to_choose=HOW_TO_CHOOSE_OK + ["競合の B0FAKECOMP は…。"],
        asin="B0NOSUCHPD",
    )
    result = check_how_to_choose(article)
    assert result.passed is False
    assert result.score == 0.0


def test_pre_enforce_slug_skips_even_with_inline_asin():
    """施行日前は soft 減点も出さない (既存 1494 件のスコアを動かさない)。"""
    article = _article(
        slug="2026-07-15-B0XXXXXXXX",
        how_to_choose=HOW_TO_CHOOSE_OK + ["B0OTHERCMP も候補です。"],
    )
    result = check_how_to_choose(article)
    assert result.passed
    assert result.score == 1.0


def test_competitors_json_missing_with_asin_mention_fails_safe(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    how_to_choose = HOW_TO_CHOOSE_OK + ["競合の B0SOMEOTHR は組みやすさ重視です。"]
    article = _article(how_to_choose=how_to_choose, asin="B0XXXXXXXX")
    result = check_how_to_choose(article)
    assert not result.passed
    assert "unreadable" in result.message


# ---------------------------------------------------------------------------
# 生 ASIN 表記の hard 昇格 (#4826 項目2)
#
# プロンプト v7.2 (2026-08-10T01:00Z) 以降に生成された 163 本で発火 0 だったため、
# soft 導入時 (#4855) に置いた昇格条件を満たした。既存 94 本を巻き込まないよう、
# 封じ込め検査とは別の施行日 (HOW_TO_CHOOSE_INLINE_ASIN_ENFORCE_FROM) で切る。
# ---------------------------------------------------------------------------

def test_inline_asin_hard_fails_after_enforce_date(tmp_path, monkeypatch):
    """施行日以降の slug では生 ASIN が hard 不合格になる。"""
    monkeypatch.chdir(tmp_path)
    comp_dir = tmp_path / "data" / "raw" / "per_asin" / "B0XXXXXXXX"
    comp_dir.mkdir(parents=True)
    (comp_dir / "competitors.json").write_text(
        json.dumps({"asin": "B0XXXXXXXX", "competitors": [{"asin": "B0REALCOMP", "name": "実在競合"}]}),
        encoding="utf-8",
    )
    article = _article(
        slug="2026-08-11-B0XXXXXXXX",
        how_to_choose=HOW_TO_CHOOSE_OK + ["競合の B0REALCOMP は組みやすさ重視です。"],
        asin="B0XXXXXXXX",
    )
    result = check_how_to_choose(article)
    assert result.passed is False
    assert "B0REALCOMP" in result.message
    # 捏造 (score 0.0) とは区別できる。実在競合を名前で呼んでいないだけ。
    assert result.score == HOW_TO_CHOOSE_INLINE_ASIN_SOFT_SCORE


def test_inline_own_asin_also_hard_after_enforce_date(tmp_path, monkeypatch):
    """自商品の ASIN も対象。読者に意味がないのは競合と同じ。"""
    monkeypatch.chdir(tmp_path)
    article = _article(
        slug="2026-08-11-B0XXXXXXXX",
        how_to_choose=HOW_TO_CHOOSE_OK + ["B0XXXXXXXX は完成形の自由度が高い代表例です。"],
        asin="B0XXXXXXXX",
    )
    result = check_how_to_choose(article)
    assert result.passed is False
    assert "B0XXXXXXXX" in result.message


def test_inline_asin_day_before_enforce_date_stays_soft(tmp_path, monkeypatch):
    """施行日の前日はまだ soft (既存 94 本を落とさない境界)。"""
    monkeypatch.chdir(tmp_path)
    article = _article(
        slug="2026-08-10-B0XXXXXXXX",
        how_to_choose=HOW_TO_CHOOSE_OK + ["B0XXXXXXXX は…。"],
        asin="B0XXXXXXXX",
    )
    result = check_how_to_choose(article)
    assert result.passed is True
    assert result.score == HOW_TO_CHOOSE_INLINE_ASIN_SOFT_SCORE


def test_no_inline_asin_after_enforce_date_still_passes():
    """施行日以降でも、ASIN を書いていなければ従来どおり満点。"""
    result = check_how_to_choose(_article(slug="2026-08-11-B0XXXXXXXX"))
    assert result.passed is True
    assert result.score == 1.0
    assert result.message == "OK"


def test_missing_slug_enforces_on_the_safe_side():
    """slug が無い場合は施行する (施行日ゲートと同じ流儀)。"""
    article = {
        "narrative": {"how_to_choose": HOW_TO_CHOOSE_OK + ["B0XXXXXXXX は…。"]},
        "product": {"asin": "B0XXXXXXXX"},
    }
    result = check_how_to_choose(article)
    assert result.passed is False
