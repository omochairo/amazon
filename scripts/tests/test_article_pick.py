"""article_pick (記事の候補選定) のテスト。

元は GitLab 側の Jules 記事生成の候補選定と 03 の pick-asin が同じ結果を返すかも
比べていたが、GitLab 側の Jules 記事生成は削除した (amazon-navi-brain#170)。
"""
from __future__ import annotations

import contextlib
import json
import os
import pathlib
import re
import sys
import tempfile
import textwrap
import types
import unittest
from unittest import mock

THIS_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = THIS_DIR.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import article_pick  # noqa: E402
import score_per_asin_info as real_scorer  # noqa: E402


def _asin(n: int) -> str:
    return f"B0TEST{n:04d}"


class SelectCandidatesTest(unittest.TestCase):
    def test_waiting_rewrite_does_not_take_rewrite_slot(self):
        # navi-brain#117: 待ちの書き直しは上限 (rewrite_cap) で切る前に外す。
        # 後で外すと先頭の待ちが枠を占有し、書き直しが回らない
        waiting = {_asin(50)}
        scorer = types.SimpleNamespace(score_asin=lambda a: a,
                                       should_defer=lambda r: r in waiting)
        with contextlib.redirect_stdout(open(os.devnull, "w", encoding="utf-8")):
            out = article_pick.select_candidates(
                [], set(), [], [], [_asin(50), _asin(51), _asin(52)], "s", scorer,
                rewrite_cap=2)
        self.assertEqual([o["asin"] for o in out if o["pool"] == "rewrite-queue"],
                         [_asin(51), _asin(52)])

    def test_scorer_failure_does_not_stop_pick(self):
        broken = types.SimpleNamespace(score_asin=lambda a: 1 / 0, should_defer=lambda r: True)
        with contextlib.redirect_stdout(open(os.devnull, "w", encoding="utf-8")):
            out = article_pick.select_candidates(
                [{"asin": _asin(1)}], set(), [], [], None, "s", broken)
        self.assertEqual(out, [{"asin": _asin(1), "pool": "", "source_keyword": None,
                               "priority_score": None, "priority_breakdown": None,
                               "character": None}])

    def test_genre_check_failure_does_not_stop_pick(self):
        # 判定不能・判定の例外は通す (fail-open)。pick を止めない。
        def boom(a):
            raise RuntimeError("broken snapshot")
        with contextlib.redirect_stdout(open(os.devnull, "w", encoding="utf-8")):
            out = article_pick.select_candidates(
                [{"asin": _asin(1)}, {"asin": _asin(2)}], set(), [], [], None, "s",
                genre_flagged=boom, blocked={_asin(2)})
        self.assertEqual([o["asin"] for o in out], [_asin(1)])

    def test_blocklisted_first_party_is_not_reported_as_waiting(self):
        # blocklist 済みは商品データも消してあるので、「データ待ち」には数えない
        # (待っても取得されない。除外の行に出す)。
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            out = article_pick.select_candidates(
                [], set(), [], [_asin(60), _asin(61), _asin(62)], None, "s",
                has_amazon_item=lambda a: a == _asin(62), blocked={_asin(60)})
        self.assertEqual([o["asin"] for o in out], [_asin(62)])
        log = buf.getvalue()
        self.assertIn(f"first-party waiting for amazon data (#9155): 1 ['{_asin(61)}']", log)
        self.assertIn(f"excluded at pick, blocklist (#9155): 1 ['{_asin(60)}']", log)

    def test_load_blocklist(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "b.json")
            self.assertEqual(article_pick.load_blocklist(p), set())  # 無ければ空
            with open(p, "w", encoding="utf-8") as f:
                f.write("{broken")
            with open(os.devnull, "w", encoding="utf-8") as null, \
                    contextlib.redirect_stdout(null), contextlib.redirect_stderr(null):
                self.assertEqual(article_pick.load_blocklist(p), set())  # 壊れていれば空
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"blocked": [{"asin": _asin(1)}, {"reason": "no asin"}, "x"]}, f)
            self.assertEqual(article_pick.load_blocklist(p), {_asin(1)})

    def test_load_quarantine(self):
        """#11311: 退避中 (人のレビュー待ち) の ASIN。無い・壊れていれば空。"""
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "q.json")
            self.assertEqual(article_pick.load_quarantine(p), set())
            with open(p, "w", encoding="utf-8") as f:
                f.write("{broken")
            with open(os.devnull, "w", encoding="utf-8") as null, \
                    contextlib.redirect_stdout(null), contextlib.redirect_stderr(null):
                self.assertEqual(article_pick.load_quarantine(p), set())
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"_doc": "x", "candidates": [
                    {"asin": _asin(2), "ratio": 1.5}, {"reason": "no asin"}, "x"]}, f)
            self.assertEqual(article_pick.load_quarantine(p), {_asin(2)})
            # 配列欄が null・dict でない最上位でも止めない
            for bad in ({"candidates": None}, {"candidates": "x"}, [1, 2]):
                with open(p, "w", encoding="utf-8") as f:
                    json.dump(bad, f)
                self.assertEqual(article_pick.load_quarantine(p), set())
                with open(p, "w", encoding="utf-8") as f:
                    json.dump({"blocked": None} if isinstance(bad, dict) else bad, f)
                self.assertEqual(article_pick.load_blocklist(p), set())

    def test_pick_from_repo_excludes_quarantined(self):
        """#11311: pick_from_repo は blocklist と退避中の両方を blocked に渡す。"""
        captured = {}

        def fake_select(*args, **kw):
            captured.update(kw)
            return []

        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "data", "raw"))
            with open(os.path.join(tmp, "data", "raw", "amazon.json"), "w",
                      encoding="utf-8") as f:
                json.dump({"items": []}, f)
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                with mock.patch.object(article_pick, "select_candidates", fake_select), \
                        mock.patch.object(article_pick, "load_blocklist",
                                          return_value={_asin(1)}), \
                        mock.patch.object(article_pick, "load_quarantine",
                                          return_value={_asin(2)}), \
                        mock.patch.dict(os.environ, {"JULES_RECENT_SESSIONS": "",
                                                     "CLOSED_ARTICLE_PRS": ""}), \
                        open(os.devnull, "w", encoding="utf-8") as null, \
                        contextlib.redirect_stdout(null), contextlib.redirect_stderr(null):
                    article_pick.pick_from_repo(set(), "seed")
            finally:
                os.chdir(cwd)
        self.assertEqual(captured["blocked"], {_asin(1), _asin(2)})


