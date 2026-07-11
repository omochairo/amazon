"""Unit tests for #2370 — A-3 query cannibalization canonical consolidation
(data/analytics/canonical_overrides.json) -> canonicalURL front matter switch
in build_post.

_load_canonical_overrides() must degrade gracefully (missing/empty/malformed
file => empty dict) so pages without an override always keep their own
self-canonical (current/unchanged behavior). _frontmatter_meta() must only
set canonicalURL when the page's own /products/<asin>/ url is a key in the
override map.
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

THIS_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = THIS_DIR.parent.parent  # amazon-clone/
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from build_post import _frontmatter_meta, _load_canonical_overrides  # type: ignore[import-not-found]


class LoadCanonicalOverridesTests(unittest.TestCase):
    def test_missing_file_returns_empty_dict(self):
        self.assertEqual(_load_canonical_overrides(pathlib.Path("does/not/exist.json")), {})

    def test_malformed_json_returns_empty_dict(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "canonical_overrides.json"
            p.write_text("{not valid json", encoding="utf-8")
            self.assertEqual(_load_canonical_overrides(p), {})

    def test_override_is_loaded(self):
        payload = {
            "overrides": [
                {
                    "page": "https://navi.omcha.jp/products/b0gfvtpdbm/",
                    "canonical": "https://navi.omcha.jp/products/b0gl815n6k/",
                    "reason": "A-3 (#2370)",
                    "issue": 2370,
                },
            ]
        }
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "canonical_overrides.json"
            p.write_text(json.dumps(payload), encoding="utf-8")
            result = _load_canonical_overrides(p)
        self.assertEqual(result, {
            "/products/b0gfvtpdbm/": "https://navi.omcha.jp/products/b0gl815n6k/",
        })

    def test_rows_missing_page_or_canonical_are_skipped(self):
        payload = {"overrides": [
            {"page": "", "canonical": "https://navi.omcha.jp/products/b0gl815n6k/"},
            {"page": "https://navi.omcha.jp/products/b0gfvtpdbm/"},
            "not-a-dict",
        ]}
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "canonical_overrides.json"
            p.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(_load_canonical_overrides(p), {})

    def test_path_lookup_key_is_lowercased(self):
        payload = {"overrides": [
            {"page": "https://navi.omcha.jp/PRODUCTS/B0GFVTPDBM/",
             "canonical": "https://navi.omcha.jp/products/b0gl815n6k/"},
        ]}
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "canonical_overrides.json"
            p.write_text(json.dumps(payload), encoding="utf-8")
            result = _load_canonical_overrides(p)
        self.assertEqual(result, {"/products/b0gfvtpdbm/": "https://navi.omcha.jp/products/b0gl815n6k/"})


class FrontmatterMetaCanonicalOverrideTests(unittest.TestCase):
    def _meta(self, asin: str, canonical_overrides: dict[str, str] | None):
        data = {"title": "T", "product": {"asin": asin}, "date": "2026-05-29T10:00:00+09:00"}
        return _frontmatter_meta(
            data, f"2026-05-29-{asin}", False, {}, pathlib.Path(f"{asin}.json"),
            canonical_overrides=canonical_overrides,
        )

    def test_override_present_sets_canonical_url(self):
        overrides = {"/products/b0gfvtpdbm/": "https://navi.omcha.jp/products/b0gl815n6k/"}
        meta = self._meta("B0GFVTPDBM", overrides)
        self.assertEqual(meta["canonicalURL"], "https://navi.omcha.jp/products/b0gl815n6k/")

    def test_no_override_leaves_canonical_url_unset(self):
        overrides = {"/products/b0gfvtpdbm/": "https://navi.omcha.jp/products/b0gl815n6k/"}
        meta = self._meta("B0GL815N6K", overrides)
        self.assertNotIn("canonicalURL", meta)

    def test_empty_overrides_leaves_canonical_url_unset(self):
        meta = self._meta("B0GFVTPDBM", {})
        self.assertNotIn("canonicalURL", meta)

    def test_none_overrides_leaves_canonical_url_unset(self):
        meta = self._meta("B0GFVTPDBM", None)
        self.assertNotIn("canonicalURL", meta)


if __name__ == "__main__":
    unittest.main()
