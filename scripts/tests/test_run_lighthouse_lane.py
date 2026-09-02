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
    lcp_is_unmeasured,
    load_history,
    logical_date,
    mad_for,
    missing_dates,
    render_report,
    run_wide_shift,
    upper_fence_for,
    warmup_gates,
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


def _lh13_json(
    observed_lcp=1450.0,
    observed_fcp=1450.0,
    lcp_selector="body#top > main.main > section.home-hero > div.home-hero-lead",
    lh_version="13.4.1",
    throttling="simulate",
    **kw
):
    """LH13 形式 (#4160): observed 値 / lcp-breakdown-insight / configSettings 付き。"""
    lh = _lh_json(**kw)
    lh["lighthouseVersion"] = lh_version
    lh["configSettings"] = {"throttlingMethod": throttling}
    lh["audits"]["metrics"] = {
        "details": {"items": [{
            "observedLargestContentfulPaint": observed_lcp,
            "observedFirstContentfulPaint": observed_fcp,
        }]}
    }
    lh["audits"]["lcp-breakdown-insight"] = {
        "details": {"items": [{"type": "node", "selector": lcp_selector}]}
    }
    return lh


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


# ---------- extract_metrics: observed / lcp_element (#4160) ----------

def test_extract_metrics_reads_observed_and_lcp_element_lh13():
    """LH13 形式: metrics audit の observed 値と lcp-breakdown-insight の selector。"""
    m = extract_metrics(_lh13_json())
    assert m["observed_lcp"] == 1450.0
    assert m["observed_fcp"] == 1450.0
    assert m["lcp_element"] == "body#top > main.main > section.home-hero > div.home-hero-lead"
    assert m["throttling_method"] == "simulate"
    assert m["lh_version"] == "13.4.1"


def test_extract_metrics_lcp_element_legacy_fallback():
    """旧版 largest-contentful-paint-element (lcp-breakdown-insight 無し) にフォールバック。"""
    lh = _lh_json()
    lh["audits"]["largest-contentful-paint-element"] = {
        "details": {"items": [{"node": {"type": "node", "selector": "div.legacy-lcp"}}]}
    }
    m = extract_metrics(lh)
    assert m["lcp_element"] == "div.legacy-lcp"


def test_extract_metrics_lcp_element_reason_none_when_found():
    """selector が取れたときは理由を残さない (None)。"""
    assert extract_metrics(_lh13_json())["lcp_element_reason"] is None


def test_extract_metrics_lcp_element_reason_not_applicable():
    """#4441 実測形: audit はあるが notApplicable で details=null。

    product ページは trace に largestContentfulPaint::Candidate が無く
    (Invalidate のみ)、LH13 はこの形の audit を出す。parser 側の不具合と
    区別できるよう scoreDisplayMode をそのまま理由に残す。
    """
    lh = _lh13_json()
    lh["audits"]["lcp-breakdown-insight"] = {
        "score": None, "scoreDisplayMode": "notApplicable", "details": None,
    }
    m = extract_metrics(lh)
    assert m["lcp_element"] is None
    assert m["lcp_element_reason"] == "notApplicable"


def test_extract_metrics_lcp_element_reason_audit_missing():
    """LH12 以前の JSON (insight audit 自体が無い)。"""
    m = extract_metrics(_lh_json())
    assert m["lcp_element_reason"] == "audit-missing"


def test_extract_metrics_lcp_element_reason_no_node():
    """details はあるのに node が無い = 真に parser を疑うべきケース。"""
    lh = _lh13_json()
    lh["audits"]["lcp-breakdown-insight"] = {
        "scoreDisplayMode": "informative",
        "details": {"items": [{"type": "table", "items": []}]},
    }
    m = extract_metrics(lh)
    assert m["lcp_element"] is None
    assert m["lcp_element_reason"] == "no-node-in-details"


# ---------- extract_metrics: lcp_subparts (#5081 やること2) ----------

def _lh13_with_subparts(rows):
    """lcp-breakdown-insight を「table + node」の実測形にした LH13 JSON。

    2026-08-20 に lighthouse@13.4.0 を navi のハブページへ当てて採取した形
    (details.type == "list" / items[0]=table / items[1]=node)。
    """
    lh = _lh13_json()
    lh["audits"]["lcp-breakdown-insight"] = {
        "scoreDisplayMode": "informative",
        "details": {
            "type": "list",
            "items": [
                {"type": "table",
                 "headings": [{"key": "label"}, {"key": "duration"}],
                 "items": rows},
                {"type": "node", "selector": "body#top > main.main > header > h1"},
            ],
        },
    }
    return lh


def test_extract_metrics_lcp_subparts_text_lcp_has_two_phases():
    """テキストが LCP のときは ttfb / render delay の 2 つしか出ない (実測形)。"""
    lh = _lh13_with_subparts([
        {"subpart": "timeToFirstByte", "label": "Time to first byte", "duration": 47.385},
        {"subpart": "elementRenderDelay", "label": "Element render delay", "duration": 356.852},
    ])
    m = extract_metrics(lh)
    assert m["lcp_subparts"] == {"timeToFirstByte": 47.4, "elementRenderDelay": 356.9}
    assert m["lcp_subparts_reason"] is None


def test_extract_metrics_lcp_subparts_image_lcp_has_four_phases():
    """画像が LCP なら 4 分割になる。固定キーを期待せず出たぶんだけ拾う。"""
    lh = _lh13_with_subparts([
        {"subpart": "timeToFirstByte", "duration": 100.0},
        {"subpart": "resourceLoadDelay", "duration": 50.0},
        {"subpart": "resourceLoadDuration", "duration": 200.0},
        {"subpart": "elementRenderDelay", "duration": 30.0},
    ])
    assert extract_metrics(lh)["lcp_subparts"] == {
        "timeToFirstByte": 100.0, "resourceLoadDelay": 50.0,
        "resourceLoadDuration": 200.0, "elementRenderDelay": 30.0,
    }


