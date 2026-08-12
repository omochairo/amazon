"""quality_gate.py の「どこで買える/在庫」記事型検証 (#2686 / #4964)。

validate_article.py に一度実装されたが CI のどこからも呼ばれていなかった
死んだコードだったため、実際のゲート (scripts/quality_gate.py,
04-validate-article-pr.yml が --strict 付きで実行する) に移した。

Coverage:
1. タイトルが在庫系キーワードを含むのに、本文に取得日時付きの在庫記述が
   無ければ fail する。
2. 取得日時付きの在庫記述があれば pass する。
3. 実店舗の在庫を断定する表現を検出したら fail する。
4. state=unknown の記事に新型タイトルが付いていたら fail する。
5. 在庫系キーワードを含まないタイトルはチェック対象外 (pass)。
6. evaluate_article() 経由 (rendered markdown frontmatter からタイトル/asin
   を解決する経路) の統合テスト。
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import stock_status as ss  # noqa: E402
from quality_gate import (  # noqa: E402
    check_no_physical_store_claims,
    check_no_unknown_state_stock_title,
    check_stock_title_has_dated_conclusion,
    check_stock_where_to_buy,
)


# ---------------------------------------------------------------------------
# 1 / 2: 取得日時付き在庫記述の有無
# ---------------------------------------------------------------------------

def test_undated_stock_claim_fails():
    title = "テスト商品はどこで買える？在庫と価格を毎日チェック（Amazon）"
    body = "この商品は在庫あり、今すぐ購入できます。"
    violations = check_stock_title_has_dated_conclusion(title, body)
    assert violations
    assert any("dated-conclusion" in v for v in violations)


def test_dated_stock_claim_passes():
    title = "テスト商品はどこで買える？在庫と価格を毎日チェック（Amazon）"
    body = "2026-08-12 時点、Amazon に在庫あり ￥6,490。楽天・Yahoo では取扱を確認できませんでした。"
    violations = check_stock_title_has_dated_conclusion(title, body)
    assert violations == []


def test_date_present_but_no_stock_mention_fails():
    title = "テスト商品の在庫状況まとめ"
    body = "2026-08-12 時点、この商品はとても人気です。"
    violations = check_stock_title_has_dated_conclusion(title, body)
    assert violations
    assert any("stock-mention" in v for v in violations)


def test_non_stock_title_is_not_checked():
    title = "テスト商品のレビュー"
    body = "在庫あり。とても良い商品です。"
    violations = check_stock_title_has_dated_conclusion(title, body)
    assert violations == []


# ---------------------------------------------------------------------------
# 3: 実店舗の断定表現
# ---------------------------------------------------------------------------

def test_physical_store_claim_fails():
    body = "トイザらスで買えます。ぜひ店頭でご確認ください。"
    violations = check_no_physical_store_claims(body)
    assert violations
    assert any("physical-store-claim" in v for v in violations)


def test_physical_store_search_guidance_passes():
    body = "実店舗の在庫は各社の在庫検索でご確認ください（店舗の在庫を検索する →）。"
    violations = check_no_physical_store_claims(body)
    assert violations == []


def test_physical_store_name_without_claim_passes():
    # 店名が出ても「断定」語とセットでなければ許容 (例: 注記文の中の店名列挙)。
    body = "トイザらス・イオンなど実店舗の在庫データは持っていません。"
    violations = check_no_physical_store_claims(body)
    assert violations == []


# ---------------------------------------------------------------------------
# 4: state=unknown + 在庫系タイトル
# ---------------------------------------------------------------------------

def test_unknown_state_with_stock_title_fails():
    title = "テスト商品はどこで買える？在庫と価格を毎日チェック"
    violations = check_no_unknown_state_stock_title(title, ss.STATE_UNKNOWN)
    assert violations
    assert any("unknown-state" in v for v in violations)


def test_known_state_with_stock_title_passes():
    title = "テスト商品はどこで買える？在庫と価格を毎日チェック（Amazon）"
    violations = check_no_unknown_state_stock_title(title, ss.STATE_IN_STOCK)
    assert violations == []


def test_unknown_state_without_stock_title_passes():
    title = "テスト商品のレビュー"
    violations = check_no_unknown_state_stock_title(title, ss.STATE_UNKNOWN)
    assert violations == []


# ---------------------------------------------------------------------------
# check_stock_where_to_buy() 統合 (evaluate_article が呼ぶ経路)
# ---------------------------------------------------------------------------

_FRONTMATTER_TMPL = """---
title: "{title}"
url: "/products/{asin}/"
date: "2026-08-13T10:00:00+09:00"
---
{body}
"""


def _index(items: dict) -> ss.StockIndex:
    return ss.StockIndex(items=items, generated_at="2026-08-12T00:00:00+00:00", price_index={})


def test_check_passes_for_well_formed_stock_article():
    title = "テスト商品はどこで買える？在庫と価格を毎日チェック（Amazon）"
    body = (
        "2026-08-12 時点、Amazon に在庫あり ￥6,490。楽天・Yahoo では取扱を確認できませんでした。\n\n"
        "実店舗の在庫は各社の在庫検索でご確認ください。"
    )
    md_text = _FRONTMATTER_TMPL.format(title=title, asin="B0TESTASIN", body=body)
    idx = _index({"B0TESTASIN": {"avail": "在庫あり。"}})
    result = check_stock_where_to_buy({}, md_text, stock_index=idx)
    assert result.passed
    assert result.name == "stock_where_to_buy"


def test_check_catches_multiple_violations_together():
    title = "テスト商品はどこで買える？在庫と価格を毎日チェック"
    body = "在庫あり。トイザらスで買えます。"
    md_text = _FRONTMATTER_TMPL.format(title=title, asin="B0TESTASIN", body=body)
    # index に entry が無い = avail 欠落 = state unknown
    idx = _index({})
    result = check_stock_where_to_buy({}, md_text, stock_index=idx)
    assert not result.passed
    # dated-conclusion 欠落 + 実店舗断定 + unknown-state の3種すべて検出する
    assert result.message.count(";") >= 2


def test_check_no_markdown_no_stock_index_is_noop_for_non_stock_article():
    data = {"title": "テスト商品のレビュー"}
    result = check_stock_where_to_buy(data, None, stock_index=None)
    assert result.passed


def test_check_falls_back_to_json_title_when_md_missing():
    data = {"title": "テスト商品はどこで買える？在庫と価格を毎日チェック（Amazon）"}
    result = check_stock_where_to_buy(data, None, stock_index=None)
    assert not result.passed
    assert "dated-conclusion" in result.message
