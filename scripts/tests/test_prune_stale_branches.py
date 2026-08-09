"""Unit tests for #4765 follow-up — 滞留ブランチの段階的削除。

このスクリプトはブランチを消すので、守るべき不変条件は「消してはいけないものを
選ばない」側にある:
  - jules-lock/* は Jules の運用ロック。消すと排他が壊れる。
  - open PR の head は作業中。
  - PR を持たないブランチは素性が分からないので触らない。
  - 直近のブランチは触らない (min-age)。
"""
from __future__ import annotations

import pathlib
import sys
from datetime import datetime, timedelta, timezone

THIS_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = THIS_DIR.parent.parent  # amazon-clone/
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from prune_stale_branches import PROTECT_PREFIXES, select_targets  # type: ignore[import-not-found]

NOW = datetime(2026, 8, 9, tzinfo=timezone.utc)
CUTOFF = NOW - timedelta(days=7)
OLD = NOW - timedelta(days=90)
RECENT = NOW - timedelta(days=1)


def _sel(branches, deletable, open_heads=frozenset(), limit=100, cutoff=CUTOFF):
    return [n for n, _ in select_targets(branches, set(deletable), set(open_heads),
                                         cutoff, limit)]


def test_merged_branch_is_selected():
    assert _sel([("feat/a", OLD)], {"feat/a"}) == ["feat/a"]


def test_jules_lock_is_never_selected():
    """運用ロックは PR が付いていても消さない (保険の PROTECT_PREFIXES)。"""
    assert "jules-lock/" in PROTECT_PREFIXES
    assert _sel([("jules-lock/x", OLD)], {"jules-lock/x"}) == []


def test_gitlab_prefix_is_protected():
    assert _sel([("gitlab/add-article-B0X", OLD)], {"gitlab/add-article-B0X"}) == []


def test_branch_without_pr_is_skipped():
    """PR が無いブランチは素性が分からないので触らない。"""
    assert _sel([("mystery/x", OLD)], set()) == []


def test_open_pr_head_is_skipped():
    assert _sel([("feat/wip", OLD)], {"feat/wip"}, open_heads={"feat/wip"}) == []


def test_recent_branch_is_skipped():
    assert _sel([("feat/new", RECENT)], {"feat/new"}) == []


def test_limit_caps_the_batch_and_keeps_oldest_first():
    branches = [("feat/{}".format(i), OLD + timedelta(days=i)) for i in range(5)]
    got = _sel(branches, {b for b, _ in branches}, limit=2)
    assert got == ["feat/0", "feat/1"]


def test_mixed_set_selects_only_the_safe_ones():
    branches = [
        ("jules-lock/a", OLD),
        ("feat/merged", OLD),
        ("feat/open", OLD),
        ("mystery/b", OLD),
        ("feat/recent", RECENT),
    ]
    deletable = {"feat/merged", "feat/open", "feat/recent"}
    assert _sel(branches, deletable, open_heads={"feat/open"}) == ["feat/merged"]


def test_main_is_not_selected_even_if_listed():
    assert _sel([("main", OLD)], {"main"}) == []
