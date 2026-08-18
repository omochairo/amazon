"""Unit tests for stock_status.py (#2686 / #4964 「どこで買える/在庫」データ層)。

Coverage:
1. 5 つの state (in_stock / low_stock / out_of_stock / delayed / unknown) の分類。
2. 「残り12点」から remaining=12 が取れる。
3. avail 欠落時は unknown になり、in_stock に倒れない。
4. price_watch 未収録 ASIN は can_use_stock_title が False。
5. price_watch 収録済でも avail 欠落/unknown 文言なら can_use_stock_title が False。
6. 楽天/Yahoo の is_search=true は「取扱を確認できず」(available=False) に分類。
7. price は price_overlay 経由で解決される (price_watch 優先)。
8. off / p 欠落時も avail 分類は独立して機能する (price_overlay が price 無しで
   index から落とす entry でも avail は読める)。
9. StockIndex が壊れた/存在しない latest.json で graceful に空になる。
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

NOW = datetime(2026, 8, 12, 0, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_latest(path: Path, items: dict, generated_at: str | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"generated_at": generated_at or _iso(NOW), "items": items},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# 1: 5 state の分類 (実際の avail 文言をそのまま使う)
# ---------------------------------------------------------------------------

def test_classify_in_stock():
    state, remaining = ss.classify_availability("在庫あり。")
    assert state == ss.STATE_IN_STOCK
    assert remaining is None


def test_classify_low_stock():
    state, remaining = ss.classify_availability("残り3点 ご注文はお早めに")
    assert state == ss.STATE_LOW_STOCK
    assert remaining == 3


def test_classify_out_of_stock():
    state, remaining = ss.classify_availability("現在在庫切れです。")
    assert state == ss.STATE_OUT_OF_STOCK
    assert remaining is None


def test_classify_delayed():
    state, remaining = ss.classify_availability("通常4～5日以内に発送します。")
    assert state == ss.STATE_DELAYED
    assert remaining is None


def test_classify_unknown_missing_or_unrecognized():
    """unknown に落とすのは「観測が取れていない」ときだけ (#5483)。

    以前はここに `この商品は現在お取り扱いできません` も入っていたが、あれは
    **買えないと分かっている**観測なので out_of_stock に移した
    (test_classify_out_of_stock_variants)。unknown は「分からない」専用にする。
    """
    assert ss.classify_availability(None) == (ss.STATE_UNKNOWN, None)
    assert ss.classify_availability("") == (ss.STATE_UNKNOWN, None)
    assert ss.classify_availability("   ") == (ss.STATE_UNKNOWN, None)
    assert ss.classify_availability(12345) == (ss.STATE_UNKNOWN, None)
    # 実データに無い未知文言は従来どおり unknown。
    assert ss.classify_availability("なにかの新しい文言") == (ss.STATE_UNKNOWN, None)


# ---------------------------------------------------------------------------
# 1b: unknown に落ちていた実文言の再分類 (#5483)
#
# latest.json 2,001 件のうち 69 件が unknown だった。内訳は avail 欠落 24 件と、
# **意味が読み取れるのに拾えていない 45 件**。後者は記事に「在庫状況は確認でき
# ませんでした」と出ており、分かっていることを黙る形になっていた。
# ---------------------------------------------------------------------------

def test_classify_out_of_stock_variants():
    """「買えない」と読める文言は全て out_of_stock (実データ 23 件)。"""
    for raw in (
        "現在在庫切れです。",                        # 従来から拾えていた
        "一時的に在庫切れ; 入荷時期は未定です。",    # 実データ 15 件
        "一時的に在庫切れ",                          # 実データ 2 件
        "この商品は現在お取り扱いできません。",      # 実データ 6 件
    ):
        assert ss.classify_availability(raw) == (ss.STATE_OUT_OF_STOCK, None), raw


def test_classify_preorder():
    """発売予定日つきの文言は preorder (実データ 19 件)。

    予約は「買えるが、まだ発売していない」。in_stock にすると発売前の商品を
    在庫ありと言うことになり、out_of_stock にすると予約できるのに買えないと
    言うことになるので、独立した状態にする。
    """
    assert ss.classify_availability("この商品の発売予定日は2026年9月19日です。") == (
        ss.STATE_PREORDER, None,
    )


def test_classify_delayed_accepts_units_longer_than_days():
    """`通常N〜Nか月以内に発送` も delayed (実データ 3 件)。

    従来の正規表現は日単位だけを見ていたため unknown に落ちていた。発送に
    時間がかかるだけで購入自体はできるので delayed が正しい。
    """
    assert ss.classify_availability("通常1～2か月以内に発送します。") == (
        ss.STATE_DELAYED, None,
    )
    assert ss.classify_availability("通常2～3週間以内に発送します。") == (
        ss.STATE_DELAYED, None,
    )
    # 日単位は従来どおり。
    assert ss.classify_availability("通常4～5日以内に発送します。") == (
        ss.STATE_DELAYED, None,
    )


def test_out_of_stock_wins_over_preorder():
    """両方に当たったら「買えない」を優先する。"""
    assert ss.classify_availability(
        "この商品の発売予定日は2026年9月19日です。現在在庫切れです。"
    ) == (ss.STATE_OUT_OF_STOCK, None)


# ---------------------------------------------------------------------------
# 2: remaining の抽出 (2桁)
# ---------------------------------------------------------------------------

def test_low_stock_remaining_two_digits():
    state, remaining = ss.classify_availability("残り12点 ご注文はお早めに")
    assert state == ss.STATE_LOW_STOCK
    assert remaining == 12


# ---------------------------------------------------------------------------
# 3: avail 欠落 → unknown (in_stock に倒れない)
# ---------------------------------------------------------------------------

def test_resolve_stock_missing_avail_is_unknown_not_in_stock(tmp_path):
    latest = _write_latest(
        tmp_path / "price_watch" / "latest.json",
        {"B001": {"p": 3149, "ts": _iso(NOW)}},  # avail キー自体が無い
    )
    idx = ss.load_stock_index(latest, now=NOW)
    obs = ss.resolve_stock("B001", idx, now=NOW)

    assert obs.state == ss.STATE_UNKNOWN
    assert obs.raw_avail is None
    assert obs.remaining is None
    assert obs.price == 3149  # 価格は別途 price_overlay 経由で取れる


def test_resolve_stock_asin_not_in_price_watch_is_unknown(tmp_path):
    latest = _write_latest(tmp_path / "price_watch" / "latest.json", {})
    idx = ss.load_stock_index(latest, now=NOW)
    obs = ss.resolve_stock("B404", idx, now=NOW)

    assert obs.state == ss.STATE_UNKNOWN
    assert obs.price is None
    assert obs.observed_at == _iso(NOW)  # generated_at にフォールバック


# ---------------------------------------------------------------------------
# 4 / 5: can_use_stock_title
# ---------------------------------------------------------------------------

def test_can_use_stock_title_false_when_asin_not_in_price_watch(tmp_path):
    latest = _write_latest(
        tmp_path / "price_watch" / "latest.json",
        {"B001": {"p": 3149, "ts": _iso(NOW), "avail": "在庫あり。"}},
    )
    idx = ss.load_stock_index(latest, now=NOW)
    assert ss.can_use_stock_title("B404", idx) is False


def test_can_use_stock_title_false_when_avail_missing_or_unknown(tmp_path):
    latest = _write_latest(
        tmp_path / "price_watch" / "latest.json",
        {
            "B001": {"p": 3149, "ts": _iso(NOW)},  # avail 欠落
            "B002": {"p": 1000, "ts": _iso(NOW), "avail": "謎の未知文言です"},
        },
    )
    idx = ss.load_stock_index(latest, now=NOW)
    assert ss.can_use_stock_title("B001", idx) is False
    assert ss.can_use_stock_title("B002", idx) is False


def test_can_use_stock_title_true_when_avail_classified(tmp_path):
    latest = _write_latest(
        tmp_path / "price_watch" / "latest.json",
        {"B001": {"p": 3149, "ts": _iso(NOW), "avail": "在庫あり。"}},
    )
    idx = ss.load_stock_index(latest, now=NOW)
    assert ss.can_use_stock_title("B001", idx) is True


def test_can_use_stock_title_true_even_without_price(tmp_path):
    # avail は取れるが p が無い entry (実測 6.5% の存在パターン)。
    # can_use_stock_title は avail の分類だけを見るので price 有無を問わない。
    latest = _write_latest(
        tmp_path / "price_watch" / "latest.json",
        {"B001": {"ts": _iso(NOW), "avail": "在庫あり。"}},  # p キー無し
    )
    idx = ss.load_stock_index(latest, now=NOW)
    assert "B001" not in idx.price_index
    assert ss.can_use_stock_title("B001", idx) is True


# ---------------------------------------------------------------------------
# 6: rakuten/yahoo is_search=true → 取扱を確認できず
# ---------------------------------------------------------------------------

def test_purchase_options_is_search_means_unavailable():
    product_prices = {
        "amazon": {"price": 3149, "url": "https://www.amazon.co.jp/dp/B001/?tag=x-22"},
        "rakuten": {"price": 0, "url": "", "is_search": True},
        "yahoo": {"price": 2980, "url": "https://shopping.yahoo.co.jp/x", "is_search": False},
    }
    stock_obs = ss.StockObservation(
        asin="B001", state=ss.STATE_IN_STOCK, remaining=None,
        raw_avail="在庫あり。", price=3149, observed_at=_iso(NOW),
    )
    options = ss.resolve_purchase_options(product_prices, stock_obs)

    assert options["rakuten"]["available"] is False
    assert options["rakuten"]["is_search"] is True
    assert options["rakuten"]["price"] is None

    assert options["yahoo"]["available"] is True
    assert options["yahoo"]["price"] == 2980

    assert options["amazon"]["available"] is True
    assert options["amazon"]["price"] == 3149
    assert options["amazon"]["url"].endswith("tag=x-22")


def test_purchase_options_amazon_unavailable_when_out_of_stock():
    product_prices = {
        "amazon": {"price": 3149, "url": "https://www.amazon.co.jp/dp/B001/?tag=x-22"},
    }
    stock_obs = ss.StockObservation(
        asin="B001", state=ss.STATE_OUT_OF_STOCK, remaining=None,
        raw_avail="現在在庫切れです。", price=3149, observed_at=_iso(NOW),
    )
    options = ss.resolve_purchase_options(product_prices, stock_obs)
    assert options["amazon"]["available"] is False


def test_purchase_options_missing_site_defaults_unavailable():
    stock_obs = ss.StockObservation(
        asin="B001", state=ss.STATE_UNKNOWN, remaining=None,
        raw_avail=None, price=None, observed_at=None,
    )
    options = ss.resolve_purchase_options({}, stock_obs)
    assert options["rakuten"] == {"available": False, "price": None, "url": None, "is_search": False}
    assert options["yahoo"] == {"available": False, "price": None, "url": None, "is_search": False}
    assert options["amazon"]["available"] is False


# ---------------------------------------------------------------------------
# 7: price は price_overlay 経由 (price_watch 優先 → per_asin フォールバック)
# ---------------------------------------------------------------------------

def test_price_resolved_via_price_overlay_fallback_to_per_asin(tmp_path):
    # price_watch は avail はあるが stale (price_overlay の 3日ガードを超える)
    latest = _write_latest(
        tmp_path / "price_watch" / "latest.json",
        {"B001": {"p": 7927, "ts": _iso(NOW - timedelta(days=10)), "avail": "在庫あり。"}},
    )
    per_asin = tmp_path / "per_asin"
    per_asin_dir = per_asin / "B001"
    per_asin_dir.mkdir(parents=True)
    (per_asin_dir / "amazon.json").write_text(
        json.dumps({
            "asin": "B001",
            "fetched_at": _iso(NOW - timedelta(days=2)),
            "item": {"price": 3149, "availability": "在庫あり。"},
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    idx = ss.load_stock_index(latest, now=NOW)
    obs = ss.resolve_stock("B001", idx, per_asin_root=per_asin, now=NOW)

    # avail の state は price_watch (stale でも avail 自体は読む) から
    assert obs.state == ss.STATE_IN_STOCK
    # price は per_asin フォールバック経由
    assert obs.price == 3149


# ---------------------------------------------------------------------------
# 8: avail は price 欠落 entry でも独立して読める
# ---------------------------------------------------------------------------

def test_avail_readable_even_when_price_missing(tmp_path):
    latest = _write_latest(
        tmp_path / "price_watch" / "latest.json",
        {"B001": {"ts": _iso(NOW), "avail": "現在在庫切れです。"}},  # p キー無し
    )
    idx = ss.load_stock_index(latest, now=NOW)
    # price_overlay 側の index には price が無いので採択されない
    assert "B001" not in idx.price_index

    obs = ss.resolve_stock("B001", idx, now=NOW)
    assert obs.state == ss.STATE_OUT_OF_STOCK
    assert obs.price is None
    assert ss.can_use_stock_title("B001", idx) is True


# ---------------------------------------------------------------------------
# 9: graceful degrade
# ---------------------------------------------------------------------------

def test_load_stock_index_graceful_on_missing_or_broken_file(tmp_path):
    missing = tmp_path / "nope" / "latest.json"
    idx = ss.load_stock_index(missing, now=NOW)
    assert idx.items == {}
    assert idx.generated_at is None
    assert idx.price_index == {}

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    idx2 = ss.load_stock_index(broken, now=NOW)
    assert idx2.items == {}

    obs = ss.resolve_stock("B001", idx, now=NOW)
    assert obs.state == ss.STATE_UNKNOWN
    assert obs.price is None
    assert ss.can_use_stock_title("B001", idx) is False


def test_resolve_stock_empty_asin_is_unknown(tmp_path):
    latest = _write_latest(tmp_path / "price_watch" / "latest.json", {})
    idx = ss.load_stock_index(latest, now=NOW)
    obs = ss.resolve_stock("", idx, now=NOW)
    assert obs.asin == ""
    assert obs.state == ss.STATE_UNKNOWN


# ---------------------------------------------------------------------------
# ASIN 正規化 (小文字入力でも大文字化)
# ---------------------------------------------------------------------------

def test_asin_lowercase_input_is_normalized(tmp_path):
    latest = _write_latest(
        tmp_path / "price_watch" / "latest.json",
        {"b0875fv2bq": {"p": 3149, "ts": _iso(NOW), "avail": "在庫あり。"}},
    )
    idx = ss.load_stock_index(latest, now=NOW)
    obs = ss.resolve_stock("b0875fv2bq", idx, now=NOW)
    assert obs.asin == "B0875FV2BQ"
    assert obs.state == ss.STATE_IN_STOCK
    assert ss.can_use_stock_title("b0875fv2bq", idx) is True


# ---------------------------------------------------------------------------
# 1c: タイトル適用の allowlist 化 (#5483)
# ---------------------------------------------------------------------------

def test_stock_title_states_exclude_preorder(tmp_path):
    """preorder は分類としては正しく持つが、「どこで買える」枠は当てない。

    発売前の商品にその枠を当てても、読者に返せる中身が「まだ売っていない」しか
    無い。分類の正しさと、枠を当てるかは別の判断。
    """
    latest = tmp_path / "latest.json"
    latest.write_text(json.dumps({"items": {
        "B0IN": {"avail": "在庫あり。", "p": 1000, "ts": NOW.isoformat()},
        "B0OOS": {"avail": "一時的に在庫切れ; 入荷時期は未定です。", "p": 1000, "ts": NOW.isoformat()},
        "B0PRE": {"avail": "この商品の発売予定日は2026年9月19日です。", "p": 1000, "ts": NOW.isoformat()},
        "B0DLY": {"avail": "通常1～2か月以内に発送します。", "p": 1000, "ts": NOW.isoformat()},
        "B0UNK": {"avail": None, "p": 1000, "ts": NOW.isoformat()},
    }}, ensure_ascii=False), encoding="utf-8")
    idx = ss.load_stock_index(latest, now=NOW)
    assert ss.can_use_stock_title("B0IN", idx) is True
    assert ss.can_use_stock_title("B0DLY", idx) is True
    # 在庫切れは「買えない」と書く枠として成立するので対象のまま。
    assert ss.can_use_stock_title("B0OOS", idx) is True
    assert ss.can_use_stock_title("B0PRE", idx) is False
    assert ss.can_use_stock_title("B0UNK", idx) is False
