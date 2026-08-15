"""gitlab_pipeline_status.py の unit tests (#5260).

この script の値は「マージしたのに本番が変わらない」ときの切り分け 1 点に尽きる。
したがって固定したいのは verdict の 4 分岐:

  - 配信ジョブが失敗 → 落ちている
  - 実行中 / 待機中   → まだ古いのが正常 (待てばよい)
  - 全部成功         → 配信は終わっている = 以降はエッジキャッシュ側の問題 (#5260)
  - 配信ジョブが無い → push 起動になっていない

データ収集系のジョブ (fetch-data 等) が同じ ref で走っていても、配信の可否とは
関係が無い。それらに引きずられて verdict が変わらないことも固定する。
"""
from __future__ import annotations

import datetime as dt

import pytest

from scripts.gitlab_pipeline_status import (
    ApiError,
    fetch_jobs,
    fetch_pipelines,
    render,
    summarize,
)

NOW = dt.datetime(2026, 8, 15, 12, 0, tzinfo=dt.timezone.utc)


def pipeline(pid=101, status="running", sha="abc123def4567890"):
    return {
        "id": pid,
        "status": status,
        "sha": sha,
        "ref": "main",
        "created_at": "2026-08-15T11:30:00.000Z",
        "updated_at": "2026-08-15T11:45:00.000Z",
        "web_url": f"https://gitlab.com/omocha/navi/-/pipelines/{pid}",
    }


def job(name, status, stage="deploy", duration=60.0):
    return {"name": name, "stage": stage, "status": status, "duration": duration,
            "started_at": "2026-08-15T11:31:00.000Z",
            "finished_at": "2026-08-15T11:44:00.000Z",
            "web_url": "https://gitlab.com/omocha/navi/-/jobs/1"}


# --- verdict ---------------------------------------------------------------

def test_failed_pages_job_is_reported_as_failure():
    summary = summarize([pipeline(status="failed")],
                        [job("pages", "failed")], NOW)
    assert summary["verdict"] == "配信ジョブが失敗している"


def test_running_pages_job_means_production_is_still_old():
    summary = summarize([pipeline()], [job("pages", "running")], NOW)
    assert summary["verdict"] == "配信ジョブが実行中 / 待機中 (本番はまだ古い)"


def test_pending_counts_as_waiting_not_success():
    """concurrent=1 の runner に待たされている状態を「完走」と読ませない。"""
    summary = summarize([pipeline()], [job("pages", "pending")], NOW)
    assert "実行中" in summary["verdict"]


def test_all_delivery_jobs_success_points_at_the_edge_cache():
    summary = summarize(
        [pipeline(status="success")],
        [job("pages", "success"), job("pages:deploy", "success"),
         job("cf-purge", "success", stage="postdeploy")],
        NOW,
    )
    assert summary["verdict"] == (
        "配信ジョブは完走済み (本番に出ていなければエッジキャッシュを疑う)"
    )


def test_pipeline_without_delivery_jobs_is_flagged():
    """schedule 起動の invoke-jules 等だけのパイプラインを掴んだ場合。"""
    summary = summarize([pipeline()], [job("invoke-jules", "success", stage="generate")],
                        NOW)
    assert summary["verdict"] == "配信ジョブが 1 つも無い (push 起動になっていない可能性)"


def test_data_jobs_do_not_drag_the_verdict():
    summary = summarize(
        [pipeline(status="running")],
        [job("fetch-data", "failed", stage="generate"), job("pages", "success")],
        NOW,
    )
    assert "完走済み" in summary["verdict"]


def test_no_pipeline_at_all():
    summary = summarize([], [], NOW)
    assert summary["verdict"] == "該当するパイプラインが無い"
    assert summary["pipeline"]["id"] is None


# --- 整形 -------------------------------------------------------------------

def test_delivery_jobs_are_listed_first():
    summary = summarize(
        [pipeline()],
        [job("fetch-data", "success", stage="generate"), job("pages", "success")],
        NOW,
    )
    assert [j["name"] for j in summary["jobs"]] == ["pages", "fetch-data"]


def test_sha_is_shortened_for_display():
    summary = summarize([pipeline(sha="abcdef1234567890abcdef")], [], NOW)
    assert summary["pipeline"]["sha"] == "abcdef123456"


def test_render_includes_verdict_and_jobs():
    summary = summarize([pipeline()], [job("pages", "running")], NOW)
    out = render(summary, NOW)
    assert "配信ジョブが実行中" in out
    assert "pages" in out
    assert "https://gitlab.com/omocha/navi/-/pipelines/101" in out


# --- API 呼び出し -----------------------------------------------------------

def test_sha_lookup_drops_the_ref_filter():
    """GitHub 側の SHA をそのまま渡せることが要件。ref と併用すると取りこぼす。"""
    seen = {}

    def get(path, params):
        seen[path] = params
        return [pipeline()]

    fetch_pipelines(get, "main", "abc123", 5)
    assert seen["/pipelines"] == {"sha": "abc123", "per_page": "5"}


def test_ref_lookup_without_sha():
    seen = {}

    def get(path, params):
        seen[path] = params
        return [pipeline()]

    fetch_pipelines(get, "main", None, 3)
    assert seen["/pipelines"] == {"ref": "main", "per_page": "3"}


def test_unexpected_api_shape_raises():
    """権限不足時に dict のエラー body が返るのを list として扱わない。"""
    with pytest.raises(ApiError):
        fetch_pipelines(lambda p, q: {"message": "403 Forbidden"}, "main", None, 5)
    with pytest.raises(ApiError):
        fetch_jobs(lambda p, q: {"message": "403 Forbidden"}, 101)
