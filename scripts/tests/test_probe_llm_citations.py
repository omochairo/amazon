"""test_probe_llm_citations.py

probe_llm_citations.py の単体テストおよび統合テスト。
外部通信 (Network) および外部サブプロセス (Subprocess) を行わずにテストを完結させる。
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import pathlib
from unittest.mock import MagicMock, patch
import pytest

from scripts._llm_citation import SITES
from scripts.probe_llm_citations import (
    ENGINES,
    EngineError,
    build_agy_argv,
    build_prompt,
    engine_fixture,
    engine_perplexity,
    engine_agy,
    load_gsc_rows,
    main,
    seen_key,
)


# ==============================================================================
# build_agy_argv
# ==============================================================================

def test_build_agy_argv_with_model():
    prompt = "おすすめの知育玩具を教えてください"
    model = "gemini-3.8-flash"
    argv = build_agy_argv(prompt, model)
    assert argv == ["agy", "--model", "gemini-3.8-flash", f"--print={prompt}"]
    # --model が必ず --print= の前に来ていることを検証
    assert argv.index("--model") < argv.index(f"--print={prompt}")


def test_build_agy_argv_without_model():
    prompt = "おすすめの知育玩具を教えてください"
    argv_none = build_agy_argv(prompt, None)
    assert argv_none == ["agy", f"--print={prompt}"]
    assert "--model" not in argv_none

    argv_empty = build_agy_argv(prompt, "")
    assert argv_empty == ["agy", f"--print={prompt}"]
    assert "--model" not in argv_empty


# ==============================================================================
# build_prompt
# ==============================================================================

def test_build_prompt_contains_query():
    query = "木製パズル 2歳"
    prompt = build_prompt(query)
    assert isinstance(prompt, str)
    assert query in prompt


# ==============================================================================
# load_gsc_rows
# ==============================================================================

def test_load_gsc_rows_skips_blank_and_malformed(tmp_path):
    p = tmp_path / "gsc_sample.jsonl"
    lines = [
        '{"date": "2026-09-01", "query": "q1", "clicks": 5, "impressions": 50}',
        "",
        "   ",
        "invalid json string {{{",
        '{"date": "2026-09-01", "query": "q2", "clicks": 10, "impressions": 100}',
        "[1, 2, 3]",  # not a dict
    ]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")

    rows = load_gsc_rows(p)
    assert len(rows) == 2
    assert rows[0]["query"] == "q1"
    assert rows[1]["query"] == "q2"


def test_load_gsc_rows_nonexistent():
    rows = load_gsc_rows("non_existent_path.jsonl")
    assert rows == []


# ==============================================================================
# seen_key
# ==============================================================================

def test_seen_key_format():
    assert seen_key("2026-09-09", "navi", "agy") == "2026-09-09|navi|agy"
    assert seen_key("2026-09-09", "omcha", "perplexity") == "2026-09-09|omcha|perplexity"
    assert seen_key("2026-01-01", "navi", "fixture") == "2026-01-01|navi|fixture"


# ==============================================================================
# End-to-end via main() & Idempotency & Series separation
# ==============================================================================

def _setup_mock_gsc(tmp_path: pathlib.Path) -> pathlib.Path:
    """SITES の相対パス構造に合わせて GSC のモックデータを書き込む。"""
    history_dir = tmp_path / "data" / "analytics" / "history"
    history_dir.mkdir(parents=True, exist_ok=True)

    navi_file = tmp_path / SITES["navi"]["gsc_history"]
    navi_data = [
        {"date": "2026-09-01", "query": "navi知育玩具", "clicks": 10, "impressions": 100},
    ]
    with navi_file.open("w", encoding="utf-8") as f:
        for r in navi_data:
            f.write(json.dumps(r) + "\n")

    omcha_file = tmp_path / SITES["omcha"]["gsc_history"]
    omcha_data = [
        {"date": "2026-09-01", "query": "omchaブログ", "clicks": 5, "impressions": 50},
    ]
    with omcha_file.open("w", encoding="utf-8") as f:
        for r in omcha_data:
            f.write(json.dumps(r) + "\n")

    return history_dir


def test_main_e2e_dry_run_and_idempotency(tmp_path):
    _setup_mock_gsc(tmp_path)
    history_dir = tmp_path / "data" / "analytics" / "history"
    citations_file = history_dir / "llm_citations.jsonl"
    seen_file = history_dir / "llm_citations_seen.json"

    # --- 1回目の実行 ---
    code = main(
        ["--root", str(tmp_path), "--dry-run", "--sleep", "0"],
        sleeper=lambda *_: None,
    )
    assert code == 0

    assert citations_file.exists()
    lines = citations_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2

    records = [json.loads(line) for line in lines]
    expected_keys = {
        "date",
        "site",
        "engine",
        "model",
        "query",
        "cited",
        "matched_urls",
        "mention_without_link",
        "urls_found",
        "other_hosts",
        "answer_chars",
        "citation_count",
        "latency_ms",
    }
    for r in records:
        assert set(r.keys()) == expected_keys
        assert r["engine"] == "fixture"
        assert r["model"] == "fixture"

    # navi クエリは site navi で cited=True
    navi_rec = next(r for r in records if r["site"] == "navi")
    assert navi_rec["query"] == "navi知育玩具"
    assert navi_rec["cited"] is True
    assert "https://navi.omcha.jp/example/" in navi_rec["matched_urls"]

    # omcha クエリは site omcha で cited=True
    omcha_rec = next(r for r in records if r["site"] == "omcha")
    assert omcha_rec["query"] == "omchaブログ"
    assert omcha_rec["cited"] is True
    assert "https://omcha.jp/example/" in omcha_rec["matched_urls"]

    # サイドカーファイルの確認
    assert seen_file.exists()
    seen_data = json.loads(seen_file.read_text(encoding="utf-8"))
    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert seen_key(today_utc, "navi", "fixture") in seen_data["seen"]
    assert seen_key(today_utc, "omcha", "fixture") in seen_data["seen"]

    # --- 2回目の実行 (冪等性: 追加行なし) ---
    code2 = main(
        ["--root", str(tmp_path), "--dry-run", "--sleep", "0"],
        sleeper=lambda *_: None,
    )
    assert code2 == 0
    lines2 = citations_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines2) == 2

    # --- 3回目の実行 (--force: 再プローブして追記) ---
    code3 = main(
        ["--root", str(tmp_path), "--dry-run", "--sleep", "0", "--force"],
        sleeper=lambda *_: None,
    )
    assert code3 == 0
    lines3 = citations_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines3) == 4


def test_fixture_engine_series_separation(tmp_path):
    """fixture の navi URL は site omcha に対する cited を True にしない (系列分離の検証)。"""
    history_dir = tmp_path / "data" / "analytics" / "history"
    history_dir.mkdir(parents=True, exist_ok=True)

    # omcha の GSC データに "navi" を含むクエリを登録
    omcha_file = tmp_path / SITES["omcha"]["gsc_history"]
    omcha_data = [
        {"date": "2026-09-01", "query": "navi比較レビュー", "clicks": 10, "impressions": 100},
    ]
    with omcha_file.open("w", encoding="utf-8") as f:
        for r in omcha_data:
            f.write(json.dumps(r) + "\n")

    code = main(
        ["--root", str(tmp_path), "--site", "omcha", "--dry-run", "--sleep", "0"],
        sleeper=lambda *_: None,
    )
    assert code == 0

    citations_file = history_dir / "llm_citations.jsonl"
    lines = citations_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])

    assert rec["site"] == "omcha"
    assert rec["query"] == "navi比較レビュー"
    # fixture は "navi" がクエリに含まれるため https://navi.omcha.jp/example/ を回答に含むが、
    # omcha サイトの host "omcha.jp" と完全一致しないため cited は False になる
    assert rec["cited"] is False
    assert rec["matched_urls"] == []


def test_perplexity_raises_engine_error_when_key_absent(monkeypatch):
    """PERPLEXITY_API_KEY が未設定のとき EngineError を送出する。"""
    monkeypatch.delenv("PERPLEXITY_API_KEY", raising=False)
    with pytest.raises(EngineError, match="PERPLEXITY_API_KEY"):
        engine_perplexity("知育玩具", timeout=10)


# ==============================================================================
# Engine error handling & return code tests
# ==============================================================================

def test_engine_agy_subprocess_failure():
    with patch("subprocess.run") as mock_run:
        mock_res = MagicMock()
        mock_res.returncode = 1
        mock_res.stderr = "Command failed"
        mock_run.return_value = mock_res

        with pytest.raises(EngineError, match="agy process exited with code 1"):
            engine_agy("test query", timeout=10)


def test_engine_agy_empty_stdout():
    with patch("subprocess.run") as mock_run:
        mock_res = MagicMock()
        mock_res.returncode = 0
        mock_res.stdout = "   \n"
        mock_res.stderr = ""
        mock_run.return_value = mock_res

        with pytest.raises(EngineError, match="empty stdout"):
            engine_agy("test query", timeout=10)


def test_engine_perplexity_odd_shape(monkeypatch):
    monkeypatch.setenv("PERPLEXITY_API_KEY", "dummy_key")

    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"unexpected": "structure"}).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        with pytest.raises(EngineError, match="missing valid choices"):
            engine_perplexity("test query", timeout=10)


def test_main_returns_one_when_work_failed_to_write(tmp_path):
    """作業対象があったにもかかわらず1件も書き込めなかった場合は戻り値 1 を返す。"""
    # GSC データはあるが、エンジンがすべて失敗するケース
    history_dir = _setup_mock_gsc(tmp_path)
    citations_file = history_dir / "llm_citations.jsonl"

    def broken_engine(query: str, *, timeout: int):
        raise EngineError("Always failing")

    with patch.dict(ENGINES, {"broken": broken_engine}):
        code = main(
            ["--root", str(tmp_path), "--site", "navi", "--engine", "broken", "--sleep", "0"],
            sleeper=lambda *_: None,
        )
        assert code == 1
        assert not citations_file.exists()
