"""scripts/_seo_sidecar.py unit tests (#3332 N1)."""
from __future__ import annotations

import json
import pathlib
import tempfile
import unittest

from scripts._seo_sidecar import load_sidecar, sidecar_path, update_sidecar


class SidecarPathTest(unittest.TestCase):
    def test_builds_expected_path(self):
        p = sidecar_path(pathlib.Path("data/articles"), "2026-07-01-B0AAAAAAAA")
        self.assertEqual(p, pathlib.Path("data/articles/2026-07-01-B0AAAAAAAA.seo.json"))


class LoadSidecarTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_file_returns_empty_dict(self):
        self.assertEqual(load_sidecar(self.root / "nope.seo.json"), {})

    def test_malformed_json_returns_empty_dict(self):
        p = self.root / "bad.seo.json"
        p.write_text("{not valid json", encoding="utf-8")
        self.assertEqual(load_sidecar(p), {})

    def test_json_array_returns_empty_dict(self):
        p = self.root / "array.seo.json"
        p.write_text("[1, 2, 3]", encoding="utf-8")
        self.assertEqual(load_sidecar(p), {})

    def test_valid_json_round_trips(self):
        p = self.root / "ok.seo.json"
        p.write_text(json.dumps({"meta_description_optimized": "説明"}, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(load_sidecar(p), {"meta_description_optimized": "説明"})


class UpdateSidecarTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_creates_new_file(self):
        p = self.root / "new.seo.json"
        update_sidecar(p, {"meta_description_optimized": "新しい説明"})
        self.assertTrue(p.exists())
        self.assertEqual(load_sidecar(p), {"meta_description_optimized": "新しい説明"})

    def test_creates_parent_directories(self):
        p = self.root / "nested" / "dir" / "new.seo.json"
        update_sidecar(p, {"meta_description_optimized": "説明"})
        self.assertTrue(p.exists())

    def test_preserves_existing_keys_not_in_updates(self):
        p = self.root / "existing.seo.json"
        update_sidecar(p, {"faq_extended": [{"question": "Q", "answer": "A"}], "internal_link_suggestions": ["x"]})
        update_sidecar(p, {"meta_description_optimized": "追加された説明"})
        data = load_sidecar(p)
        self.assertEqual(data["internal_link_suggestions"], ["x"])
        self.assertEqual(data["meta_description_optimized"], "追加された説明")
        self.assertIn("faq_extended", data)

    def test_none_value_deletes_key(self):
        p = self.root / "delete_key.seo.json"
        update_sidecar(p, {"meta_description_optimized": "説明", "faq_extended": [{"question": "Q", "answer": "A"}]})
        update_sidecar(p, {"meta_description_optimized": None})
        data = load_sidecar(p)
        self.assertNotIn("meta_description_optimized", data)
        self.assertIn("faq_extended", data)

    def test_empty_result_deletes_file(self):
        p = self.root / "to_delete.seo.json"
        update_sidecar(p, {"meta_description_optimized": "説明"})
        self.assertTrue(p.exists())
        update_sidecar(p, {"meta_description_optimized": None})
        self.assertFalse(p.exists())

    def test_empty_result_on_nonexistent_file_does_not_create_it(self):
        p = self.root / "never_existed.seo.json"
        update_sidecar(p, {"meta_description_optimized": None})
        self.assertFalse(p.exists())

    def test_round_trip_does_not_escape_japanese(self):
        p = self.root / "japanese.seo.json"
        update_sidecar(p, {"meta_description_optimized": "日本語の説明文です"})
        raw = p.read_text(encoding="utf-8")
        self.assertIn("日本語の説明文です", raw)
        self.assertNotIn("\\u", raw)

    def test_written_file_ends_with_newline(self):
        p = self.root / "trailing_newline.seo.json"
        update_sidecar(p, {"meta_description_optimized": "説明"})
        raw = p.read_text(encoding="utf-8")
        self.assertTrue(raw.endswith("\n"))


if __name__ == "__main__":
    unittest.main()