def test_extract_metrics_lcp_subparts_shares_reason_with_element():
    """details が null の行では element と subparts が同じ理由で同時に落ちる。

    商品ページの実測形 (#4441)。独立した 2 つの欠測に見えないよう語彙を揃える。
    """
    lh = _lh13_json()
    lh["audits"]["lcp-breakdown-insight"] = {
        "score": None, "scoreDisplayMode": "notApplicable", "details": None,
    }
    m = extract_metrics(lh)
    assert m["lcp_subparts"] is None
    assert m["lcp_subparts_reason"] == "notApplicable"
    assert m["lcp_element_reason"] == "notApplicable"


def test_extract_metrics_lcp_subparts_audit_missing_on_lh12():
    """LH12 以前は insight audit 自体が無い。"""
    m = extract_metrics(_lh_json())
    assert m["lcp_subparts"] is None
    assert m["lcp_subparts_reason"] == "audit-missing"


def test_extract_metrics_lcp_subparts_node_only_details():
    """node はあるが table が無い = subparts だけ独自の理由になる。"""
    m = extract_metrics(_lh13_json())
    assert m["lcp_element"] is not None
    assert m["lcp_subparts"] is None
    assert m["lcp_subparts_reason"] == "no-subparts-in-details"


def test_extract_metrics_observed_and_element_default_none():
    """LH13 拡張 audit が無い旧版そのままの JSON では None のまま。"""
    m = extract_metrics(_lh_json())
    assert m["observed_lcp"] is None
    assert m["observed_fcp"] is None
    assert m["lcp_element"] is None
    assert m["throttling_method"] is None
    assert m["lh_version"] is None


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


def test_aggregate_runs_takes_median_of_observed():
    runs = [extract_metrics(_lh13_json(observed_lcp=v)) for v in (1400.0, 1500.0, 1600.0)]
    agg = aggregate_runs(runs)
    assert agg["observed_lcp"] == 1500.0


def test_aggregate_runs_uses_first_run_value_for_element_and_version():
    runs = [extract_metrics(_lh13_json()), extract_metrics(_lh13_json())]
    agg = aggregate_runs(runs)
    assert agg["lcp_element"] == "body#top > main.main > section.home-hero > div.home-hero-lead"
    assert agg["throttling_method"] == "simulate"
    assert agg["lh_version"] == "13.4.1"


def test_aggregate_runs_carries_lcp_element_reason(caplog):
    """selector が全 run で取れない場合、理由は集計行にも残る (#4441)。"""
    lh = _lh13_json()
    lh["audits"]["lcp-breakdown-insight"] = {
        "scoreDisplayMode": "notApplicable", "details": None,
    }
    agg = aggregate_runs([extract_metrics(lh), extract_metrics(lh)])
    assert agg["lcp_element"] is None
    assert agg["lcp_element_reason"] == "notApplicable"


def test_aggregate_runs_takes_median_per_subpart():
    """subpart ごとに median を採る (run 間で内訳が割れるため)。"""
    runs = [
        extract_metrics(_lh13_with_subparts([{"subpart": "timeToFirstByte", "duration": v}]))
        for v in (100.0, 300.0, 200.0)
    ]
    agg = aggregate_runs(runs)
    assert agg["lcp_subparts"] == {"timeToFirstByte": 200.0}
    assert agg["lcp_subparts_runs"] == 3
    assert agg["lcp_subparts_reason"] is None


def test_aggregate_runs_keeps_subparts_when_only_one_run_has_them():
    """1 回でも取れたら取れている扱い (lcp_element と同じ排他)。"""
    ok = extract_metrics(_lh13_with_subparts([{"subpart": "timeToFirstByte", "duration": 50.0}]))
    lh = _lh13_json()
    lh["audits"]["lcp-breakdown-insight"] = {"scoreDisplayMode": "notApplicable", "details": None}
    agg = aggregate_runs([extract_metrics(lh), ok, extract_metrics(lh)])
    assert agg["lcp_subparts"] == {"timeToFirstByte": 50.0}
    assert agg["lcp_subparts_runs"] == 1
    assert agg["lcp_subparts_reason"] is None


def test_aggregate_runs_carries_subparts_reason_when_never_available():
    """全 run で取れなければ理由を集計行に残す。"""
    lh = _lh13_json()
    lh["audits"]["lcp-breakdown-insight"] = {"scoreDisplayMode": "notApplicable", "details": None}
    agg = aggregate_runs([extract_metrics(lh), extract_metrics(lh)])
    assert agg["lcp_subparts"] is None
    assert agg["lcp_subparts_reason"] == "notApplicable"


def test_aggregate_runs_warns_when_element_differs_across_runs(caplog):
    """run 間で selector が割れたら最初の run 値を採るが、追跡用に warning を残す。"""
    runs = [
        extract_metrics(_lh13_json(lcp_selector="div.a")),
        extract_metrics(_lh13_json(lcp_selector="div.b")),
    ]
    with caplog.at_level("WARNING"):
        agg = aggregate_runs(runs)
    assert agg["lcp_element"] == "div.a"
    assert "lcp_element" in caplog.text


def _lh13_no_candidate():
    """LCP 候補が確定しなかった run (element=None / reason=notApplicable)。"""
    lh = _lh13_json()
    lh["audits"]["lcp-breakdown-insight"] = {
        "scoreDisplayMode": "notApplicable", "details": None,
    }
    return lh


def test_aggregate_runs_keeps_element_and_reason_exclusive(caplog):
    """間欠取得でも element と reason は排他のまま (#5081 項目2)。

    per-run の不変条件 (片方が入れば他方は None) を集約でも保つ。2 キーを独立に
    畳んでいた頃は両方入った行ができ、lcp_is_unmeasured() が reason だけを見る
    せいで「3 回中 2 回は LCP が取れていた行」が無音で LCP ゲートから外れていた。
    """
    runs = [
        extract_metrics(_lh13_no_candidate()),
        extract_metrics(_lh13_json()),
        extract_metrics(_lh13_no_candidate()),
    ]
    with caplog.at_level("WARNING"):
        agg = aggregate_runs(runs)
    assert agg["lcp_element"] == "body#top > main.main > section.home-hero > div.home-hero-lead"
    assert agg["lcp_element_reason"] is None
    # 3 回中 1 回しか取れていないことが JSONL から追える
    assert agg["lcp_element_runs"] == 1
    assert agg["runs"] == 3
    # element が 1 種類しか無いので selector 割れの警告は出さない
    assert "differs across runs" not in caplog.text
    # 集約行が LCP ゲートの対象に残る
    assert lcp_is_unmeasured(agg) is False


