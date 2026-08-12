"""where_to_buy_format.py

#2686 / #4964 — 「どこで買える/在庫」記事型の決定的レンダリング層。

stock_status.py (在庫・価格の解決) の上に、タイトル / 結論ブロック / 在庫
テーブル / 価格推移メモ / 実店舗注記を **決定的に** 組み立てる。

なぜ LLM を通さないか:
  タイトルで「在庫を毎日チェック」と約束しても本文に答えが無い、という事故が
  B0H4PQ29JS で実際に発生し、Google のインデックスから記事が消えた実績がある。
  タイトルも本文の結論ブロックも同じ StockObservation / purchase_options から
  機械的に導出することで、この種の食い違いを構造的に起こらなくする。

ロールアウト日ゲート:
  既存 1,946 本のタイトル・本文を書き換えないため、``is_stock_format_eligible``
  は記事の ``date`` が ``ROLLOUT_DATE`` 以降のときだけ True を返す。
  stock_status.can_use_stock_title() (price_watch に載っており avail が
  unknown でない) と AND を取る。

pure formatting only — 外部 API 呼び出しなし・data/ 書き込みなし。
"""
from __future__ import annotations

import json
import pathlib
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import stock_status

# #2686: この日付以降の date を持つ記事にのみ新型を適用する。既存記事保護の
# ための唯一のロールアウト定数 (1 箇所に集約)。
ROLLOUT_DATE = "2026-08-13"

_JST = timezone(timedelta(hours=9))

_SITE_ORDER = ("amazon", "rakuten", "yahoo")
_SITE_LABELS = {"amazon": "Amazon", "rakuten": "楽天", "yahoo": "Yahoo!ショッピング"}

_STATE_LABELS = {
    stock_status.STATE_IN_STOCK: "在庫あり",
    stock_status.STATE_LOW_STOCK: "残りわずか",
    stock_status.STATE_OUT_OF_STOCK: "在庫切れ",
    stock_status.STATE_DELAYED: "発送に数日",
    stock_status.STATE_UNKNOWN: "確認できず",
}

PRICE_HISTORY_WINDOW_DAYS = 30
# 過去 30 日窓の最安値メモを出すために最低限必要な観測点数 (窓内)。
PRICE_HISTORY_MIN_POINTS = 2


# ---------------------------------------------------------------------------
# 適用可否 (ロールアウト日ゲート)
# ---------------------------------------------------------------------------

def _article_date_str(date_value: Any) -> str:
    """記事の ``date`` フィールド (ISO8601 文字列) から ``YYYY-MM-DD`` を取る。

    形式が壊れている/欠落している場合は空文字 (=不適格) を返す。
    """
    if isinstance(date_value, str) and len(date_value) >= 10:
        head = date_value[:10]
        # 雑にでも YYYY-MM-DD の形をしているか確認 (ハイフン位置)。
        if len(head) == 10 and head[4] == "-" and head[7] == "-":
            return head
    return ""


def is_stock_format_eligible(
    asin: str,
    article_date: Any,
    index: stock_status.StockIndex,
    *,
    rollout_date: str = ROLLOUT_DATE,
) -> bool:
    """新型 (「どこで買える」タイトル + 在庫ブロック) を適用してよいかを判定する。

    次を両方満たすときだけ True (**既存記事は絶対に変化しない**):
      - 記事の ``date`` が ``rollout_date`` 以降
      - stock_status.can_use_stock_title(asin, index) が True
        (price_watch に載っており avail が unknown でない)
    """
    d = _article_date_str(article_date)
    if not d or d < rollout_date:
        return False
    return stock_status.can_use_stock_title(asin, index)


# ---------------------------------------------------------------------------
# タイトル
# ---------------------------------------------------------------------------

def _available_site_labels(purchase_options: dict[str, dict]) -> list[str]:
    labels: list[str] = []
    for site in _SITE_ORDER:
        opt = purchase_options.get(site) or {}
        if opt.get("available"):
            labels.append(_SITE_LABELS[site])
    return labels


def build_title(product_name: str, purchase_options: dict[str, dict]) -> str:
    """決定的タイトル生成。

    末尾の括弧は実際に取扱が確認できたサイト名だけに縮める。1 件も取扱が
    確認できない場合は括弧自体を出さない。
    """
    name = product_name or ""
    base = f"{name}はどこで買える？在庫と価格を毎日チェック"
    labels = _available_site_labels(purchase_options)
    if not labels:
        return base
    return f"{base}（{'/'.join(labels)}）"


