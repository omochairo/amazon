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
import re
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import price_overlay
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
    stock_status.STATE_PREORDER: "予約受付中",
    stock_status.STATE_UNKNOWN: "確認できず",
}

# 「この商品の発売予定日は2026年9月19日です。」から発売予定日を抜く (#5483)。
# 予約商品では「いつ届くか」が読者の判断材料そのものなので、状態だけでなく
# 日付まで出す。取れなければ日付なしの文に落とす (fail-soft)。
_RE_RELEASE_DATE = re.compile(r"発売予定日は\s*(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日")

# delayed のうち「日」より長い単位のもの (#5483)。既存の delayed 文言は
# 「発送に数日」で正しかったが、`通常1～2か月以内に発送します。` を delayed に
# 入れた結果、数日と言い切ると嘘になるケースが出た。観測した幅をそのまま出す。
_RE_DELAYED_LONG = re.compile(r"通常(\d+[~〜～\-−]\d+(?:週間|か月|ヶ月|カ月))以内に発送")

PRICE_HISTORY_WINDOW_DAYS = 30
# 過去 30 日の最安値メモを出すために最低限必要な観測点数 (ever・窓内外問わず)。
# 1 点しか無い場合は「価格推移」を語れる材料が無いとみなして出さない。
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

def _delayed_lead_label(raw_avail: Optional[str]) -> Optional[str]:
    """delayed のうち日単位を超えるものだけ、観測した所要幅 (例 "1～2か月") を返す。

    日単位 (`通常2～3日以内に発送します。`) は None を返し、従来の「数日」表現を
    そのまま使う — 既存 89 件の文言を 1 文字も変えないため。
    """
    if not isinstance(raw_avail, str):
        return None
    m = _RE_DELAYED_LONG.search(raw_avail)
    return m.group(1) if m else None


def _release_date_label(raw_avail: Optional[str]) -> Optional[str]:
    """``avail`` から発売予定日を「2026年9月19日」形式で返す。取れなければ None。"""
    if not isinstance(raw_avail, str):
        return None
    m = _RE_RELEASE_DATE.search(raw_avail)
    if not m:
        return None
    year, month, day = m.groups()
    return f"{year}年{int(month)}月{int(day)}日"


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
        lead = _delayed_lead_label(stock_obs.raw_avail)
        if lead:
            head = f"{date_label} 時点、Amazon に在庫あり（発送まで{lead}かかります）{price_part}。"
        else:
            head = f"{date_label} 時点、Amazon に在庫あり（発送までお時間をいただく場合があります）{price_part}。"
    elif state == stock_status.STATE_OUT_OF_STOCK:
        head = f"{date_label} 時点、Amazon は在庫切れです。"
    elif state == stock_status.STATE_PREORDER:
        # 予約は「買えるが、まだ手元には来ない」。在庫の言葉で語ると必ずどちらかに
        # 嘘が混じるので、発売予定日を主語にする (#5483)。
        release = _release_date_label(stock_obs.raw_avail)
        if release:
            head = f"{date_label} 時点、Amazon は予約受付中です（発売予定日 {release}）{price_part}。"
        else:
            head = f"{date_label} 時点、Amazon は予約受付中です（発売前）{price_part}。"
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

def _amazon_state_label(state: str, raw_avail: Optional[str] = None) -> str:
    if state == stock_status.STATE_DELAYED:
        lead = _delayed_lead_label(raw_avail)
        if lead:
            return f"発送に{lead}"
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
            state_label = _amazon_state_label(stock_obs.state, stock_obs.raw_avail)
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