def test_aggregate_runs_all_runs_without_candidate_stay_unmeasured():
    """全 run で候補が取れなければ従来どおり reason を残しゲートから外す。"""
    agg = aggregate_runs([extract_metrics(_lh13_no_candidate()) for _ in range(3)])
    assert agg["lcp_element"] is None
    assert agg["lcp_element_reason"] == "notApplicable"
    assert "lcp_element_runs" not in agg
    assert lcp_is_unmeasured(agg) is True


def test_aggregate_runs_records_element_runs_when_all_runs_have_it():
    agg = aggregate_runs([extract_metrics(_lh13_json()) for _ in range(3)])
    assert agg["lcp_element_runs"] == 3
    assert agg["lcp_element_reason"] is None


def test_aggregate_runs_warns_when_reason_differs_across_runs(caplog):
    """候補ゼロでも理由が run 間で割れたら追跡用に warning を残す。"""
    missing = _lh13_json()
    del missing["audits"]["lcp-breakdown-insight"]
    runs = [extract_metrics(_lh13_no_candidate()), extract_metrics(missing)]
    with caplog.at_level("WARNING"):
        agg = aggregate_runs(runs)
    assert agg["lcp_element_reason"] == "notApplicable"
    assert "lcp_element_reason differs across runs" in caplog.text


# ---------- detect_regressions ----------

def _row(url="https://x/", ff="mobile", **kw):
    base = {"date": "2026-07-16", "url": url, "form_factor": ff, "runs": 3}
    base.update(kw)
    return base


def test_detect_regressions_quiet_when_stable():
    # MIN_BASELINE_SAMPLES が 3→7 (#4160) になったため 7 件そろえる
    history = [_row(lcp=2000.0, perf_score=95.0) for _ in range(7)]
    current = [_row(lcp=2050.0, perf_score=95.0)]
    assert detect_regressions(history, current) == []


def test_detect_regressions_flags_threshold_crossing():
    """good 閾値 (LCP 2500) を跨いだら鳴る。"""
    history = [_row(lcp=2000.0) for _ in range(7)]  # MIN_BASELINE_SAMPLES=7 (#4160)
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
    """元から閾値超えでも、微小な揺れでは鳴らさない (誤検出で issue を湧かせない)。

    baseline が全部同値 (MAD=0) のケースでもあり、MIN_ABS_DELTA が下限として
    効くことの確認も兼ねる (#4160)。
    """
    history = [_row(lcp=5000.0) for _ in range(7)]  # MIN_BASELINE_SAMPLES=7
    current = [_row(lcp=5100.0)]
    assert detect_regressions(history, current) == []


def test_detect_regressions_flags_relative_regression_when_already_bad():
    """元から悪くても、baseline 比で大きく悪化したら鳴る。"""
    history = [_row(lcp=5000.0) for _ in range(7)]  # MIN_BASELINE_SAMPLES=7
    current = [_row(lcp=8000.0)]
    alerts = detect_regressions(history, current)
    assert any(a["kind"] == "relative" for a in alerts)


def test_detect_regressions_first_run_is_silent_even_when_bad():
    """baseline が無い初回計測は、絶対値が悪くても鳴らさない。

    商品ページの mobile LCP 5.4s は既知の遅さであって回帰ではない。GSC 週次上位の
    入れ替わりで新規 URL が入るたびに一斉に鳴っていたのを止める (#4034)。
    """
    assert detect_regressions([], [_row(lcp=6000.0)]) == []


def test_detect_regressions_first_run_quiet_when_good():
    assert detect_regressions([], [_row(lcp=1500.0)]) == []


def test_detect_regressions_silent_while_baseline_too_thin():
    """履歴 2 件だけの URL では、大きく振れても比較を始めない。"""
    history = [_row(lcp=2500.0), _row(lcp=2600.0)]
    current = [_row(lcp=6000.0)]
    assert detect_regressions(history, current) == []


def test_detect_regressions_starts_alerting_once_baseline_is_thick_enough():
    """MIN_BASELINE_SAMPLES (=7, #4160) 件そろえば通常どおり鳴る (抑制が恒久化しない)。"""
    history = [_row(lcp=v) for v in (2500.0, 2600.0, 2550.0, 2500.0, 2600.0, 2550.0, 2500.0)]
    current = [_row(lcp=6000.0)]
    alerts = detect_regressions(history, current)
    assert any(a["kind"] == "relative" for a in alerts)


def test_detect_regressions_score_drop_needs_thick_baseline():
    """perf score 側も薄い baseline では鳴らない。"""
    history = [_row(lcp=2000.0, perf_score=95.0)]
    current = [_row(lcp=2000.0, perf_score=70.0)]
    assert detect_regressions(history, current) == []


def test_detect_regressions_flags_score_drop():
    history = [_row(lcp=2000.0, perf_score=95.0) for _ in range(7)]  # MIN_BASELINE_SAMPLES=7
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


# ---------- detect_regressions: MAD 分散ゲート / observed 裏取り (#4160) ----------

def test_detect_regressions_no_alert_for_replayed_home_series_4160():
    """#4160 の実測回帰テスト本体。

    navi ホーム (mobile) の実履歴 (2026-07-16〜26 の JSONL から抽出、simulated
    LCP, ms) を baseline として replay する。旧ゲート (window=5,
    MIN_BASELINE_SAMPLES=3, ratio のみ) では baseline(median)=3549.2 に対し
    current=4530.5 で relative 発火していたが、この系列は stdev=957 相当と
    分散が大きく、新ゲート (MAD ベース) では鳴らないことを確認する。
    """
    lcps = [2857.1, 4871.1, 4500.9, 4486.3, 3549.2, 4942.6, 2665.4, 5007.9, 2714.2]
    history = [_row(lcp=v) for v in lcps]
    current = [_row(lcp=4530.5)]
    alerts = detect_regressions(history, current)
    assert not any(a["kind"] == "relative" and a["metric"] == "lcp" for a in alerts)


