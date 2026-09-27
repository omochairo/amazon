"""楽天テキスト検索のキーワード整形のテスト。

楽天 Ichiba 商品検索は、半角 1 文字の語 ('X') や記号だけの語 ('-' '&' '、') が
1 つでも混ざると HTTP 400 'keyword is not valid' を返す。01-fetch-products の
実行ログで Stage2 が失敗したキーワードを使い、送る前に除かれることを pin する。
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.dirname(THIS_DIR)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import fetch_cross_search  # noqa: E402
from fetch_cross_search import _sanitize_rakuten_keyword  # noqa: E402


class SanitizeRakutenKeywordTest(unittest.TestCase):
    def test_drops_rejected_tokens(self):
        cases = {
            "Playmobil X F-150": "Playmobil F-150",
            "ベイブレード X 2-60HN": "ベイブレード 2-60HN",
            "GraviTrax - Action-Set M": "GraviTrax Action-Set",
            "Melissa & Doug Pattern": "Melissa Doug Pattern",
            "PlanToys® 木製ベリー&シトラスバスケット | モンテッソーリ":
                "PlanToys® 木製ベリー&シトラスバスケット モンテッソーリ",
            "BANDAI ディズニー Peek a": "BANDAI ディズニー Peek",
            "［ ］ キッチンサイエンスセット": "キッチンサイエンスセット",
            "4~ のお子様向け水彩絵本付きペイント、 、": "4~ のお子様向け水彩絵本付きペイント、",
            "- STEAM 恐竜の世界": "STEAM 恐竜の世界",
            "XT60 -": "XT60",
        }
        for src, want in cases.items():
            with self.subTest(src=src):
                self.assertEqual(_sanitize_rakuten_keyword(src), want)

    def test_keeps_valid_keywords(self):
        for kw in ["タカラトミー トミカ No.1", "LEGO デュプロ 10475", "ボーネルンド ジャンボリーミニ",
                   "くもん 日本地図パズル", "木 積み木"]:
            with self.subTest(kw=kw):
                self.assertEqual(_sanitize_rakuten_keyword(kw), kw)

    def test_all_rejected_returns_empty(self):
        self.assertEqual(_sanitize_rakuten_keyword("- & X"), "")


class SearchRakutenTieredSanitizeTest(unittest.TestCase):
    def _mk_resp(self, status, payload):
        m = MagicMock()
        m.status_code = status
        m.json.return_value = payload
        m.text = ""
        return m

    def test_text_stages_send_sanitized_keyword(self):
        empty = self._mk_resp(200, {"Items": []})
        with patch.object(fetch_cross_search.requests, "get", return_value=empty) as mock_get, \
             patch.object(fetch_cross_search.time, "sleep"):
            fetch_cross_search.search_rakuten_tiered("Playmobil X Ferrari GTO", app_id="dummy")
        sent = [c.kwargs["params"].get("keyword") for c in mock_get.call_args_list]
        # Books → Ichiba → 短縮 Ichiba の 3 回とも 'X' が落ちている
        self.assertEqual(sent, ["Playmobil Ferrari GTO", "Playmobil Ferrari GTO", "Playmobil Ferrari"])

    def test_no_text_search_when_nothing_left(self):
        with patch.object(fetch_cross_search.requests, "get") as mock_get:
            result = fetch_cross_search.search_rakuten_tiered("- 、", app_id="dummy")
        self.assertIsNone(result)
        mock_get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
