"""scripts/run_lighthouse_lane.py unit tests (#2995 案6 / epic #1357).

Lighthouse の実行は run_lighthouse_once / subprocess をモックして避ける
(CI で Chrome を起動しない — 本スクリプトは self-hosted runner 専用)。
"""
from __future__ import annotations

import json
import pathlib

import pytest

import scripts.run_lighthouse_lane as rll
from scripts.run_lighthouse_lane import (
    LIGHTHOUSE_HISTORY_FILENAME,
    aggregate_runs,
    append_records,
    baseline_for,
    build_lighthouse_argv,
    detect_regressions,
    extract_metrics,
    get_targets,
    load_history,
    render_report,
)


def _lh_json(lcp=2000.0, cls=0.01, tbt=100.0, score=0.95, lcp_error=None):
    """Lighthouse JSON の最小形。"""
    audits = {
        "largest-contentful-paint": {"numericValue": lcp},
        "first-contentful-paint": {"numericValue": 1200.0},
        "cumulative-layout-shift": {"numericValue": cls},
        "total-blocking-time": {"numericValue": tbt},
        "speed-index": {"numericValue": 1500.0},
    }
    if lcp_error:
        audits["largest-contentful-paint"] = {"errorMessage": lcp_error}
    return {"categories": {"performance": {"score": score}}, "audits": audits}


# ---------- extract_metrics ----------

def test_extract_metrics_reads_values_and_score():
    m = extract_metrics(_lh_json(lcp=2500.0, cls=0.05, score=0.9))
    assert m["lcp"] == 2500.0
    assert m["cls"] == 0.05
    assert m["perf_score"] == 90.0
    assert "lcp_error" not in m


def test_extract_metrics_separates_error_from_value():
    """NO_LCP は値ではなく error として記録する (2026-07-16 PSI の教訓)。"""
    m = extract_metrics(_lh_json(lcp_error="NO_LCP"))
    assert m["lcp"] is None
    assert m["lcp_error"] == "NO_LCP"
    # 他の metric は生きている
    assert m["cls"] == 0.01


def test_extract_metrics_handles_null_score():
    lh = _lh_json()
    lh["categories"]["performance"]["score"] = None
    assert extract_metrics(lh)["perf_score"] is None


def test_extract_metrics_records_runtime_error():
    lh = _lh_json()
    lh["runtimeError"] = {"code": "ERRORED_DOCUMENT_REQUEST"}
    assert extract_metrics(lh)["runtime_error"] == "ERRORED_DOCUMENT_REQUEST"


def test_extract_metrics_ignores_no_error_sentinel():
    lh = _lh_json()
    lh["runtimeError"] = {"code": "NO_ERROR"}
    assert "runtime_error" not in extract_metrics(lh)


# ---------- aggregate_runs ----------

def test_aggregate_runs_takes_median():
    runs = [extract_metrics(_lh_json(lcp=v)) for v in (1000.0, 5000.0, 2000.0)]
    agg = aggregate_runs(runs)
    assert agg["lcp"] == 2000.0
    assert agg["runs"] == 3


def test_aggregate_runs_excludes_errored_runs_from_median():
    """error 混じりでも、値のある run だけで median を出す。"""
    runs = [
        extract_metrics(_lh_json(lcp=2000.0)),
        extract_metrics(_lh_json(lcp_error="NO_LCP")),
        extract_metrics(_lh_json(lcp=3000.0)),
    ]
    agg = aggregate_runs(runs)
    assert agg["lcp"] == 2500.0  # (2000+3000)/2 — error run は母数外
    assert agg["lcp_error"] == "NO_LCP"
    assert agg["lcp_error_runs"] == 1


def test_aggregate_runs_all_errored_keeps_value_none():
    runs = [extract_metrics(_lh_json(lcp_error="NO_LCP")) for _ in range(2)]
    agg = aggregate_runs(runs)
    assert agg["lcp"] is None
    assert agg["lcp_error_runs"] == 2


