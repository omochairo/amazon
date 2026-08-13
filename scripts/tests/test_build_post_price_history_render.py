"""#5120 追補: price-history-block が実際に生成 markdown で1行に収まること、
および checked_daily (「毎日巡回」表記) が build_post.main() のエンドツーエンド
経路で latest.json から正しく配線されることを確認する。

price-history-block はインライン HTML としてテンプレに埋め込まれているため、
改行が混ざるとブロックの外側 (markdown の地の文パーサ) に解釈されてしまい
描画が壊れる。ここでは _attach_price_history / _build_price_history_spark の
単体テストでは検出できない「Jinja レンダリング後の実際の1行性」を、
test_build_post_manifest.py と同じ subprocess 経由の smoke test で検証する。
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
from datetime import datetime, timedelta, timezone

THIS_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = THIS_DIR.parent.parent  # amazon-clone/
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _rec(days_ago: int, price: int) -> dict:
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    return {"ts": ts, "source": "amazon", "price": price, "availability": None}


_ASIN = "B0TEST00002"

_ARTICLE = {
    "slug": "2026-01-01-B0TEST00002",
    "title": "テスト商品2 (B0TEST00002)",
    "product": {
        "asin": _ASIN,
        "brand": "Test",
        "name": "テスト商品2",
        "best_price": 1900,
        "best_platform": "amazon",
        # post.md.j2 の price-card が product.prices.amazon.* に直接アクセス
        # するため、最小限のスキーマを満たす (テンプレレンダリングを実際に
        # 通す必要があるのは #5120 の1行性検証がレンダリング後の markdown を
        # 見るため)。
        "prices": {
            "amazon": {
                "price": 1900,
                "url": "https://www.amazon.co.jp/dp/B0TEST00002",
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
                  "daily_use": "", "safety_note": "", "closing": ""},
    "persona_fit": {},
    "faq": [],
    "keywords": [],
}


class PriceHistoryBlockRenderTest(unittest.TestCase):
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
        (per_asin_root / _ASIN).mkdir()
        raw = self.data_dir / "raw"
        (raw / "amazon.json").write_text('{"items": []}', encoding="utf-8")
        (self.src / "2026-01-01-B0TEST00002.json").write_text(
            json.dumps(_ARTICLE, ensure_ascii=False), encoding="utf-8"
        )

        # #5120: 価格記録ゲートを通す jsonl (>=3点・スパン>=14日・14日超ギャップ
        # を1本含めて legend/dashed セグメントも一緒に踏む)。
        price_history_root = self.data_dir / "price_history"
        price_history_root.mkdir(parents=True)
        recs = [_rec(25, 1680), _rec(20, 1750), _rec(5, 1900), _rec(0, 1900)]
        with open(price_history_root / f"{_ASIN}.jsonl", "w", encoding="utf-8") as f:
            for r in recs:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

        # #5120 追補: latest.json に当該 ASIN を載せて checked_daily=True を踏む。
        price_watch_dir = self.data_dir / "price_watch"
        price_watch_dir.mkdir(parents=True)
        (price_watch_dir / "history").mkdir()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        (price_watch_dir / "latest.json").write_text(
            json.dumps({"generated_at": today,
                        "items": {_ASIN: {"p": 1900, "ts": today}},
                        "source": "creators-api", "stats": {}}, ensure_ascii=False),
            encoding="utf-8",
        )

        cfg_dir = self.tmp_path / "hugo"
        cfg_dir.mkdir()
        (cfg_dir / "config.toml").write_text(
            'baseURL = "https://example.com/"\n'
            "[params]\n"
            '  amazonPartnerTag = "chk01-22"\n',
            encoding="utf-8",
        )
        (self.tmp_path / "scripts").mkdir(exist_ok=True)
        shutil.copytree(SCRIPTS_DIR / "templates", self.tmp_path / "scripts" / "templates")

    def tearDown(self):
        self.tmp.cleanup()

    def test_price_history_block_is_single_line_and_checked_daily(self):
        env = os.environ.copy()
        env.pop("GITHUB_STEP_SUMMARY", None)
        cmd = [
            sys.executable, str(SCRIPTS_DIR / "build_post.py"),
            "--src", str(self.src) + os.sep,
            "--dst", str(self.dst) + os.sep,
            "--raw-amazon", str(self.data_dir / "raw" / "amazon.json"),
            "--per-asin-root", str(self.data_dir / "raw" / "per_asin"),
        ]
        proc = subprocess.run(
            cmd, cwd=str(self.tmp_path), env=env,
            capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(proc.returncode, 0, msg=f"stdout={proc.stdout}\nstderr={proc.stderr}")

        out_files = list(self.dst.glob("*.md"))
        self.assertEqual(len(out_files), 1, msg=f"dst listing: {list(self.dst.iterdir())}")
        content = out_files[0].read_text(encoding="utf-8")

        start = content.find('<div class="price-history-block">')
        self.assertNotEqual(start, -1, msg="price-history-block not rendered")

        # ブロック全体 (対応する閉じタグ </div> まで) を1行に収める。内部に
        # <div class="price-history-label"> が1つネストしているので、単純に
        # 最初の "</div>" では止まらず、開閉タグ数を数えて対応する終端を探す。
        depth = 0
        pos = start
        end = -1
        for m in re.finditer(r"<div\b|</div>", content[start:]):
            if m.group() == "</div>":
                depth -= 1
            else:
                depth += 1
            if depth == 0:
                end = start + m.end()
                break
        self.assertNotEqual(end, -1, msg="could not find matching closing </div>")
        block = content[start:end]
        self.assertNotIn("\n", block, msg=f"price-history-block spans multiple lines:\n{block}")

        # #5120 追補: latest.json に ASIN があるので「毎日巡回」表記になる。
        self.assertIn("毎日巡回", block)
        self.assertIn("最終確認", block)
        self.assertIn("破線＝未観測期間", block)  # 25日ギャップを1本含めたので凡例が出る
        self.assertIn('viewBox="0 0 300 90"', block)
        self.assertIn("<circle", block)
        self.assertIn("<title>", block)
        self.assertIn("<desc>", block)


if __name__ == "__main__":
    unittest.main()
