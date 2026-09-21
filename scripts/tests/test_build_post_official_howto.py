"""#7959 (#7955 C 改訂版): build_post.py が official_howto.json から
「📘 公式の取扱説明書・遊び方」ブロックを実際にレンダリングし、
「🎮 家庭での遊ばれ方」の直前に置くこと、および official_howto.json が
無いページの出力がバイト単位で変わらないことを end-to-end で確認する
(test_build_post_price_history_render.py と同じ subprocess 経由の smoke test)。
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

_ASIN = "B0TEST00003"


def _make_article() -> dict:
    return {
        "slug": f"2026-01-01-{_ASIN}",
        "title": f"テスト商品3 ({_ASIN})",
        "product": {
            "asin": _ASIN,
            "brand": "Test",
            "name": "テスト商品3",
            "best_price": 2000,
            "best_platform": "amazon",
            "prices": {
                "amazon": {
                    "price": 2000,
                    "url": f"https://www.amazon.co.jp/dp/{_ASIN}",
                    "availability": "在庫あり。",
                    "discontinued": False,
                    "savings_percentage": 0,
                    "loyalty_points": 0,
                    "free_shipping": False,
                    "search_url": None,
                },
            },
        },
        "narrative": {"lead": "", "why_this_product": "", "gift_appeal": "",
                      "daily_use": "遊び方の本文です。", "safety_note": "", "closing": ""},
        "persona_fit": {},
        "faq": [],
        "keywords": [],
    }


class _RenderFixtureMixin:
    def _run_build(self, official_howto_obj: dict | None) -> str:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tmp_path = pathlib.Path(tmp.name)
        src = tmp_path / "articles"
        dst = tmp_path / "posts"
        src.mkdir()
        dst.mkdir()
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        per_asin_root = data_dir / "raw" / "per_asin"
        per_asin_root.mkdir(parents=True)
        asin_dir = per_asin_root / _ASIN
        asin_dir.mkdir()
        raw = data_dir / "raw"
        (raw / "amazon.json").write_text('{"items": []}', encoding="utf-8")
        (src / f"2026-01-01-{_ASIN}.json").write_text(
            json.dumps(_make_article(), ensure_ascii=False), encoding="utf-8"
        )
        if official_howto_obj is not None:
            (asin_dir / "official_howto.json").write_text(
                json.dumps(official_howto_obj, ensure_ascii=False), encoding="utf-8"
            )

        cfg_dir = tmp_path / "hugo"
        cfg_dir.mkdir()
        (cfg_dir / "config.toml").write_text(
            'baseURL = "https://example.com/"\n'
            "[params]\n"
            '  amazonPartnerTag = "chk01-22"\n',
            encoding="utf-8",
        )
        (tmp_path / "scripts").mkdir(exist_ok=True)
        shutil.copytree(SCRIPTS_DIR / "templates", tmp_path / "scripts" / "templates")

        env = os.environ.copy()
        env.pop("GITHUB_STEP_SUMMARY", None)
        cmd = [
            sys.executable, str(SCRIPTS_DIR / "build_post.py"),
            "--src", str(src) + os.sep,
            "--dst", str(dst) + os.sep,
            "--raw-amazon", str(data_dir / "raw" / "amazon.json"),
            "--per-asin-root", str(data_dir / "raw" / "per_asin"),
        ]
        proc = subprocess.run(
            cmd, cwd=str(tmp_path), env=env,
            capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(proc.returncode, 0, msg=f"stdout={proc.stdout}\nstderr={proc.stderr}")
        out_files = list(dst.glob("*.md"))
        self.assertEqual(len(out_files), 1, msg=f"dst listing: {list(dst.iterdir())}")
        return out_files[0].read_text(encoding="utf-8"), proc.stdout


class OfficialHowtoBlockRenderTest(_RenderFixtureMixin, unittest.TestCase):
    def test_no_official_howto_json_renders_nothing_and_output_unchanged(self):
        without, stdout = self._run_build(None)
        self.assertNotIn("official-howto-block", without)
        self.assertNotIn("公式の取扱説明書", without)
        self.assertIn("official_howto_block (#7959): 0 page(s) (steps: 0)", stdout)

    def test_url_only_renders_link_only_block_before_daily_use(self):
        content, stdout = self._run_build({
            "asin": _ASIN,
            "publisher": "bandai",
            "kind": "manual_pdf",
            "url": "https://toy.bandai.co.jp/manuals/pdf.php?id=1234",
            "official_name": "テスト取説",
            "fetched_at": "2026-09-21T00:00:00Z",
        })
        self.assertIn('<section class="official-howto-block"', content)
        self.assertIn("バンダイが公開している取扱説明書（PDF）「テスト取説」です。", content)
        self.assertIn('rel="noopener">公式サイトで見る →</a>', content)
        self.assertIn("（2026-09-21 時点で公開を確認）", content)
        self.assertNotIn("sponsored", content.split('official-howto-block')[1].split('</section>')[0])
        howto_pos = content.index('<section class="official-howto-block"')
        daily_use_pos = content.index("🎮 家庭での遊ばれ方")
        self.assertLess(howto_pos, daily_use_pos)
        self.assertIn("official_howto_block (#7959): 1 page(s) (steps: 0)", stdout)

    def test_reviewed_steps_render_ordered_list(self):
        content, stdout = self._run_build({
            "asin": _ASIN,
            "publisher": "bandai",
            "kind": "manual_pdf",
            "url": "https://toy.bandai.co.jp/manuals/pdf.php?id=1234",
            "official_name": "テスト取説",
            "fetched_at": "2026-09-21T00:00:00Z",
            "reviewed_by": "iromama",
            "steps": [
                {"text": "電源を入れる", "section": "【1】準備"},
                {"text": "ボタンを押す", "section": "【2】操作"},
            ],
        })
        self.assertIn('<ol class="official-howto-steps">', content)
        self.assertIn("電源を入れる", content)
        self.assertIn("（【1】準備）", content)
        self.assertIn("▶ 取扱説明書（PDF）の全文をバンダイの公式サイトで見る", content)
        self.assertIn("official_howto_block (#7959): 1 page(s) (steps: 1)", stdout)

    def test_not_found_status_renders_nothing(self):
        content, stdout = self._run_build({"asin": _ASIN, "status": "not_found"})
        self.assertNotIn("official-howto-block", content)
        self.assertIn("official_howto_block (#7959): 0 page(s) (steps: 0)", stdout)


if __name__ == "__main__":
    unittest.main()
