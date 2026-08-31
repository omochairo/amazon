"""#5490 — リライト待ちを「候補として足す」経路のテスト。

**何が壊れていたか**: `eligible_rewrite_asins` は pick-asin の *除外* を外すだけで、
候補列そのものは `data/raw/amazon.json` の `items[]` から作られる。リライト対象は
「既に記事がある古い ASIN」なので普段そこに居らず、idle-fill が prepend しても
日次の 01-fetch-products が amazon.json を作り直して消す。実測 2026-08-31 で
マーカー 172 件・消化 0 件・候補プールに居るもの 0 件だった。

ここで固定する不変条件:
  A. `pending_rewrite_candidates` は「待っている・素材がある・生成側が defer しない」
     ものだけを、依頼が古い順 (FIFO) で返す
  B. 03-invoke-jules の inline pick-asin と 12-rewrite-idle-fill の配線が消えていない
     (ワークフロー YAML は単体テストの外なので、ここで最低限の網を張る)
"""
from __future__ import annotations

import ast
import json
import os
import pathlib
import re
import sys
import tempfile
import textwrap
import unittest

THIS_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = THIS_DIR.parent.parent  # amazon-clone/
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import rewrite_queue as rq  # type: ignore[import-not-found]


class PendingRewriteCandidatesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        self.articles = root / "articles"
        self.queue = root / "queue"
        self.per_asin = root / "per_asin"
        for d in (self.articles, self.queue, self.per_asin):
            d.mkdir()
        # is_generatable は score_per_asin_info を触るので既定で True に固定する。
        self._orig = rq.is_generatable
        rq.is_generatable = lambda asin: True  # type: ignore[assignment]
        self.addCleanup(self._restore)
        self.addCleanup(self.tmp.cleanup)

    def _restore(self):
        rq.is_generatable = self._orig  # type: ignore[assignment]

    def _marker(self, asin: str, old_slug: str, requested_at: str):
        (self.queue / f"{asin}.json").write_text(
            json.dumps({"asin": asin, "old_slug": old_slug, "requested_at": requested_at}),
            encoding="utf-8",
        )

    def _body(self, slug: str):
        (self.articles / f"{slug}.json").write_text("{}", encoding="utf-8")

    def _source(self, asin: str):
        d = self.per_asin / asin
        d.mkdir(parents=True, exist_ok=True)
        (d / "amazon.json").write_text("{}", encoding="utf-8")

    def _call(self, **kw):
        return rq.pending_rewrite_candidates(
            articles_dir=str(self.articles), queue_dir=str(self.queue),
            per_asin_root=str(self.per_asin), **kw)

    def test_returns_pending_in_requested_order(self):
        for asin, ts in (("B00000AAA1", "2026-08-20T00:00:00Z"),
                         ("B00000AAA2", "2026-08-10T00:00:00Z"),
                         ("B00000AAA3", "2026-08-15T00:00:00Z")):
            self._marker(asin, f"2026-05-01-{asin}", ts)
            self._body(f"2026-05-01-{asin}")
            self._source(asin)
        # FIFO。shuffle しないのは「毎回同じ 12 件を選び直して何も進まない」形に
        # 戻さないため。
        self.assertEqual(self._call(), ["B00000AAA2", "B00000AAA3", "B00000AAA1"])

    def test_drops_asin_whose_replacement_landed(self):
        self._marker("B00000AAA1", "2026-05-01-B00000AAA1", "2026-08-01T00:00:00Z")
        self._body("2026-05-01-B00000AAA1")
        self._body("2026-08-02-B00000AAA1")  # 新しい本体が着地した
        self._source("B00000AAA1")
        self.assertEqual(self._call(), [])

    def test_drops_asin_without_prompt_source(self):
        # build_jules_prompt.py は per_asin/<ASIN>/amazon.json が無いと
        # SystemExit する。生成に回しても必ず落ちるので候補にしない。
        self._marker("B00000AAA1", "2026-05-01-B00000AAA1", "2026-08-01T00:00:00Z")
        self._body("2026-05-01-B00000AAA1")
        self.assertEqual(self._call(), [])

    def test_drops_deferred_band(self):
        rq.is_generatable = lambda asin: asin != "B00000AAA2"  # type: ignore[assignment]
        for asin in ("B00000AAA1", "B00000AAA2"):
            self._marker(asin, f"2026-05-01-{asin}", "2026-08-01T00:00:00Z")
            self._body(f"2026-05-01-{asin}")
            self._source(asin)
        self.assertEqual(self._call(), ["B00000AAA1"])

    def test_limit_caps_the_result(self):
        for i, ts in enumerate(("2026-08-01T00:00:00Z", "2026-08-02T00:00:00Z",
                                "2026-08-03T00:00:00Z"), start=1):
            asin = f"B00000AAA{i}"
            self._marker(asin, f"2026-05-01-{asin}", ts)
            self._body(f"2026-05-01-{asin}")
            self._source(asin)
        self.assertEqual(self._call(limit=2), ["B00000AAA1", "B00000AAA2"])
        self.assertEqual(self._call(limit=0), [])

    def test_has_prompt_source(self):
        self._source("B00000AAA1")
        self.assertTrue(rq.has_prompt_source("B00000AAA1", str(self.per_asin)))
        self.assertFalse(rq.has_prompt_source("B00000AAA2", str(self.per_asin)))


class WorkflowWiringTests(unittest.TestCase):
    """配線が消えたら気づけるようにする (inline python はここ以外で走らない)。"""

    def _wf(self, name: str) -> str:
        return (REPO_ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")

    def test_pick_asin_inline_block_is_valid_python(self):
        src = self._wf("03-invoke-jules.yml")
        m = re.search(r"python3 - <<'PY'\n(.*?)\n\s*PY\n", src, re.S)
        self.assertIsNotNone(m, "inline pick-asin block not found")
        ast.parse(textwrap.dedent(m.group(1)))

    def test_pick_asin_adds_rewrite_candidates_before_keyword_pool(self):
        body = textwrap.dedent(
            re.search(r"python3 - <<'PY'\n(.*?)\n\s*PY\n",
                      self._wf("03-invoke-jules.yml"), re.S).group(1))
        self.assertIn("pending_rewrite_candidates", body)
        self.assertIn("REWRITE_PICKS_PER_RUN", body)
        # 候補列の順序: ranking -> rewrite -> keyword
        order = re.search(
            r"remaining\s*=\s*remaining_ranking\s*\+\s*rewrite_first\s*\+", body)
        self.assertIsNotNone(order, "rewrite_first must be spliced before remaining_kw")

    def test_idle_fill_caps_the_backlog(self):
        src = self._wf("12-rewrite-idle-fill.yml")
        self.assertIn("eligible_rewrite_asins", src)
        self.assertIn("REWRITE_QUEUE_CAP", src)

    def test_repoless_lane_uses_the_same_helper(self):
        src = (REPO_ROOT / "scripts" / "invoke_jules_repoless.py").read_text(encoding="utf-8")
        self.assertIn("pending_rewrite_candidates", src)
        self.assertIn("REWRITE_PICKS_PER_RUN", src)


if __name__ == "__main__":
    unittest.main()
