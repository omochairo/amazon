"""#5490: 最安カードが未検証のときだけ出す明示警告のレンダリング検証。

背景 (2026-08-19 実測):

- `_recompute_best_price` は `verified` を見ないので、同一商品と確認できていない
  楽天/Yahoo の価格がそのまま「最安」になり、`is-cheapest` のパルス強調まで付く。
  配信 2,052 本のうち **197 本 (9.6%)** が「最安カードに確度低バッジ」だった。
- 既存の警告は `※確度低` バッジの `title` 属性 (ツールチップ) だけで、最安の
  強調に視線を持っていかれると気づかれない。

そこで **最安 かつ 未検証 かつ 検索リンクでない** ときに限り、可視の 1 行を出す。
条件を絞る理由は本文コメント参照。テストはこの 3 条件の AND を固定する。

レンダリングは Jinja テンプレ経由でしか確認できないため、
test_build_post_price_history_render.py と同じ subprocess smoke で検証する。
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
REPO_ROOT = THIS_DIR.parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"

_ASIN = "B0TEST00003"
NOTICE = "この最安値は同一商品と確認できていません"
BADGE = "price-badge--unverified"


def _article(yahoo: dict, amazon_price: int = 3000) -> dict:
    return {
        "slug": "2026-01-01-B0TEST00003",
        "title": "テスト商品3",
        "meta_description": "テスト",
        "date": "2026-01-01T00:00:00+09:00",
        "tags": [],
        "keywords": [],
        "product": {
            "asin": _ASIN,
            "brand": "Test",
            "name": "テスト商品3",
            "prices": {
                "amazon": {"price": amazon_price, "url": f"https://www.amazon.co.jp/dp/{_ASIN}/"},
                "rakuten": {"price": 0, "url": ""},
                "yahoo": yahoo,
            },
        },
        "narrative": {"lead": "", "why_this_product": "", "gift_appeal": "",
                      "daily_use": "", "safety_note": "", "closing": ""},
        "persona_fit": {},
        "faq": [],
    }


class UnverifiedCheapestNoticeTest(unittest.TestCase):
    def _render(self, article: dict) -> str:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = pathlib.Path(tmp.name)
        src, dst = root / "articles", root / "posts"
        src.mkdir()
        dst.mkdir()
        raw = root / "data" / "raw"
        (raw / "per_asin" / _ASIN).mkdir(parents=True)
        (raw / "amazon.json").write_text('{"items": []}', encoding="utf-8")
        (src / "2026-01-01-B0TEST00003.json").write_text(
            json.dumps(article, ensure_ascii=False), encoding="utf-8")
        cfg = root / "hugo"
        cfg.mkdir()
        (cfg / "config.toml").write_text(
            'baseURL = "https://example.com/"\n[params]\n  amazonPartnerTag = "chk01-22"\n',
            encoding="utf-8")
        (root / "scripts").mkdir(exist_ok=True)
        shutil.copytree(SCRIPTS_DIR / "templates", root / "scripts" / "templates")

        env = os.environ.copy()
        env.pop("GITHUB_STEP_SUMMARY", None)
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "build_post.py"),
             "--src", str(src) + os.sep, "--dst", str(dst) + os.sep,
             "--raw-amazon", str(raw / "amazon.json"),
             "--per-asin-root", str(raw / "per_asin")],
            cwd=str(root), env=env, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(proc.returncode, 0, msg=f"stdout={proc.stdout}\nstderr={proc.stderr}")
        out = list(dst.glob("*.md"))
        self.assertEqual(len(out), 1, msg=f"rendered files: {out}")
        return out[0].read_text(encoding="utf-8")

    def test_cheapest_and_unverified_shows_notice(self):
        md = self._render(_article({
            "price": 2000, "url": "https://store.shopping.yahoo.co.jp/x/1",
            "verified": False, "is_search": False,
        }))
        self.assertIn(BADGE, md, "前提: 確度低バッジは従来どおり出る")
        self.assertIn(NOTICE, md)

    def test_cheapest_but_verified_shows_no_notice(self):
        md = self._render(_article({
            "price": 2000, "url": "https://store.shopping.yahoo.co.jp/x/1",
            "verified": True, "is_search": False,
        }))
        self.assertNotIn(NOTICE, md)
        self.assertNotIn(BADGE, md)

    def test_unverified_but_not_cheapest_shows_badge_only(self):
        # Amazon の方が安い = Yahoo は最安ではない。バッジは出るが警告は出さない
        # (2 枚とも未検証の記事で警告だらけになると無視されるため、最安に絞る)。
        md = self._render(_article({
            "price": 5000, "url": "https://store.shopping.yahoo.co.jp/x/1",
            "verified": False, "is_search": False,
        }, amazon_price=3000))
        self.assertIn(BADGE, md)
        self.assertNotIn(NOTICE, md)

    def test_search_link_shows_no_notice(self):
        # 検索リンクはそもそも「同一商品」を主張していないので対象外。
        md = self._render(_article({
            "price": 2000, "url": "https://shopping.yahoo.co.jp/search?p=x",
            "verified": False, "is_search": True,
        }))
        self.assertNotIn(NOTICE, md)


if __name__ == "__main__":
    unittest.main()
