"""Smoke test for build_post manifest output (Issue #677).

Runs build_post.main() against a tiny synthetic fixture and asserts that
``data/build_manifest.json`` includes both ``by_asin`` records and the
``media_exposure_metrics`` summary from score_calculator.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest


THIS_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = THIS_DIR.parent.parent  # amazon-clone/
SCRIPTS_DIR = REPO_ROOT / "scripts"
TEMPLATE_SRC = SCRIPTS_DIR / "templates" / "post.md.j2"


_ARTICLE = {
    "slug": "2026-01-01-B0TEST00001",
    "title": "テスト商品 (B0TEST00001)",
    "product": {
        "asin": "B0TEST00001",
        "brand": "Test",
        "name": "テスト商品",
        "best_price": 3000,
    },
    "narrative": {"lead": "", "why_this_product": "", "gift_appeal": "",
                  "daily_use": "", "safety_note": "", "closing": ""},
    "persona_fit": {},
    "faq": [],
    "keywords": [],
}


class BuildManifestSmokeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = pathlib.Path(self.tmp.name)
        self.src = self.tmp_path / "articles"
        self.dst = self.tmp_path / "posts"
        self.src.mkdir()
        self.dst.mkdir()
        self.data_dir = self.tmp_path / "data"
        self.data_dir.mkdir()
        per_asin_root = self.data_dir / "raw" / "per_asin"
        per_asin_root.mkdir(parents=True)
        (per_asin_root / "B0TEST00001").mkdir()
        raw = self.data_dir / "raw"
        (raw / "amazon.json").write_text('{"items": []}', encoding="utf-8")
        (self.src / "2026-01-01-B0TEST00001.json").write_text(
            json.dumps(_ARTICLE, ensure_ascii=False), encoding="utf-8"
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_manifest_contains_by_asin_and_metrics(self):
        env = os.environ.copy()
        env.pop("GITHUB_STEP_SUMMARY", None)
        # build_post writes manifest to cwd/data/build_manifest.json, so run
        # from the tmp dir.
        cmd = [
            sys.executable, str(SCRIPTS_DIR / "build_post.py"),
            "--src", str(self.src) + os.sep,
            "--dst", str(self.dst) + os.sep,
            "--raw-amazon", str(self.data_dir / "raw" / "amazon.json"),
            "--per-asin-root", str(self.data_dir / "raw" / "per_asin"),
        ]
        # Provide a minimal hugo/config.toml in cwd so build_post doesn't error.
        # #5087: [params].amazonPartnerTag is the committed SSOT for the Amazon
        # affiliate tag; _resolve_amazon_partner_tag() hard-fails without it
        # (no secret is set in this test either, matching the GitLab pages
        # deploy build that has no secrets at all).
        cfg_dir = self.tmp_path / "hugo"
        cfg_dir.mkdir()
        (cfg_dir / "config.toml").write_text(
            'baseURL = "https://example.com/"\n'
            "[params]\n"
            '  amazonPartnerTag = "chk01-22"\n',
            encoding="utf-8",
        )
        # Copy templates dir so relative path scripts/templates/post.md.j2
        # resolves from cwd.
        (self.tmp_path / "scripts").mkdir(exist_ok=True)
        shutil.copytree(SCRIPTS_DIR / "templates", self.tmp_path / "scripts" / "templates")
        proc = subprocess.run(
            cmd, cwd=str(self.tmp_path), env=env,
            capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(proc.returncode, 0, msg=f"stdout={proc.stdout}\nstderr={proc.stderr}")
        manifest_path = self.tmp_path / "data" / "build_manifest.json"
        self.assertTrue(manifest_path.exists(), msg=f"missing manifest. stdout={proc.stdout}")
        m = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertIn("by_asin", m)
        self.assertIn("B0TEST00001", m["by_asin"])
        fallbacks = m["by_asin"]["B0TEST00001"]["fallbacks"]
        self.assertEqual(set(fallbacks), {"youtube", "news", "books", "omcha"})
        for v in fallbacks.values():
            self.assertIn(v, {"primary", "fallback_used", "missing"})
        self.assertIn("media_exposure_metrics", m["summary"])
        me = m["summary"]["media_exposure_metrics"]
        self.assertGreaterEqual(me["total"], 1)
        # Empty per-asin dir -> all missing (file 不在)
        self.assertEqual(me["yt_missing"], me["total"])


if __name__ == "__main__":
    unittest.main()
