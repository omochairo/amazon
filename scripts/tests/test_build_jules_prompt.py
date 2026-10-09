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


# --------------------------------------------------------------------------
# _first_party_note: omcha.jp の実使用記事を出典に使ってよい注記 (#9199 案 b)
# --------------------------------------------------------------------------

def _write_first_party(tmp_path, rows):
    d = tmp_path / "data" / "analytics"
    d.mkdir(parents=True, exist_ok=True)
    (d / "first_party_sources.json").write_text(json.dumps({"sources": rows}), encoding="utf-8")


def test_first_party_note_lists_primary_omcha_posts(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _write_first_party(tmp_path, [
        {"asin": "B0FNM4Y35G", "role": "primary", "post_url": "https://omcha.jp/review-a/"},
        {"asin": "B0FNM4Y35G", "role": "compared", "post_url": "https://omcha.jp/hikaku-b/"},
    ])
    rc = _run_main(monkeypatch, ["--asin", "B0FNM4Y35G", "--print-note", "first_party"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "https://omcha.jp/review-a/" in out
    assert "hikaku-b" not in out  # 比較で名前が出るだけの記事は渡さない
    assert "1 件まで" in out and "外部のサイト" in out and "navi.omcha.jp" in out


def test_first_party_note_empty_without_post(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_first_party(tmp_path, [
        {"asin": "B0OTHER001", "role": "primary", "post_url": "https://omcha.jp/x/"}])
    assert build_jules_prompt._first_party_note("B0FNM4Y35G") == ""


def test_build_prompt_includes_first_party_note_and_third_party_rule(tmp_path, monkeypatch):
    # 03 (--print-note first_party) と repoless (build_prompt) で同じ文面を渡す
    real_read = build_jules_prompt._read
    monkeypatch.setattr(
        build_jules_prompt, "_read",
        lambda path: "" if path in ("AGENTS.md", "jules/PROMPT_TEMPLATE.md") else real_read(path))
    monkeypatch.chdir(tmp_path)
    asin = "B0FNM4Y35G"
    raw_dir = tmp_path / "data" / "raw"
    raw_dir.mkdir(parents=True)
    (raw_dir / "amazon.json").write_text(
        json.dumps({"items": [{"asin": asin, "title": "テスト商品"}]}), encoding="utf-8")
    _write_first_party(tmp_path, [
        {"asin": asin, "role": "primary", "post_url": "https://omcha.jp/review-a/"}])
    prompt = build_prompt(asin, today="2026-10-08")
    assert build_jules_prompt._first_party_note(asin) in prompt
    assert "third_party_sources.json は、システムが事前収集した非販売の第三者候補" in prompt
    # #9239: 03 (--print-note sources) と同じ組み立て注記を渡し、販売ページを締め出さない
    assert build_jules_prompt._sources_note(asin) in prompt
    assert "楽天・Yahoo の販売ページは sources に入れない" not in prompt


# --------------------------------------------------------------------------
# _sources_note: sources_v5 (合計 5 件・非販売 2 件) の組み立て方 (#9239)
# --------------------------------------------------------------------------

def test_sources_note_lists_mall_pages_and_sorts_candidates(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    asin = "B0FNM4Y35G"
    raw = tmp_path / "data" / "raw"
    (raw / "per_asin" / asin).mkdir(parents=True)
    (raw / "rakuten_matched.json").write_text(json.dumps({"items": [
        {"matched_asin": asin, "url": "https://hb.afl.rakuten.co.jp/hgc/x/?pc=https%3A%2F%2Fitem.rakuten.co.jp%2Fshop%2F123%2F&m=y"},
    ]}), encoding="utf-8")
    (raw / "yahoo_matched.json").write_text(json.dumps({"items": []}), encoding="utf-8")
    (raw / "per_asin" / asin / "third_party_sources.json").write_text(json.dumps({"sources": [
        {"url": "https://ameblo.jp/a/entry-1.html", "host": "ameblo.jp", "title": "使ってみた"},
        {"url": "https://www.yodobashi.com/product/1/", "host": "yodobashi.com"},
        {"url": "https://rocketreach.co/acme", "host": "rocketreach.co"},
        {"url": "https://search.kakaku.com/x", "host": "search.kakaku.com"},
    ]}), encoding="utf-8")
    rc = _run_main(monkeypatch, ["--asin", asin, "--print-note", "sources"])
    out = capsys.readouterr().out
    assert rc == 0
    assert f"https://www.amazon.co.jp/dp/{asin}/" in out
    assert "https://item.rakuten.co.jp/shop/123/" in out  # アフィリエイトを剥がした商品ページ
    assert "Yahoo!ショッピング 商品ページ" not in out  # 照合が無いモールは出さない
    third = out.split("第三者の候補")[1].split("2.")[0]
    assert "ameblo.jp" in third and "yodobashi" not in third
    assert "yodobashi.com" in out.split("通販サイト")[1]
    assert "rocketreach" not in out and "search.kakaku" not in out
    assert "水増しではない" in out and "合計 5 件" in out


def test_sources_note_without_candidates_still_gives_amazon(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    note = build_jules_prompt._sources_note("B0FNM4Y35G")
    assert "https://www.amazon.co.jp/dp/B0FNM4Y35G/" in note
    assert "第三者の候補" not in note