def test_aggregate_runs_empty():
    assert aggregate_runs([]) == {}


# ---------- detect_regressions ----------

def _row(url="https://x/", ff="mobile", **kw):
    base = {"date": "2026-07-16", "url": url, "form_factor": ff, "runs": 3}
    base.update(kw)
    return base


def test_detect_regressions_quiet_when_stable():
    history = [_row(lcp=2000.0, perf_score=95.0) for _ in range(3)]
    current = [_row(lcp=2050.0, perf_score=95.0)]
    assert detect_regressions(history, current) == []


def test_detect_regressions_flags_threshold_crossing():
    """good 閾値 (LCP 2500) を跨いだら鳴る。"""
    history = [_row(lcp=2000.0) for _ in range(3)]
    current = [_row(lcp=4000.0)]
    alerts = detect_regressions(history, current)
    kinds = {a["kind"] for a in alerts}
    assert "threshold" in kinds


def test_detect_regressions_flags_audit_error():
    history = [_row(lcp=2000.0) for _ in range(3)]
    current = [_row(lcp=None, lcp_error="NO_LCP", lcp_error_runs=3)]
    alerts = detect_regressions(history, current)
    assert any(a["kind"] == "error" and a["metric"] == "lcp" for a in alerts)


def test_detect_regressions_ignores_small_noise_above_threshold():
    """元から閾値超えでも、微小な揺れでは鳴らさない (誤検出で issue を湧かせない)。"""
    history = [_row(lcp=5000.0) for _ in range(3)]
    current = [_row(lcp=5100.0)]
    assert detect_regressions(history, current) == []


def test_detect_regressions_flags_relative_regression_when_already_bad():
    """元から悪くても、baseline 比で大きく悪化したら鳴る。"""
    history = [_row(lcp=5000.0) for _ in range(3)]
    current = [_row(lcp=8000.0)]
    alerts = detect_regressions(history, current)
    assert any(a["kind"] == "relative" for a in alerts)


def test_detect_regressions_first_run_flags_only_threshold():
    """baseline が無い初回計測では、閾値超えだけ拾う。"""
    alerts = detect_regressions([], [_row(lcp=6000.0)])
    assert len(alerts) == 1
    assert alerts[0]["kind"] == "threshold"
    assert alerts[0]["baseline"] is None


def test_detect_regressions_first_run_quiet_when_good():
    assert detect_regressions([], [_row(lcp=1500.0)]) == []


def test_detect_regressions_flags_score_drop():
    history = [_row(lcp=2000.0, perf_score=95.0) for _ in range(3)]
    current = [_row(lcp=2000.0, perf_score=80.0)]
    alerts = detect_regressions(history, current)
    assert any(a["kind"] == "score" for a in alerts)


def test_detect_regressions_is_per_url_and_form_factor():
    """別 URL の履歴を baseline に使ってはいけない。"""
    history = [_row(url="https://a/", lcp=2000.0) for _ in range(3)]
    current = [_row(url="https://b/", lcp=2100.0)]
    # b は初回計測 + 閾値内なので鳴らない (a の baseline に引きずられない)
    assert detect_regressions(history, current) == []


def test_detect_regressions_error_skips_value_checks():
    """error の行は値判定に進まない (None との比較で落ちない)。"""
    history = [_row(lcp=2000.0) for _ in range(3)]
    current = [_row(lcp=None, lcp_error="NO_LCP")]
    alerts = detect_regressions(history, current)
    assert all(a["kind"] == "error" for a in alerts)


def test_detect_regressions_flags_runtime_error():
    alerts = detect_regressions([], [_row(runtime_error="ERRORED_DOCUMENT_REQUEST")])
    assert any(a["metric"] == "runtime" for a in alerts)


# ---------- baseline_for ----------

def test_baseline_for_uses_window_and_skips_nulls():
    history = [
        _row(lcp=1000.0), _row(lcp=None), _row(lcp=2000.0), _row(lcp=3000.0),
    ]
    assert baseline_for(history, "https://x/", "mobile", "lcp", window=5) == 2000.0


