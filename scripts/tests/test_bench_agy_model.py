"""scripts/bench_agy_model.py unit tests.

採点器そのもののテスト。bench は agy を実際に叩くので CI では回せないが、
**スコアリングは決定的**なので、そこだけは固定しておく。ここが壊れると
「実測で選んだ」という根拠が黙って崩れる。
"""
from __future__ import annotations

import json

import pytest

from scripts import bench_agy_model as bench

_GOOD = """『ボーネルンド ケルチェッティ』の口コミ要約です。

* 歯車が連動する仕組みに子どもが夢中になると好評です。
* 小さな力でも滑らかに回る操作性が評価されています。
* 最初の組み立ては難しく、大人の手助けが要るという声があります。
"""


def test_score_text_full_marks_on_grounded_bullets():
    r = bench.score_text(_GOOD, "ケルチェッティ", "ボーネルンド")
    assert r["bullets"] == 3
    assert r["parts"]["format"] == 1.0
    assert r["parts"]["japanese"] == 1.0
    assert r["parts"]["no_refusal"] == 1.0
    assert r["parts"]["grounding"] == 1.0
    assert r["parts"]["balance"] == 1.0
    assert r["score"] == 1.0


def test_score_text_penalises_refusal():
    text = "* 該当する商品の口コミは見つかりませんでした。\n* 一般的な情報のみです。\n* 推測になります。"
    r = bench.score_text(text, "ケルチェッティ", "ボーネルンド")
    assert r["parts"]["no_refusal"] == 0.0
    assert r["refusal_markers"]


def test_score_text_penalises_wrong_bullet_count():
    one = "* 歯車が連動して楽しいと好評のボーネルンド ケルチェッティです。"
    assert bench.score_text(one, "ケルチェッティ", "ボーネルンド")["parts"]["format"] == 0.0
    six = "\n".join(f"* ボーネルンド ケルチェッティの評価 {i} です。" for i in range(6))
    assert bench.score_text(six, "ケルチェッティ", "ボーネルンド")["parts"]["format"] == 0.5


def test_score_text_flags_non_japanese_output():
    text = "* Great gears for kids.\n* Smooth to turn.\n* Needs adult help at first."
    r = bench.score_text(text, "Gears", "Bornelund")
    assert r["parts"]["japanese"] == 0.0


def test_product_tokens_keeps_prolonged_sound_mark():
    """カタカナ語の長音符を落とすと grounding が過小評価になる。

    \\p{Katakana} 相当のクラスは ー にマッチしない — カタカナ語を語に割る箇所で
    必ず踏む罠なので、ここで固定しておく。
    """
    toks = bench.product_tokens("ブロックレール", "ブリオ")
    assert "ブロックレール" in toks


def test_score_text_grounding_is_partial_on_partial_hits():
    text = "* ボーネルンドの玩具は評価が高いです。\n* 丈夫です。\n* 価格は高めです。"
    r = bench.score_text(text, "ケルチェッティ QR2341", "ボーネルンド")
    assert 0.0 < r["parts"]["grounding"] < 1.0


def test_call_agy_json_reports_failure_when_agy_missing(monkeypatch):
    def boom(*_a, **_k):
        raise FileNotFoundError("agy")

    monkeypatch.setattr(bench.subprocess, "run", boom)
    monkeypatch.setattr(bench.shutil, "which", lambda _n: None)
    res = bench.call_agy_json("prompt", "gemini-3.8-flash-low")
    assert res["ok"] is False
    assert res["error"] == "agy_not_found"


def test_call_agy_json_parses_success_payload(monkeypatch):
    captured = {}

    class _P:
        returncode = 0
        stdout = json.dumps({
            "status": "SUCCESS", "response": "本文\n", "duration_seconds": 3.5,
            "num_turns": 1, "usage": {"output_tokens": 42, "total_tokens": 99},
        })
        stderr = ""

    def fake_run(cmd, **_k):
        captured["cmd"] = cmd
        return _P()

    monkeypatch.setattr(bench.subprocess, "run", fake_run)
    monkeypatch.setattr(bench.shutil, "which", lambda _n: None)
    res = bench.call_agy_json("prompt", "gemini-3.8-flash-high")
    assert res["ok"] is True
    assert res["text"] == "本文"
    assert res["output_tokens"] == 42
    # variant の空文字は「--model を渡さない = 現状」を意味する
    assert captured["cmd"][:4] == ["agy", "--output-format", "json", "--model"]


def test_call_agy_json_default_variant_omits_model(monkeypatch):
    captured = {}

    class _P:
        returncode = 0
        stdout = json.dumps({"status": "SUCCESS", "response": "本文"})
        stderr = ""

    monkeypatch.setattr(bench.subprocess, "run",
                        lambda cmd, **_k: (captured.update(cmd=cmd), _P())[1])
    monkeypatch.setattr(bench.shutil, "which", lambda _n: None)
    bench.call_agy_json("prompt", "")
    assert "--model" not in captured["cmd"]