def _load_amazon_observations(root: pathlib.Path | str, asin: str) -> list[dict[str, Any]]:
    """``<root>/<ASIN>.jsonl`` から source=amazon の観測を **在庫状態ごと** ts 昇順で
    返す。無い/壊れていればベストエフォートで空を返す。``root`` は
    ``data/price_watch/history/`` (日次) と ``data/price_history/`` (週次)
    のどちらでも呼べる (レコード形式は両レーン同一)。

    各要素は ``{"ts", "price" (int|None), "availability" (str|None), "buyable"}``。

    ``buyable`` = 「その時点で、その価格で実際に買えたと確認できた」。#5130 残件2 で
    追加した。価格の有無だけでは足りない 2 つの形があるため:

      - ``price: null`` + ``availability`` … 在庫切れ観測 (#5130 項目1 から記録)
      - **価格はあるが在庫が無い** … `一時的に在庫切れ; 入荷時期は未定です。` と
        価格が同時に載る形。実測 (2026-08-18) で全 38,666 行中 163 行

    後者が本 issue の核心で、価格だけ見ると「安くなった」ように読めるが誰も買え
    ない。判定は price_overlay に集約した SSOT を使う (/price/ /deals/ と同じ)。
    """
    if not asin:
        return []
    p = pathlib.Path(root) / f"{asin.upper()}.jsonl"
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
            ts = rec.get("ts")
            if not isinstance(ts, str) or not ts:
                continue
            price = rec.get("price")
            if not isinstance(price, int) or isinstance(price, bool) or price <= 0:
                price = None
            availability = rec.get("availability")
            if not isinstance(availability, str) or not availability.strip():
                availability = None
            if price is None and availability is None:
                # 価格も在庫根拠も無い行は、何も主張していないので読まない。
                continue
            points.append({
                "ts": ts,
                "price": price,
                "availability": availability,
                "buyable": price is not None
                and not price_overlay.is_explicitly_unavailable(availability),
            })
    except OSError:
        return []
    points.sort(key=lambda r: r["ts"])
    return points


def _load_amazon_price_points(root: pathlib.Path | str, asin: str) -> list[dict[str, Any]]:
    """価格が付いている観測点だけを ts 昇順で返す (在庫状態は見ない)。

    「価格推移を語れるだけの観測があるか」の門番 (PRICE_HISTORY_MIN_POINTS) に
    使う。門番は在庫と無関係な「記録の厚み」の判定なので、母数は従来どおり
    価格観測の全件にしておく。
    """
    return [{"ts": pt["ts"], "price": pt["price"]}
            for pt in _load_amazon_observations(root, asin) if pt["price"] is not None]


