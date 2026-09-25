"""テストの無かった 3 スクリプトのガード。

1. fetch_rakuten._write_search_items: Search 0 件で rakuten.json を空上書きしない
2. build_og_image._build_all: `*.seo.json` サイドカーを記事として扱わない
3. generate_term_stubs._write_stub_if_missing: `"` を含む用語でも front matter が壊れない
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

import yaml

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.dirname(THIS_DIR)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import build_og_image  # noqa: E402
import fetch_rakuten  # noqa: E402
import generate_term_stubs  # noqa: E402


class TestWriteSearchItems(unittest.TestCase):
    def test_empty_items_preserve_previous_file(self):
        with tempfile.TemporaryDirectory() as d:
            out = pathlib.Path(d)
            prev = {"keyword": "知育玩具", "items": [{"itemCode": "shop:1"}]}
            (out / "rakuten.json").write_text(json.dumps(prev), encoding="utf-8")
            self.assertFalse(fetch_rakuten._write_search_items(out, "知育玩具", []))
            self.assertEqual(json.loads((out / "rakuten.json").read_text(encoding="utf-8")), prev)

    def test_non_empty_items_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            out = pathlib.Path(d)
            (out / "rakuten.json").write_text('{"keyword": "x", "items": []}', encoding="utf-8")
            items = [{"itemCode": "shop:2"}]
            self.assertTrue(fetch_rakuten._write_search_items(out, "知育玩具", items))
            data = json.loads((out / "rakuten.json").read_text(encoding="utf-8"))
            self.assertEqual(data, {"keyword": "知育玩具", "items": items})


class TestBuildAllSkipsSidecars(unittest.TestCase):
    def test_seo_sidecar_is_not_treated_as_article(self):
        with tempfile.TemporaryDirectory() as d:
            art = pathlib.Path(d)
            (art / "2026-01-01-B0AAAAAAAA.json").write_text("{}", encoding="utf-8")
            (art / "2026-01-01-B0AAAAAAAA.seo.json").write_text("{}", encoding="utf-8")
            with mock.patch.object(build_og_image, "ARTICLES_DIR", art), \
                    mock.patch.object(build_og_image, "_build_one", return_value=0) as one:
                rc = build_og_image._build_all(force=False)
            self.assertEqual(rc, 0)
            self.assertEqual([c.args[0] for c in one.call_args_list], ["B0AAAAAAAA"])


class TestStubTitleEscaping(unittest.TestCase):
    def _front_matter(self, title: str) -> dict:
        with tempfile.TemporaryDirectory() as d:
            base = pathlib.Path(d)
            self.assertEqual(
                generate_term_stubs._write_stub_if_missing(base, "slug", title, dry_run=False),
                "created",
            )
            text = (base / "slug" / "_index.md").read_text(encoding="utf-8")
        return yaml.safe_load(text.split("---")[1])

    def test_plain_title_unchanged(self):
        self.assertEqual(self._front_matter("知育玩具"), {"title": "知育玩具"})

    def test_title_with_quote_and_backslash_round_trips(self):
        title = 'LEGO "Classic" 3\\4'
        self.assertEqual(self._front_matter(title), {"title": title})


if __name__ == "__main__":
    unittest.main()