def test_detect_regressions_still_flags_real_regression_despite_dispersion():
    """同じ分散の大きい系列でも、本物の劣化 (9000ms) は検出できる。"""
    lcps = [2857.1, 4871.1, 4500.9, 4486.3, 3549.2, 4942.6, 2665.4, 5007.9, 2714.2]
    history = [_row(lcp=v) for v in lcps]
    current = [_row(lcp=9000.0)]
    alerts = detect_regressions(history, current)
    assert any(a["metric"] == "lcp" for a in alerts)


def test_detect_regressions_lcp_simulated_only_worse_stays_quiet_when_observed_flat():
    """simulated だけ悪化・observed が横ばいなら observed 側が裏取りできず鳴らない。"""
    history = [
        _row(lcp=v, observed_lcp=1450.0)
        for v in (2900.0, 2950.0, 3000.0, 3000.0, 3050.0, 3100.0, 3100.0)
    ]
    current = [_row(lcp=6000.0, observed_lcp=1460.0)]
    alerts = detect_regressions(history, current)
    assert not any(a["metric"] == "lcp" for a in alerts)


def test_detect_regressions_lcp_fires_when_observed_also_worse():
    """simulated と observed の両方が悪化していれば鳴る。"""
    history = [
        _row(lcp=v, observed_lcp=1450.0)
        for v in (2900.0, 2950.0, 3000.0, 3000.0, 3050.0, 3100.0, 3100.0)
    ]
    current = [_row(lcp=6000.0, observed_lcp=3000.0)]
    alerts = detect_regressions(history, current)
    assert any(a["metric"] == "lcp" for a in alerts)


def test_detect_regressions_lcp_degrades_to_simulated_only_without_observed_history():
    """observed の履歴がまだ無い期間は、従来どおり simulated だけで判定する (degrade)。"""
    history = [_row(lcp=v) for v in (2900.0, 2950.0, 3000.0, 3000.0, 3050.0, 3100.0, 3100.0)]
    current = [_row(lcp=6000.0)]  # observed_lcp キー自体が無い
    alerts = detect_regressions(history, current)
    assert any(a["metric"] == "lcp" for a in alerts)


# ---------- mad_for ----------

def test_mad_for_zero_for_constant_series():
    history = [_row(lcp=2000.0) for _ in range(7)]
    assert mad_for(history, "https://x/", "mobile", "lcp", window=10) == 0.0


def test_mad_for_none_below_min_samples():
    history = [_row(lcp=2000.0), _row(lcp=2100.0)]
    assert mad_for(history, "https://x/", "mobile", "lcp", window=10) is None


# ---------- baseline_for ----------

def test_baseline_for_uses_window_and_skips_nulls():
    # MIN_BASELINE_SAMPLES が 3→7 (#4160) になったため有効値 7 件そろえる
    history = [
        _row(lcp=1000.0), _row(lcp=None), _row(lcp=2000.0), _row(lcp=3000.0),
        _row(lcp=4000.0), _row(lcp=5000.0), _row(lcp=6000.0), _row(lcp=7000.0),
    ]
    assert baseline_for(history, "https://x/", "mobile", "lcp", window=8) == 4000.0


def test_baseline_for_returns_none_without_data():
    assert baseline_for([], "https://x/", "mobile", "lcp", window=5) is None


def test_baseline_for_returns_none_below_min_samples():
    history = [_row(lcp=1000.0), _row(lcp=2000.0)]
    assert baseline_for(history, "https://x/", "mobile", "lcp", window=5) is None


def test_baseline_for_min_samples_is_overridable():
    history = [_row(lcp=1000.0), _row(lcp=2000.0)]
    assert baseline_for(
        history, "https://x/", "mobile", "lcp", window=5, min_samples=2
    ) == 1500.0


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


# ---------- chrome_version (#4583) ----------

def test_extract_metrics_reads_chrome_version():
    lh = _lh13_json()
    lh["environment"] = {"hostUserAgent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "HeadlessChrome/151.0.0.0 Safari/537.36"
    )}
    assert extract_metrics(lh)["chrome_version"] == "151.0.0.0"


def test_extract_metrics_reads_chrome_version_non_headless():
    lh = _lh13_json()
    lh["environment"] = {"hostUserAgent": "Mozilla/5.0 Chrome/150.0.7871.128 Safari/537.36"}
    assert extract_metrics(lh)["chrome_version"] == "150.0.7871.128"


def test_extract_metrics_chrome_version_none_when_absent():
    """environment が無い過去 JSON でも落ちない (列は None で埋める)。"""
    assert extract_metrics(_lh13_json())["chrome_version"] is None


def test_aggregate_runs_carries_chrome_version():
    lh = _lh13_json()
    lh["environment"] = {"hostUserAgent": "HeadlessChrome/151.0.0.0"}
    agg = aggregate_runs([extract_metrics(lh), extract_metrics(lh)])
    assert agg["chrome_version"] == "151.0.0.0"


# ---------- LCP ゲートの除外 (#4441) ----------

def test_lcp_is_unmeasured_flags_not_applicable():
    assert lcp_is_unmeasured(_row(lcp_element_reason="notApplicable")) is True


def test_lcp_is_unmeasured_ignores_parser_case_and_missing_key():
    """details がある (= 実 Candidate があった) 行と、reason 列が無い過去行は除外しない。"""
    assert lcp_is_unmeasured(_row(lcp_element_reason="no-node-in-details")) is False
    assert lcp_is_unmeasured(_row(lcp_element_reason="audit-missing")) is False
    assert lcp_is_unmeasured(_row()) is False


def test_lcp_is_unmeasured_false_when_element_present():
    """element が入っていれば reason が残っていても計測済み扱い (#5081 項目2)。

    2026-08-07〜08-15 の集約バグで両方入った行が JSONL に残っているため、履歴を
    書き換えずに読み出し側で救う。
    """
    row = _row(lcp_element="div.hero", lcp_element_reason="notApplicable")
    assert lcp_is_unmeasured(row) is False


