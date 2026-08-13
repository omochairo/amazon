"""price_spark.py — 価格推移スパークラインのジオメトリ計算 (SSOT)。

#5120 / #5135 で商品ページ用に作り込んだ描画ロジックを、一覧ページ
(/price/ /deals/) のカードからも使えるように build_post.py から切り出したもの。
呼び出し側は build_post.py (ARTICLE_GEOM) / build_price_dashboard.py /
build_feature_lists.py (CARD_GEOM)。

ジオメトリをモジュール定数でなく SparkGeom で受け取るのは、商品ページ版
(軸ラベル・凡例・観測ドットあり、300x90) とカード版 (線だけ、160x40) で
同じ階段線ロジックを共有しつつ寸法だけ差し替えるため。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

# 一覧カードにスパークラインを出す最低条件 (#5120 の商品ページ側とは別基準)。
# CARD_MIN_POINTS: 2点では「線」にはなるが推移として読めない。
# CARD_MIN_DISTINCT_PRICES: 価格の種類が1つだと必ず水平な棒になり情報量ゼロ。
#   実測 (2026-08-13) では /deals/ 20件中13件がこれに該当したため、無条件描画だと
#   カードの2/3がただの横棒になる。
CARD_MIN_POINTS = 3
CARD_MIN_DISTINCT_PRICES = 2


@dataclass(frozen=True)
class SparkGeom:
    width: int
    height: int
    plot_x_min: float
    plot_x_max: float
    plot_y_top: float
    plot_y_bottom: float
    gap_days: int
    show_dots: bool
    show_labels: bool
    show_legend: bool
    y_label_x: float
    y_label_max_y: float
    y_label_min_y: float
    x_label_y: float
    legend_y: float
    legend_dash_len: float
    legend_text_gap: float
    legend_text: str


ARTICLE_GEOM = SparkGeom(
    width=300,
    height=90,
    plot_x_min=52.0,
    plot_x_max=296.0,
    plot_y_top=8.0,
    plot_y_bottom=56.0,
    gap_days=14,
    show_dots=True,
    show_labels=True,
    show_legend=True,
    y_label_x=48.0,
    y_label_max_y=11.0,
    y_label_min_y=59.0,
    x_label_y=74.0,
    legend_y=86.0,
    legend_dash_len=16.0,
    legend_text_gap=4.0,
    legend_text="破線＝未観測期間",
)

CARD_GEOM = SparkGeom(
    width=160,
    height=40,
    plot_x_min=2.0,
    plot_x_max=158.0,
    plot_y_top=4.0,
    plot_y_bottom=36.0,
    gap_days=14,
    show_dots=False,
    show_labels=False,
    show_legend=False,
    y_label_x=0.0,
    y_label_max_y=0.0,
    y_label_min_y=0.0,
    x_label_y=0.0,
    legend_y=0.0,
    legend_dash_len=0.0,
    legend_text_gap=0.0,
    legend_text="",
)


def parse_ts(ts: str) -> Optional[datetime]:
    """観測点の ``ts`` (ISO8601, ``Z`` 表記あり) を aware datetime にパースする。

    失敗したら None（呼び出し側でその点をスパーク描画から除外する）。
    """
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def empty_spark(geom: SparkGeom) -> dict[str, Any]:
    return {"width": geom.width, "height": geom.height, "segments": [], "dots": [],
            "y_labels": [], "x_labels": [], "legend": None}


def build_spark(points: list[dict[str, Any]], min_price: int, max_price: int,
                geom: SparkGeom,
                extend_to_dt: Optional[datetime] = None) -> dict[str, Any]:
    """#5120: SVG スパークラインの座標・軸ラベル・観測ドット・凡例をテンプレの
    外 (Python) で計算する。テンプレは受け取った文字列/座標をそのまま描画する
    だけにして、算術をテンプレ側に持たせない。

    x を「経過日数 (date, 日単位)」でなく秒解像度の ``ts`` に比例させる必要が
    あるのは、週1巡回 + 変化即追記のマージ実データで 335/1219 (27%) のページが
    同一日に 2 点以上の観測を持つため。date だけでは同日内の順序・間隔を
    復元できず点が重なる。#5120 の実測では等間隔描画が本来の位置から最大
    209px (描画幅296px中) ずれていた。

    折れ線を直線補間でなく階段 (step-after: 前の点の価格を次の観測まで
    水平に保持し、観測が来た瞬間に垂直移動) で描くのは、jsonl が dedupe を
    通った「価格の変化点」ログであり、観測点間の連続的な変化は記録されて
    いないため。直線補間は記録にない変化を描いてしまう。

    観測間隔が ``geom.gap_days`` (14日) を超える区間は「未観測」として別
    セグメントに分け、破線・低不透明度で描く。ただしギャップ辺の垂直移動
    (新観測時点で実際に変わった価格) は未観測ではないので、水平ホールド部分
    だけを破線側に、垂直部分は次の観測済みセグメントの先頭に入れる。

    ``dots`` は実観測点のみ (step-after で挿入した水平保持の中間頂点には
    打たない)。``geom.show_dots`` が False (カード版) なら空。

    ``extend_to_dt``: 「最終確認日まで価格が変わっていないことが latest.json で
    確定している」と呼び出し側が判定した場合にだけ渡される。渡されたら x 軸
    ドメインの終端をこの日時まで延ばし、最後の観測点から x 右端まで実線で
    水平に延長する (未観測ではなく確定した継続なので 14日ギャップ判定の対象外)。
    """
    x_min, x_max = geom.plot_x_min, geom.plot_x_max
    draw_w = x_max - x_min
    y_top, y_bottom = geom.plot_y_top, geom.plot_y_bottom
    draw_h = y_bottom - y_top

    parsed: list[tuple[datetime, int]] = []
    for pt in points:
        dt = parse_ts(pt["ts"])
        if dt is None:
            continue
        parsed.append((dt, pt["price"]))

    if not parsed:
        return empty_spark(geom)

    n = len(parsed)
    oldest_dt = parsed[0][0]
    newest_dt = parsed[-1][0]
    extend = extend_to_dt is not None and extend_to_dt > newest_dt
    domain_end_dt = extend_to_dt if extend else newest_dt
    total_seconds = (domain_end_dt - oldest_dt).total_seconds()
    price_range = max_price - min_price

    def _y(price: int) -> float:
        if price_range == 0:
            return round((y_top + y_bottom) / 2, 1)
        return round(y_top + (1 - (price - min_price) / price_range) * draw_h, 1)

    def _x(index: int, dt: datetime) -> float:
        # 合計スパンが 0 秒のときだけ等間隔に落とす (ゼロ割り防止のガード)。
        if total_seconds <= 0:
            return round(x_min + (index / (n - 1) * draw_w if n > 1 else 0.0), 1)
        return round(x_min + (dt - oldest_dt).total_seconds() / total_seconds * draw_w, 1)

    coords = [(_x(i, dt), _y(price)) for i, (dt, price) in enumerate(parsed)]
    dots = [{"x": x, "y": y} for x, y in coords] if geom.show_dots else []

    y_labels = []
    if geom.show_labels:
        if price_range == 0:
            y_labels = [{
                "x": geom.y_label_x,
                "y": round((y_top + y_bottom) / 2, 1),
                "text": f"¥{min_price:,}",
                "anchor": "end",
            }]
        else:
            y_labels = [
                {"x": geom.y_label_x, "y": geom.y_label_max_y,
                 "text": f"¥{max_price:,}", "anchor": "end"},
                {"x": geom.y_label_x, "y": geom.y_label_min_y,
                 "text": f"¥{min_price:,}", "anchor": "end"},
            ]

    x_labels = []
    if geom.show_labels:
        x_labels = [
            {"x": x_min, "y": geom.x_label_y, "text": oldest_dt.strftime("%Y-%m-%d"), "anchor": "start"},
            {"x": x_max, "y": geom.x_label_y, "text": domain_end_dt.strftime("%Y-%m-%d"), "anchor": "end"},
        ]

    def _append_coord(coords_list: list[tuple[float, float]], xy: tuple[float, float]) -> None:
        # #5120 追補: 丸め後の座標が直前と完全一致するなら追加しない。価格が
        # 変わらない辺では step 頂点と実観測点が同一座標になり、そのまま積むと
        # 冗長な重複頂点が大量発生する (3,979 ページ分の無駄なマークアップ)。
        if coords_list and coords_list[-1] == xy:
            return
        coords_list.append(xy)

    if n < 2:
        cur_seg: list[tuple[float, float]] = [coords[0]]
        if extend:
            _append_coord(cur_seg, (x_max, coords[0][1]))
        segments = [{"points": " ".join(f"{x},{y}" for x, y in cur_seg), "observed": True}] \
            if len(cur_seg) >= 2 else [{"points": f"{coords[0][0]},{coords[0][1]}", "observed": True}]
        return {"width": geom.width, "height": geom.height, "segments": segments,
                "dots": dots, "y_labels": y_labels, "x_labels": x_labels, "legend": None}

    edges = []
    for i in range(1, n):
        gap_days_val = (parsed[i][0] - parsed[i - 1][0]).total_seconds() / 86400.0
        edges.append((coords[i - 1], coords[i], gap_days_val > geom.gap_days))

    def _flush(segs: list[dict[str, Any]], coords_list: list[tuple[float, float]], observed: bool) -> None:
        if len(coords_list) < 2:
            return
        segs.append({
            "points": " ".join(f"{x},{y}" for x, y in coords_list),
            "observed": observed,
        })

    segments: list[dict[str, Any]] = []
    # cur_seg は常に「観測済み」の累積。破線セグメントはギャップ辺ごとに単発で挟まれる。
    cur_seg = [coords[0]]

    for prev_xy, cur_xy, is_gap in edges:
        step_xy = (cur_xy[0], prev_xy[1])  # 前の価格を次の x まで水平に保持 (step-after)
        if is_gap:
            _flush(segments, cur_seg, True)
            dashed: list[tuple[float, float]] = [prev_xy]
            _append_coord(dashed, step_xy)
            _flush(segments, dashed, False)
            cur_seg = [step_xy]
            _append_coord(cur_seg, cur_xy)
        else:
            _append_coord(cur_seg, step_xy)
            _append_coord(cur_seg, cur_xy)

    if extend:
        _append_coord(cur_seg, (x_max, cur_seg[-1][1]))

    _flush(segments, cur_seg, True)

    legend = None
    if geom.show_legend and any(not seg["observed"] for seg in segments):
        dash_x1 = x_min
        dash_x2 = x_min + geom.legend_dash_len
        legend = {
            "dash_x1": dash_x1,
            "dash_x2": dash_x2,
            "y": geom.legend_y,
            "text_x": dash_x2 + geom.legend_text_gap,
            "text": geom.legend_text,
        }

    return {"width": geom.width, "height": geom.height, "segments": segments,
            "dots": dots, "y_labels": y_labels, "x_labels": x_labels, "legend": legend}


def build_card_spark(history: list[tuple[datetime, int]]) -> Optional[dict[str, Any]]:
    """一覧カード (/price/ /deals/) 用のスパークラインを作る。描画に値しない
    履歴なら None を返す (呼び出し側は item に ``spark`` キーを付けない)。

    引数は ``build_price_dashboard.load_merged_history`` の戻り値そのまま
    (``list[tuple[datetime, int]]``、ts 昇順)。build_spark が期待する dict 列への
    変換をここ1箇所に閉じ込め、/price/ と /deals/ で採択条件がズレないようにする。
    """
    if len(history) < CARD_MIN_POINTS:
        return None
    prices = {price for _, price in history}
    if len(prices) < CARD_MIN_DISTINCT_PRICES:
        return None
    min_price, max_price = min(prices), max(prices)
    points = [{"ts": dt.isoformat(), "price": price} for dt, price in history]
    spark = build_spark(points, min_price, max_price, CARD_GEOM)
    # カードの文脈 (値下げ幅・現在価格の隣) で縦軸の範囲を示せるよう、
    # 描画には使わないが min/max を添える。
    spark["min_price"] = min_price
    spark["max_price"] = max_price
    return spark
