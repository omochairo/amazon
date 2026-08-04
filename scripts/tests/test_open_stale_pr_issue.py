"""open_stale_pr_issue.py の純粋関数を stdlib unittest で検証する。

実行: `python -m unittest scripts.tests.test_open_stale_pr_issue` を amazon-clone 直下から、
または `python scripts/tests/test_open_stale_pr_issue.py`。

gh は呼ばない (select_stale / render_body は pure function)。
"""
import datetime as dt
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))  # scripts/ を import path に追加

from open_stale_pr_issue import (  # noqa: E402
    MARKER, render_body, select_stale,
)

NOW = dt.datetime(2026, 8, 4, 12, 0, 0, tzinfo=dt.timezone.utc)


def _pr(number, hours_ago, *, draft=False, updated_hours_ago=None, **kw):
    created = NOW - dt.timedelta(hours=hours_ago)
    updated = NOW - dt.timedelta(
        hours=updated_hours_ago if updated_hours_ago is not None else hours_ago
    )
    pr = {
        "number": number,
        "title": f"PR {number}",
        "url": f"https://github.com/omochairo/amazon/pull/{number}",
        "createdAt": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "updatedAt": updated.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "isDraft": draft,
        "author": {"login": "omochairo"},
        "labels": [],
        "mergeStateStatus": "CLEAN",
        "headRefName": f"branch-{number}",
    }
    pr.update(kw)
    return pr


class SelectStaleTests(unittest.TestCase):

    def test_picks_only_prs_older_than_threshold(self):
        prs = [_pr(1, 30), _pr(2, 5), _pr(3, 24)]
        rows = select_stale(prs, NOW, 24)
        # 24h ちょうども滞留扱い (境界を pass 側に潰さない)
        self.assertEqual([r["number"] for r in rows], [1, 3])

    def test_excludes_drafts(self):
        rows = select_stale([_pr(1, 100, draft=True), _pr(2, 100)], NOW, 24)
        self.assertEqual([r["number"] for r in rows], [2])

    def test_sorted_by_age_desc(self):
        prs = [_pr(1, 30), _pr(2, 96), _pr(3, 48)]
        self.assertEqual([r["number"] for r in select_stale(prs, NOW, 24)], [2, 3, 1])

    def test_recent_updated_at_does_not_reset_age(self):
        """bot の push/ラベル付けで updatedAt だけ新しい PR も滞留として拾う (#4280)。"""
        prs = [_pr(1, 96, updated_hours_ago=0)]
        rows = select_stale(prs, NOW, 24)
        self.assertEqual([r["number"] for r in rows], [1])
        self.assertAlmostEqual(rows[0]["age_hours"], 96.0, places=3)

    def test_empty_input(self):
        self.assertEqual(select_stale([], NOW, 24), [])


class RenderBodyTests(unittest.TestCase):

    def test_contains_marker_and_rows(self):
        rows = select_stale([_pr(4280, 96), _pr(9, 25)], NOW, 24)
        body = render_body(rows, 24)
        self.assertIn(f"<!-- {MARKER} -->", body)
        self.assertIn("#4280", body)
        self.assertIn("#9", body)
        self.assertIn("4.0 日", body)   # 48h 超は日表示
        self.assertIn("25 時間", body)  # 48h 未満は時間表示

    def test_escapes_pipe_in_title(self):
        rows = select_stale([_pr(1, 30, title="a | b")], NOW, 24)
        body = render_body(rows, 24)
        # テーブルが壊れないよう | は全角に置換される
        self.assertIn("a ／ b", body)

    def test_labels_and_author_rendered(self):
        rows = select_stale(
            [_pr(1, 30, labels=[{"name": "auto-merge-skipped"}])], NOW, 24
        )
        body = render_body(rows, 24)
        self.assertIn("auto-merge-skipped", body)
        self.assertIn("omochairo", body)


if __name__ == "__main__":
    unittest.main()
