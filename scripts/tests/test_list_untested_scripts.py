"""list_untested_scripts の単体テスト (#8272 項目4)。"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import unittest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.dirname(THIS_DIR)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import list_untested_scripts  # noqa: E402


class TestFindUntested(unittest.TestCase):
    def test_classifies_tested_untested_and_callers(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            (root / "scripts" / "tests").mkdir(parents=True)
            (root / ".github" / "workflows").mkdir(parents=True)
            (root / "scripts" / "tested.py").write_text("x = 1\n", encoding="utf-8")
            (root / "scripts" / "used.py").write_text("a = 1\nb = 2\n", encoding="utf-8")
            (root / "scripts" / "used_ext.py").write_text("c = 1\n", encoding="utf-8")
            (root / "scripts" / "orphan.py").write_text("d = 1\n", encoding="utf-8")
            (root / "scripts" / "tests" / "test_x.py").write_text("import tested\n", encoding="utf-8")
            (root / ".github" / "workflows" / "w.yml").write_text(
                "run: python scripts/used.py\n", encoding="utf-8")
            (root / "scripts" / "caller.sh").write_text("python scripts/used.py\n", encoding="utf-8")
            (root / "scripts" / "helper.py").write_text("import used_ext\n", encoding="utf-8")
            # used_ext は used という別名の部分文字列を含むが、単語一致で区別する

            rows = {r["path"]: r for r in list_untested_scripts.find_untested(root)}

            self.assertNotIn("scripts/tested.py", rows)
            self.assertEqual(rows["scripts/used.py"]["used_by"],
                             [".github/workflows/w.yml", "scripts/caller.sh"])
            self.assertEqual(rows["scripts/used.py"]["lines"], 2)
            self.assertEqual(rows["scripts/used_ext.py"]["used_by"], ["scripts/helper.py"])
            self.assertEqual(rows["scripts/orphan.py"]["used_by"], [])
            # 行数の多い順
            self.assertEqual(next(iter(rows)), "scripts/used.py")


if __name__ == "__main__":
    unittest.main()
