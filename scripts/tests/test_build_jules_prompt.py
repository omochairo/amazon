"""build_jules_prompt._audit_note (#2995 消費側② Phase 3 / #3203 Phase 1-C) の単体テスト。"""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import build_jules_prompt  # noqa: E402
from build_jules_prompt import _audit_note, _experience_note, build_prompt, main  # noqa: E402


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


# --------------------------------------------------------------------------
# --print-note CLI (#3203 未配線修正・2026-07-16: 03-invoke-jules.yml から呼ぶ軽量モード)
# --------------------------------------------------------------------------

def _run_main(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", ["build_jules_prompt.py"] + argv)
    return main()


def test_print_note_audit_outputs_note_for_matching_asin(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _write_audit(tmp_path, [
        {
            "asin": "B0FNM4Y35G",
            "queries": [
                {"query": "ジェリーブロックス 口コミ", "missing_aspects": ["対象年齢の目安"]},
            ],
        },
    ])
    rc = _run_main(monkeypatch, ["--asin", "B0FNM4Y35G", "--print-note", "audit"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "前回記事に不足していた観点" in out
    assert "対象年齢の目安" in out


def test_print_note_audit_empty_for_non_matching_asin(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _write_audit(tmp_path, [
        {"asin": "B0OTHERASIN", "queries": [{"query": "q", "missing_aspects": ["x"]}]},
    ])
    rc = _run_main(monkeypatch, ["--asin", "B0FNM4Y35G", "--print-note", "audit"])
    out = capsys.readouterr().out
    assert rc == 0
    assert out == ""


def test_print_note_experience_outputs_note_when_snippets_exist(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _write_experience(tmp_path, "B0FNM4Y35G", [
        {"aspect": "体験談", "text": "使い心地が良いという声", "source_type": "blog",
         "source_url": "https://example.com", "usable_as": "quote", "confidence": "high"},
    ])
    rc = _run_main(monkeypatch, ["--asin", "B0FNM4Y35G", "--print-note", "experience"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "体験談素材" in out
    assert "quote" in out and "paraphrase" in out


def test_print_note_experience_empty_when_file_missing(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    rc = _run_main(monkeypatch, ["--asin", "B0FNM4Y35G", "--print-note", "experience"])
    out = capsys.readouterr().out
    assert rc == 0
    assert out == ""


# --------------------------------------------------------------------------
# build_prompt: per_asin/youtube.json の title_only 除外 (#8162 案 A)
# --------------------------------------------------------------------------

def test_build_prompt_excludes_title_only_youtube_items(tmp_path, monkeypatch):
    # jules/PROMPT_TEMPLATE.md は private repo 側にしか無いので _read だけ差し替える
    # (AGENTS.md / jules/PROMPT_TEMPLATE.md 以外の実ファイル読み込みは素通し)。
    real_read = build_jules_prompt._read
    monkeypatch.setattr(
        build_jules_prompt, "_read",
        lambda path: "" if path in ("AGENTS.md", "jules/PROMPT_TEMPLATE.md") else real_read(path))
    monkeypatch.chdir(tmp_path)
    asin = "B0FNM4Y35G"
    raw_dir = tmp_path / "data" / "raw"
    (raw_dir).mkdir(parents=True)
    (raw_dir / "amazon.json").write_text(
        json.dumps({"items": [{"asin": asin, "title": "テスト商品"}]}), encoding="utf-8")
    per_asin_dir = raw_dir / "per_asin" / asin
    per_asin_dir.mkdir(parents=True)
    (per_asin_dir / "youtube.json").write_text(json.dumps({"items": [
        {"title": "一般語の別商品", "url": "https://www.youtube.com/watch?v=titleonly01",
         "_match": "title_only"},
        {"title": "判定済みの動画", "url": "https://www.youtube.com/watch?v=confirmed01"},
    ]}), encoding="utf-8")

    prompt = build_prompt(asin, today="2026-09-24")
    assert "confirmed01" in prompt
    assert "判定済みの動画" in prompt
    assert "titleonly01" not in prompt
    assert "一般語の別商品" not in prompt
