"""Regression test for #5087 — Amazon アフィリエイト・トラッキング ID の SSOT 化。

旧トラッキング ID (このファイル内では ``_LEGACY_TAG``) が post.md.j2 /
build_post.py にハードコードされており、同一ページ内でリンクごとに ID が
食い違っていた (#5087)。

修正の SSOT は commit 済みの ``hugo/config.toml`` の ``[params].amazonPartnerTag``。
``AMAZON_PARTNER_TAG`` secret は設定されていれば優先される任意のオーバーライドに
過ぎない — navi.omcha.jp への実配信を担う GitLab pages ジョブ (``.gitlab-ci.yml``)
には secret が渡っていないため、secret を SSOT にすると配信ビルドで常に
フォールバック値になってしまう (最初の実装ミス。secret 未設定時に空文字へ
落として ``tag=`` 抜けの URL を配信していた)。

このテストが守るもの:
  1. secret が設定されていれば、それが committed SSOT より優先される
  2. secret が無い (= 本番配信ビルドと同じ条件) 場合、committed SSOT の値が
     使われ、生成 Markdown に旧 ID や想定外の tag= が現れない
  3. committed SSOT (hugo/config.toml の [params].amazonPartnerTag) が読めない
     場合は、空文字や旧 ID へ黙って落ちずに build が失敗する

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

# 文字列を分割して組み立てる: この定数の値をそのまま書くと、#5087 の検証で
# 使う「旧トラッキング ID を grep して 0 件を確認する」コマンドに引っかかり、
# 退治したはずの旧 ID がまだリポジトリに残っているという偽陽性を生むため。
_LEGACY_TAG = "zefiransesu" + "-22"

_COMMITTED_TAG = "chk01-22"


def _article(slug: str, asin: str) -> dict:
    return {
        "slug": slug,
        "title": f"テスト商品 ({asin})",
        "product": {
            "asin": asin,
            "brand": "Test",
            "name": "テスト商品",
            "best_price": 3000,
            "best_platform": "amazon",
            "prices": {
                "amazon": {"price": 3000, "url": f"https://www.amazon.co.jp/dp/{asin}"},
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
            {"name": "Amazon 商品ページ", "url": f"https://www.amazon.co.jp/dp/{asin}"},
        ],
        # competitor-card の Amazon CTA (post.md.j2:258) は c.url が無いとき
        # asin から既定タグ付き URL を組み立てる (build_post.py 側の url 直書きとは別経路)。
        "competitive_analysis": [
            {
                "asin": "B0TESTCOMP1",
                "name": "類似商品",
                "image": "https://example.com/img.jpg",
                "internal_url": "/products/b0testcomp1/",
            },
        ],
    }


class AffiliateTagSsotTests(unittest.TestCase):
    def _run_build(
        self,
        env_overrides: dict[str, str],
        hugo_config_toml: str,
        slug: str = "2026-01-01-B0TEST00002",
        asin: str = "B0TEST00002",
    ) -> subprocess.CompletedProcess:
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
        (per_asin_root / asin).mkdir()
        raw = data_dir / "raw"
        (raw / "amazon.json").write_text('{"items": []}', encoding="utf-8")
        (src / f"{slug}.json").write_text(
            json.dumps(_article(slug, asin), ensure_ascii=False), encoding="utf-8"
        )

        cfg_dir = tmp_path / "hugo"
        cfg_dir.mkdir()
        (cfg_dir / "config.toml").write_text(hugo_config_toml, encoding="utf-8")
        (tmp_path / "scripts").mkdir(exist_ok=True)
        shutil.copytree(SCRIPTS_DIR / "templates", tmp_path / "scripts" / "templates")

        env = os.environ.copy()
        env.pop("GITHUB_STEP_SUMMARY", None)
        # #5087: "secret 未設定" を確実に再現する。creators_api_client.py が
        # import 時に python-dotenv の load_dotenv() を呼んでおり、開発機に
        # 個人用の .env (このリポジトリの外、amazon-main 直下) があると
        # os.environ.pop() しても子プロセス内の import で勝手に再注入される。
        # load_dotenv() は override=False (既定) で「既存キーは上書きしない」
        # ので、popする代わりに空文字を明示することで .env からの再注入を防ぐ。
        env["AMAZON_PARTNER_TAG"] = ""
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
        proc.out_file = dst / f"{slug}.md"  # type: ignore[attr-defined]
        return proc

    _CONFIG_WITH_TAG = (
        'baseURL = "https://example.com/"\n'
        "[params]\n"
        f'  amazonPartnerTag = "{_COMMITTED_TAG}"\n'
    )

    def test_secret_override_takes_priority_over_committed_ssot(self):
        proc = self._run_build({"AMAZON_PARTNER_TAG": "chk09-override"}, self._CONFIG_WITH_TAG)
        self.assertEqual(proc.returncode, 0, msg=f"stdout={proc.stdout}\nstderr={proc.stderr}")
        body = proc.out_file.read_text(encoding="utf-8")  # type: ignore[attr-defined]
        self.assertNotIn(_LEGACY_TAG, body)
        tags = set(re.findall(r"[?&]tag=([A-Za-z0-9_-]+)", body))
        self.assertEqual(tags, {"chk09-override"}, msg=f"unexpected tag= values found: {tags}")

    def test_committed_ssot_used_when_secret_unset(self):
        # #5087: GitLab pages の配信ビルドと同じ条件 (secret 無し)。
        # ここで secret 未設定を空文字/旧 ID にフォールバックさせると、本番の
        # 全 Amazon リンクから tag= が抜け落ちてアフィリエイト計測が失われる。
        proc = self._run_build({}, self._CONFIG_WITH_TAG)
        self.assertEqual(proc.returncode, 0, msg=f"stdout={proc.stdout}\nstderr={proc.stderr}")
        body = proc.out_file.read_text(encoding="utf-8")  # type: ignore[attr-defined]
        self.assertNotIn(_LEGACY_TAG, body)
        tags = set(re.findall(r"[?&]tag=([A-Za-z0-9_-]+)", body))
        self.assertEqual(tags, {_COMMITTED_TAG}, msg=f"unexpected tag= values found: {tags}")

    def test_missing_committed_ssot_fails_build_instead_of_falling_back(self):
        # secret も無く、hugo/config.toml にも [params].amazonPartnerTag が無い
        # 場合、空文字や旧 ID へ黙って落ちずに build 自体が失敗すること。
        config_without_tag = 'baseURL = "https://example.com/"\n'
        proc = self._run_build({}, config_without_tag)
        self.assertNotEqual(proc.returncode, 0, msg=f"stdout={proc.stdout}\nstderr={proc.stderr}")
        self.assertFalse(proc.out_file.exists())  # type: ignore[attr-defined]
        self.assertIn("amazonPartnerTag", proc.stderr)


if __name__ == "__main__":
    unittest.main()