def test_detect_regressions_skips_lcp_when_unmeasured():
    """LCP 候補が確定していない行の lcp は合成値なので比較しない (#4441)。

    baseline 2000 → 8000 は本来 threshold + relative の両方を踏む値。
    """
    history = [_row(lcp=2000.0, lcp_element_reason="notApplicable") for _ in range(7)]
    current = [_row(lcp=8000.0, lcp_element_reason="notApplicable")]
    assert [a for a in detect_regressions(history, current) if a["metric"] == "lcp"] == []


def test_detect_regressions_still_flags_lcp_when_measured():
    """同じ悪化でも、LCP 要素が取れている行は従来どおり鳴る (除外が効きすぎない)。"""
    history = [_row(lcp=2000.0, lcp_element_reason=None) for _ in range(7)]
    current = [_row(lcp=8000.0, lcp_element_reason=None)]
    alerts = detect_regressions(history, current)
    assert any(a["metric"] == "lcp" for a in alerts)


def test_detect_regressions_unmeasured_lcp_does_not_mute_other_metrics():
    """除外するのは lcp アームだけ。tbt などは同じ行でも従来どおり鳴る。

    2026-08-05 の #4583 が tbt 131→398 で鳴った実例に相当する。
    """
    history = [_row(lcp=2000.0, tbt=130.0, lcp_element_reason="notApplicable")
               for _ in range(7)]
    current = [_row(lcp=8000.0, tbt=398.0, lcp_element_reason="notApplicable")]
    metrics = {a["metric"] for a in detect_regressions(history, current)}
    assert "tbt" in metrics
    assert "lcp" not in metrics


def test_detect_regressions_unmeasured_lcp_still_reports_audit_error():
    """kind="error" は計測基盤の失敗検出なので除外の対象外。"""
    history = [_row(lcp=2000.0, lcp_element_reason="notApplicable") for _ in range(7)]
    current = [_row(lcp=None, lcp_error="NO_LCP", lcp_error_runs=3,
                    lcp_element_reason="notApplicable")]
    alerts = detect_regressions(history, current)
    assert any(a["kind"] == "error" and a["metric"] == "lcp" for a in alerts)


def test_detect_regressions_logs_when_lcp_gate_skipped(caplog):
    """無音で消さない: 除外したことを必ずログに残す。"""
    history = [_row(lcp=2000.0, lcp_element_reason="notApplicable") for _ in range(7)]
    current = [_row(lcp=8000.0, lcp_element_reason="notApplicable")]
    with caplog.at_level("WARNING"):
        detect_regressions(history, current)
    assert "lcp gate skipped" in caplog.text


def test_render_report_notes_unmeasured_lcp_urls():
    alerts = [
        {"kind": "relative", "url": "https://x/", "form_factor": "mobile",
         "metric": "tbt", "value": 398.0, "baseline": 130.5, "detail": "d"},
    ]
    md = render_report(alerts, "2026-08-05", [
        {"url": "https://x/", "form_factor": "mobile",
         "lcp_element_reason": "notApplicable"},
    ])
    assert "LCP を判定していない URL" in md
    assert "notApplicable" in md


def test_render_report_omits_unmeasured_section_when_empty():
    alerts = [
        {"kind": "threshold", "url": "https://y/", "form_factor": "mobile",
         "metric": "lcp", "value": 4000.0, "baseline": 2000.0, "detail": "d"},
    ]
    assert "LCP を判定していない URL" not in render_report(alerts, "2026-08-05")


# ---------- chrome_version による baseline 分割 (#4765) ----------

def test_baseline_for_segments_by_chrome_version():
    """同じ Chrome major で測った行だけを baseline に使う。"""
    history = [_row(si=2800.0, chrome_version="150.0.0.0") for _ in range(7)]
    history += [_row(si=4400.0, chrome_version="151.0.0.0") for _ in range(7)]
    assert baseline_for(history, "https://x/", "mobile", "si", 10,
                        chrome_version="151.0.0.0") == 4400.0
    assert baseline_for(history, "https://x/", "mobile", "si", 10,
                        chrome_version="150.0.0.0") == 2800.0


def test_baseline_for_returns_none_right_after_chrome_bump():
    """版が変わった直後は同版のサンプルが薄いので判定を見送る。"""
    history = [_row(si=2800.0, chrome_version="150.0.0.0") for _ in range(20)]
    history += [_row(si=4400.0, chrome_version="151.0.0.0")]
    assert baseline_for(history, "https://x/", "mobile", "si", 10,
                        chrome_version="151.0.0.0") is None


def test_baseline_for_unsegmented_when_chrome_version_is_none():
    """chrome_version を渡さなければ従来どおり版を跨いで集める (degrade)。"""
    history = [_row(si=2800.0, chrome_version="150.0.0.0") for _ in range(7)]
    assert baseline_for(history, "https://x/", "mobile", "si", 10) == 2800.0


def test_mad_for_segments_by_chrome_version():
    history = [_row(si=2800.0, chrome_version="150.0.0.0") for _ in range(7)]
    history += [_row(si=4400.0, chrome_version="151.0.0.0") for _ in range(7)]
    assert mad_for(history, "https://x/", "mobile", "si", 10,
                   chrome_version="151.0.0.0") == 0.0


def test_detect_is_quiet_across_chrome_major_bump():
    """#4652 の機構: Chrome 版が上がった当日の一斉悪化では鳴らさない。

    2026-08-07 に mobile SI が全 11 URL 同時に 2849→4421 と跳ねたが、hugo の
    layouts/assets/static には一切変更が無く、動いていたのは Chrome 150→151
    だけだった。版を跨いだ baseline と比べる限りこれは必ず鳴る。
    """
    history = [_row(si=2800.0, perf_score=78.0, chrome_version="150.0.0.0")
               for _ in range(10)]
    current = [_row(si=4400.0, perf_score=70.0, chrome_version="151.0.0.0")]
    assert detect_regressions(history, current) == []