def _merge_price_points(*lanes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """複数レーンの観測点を (ts, price) 一致で dedupe して ts 昇順で返す。"""
    seen: set[tuple[str, Any]] = set()
    merged: list[dict[str, Any]] = []
    for lane in lanes:
        for pt in lane:
            key = (pt["ts"], pt["price"])
            if key in seen:
                continue
            seen.add(key)
            merged.append(pt)
    merged.sort(key=lambda r: r["ts"])
    return merged


def build_price_history_note(
    asin: str,
    price_history_root: pathlib.Path | str | None = None,
    *,
    price_watch_root: pathlib.Path | str | None = None,
    now: Optional[datetime] = None,
) -> Optional[str]:
    """過去 30 日の最安値メモ。ファイルが無い/窓内の点が足りない場合は None
    (テンプレ側はこの行を出さない)。

    #5130 残件2: ここでいう最安値は **実際に買えた時点の価格の最小値**。在庫が
    無かった期間の価格は候補に入れない (無限に高い価格が付いていたのと同じ扱い)。
    30 日のうち 1 日しか買えなかった商品でも、その 1 日の価格は候補に残す —
    短時間で売り切れるのは需要が高いからで、「その値段で買えた」ことは事実だから。

    実データ replay (2026-08-18, 8,824 ASIN):

      最安値が変わる …  3 ASIN (例 B0D7LBBD88: ￥1,264 → ￥1,800)
      note が消える  …  8 ASIN (窓内に買えた記録が 1 つも無い)
      変化なし       … 5,921 ASIN

    B0D7LBBD88 は 7/19 に ￥1,800 (通常発送) で観測されたあと、8/6 に ￥1,264 が
    付いたが `一時的に在庫切れ; 入荷時期は未定です。` だった。旧実装はこの
    ￥1,264 を最安値として出しており、**誰も買えなかった価格**を提示していた。

    2 レーン (``data/price_watch/history/`` 日次 / ``data/price_history/``
    週次) を **マージ** して読む。

    #5011 では「watch を第一参照にし、無いときだけ history へフォールバック」
    という file 単位の選択にしていた (single-writer 原則の read 側鏡像のつもり
    だった)。これは誤り。single-writer は *書き込み* 先を分ける制約であって、
    読む側が片方を捨てる理由にはならない。

    実測 (2026-08-12): 共通 1,854 ASIN のうち 1,826 で 2 レーンの (日付, 価格)
    集合が食い違う。dedupe (同一価格が 6 日未満なら書かない) が両レーンで独立に
    効くため位相がずれ、日付ベースで watch 専用 8,754 点 / history 専用 6,945 点 /
    共通 1,542 点と、両者はほぼ相補だった。フォールバックは片側の観測点を丸ごと
    捨てており、実データ replay では 96 ASIN で実際より高い最安値を出し、
    129 ASIN で出せるはずの note を落としていた (逆向き = マージで最安値が
    上がるケースは 0 件)。

    マージの dedupe は (ts, price) の完全一致のみ。同一観測点が両ディレクトリに
    独立に書かれることは無いはずだが、``build_price_dashboard.load_merged_history``
    と同じ保険を掛けて挙動を揃える。
    """
    observations = _merge_price_points(
        _load_amazon_observations(price_watch_root, asin) if price_watch_root is not None else [],
        _load_amazon_observations(price_history_root, asin) if price_history_root is not None else [],
    )
    if sum(1 for pt in observations if pt["price"] is not None) < PRICE_HISTORY_MIN_POINTS:
        # 観測点そのものが少なすぎる (=単発の値しか知らない) ときは、
        # 「価格推移」を語れるだけの材料が無いので出さない。門番は在庫と無関係な
        # 「記録の厚み」の判定なので、母数は価格観測の全件のまま。
        return None
    ref_now = now if now is not None else datetime.now(timezone.utc)
    if ref_now.tzinfo is None:
        ref_now = ref_now.replace(tzinfo=timezone.utc)
    cutoff = ref_now - timedelta(days=PRICE_HISTORY_WINDOW_DAYS)

    # #5011: 記録は price_history.append_price_point の dedupe (同一価格が
    # 6日未満続く場合は書かない) を通っているため、各行は「日次サンプル」
    # ではなく「価格の変化点」。過去30日の最安値を出すには階段関数として
    # 解釈する必要がある — 窓の開始時点で有効だった価格は、窓より前の
    # 直近の変化点 (=carry) にあることが多い。それを候補に含め忘れると、
    # 「窓の直前に安くなって窓の間ずっとその値段だった」ケースを取りこぼす。
    before_window = [pt for pt in observations if (_parse_iso(pt["ts"]) or cutoff) < cutoff]
    within_window = [pt for pt in observations if (_parse_iso(pt["ts"]) or cutoff) >= cutoff]

    # #5130 残件2: 「買えなかった期間は最安値の母数に入れない」。
    #
    # 在庫が無い期間の価格は、階段関数として素直に伸ばすと「その値段で売っていた」
    # という主張になるが、誰も買えていない。読者にとっての最安値は **実際に買えた
    # 時点の価格の最小値** なので、買えない観測は候補から外す (= 無限に高い価格が
    # 付いていたのと同じ扱いにする)。
    #
    # 逆に、30 日のうち 1 日しか買えなかった商品でも、その 1 日の価格は候補に残す。
    # 短時間で売り切れる商品はそれだけ需要が高く、「その値段で買えた」ことは事実
    # だから。窓内の買えた観測は期間の長さに関係なく全て数える。
    candidates: list[int] = [pt["price"] for pt in within_window if pt["buyable"]]
    # 除外した価格 = 「買えていれば最安値になりえた」もの。注記を出すかの判定に使う。
    rejected: list[int] = [pt["price"] for pt in within_window
                           if not pt["buyable"] and pt["price"] is not None]
    if before_window:
        last_before = before_window[-1]
        if last_before["buyable"]:
            candidates.append(last_before["price"])  # carry: 窓開始時点で有効だった価格
        elif last_before["price"] is not None:
            # 窓に入る前から買えない状態が続いていた。carry を足すと「窓の間ずっと
            # この値段で売っていた」という、記録と正反対の主張になる。
            rejected.append(last_before["price"])
    if not candidates:
        # 窓内に「買えた」と言える記録が 1 つも無い。出せる最安値が無いので黙る。
        # 在庫が無いこと自体は conclusion 行が取得日つきで述べているので、
        # ここで代わりに何かを書く必要は無い。
        return None
    min_price = min(candidates)
    if not any(price < min_price for price in rejected):
        # 除外した価格が全て最安値以上なら、除外の有無で読者に見える数字は変わらない。
        # 注記は「なぜ表より高い数字が出ているのか」を説明するためのものなので、
        # 説明する食い違いが無いときは出さない (実測 8,824 ASIN 中 132 件がこれ)。
        return (f"過去{PRICE_HISTORY_WINDOW_DAYS}日間の本サイト計測では、"
                f"Amazon 最安値は ￥{min_price:,} でした。")
    # ここに来るのは「在庫切れ中により安い価格が付いていた」ケース。注記を書かないと、
    # 表に出ている価格 (在庫切れでも価格は載る) より最安値のほうが高いという一見
    # 矛盾した並びになり、読者が理由を追えない。
    # 実例 (B0D7LBBD88): 表は ￥1,264 / 在庫切れ、最安値は買えた最後の ￥1,800。
    return (f"過去{PRICE_HISTORY_WINDOW_DAYS}日間の本サイト計測では、"
            f"Amazon 最安値は ￥{min_price:,} でした"
            f"（在庫切れを確認した期間は除いています）。")


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

# #5011 オーナー指摘: Google 検索への導線はアフィリエイトを産まず、検索から
# 検索へ送り返すだけの無駄な導線になる。Amazon/楽天/Yahoo のいずれかで取扱が
# 確認できていればテーブル (アフィリエイトリンク付き) に読者を戻し、外部
# 検索エンジンへのリンクは一切置かない。3 サイトとも取扱が確認できないとき
# だけ、消極的な受け皿としてトイザらスの在庫検索へリンクする
# (https://www.toysrus.co.jp/search/?q=<keyword> — 到達確認済み。他チェーンは
# URL を確認できていないため追加しない)。
_TOYSRUS_SEARCH_URL = "https://www.toysrus.co.jp/search/?q={q}"


def build_offline_note(product_name: str, purchase_options: dict[str, dict] | None = None) -> str:
    """実店舗の在庫は確認していない旨を明記する。

    - Amazon/楽天/Yahoo のいずれかで取扱が確認できている場合: 外部検索リンクは
      置かず、上の在庫・価格テーブルに戻るよう案内する。
    - 3 サイトとも取扱が確認できない場合のみ: 消極的な受け皿としてトイザらスの
      在庫検索へリンクする (アフィリエイトタグなし、rel=noopener nofollow)。

    どちらの分岐でも、特定チェーンの在庫を断定する文言 (「〜に在庫あり」等)
    は絶対に含めない。
    """
    purchase_options = purchase_options or {}
    any_available = any((purchase_options.get(site) or {}).get("available") for site in _SITE_ORDER)

    base = (
        "本サイトはオンラインストア（Amazon・楽天市場・Yahoo!ショッピング）の在庫のみを"
        "確認しています。トイザらス・イオンなど実店舗の在庫データは持っていないため、"
        "実店舗の在庫は各店舗へ直接お問い合わせください。"
    )
    if any_available:
        return base + "オンラインでの購入は上記の在庫・価格の一覧からご確認いただけます。"

    q = urllib.parse.quote(product_name or "")
    url = _TOYSRUS_SEARCH_URL.format(q=q)
    return (
        base
        + f"（<a href=\"{url}\" target=\"_blank\" rel=\"noopener nofollow\">"
        "トイザらス オンラインストアで検索する →</a>）"
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
    price_watch_root: pathlib.Path | str | None = None,
    asin: str = "",
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """テンプレート ``stock_where_to_buy`` コンテキストを組み立てる。

    ``price_watch_root`` (日次レーン) と ``price_history_root`` (週次レーン) は
    両方渡してよく、``build_price_history_note`` 側でマージされる。片方だけを
    渡す呼び出し元はそのレーンのみで動く (後方互換)。
    """
    block: dict[str, Any] = {
        "conclusion": build_conclusion(product_name, stock_obs, purchase_options),
        "rows": build_rows(stock_obs, purchase_options),
        "price_history_note": None,
        "low_stock_note": build_low_stock_note(stock_obs),
        "offline_note": build_offline_note(product_name, purchase_options),
    }
    if (price_history_root is not None or price_watch_root is not None) and asin:
        block["price_history_note"] = build_price_history_note(
            asin, price_history_root,
            price_watch_root=price_watch_root, now=now,
        )
    return block
