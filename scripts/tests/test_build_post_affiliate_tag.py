"""Regression test for #5087 — Amazon アフィリエイト・トラッキング ID の SSOT 化。

旧 ID ``zefiransesu-22`` が post.md.j2 / build_post.py にハードコードされており、
同一ページ内でリンクごとに ID が食い違っていた (#5087)。修正後は
``AMAZON_PARTNER_TAG`` secret (or Hugo 側は ``site.Params.amazonPartnerTag``) が
唯一の SSOT になるべきで、生成された Markdown に旧 ID や secret 経由でない
想定外の ``tag=`` が現れたら本テストが落ちる。

build_post.main() をサブプロセスで実行する点は test_build_post_manifest.py の
パターンを踏襲。
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest


THIS_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = THIS_DIR.parent.parent  # amazon-clone/
SCRIPTS_DIR = REPO_ROOT / "scripts"

# 文字列を分割して組み立てる: この定数の値そのものが `git grep -n 'zefiransesu-22'`
# (#5087 の検証コマンド。scripts/** も対象) に引っかかってしまい、退治したはずの
# 旧 ID がまだリポジトリに残っているという偽陽性を生むため。
_LEGACY_TAG = "zefiransesu" + "-22"

_ARTICLE = {
    "slug": "2026-01-01-B0TEST00002",
    "title": "テスト商品2 (B0TEST00002)",
    "product": {
        "asin": "B0TEST00002",
        "brand": "Test",
        "name": "テスト商品2",
        "best_price": 3000,
        "best_platform": "amazon",
        "prices": {
            "amazon": {"price": 3000, "url": "https://www.amazon.co.jp/dp/B0TEST00002"},
        },
    },
    "narrative": {"lead": "", "why_this_product": "", "gift_appeal": "",
                  "daily_use": "", "safety_note": "", "closing": ""},
    "persona_fit": {},
    "faq": [],
    "keywords": [],
    # affiliate_url マクロ (post.md.j2:16-24) は tag= を持たない amazon.co.jp URL
    # にだけ既定タグを付与する。ここでタグ無し URL を仕込んで経路を踏ませる。
    "sources": [
        {"name": "Amazon 商品ページ", "url": "https://www.amazon.co.jp/dp/B0TEST00002"},
    ],
    # competitor-card の Amazon CTA (post.md.j2:258) は c.url が無いとき
    # asin から既定タグ付き URL を組み立てる (build_post.py 側の url 直書きとは別経路)。
    "competitive_analysis": [
        {
            "asin": "B0TEST00003",
            "name": "類似商品",
            "image": "https://example.com/img.jpg",
            "internal_url": "/products/b0test00003/",
        },
    ],
}


class AffiliateTagSsotTests(unittest.TestCase):
    def _run_build(self, env_overrides: dict[str, str]) -> str:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tmp_path = pathlib.Path(tmp.name)
        src = tmp_path / "articles"
        dst = tmp_path / "posts"
        src.mkdir()
        dst.mkdir()
        data_dir = tmp_path / "data"
        per_asin_root = data_dir / "raw" / "per_asin"
        per_asin_root.mkdir(parents=True)
        (per_asin_root / "B0TEST00002").mkdir()
        raw = data_dir / "raw"
        (raw / "amazon.json").write_text('{"items": []}', encoding="utf-8")
        (src / "2026-01-01-B0TEST00002.json").write_text(
            json.dumps(_ARTICLE, ensure_ascii=False), encoding="utf-8"
        )

        cfg_dir = tmp_path / "hugo"
        cfg_dir.mkdir()
        (cfg_dir / "config.toml").write_text('baseURL = "https://example.com/"\n', encoding="utf-8")
        (tmp_path / "scripts").mkdir(exist_ok=True)
        shutil.copytree(SCRIPTS_DIR / "templates", tmp_path / "scripts" / "templates")

        env = os.environ.copy()
        env.pop("GITHUB_STEP_SUMMARY", None)
        env.pop("AMAZON_PARTNER_TAG", None)
        env["PYTHONIOENCODING"] = "utf-8"
        env.update(env_overrides)

        cmd = [
            sys.executable, str(SCRIPTS_DIR / "build_post.py"),
            "--src", str(src) + os.sep,
            "--dst", str(dst) + os.sep,
            "--raw-amazon", str(data_dir / "raw" / "amazon.json"),
            "--per-asin-root", str(per_asin_root),
        ]
        proc = subprocess.run(
            cmd, cwd=str(tmp_path), env=env,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        self.assertEqual(proc.returncode, 0, msg=f"stdout={proc.stdout}\nstderr={proc.stderr}")
        out_file = dst / "2026-01-01-B0TEST00002.md"
        self.assertTrue(out_file.exists(), msg=f"missing output. stdout={proc.stdout}")
        return out_file.read_text(encoding="utf-8")

    def test_env_partner_tag_is_the_only_tag_used(self):
        body = self._run_build({"AMAZON_PARTNER_TAG": "chk01-22"})
        self.assertNotIn(_LEGACY_TAG, body)
        tags = set(re.findall(r"[?&]tag=([A-Za-z0-9_-]+)", body))
        self.assertEqual(tags, {"chk01-22"}, msg=f"unexpected tag= values found: {tags}")

    def test_missing_secret_never_falls_back_to_legacy_tag(self):
        # #5087: secret 未設定時に旧 ID へ黙って落ちる作りにしない
        # (#4793 の「無言 skip で間違った値のまま緑になる」の再発防止)。
        body = self._run_build({})
        self.assertNotIn(_LEGACY_TAG, body)


if __name__ == "__main__":
    unittest.main()
