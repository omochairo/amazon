"""scripts/bench_snippet_yield.py unit tests.

gemma を叩く部分は CI では回せない (K8 の Ollama が要る) が、**集計は決定的**
なので固定しておく。特に落としてはいけないのは「上流で落ちた回を分母に残す」
挙動 — ここが壊れると、失敗した回が黙って消えて歩留まりが実際より良く見える
(本番の experience.json が 0 本のときファイルを書かず生存者バイアスになるのと
同じ罠。#5941 と同型)。
"""
from __future__ import annotations

from scripts import bench_snippet_yield as yield_bench


def _rec(variant, ok, n_snippets, aspect="体験談"):
    return {
        "variant": variant,
        "upstream_ok": ok,
        "candidate_chars": 100 if ok else 0,
        "latency_s": 10.0 if ok else 0.0,
        "n_snippets": n_snippets,
        "snippets": [{"aspect": aspect} for _ in range(n_snippets)],
    }


def test_build_candidate_matches_production_shape():
    """gather_antigravity と同じ形でないと歩留まりが本番と別物になる。"""
    cand = yield_bench.build_candidate("木の質感が良いです。 https://example.com/a\n続き。")
    assert cand["source_type"] == "antigravity"
    assert cand["source_url"] == ""
    assert cand["source_urls"] == []
    assert "https://example.com/a" not in cand["text"]
    assert "木の質感が良いです。" in cand["text"]


def test_build_candidate_truncates_to_production_limit():
    from scripts import mine_experience

    cand = yield_bench.build_candidate("あ" * (mine_experience.MAX_CANDIDATE_TEXT_LEN + 500))
    assert len(cand["text"]) == mine_experience.MAX_CANDIDATE_TEXT_LEN


def test_summarize_keeps_upstream_failures_in_the_denominator():
    rows = yield_bench.summarize([
        _rec("v1", True, 4),
        _rec("v1", True, 2),
        _rec("v1", False, 0),  # agy が落ちた回
        _rec("v1", True, 0),   # agy は応答したが entailed を通らなかった回
    ])
    (r,) = rows
    assert r["n"] == 4
    assert r["upstream_ok_rate"] == 0.75
    # entail_rate は上流 ok の 3 件が分母 (2/3)
    assert r["entail_rate"] == round(2 / 3, 3)
    # レーン全体の本数は 4 件で割る (落ちた回も 0 本として効かせる)
    assert r["snippets_mean"] == 1.5
    # 上流の落ちを除いた変換率は 3 件で割る
    assert r["yield_per_ok"] == 2.0


def test_summarize_sorts_by_lane_total_not_per_ok():
    """上流がよく落ちる variant が yield_per_ok だけ高くて 1 位になると誤る。"""
    rows = yield_bench.summarize([
        _rec("flaky", True, 10),
        _rec("flaky", False, 0),
        _rec("flaky", False, 0),
        _rec("steady", True, 4),
        _rec("steady", True, 4),
        _rec("steady", True, 4),
    ])
    assert [r["variant"] for r in rows] == ["steady", "flaky"]
    assert rows[1]["yield_per_ok"] == 10.0  # per-ok では flaky が勝つ


def test_summarize_counts_aspect_distribution():
    rows = yield_bench.summarize([
        _rec("v1", True, 2, aspect="不満"),
        _rec("v1", True, 1, aspect="体験談"),
    ])
    assert rows[0]["aspects"] == {"不満": 2, "体験談": 1}


def test_summarize_handles_all_failed_variant():
    rows = yield_bench.summarize([_rec("dead", False, 0), _rec("dead", False, 0)])
    (r,) = rows
    assert r["upstream_ok_rate"] == 0.0
    assert r["entail_rate"] == 0.0
    assert r["yield_per_ok"] == 0.0
    assert r["snippets_mean"] == 0.0


def test_format_report_renders_without_error():
    rows = yield_bench.summarize([_rec("v1", True, 3), _rec("v1", False, 0)])
    out = yield_bench.format_report(rows)
    assert "v1" in out
    assert "aspect" in out