# ---------------------------------------------------------------------------
# 日付表示 (JST)
# ---------------------------------------------------------------------------

def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not isinstance(ts, str) or not ts.strip():
        return None
    s = ts.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def to_jst_date(ts: Optional[str]) -> Optional[str]:
    dt = _parse_iso(ts)
    if dt is None:
        return None
    return dt.astimezone(_JST).strftime("%Y-%m-%d")


def to_jst_datetime(ts: Optional[str]) -> Optional[str]:
    dt = _parse_iso(ts)
    if dt is None:
        return None
    return dt.astimezone(_JST).strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------------------
# 結論ブロック
# ---------------------------------------------------------------------------

def build_conclusion(
    product_name: str,
    stock_obs: stock_status.StockObservation,
    purchase_options: dict[str, dict],
) -> str:
    """冒頭 2-3 文の結論ブロック。**在庫・価格は必ず取得日時とセット**で書く
    (単独の断定を避ける安全装置)。
    """
    date_label = to_jst_date(stock_obs.observed_at) or "確認日不明"
    amazon = purchase_options.get("amazon") or {}
    amazon_price = amazon.get("price")
    price_part = f" ￥{amazon_price:,}" if isinstance(amazon_price, int) else ""

    state = stock_obs.state
    if state == stock_status.STATE_IN_STOCK:
        head = f"{date_label} 時点、Amazon に在庫あり{price_part}。"
    elif state == stock_status.STATE_LOW_STOCK:
        remain = f"残り{stock_obs.remaining}点" if stock_obs.remaining else "残りわずか"
        head = f"{date_label} 時点、Amazon は{remain}{price_part}。"
    elif state == stock_status.STATE_DELAYED:
        head = f"{date_label} 時点、Amazon に在庫あり（発送までお時間をいただく場合があります）{price_part}。"
    elif state == stock_status.STATE_OUT_OF_STOCK:
        head = f"{date_label} 時点、Amazon は在庫切れです。"
    else:  # unknown — 実運用ゲート経由では到達しないが、単体呼び出し用に完備しておく。
        head = f"{date_label} 時点、Amazon の在庫状況は確認できませんでした。"

    other_sites = [s for s in ("rakuten", "yahoo")]
    available_others = [
        _SITE_LABELS[s] for s in other_sites if (purchase_options.get(s) or {}).get("available")
    ]
    unavailable_others = [
        _SITE_LABELS[s] for s in other_sites if not (purchase_options.get(s) or {}).get("available")
    ]

    tail = ""
    if unavailable_others and available_others:
        tail = (
            f"{'・'.join(available_others)}では取扱を確認できましたが、"
            f"{'・'.join(unavailable_others)}では取扱を確認できませんでした。"
        )
    elif unavailable_others:
        tail = f"{'・'.join(unavailable_others)}では取扱を確認できませんでした。"
    elif available_others:
        tail = f"{'・'.join(available_others)}でも取扱を確認できました。"

    return (head + tail).strip()


# ---------------------------------------------------------------------------
# 在庫・価格テーブル
# ---------------------------------------------------------------------------

def _amazon_state_label(state: str) -> str:
    return _STATE_LABELS.get(state, _STATE_LABELS[stock_status.STATE_UNKNOWN])


def _other_state_label(opt: dict) -> str:
    if opt.get("available"):
        return "取扱あり"
    if opt.get("is_search"):
        return "検索で確認"
    return "取扱を確認できず"


def build_rows(
    stock_obs: stock_status.StockObservation,
    purchase_options: dict[str, dict],
) -> list[dict[str, Any]]:
    observed_at_label = to_jst_datetime(stock_obs.observed_at) or "—"
    rows: list[dict[str, Any]] = []
    for site in _SITE_ORDER:
        opt = purchase_options.get(site) or {}
        if site == "amazon":
            state_label = _amazon_state_label(stock_obs.state)
        else:
            state_label = _other_state_label(opt)
        rows.append({
            "site": _SITE_LABELS[site],
            "price": opt.get("price"),
            "state_label": state_label,
            "observed_at_label": observed_at_label,
            "url": opt.get("url"),
        })
    return rows


# ---------------------------------------------------------------------------
# 価格推移 (過去30日最安値)
# ---------------------------------------------------------------------------

