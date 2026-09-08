"""check_article_pr_deletions.py の unit tests (#6793)。"""
from __future__ import annotations

from scripts.check_article_pr_deletions import disallowed_deletions


def test_deletions_only_under_data_articles_are_allowed():
    assert disallowed_deletions([
        "data/articles/foo.json",
        "data/articles/bar.enrichment.json",
    ]) == []


def test_deletion_outside_data_articles_is_rejected():
    """#6788 の実ケース: 修正コミットが per_asin の sidecar を巻き添えで消した。"""
    assert disallowed_deletions([
        "data/raw/per_asin/B0DPHB7DMT/omcha_related.json",
    ]) == ["data/raw/per_asin/B0DPHB7DMT/omcha_related.json"]


def test_no_deletions_is_allowed():
    assert disallowed_deletions([]) == []


def test_mixed_allowed_and_disallowed_is_rejected():
    assert disallowed_deletions([
        "data/articles/foo.json",
        "data/raw/per_asin/B0DPHB7DMT/omcha_related.json",
    ]) == ["data/raw/per_asin/B0DPHB7DMT/omcha_related.json"]