class RecentlyFailedTest(unittest.TestCase):
    """#9239: マージされずに close された記事 PR の ASIN を、しばらく pick に戻さない。"""

    def setUp(self):
        import datetime as dt
        self.dt = dt
        self.now = dt.datetime(2026, 10, 9, 12, 0, tzinfo=dt.timezone.utc)

    def _ago(self, days):
        return (self.now - self.dt.timedelta(days=days)).isoformat().replace("+00:00", "Z")

    def test_single_close_cools_down_then_returns(self):
        rows = [{"title": "feat(article): add article JSON for B0FYCTJF5Z", "closedAt": self._ago(1)},
                {"title": "Add article JSON for B0OLDCLOSE", "closedAt": self._ago(20)}]
        cooling, stuck = article_pick.recently_failed(rows, self.now)
        self.assertEqual(cooling, {"B0FYCTJF5Z"})  # 14 日を過ぎた B0OLDCLOSE は戻す
        self.assertEqual(stuck, set())

    def test_lowercase_title_and_material_refresh(self):
        rows = [{"title": "Add article JSON for b0h11r2ft2 (3D魔法ペイント)", "closedAt": self._ago(2)}]
        cooling, _ = article_pick.recently_failed(rows, self.now)
        self.assertEqual(cooling, {"B0H11R2FT2"})
        # close の後に事前収集を取り直していれば、素材が変わったので待たせない
        fresh = lambda a: self.now - self.dt.timedelta(days=1)  # noqa: E731
        cooling, _ = article_pick.recently_failed(rows, self.now, fresh)
        self.assertEqual(cooling, set())

    def test_repeated_closes_are_stuck_until_material_changes(self):
        rows = [{"title": "Add article JSON for B0FYCTJF5Z", "closedAt": self._ago(d)}
                for d in (0, 1, 25)]
        before = lambda a: self.now - self.dt.timedelta(days=3)  # noqa: E731
        cooling, stuck = article_pick.recently_failed(rows, self.now, before)
        self.assertEqual(stuck, {"B0FYCTJF5Z"})
        self.assertEqual(cooling, set())
        # 最後の close の後に取り直していれば戻す (B0C6THPT89 の実例)
        after = lambda a: self.now + self.dt.timedelta(minutes=1)  # noqa: E731
        self.assertEqual(article_pick.recently_failed(rows, self.now, after), (set(), set()))
        # 窓 (30 日) の外の close は数えない
        old = [{"title": "Add article JSON for B0AAAAAAAA", "closedAt": self._ago(d)}
               for d in (31, 40)]
        self.assertEqual(article_pick.recently_failed(old, self.now), (set(), set()))

    def test_exclude_reads_file_and_is_fail_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "closed.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump([{"title": "Add article JSON for B0FYCTJF5Z", "closedAt": self._ago(1)}], f)
            out = article_pick.exclude_recently_failed({"B0EXISTING"}, path=path, now=self.now)
            self.assertEqual(out, {"B0EXISTING", "B0FYCTJF5Z"})
            # 指定無し・読めないファイルは除外しない
            self.assertEqual(article_pick.exclude_recently_failed({"B0X"}, path=""), {"B0X"})
            self.assertEqual(article_pick.exclude_recently_failed(
                {"B0X"}, path=os.path.join(tmp, "missing.json")), {"B0X"})


