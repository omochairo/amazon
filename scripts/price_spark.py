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
    # --- 以下は #5167 で追加。既定値は商品ページ (ARTICLE_GEOM) の従来挙動 ---
    # y 軸ラベルの寄せ。商品ページはプロット左に置くので "end"、カードは右に
    # 置いて横幅を稼ぐので "start"。
    y_label_anchor: str = "end"
    # x 軸ラベルの日付書式。"iso" = 2026-08-14 (商品ページ)、
    # "short" = 8/14 (カード。桁数を削って小さい図でも読めるようにする)。
    # strftime の %-m はプラットフォーム依存なので自前で組む。
    date_style: str = "iso"
    # 折れ線の下を塗るか。カードでは「線が1本あるだけ」に見えるのを避けるため塗る。
    show_area: bool = False
    # 最新観測点にマーカーを打つか (「今どこにいるか」を示す)。
    show_last_marker: bool = False
    # 座標の小数桁数 (#5225)。カードは全ページに最大150枚並ぶので、points 文字列が
    # そのままページ重量になる。viewBox 220x72 を 220px で描くので 1 単位 = 約1px、
    # 整数に丸めても誤差は 0.5px 未満で見た目に影響しない。商品ページ (300x90、
    # 1ページ1枚) は従来どおり小数1桁のままにして出力を変えない。
    coord_precision: int = 1


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

# 一覧カード用 (#5167 で再設計)。
#
# 初版 (#5143) は「線だけ・軸ラベルなし・160x40」だった。これは失敗で、カード上では
# 意味の分からない線が1本あるだけに見え、読者に何も伝わっていなかった。
#
# 商品ページのグラフ (#5120) と一覧カードのグラフは目的が違う:
#   商品ページ … Keepa グラフの隣に置き、「このサイトは自前で価格を観測している」
#                という事実を検索エンジンと読者に示す証跡。日付も絶対値も要る
#   一覧カード … 「この商品の価格がどう動いて今いくらか」を一目で伝える。
#                カード内の限られた面積で、価格の上下と現在位置が読めればよい
#
# そこでカード版は「軸ラベルは残すが最小限、面で塗って形を読ませ、最新点を打つ」に
# する。y ラベルはプロットの右に置いて横幅を稼ぎ、日付は M/D に詰める。
CARD_GEOM = SparkGeom(
    width=220,
    height=72,
    plot_x_min=3.0,
    plot_x_max=163.0,
    plot_y_top=13.0,
    plot_y_bottom=47.0,
    gap_days=14,
    show_dots=False,          # 観測点を全部打つと小さい図では潰れるので最新点だけ
    show_labels=True,
    show_legend=False,        # 凡例の代わりに破線区間の説明は <desc> に持たせる
    y_label_x=168.0,
    y_label_max_y=17.0,
    y_label_min_y=51.0,
    x_label_y=63.0,
    legend_y=0.0,
    legend_dash_len=0.0,
    legend_text_gap=0.0,
    legend_text="",
    y_label_anchor="start",
    date_style="short",
    show_area=True,
    show_last_marker=True,
    coord_precision=0,
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
            "y_labels": [], "x_labels": [], "legend": None, "area": "", "last_point": None}


