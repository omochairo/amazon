import datetime as dt

from scripts import close_failed_article_prs as m

NOW = dt.datetime(2026, 10, 16, 12, 0, tzinfo=dt.timezone.utc)
ARTICLE = "data/articles/2026-10-16-B0BN7V519Z.json"


def _pr(number=1, files=(ARTICLE,), conclusion="FAILURE", completed="2026-10-16T00:00:00Z",
        labels=(), draft=False, extra_checks=()):
    checks = [{"name": "validate", "status": "COMPLETED", "conclusion": conclusion,
               "completedAt": completed}]
    checks.extend(extra_checks)
    return {
        "number": number, "title": f"add article for B0BN7V519Z #{number}",
        "isDraft": draft, "labels": [{"name": n} for n in labels],
        "files": [{"path": p} for p in files], "statusCheckRollup": checks,
    }


def _numbers(prs, grace=6):
    return [p["number"] for p in m.select_failed(prs, NOW, grace)]


def test_failed_article_pr_past_grace_is_selected():
    assert _numbers([_pr()]) == [1]


def test_within_grace_is_kept():
    assert _numbers([_pr(completed="2026-10-16T08:00:00Z")]) == []


def test_passing_or_pending_validate_is_kept():
    assert _numbers([_pr(conclusion="SUCCESS"), _pr(number=2, conclusion=None)]) == []


def test_latest_validate_run_wins():
    # 古い run は FAILURE、修正後の新しい run は SUCCESS → close しない
    pr = _pr(extra_checks=[{"name": "validate", "status": "COMPLETED",
                            "conclusion": "SUCCESS", "completedAt": "2026-10-16T01:00:00Z"}])
    assert _numbers([pr]) == []


def test_other_failing_check_is_ignored():
    pr = _pr(conclusion="SUCCESS", extra_checks=[
        {"name": "unit-tests", "status": "COMPLETED", "conclusion": "FAILURE",
         "completedAt": "2026-10-16T00:00:00Z"}])
    assert _numbers([pr]) == []


def test_per_asin_and_brand_index_stay_in_scope():
    files = (ARTICLE, "data/raw/per_asin/B0BN7V519Z/omcha_related.json",
             "hugo/content/brands/foo/_index.md")
    assert _numbers([_pr(files=files)]) == [1]


def test_out_of_scope_files_are_left_for_humans():
    assert _numbers([_pr(files=(ARTICLE, "scripts/quality_gate.py"))]) == []


def test_non_article_pr_is_kept():
    assert _numbers([_pr(files=("data/raw/per_asin/B0BN7V519Z/x.json",))]) == []
    assert _numbers([_pr(number=2, files=("data/articles/notes.json",))]) == []


def test_draft_and_keep_label_are_kept():
    assert _numbers([_pr(draft=True), _pr(number=2, labels=("keep-open",))]) == []


def test_oldest_first():
    prs = [_pr(number=1, completed="2026-10-16T02:00:00Z"),
           _pr(number=2, completed="2026-10-15T00:00:00Z")]
    assert _numbers(prs) == [2, 1]


def test_dry_run_does_not_close(monkeypatch):
    monkeypatch.setattr(m, "list_open_prs", lambda repo: [_pr(completed="2026-01-01T00:00:00Z")])
    called = []
    monkeypatch.setattr(m, "close_pr", lambda *a: called.append(a) or True)
    assert m.main(["--dry-run"]) == 0
    assert called == []


def test_max_close_caps_writes(monkeypatch):
    monkeypatch.setattr(m, "list_open_prs", lambda repo: [_pr(number=i, completed="2026-01-01T00:00:00Z") for i in range(5)])
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    called = []
    monkeypatch.setattr(m, "close_pr", lambda repo, n, g: called.append(n) or True)
    assert m.main(["--max-close", "2", "--repo", "o/r"]) == 0
    assert len(called) == 2