def test_detect_still_flags_regression_within_same_chrome_version():
    """版を分けても、同じ版の中で起きた本物の劣化は従来どおり鳴る。"""
    history = [_row(si=2800.0, perf_score=78.0, chrome_version="151.0.0.0")
               for _ in range(10)]
    current = [_row(si=4400.0, perf_score=70.0, chrome_version="151.0.0.0")]
    kinds = {a["kind"] for a in detect_regressions(history, current)}
    assert kinds  # 何かしら鳴る
    assert "threshold" in kinds or "relative" in kinds


def test_detect_degrades_when_history_has_no_chrome_version():
    """chrome_version 導入前の行しか無い期間は従来挙動を維持する。"""
    history = [_row(si=2800.0, perf_score=78.0) for _ in range(10)]
    current = [_row(si=4400.0, perf_score=70.0)]
    assert detect_regressions(history, current) != []


# ---------- 同日再計測の後勝ち (#4765 / #4652) ----------

def test_append_records_replaces_same_date_url_form_factor(tmp_path):
    """同じ日にレーンが 2 回走っても 1 行しか残さない (後勝ち)。"""
    p = tmp_path / LIGHTHOUSE_HISTORY_FILENAME
    append_records(p, [_row(date="2026-08-07", si=2830.0)])
    append_records(p, [_row(date="2026-08-07", si=4430.0)])
    rows = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(rows) == 1
    assert rows[0]["si"] == 4430.0


def test_append_records_keeps_other_dates_and_form_factors(tmp_path):
    p = tmp_path / LIGHTHOUSE_HISTORY_FILENAME
    append_records(p, [_row(date="2026-08-06", si=2800.0),
                       _row(date="2026-08-07", ff="desktop", si=1200.0)])
    append_records(p, [_row(date="2026-08-07", si=4430.0)])
    rows = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    keys = {(r["date"], r["form_factor"]) for r in rows}
    assert keys == {("2026-08-06", "mobile"), ("2026-08-07", "desktop"), ("2026-08-07", "mobile")}


def test_append_records_is_plain_append_for_new_keys(tmp_path):
    p = tmp_path / LIGHTHOUSE_HISTORY_FILENAME
    assert append_records(p, [_row(date="2026-08-06", si=2800.0)]) == 1
    assert append_records(p, [_row(date="2026-08-07", si=2810.0)]) == 1
    rows = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert [r["date"] for r in rows] == ["2026-08-06", "2026-08-07"]


# --- 論理日と欠測検出 (#4785) ------------------------------------------
# 実測: home-ops の cron は 21:40 UTC だが schedule dispatch が恒常的に 50-60 分
# 遅れ、2026-08-07 の run は 01:04 UTC に起動した。date.today() だと 08-06 が
# 丸ごと欠測して 08-07 が 2 バッチになる (#4652 の残り半分)。

def _utc(s):
    from datetime import datetime, timezone
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


@pytest.mark.parametrize("started,expected", [
    # 平常運転: 22:32-22:42 UTC の起動はその日に落ちる
    ("2026-08-08T22:38:34", "2026-08-08"),
    ("2026-08-08T21:40:00", "2026-08-08"),
    # 遅延して真夜中 UTC を越えた run も前日に戻る (これが 08-06 欠測の再現)
    ("2026-08-07T01:04:05", "2026-08-06"),
    ("2026-08-07T05:59:59", "2026-08-06"),
    # 境界: 06:00 UTC 以降は当日
    ("2026-08-07T06:00:00", "2026-08-07"),
])
def test_logical_date_absorbs_schedule_delay(started, expected):
    assert logical_date(_utc(started), 6) == expected


def test_logical_date_offset_zero_is_wall_clock():
    assert logical_date(_utc("2026-08-07T01:04:05"), 0) == "2026-08-07"


def test_logical_date_treats_naive_as_utc():
    from datetime import datetime
    assert logical_date(datetime(2026, 8, 7, 1, 4, 5), 6) == "2026-08-06"


def test_logical_date_normalizes_other_timezone():
    from datetime import datetime, timedelta, timezone
    jst = timezone(timedelta(hours=9))
    # JST 10:04 on 08-07 = 01:04 UTC → 論理日は 08-06
    assert logical_date(datetime(2026, 8, 7, 10, 4, 5, tzinfo=jst), 6) == "2026-08-06"


def test_missing_dates_detects_skipped_day():
    hist = [{"date": "2026-08-05"}, {"date": "2026-08-05"}]
    assert missing_dates(hist, "2026-08-07") == ["2026-08-06"]


def test_missing_dates_empty_on_consecutive_day():
    assert missing_dates([{"date": "2026-08-08"}], "2026-08-09") == []


def test_missing_dates_empty_on_remeasure_of_same_day():
    # 同日再計測 (#4772 の後勝ち経路) を欠測扱いしない
    assert missing_dates([{"date": "2026-08-08"}], "2026-08-08") == []


def test_missing_dates_empty_when_target_is_older():
    assert missing_dates([{"date": "2026-08-08"}], "2026-08-01") == []


def test_missing_dates_empty_history_is_not_a_gap():
    # 初回投入で 30 日分の警告が出ないこと
    assert missing_dates([], "2026-08-09") == []


def test_missing_dates_ignores_malformed_rows():
    hist = [{"date": "not-a-date"}, {"date": None}, {}, {"date": "2026-08-05"}]
    assert missing_dates(hist, "2026-08-07") == ["2026-08-06"]


def test_missing_dates_capped():
    gap = missing_dates([{"date": "2026-01-01"}], "2026-08-09", cap=5)
    assert len(gap) == 5
    assert gap[0] == "2026-01-02"


def test_missing_dates_invalid_target_is_not_a_gap():
    assert missing_dates([{"date": "2026-08-05"}], "garbage") == []


# ---------- #5264: 二峰性に対する分位ゲート ----------

# #5259 の実データ。ホーム (mobile) TBT の baseline 窓 (同一 Chrome major の 7 件)。
# median=51.0 だが MAD=6.5 しかなく、窓の中に 231.5 / 272.5 がある。
_BIMODAL_TBT = [85.0, 51.0, 44.5, 48.0, 231.5, 46.5, 272.5]