def _format_axis_date(dt: datetime, style: str) -> str:
    """x 軸の日付ラベル。``%-m`` 等はプラットフォーム依存なので自前で組む。"""
    if style == "short":
        return f"{dt.month}/{dt.day}"
    return dt.strftime("%Y-%m-%d")


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

    def _round(value: float) -> float | int:
        """#5225: geom.coord_precision に従って丸める。0 なら int にして
        points 文字列から不要な ".0" を消す (ページ重量が直接減る)。"""
        rounded = round(value, geom.coord_precision)
        return int(rounded) if geom.coord_precision == 0 else rounded

    def _y(price: int) -> float:
        if price_range == 0:
            return _round((y_top + y_bottom) / 2)
        return _round(y_top + (1 - (price - min_price) / price_range) * draw_h)

    def _x(index: int, dt: datetime) -> float:
        # 合計スパンが 0 秒のときだけ等間隔に落とす (ゼロ割り防止のガード)。
        if total_seconds <= 0:
            return _round(x_min + (index / (n - 1) * draw_w if n > 1 else 0.0))
        return _round(x_min + (dt - oldest_dt).total_seconds() / total_seconds * draw_w)

    coords = [(_x(i, dt), _y(price)) for i, (dt, price) in enumerate(parsed)]
    dots = [{"x": x, "y": y} for x, y in coords] if geom.show_dots else []

    y_labels = []
    if geom.show_labels:
        if price_range == 0:
            y_labels = [{
                "x": geom.y_label_x,
                "y": round((y_top + y_bottom) / 2, 1),
                "text": f"¥{min_price:,}",
                "anchor": geom.y_label_anchor,
            }]
        else:
            y_labels = [
                {"x": geom.y_label_x, "y": geom.y_label_max_y,
                 "text": f"¥{max_price:,}", "anchor": geom.y_label_anchor},
                {"x": geom.y_label_x, "y": geom.y_label_min_y,
                 "text": f"¥{min_price:,}", "anchor": geom.y_label_anchor},
            ]

    x_labels = []
    if geom.show_labels:
        x_labels = [
            {"x": x_min, "y": geom.x_label_y,
             "text": _format_axis_date(oldest_dt, geom.date_style), "anchor": "start"},
            {"x": x_max, "y": geom.x_label_y,
             "text": _format_axis_date(domain_end_dt, geom.date_style), "anchor": "end"},
        ]

    def _append_coord(coords_list: list[tuple[float, float]], xy: tuple[float, float]) -> None:
        # #5120 追補: 丸め後の座標が直前と完全一致するなら追加しない。価格が
        # 変わらない辺では step 頂点と実観測点が同一座標になり、そのまま積むと
        # 冗長な重複頂点が大量発生する (3,979 ページ分の無駄なマークアップ)。
        if coords_list and coords_list[-1] == xy:
            return
        coords_list.append(xy)

    def _area(path: list[tuple[float, float]]) -> str:
        """折れ線の下を塗る polygon の points。線を y 下端まで落として閉じる。

        破線 (未観測) 区間も含めた連続パスで塗る。塗りは「価格がどのあたりを
        推移したか」の形を読ませるためのもので、観測の有無は線種側で示す。
        """
        if not geom.show_area or len(path) < 2:
            return ""
        pts = list(path)
        # 底辺も同じ精度で丸める (float のままだと "47.0" が出て points が伸びる)。
        bottom = _round(y_bottom)
        pts.append((pts[-1][0], bottom))
        pts.append((pts[0][0], bottom))
        return " ".join(f"{x},{y}" for x, y in pts)

    if n < 2:
        cur_seg: list[tuple[float, float]] = [coords[0]]
        if extend:
            _append_coord(cur_seg, (_round(x_max), coords[0][1]))
        segments = [{"points": " ".join(f"{x},{y}" for x, y in cur_seg), "observed": True}] \
            if len(cur_seg) >= 2 else [{"points": f"{coords[0][0]},{coords[0][1]}", "observed": True}]
        last = {"x": cur_seg[-1][0], "y": cur_seg[-1][1]} if geom.show_last_marker else None
        return {"width": geom.width, "height": geom.height, "segments": segments,
                "dots": dots, "y_labels": y_labels, "x_labels": x_labels, "legend": None,
                "area": _area(cur_seg), "last_point": last}

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
    # full_path は破線区間も含めた1本の連続パス。面塗り (_area) 用に別途ためる。
    full_path: list[tuple[float, float]] = [coords[0]]

    for prev_xy, cur_xy, is_gap in edges:
        step_xy = (cur_xy[0], prev_xy[1])  # 前の価格を次の x まで水平に保持 (step-after)
        _append_coord(full_path, step_xy)
        _append_coord(full_path, cur_xy)
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
        _append_coord(cur_seg, (_round(x_max), cur_seg[-1][1]))
        _append_coord(full_path, (_round(x_max), full_path[-1][1]))

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

    last = {"x": full_path[-1][0], "y": full_path[-1][1]} if geom.show_last_marker else None
    return {"width": geom.width, "height": geom.height, "segments": segments,
            "dots": dots, "y_labels": y_labels, "x_labels": x_labels, "legend": legend,
            "area": _area(full_path), "last_point": last}


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
