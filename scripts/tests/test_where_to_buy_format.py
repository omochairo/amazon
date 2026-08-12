"""Unit tests for where_to_buy_format.py (#2686 / #4964)。

Coverage:
1. 5 state それぞれの結論ブロック文面。
2. Amazon のみ取扱ありのときタイトル括弧が「（Amazon）」に縮む。
3. ロールアウト日より前の記事は is_stock_format_eligible が False (既存記事保護)。
4. 過去30日の価格履歴メモ (十分な点/不足/ファイル無し)。
5. 品薄サイン (low_stock/out_of_stock/それ以外)。
6. 実店舗注記に断定表現が含まれない。
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.dirname(THIS_DIR)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import stock_status as ss  # noqa: E402
import where_to_buy_format as wtb  # noqa: E402

NOW = datetime(2026, 8, 12, 3, 0, 0, tzinfo=timezone.utc)  # JST 12:00


def _obs(state: str, *, price=6490, remaining=None, observed_at="2026-08-12T03:00:00+00:00") -> ss.StockObservation:
    return ss.StockObservation(
        asin="B001", state=state, remaining=remaining, raw_avail=None,
        price=price, observed_at=observed_at,
    )


def _options(amazon=True, rakuten=False, yahoo=False, amazon_price=6490):
    return {
        "amazon": {"available": amazon, "price": amazon_price if amazon else None,
                   "url": "https://www.amazon.co.jp/dp/B001/?tag=x-22", "is_search": False},
        "rakuten": {"available": rakuten, "price": 5980 if rakuten else None,
                    "url": "https://item.rakuten.co.jp/x/" if rakuten else None, "is_search": False},
        "yahoo": {"available": yahoo, "price": None, "url": None, "is_search": False},
    }


# ---------------------------------------------------------------------------
# 1: 5 state の結論ブロック
# ---------------------------------------------------------------------------

def test_conclusion_in_stock():
    text = wtb.build_conclusion("テスト商品", _obs(ss.STATE_IN_STOCK), _options())
    assert "2026-08-12 時点" in text
    assert "在庫あり" in text
    assert "￥6,490" in text
    assert "取扱を確認できませんでした" in text  # 楽天/Yahoo とも取扱なし


def test_conclusion_low_stock():
    obs = _obs(ss.STATE_LOW_STOCK, remaining=3)
    text = wtb.build_conclusion("テスト商品", obs, _options())
    assert "2026-08-12 時点" in text
    assert "残り3点" in text


def test_conclusion_low_stock_without_remaining_count():
    obs = _obs(ss.STATE_LOW_STOCK, remaining=None)
    text = wtb.build_conclusion("テスト商品", obs, _options())
    assert "残りわずか" in text


def test_conclusion_out_of_stock():
    obs = _obs(ss.STATE_OUT_OF_STOCK, price=None)
    text = wtb.build_conclusion("テスト商品", obs, _options(amazon=False))
    assert "在庫切れ" in text
    assert "2026-08-12 時点" in text


def test_conclusion_delayed():
    obs = _obs(ss.STATE_DELAYED)
    text = wtb.build_conclusion("テスト商品", obs, _options())
    assert "発送までお時間をいただく場合があります" in text
    assert "2026-08-12 時点" in text


def test_conclusion_unknown_still_produces_dated_text():
    # 実運用ゲートでは到達しないが、単体では state-complete であるべき。
    obs = _obs(ss.STATE_UNKNOWN, price=None)
    text = wtb.build_conclusion("テスト商品", obs, _options(amazon=False))
    assert "確認できませんでした" in text
    assert "2026-08-12 時点" in text


def test_conclusion_mentions_available_other_sites():
    obs = _obs(ss.STATE_IN_STOCK)
    text = wtb.build_conclusion("テスト商品", obs, _options(rakuten=True, yahoo=True))
    assert "でも取扱を確認できました" in text


# ---------------------------------------------------------------------------
# 2: タイトルの括弧が実際の取扱サイトだけに縮む
# ---------------------------------------------------------------------------

def test_title_amazon_only_parens():
    title = wtb.build_title("テスト商品", _options(amazon=True, rakuten=False, yahoo=False))
    assert title == "テスト商品はどこで買える？在庫と価格を毎日チェック（Amazon）"


def test_title_all_three_sites():
    title = wtb.build_title("テスト商品", _options(amazon=True, rakuten=True, yahoo=True))
    assert title == "テスト商品はどこで買える？在庫と価格を毎日チェック（Amazon/楽天/Yahoo!ショッピング）"


def test_title_no_site_available_omits_parens():
    title = wtb.build_title("テスト商品", _options(amazon=False, rakuten=False, yahoo=False))
    assert title == "テスト商品はどこで買える？在庫と価格を毎日チェック"
    assert "（" not in title


# ---------------------------------------------------------------------------
# 3: ロールアウト日ゲート (既存記事保護)
# ---------------------------------------------------------------------------

def _write_latest(path: Path, items: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"generated_at": NOW.isoformat(), "items": items}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def test_eligible_true_on_and_after_rollout_date(tmp_path):
    latest = _write_latest(
        tmp_path / "price_watch" / "latest.json",
        {"B001": {"p": 3149, "ts": NOW.isoformat(), "avail": "在庫あり。"}},
    )
    idx = ss.load_stock_index(latest, now=NOW)
    assert wtb.is_stock_format_eligible("B001", "2026-08-13T10:00:00+09:00", idx) is True
    assert wtb.is_stock_format_eligible("B001", "2026-09-01T10:00:00+09:00", idx) is True


def test_eligible_false_before_rollout_date_even_if_stock_known(tmp_path):
    """既存記事保護: 在庫状態が classify 可能でも date がロールアウト日より
    前なら新型を適用しない。"""
    latest = _write_latest(
        tmp_path / "price_watch" / "latest.json",
        {"B001": {"p": 3149, "ts": NOW.isoformat(), "avail": "在庫あり。"}},
    )
    idx = ss.load_stock_index(latest, now=NOW)
    assert ss.can_use_stock_title("B001", idx) is True  # データ層は許可している
    assert wtb.is_stock_format_eligible("B001", "2026-08-12T10:00:00+09:00", idx) is False
    assert wtb.is_stock_format_eligible("B001", "2026-05-14T10:00:00+09:00", idx) is False


def test_eligible_false_when_state_unknown_even_after_rollout(tmp_path):
    latest = _write_latest(tmp_path / "price_watch" / "latest.json", {})
    idx = ss.load_stock_index(latest, now=NOW)
    assert wtb.is_stock_format_eligible("B404", "2026-09-01T10:00:00+09:00", idx) is False


def test_eligible_false_when_date_missing_or_malformed(tmp_path):
    latest = _write_latest(
        tmp_path / "price_watch" / "latest.json",
        {"B001": {"p": 3149, "ts": NOW.isoformat(), "avail": "在庫あり。"}},
    )
    idx = ss.load_stock_index(latest, now=NOW)
    assert wtb.is_stock_format_eligible("B001", None, idx) is False
    assert wtb.is_stock_format_eligible("B001", "", idx) is False
    assert wtb.is_stock_format_eligible("B001", "not-a-date", idx) is False


# ---------------------------------------------------------------------------
# 4: 過去30日の価格履歴メモ
# ---------------------------------------------------------------------------

def _write_jsonl(root: Path, asin: str, records: list[dict]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{asin.upper()}.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )


def _rec(days_ago: int, price: int, source: str = "amazon") -> dict:
    ts = (NOW - timedelta(days=days_ago)).isoformat()
    return {"ts": ts, "source": source, "price": price, "availability": "在庫あり。"}


def test_price_history_note_present_with_enough_recent_points(tmp_path):
    root = tmp_path / "price_history"
    _write_jsonl(root, "B001", [_rec(20, 1200), _rec(10, 999), _rec(1, 1050)])
    note = wtb.build_price_history_note("B001", root, now=NOW)
    assert note is not None
    assert "￥999" in note
    assert "30日間" in note


def test_price_history_note_none_when_file_missing(tmp_path):
    root = tmp_path / "price_history"
    note = wtb.build_price_history_note("B001", root, now=NOW)
    assert note is None


def test_price_history_note_none_when_too_few_points_in_window(tmp_path):
    root = tmp_path / "price_history"
    # 1点しか30日以内に無い (もう1点は60日以上前)
    _write_jsonl(root, "B001", [_rec(90, 2000), _rec(5, 1200)])
    note = wtb.build_price_history_note("B001", root, now=NOW)
    assert note is None


def test_price_history_note_ignores_non_amazon_source(tmp_path):
    root = tmp_path / "price_history"
    _write_jsonl(root, "B001", [_rec(10, 999, source="rakuten"), _rec(5, 1050, source="rakuten")])
    note = wtb.build_price_history_note("B001", root, now=NOW)
    assert note is None


# ---------------------------------------------------------------------------
# 5: 品薄サイン
# ---------------------------------------------------------------------------

def test_low_stock_note_with_remaining_count():
    note = wtb.build_low_stock_note(_obs(ss.STATE_LOW_STOCK, remaining=2))
    assert note is not None
    assert "残り2点" in note


def test_low_stock_note_out_of_stock():
    note = wtb.build_low_stock_note(_obs(ss.STATE_OUT_OF_STOCK, price=None))
    assert note is not None
    assert "在庫切れ" in note


def test_low_stock_note_none_for_in_stock():
    note = wtb.build_low_stock_note(_obs(ss.STATE_IN_STOCK))
    assert note is None


# ---------------------------------------------------------------------------
# 6: 実店舗注記 — 断定表現を含まない・在庫データを持たない旨を明記
# ---------------------------------------------------------------------------

def test_offline_note_does_not_assert_physical_stock():
    note = wtb.build_offline_note("テスト商品")
    assert "在庫のみを" in note or "オンラインストア" in note
    for forbidden in ("トイザらスで買えます", "に在庫あり", "で購入できます"):
        assert forbidden not in note
    assert "google.com/search" in note


# ---------------------------------------------------------------------------
# rows / build_stock_block の組立て
# ---------------------------------------------------------------------------

def test_build_rows_amazon_state_label_and_others():
    rows = wtb.build_rows(_obs(ss.STATE_LOW_STOCK, remaining=1), _options(rakuten=True))
    by_site = {r["site"]: r for r in rows}
    assert by_site["Amazon"]["state_label"] == "残りわずか"
    assert by_site["楽天"]["state_label"] == "取扱あり"
    assert by_site["Yahoo!ショッピング"]["state_label"] == "取扱を確認できず"


def test_build_stock_block_aggregates_all_fields(tmp_path):
    root = tmp_path / "price_history"
    _write_jsonl(root, "B001", [_rec(10, 999), _rec(2, 1050)])
    block = wtb.build_stock_block(
        "テスト商品", _obs(ss.STATE_IN_STOCK), _options(),
        price_history_root=root, asin="B001", now=NOW,
    )
    assert block["conclusion"]
    assert len(block["rows"]) == 3
    assert block["price_history_note"] is not None
    assert block["low_stock_note"] is None
    assert block["offline_note"]
