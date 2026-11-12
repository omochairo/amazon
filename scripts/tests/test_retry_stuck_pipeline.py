"""retry_stuck_pipeline.py の単体テスト。

重心は「やり直してはいけないときにやり直さない」に置く
(ビルド自体の失敗・やり直し済み・ランナー不在・古い失敗・新しい push がある)。
"""
from __future__ import annotations

import datetime as dt
import pathlib
import sys

THIS_DIR = pathlib.Path(__file__).resolve().parent
SCRIPTS_DIR = THIS_DIR.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import retry_stuck_pipeline as rsp  # noqa: E402

NOW = dt.datetime(2026, 11, 12, 0, 0, 0, tzinfo=dt.timezone.utc)
STUCK = "stuck_pending_no_matching_runners"


def pipeline(status="failed", created="2026-11-11T21:16:15.511Z"):
    return {"id": 3056446351, "status": status, "sha": "1d4d61e66d24b370fe3ba5",
            "created_at": created}


def job(name, status, reason=None, jid=1):
    return {"id": jid, "name": name, "status": status, "failure_reason": reason,
            "allow_failure": False}


def stuck_jobs():
    # 2026-11-12 の実物の形: build-site だけ落ち、後続は skipped
    return [job("deploy-nas", "skipped", jid=4), job("cf-purge", "skipped", jid=3),
            job("pages", "skipped", jid=2), job("build-site", "failed", STUCK, jid=1)]


def test_runner_absence_is_retried_when_a_runner_is_back():
    assert rsp.decide(pipeline(), stuck_jobs(), 1, NOW)["action"] == "retry"


def test_waits_while_no_runner_is_online():
    assert rsp.decide(pipeline(), stuck_jobs(), 0, NOW)["action"] == "wait"


def test_real_build_failure_is_not_retried():
    jobs = [job("build-site", "failed", "script_failure")]
    assert rsp.decide(pipeline(), jobs, 2, NOW)["action"] == "real_failure"


def test_mixed_reasons_are_not_retried():
    jobs = [job("build-site", "failed", STUCK, jid=1),
            job("deploy-nas", "failed", "script_failure", jid=2)]
    assert rsp.decide(pipeline(), jobs, 2, NOW)["action"] == "real_failure"


def test_only_one_retry():
    jobs = [job("build-site", "failed", STUCK, jid=1), job("build-site", "failed", STUCK, jid=9)]
    assert rsp.decide(pipeline(), jobs, 2, NOW)["action"] == "exhausted"


def test_retried_and_succeeded_job_is_not_counted_as_failed():
    jobs = [job("build-site", "failed", STUCK, jid=1), job("build-site", "success", jid=9)]
    assert rsp.decide(pipeline(), jobs, 2, NOW)["action"] == "none"


def test_old_failure_is_left_to_humans():
    old = pipeline(created="2026-11-10T00:00:00Z")
    assert rsp.decide(old, stuck_jobs(), 2, NOW)["action"] == "none"


def test_latest_not_failed_means_nothing_to_do():
    for status in ("success", "running", "pending", "canceled"):
        assert rsp.decide(pipeline(status=status), [], 2, NOW)["action"] == "none"


def test_non_delivery_job_failure_is_ignored():
    jobs = [job("fetch-data", "failed", STUCK)]
    assert rsp.decide(pipeline(), jobs, 2, NOW)["action"] == "none"


def test_allowed_failure_is_ignored():
    j = job("cf-purge", "failed", "script_failure")
    j["allow_failure"] = True
    assert rsp.decide(pipeline(), [j], 2, NOW)["action"] == "none"


def _fake_call(pipelines, jobs, runners, posted):
    def call(method, path, params):
        if method == "POST":
            posted.append(path)
            return {}
        if path == "/pipelines":
            assert params["ref"] == "main" and params["per_page"] == "1"
            return pipelines
        if path.endswith("/jobs"):
            assert params["include_retried"] == "true"
            return jobs
        if path == "/runners":
            return runners
        raise AssertionError(path)
    return call


def test_run_posts_retry_only_when_decided():
    posted = []
    row = rsp.run(_fake_call([pipeline()], stuck_jobs(), [{"paused": False}], posted),
                  "main", NOW, dry_run=False, max_age_hours=24, max_retries=1)
    assert row["action"] == "retry"
    assert posted == ["/pipelines/3056446351/retry"]


def test_run_paused_runner_does_not_count():
    posted = []
    row = rsp.run(_fake_call([pipeline()], stuck_jobs(), [{"paused": True}], posted),
                  "main", NOW, dry_run=False, max_age_hours=24, max_retries=1)
    assert row["action"] == "wait" and posted == []


def test_run_dry_run_does_not_post():
    posted = []
    rsp.run(_fake_call([pipeline()], stuck_jobs(), [{"paused": False}], posted),
            "main", NOW, dry_run=True, max_age_hours=24, max_retries=1)
    assert posted == []


def test_run_success_does_not_read_jobs():
    posted = []
    row = rsp.run(_fake_call([pipeline(status="success")], None, None, posted),
                  "main", NOW, dry_run=False, max_age_hours=24, max_retries=1)
    assert row["action"] == "none" and posted == []


def test_missing_token_is_red(monkeypatch):
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    assert rsp.main([]) == 2


def test_transient_api_error_is_a_warning_not_red(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "t")

    def boom(*a, **k):
        raise rsp.ApiError("HTTP 502")
    monkeypatch.setattr(rsp, "run", boom)
    assert rsp.main([]) == 0


def test_auth_error_is_red(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "t")

    def boom(*a, **k):
        raise rsp.AuthError("401")
    monkeypatch.setattr(rsp, "run", boom)
    assert rsp.main([]) == 1