class FailingSessionsTest(unittest.TestCase):
    """#9410: 同じ ASIN の session が続けて FAILED なら pick から外す。"""

    def setUp(self):
        import datetime as dt
        self.dt = dt
        self.now = dt.datetime(2026, 10, 12, 12, 0, tzinfo=dt.timezone.utc)

    def _s(self, asin, state, hours_ago, title=None):
        created = self.now - self.dt.timedelta(hours=hours_ago)
        return {"id": "1", "title": title or f"[wf03] 記事生成: {asin}",
                "state": state, "createTime": created.isoformat()}

    def test_three_failed_in_a_row_is_excluded(self):
        rows = [self._s("B00JP15EE2", "FAILED", h) for h in (1, 26, 50)]
        rows += [self._s("B00IUD8SFI", "FAILED", h) for h in (14, 26)]  # 2 回はまだ外さない
        self.assertEqual(article_pick.failing_sessions(rows, self.now), {"B00JP15EE2"})

    def test_empty_completed_is_not_counted(self):
        # 空振り (COMPLETED で PR 無し) は数えない。FAILED の間に挟まれば連続も切れる
        rows = [self._s("B0DJNNHSCB", "COMPLETED", h) for h in (1, 10, 20)]
        rows += [self._s("B0AAAAAAAA", s, h) for s, h in
                 (("FAILED", 1), ("FAILED", 5), ("COMPLETED", 9), ("FAILED", 13))]
        self.assertEqual(article_pick.failing_sessions(rows, self.now), set())

    def test_newest_session_decides(self):
        # 3 回 FAILED のあとに作り直した session が走っている・終わったなら外さない
        failed = [self._s("B0AAAAAAAA", "FAILED", h) for h in (5, 10, 15)]
        for state in ("IN_PROGRESS", "COMPLETED", None):
            rows = failed + [self._s("B0AAAAAAAA", state, 1)]
            self.assertEqual(article_pick.failing_sessions(rows, self.now), set())

    def test_window_and_other_sessions_are_ignored(self):
        # 窓 (72h) の外の FAILED は数えない = 古い FAILED が出れば試し直しになる
        rows = [self._s("B0AAAAAAAA", "FAILED", h) for h in (1, 30, 73)]
        # 題名に ASIN が無い旧 session・記事生成以外の session・壊れた行
        rows += [self._s("", "FAILED", h, title="[wf03] 記事生成: 商品深掘りガイド") for h in (1, 2, 3)]
        rows += [self._s("", "FAILED", h, title="[wf14] narrative: B0BBBBBBBB") for h in (1, 2, 3)]
        rows += [None, {"title": "[wf03] 記事生成: B0CCCCCCCC", "state": "FAILED"}]
        self.assertEqual(article_pick.failing_sessions(rows, self.now), set())

    def test_exclude_reads_file_and_is_fail_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sessions.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump([self._s("B00JP15EE2", "FAILED", h) for h in (1, 2, 3)], f)
            out = article_pick.exclude_failing_sessions({"B0EXISTING"}, path=path, now=self.now)
            self.assertEqual(out, {"B0EXISTING", "B00JP15EE2"})
            # 指定無し・読めないファイル (ゲートの fallback・手動起動) は除外しない
            self.assertEqual(article_pick.exclude_failing_sessions({"B0X"}, path=""), {"B0X"})
            self.assertEqual(article_pick.exclude_failing_sessions(
                {"B0X"}, path=os.path.join(tmp, "missing.json")), {"B0X"})
            for broken in ("3", "{not json"):
                with open(path, "w", encoding="utf-8") as f:
                    f.write(broken)
                self.assertEqual(article_pick.exclude_failing_sessions({"B0X"}, path=path), {"B0X"})

    def test_gate_output_feeds_the_pick(self):
        # jules_quota_gate.write_sessions の出力をそのまま読める (API の createTime は Z 付き・ナノ秒)
        import jules_quota_gate
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sessions.json")
            jules_quota_gate.write_sessions(path, [
                {"id": str(i), "title": "[wf03] 記事生成: B00JP15EE2", "state": "FAILED",
                 "createTime": f"2026-10-12T0{i}:00:00.123456789Z", "prompt": "x" * 100}
                for i in range(3)])
            with open(path, encoding="utf-8") as f:
                self.assertNotIn("prompt", f.read())
            out = article_pick.exclude_failing_sessions(set(), path=path, now=self.now)
            self.assertEqual(out, {"B00JP15EE2"})


if __name__ == "__main__":
    unittest.main()