def test_summarize_counts_failures_as_zero_and_ranks_by_score():
    records = [
        {"variant": "a", "ok": True, "score": 1.0, "parts": {"grounding": 1.0, "no_refusal": 1.0, "format": 1.0},
         "latency_s": 10.0, "chars": 100, "error": None},
        {"variant": "a", "ok": False, "score": 0.0, "parts": {}, "latency_s": 0.0, "chars": 0, "error": "timeout"},
        {"variant": "b", "ok": True, "score": 0.9, "parts": {"grounding": 0.8, "no_refusal": 1.0, "format": 1.0},
         "latency_s": 5.0, "chars": 90, "error": None},
        {"variant": "b", "ok": True, "score": 0.9, "parts": {"grounding": 0.8, "no_refusal": 1.0, "format": 1.0},
         "latency_s": 6.0, "chars": 90, "error": None},
    ]
    rows = bench.summarize(records)
    by = {r["variant"]: r for r in rows}
    # 失敗を除外して平均すると a が 1.00 で勝ってしまう。落ちる回数も品質のうち。
    assert by["a"]["score_mean"] == 0.5
    assert by["a"]["success_rate"] == 0.5
    assert by["a"]["errors"] == ["timeout"]
    assert rows[0]["variant"] == "b"


def test_load_products_spreads_across_brands(monkeypatch, tmp_path):
    """先頭から素直に取ると同一シリーズだけになり、結論が一般化できない。"""
    identities = {
        "A1": ("t", "シールブック1", "BabyBus"),
        "A2": ("t", "シールブック2", "BabyBus"),
        "A3": ("t", "シールブック3", "BabyBus"),
        "A4": ("t", "木製レール", "ブリオ"),
        "A5": ("t", "ケルチェッティ", "ボーネルンド"),
        "A6": ("t", "無名の玩具", "ノーブランド"),
    }
    for asin in identities:
        (tmp_path / asin).mkdir()
    monkeypatch.setattr(bench.mine_experience, "PER_ASIN_DIR", tmp_path)
    monkeypatch.setattr(bench.mine_experience, "resolve_product_identity",
                        lambda asin, *a, **k: identities[asin])

    got = bench.load_products(3)
    assert [p["brand"] for p in got] == ["BabyBus", "ブリオ", "ボーネルンド"]


def test_load_products_uses_explicit_asins_verbatim(monkeypatch, tmp_path):
    identities = {"A1": ("t", "シールブック1", "BabyBus"), "A2": ("t", "シールブック2", "BabyBus")}
    monkeypatch.setattr(bench.mine_experience, "PER_ASIN_DIR", tmp_path)
    monkeypatch.setattr(bench.mine_experience, "resolve_product_identity",
                        lambda asin, *a, **k: identities[asin])
    got = bench.load_products(5, ["A1", "A2"])
    assert [p["asin"] for p in got] == ["A1", "A2"]


def test_rescore_applies_current_rubric_to_stored_text():
    """採点器を直しても 90 回叩き直さずに済むこと (比較は同じ物差しで揃える)。"""
    records = [
        {"variant": "a", "ok": True, "score": 0.0, "text": _GOOD,
         "product_name": "ケルチェッティ", "brand": "ボーネルンド"},
        {"variant": "a", "ok": False, "score": 0.0, "text": "",
         "product_name": "ケルチェッティ", "brand": "ボーネルンド"},
    ]
    out = bench.rescore(records)
    assert out[0]["score"] == 1.0
    assert out[1]["score"] == 0.0  # 失敗レコードは触らない


def test_diagnostics_count_hedges_and_sources():
    text = (
        "* 楽天のレビューでは高評価のようです。\n"
        "* Amazon の口コミでも好評と思われます。\n"
        "* 丈夫だという声があります。"
    )
    d = bench.score_text(text, "ケルチェッティ", "ボーネルンド")["diagnostics"]
    assert d["hedges"] == 2
    assert d["sources"] >= 2


def test_diagnostics_flag_praise_only_summaries():
    """賞賛のみの要約は素材として弱い (#3203 の凡庸化と直結する)。"""
    praise = "* 楽しいと好評です。\n* 知育効果が高いです。\n* デザインが綺麗です。"
    balanced = praise + "\n* 最初は組み立てが難しく大人の手助けが要るという指摘もあります。"
    assert bench.score_text(praise, "ギア", "ボーネルンド")["diagnostics"]["caveats"] == 0
    assert bench.score_text(balanced, "ギア", "ボーネルンド")["diagnostics"]["caveats"] > 0


def test_summarize_reports_praise_only_rate():
    recs = [
        {"variant": "a", "ok": True, "score": 1.0, "parts": {}, "latency_s": 1.0, "chars": 10,
         "error": None, "diagnostics": {"caveats": 0}},
        {"variant": "a", "ok": True, "score": 1.0, "parts": {}, "latency_s": 1.0, "chars": 10,
         "error": None, "diagnostics": {"caveats": 2}},
    ]
    assert bench.summarize(recs)[0]["praise_only_rate"] == 0.5


def test_balance_is_weighted_not_only_diagnostic():
    """賞賛のみの応答は score でも減点されること (診断表示だけにしない)。"""
    praise = "* 楽しいと好評のボーネルンド ケルチェッティです。\n* 知育効果が高いです。\n* デザインが綺麗です。"
    balanced = praise + "\n* 最初は組み立てが難しいという指摘もあります。"
    praise_score = bench.score_text(praise, "ケルチェッティ", "ボーネルンド")["score"]
    balanced_score = bench.score_text(balanced, "ケルチェッティ", "ボーネルンド")["score"]
    assert balanced_score > praise_score
    assert balanced_score - praise_score == pytest.approx(bench.WEIGHTS["balance"], abs=1e-6)