def _load_amazon_price_points(price_history_root: pathlib.Path | str, asin: str) -> list[dict[str, Any]]:
    """``data/price_history/<ASIN>.jsonl`` から source=amazon, price>0 の観測点
    を ts 昇順で返す。無い/壊れていればベストエフォートで空を返す。
    """
    if not asin:
        return []
    p = pathlib.Path(price_history_root) / f"{asin.upper()}.jsonl"
    if not p.exists():
        return []
    points: list[dict[str, Any]] = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict) or rec.get("source") != "amazon":
                continue
            price = rec.get("price")
            ts = rec.get("ts")
            if not isinstance(price, int) or isinstance(price, bool) or price <= 0:
                continue
            if not isinstance(ts, str) or not ts:
                continue
            points.append({"ts": ts, "price": price})
    except OSError:
        return []
    points.sort(key=lambda r: r["ts"])
    return points


def build_price_history_note(
    asin: str,
    price_history_root: pathlib.Path | str,
    *,
    now: Optional[datetime] = None,
) -> Optional[str]:
    """過去 30 日の最安値メモ。ファイルが無い/窓内の点が足りない場合は None
    (テンプレ側はこの行を出さない)。
    """
    points = _load_amazon_price_points(price_history_root, asin)
    if not points:
        return None
    ref_now = now if now is not None else datetime.now(timezone.utc)
    if ref_now.tzinfo is None:
        ref_now = ref_now.replace(tzinfo=timezone.utc)
    cutoff = ref_now - timedelta(days=PRICE_HISTORY_WINDOW_DAYS)

    recent: list[int] = []
    for pt in points:
        dt = _parse_iso(pt["ts"])
        if dt is None:
            continue
        if dt >= cutoff:
            recent.append(pt["price"])
    if len(recent) < PRICE_HISTORY_MIN_POINTS:
        return None
    min_price = min(recent)
    return f"過去{PRICE_HISTORY_WINDOW_DAYS}日間の本サイト計測では、Amazon 最安値は ￥{min_price:,} でした。"


# ---------------------------------------------------------------------------
# 品薄サイン
# ---------------------------------------------------------------------------

def build_low_stock_note(stock_obs: stock_status.StockObservation) -> Optional[str]:
    if stock_obs.state == stock_status.STATE_LOW_STOCK:
        if stock_obs.remaining:
            return f"⚠️ Amazon の在庫は残り{stock_obs.remaining}点です。ご検討の際はお早めにご確認ください。"
        return "⚠️ Amazon の在庫が少なくなっています。ご検討の際はお早めにご確認ください。"
    if stock_obs.state == stock_status.STATE_OUT_OF_STOCK:
        return "🔴 Amazon は現在在庫切れです。再入荷時期は不明です。"
    return None


# ---------------------------------------------------------------------------
# 実店舗についての注記 (オンライン在庫しか確認していない、という限界表明)
# ---------------------------------------------------------------------------

def build_offline_note(product_name: str) -> str:
    """実店舗の在庫は確認していない旨を明記し、各社の在庫検索への案内リンクを
    置く。特定チェーンの在庫を断定する文言は絶対に含めない。

    リンク先は商品名で検索するだけの汎用ページ (実店舗を騙る/アフィリエイト
    タグ付与は一切しない)。
    """
    q = urllib.parse.quote(f"{product_name} 在庫 店舗")
    url = f"https://www.google.com/search?q={q}"
    return (
        "本サイトはオンラインストア（Amazon・楽天市場・Yahoo!ショッピング）の在庫のみを"
        "確認しています。トイザらス・イオンなど実店舗の在庫データは持っていないため、"
        f"実店舗の在庫は各社の在庫検索でご確認ください（<a href=\"{url}\" target=\"_blank\" "
        "rel=\"noopener nofollow\">店舗の在庫を検索する →</a>）。"
    )


# ---------------------------------------------------------------------------
# まとめ
# ---------------------------------------------------------------------------

def build_stock_block(
    product_name: str,
    stock_obs: stock_status.StockObservation,
    purchase_options: dict[str, dict],
    *,
    price_history_root: pathlib.Path | str | None = None,
    asin: str = "",
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """テンプレート ``stock_where_to_buy`` コンテキストを組み立てる。"""
    block: dict[str, Any] = {
        "conclusion": build_conclusion(product_name, stock_obs, purchase_options),
        "rows": build_rows(stock_obs, purchase_options),
        "price_history_note": None,
        "low_stock_note": build_low_stock_note(stock_obs),
        "offline_note": build_offline_note(product_name),
    }
    if price_history_root is not None and asin:
        block["price_history_note"] = build_price_history_note(
            asin, price_history_root, now=now,
        )
    return block
