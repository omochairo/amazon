"""scripts/open_toy_recall_issue.py unit tests (#4320 follow-up)."""
from __future__ import annotations

import json

from scripts import open_toy_recall_issue as otri


class _FakeCompletedProcess:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_render_body_groups_by_brand_desc_count():
    data = {
        "candidates": [
            {"brand": "A", "post_date": "2020-01", "category": "c", "title": "t1", "url": "u1"},
            {"brand": "B", "post_date": "2020-01", "category": "c", "title": "t2", "url": "u2"},
            {"brand": "B", "post_date": "2020-02", "category": "c", "title": "t3", "url": "u3"},
        ],
        "rows_scanned": 10,
        "verified_excluded": 1,
    }
    body = otri.render_body(data)
    assert body.index("### B (2)") < body.index("### A (1)")
    assert "3 件" in body


def test_render_match_comment_lists_asin_links_and_reason():
    matches = [
        {
            "brand": "くもん出版",
            "title": "くもん出版「玩具：くるくるチャイム」 - 交換／返金",
            "matched_asins": ["B0BD3GW7S8", "B0CL4WXQZ6"],
            "match_reason": "一次情報と一致",
            "post_date": "2026-07-30",
            "url": "https://example/detail",
        },
    ]
    body = otri.render_match_comment(matches, "gemini-3.8-flash-high")
    assert "[B0BD3GW7S8](https://www.amazon.co.jp/dp/B0BD3GW7S8)" in body
    assert "[B0CL4WXQZ6](https://www.amazon.co.jp/dp/B0CL4WXQZ6)" in body
    assert "一次情報と一致" in body
    assert "gemini-3.8-flash-high" in body
    assert otri.MATCH_COMMENT_MARKER in body


def test_find_open_issue_number_returns_number_when_found(monkeypatch):
    def fake_run(cmd, **kwargs):
        return _FakeCompletedProcess(stdout=json.dumps({"items": [{"number": 4320}]}))

    monkeypatch.setattr("scripts.open_toy_recall_issue.subprocess.run", fake_run)
    assert otri.find_open_issue_number("owner/repo") == 4320


def test_find_open_issue_number_returns_none_when_absent(monkeypatch):
    def fake_run(cmd, **kwargs):
        return _FakeCompletedProcess(stdout=json.dumps({"items": []}))

    monkeypatch.setattr("scripts.open_toy_recall_issue.subprocess.run", fake_run)
    assert otri.find_open_issue_number("owner/repo") is None


def test_post_match_comment_dry_run_writes_preview_and_skips_gh(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    matches_file = tmp_path / "matches.json"
    matches_file.write_text(json.dumps({
        "candidates": [
            {"brand": "b", "title": "t", "matched_asins": ["B0BD3GW7S8"], "match_reason": "r"},
            {"brand": "b2", "title": "t2", "matched_asins": []},
        ],
    }), encoding="utf-8")

    calls = []
    monkeypatch.setattr(otri, "find_open_issue_number", lambda repo: 999)
    monkeypatch.setattr(otri, "post_issue_comment", lambda *a, **k: calls.append((a, k)))

    args = otri.argparse.Namespace(
        matches=str(matches_file), repo="owner/repo", dry_run=True, model="gemini-3.8-flash-high",
    )
    assert otri._post_match_comment(args) == 0
    assert calls == []
    preview = tmp_path / "_toy_recall_match_comment_preview.md"
    assert preview.exists()
    assert "t" in preview.read_text(encoding="utf-8")


def test_post_match_comment_returns_zero_when_no_matches(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    matches_file = tmp_path / "matches.json"
    matches_file.write_text(json.dumps({"candidates": [{"brand": "b", "matched_asins": []}]}), encoding="utf-8")

    monkeypatch.setattr(otri, "find_open_issue_number", lambda repo: (_ for _ in ()).throw(AssertionError("should not be called")))

    args = otri.argparse.Namespace(matches=str(matches_file), repo="owner/repo", dry_run=False, model="m")
    assert otri._post_match_comment(args) == 0


def test_post_match_comment_warns_and_returns_nonzero_when_no_open_issue(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    matches_file = tmp_path / "matches.json"
    matches_file.write_text(json.dumps({
        "candidates": [{"brand": "b", "title": "t", "matched_asins": ["B0BD3GW7S8"]}],
    }), encoding="utf-8")

    monkeypatch.setattr(otri, "find_open_issue_number", lambda repo: None)
    args = otri.argparse.Namespace(matches=str(matches_file), repo="owner/repo", dry_run=False, model="m")
    assert otri._post_match_comment(args) == 1


def test_post_match_comment_posts_comment_when_issue_found(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    matches_file = tmp_path / "matches.json"
    matches_file.write_text(json.dumps({
        "candidates": [{"brand": "b", "title": "t", "matched_asins": ["B0BD3GW7S8"]}],
    }), encoding="utf-8")

    posted = {}

    def fake_post(repo, issue_number, body):
        posted["repo"] = repo
        posted["issue_number"] = issue_number
        posted["body"] = body

    monkeypatch.setattr(otri, "find_open_issue_number", lambda repo: 4320)
    monkeypatch.setattr(otri, "post_issue_comment", fake_post)

    args = otri.argparse.Namespace(matches=str(matches_file), repo="owner/repo", dry_run=False, model="m")
    assert otri._post_match_comment(args) == 0
    assert posted["issue_number"] == 4320
    assert posted["repo"] == "owner/repo"
    assert "B0BD3GW7S8" in posted["body"]