def test_baseline_for_returns_none_without_data():
    assert baseline_for([], "https://x/", "mobile", "lcp", window=5) is None


# ---------- history io ----------

def test_append_and_load_history_roundtrip(tmp_path: pathlib.Path):
    path = tmp_path / LIGHTHOUSE_HISTORY_FILENAME
    append_records(path, [_row(lcp=2000.0), _row(lcp=2100.0)])
    rows = load_history(path)
    assert [r["lcp"] for r in rows] == [2000.0, 2100.0]


def test_load_history_skips_malformed_lines(tmp_path: pathlib.Path):
    path = tmp_path / LIGHTHOUSE_HISTORY_FILENAME
    path.write_text('{"url": "https://x/"}\nnot json\n\n', encoding="utf-8")
    assert len(load_history(path)) == 1


def test_load_history_missing_file(tmp_path: pathlib.Path):
    assert load_history(tmp_path / "nope.jsonl") == []


def test_append_records_noop_on_empty(tmp_path: pathlib.Path):
    path = tmp_path / LIGHTHOUSE_HISTORY_FILENAME
    assert append_records(path, []) == 0
    assert not path.exists()


# ---------- targets ----------

def test_get_targets_origin_only_without_gsc(tmp_path: pathlib.Path, monkeypatch):
    """gsc_path が無く、リポジトリの gsc_history フォールバックも無いケース。"""
    monkeypatch.setattr(rll, "latest_gsc_history", lambda: None)
    t = get_targets("https://navi.omcha.jp", tmp_path / "missing.json", 5, [])
    assert t == ["https://navi.omcha.jp/"]


def test_get_targets_merges_gsc_top_and_extra(tmp_path: pathlib.Path):
    gsc = tmp_path / "gsc.json"
    gsc.write_text(json.dumps({"by_page": [
        {"page": "/products/a/", "impressions": 10},
        {"page": "/products/b/", "impressions": 99},
    ]}), encoding="utf-8")
    t = get_targets("https://navi.omcha.jp", gsc, 3, ["https://navi.omcha.jp/extra/"])
    # impressions 降順 = b が先
    assert t[1] == "https://navi.omcha.jp/products/b/"
    assert "https://navi.omcha.jp/extra/" in t


def test_get_targets_dedupes(tmp_path: pathlib.Path, monkeypatch):
    monkeypatch.setattr(rll, "latest_gsc_history", lambda: None)
    t = get_targets("https://navi.omcha.jp", tmp_path / "x.json", 3,
                    ["https://navi.omcha.jp/", "https://navi.omcha.jp/"])
    assert len(t) == 1


# ---------- argv ----------

def test_build_lighthouse_argv_splits_cmd_and_sets_mobile():
    argv = build_lighthouse_argv("npx --yes lighthouse", "https://x/", "/tmp/o.json", "mobile")
    assert argv[:3] == ["npx", "--yes", "lighthouse"]
    assert "https://x/" in argv
    assert "--form-factor=mobile" in argv
    assert "--output-path=/tmp/o.json" in argv


def test_build_lighthouse_argv_desktop_disables_screen_emulation():
    argv = build_lighthouse_argv("lighthouse", "https://x/", "/tmp/o.json", "desktop")
    assert "--form-factor=desktop" in argv
    assert "--screenEmulation.disabled" in argv


# ---------- report ----------

def test_render_report_separates_errors_from_regressions():
    alerts = [
        {"kind": "error", "url": "https://x/", "form_factor": "mobile",
         "metric": "lcp", "detail": "audit error: NO_LCP (3/3 runs)"},
        {"kind": "threshold", "url": "https://y/", "form_factor": "mobile",
         "metric": "lcp", "value": 4000.0, "baseline": 2000.0, "detail": "d"},
    ]
    md = render_report(alerts, "2026-07-16")
    assert "計測エラー" in md
    assert "パフォーマンス劣化" in md
    assert "NO_LCP" in md
    assert "#2995" in md
