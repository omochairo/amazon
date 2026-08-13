"""#5120 追補: price-history-block が実際に生成 markdown で1行に収まること、
checked_daily (「毎日巡回」表記) / change_count (「値動きをN回とらえました」)
文面、および観測点テーブル (<details>) が build_post.main() のエンドツーエンド
経路で正しく組み立つことを確認する。

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


def _make_article() -> dict:
    return {
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


class _RenderFixtureMixin:
    """build_post.py をサブプロセスで実走し、生成 markdown の price-history-block
    文字列 (対応する </div> まで) を取り出す共通ヘルパー。
    """

    def _run_and_extract_block(self, recs: list[dict], latest_items: dict | None) -> str:
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
        (per_asin_root / _ASIN).mkdir()
        raw = data_dir / "raw"
        (raw / "amazon.json").write_text('{"items": []}', encoding="utf-8")
        (src / "2026-01-01-B0TEST00002.json").write_text(
            json.dumps(_make_article(), ensure_ascii=False), encoding="utf-8"
        )

        price_history_root = data_dir / "price_history"
        price_history_root.mkdir(parents=True)
        with open(price_history_root / f"{_ASIN}.jsonl", "w", encoding="utf-8") as f:
            for r in recs:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

        price_watch_dir = data_dir / "price_watch"
        price_watch_dir.mkdir(parents=True)
        (price_watch_dir / "history").mkdir()
        if latest_items is not None:
            (price_watch_dir / "latest.json").write_text(
                json.dumps({"generated_at": "2026-08-13T00:00:00+00:00",
                            "items": latest_items,
                            "source": "creators-api", "stats": {}}, ensure_ascii=False),
                encoding="utf-8",
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
        content = out_files[0].read_text(encoding="utf-8")

        start = content.find('<div class="price-history-block">')
        self.assertNotEqual(start, -1, msg="price-history-block not rendered")

        # ブロック全体 (対応する閉じタグ </div> まで) を1行に収める。内部に
        # <div class="price-history-label"> が1つネストしているので、単純に
        # 最初の "</div>" では止まらず、開閉タグ数を数えて対応する終端を探す。
        depth = 0
        end = -1
        for m in re.finditer(r"<div\b|</div>", content[start:]):
            depth += -1 if m.group() == "</div>" else 1
            if depth == 0:
                end = start + m.end()
                break
        self.assertNotEqual(end, -1, msg="could not find matching closing </div>")
        block = content[start:end]
        self.assertNotIn("\n", block, msg=f"price-history-block spans multiple lines:\n{block}")
        return block


class PriceHistoryBlockRenderTest(_RenderFixtureMixin, unittest.TestCase):
    def test_checked_daily_with_changes(self):
        # 25日ギャップを1本含めて legend/dashed セグメントも一緒に踏む。
        # 1680 -> 1750 (変化) -> 1900 (変化) -> 1900 (変化なし) = change_count 2。
        recs = [_rec(25, 1680), _rec(20, 1750), _rec(5, 1900), _rec(0, 1900)]
        today = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        block = self._run_and_extract_block(recs, {_ASIN: {"p": 1900, "ts": today}})

        self.assertIn("毎日巡回", block)
        self.assertIn("最終確認", block)
        self.assertIn("値動きを2回とらえました", block)
        self.assertIn("破線＝未観測期間", block)  # 25日ギャップを1本含めたので凡例が出る
        self.assertIn('viewBox="0 0 300 90"', block)
        self.assertIn("<circle", block)
        self.assertIn("<title>", block)
        self.assertIn("<desc>", block)
        self.assertNotIn("回計測しました", block)  # 「計測回数」と誤読される言い方をしない

        # 観測点テーブル: <details> の中に古い順で全4行。
        details_start = block.find('<details class="price-history-table">')
        self.assertNotEqual(details_start, -1)
        self.assertIn("計測した4件の記録を表で見る", block)
        table_html = block[details_start:]
        rows = re.findall(r"<tr><td>([\d-]+)</td><td>¥([\d,]+)</td></tr>", table_html)
        self.assertEqual(len(rows), 4)
        dates = [r[0] for r in rows]
        self.assertEqual(dates, sorted(dates))  # 古い順
        self.assertEqual(rows[0][1], "1,680")
        self.assertEqual(rows[-1][1], "1,900")
        self.assertIn('<th scope="col">日付</th>', block)
        self.assertIn('<th scope="col">価格</th>', block)

    def test_checked_daily_without_changes(self):
        recs = [_rec(20, 1500), _rec(10, 1500), _rec(0, 1500)]
        today = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        block = self._run_and_extract_block(recs, {_ASIN: {"p": 1500, "ts": today}})

        self.assertIn("毎日巡回", block)
        self.assertIn("最終確認", block)
        self.assertIn("値動きはありませんでした", block)
        self.assertNotIn("とらえました", block)
        self.assertNotIn("回計測しました", block)

    def test_weekly_fallback_has_no_change_count_wording(self):
        # latest.json 無し -> checked_daily=False -> 従来どおり週次表現。
        recs = [_rec(20, 1000), _rec(10, 1100), _rec(0, 1200)]
        block = self._run_and_extract_block(recs, None)

        self.assertIn("週1巡回", block)
        self.assertIn("約週1回の巡回で記録した参考値です", block)
        self.assertNotIn("とらえました", block)
        self.assertNotIn("値動きは", block)
        self.assertNotIn("最終確認", block)

    def test_weekly_fallback_with_changes_still_no_change_count(self):
        # checked_daily=False のときは change_count が >0 でも「回数」に触れない。
        recs = [_rec(20, 1000), _rec(10, 1500), _rec(0, 1600)]
        block = self._run_and_extract_block(recs, None)

        self.assertIn("週1巡回", block)
        self.assertNotIn("とらえました", block)
        self.assertNotIn("値動きは", block)


if __name__ == "__main__":
    unittest.main()