def test_upper_fence_reflects_recurring_high_cluster_unlike_mad():
    """MAD は多数派クラスタの幅しか測らないが、fence は再発クラスタを織り込む。"""
    history = [_row(tbt=v) for v in _BIMODAL_TBT]
    mad = mad_for(history, "https://x/", "mobile", "tbt", window=10)
    fence = upper_fence_for(history, "https://x/", "mobile", "tbt", window=10)
    assert mad < 10           # 231.5 / 272.5 を無視している
    assert fence > 150        # fence は窓の実測レンジを反映する


def test_no_alert_when_value_is_inside_baseline_spread_5259():
    """#5259 の再現: baseline 51.0 に対する 150 は窓 (44.5〜272.5) の内側。鳴らさない。"""
    history = [_row(tbt=v) for v in _BIMODAL_TBT]
    alerts = detect_regressions(history, [_row(tbt=150.0)])
    assert not any(a["metric"] == "tbt" for a in alerts)


def test_threshold_arm_is_also_gated_by_fence():
    """relative だけ塞ぐと、同じ日に good 閾値跨ぎ (threshold) で鳴り直してしまう。"""
    history = [_row(tbt=v) for v in _BIMODAL_TBT]
    # good 閾値 200 を跨ぐが、窓の中で既に起きている振れ幅の内側。
    alerts = detect_regressions(history, [_row(tbt=240.0)])
    assert not any(a["metric"] == "tbt" for a in alerts)


def test_large_excursion_beyond_the_spread_still_fires():
    """窓の振れ幅を超える悪化は従来どおり鳴る (2026-08-05 の商品 398 相当)。"""
    history = [_row(tbt=v) for v in _BIMODAL_TBT]
    alerts = detect_regressions(history, [_row(tbt=900.0)])
    assert any(a["metric"] == "tbt" for a in alerts)


def test_fence_does_not_dull_gate_on_tight_series():
    """分布が締まっている系列では fence も締まるので、小さめの劣化でも鳴る。"""
    history = [_row(tbt=v) for v in (48.0, 50.0, 49.0, 51.0, 50.0, 49.5, 50.5)]
    alerts = detect_regressions(history, [_row(tbt=110.0)])
    assert any(a["metric"] == "tbt" for a in alerts)


# ---------- #5264: ウォームアップの可視化 ----------

def test_warmup_gates_lists_metrics_without_enough_baseline():
    history = [_row(tbt=50.0) for _ in range(3)]  # MIN_BASELINE_SAMPLES=7 に届かない
    out = warmup_gates(history, [_row(tbt=50.0)])
    assert [w["metric"] for w in out] == ["tbt"]
    assert out[0]["samples"] == 3 and out[0]["needed"] == rll.MIN_BASELINE_SAMPLES


def test_warmup_gates_empty_when_baseline_is_grown():
    history = [_row(tbt=50.0) for _ in range(7)]
    assert warmup_gates(history, [_row(tbt=50.0)]) == []


def test_warmup_gates_counts_chrome_major_bump_as_warm_up():
    """Chrome major が変わると baseline がリセットされ、再び沈黙する (#4765)。"""
    history = [_row(tbt=50.0, chrome_version="150.0.0.0") for _ in range(10)]
    out = warmup_gates(history, [_row(tbt=50.0, chrome_version="151.0.0.0")])
    assert [w["metric"] for w in out] == ["tbt"]
    assert out[0]["samples"] == 0


def test_render_report_shows_warmup_section():
    alerts = [{"kind": "relative", "url": "https://x/", "form_factor": "mobile",
               "metric": "tbt", "value": 900, "baseline": 50, "detail": "x"}]
    md = render_report(alerts, "2026-08-14", None,
                       [{"url": "https://x/", "form_factor": "mobile", "metric": "lcp",
                         "samples": 2, "needed": 7, "chrome_version": "151.0.0.0"}])
    assert "baseline が育っておらず判定を見送っている項目 (1 件)" in md
    assert "健全" in md  # 「鳴っていない = 健全」とは読めない、の注記


# ---------- #5264: run 間のばらつきを残す ----------

def test_aggregate_runs_records_spread_between_runs():
    runs = [
        {"tbt": 40.0, "lcp": 2000.0, "cls": 0.01, "fcp": 1000.0, "si": 1500.0},
        {"tbt": 300.0, "lcp": 2100.0, "cls": 0.02, "fcp": 1010.0, "si": 1600.0},
        {"tbt": 50.0, "lcp": 2050.0, "cls": 0.015, "fcp": 1005.0, "si": 1550.0},
    ]
    out = aggregate_runs(runs)
    assert out["tbt"] == 50.0            # median は従来どおり
    assert out["tbt_spread"] == 260.0    # 「1 回だけ暴れた」が後から分かる
    assert out["cls_spread"] == 0.01


def test_aggregate_runs_has_no_spread_when_metric_is_missing():
    out = aggregate_runs([{"tbt": None}, {"tbt": None}])
    assert out["tbt"] is None
    assert "tbt_spread" not in out


# ---------- #5320: run 全体汚染ガード ----------

# 2026-08-15 の形を縮めたもの: 6 URL の SI が一斉に ~1.6x 沈む。
_RUN_URLS = ["https://x/a/", "https://x/b/", "https://x/c/",
             "https://x/d/", "https://x/e/", "https://x/f/"]


def _stable_history(metric, value, urls=_RUN_URLS, n=7):
    return [_row(url=u, **{metric: value}) for u in urls for _ in range(n)]


def _run(metric, values, urls=_RUN_URLS):
    return [_row(url=u, **{metric: v}) for u, v in zip(urls, values)]


def test_run_wide_shift_detects_uniform_slowdown():
    """全 URL が同じ幅で沈んだ run は計測環境由来と判定する。"""
    history = _stable_history("si", 2800.0)
    shifted = run_wide_shift(history, _run("si", [4700.0] * 6))
    assert "si" in shifted
    assert shifted["si"]["median_ratio"] == pytest.approx(4700.0 / 2800.0, rel=1e-3)
    assert shifted["si"]["over"] == 6 and shifted["si"]["samples"] == 6


