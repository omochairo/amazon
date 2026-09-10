"""scripts/verify_toy_recall_matches.py unit tests (#4320 follow-up)."""
from __future__ import annotations

import json

from scripts import verify_toy_recall_matches as vtrm


class _FakeCompletedProcess:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _write_article(tmp_path, filename, asin, name, brand, name_full=""):
    path = tmp_path / filename
    path.write_text(
        json.dumps({"product": {"asin": asin, "name": name, "name_full": name_full or name, "brand": brand}}),
        encoding="utf-8",
    )
    return path


# --------------------------------------------------------------------------
# build_article_index
# --------------------------------------------------------------------------

def test_build_article_index_normalizes_brand_and_skips_no_asin(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_article(tmp_path, "a.json", "B0000000AA", "くるくるチャイム", "くもん出版(KUMON)")
    _write_article(tmp_path, "b.json", "B0000000BB", "パズル", "くもん出版")
    (tmp_path / "c.json").write_text(json.dumps({"product": {"name": "ASINなし", "brand": "くもん出版"}}), encoding="utf-8")

    index = vtrm.build_article_index(str(tmp_path / "*.json"))
    assert set(a["asin"] for a in index["くもん出版"]) == {"B0000000AA", "B0000000BB"}


def test_build_article_index_excludes_sidecar_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_article(tmp_path, "a.json", "B0000000AA", "本体", "レゴ")
    _write_article(tmp_path, "a.quality.json", "B0000000AA", "品質サイドカー", "レゴ")

    index = vtrm.build_article_index(str(tmp_path / "*.json"))
    assert len(index["レゴ"]) == 1


def test_build_article_index_skips_unreadable_json(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    _write_article(tmp_path, "ok.json", "B0000000AA", "本体", "レゴ")

    index = vtrm.build_article_index(str(tmp_path / "*.json"))
    assert list(index.keys()) == ["レゴ"]


# --------------------------------------------------------------------------
# select_relevant_articles — agy への入力サイズを機械的に抑える
# --------------------------------------------------------------------------

def test_select_relevant_articles_passthrough_when_under_limit():
    articles = [{"asin": "A", "name": "x", "name_full": "x"}]
    assert vtrm.select_relevant_articles({"title": "t"}, articles, limit=5) == articles


def test_select_relevant_articles_keeps_true_match_at_top_when_over_limit():
    target = {"asin": "TARGET0001", "name": "くるくるチャイム", "name_full": "くもん出版 くるくるチャイム"}
    noise = [
        {"asin": f"NOISE{i:04d}", "name": f"無関係な知育玩具{i}", "name_full": f"無関係な知育玩具{i} 木製 3歳以上"}
        for i in range(30)
    ]
    candidate = {"title": "くもん出版「玩具：くるくるチャイム」 - 交換／返金"}

    selected = vtrm.select_relevant_articles(candidate, noise + [target], limit=5)
    assert len(selected) == 5
    assert target in selected


def test_select_relevant_articles_falls_back_to_head_when_title_is_empty():
    articles = [{"asin": f"A{i:09d}", "name": "x", "name_full": "x"} for i in range(10)]
    selected = vtrm.select_relevant_articles({"title": ""}, articles, limit=3)
    assert selected == articles[:3]


# --------------------------------------------------------------------------
# parse_verify_response — fail-closed が肝
# --------------------------------------------------------------------------

def test_parse_verify_response_match_found():
    text = "一致: あり\nASIN: B0BD3GW7S8, B0CL4WXQZ6\n理由: 同一商品のセット違い"
    asins, reason = vtrm.parse_verify_response(text, {"B0BD3GW7S8", "B0CL4WXQZ6"})
    assert asins == ["B0BD3GW7S8", "B0CL4WXQZ6"]
    assert reason == "同一商品のセット違い"


def test_parse_verify_response_no_match():
    text = "一致: なし\n理由: 別商品"
    assert vtrm.parse_verify_response(text, {"B0BD3GW7S8"}) == ([], "別商品")


def test_parse_verify_response_rejects_hallucinated_asin():
    """候補一覧に無い ASIN は幻覚とみなし破棄する。"""
    text = "一致: あり\nASIN: B0000000ZZ\n理由: それっぽい"
    asins, _reason = vtrm.parse_verify_response(text, {"B0BD3GW7S8"})
    assert asins == []


def test_parse_verify_response_malformed_falls_back_to_no_match():
    assert vtrm.parse_verify_response("よくわかりません", {"B0BD3GW7S8"}) == ([], "")


def test_parse_verify_response_match_without_asin_line_falls_back_to_no_match():
    text = "一致: あり\n理由: ASIN欄を書き忘れた"
    asins, reason = vtrm.parse_verify_response(text, {"B0BD3GW7S8"})
    assert asins == []
    assert "不正" in reason


def test_parse_verify_response_dedupes_repeated_asin():
    text = "一致: あり\nASIN: B0BD3GW7S8, B0BD3GW7S8\n理由: 重複"
    asins, _reason = vtrm.parse_verify_response(text, {"B0BD3GW7S8"})
    assert asins == ["B0BD3GW7S8"]


# --------------------------------------------------------------------------
# build_agy_argv — #6539 の罠回避形を固定
# --------------------------------------------------------------------------

def test_build_agy_argv_puts_model_before_print():
    argv = vtrm.build_agy_argv("判定して", "gemini-3.8-flash-high")
    assert argv == ["agy", "--model", "gemini-3.8-flash-high", "--print=判定して"]


# --------------------------------------------------------------------------
# call_agy — dbus-run-session フォールバック + 空応答リトライ
# --------------------------------------------------------------------------

def test_call_agy_returns_stdout_on_success(monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakeCompletedProcess(returncode=0, stdout="一致: なし\n理由: 別商品\n")

    monkeypatch.setattr("scripts.verify_toy_recall_matches.subprocess.run", fake_run)
    text = vtrm.call_agy("プロンプト")
    assert text == "一致: なし\n理由: 別商品"
    assert captured["cmd"][:3] == ["dbus-run-session", "--", "agy"]


def test_call_agy_skips_when_command_not_found(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError()

    monkeypatch.setattr("scripts.verify_toy_recall_matches.subprocess.run", fake_run)
    assert vtrm.call_agy("プロンプト") == ""


def test_call_agy_retries_empty_response_then_succeeds(monkeypatch):
    calls = {"n": 0}

    def fake_run(cmd, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _FakeCompletedProcess(returncode=0, stdout="")
        return _FakeCompletedProcess(returncode=0, stdout="一致: なし\n理由: x")

    monkeypatch.setattr("scripts.verify_toy_recall_matches.subprocess.run", fake_run)
    slept = []
    text = vtrm.call_agy("プロンプト", sleeper=slept.append)
    assert text == "一致: なし\n理由: x"
    assert slept == [vtrm.RETRY_SLEEP_SECONDS]


def test_call_agy_gives_up_after_all_empty(monkeypatch):
    def fake_run(cmd, **kwargs):
        return _FakeCompletedProcess(returncode=0, stdout="")

    monkeypatch.setattr("scripts.verify_toy_recall_matches.subprocess.run", fake_run)
    assert vtrm.call_agy("プロンプト", sleeper=lambda _s: None) == ""


def test_call_agy_retries_timeout_then_succeeds(monkeypatch):
    """mine_experience と異なり timeout もリトライする (見逃し防止、#4320)。"""
    import subprocess

    calls = {"n": 0}

    def fake_run(cmd, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 120))
        return _FakeCompletedProcess(returncode=0, stdout="一致: なし\n理由: x")

    monkeypatch.setattr("scripts.verify_toy_recall_matches.subprocess.run", fake_run)
    slept = []
    assert vtrm.call_agy("プロンプト", sleeper=slept.append) == "一致: なし\n理由: x"
    assert slept == [vtrm.RETRY_SLEEP_SECONDS]


def test_call_agy_gives_up_after_all_timeout(monkeypatch):
    import subprocess

    calls = {"n": 0}

    def fake_run(cmd, **kwargs):
        calls["n"] += 1
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 120))

    monkeypatch.setattr("scripts.verify_toy_recall_matches.subprocess.run", fake_run)
    assert vtrm.call_agy("プロンプト", sleeper=lambda _s: None) == ""
    assert calls["n"] == vtrm.MAX_EXTRA_RETRIES + 1


def test_call_agy_does_not_retry_nonzero_exit(monkeypatch):
    calls = {"n": 0}

    def fake_run(cmd, **kwargs):
        calls["n"] += 1
        return _FakeCompletedProcess(returncode=2, stderr="boom")

    monkeypatch.setattr("scripts.verify_toy_recall_matches.subprocess.run", fake_run)
    assert vtrm.call_agy("プロンプト", sleeper=lambda _s: None) == ""
    assert calls["n"] == 1


# --------------------------------------------------------------------------
# verify_candidates — caller を差し替えて end-to-end 相当を検証
# --------------------------------------------------------------------------

def test_verify_candidates_attaches_matched_asins_from_caller():
    candidates = [
        {"brand": "くもん出版", "title": "くもん出版「玩具：くるくるチャイム」 - 交換／返金", "url": "https://example/1"},
    ]
    index = {
        "くもん出版": [
            {"asin": "B0BD3GW7S8", "name": "くるくるチャイム", "name_full": "くるくるチャイム"},
            {"asin": "B0CL4WXQZ6", "name": "くるくるチャイム＋かさねてコーン20", "name_full": ""},
            {"asin": "B0UNRELATED", "name": "図形キューブ", "name_full": ""},
        ],
    }

    def fake_caller(prompt, *, model, timeout_s):
        assert "B0UNRELATED" in prompt  # 対象ブランドの記事は全部渡している
        return "一致: あり\nASIN: B0BD3GW7S8, B0CL4WXQZ6\n理由: 一次情報と一致"

    out = vtrm.verify_candidates(candidates, index, caller=fake_caller)
    assert out[0]["matched_asins"] == ["B0BD3GW7S8", "B0CL4WXQZ6"]
    assert out[0]["match_reason"] == "一次情報と一致"
    # 入力は破壊しない
    assert "matched_asins" not in candidates[0]


def test_verify_candidates_skips_agy_call_when_brand_has_no_articles():
    calls = []

    def fake_caller(prompt, *, model, timeout_s):
        calls.append(prompt)
        return "一致: あり\nASIN: X\n理由: y"

    out = vtrm.verify_candidates(
        [{"brand": "無名ブランド", "title": "t"}], {}, caller=fake_caller,
    )
    assert out[0]["matched_asins"] == []
    assert calls == []
