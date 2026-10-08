"""#9073 — GitLab 側の候補選定 (article_pick / invoke_jules_repoless) が
03-invoke-jules.yml の pick-asin と同じ結果を返すことを固定する。

03 の inline Python (heredoc) をそのまま取り出し、同じ入力ファイル・同じ seed・
同じ偽モジュール (rewrite_queue / score_per_asin_info) で両方を実行して、
出力 (並び順と出自) を比べる。03 だけ直して repoless が置いていかれると
ここが落ちる。
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

THIS_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = THIS_DIR.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import article_pick  # noqa: E402
import invoke_jules_repoless as repoless  # noqa: E402
import score_per_asin_info as real_scorer  # noqa: E402


def _asin(n: int) -> str:
    return f"B0TEST{n:04d}"


def _wf03_pick_source() -> str:
    text = (REPO_ROOT / ".github" / "workflows" / "03-invoke-jules.yml").read_text(encoding="utf-8")
    m = re.search(r"python3 - <<'PY'\n(.*?)\n\s*PY\n", text, re.S)
    assert m, "03-invoke-jules.yml の pick-asin heredoc が見つからない"
    return textwrap.dedent(m.group(1))


class FakeGitLab:
    def __init__(self, open_titles, locked):
        self._open, self._locked = open_titles, locked

    def open_mr_titles(self):
        return list(self._open)

    def lock_branches(self):
        return list(self._locked)


class ParityTest(unittest.TestCase):
    def _fixture(self, tmp, *, first_party=None, bands=None, hosts=None, no_amazon=(),
                 off_genre=()):
        root = pathlib.Path(tmp)
        (root / "data" / "raw").mkdir(parents=True)
        (root / "data" / "articles").mkdir(parents=True)
        (root / "tmp").mkdir()
        items = []
        for n in range(1, 21):
            item = {"asin": _asin(n)}
            if n % 3 == 0:
                item.update(lane="demand", source_keyword=f"kw{n}")
            elif n % 3 == 1:
                item["lane"] = "supply-random"
            items.append(item)
        items.append({"asin": "4910762175"})  # B0 形式でないものは候補にしない
        (root / "data" / "raw" / "amazon.json").write_text(
            json.dumps({"items": items}), encoding="utf-8")
        ranking = [_asin(n) for n in (2, 5, 31, 32, 33, 34)]
        (root / "data" / "raw" / "ranking_pool.json").write_text(
            json.dumps({"asins": ranking + ["9784000000000"]}), encoding="utf-8")
        if first_party is not None:
            (root / "data" / "raw" / "first_party_pool.json").write_text(
                json.dumps({"asins": first_party}), encoding="utf-8")
            # #9155: 商品データ (per_asin amazon.json の item) がある ASIN だけ pick される
            for a in first_party:
                if a in no_amazon:
                    continue
                d = root / "data" / "raw" / "per_asin" / a
                d.mkdir(parents=True, exist_ok=True)
                item = {"asin": a, "title": f"商品 {a}"}
                if a in off_genre:  # #9155: 取得時ゲートを通らない first-party のジャンル不一致
                    item["browse_nodes"] = [{"id": "1", "name": "漂白剤", "root": "ドラッグストア"}]
                (d / "amazon.json").write_text(
                    json.dumps({"asin": a, "item": item}), encoding="utf-8")
        for n in (1, 4, 50):  # 既存記事 (50 はリライト待ち)
            (root / "data" / "articles" / f"2026-01-01-{_asin(n)}.json").write_text("{}")
        (root / "data" / "articles" / f"2026-01-01-{_asin(7)}.quality.json").write_text("{}")
        open_titles = [f"feat: generate article JSON for {_asin(8)}"]
        locked = [_asin(9), _asin(32)]
        (root / "tmp" / "open_titles.txt").write_text("\n".join(open_titles) + "\n", encoding="utf-8")
        (root / "tmp" / "locked.txt").write_text("\n".join(locked) + "\n", encoding="utf-8")

        bands = bands or {}
        hosts = hosts or {}
        scorer = types.ModuleType("score_per_asin_info")
        scorer.score_asin = lambda a: {"band": bands.get(a, "rich"),
                                       "third_party_hosts": hosts.get(a, 0)}
        scorer.should_defer = real_scorer.should_defer
        rq = types.ModuleType("rewrite_queue")
        rq.eligible_rewrite_asins = lambda d: {_asin(50)}
        rq.pending_rewrite_candidates = lambda d: [_asin(50), _asin(51), _asin(52), _asin(5)]
        return root, FakeGitLab(open_titles, locked), {"score_per_asin_info": scorer,
                                                       "rewrite_queue": rq}

    @contextlib.contextmanager
    def _env(self, root, modules, seed):
        saved_mods = {k: sys.modules.get(k) for k in modules}
        saved_env = {k: os.environ.get(k) for k in
                     ("GITHUB_RUN_ID", "CI_PIPELINE_ID", "INPUT_ASIN",
                      "FIRST_PARTY_PICKS_PER_RUN", "REWRITE_PICKS_PER_RUN")}
        cwd = os.getcwd()
        sys.modules.update(modules)
        for k in saved_env:
            os.environ.pop(k, None)
        os.environ["GITHUB_RUN_ID"] = os.environ["CI_PIPELINE_ID"] = seed
        os.chdir(root)
        try:
            with contextlib.redirect_stdout(open(os.devnull, "w", encoding="utf-8")):
                yield
        finally:
            os.chdir(cwd)
            for k, v in saved_mods.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v
            for k, v in saved_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def _both(self, seed="12345", **fixture):
        with tempfile.TemporaryDirectory() as tmp:
            root, gl, modules = self._fixture(tmp, **fixture)
            src = _wf03_pick_source().replace("/tmp/", (root / "tmp").as_posix() + "/")
            with self._env(root, modules, seed):
                exec(compile(src, "03-invoke-jules.yml:pick-asin", "exec"), {"__name__": "__wf03__"})
                with open(root / "tmp" / "shuffled.jsonl", encoding="utf-8") as f:
                    wf03 = [json.loads(line) for line in f if line.strip()]
                ours = repoless.pick_candidates(gl, budget=6)
        return wf03, ours

    def test_mixed_pools_match(self):
        fp = [_asin(60), _asin(1), _asin(61), _asin(62)]  # 1 は既存記事 → 除外、上限 2 件
        bands = {_asin(n): "zero" for n in (3, 60)}
        bands.update({_asin(31): "unfetched", _asin(33): "unfetched"})
        for seed in ("1", "12345", "37399969493"):
            with self.subTest(seed=seed):
                wf03, ours = self._both(seed, first_party=fp, bands=bands,
                                        hosts={_asin(33): 2})
                self.assertEqual(ours, wf03)
        pools = {o["pool"] for o in ours}
        self.assertTrue({"first-party", "ranking-sniper", "rewrite-queue",
                         "demand", "supply-random"} <= pools)
        picked = [o["asin"] for o in ours]
        self.assertEqual(picked[:2], [_asin(60), _asin(61)])  # first-party は zero でも残る
        self.assertNotIn(_asin(31), picked)  # unfetched + host 不足は defer
        self.assertIn(_asin(33), picked)  # unfetched でも host 2 件あれば残る

    def test_all_deferred_valve_ignores_first_party(self):
        bands = {_asin(n): "zero" for n in range(1, 60)}
        wf03, ours = self._both(first_party=[_asin(60)], bands=bands)
        self.assertEqual(ours, wf03)
        self.assertGreater(len(ours), 1)  # 安全弁で defer が無効化される

    def test_first_party_waits_for_amazon_data(self):
        # #9155: 商品データの無い first-party は待たせ、上限 2 件の枠は次の ASIN に回す。
        # miss だけ記録された snapshot (item 無し) もデータ無し扱い。
        fp = [_asin(60), _asin(61), _asin(62), _asin(63)]
        for seed in ("1", "12345"):
            with self.subTest(seed=seed):
                wf03, ours = self._both(seed, first_party=fp, no_amazon={_asin(60), _asin(62)})
                self.assertEqual(ours, wf03)
                fp_picked = [o["asin"] for o in ours if o["pool"] == "first-party"]
                self.assertEqual(fp_picked, [_asin(61), _asin(63)])
                self.assertNotIn(_asin(60), [o["asin"] for o in ours])

    def test_first_party_skips_genre_mismatch(self):
        # #9155: ジャンル不一致の first-party は外し、上限 2 件の枠は次の ASIN に回す。
        fp = [_asin(60), _asin(61), _asin(62), _asin(63)]
        for seed in ("1", "12345"):
            with self.subTest(seed=seed):
                wf03, ours = self._both(seed, first_party=fp, off_genre={_asin(60)},
                                        no_amazon={_asin(62)})
                self.assertEqual(ours, wf03)
                fp_picked = [o["asin"] for o in ours if o["pool"] == "first-party"]
                self.assertEqual(fp_picked, [_asin(61), _asin(63)])
                self.assertNotIn(_asin(60), [o["asin"] for o in ours])

    def test_without_first_party_pool_file(self):
        wf03, ours = self._both(first_party=None)
        self.assertEqual(ours, wf03)
        self.assertNotIn("first-party", {o["pool"] for o in ours})


class SelectCandidatesTest(unittest.TestCase):
    def test_scorer_failure_does_not_stop_pick(self):
        broken = types.SimpleNamespace(score_asin=lambda a: 1 / 0, should_defer=lambda r: True)
        with contextlib.redirect_stdout(open(os.devnull, "w", encoding="utf-8")):
            out = article_pick.select_candidates(
                [{"asin": _asin(1)}], set(), [], [], None, "s", broken)
        self.assertEqual(out, [{"asin": _asin(1), "pool": "", "source_keyword": None}])


class OriginLedgerTest(unittest.TestCase):
    def test_records_are_appended_and_mr_requested(self):
        calls = []
        orig_run = repoless.subprocess.run
        repoless.subprocess.run = lambda args, **kw: calls.append(args)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, "asin_origin.jsonl")
                origins = [{"asin": _asin(1), "pool": "ranking-sniper", "source_keyword": None},
                           {"asin": _asin(2), "pool": "demand", "source_keyword": "kw"}]
                repoless.record_origin_ledger(origins, path=path)
                repoless.record_origin_ledger(origins, path=path)  # 同じ run の再記録は足さない
                with open(path, encoding="utf-8") as f:
                    rows = [json.loads(line) for line in f]
        finally:
            repoless.subprocess.run = orig_run
        self.assertEqual([(r["asin"], r["pool"]) for r in rows],
                         [(_asin(1), "ranking-sniper"), (_asin(2), "demand")])
        self.assertEqual(rows[0]["workflow"], "invoke-jules-repoless")
        self.assertEqual(len(calls), 1)
        self.assertIn("create_data_mr.py", calls[0][1])

    def test_failure_is_swallowed(self):
        orig_run = repoless.subprocess.run

        def boom(*a, **kw):
            raise RuntimeError("push failed")
        repoless.subprocess.run = boom
        try:
            with tempfile.TemporaryDirectory() as tmp:
                repoless.record_origin_ledger(
                    [{"asin": _asin(1), "pool": "demand", "source_keyword": None}],
                    path=os.path.join(tmp, "a.jsonl"))
        finally:
            repoless.subprocess.run = orig_run


if __name__ == "__main__":
    unittest.main()