def test_run_wide_shift_ignores_single_url_regression():
    """1 本だけ沈んだ run は汚染ではない (= 本物の劣化として通す)。"""
    history = _stable_history("si", 2800.0)
    assert run_wide_shift(history, _run("si", [4700.0] + [2800.0] * 5)) == {}


def test_run_wide_shift_needs_both_median_and_fraction():
    """中央比は届いていても、1.25x 超が半数に満たなければ汚染としない。

    #5264 の TBT の裾 (2026-07-24 は run 中央比 1.01 なのに 2/5 が 1.25x 超) を
    汚染と誤判定しないための条件。
    """
    history = _stable_history("tbt", 50.0)
    # 3/6 が大きく飛ぶが中央比は 1.0 付近 = (URL x 日) 単位の裾
    assert run_wide_shift(history, _run("tbt", [300.0, 50.0, 300.0, 50.0, 300.0, 50.0]))
    assert run_wide_shift(history, _run("tbt", [300.0, 50.0, 50.0, 50.0, 50.0, 50.0])) == {}


def test_run_wide_shift_ignores_uniform_improvement():
    """全 URL が一斉に速くなった run は疑わない (片側だけ見る)。"""
    history = _stable_history("si", 4000.0)
    assert run_wide_shift(history, _run("si", [1500.0] * 6)) == {}


def test_run_wide_shift_needs_minimum_samples():
    """baseline を持つ URL が少なすぎる run では run 単位の統計を作らない。"""
    urls = _RUN_URLS[:3]
    history = _stable_history("si", 2800.0, urls=urls)
    assert run_wide_shift(history, _run("si", [4700.0] * 3, urls=urls)) == {}


def test_contaminated_run_suppresses_alerts_5320():
    """#5320 の再現: 全 URL の SI が一斉に沈んだ run では SI で鳴らさない。"""
    history = _stable_history("si", 2800.0)
    alerts = detect_regressions(history, _run("si", [4700.0] * 6))
    assert not any(a["metric"] == "si" for a in alerts)


def test_contaminated_run_still_fires_on_uncontaminated_metric():
    """汚染は metric ごとに独立。SI が汚染されていても TBT の本物は残る。

    2026-08-15 の実データがこの形で、SI/FCP は run 全体が沈んでいたが TBT の
    run 中央比は 0.90 だった (= TBT の 1 件は本物として残すべき)。
    """
    history = [_row(url=u, si=2800.0, tbt=50.0) for u in _RUN_URLS for _ in range(7)]
    current = [_row(url=u, si=4700.0, tbt=50.0) for u in _RUN_URLS]
    current[0]["tbt"] = 900.0
    alerts = detect_regressions(history, current)
    assert not any(a["metric"] == "si" for a in alerts)
    assert [a["url"] for a in alerts if a["metric"] == "tbt"] == [_RUN_URLS[0]]


def test_contaminated_run_suppresses_perf_score_alerts():
    """perf_score は素の metric の加重合成なので、汚染時は巻き添えで落ちる。"""
    history = [_row(url=u, si=2800.0, perf_score=85.0) for u in _RUN_URLS for _ in range(7)]
    current = [_row(url=u, si=4700.0, perf_score=64.0) for u in _RUN_URLS]
    assert detect_regressions(history, current) == []


def test_uncontaminated_run_keeps_perf_score_alerts():
    """汚染が無ければ perf_score は従来どおり鳴る。"""
    history = [_row(url=u, si=2800.0, perf_score=85.0) for u in _RUN_URLS for _ in range(7)]
    current = [_row(url=u, si=2800.0, perf_score=85.0) for u in _RUN_URLS]
    current[0]["perf_score"] = 64.0
    alerts = detect_regressions(history, current)
    assert [a["metric"] for a in alerts] == ["perf_score"]


def test_contaminated_run_is_logged_not_silently_dropped(caplog):
    """無音で消すと「鳴らないから健全」に見えるので、必ずログに残す。"""
    history = _stable_history("si", 2800.0)
    with caplog.at_level("WARNING"):
        detect_regressions(history, _run("si", [4700.0] * 6))
    assert "run-wide shift" in caplog.text


def test_render_report_shows_run_shift_section():
    alerts = [{"kind": "threshold", "url": "https://x/", "form_factor": "mobile",
               "metric": "tbt", "value": 900.0, "baseline": 50.0, "detail": "d"}]
    md = render_report(alerts, "2026-08-15", None, None,
                       {"si": {"median_ratio": 1.59, "fraction": 0.83,
                               "samples": 6, "over": 5}})
    assert "run 全体が沈んでいて判定を見送った metric" in md
    assert "1.59x" in md and "5/6" in md


def test_render_report_omits_run_shift_section_when_clean():
    alerts = [{"kind": "threshold", "url": "https://x/", "form_factor": "mobile",
               "metric": "tbt", "value": 900.0, "baseline": 50.0, "detail": "d"}]
    assert "run 全体が沈んでいて" not in render_report(alerts, "2026-08-15", None, None, {})


def test_build_lighthouse_argv_marks_ua_for_both_form_factors():
    """GA4 側でラボ計測を落とせるよう、両 form factor に UA マーカーが乗る (#6398)。

    受け側は hugo/layouts/partials/extend_head.html の `ga-disable-*`。
    ここが落ちたら GA4 がラボのヒットで汚れる。
    """
    for form_factor in ("mobile", "desktop"):
        argv = build_lighthouse_argv(
            "lighthouse", "https://navi.omcha.jp/", "out.json", form_factor
        )
        ua = [a for a in argv if a.startswith("--emulated-user-agent=")]
        assert len(ua) == 1, form_factor
        assert ua[0].endswith(" " + rll.LAB_UA_MARKER), form_factor
        # 既定 UA の中身は保つ (端末判定を変えない)
        assert ("Mobile Safari" in ua[0]) is (form_factor == "mobile")
