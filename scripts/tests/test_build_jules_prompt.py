"""build_jules_prompt._audit_note (#2995 消費側② Phase 3 / #3203 Phase 1-C) の単体テスト。"""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from build_jules_prompt import _audit_note, _experience_note  # noqa: E402


def _write_audit(tmp_path, pages):
    analytics_dir = tmp_path / "data" / "analytics"
    analytics_dir.mkdir(parents=True)
    (analytics_dir / "answerability_audit.json").write_text(
        json.dumps({"pages": pages}), encoding="utf-8"
    )


def test_audit_note_generates_block_for_matching_asin(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_audit(tmp_path, [
        {
            "asin": "B0FNM4Y35G",
            "queries": [
                {
                    "query": "ジェリーブロックス 口コミ",
                    "missing_aspects": [
                        "具体的なユーザーの体験談（良い点・悪い点の詳細）",
                        "他のブロック玩具との比較による評価",
                    ],
                },
            ],
        },
    ])
    note = _audit_note("B0FNM4Y35G")
    assert "前回記事に不足していた観点" in note
    assert "ジェリーブロックス 口コミ" in note
    assert "具体的なユーザーの体験談（良い点・悪い点の詳細）" in note
    assert "他のブロック玩具との比較による評価" in note
    assert "創作せず省いて構いません" in note


def test_audit_note_empty_for_non_matching_asin(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_audit(tmp_path, [
        {"asin": "B0OTHERASIN", "queries": [{"query": "q", "missing_aspects": ["x"]}]},
    ])
    assert _audit_note("B0FNM4Y35G") == ""


def test_audit_note_empty_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert _audit_note("B0FNM4Y35G") == ""


def test_audit_note_empty_when_no_missing_aspects(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_audit(tmp_path, [
        {"asin": "B0FNM4Y35G", "queries": [{"query": "q", "missing_aspects": []}]},
    ])
    assert _audit_note("B0FNM4Y35G") == ""


# --------------------------------------------------------------------------
# _experience_note (#3203 Phase 2)
# --------------------------------------------------------------------------

def _write_experience(tmp_path, asin, snippets):
    d = tmp_path / "data" / "raw" / "per_asin" / asin
    d.mkdir(parents=True)
    (d / "experience.json").write_text(
        json.dumps({"asin": asin, "snippets": snippets}), encoding="utf-8"
    )


def test_experience_note_present_when_snippets_exist(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_experience(tmp_path, "B0FNM4Y35G", [
        {"aspect": "体験談", "text": "使い心地が良いという声", "source_type": "blog",
         "source_url": "https://example.com", "usable_as": "quote", "confidence": "high"},
    ])
    note = _experience_note("B0FNM4Y35G")
    assert "体験談素材" in note
    assert "quote" in note and "paraphrase" in note


def test_experience_note_empty_when_no_snippets(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_experience(tmp_path, "B0FNM4Y35G", [])
    assert _experience_note("B0FNM4Y35G") == ""


def test_experience_note_empty_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert _experience_note("B0FNM4Y35G") == ""


def test_experience_note_empty_when_malformed_json(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    d = tmp_path / "data" / "raw" / "per_asin" / "B0FNM4Y35G"
    d.mkdir(parents=True)
    (d / "experience.json").write_text("{not valid json", encoding="utf-8")
    assert _experience_note("B0FNM4Y35G") == ""
