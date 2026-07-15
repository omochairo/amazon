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
    assert result.message == "OK"


def test_own_asin_mention_allowed_without_competitors_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    how_to_choose = HOW_TO_CHOOSE_OK + ["B0XXXXXXXX は完成形の自由度が高い代表例です。"]
    article = _article(how_to_choose=how_to_choose, asin="B0XXXXXXXX")
    result = check_how_to_choose(article)
    assert result.passed


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
    assert result.passed


def test_competitors_json_missing_with_asin_mention_fails_safe(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    how_to_choose = HOW_TO_CHOOSE_OK + ["競合の B0SOMEOTHR は組みやすさ重視です。"]
    article = _article(how_to_choose=how_to_choose, asin="B0XXXXXXXX")
    result = check_how_to_choose(article)
    assert not result.passed
    assert "unreadable" in result.message
