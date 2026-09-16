"""scripts/experimental/search_query_trial/domains.py の単体テスト (#4841 V2)。"""
from __future__ import annotations

import unittest

from scripts.analyze_third_party_yield import HOST_CATEGORIES
from scripts.experimental.search_query_trial.domains import (
    PERSONAL_BLOG_DOMAINS,
    build_q2_include_domains,
)


class BuildQ2IncludeDomainsTest(unittest.TestCase):
    def test_includes_all_blog_service_domains(self):
        result = set(build_q2_include_domains())
        self.assertTrue(set(HOST_CATEGORIES["blog"]).issubset(result))

    def test_includes_fixed_personal_blog_domains(self):
        result = set(build_q2_include_domains())
        self.assertTrue(set(PERSONAL_BLOG_DOMAINS).issubset(result))

    def test_personal_blog_domains_is_small_and_fixed(self):
        # 着手前に固定した2件のみ (V1 の snippet_source_hosts を個別に fetch して
        # 確認した結果、個人ブログと確認できたのはこの2件だけだった)
        self.assertEqual(set(PERSONAL_BLOG_DOMAINS), {"niko-shufublog.com", "oyakame.com"})

    def test_result_has_no_duplicates_and_is_sorted(self):
        result = build_q2_include_domains()
        self.assertEqual(result, sorted(set(result)))


if __name__ == "__main__":
    unittest.main()
