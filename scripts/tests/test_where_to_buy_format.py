"""Unit tests for where_to_buy_format.py (#2686 / #4964 / #5011)。

Coverage:
1. 5 state それぞれの結論ブロック文面。
2. Amazon のみ取扱ありのときタイトル括弧が「（Amazon）」に縮む。
3. ロールアウト日より前の記事は is_stock_format_eligible が False (既存記事保護)。
4. 過去30日の価格履歴メモ (十分な点/不足/ファイル無し/階段関数としての窓端の扱い/
   price_watch (日次) と price_history (週次) 2 レーンのマージと dedupe)。
5. 品薄サイン (low_stock/out_of_stock/それ以外)。
6. 実店舗注記 (#5011): Google 検索リンクが完全に無いこと、いずれかのサイトで
   取扱ありならテーブルへの導線のみ、3サイト全滅のときだけトイザらス導線。
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


def test_price_history_note_none_when_only_one_point_ever(tmp_path):
    root = tmp_path / "price_history"
    # 観測点が生涯 1 点しか無い場合は「推移」を語れないので出さない。
    _write_jsonl(root, "B001", [_rec(5, 1200)])
    note = wtb.build_price_history_note("B001", root, now=NOW)
    assert note is None


def test_price_history_note_ignores_non_amazon_source(tmp_path):
    root = tmp_path / "price_history"
    _write_jsonl(root, "B001", [_rec(10, 999, source="rakuten"), _rec(5, 1050, source="rakuten")])
    note = wtb.build_price_history_note("B001", root, now=NOW)
    assert note is None


def test_price_history_note_carries_forward_price_from_before_window(tmp_path):
    """#5011: 記録は価格の変化点であり日次サンプルではない。窓の開始時点で
    有効だった価格は窓より前の最後の変化点にあるので、そこも最安値候補に
    含める (階段関数としての解釈)。"""
    root = tmp_path / "price_history"
    # 90日前に2000円になり、5日前に1200円へ値下げ。窓(30日)の間は
    # 「2000円 → (5日前に)1200円」と推移しており、最安は1200円。
    _write_jsonl(root, "B001", [_rec(90, 2000), _rec(5, 1200)])
    note = wtb.build_price_history_note("B001", root, now=NOW)
    assert note is not None
    assert "￥1,200" in note


def test_price_history_note_uses_carry_only_price_when_no_change_in_window(tmp_path):
    """窓の間に値動きが無くても (=窓内の変化点が0でも)、窓開始時点の carry
    価格がそのまま窓全体の最安値として使える。"""
    root = tmp_path / "price_history"
    _write_jsonl(root, "B001", [_rec(90, 3000), _rec(60, 1500)])
    note = wtb.build_price_history_note("B001", root, now=NOW)
    assert note is not None
    assert "￥1,500" in note


# ---------------------------------------------------------------------------
# 4b: price_watch (日次) と price_history (週次) の 2 レーンをマージして読む
#
# 2 レーンは dedupe が独立に効いて位相がずれるため、実測でほぼ相補 (共通
# 1,854 ASIN のうち 1,826 で (日付,価格) 集合が食い違う)。片側を捨てる
# フォールバックは観測点を落として実際より高い最安値を出していた。
# ---------------------------------------------------------------------------

def test_price_history_note_uses_cheaper_point_from_history_lane(tmp_path):
    """history 側にしかない安値を拾う (watch 優先だと取りこぼしていたケース)。"""
    watch_root = tmp_path / "price_watch" / "history"
    hist_root = tmp_path / "price_history"
    _write_jsonl(watch_root, "B001", [_rec(20, 1111), _rec(2, 1050)])
    _write_jsonl(hist_root, "B001", [_rec(15, 1200), _rec(9, 999)])
    note = wtb.build_price_history_note(
        "B001", hist_root, price_watch_root=watch_root, now=NOW,
    )
    assert note is not None
    assert "￥999" in note


def test_price_history_note_uses_cheaper_point_from_watch_lane(tmp_path):
    """逆向き: watch 側にしかない安値も拾う (マージが片側に寄っていないこと)。"""
    watch_root = tmp_path / "price_watch" / "history"
    hist_root = tmp_path / "price_history"
    _write_jsonl(watch_root, "B001", [_rec(20, 1111), _rec(2, 888)])
    _write_jsonl(hist_root, "B001", [_rec(15, 1200), _rec(9, 999)])
    note = wtb.build_price_history_note(
        "B001", hist_root, price_watch_root=watch_root, now=NOW,
    )
    assert note is not None
    assert "￥888" in note


def test_price_history_note_reaches_min_points_only_after_merge(tmp_path):
    """各レーン単独では MIN_POINTS 未満でも、合算で満たせば note を出す。"""
    watch_root = tmp_path / "price_watch" / "history"
    hist_root = tmp_path / "price_history"
    _write_jsonl(watch_root, "B001", [_rec(20, 1200)])
    _write_jsonl(hist_root, "B001", [_rec(5, 990)])
    note = wtb.build_price_history_note(
        "B001", hist_root, price_watch_root=watch_root, now=NOW,
    )
    assert note is not None
    assert "￥990" in note


def test_price_history_note_dedupes_identical_points_across_lanes(tmp_path):
    """両レーンに同一 (ts, price) があっても MIN_POINTS を水増ししない。"""
    watch_root = tmp_path / "price_watch" / "history"
    hist_root = tmp_path / "price_history"
    same = _rec(5, 990)
    _write_jsonl(watch_root, "B001", [same])
    _write_jsonl(hist_root, "B001", [same])
    note = wtb.build_price_history_note(
        "B001", hist_root, price_watch_root=watch_root, now=NOW,
    )
    assert note is None


def test_price_history_note_works_when_watch_file_missing(tmp_path):
    """watch 側にこの ASIN のファイルが無くても history 単独で成立する。"""
    watch_root = tmp_path / "price_watch" / "history"
    hist_root = tmp_path / "price_history"
    watch_root.mkdir(parents=True, exist_ok=True)
    _write_jsonl(hist_root, "B001", [_rec(20, 5000), _rec(3, 4800)])
    note = wtb.build_price_history_note(
        "B001", hist_root, price_watch_root=watch_root, now=NOW,
    )
    assert note is not None
    assert "￥4,800" in note


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
# 6: 実店舗注記 (#5011) — Google 検索リンクは完全撤廃、受け皿は
#    在庫確認済みならテーブル導線、全滅ならトイザらス在庫検索のみ。
#    断定表現は含めない・在庫データを持たない旨は両分岐で明記する。
# ---------------------------------------------------------------------------

def test_offline_note_never_links_to_google_search():
    """回帰防止: どちらの分岐でも google.com/search は絶対に出ない。"""
    for opts in (
        _options(amazon=True, rakuten=False, yahoo=False),
        _options(amazon=False, rakuten=False, yahoo=False),
        None,
    ):
        note = wtb.build_offline_note("テスト商品", opts)
        assert "google.com/search" not in note


def test_offline_note_when_some_site_available_has_no_external_search_link():
    """Amazon/楽天/Yahoo のいずれかで取扱ありなら、外部検索エンジンへのリンクを
    置かず、既出の在庫・価格テーブルへ戻る案内にする。"""
    note = wtb.build_offline_note("テスト商品", _options(amazon=True, rakuten=False, yahoo=False))
    assert "<a href" not in note
    assert "在庫・価格の一覧" in note
    assert "オンラインストア" in note or "在庫のみを" in note


def test_offline_note_when_all_sites_unavailable_links_to_toysrus_search():
    """3サイトとも取扱不明のときだけ、消極的な受け皿としてトイザらスの
    在庫検索へリンクする。アフィリエイトタグは付けない。"""
    note = wtb.build_offline_note("テスト商品", _options(amazon=False, rakuten=False, yahoo=False))
    assert "toysrus.co.jp/search" in note
    assert "tag=" not in note
    assert 'rel="noopener nofollow"' in note


def test_offline_note_does_not_assert_physical_stock():
    for opts in (
        _options(amazon=True, rakuten=False, yahoo=False),
        _options(amazon=False, rakuten=False, yahoo=False),
    ):
        note = wtb.build_offline_note("テスト商品", opts)
        assert "在庫のみを" in note or "オンラインストア" in note
        for forbidden in ("トイザらスで買えます", "に在庫あり", "で購入できます", "で買える"):
            assert forbidden not in note


def test_offline_note_passes_quality_gate_physical_store_check():
    """quality_gate.check_no_physical_store_claims が、新しいトイザらス導線を
    実店舗在庫の断定として誤検出しないこと。"""
    import quality_gate as qg  # noqa: E402 (local import, avoids test-collection cost when unused)

    for opts in (
        _options(amazon=True, rakuten=False, yahoo=False),
        _options(amazon=False, rakuten=False, yahoo=False),
    ):
        note = wtb.build_offline_note("テスト商品", opts)
        assert qg.check_no_physical_store_claims(note) == []


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


def test_build_stock_block_prefers_price_watch_root_when_given(tmp_path):
    watch_root = tmp_path / "price_watch" / "history"
    hist_root = tmp_path / "price_history"
    _write_jsonl(watch_root, "B001", [_rec(10, 111), _rec(2, 100)])
    _write_jsonl(hist_root, "B001", [_rec(10, 999), _rec(2, 1050)])
    block = wtb.build_stock_block(
        "テスト商品", _obs(ss.STATE_IN_STOCK), _options(),
        price_history_root=hist_root, price_watch_root=watch_root, asin="B001", now=NOW,
    )
    assert block["price_history_note"] is not None
    assert "￥100" in block["price_history_note"]
